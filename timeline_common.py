"""i2v_timeline_cliV5.py / t2v_timeline_cliV5.py の共通コード(2026-07-08新設)。

V4までは`i2v_timeline_cliV4.py`/`t2v_timeline_cliV4.py`がそれぞれ独立にコピーされ、
パーサー・Pass0(Creative Director)/Pass1(Shot Director)/Pass1.5(Variety Auditor)・
`_enforce_*`群・キャラ/STATE周りの決定論チェックが二重管理になっていた(CLAUDE.mdの
「既知の負債」参照)。両ファイルの全関数を突き合わせ、本体ロジックが完全に一致する
ものだけをここに抽出する。

以下は意図的に含めない(i2v/t2vで構造・ロジックが本質的に異なるため):
  - Pass3プロンプト整形(i2v: keyframe+motionの二本立て / t2v: 単一LTXプロンプト)
  - 検品/lint系(`_detect_violations`・`_LINT_CHECKS`・`_lint_ltx_prompt`はチェック項目
    自体が異なる。i2v/t2vそれぞれのファイルに残す)
  - オーケストレーション(`main()`・`_run_retry`・セグメント処理本体はi2vのフェーズ1/2分割と
    t2vの単一フェーズで構造が異なる)

Pass0/1/1.5/2のシステムプロンプト定数(`_CREATIVE_DIRECTOR_SYSTEM`等)もi2v/t2vで内容が
実際に食い違っている(t2vが2026-07-05の一部シーン中立化・言い回し改善を受け取っていない
箇所がある)ため、関数本体だけをここに置き、システムプロンプト文字列は各V5ファイル側で
保持して引数として渡す(黙って統合しない — 中身が同じ関数コードの重複だけを解消する)。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from openai import AsyncOpenAI

import pipeline_config as cfg
from comfyui_client import upload_video_to_comfyui, upscale_video

# Pass4系lint(_lint_ltx_prompt・KF1/KF2・motion側lint)の最大リトライ回数。
# 従来は「検出→fix1回→残り違反はログのみ」で、fixerが従わなければ直っていないテキストが
# そのまま採用されていた(2026-07-10、C16 HAND BUDGETが実データで未解消のまま配信された
# 事故で発覚)。ユーザー提案「治るまでloopすれば」を受け、検出→fixを最大この回数まで繰り返し、
# 毎回「まだ残っている違反だけ」を次のfixerに渡す(直った分は自然に外れ、fixerの負担が
# 試行ごとに減っていく)。2回連続で違反集合が変化しなければ(進展なし)早期に打ち切るため、
# 上限を上げても「本当に直らない」ケースの無駄打ちは増えない——増分進展が続く限り粘れる
# 回数だけが伸びる。「ここが肝」というユーザー判断で3→5に引き上げた(2026-07-10)
_LINT_MAX_ATTEMPTS = 5


def _fmt_elapsed(start_time: float) -> str:
    """time.monotonic()基準の経過時間を「M分S秒」形式にする(ファイルmtimeからの逆算は誤差が出るため、実測用)。"""
    import time
    total_s = int(time.monotonic() - start_time)
    m, s = divmod(total_s, 60)
    return f"{m}分{s}秒"


def _fmt_duration(seconds: float) -> str:
    """秒数(time.monotonic()の差分)を「M分S秒」形式にする(LLM時間/生成時間の内訳表示用、2026-07-11)。"""
    total_s = int(seconds)
    m, s = divmod(total_s, 60)
    return f"{m}分{s}秒"


# ============================================================
# パーサー / ファイル出力ユーティリティ
# ============================================================

def _parse_prompt(text: str) -> tuple[str, list[dict], str]:
    """プロンプトを全体・Timelineセグメント・Ambienceに分解する。

    Timeline形式(混在可):
      Format A: "0–2s: description"                  (秒・1行)
      Format B: "00:00–00:03 → description"          (MM:SS・1行・矢印。矢印/コロンは省略可、
                 "00:00–00:03 description" のような最も素直な書き方も同じ枝で拾う)
      Format C: "00:00–00:02" 次行以降に説明          (MM:SS・複数行)
    "Timeline:" ヘッダー行は省略可(最初のタイムスタンプ前を全体とみなす)
    Ambience/Audio セクション名はどちらも可
    """
    _TS_A   = re.compile(r"^(\d+)[–\-](\d+)s:\s*(.+)")
    # 矢印(→/>)・コロンは任意。無ければ空白で区切られた説明文をそのまま拾う
    # ("00:00–00:03 description" — 矢印無しの最も普通な書き方、2026-07-16ユーザー報告)。
    # 説明文が無い行(Format Cの単独タイムスタンプ)は `(.+)` が1文字以上を要求するため
    # ここにはマッチせず、下のFormat Cへ正しく流れる
    _TS_B   = re.compile(r"^(\d{1,2}):(\d{2})[–\-](\d{1,2}):(\d{2})\s*(?:[→>:]\s*)?(.+)")
    _TS_C   = re.compile(r"^(\d{1,2}):(\d{2})[–\-](\d{1,2}):(\d{2})\s*$")
    _TS_D   = re.compile(r"^\[(\d{1,2}):(\d{2})[–\-](\d{1,2}):(\d{2})\]\s*(.+)")   # Format D: "[0:03–0:06] desc"
    _TS_ANY = re.compile(r"^\[?\d{1,2}:\d{2}[–\-]\d{1,2}:\d{2}|^\d+[–\-]\d+s:")

    # "Timeline:" ヘッダーがあればそこを境界に、なければ最初のタイムスタンプ行を起点とする
    timeline_m = re.search(r"(?i)^timeline:\s*$", text, re.MULTILINE)
    if timeline_m:
        global_desc    = text[:timeline_m.start()].strip()
        after_timeline = text[timeline_m.end():].strip()
    else:
        ts_start = re.search(r"(?m)^\*{0,2}(\[?\d{1,2}:\d{2}[–\-]\d{1,2}:\d{2}|\d+[–\-]\d+s:)", text)
        if not ts_start:
            raise ValueError("'Timeline:' ヘッダーもタイムスタンプも見つかりません")
        global_desc    = text[:ts_start.start()].strip()
        after_timeline = text[ts_start.start():].strip()

    # Ambience / Audio セクションを抽出(内容が同一行・次行どちらも対応)
    ambience_m = re.search(r"(?i)^(ambience|audio)[^:]*:", after_timeline, re.MULTILINE)
    ambience = ""
    timeline_text = after_timeline
    if ambience_m:
        ambience      = after_timeline[ambience_m.start():].strip()
        timeline_text = after_timeline[:ambience_m.start()].strip()

    segments: list[dict] = []
    lines = timeline_text.split("\n")
    i = 0
    while i < len(lines):
        # Markdown bold/italic (**text** や *text*) をタイムスタンプ前後から除去
        line = re.sub(r"^\*+|\*+$", "", lines[i].strip()).strip()

        # Format A: "0–2s: description"
        m = _TS_A.match(line)
        if m:
            start_s, end_s = int(m.group(1)), int(m.group(2))
            segments.append({"start": start_s, "end": end_s, "duration": end_s - start_s,
                              "action": m.group(3).strip(), "label": f"{m.group(1)}-{m.group(2)}s"})
            i += 1; continue

        # Format B: "00:00–00:03 → description"
        m = _TS_B.match(line)
        if m:
            start_s = int(m.group(1)) * 60 + int(m.group(2))
            end_s   = int(m.group(3)) * 60 + int(m.group(4))
            segments.append({"start": start_s, "end": end_s, "duration": end_s - start_s,
                              "action": m.group(5).strip(),
                              "label": f"{m.group(1)}:{m.group(2)}-{m.group(3)}:{m.group(4)}"})
            i += 1; continue

        # Format D: "[0:03–0:06] description"
        m = _TS_D.match(line)
        if m:
            start_s = int(m.group(1)) * 60 + int(m.group(2))
            end_s   = int(m.group(3)) * 60 + int(m.group(4))
            segments.append({"start": start_s, "end": end_s, "duration": end_s - start_s,
                              "action": m.group(5).strip(),
                              "label": f"{m.group(1)}:{m.group(2)}-{m.group(3)}:{m.group(4)}"})
            i += 1; continue

        # Format C: "00:00–00:02" 単独行 → 次行以降を説明として収集
        m = _TS_C.match(line)
        if m:
            start_s = int(m.group(1)) * 60 + int(m.group(2))
            end_s   = int(m.group(3)) * 60 + int(m.group(4))
            label   = f"{m.group(1)}:{m.group(2)}-{m.group(3)}:{m.group(4)}"
            i += 1
            action_lines: list[str] = []
            while i < len(lines):
                nl = re.sub(r"^\*+|\*+$", "", lines[i].strip()).strip()
                if _TS_ANY.match(nl):
                    break
                if nl:
                    action_lines.append(nl)
                i += 1
            segments.append({"start": start_s, "end": end_s, "duration": end_s - start_s,
                              "action": " ".join(action_lines), "label": label})
            continue

        i += 1

    if not segments:
        raise ValueError("Timeline セグメントが見つかりません(対応形式: '0–2s: desc' / '00:00–00:02 desc' / '00:00–00:02 → desc' / '00:00–00:02\\ndesc')")

    return global_desc, segments, ambience


_AUTO_SEGMENT_TARGET_DURATION_S = 15
_AUTO_SEGMENT_SYSTEM = """\
You convert a single flowing narrative-style video prompt (Seedance-style, no timeline) \
into: (1) one GLOBAL description paragraph covering the recurring character's appearance, \
the location/setting, and the visual/camera style (gather these from wherever they appear \
in the narrative, including trailing style clauses) — written as flowing prose, no timestamps; \
(2) an ordered list of shot beats that together tell the same story, each as one line \
"N. (Xs) action description", where X is a whole-second duration of **3 seconds or more \
— never less than 3**. Group fine-grained micro-actions together into a single 3-5s beat \
rather than splitting them into sub-3s beats. Default to 3 seconds; use 4-5 seconds for a beat \
that needs to linger (e.g. a finale) or that bundles multiple small actions.

COMPLETENESS OVER LENGTH: each shot beat is generated independently afterward (not as one \
continuous {target_duration}s clip), so there is NO strict total duration limit — a {target_duration}s \
narrative is only a loose reference point, not a budget to compress into. Covering the ENTIRE \
narrative from beginning to end matters far more than hitting a specific total length. NEVER omit, \
compress away, or truncate any part of the story to fit a duration target — this includes the \
ending/climax/final beat (e.g. a celebration, a punchline, a resolution) and any quoted dialogue. \
If the story has many distinct beats, use more beats (each still 3s+) rather than dropping content.

Split at natural story beats (a new action, a new emotional turn, a scene change) — do not \
arbitrarily chop mid-action. Keep each action line self-contained (a shot generator will see ONLY \
that one line, not the others), so repeat any character/location detail that specific beat depends \
on if the narrative implies it (e.g. "the villain" once introduced can be referred to briefly). \
Preserve any quoted dialogue verbatim in the beat it belongs to.

Output EXACTLY this format, nothing else:
GLOBAL:
<paragraph>
SEGMENTS:
1. (Xs) <action>
2. (Xs) <action>
...
"""


async def _auto_segment_narrative(text: str) -> str:
    """タイムライン無しの1本の物語文を、既存の`_parse_prompt()`がそのまま読める
    Timeline形式(Format B)のテキストへ変換する(2026-07-08新設、Pass -1)。
    タイムスタンプの加算はLLMに任せず、durationのみLLMに出させてPython側で計算する
    (このプロジェクト全体の「整形はPython、生成はLLM」という設計と一貫)。
    i2v/t2vで処理内容を変える理由が無いためsystem_promptは引数化せず内蔵する。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": _AUTO_SEGMENT_SYSTEM.format(target_duration=_AUTO_SEGMENT_TARGET_DURATION_S)},
            {"role": "user", "content": f"/no_think\n{text.strip()}"},
        ],
        temperature=0.4,
        max_tokens=4096,
    )
    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        raw = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()

    m = re.search(r"GLOBAL:\s*(.*?)\s*SEGMENTS:\s*(.*)", raw, re.DOTALL | re.IGNORECASE)
    if not m:
        raise ValueError(f"Pass -1 の出力形式が不正です(GLOBAL:/SEGMENTS: が見つかりません):\n{raw}")
    global_text = m.group(1).strip()
    body = m.group(2)

    beats: list[tuple[int, str]] = []
    for line in body.split("\n"):
        lm = re.match(r"^\d+[\.\:]\s*\((\d+)s\)\s*(.+)", line.strip())
        if lm:
            beats.append((int(lm.group(1)), lm.group(2).strip()))
    if not beats:
        raise ValueError(f"Pass -1 でセグメントを1件も抽出できませんでした:\n{raw}")

    # 各ビートは3秒以上(ユーザー方針: 細かい動作単位に割らず3秒以上でまとめる)。LLMが指示を
    # 無視して短いビートを出した場合の決定論的フロア。合計尺の上限は設けない
    # (各ビートは独立生成なので、物語を最後まで欠落なくカバーすることを尺より優先する、というユーザー方針)
    beats = [(max(3, d), a) for d, a in beats]

    def _fmt_ts(sec: int) -> str:
        return f"{sec // 60:02d}:{sec % 60:02d}"

    lines = []
    t = 0
    for dur, action in beats:
        lines.append(f"{_fmt_ts(t)}–{_fmt_ts(t + dur)} → {action}")
        t += dur

    return f"{global_text}\n\nTimeline:\n" + "\n".join(lines)


def _parse_numbered_lines(raw: str, count: int, fallback: str) -> list[str]:
    """LLM出力の番号付きリストをパースし、不足分はfallbackで埋める。
    区切り文字の直後に空白を1つ以上要求する(2026-07-10修正)。旧`\\s*`だと"16:9 WIDESCREEN
    LANDSCAPE"のようなアスペクト比表記("16"+":"+"9…")まで誤って1項目としてマッチしてしまい、
    Shot DirectorがオリエンテーションをそのままLLM出力の先頭行に書いた場合に本来の番号付き
    リストが1件ずつ後ろへずれ、結果として全セグメントのカメラ方向が1つ前のセグメント用の
    ものと入れ替わる事故が実データで発覚した(bus.txt run、セグメント5の屋内シーンに
    セグメント4の傘が混入)。"""
    items: list[str] = []
    for line in raw.split("\n"):
        m = re.match(r"^\d+[\.\:]\s+(.+)", line.strip())
        if m:
            items.append(m.group(1).strip())
    while len(items) < count:
        items.append(fallback)
    return items[:count]


def _seg_video_path(run_id: str, num: int, label: str, prefix: str) -> Path:
    """出力prefixはi2v/t2vそれぞれのV5ファイル側から渡す(i2v5/t2v5)。"""
    return cfg.GENERATED_DIR / f"{prefix}_{run_id}_seg{num:02d}_{label}.mp4"


def _split_direct_prompt(text: str) -> tuple[str, str]:
    """--directモード用: ファイルに`--- Keyframe prompt ---`区切りがあれば
    (keyframe_prompt, motion_prompt)に分割し、無ければ全文を両方に使う
    (i2vのみ意味を持つ。t2vは返り値の2要素目だけ使う)。既存prompts.txtの
    `--- Keyframe prompt ---`/`--- LTX prompt ---`表記をそのまま流用し、
    パース用の新しい正規表現は増やさない。"""
    m = re.search(r"---\s*Keyframe prompt\s*---\s*\n(.*?)\n---\s*LTX prompt\s*---\s*\n(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    whole = text.strip()
    return whole, whole


def _write_direct_prompts_txt(
    prompts_txt: Path, prompt_path: Path, orientation: str, width: int, height: int,
    duration: float, main_prompt: str, keyframe_prompt: str | None = None,
) -> None:
    """--directモード用: LLMパイプラインを経由しないため、通常のprompts.txtより
    ヘッダー・本文とも簡素だが、既存の`_parse_prompts_txt`(t2v/i2v各CLI)・
    Node.jsハーネスの`_PROMPTS_SEG_HEADER_RE`が読める形式は完全に維持する
    (`[1/1] direct (Ns)` + `--- LTX prompt ---`はそのまま既存正規表現に一致)。
    `direct: true`ヘッダーは--retry時に--segを省略可能にするための目印。"""
    lines = [
        f"source: {prompt_path.name}",
        f"orientation: {orientation}",
        f"size: {width}x{height}",
        f"auto_segmented: false",
        f"direct: true",
        "",
        f"[1/1] direct ({int(duration)}s)",
        "Intent: (direct mode — raw passthrough, no LLM)",
        "Camera: (n/a)",
    ]
    if keyframe_prompt is not None:
        lines.append(f"--- Keyframe prompt ---\n{keyframe_prompt}")
    lines.append(f"--- LTX prompt ---\n{main_prompt}\n")
    prompts_txt.write_text("\n".join(lines), encoding="utf-8")


async def _run_upscale(prefix: str, run_id: str | None, log_prefix: str) -> None:
    """既存runの最終動画(`{prefix}_{run_id}_final.mp4`)をRTX Video Super Resolutionで
    フルHD相当にアップスケールする(2026-07-08、`--retry`と同じ「id指定 or 省略で直近run」
    パターン)。毎回のアップスケールは不要で、SNS投稿など必要な時だけ手動で呼ぶ運用のため
    `main()`のフローに組み込まず、生成本体とは独立したコマンドにしている。
    i2v/t2vで呼び出し方が完全に同一(ロジックの違いがない)ため、`_concat_segments`同様に
    ここへ共通実装として置く。"""
    if run_id:
        run_id = re.sub(rf"^{re.escape(prefix)}_", "", run_id)
        run_id = re.sub(r"_final(_FHD)?\.mp4$", "", run_id)
        run_id = re.sub(r"_prompts\.txt$", "", run_id)
        final = cfg.GENERATED_DIR / f"{prefix}_{run_id}_final.mp4"
        if not final.exists():
            print(f"{log_prefix} 最終動画が見つかりません: {final}")
            return
    else:
        cands = list(cfg.GENERATED_DIR.glob(f"{prefix}_*_final.mp4"))
        if not cands:
            print(f"{log_prefix} generated/ に {prefix}_*_final.mp4 が見つかりません(--upscale RUN_ID で指定してください)")
            return
        final = max(cands, key=lambda p: p.stat().st_mtime)
        print(f"{log_prefix} --upscale 省略: 直近のrunを使用 → {final.name}")

    print(f"{log_prefix} アップロード中: {final.name}")
    server_name = await upload_video_to_comfyui(final)
    print(f"{log_prefix} アップスケール中(RTX Video Super Resolution)...")
    upscaled = await upscale_video(server_name)
    dest = final.with_name(final.stem + "_FHD" + final.suffix)
    upscaled.rename(dest)
    print(f"{log_prefix} 完了: {dest}")


_FADE_OUT_DURATION_S = 1.0


def _concat_segments(seg_paths: list[Path], final: Path, log_prefix: str) -> bool:
    """セグメント動画群をffmpegで連結する。成功でTrue。
    cfg.FADE_OUT_ENABLED(既定True)の場合、末尾に映像・音声とも1秒のフェードアウトを付加する
    (2026-07-08)。フェード付加は-c copyのstream copyとフィルタを併用できないため、まず
    stream copyで高速連結してから、フェード区間のみ再エンコードする2段構成にしている。"""
    filelist = final.with_suffix(".filelist.txt")
    filelist.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in seg_paths),
        encoding="utf-8",
    )
    concat_target = final.with_name(final.stem + "_raw" + final.suffix) if cfg.FADE_OUT_ENABLED else final
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(filelist), "-c", "copy", str(concat_target)],
        capture_output=True, text=True,
    )
    filelist.unlink()
    if result.returncode != 0:
        print(f"{log_prefix} ffmpegエラー(連結):\n{result.stderr}")
        return False

    if not cfg.FADE_OUT_ENABLED:
        return True

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(concat_target)],
        capture_output=True, text=True,
    )
    try:
        total_duration = float(probe.stdout.strip())
    except ValueError:
        print(f"{log_prefix} ffprobeエラー(尺取得):\n{probe.stderr}")
        return False

    fade_start = max(0.0, total_duration - _FADE_OUT_DURATION_S)
    fade_result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(concat_target),
         "-vf", f"fade=t=out:st={fade_start}:d={_FADE_OUT_DURATION_S}",
         "-af", f"afade=t=out:st={fade_start}:d={_FADE_OUT_DURATION_S}",
         "-c:v", "libx264", "-c:a", "aac", str(final)],
        capture_output=True, text=True,
    )
    concat_target.unlink(missing_ok=True)
    if fade_result.returncode != 0:
        print(f"{log_prefix} ffmpegエラー(フェード):\n{fade_result.stderr}")
        return False
    return True


# ============================================================
# 単語境界・動物検出
# ============================================================

def _has_word(text: str, word: str) -> bool:
    """単語境界での存在判定(catchesバグ類の防止: "heel" in "wheel" 等の部分文字列誤ヒット対策)。"""
    return bool(re.search(r"\b" + re.escape(word) + r"s?\b", text))


_ANIMAL_WORDS = ("cat", "kitten", "dog", "puppy", "bird")
# 部分文字列誤マッチ対策("catches"の中の"cat"等)。動物判定は必ずこのregexで行う
_ANIMAL_RE = re.compile(r"\b(cat|kitten|dog|puppy|bird)s?\b", re.IGNORECASE)


def _animals_in(text: str) -> list[str]:
    """text中の動物語(単語境界)を返す。"""
    return [m.group(1).lower() for m in _ANIMAL_RE.finditer(text)]


# 「動物がまだ登場していない」物語の検出(この書き方だと動物が生成されない)
_ANIMAL_ABSENT_RE = re.compile(
    r"where the (?:cat|dog|bird|animal) will|waiting for the (?:cat|dog|bird|animal)|"
    r"for the approaching (?:cat|dog|bird|animal)|(?:cat|dog|bird|animal)[^.]{0,30}\bwill appear|"
    r"(?:cat|dog|bird|animal) (?:is )?(?:about to|yet to) (?:appear|arrive)",
    re.IGNORECASE,
)

_ANIMAL_BEHIND_RE = re.compile(
    r"(closely\s+)?behind\s+(her|his|the\s+subject's|the)\s+(left\s+|right\s+)?(heels?|foot|feet|ankles?|legs?)",
    re.IGNORECASE,
)
# 「(alongside) closely behind」のように足部位を伴わない後方表現も対象
_ANIMAL_BEHIND_LOOSE_RE = re.compile(
    r"\b(trots|walks|follows|pads|moves)\s+(alongside\s+)?closely\s+behind\b",
    re.IGNORECASE,
)


def _enforce_animal_beside(ltx_prompt: str) -> str:
    """動物が「踵の後ろ」に配置されると生成されないため、「足の横」に機械置換する(2026-07-04)。"""
    if not _animals_in(ltx_prompt):
        return ltx_prompt
    ltx_prompt = _ANIMAL_BEHIND_RE.sub("beside her feet, fully visible in frame", ltx_prompt)
    return _ANIMAL_BEHIND_LOOSE_RE.sub(r"\1 beside her, fully visible in frame,", ltx_prompt)


_AUDIO_HEADER_RE = re.compile(
    r"((?:Audio|[Aa]mbient\s+(?:sounds?|audio))(?:\s*[:：]\s*|\s+include[sd]?\s*))",
    re.IGNORECASE,
)


def _enforce_animal_sound(ltx_prompt: str, scene_desc: str) -> str:
    """動物が映っているのに鳴き声がAudioセクションにない場合、先頭に追加する。"""
    _ANIMAL_SOUNDS: list[tuple[list[str], str, list[str]]] = [
        (["cat", "kitten", "feline"], "cat meowing", ["meow", "purr"]),
        (["dog", "puppy", "canine"], "dog barking", ["bark", "woof"]),
        (["bird", "sparrow", "crow", "pigeon"], "bird chirping", ["chirp", "tweet"]),
    ]
    combined = (scene_desc + " " + ltx_prompt).lower()

    # Audioセクションのみを抽出してサウンド確認する
    audio_m = _AUDIO_HEADER_RE.search(ltx_prompt)

    prompt_lower = ltx_prompt.lower()
    for keywords, sound, sound_indicators in _ANIMAL_SOUNDS:
        if any(re.search(r"\b" + kw + r"s?\b", combined) for kw in keywords):
            # プロンプト全体(Audioセクションに限らず)にサウンドがなければ追加
            if not any(ind in prompt_lower for ind in sound_indicators):
                if audio_m:
                    ltx_prompt = (
                        ltx_prompt[: audio_m.end()]
                        + f"{sound}, "
                        + ltx_prompt[audio_m.end():]
                    )
                    audio_m = _AUDIO_HEADER_RE.search(ltx_prompt)
                    prompt_lower = ltx_prompt.lower()
                else:
                    ltx_prompt = ltx_prompt.rstrip(" .") + f", {sound} nearby."
                    prompt_lower = ltx_prompt.lower()
    return ltx_prompt


_HANDS_ENTER_RE = re.compile(
    r"\b(her|his|the\s+subject's|a|the)\s+(hands?|arms?)\s+enter(s)?\b(\s+the\s+frame)?",
    re.IGNORECASE,
)

# 「入ってくる手」の鏡像パターン(2026-07-12): 触れる対象がフレーム外にあるとする書き方も、
# 同じ構造の第三者の手/浮いた手足を生成する(実データ: 目覚まし時計のシーンでShot Directorが
# "her hand reaching out of frame to slap the alarm clock"と書き、被写体の手だけが浮いた
# ぼやけた前景として過剰に生成された)。_enforce_attached_hands()の安全網として同じ場所に適用する。
_HANDS_EXIT_FRAME_RE = re.compile(
    r"\b(her|his|the\s+subject's|a|the)\s+(hands?|arms?)\s+"
    r"(reach(?:es|ing)?|extend(?:s|ing)?)\s+"
    r"(?:out\s+of|outside|off)\s+(?:the\s+)?frame\b",
    re.IGNORECASE,
)


def _enforce_attached_hands(ltx_prompt: str) -> str:
    """「hand enters (the frame)」表現を「her hand reaches into view」に、
    その鏡像である「hand reaches out of frame」表現も同様に置換する。
    フレーム内に被写体がいるのに手が「入ってくる」「フレーム外へ出ていく」と書くと、
    どちらも画面外の持ち主=第三者の手が生成されるため(2026-07-04、鏡像パターンは2026-07-12追加)。"""
    def _fix_enter(m: re.Match) -> str:
        part = m.group(2).lower()
        verb = "reach" if part in ("hands", "arms") else "reaches"
        return f"her {part} {verb} into view, connected to her body,"

    def _fix_exit(m: re.Match) -> str:
        part = m.group(2).lower()
        verb = "reach" if part in ("hands", "arms") else "reaches"
        return f"her {part} {verb}, staying visibly connected to her body and within the frame,"

    ltx_prompt = _HANDS_ENTER_RE.sub(_fix_enter, ltx_prompt)
    ltx_prompt = _HANDS_EXIT_FRAME_RE.sub(_fix_exit, ltx_prompt)
    return ltx_prompt


_SPEECH_VERBS = (
    r"says?|said|saying|whispers?|whispered|mutters?|muttered|mumbles?|mumbled|"
    r"exclaims?|exclaimed|calls?\s+out|called\s+out|asks?|asked|replies?|replied|"
    r"shouts?|shouted|murmurs?|murmured|sings?|sang|singing|hums?|hummed|humming"
)
_SING_ACTION_RE = re.compile(r"\b(sings?|sang|singing|hums?|hummed|humming)\b", re.IGNORECASE)
# セリフの引用符+話者動詞を前後どちらの付き方でも1つの単位として捉える
# ("She says \"...\"" と "\"...\" she says." の両方の英語の書き方に対応、2026-07-12)
_DIALOGUE_UNIT_RE = re.compile(
    r'(?:(?:she|he)\s+(?:' + _SPEECH_VERBS + r')\s*,?\s*)?'
    r'"[^"]+"'
    r'(?:\s*,?\s*(?:she|he)\s+(?:' + _SPEECH_VERBS + r')\.?)?',
    re.IGNORECASE,
)
_ACTION_QUOTE_RE = re.compile(r'"[^"]+"')


def _enforce_dialogue_attribution(ltx_prompt: str, action: str = "") -> str:
    """引用符のセリフに話者動詞(says/whispers等)が前後どちらにも無ければ「She says」を補う。
    浮いた引用符だけのセリフ("...")は、LTX-2.3が歌っているような節回しで発話する事故に
    つながるため(2026-07-12、実データ: ビーチシーンの「きもちいい〜」が話者動詞の無い
    浮いた引用符のまま、歌うように生成された)。actionにsing/hum等が明示されている場合は
    歌わせる意図なので何もしない。

    actionに引用符のセリフが一切無いのに最終プロンプトに引用符が出現した場合は、LLMが
    actionに無いセリフを勝手に捏造したものと判断し(実データ: 環境音の説明文がそのまま
    「She says "Seagulls calling..."」という発話に化けた事故、無指定シーンで英語の
    セリフが捏造された事故)、話者動詞ごと丸ごと削除する。

    **2026-07-12追記(セリフの逐語保証)**: actionにセリフがある場合、単に話者動詞を
    整えるだけでなく、**引用符の中身をaction本文のセリフに強制的に置き換える**。実データで、
    Scene Writer(Pass2)が日本語のセリフを「"Hot, isn't it?" (あついね)」のように英語の
    意訳+日本語原文の括弧書きにしてしまい、下流(Ground truth・Motion Formatter)で括弧の
    日本語だけが脱落し、英訳だけが最終プロンプトに残る事故が発覚した(C14検出自体は正しく
    機能していたが、Pass4のLLM fixerが最後まで正しい日本語に戻せなかった)。セリフは
    C17(衣装)の`_enforce_garments_present`と同じ「モグラ叩きにしない、確実に出す」対象と
    判断し、fixerに委ねず全面的にPython側で強制する: 引用符が無ければ末尾に追記、
    引用符があっても中身がaction本文と違えばaction本文の逐語テキストに置き換える。"""
    if action and _SING_ACTION_RE.search(action):
        return ltx_prompt
    action_quotes = _ACTION_QUOTE_RE.findall(action)

    if not action_quotes:
        fixed = _DIALOGUE_UNIT_RE.sub("", ltx_prompt)
        return re.sub(r"[ \t]{2,}", " ", fixed).strip()

    quote_iter = iter(action_quotes)

    def _fix(m: re.Match) -> str:
        try:
            correct_quote = next(quote_iter)
        except StopIteration:
            correct_quote = action_quotes[-1]
        return f"She says {correct_quote}"

    fixed = _DIALOGUE_UNIT_RE.sub(_fix, ltx_prompt)
    fixed = re.sub(r"[ \t]{2,}", " ", fixed).strip()

    # fixerが直しきれない場合の最終防衛: actionの逐語セリフがそれでも存在しなければ末尾に追記
    for q in action_quotes:
        if q not in fixed:
            if fixed and not fixed.endswith((".", "!", "?", "\"")):
                fixed += "."
            fixed = f"{fixed} She says {q}".strip()
    return fixed


# ============================================================
# 手の本数・持ち物・プロップ予約
# ============================================================

# C16: 手の本数超過の検出 — 片手を塞ぐ常時小道具と「both hands」の同居
_HAND_PROPS = ("umbrella", "parasol", "handbag", "tote bag", "shopping bag", "phone")
_BOTH_HANDS_RE = re.compile(r"\b(?:with\s+)?both\s+hands\b|\bwith\s+two\s+hands\b|\bin\s+each\s+hand\b", re.IGNORECASE)


def _hand_budget_violation(ltx_prompt: str) -> bool:
    """傘等で片手が塞がっているのに別の物を「両手で」持たせている場合True(3本目の手が生成される)。
    both handsの対象物の判定は前後40文字の窓で行う(文単位だと「傘を持ちつつカップを両手で」の
    同一文パターンで傘が同居してしまい検出漏れするため)。"""
    p = ltx_prompt.lower()
    m = _BOTH_HANDS_RE.search(p)
    if not m:
        return False
    # both hands が何を持っているか: 直前直後40文字の窓
    window = p[max(0, m.start() - 40): m.end() + 40]
    for prop in _HAND_PROPS:
        # プロップが単数で登場(複数形=露店の傘等の情景は対象外)し、both handsの対象がそのプロップ自身ではない
        # 単語境界で単数形のみ対象(複数形=露店の傘等の情景は対象外)
        if re.search(r"\b" + re.escape(prop) + r"\b(?!s)", p) and prop not in window:
            return True
    return False


# 他セグメントの主役プロップ(そのセグメント以外に出すと主役シーンの意味が薄れる)
_RESERVED_PROP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "laundry": ("laundry", "clothesline", "clotheslines"),
}


def _reserved_props_for(segments: list[dict], idx: int) -> list[str]:
    """他セグメントのアクションに登場し、自セグメントには登場しないプロップ名を返す。"""
    own = segments[idx]["action"].lower()
    others = " ".join(s["action"].lower() for j, s in enumerate(segments) if j != idx)
    return [
        name for name, kws in _RESERVED_PROP_KEYWORDS.items()
        if any(k in others for k in kws) and not any(k in own for k in kws)
    ]


# ============================================================
# アクション忠実性(要素脱落検出)
# ============================================================

# タイムラインactionの要素抽出用: 内容語でない語(冠詞・前置詞・カメラ用語等)
_ACTION_STOPWORDS = {
    "the", "and", "with", "from", "her", "his", "she", "into", "onto", "then", "while",
    "for", "that", "this", "there", "over", "under", "out", "off", "one", "two",
    # カメラ・構図のメタ語(構図はShot Directorが別途保証するため要素対象外)
    "low", "high", "angle", "shot", "camera", "tracking", "static", "view", "scene",
    "frame", "front", "behind", "side", "profile", "three-quarter", "close-up",
    "closeup", "wide", "medium", "yard",
    # カメラ注記語(actionテキスト内のカメラ挙動記述は視覚要素ではない)
    "composition", "recomposition", "reframe", "reframes", "framing", "bump",
    "lens", "focus", "autofocus", "exposure", "overexposes", "movement",
    # 知覚・機能語(視覚的に検証できない)
    "listening", "hearing", "without", "except", "followed", "deliberate", "accidental",
    "small", "large", "big", "little", "piece", "several", "some",
}

# 言い換え許容の同義語マップ(stem → プロンプト内でその要素とみなす語)
_ACTION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "cat":    ("cat", "kitten", "tabby", "feline"),
    "dog":    ("dog", "puppy"),
    "glanc":  ("glance", "look", "gaze", "eye contact", "eyes meet", "toward the lens"),
    "smil":   ("smile", "smiling", "grin"),
    "wav":    ("wave", "waving"),
    "say":    ("says", "saying", "mouths", "speaks", "speech"),
    "coffee": ("coffee", "cup", "mug"),
    "laundry": ("laundry", "garment", "clothesline", "fabric", "clothes"),
    "ponytail": ("ponytail", "hair"),
    "follow": ("follow", "trail", "alongside", "beside"),
    "feed":   ("feed", "offer", "food", "treat"),
    "sip":    ("sip", "drink", "to her lips", "cup to her mouth"),
    "fix":    ("fix", "adjust", "smooth", "pat", "tidy", "secur", "tuck"),
    "look":   ("look", "gaz", "glance", "eyes", "stare"),
    "away":   ("away", "off-camera", "off-screen", "off camera", "to the side", "not at the camera", "distance"),
    "alley":  ("alley", "alleyway", "residential", "street"),
    "walk":   ("walk", "stride", "step", "stroll", "crosses"),
    "hang":   ("hang", "pin", "clip", "attach"),
    "watch":  ("watch", "gaz", "look", "stare", "eyes"),
    "roll":   ("roll", "wave", "surf", "wash", "lap"),
}


def _stem(w: str) -> str:
    for suf in ("ing", "ies", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            # sipping→sipp→sip / sitting→sitt→sit の二重子音を畳む
            if len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "aeiou":
                w = w[:-1]
            return w
    return w


def _missing_action_elements(text: str, action: str, extra_stopwords: set[str] | None = None) -> list[str]:
    """タイムラインactionの内容語のうち、text(scene/LTXプロンプト)に現れないものを返す。
    パイプラインの4段変換中に要素(視線・セリフ・持ち物・動物等)が脱落する事故の検出(2026-07-04)。
    extra_stopwords: 動きプロンプト検査時に場所名詞等を対象外にする追加ストップワード。"""
    t = text.lower()
    stop = _ACTION_STOPWORDS | (extra_stopwords or set())
    missing: list[str] = []
    words = re.findall(r"[a-zA-Z][a-zA-Z\-']+", action.lower())
    for w in sorted(set(words)):
        if w in stop or len(w) < 3:
            continue
        if w.endswith("ly"):  # 副詞は視覚要素ではない(absentmindedly等がC14誤検出になる)
            continue
        stem = _stem(w)
        alts = _ACTION_SYNONYMS.get(stem, ())
        if stem in t or w in t or any(a in t for a in alts):
            continue
        missing.append(w)
    # セリフ("...")は逐語で存在すること
    for q in re.findall(r'"([^"]+)"', action):
        if q.lower().strip(" ,.!") not in t:
            missing.append(f'"{q}"')
    return missing


def _scene_reject_reason(scene_desc: str, action: str) -> str:
    """Pass2出力がactionに忠実でない場合、差し戻し理由を返す(問題なければ空文字)。"""
    reasons: list[str] = []
    missing = _missing_action_elements(scene_desc, action)
    if missing:
        reasons.append(f"these elements of the action are missing from the scene: {', '.join(missing)}")
    if _ANIMAL_ABSENT_RE.search(scene_desc):
        reasons.append("the scene describes the animal as not yet present ('waiting for' / 'will appear') — it must be IN FRAME from the first frame")
    return "; ".join(reasons)


def _strip_reference_echo(ltx_prompt: str, global_desc: str) -> str:
    """LTX/Keyframe Formatterがリファレンス(グローバル説明)を出力末尾にエコーした場合に除去する。
    行全体として独立に現れた場合のみ除去する(文中への埋め込みを消すと文が壊れるため)。"""
    for line in global_desc.split("\n"):
        line = line.strip()
        if len(line) >= 40:
            ltx_prompt = re.sub(
                rf"(?m)^[ \t]*{re.escape(line)}[ \t]*$", "", ltx_prompt
            )
    # 除去でできた過剰な空行を畳む
    return re.sub(r"\n{3,}", "\n\n", ltx_prompt).strip()


# ============================================================
# 構図enforcement
# ============================================================

def _enforce_feet_only_framing(ltx_prompt: str, direction: str) -> str:
    """feet onlyショットでフォーマッターが'wide shot'と書いた場合に補正する。"""
    if "feet only" not in direction.lower() and "on feet" not in direction.lower():
        return ltx_prompt
    # "ground-level wide shot" / "static wide shot" など → 統一した表現に置換
    return re.sub(
        r"(?:ground.level\s+)?static\s+(?:ground.level\s+)?wide shot\b",
        "static ground-level tight close-up on feet and shins",
        ltx_prompt,
        count=1,
        flags=re.IGNORECASE,
    )


def _enforce_closeup_scale(ltx_prompt: str, direction: str) -> str:
    """Shot Directorがclose-upを指定したのにフォーマッターがmedium/wideに戻した場合を補正。
    'medium close-up'は中間構図として有効なのでここでは変更しない。"""
    d = direction.lower()
    # "medium close-up" は別物なので除外。"close-up" / "extreme close-up" のみ対象
    if not re.search(r"(?<!\bmedium[\s-])\bclose.?up\b", d):
        return ltx_prompt
    # LTXプロンプトの先頭の shot scale 語句だけを修正(内容の記述には触れない)
    return re.sub(
        r"^(static\s+(?:eye.level\s+|ground.level\s+)?)(wide shot|medium wide shot|medium shot|medium close.up)\b",
        r"\1close-up",
        ltx_prompt,
        count=1,
        flags=re.IGNORECASE,
    )


_GROUND_SURFACE_WORDS = ("floor", "ground")


def _is_tight_scale(direction: str) -> bool:
    d = direction.lower()
    return ("close-up" in d or "close up" in d or "bust" in d) and "feet" not in d


def _strip_offscreen_ground_mentions(text: str, direction: str) -> str:
    """tightショット(close-up/bust系、feet-only除く)で、床/地面という物理的な面への
    言及を文中の節(カンマ区切り)単位で削除する。chest-up framingで床が見えることは
    幾何学的にありえない(床を見せるには俯瞰かローアングルで引く必要がある)ため、
    下半身語彙全般ではなく「床/地面という面の言及」という単一の物理原則に絞って対象にする
    (2026-07-08、実データ検証: 「素足」という属性自体は言及されても実害が薄く、
    "on the floor"のような面への言及こそがtightショットと両立しない、というユーザー指摘)。
    節単位で削除するのは、同じ文の中に残すべき情報(衣装等)と消すべき情報(床への言及)が
    同居しているケース(例: "...silk slip with lace trim, her bare feet planted on the
    floor.")があるため。LLM fixerへのリライト依頼ではなく確実な決定論的削除にしているのは、
    「情報を追加する」指示にfixerが従わないことがある(raincoat事故)一方、ここは「削除する」
    だけなのでPythonで直接処理する方が確実なため。"""
    if not _is_tight_scale(direction):
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    new_sentences: list[str] = []
    changed = False
    for sent in sentences:
        m = re.search(r"[.!?]+\s*$", sent)
        term = m.group(0).rstrip() if m else ""
        body = sent[: m.start()] if m else sent
        clauses = re.split(r",\s*", body)
        kept = [c for c in clauses if not any(_has_word(c.lower(), w) for w in _GROUND_SURFACE_WORDS)]
        if len(kept) != len(clauses):
            changed = True
        if kept:
            new_sentences.append(", ".join(kept) + term)
    return " ".join(new_sentences).strip() if changed else text


_WALK_KEYWORDS = ["walks", "walking", "crosses", "approaches", "steps"]
# direction側の歩行ショット指標(actionに歩行動詞がない足元トラッキング等も歩きと数える)
_WALK_DIRECTION_RE = re.compile(
    r"walks?|walking|crosses frame|tracking|feet only|on feet", re.IGNORECASE
)
_LATERAL_RE = re.compile(r"crosses frame|lateral tracking|alongside", re.IGNORECASE)


def _enforce_walking_lateral(segments: list[dict], directions: list[str], orientation: str, log_prefix: str) -> list[str]:
    """全シーン中の歩きショットが3つ以上あるのに横視点(側面)が1つもない場合、
    歩きシーンのうち最初と最後を除いた中から1つを横視点に強制変換する。
    Variety Auditorのルールだけでは守られないためPythonで保証する(2026-07-04)。"""
    walk_idx = [
        i for i, (seg, d) in enumerate(zip(segments, directions))
        if any(k in seg["action"].lower() for k in _WALK_KEYWORDS) or _WALK_DIRECTION_RE.search(d)
    ]
    if len(walk_idx) < 3:
        return directions
    if any(_LATERAL_RE.search(directions[i]) for i in walk_idx):
        return directions

    # 変換候補 = タイムラインのセグメント#1と#lastを除いた歩きシーン(ユーザー指定。
    # 導入とフィナーレが横移動なのは不自然、それ以外の歩きならどこでも可)
    last = len(segments) - 1
    mid = [i for i in walk_idx if i not in (0, last)]
    if not mid:
        return directions

    # 変換対象の優先度: 足元ショット・接近ショットはなるべく避ける(元の演出意図を保つ)
    def _score(i: int) -> int:
        d = directions[i].lower()
        s = 0
        if "feet" in d:
            s += 2
        if "toward" in d or "approach" in d:
            s += 1
        return s

    target = min(mid, key=_score)
    d = directions[target].lower()
    if "feet" in d:
        lateral = "ground-level static, feet only, subject crosses frame left-to-right"
    elif orientation == "vertical":
        lateral = "handheld lateral tracking alongside; camera films her from the side as she walks, side profile stays centered at constant size. Facing: Right profile."
    else:
        lateral = "static camera eye-level, subject crosses frame left-to-right, camera films her from the side. Facing: Right profile."
    directions = list(directions)
    directions[target] = lateral
    print(f"{log_prefix}   横視点強制: seg{target + 1} を側面ショットに変換")
    return directions


def _enforce_shot_rules(action: str, direction: str) -> str:
    """Shot Directorのルール違反をPythonレベルで補正する。"""
    act = action.lower()
    d   = direction

    # ルール1: 体・動物への接触/小物を持つ/飲む系 → 必ずclose-up
    # ※ 洗濯物を干す(hangs/attaches/clips)は close-up 対象外(ローアングル広角が正解)
    _CLOSEUP_KEYWORDS = [
        "fixes", "fixing",           # fixes ponytail
        "adjusts", "adjusting",      # adjusts hair / necklace
        "touches", "touching",       # touches face / hair
        "pets", "petting",           # pets animal
        "strokes", "stroking",       # strokes animal
        "feeds", "feeding",          # feeds animal
        "sips", "sipping",           # sips coffee
        "drinks", "drinking",        # drinks
        "holds cup", "holding cup",  # holds cup specifically
        "gripping", "grabs",
    ]
    is_touching = any(kw in act for kw in _CLOSEUP_KEYWORDS)
    is_walking  = any(kw in act for kw in _WALK_KEYWORDS)

    if is_touching and not is_walking:
        # wide / medium wide / medium shot を close-up に強制
        d = re.sub(
            r"\b(extra wide|wide|medium wide|medium shot|full body|full-body)\b",
            "close-up",
            d,
            flags=re.IGNORECASE,
        )
        # 先頭が「medium」で始まる場合も「close-up, medium close-up」と補正
        d = re.sub(r"^static camera medium\b", "static camera close-up", d, flags=re.IGNORECASE)
        d = re.sub(r"^static camera eye-level medium\b", "static camera close-up", d, flags=re.IGNORECASE)
        # まだ close-up という語がなければ先頭に追加
        if "close-up" not in d.lower() and "close up" not in d.lower():
            d = "close-up, " + d
        # 接触系close-upは static / slow push-in のみ — pull-back/zoom out を除去
        # (sipping中にpull-backすると人物が引きで小さくなり接触動作が見えなくなる)
        d = re.sub(r",?\s*(?:static with\s+)?(?:slow\s+)?(?:pull[- ]?back|dolly out|zoom(?:s|ing)? out)[^;,]*",
                   "", d, flags=re.IGNORECASE).strip().strip(";,").strip()

    # ルール2: 静止被写体で禁止されたカメラモーション → pan / follow / tilt / zoom / orbit を除去
    # (許可リストの slow push-in / slow pull-back / handheld drift は残す)
    if not is_walking:
        d = re.sub(r";?\s*(horizontal |vertical |slight )?(camera )?(pan(s)?|follow(s)?|tilt(s)?|zoom(s)?|orbit(s)?)\b[^;,]*",
                   "", d, flags=re.IGNORECASE).strip().strip(";,").strip()

    # ルール3: feet onlyショットは移動方向1つだけ — crosses frame と toward/away camera の混在を除去
    if "feet only" in d.lower() and re.search(r"crosses frame", d, re.IGNORECASE):
        d = re.sub(r",?\s*(?:while\s+)?walking toward (?:the )?camera[^;,]*", "", d, flags=re.IGNORECASE)
        d = re.sub(r",?\s*toward (?:the )?(?:camera|lens)( position)?\b[^;,]*", "", d, flags=re.IGNORECASE)
        d = d.strip().strip(";,").strip()

    return d


# ============================================================
# キャラクター / STATE
# ============================================================

_CHAR_STOPWORDS = {"with", "and", "wearing", "over", "under", "the", "her", "his", "their",
                   "a", "an", "in", "on", "of", "young", "woman", "man", "girl", "boy"}

_FOOTWEAR_ITEMS = ("sneaker", "shoe", "sandal", "boot", "sock", "heel", "loafer", "slipper")
_LOWER_BODY_ITEMS = _FOOTWEAR_ITEMS + ("jeans", "pants", "trouser", "skirt", "shorts", "legging")


async def _extract_character_line(global_desc: str) -> str:
    """グローバル説明から主人公の1文キャラクター記述(国籍・性別・年齢・髪色・髪型・メガネ等、
    タイムライン全体を通じて変わらない固定アイデンティティ)を抽出する。衣装は抽出対象から
    除外し続ける——Pass0のSTATEが毎セグメント確実にこれを保持するため、ここに残すとPass3の
    Formatterがcharacter_lineの旧衣装とSTATEの新衣装を両方書いてしまう事故につながる(実データで確認済み)。

    髪型(結び方)・メガネは以前ここから除外し、STATE側の別の補完機構
    (`_extract_baseline_hairstyle`/`_repair_missing_hairstyle`)で個別に扱っていたが、
    ユーザー判断で「国籍・性別・年齢・髪色と同じ固定アイデンティティの一部」として
    ここに統合した(2026-07-10)。実データで、Pass0bがSTATEの部分更新でこの2つを
    丸ごと落とす事故が繰り返し起きたため、都度STATE側に個別の補完機構を作るのではなく、
    既に確実な`_enforce_character_line`の強制注入に一本化する。

    胸のサイズも同じ理由で固定アイデンティティに追加(2026-07-14、ユーザー指定)。
    体型と同じく変化しない属性のため、STATE側で個別に扱う必要はない。
    `_trim_character_for_scale`は衣装語(`_FOOTWEAR_ITEMS`/`_LOWER_BODY_ITEMS`)だけを
    見て要否を判定するため、胸のサイズの記述はfeetショットでは自動的に落ち、
    close-up/wideでは残る(追加の分岐は不要)。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": (
                "Extract ONE compact English sentence describing the main character's FIXED physical "
                "identity ONLY: nationality/ethnicity, gender, approximate age (if stated), body build/"
                "physique if stated or implied (match the source exactly — could be 'slender', 'skinny', "
                "'petite', 'athletic', 'curvy', 'plus-size', etc., or omit entirely if unstated), hair "
                "COLOR, hairSTYLE (e.g. 'messy ponytail with side-swept bangs'), glasses/eyewear if any is "
                "mentioned (e.g. 'silver thin-frame round glasses'), and breast/bust size or cleavage "
                "if stated or implied in the source (match whatever the source actually says — could be "
                "'small breasts', 'medium bust', 'large bust', 'ample cleavage', etc., or omit entirely "
                "if the source says nothing about it). These persist unchanged for the whole timeline. "
                "EXCLUDE clothing/outfit and any other appearance detail that could plausibly change "
                "between scenes — those are tracked separately per shot, and including them here causes "
                "contradictions later. "
                "EXCLUDE transient states and carried items (e.g. 'sneakers carried in one hand', 'walking barefoot', "
                "'holding a bag') — those are scene actions, not permanent attributes. "
                "Output ONLY that sentence, nothing else. If no character is described, output an empty line."
            )},
            {"role": "user", "content": f"/no_think\n{global_desc}"},
        ],
        temperature=0.3,
        max_tokens=256,
    )
    line = (resp.choices[0].message.content or "").strip()
    if not line:
        line = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
    line = line.split("\n")[0].strip()
    # 「The main character is」「The recurring main character is」等のプロトコル語で始まると
    # そのままLTX/キーフレームプロンプトに漏れて破綻するため剥がす。旧正規表現は"recurring"を
    # 考慮しておらず、抽出指示文の"the recurring main character's..."という言い回しをLLMが
    # そのままオウム返しした際に剥がせず、"recurring"という語が(連続アニメ・シリーズの
    # レギュラーキャラクターを連想させ)絵柄がアニメ風になる事故が実データで発覚(2026-07-10)
    line = re.sub(r"^(?:the\s+)?(?:recurring\s+)?main\s+(?:character|subject)\s+is\s+", "", line, flags=re.IGNORECASE)
    return line[:1].upper() + line[1:] if line else line


async def _extract_baseline_hairstyle(global_desc: str) -> str:
    """参照文から髪型の基準描写(例: "messy ponytail with side-swept bangs")を抽出する。
    衣装と並んで「誰にでも必ずある」普遍的なカテゴリのため、STATEとは別に参照文から
    直接・確実に抽出しておく(2026-07-07)。Pass0(STATE)は温度0.8の確率的生成のため、
    まれに全セグメントの STATE から髪型カテゴリ自体が丸ごと抜け落ちることがあり
    (ユーザー実測で発覚)、その場合の機械的な補完に使う。髪色はcharacter_line側が
    (不変の識別子として)担当するため、ここでは「結び方・スタイル」のみを対象にする。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    try:
        resp = await client.chat.completions.create(
            model=cfg.LLM_MODEL,
            messages=[
                {"role": "system", "content": (
                    "From the reference, extract ONLY how the subject's hair is STYLED (e.g. 'messy ponytail "
                    "with side-swept bangs', 'loose braid') — NOT the hair color (that's handled elsewhere). "
                    "Output ONLY that short phrase, nothing else. If no hairstyle is mentioned, output an empty line."
                )},
                {"role": "user", "content": f"/no_think\n{global_desc}"},
            ],
            temperature=0.3,
            max_tokens=64,
        )
    except Exception:
        return ""
    line = (resp.choices[0].message.content or "").strip()
    if not line:
        line = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
    line = line.split("\n")[0].strip().strip(".")
    return line


def _repair_missing_hairstyle(intents: list[str], baseline_hairstyle: str) -> list[str]:
    """Pass0のSTATEに「hair」という語が一切無いセグメントへ、参照文から抽出済みの
    髪型基準値を機械的に補完する。髪型・衣装は「誰にでも必ずある」普遍的なカテゴリの
    ため明示的に扱う(ユーザー確認済み、他の任意的な属性(アクセサリー等)への拡張はしない)。
    「hair」という語が(どんな具体的なスタイルであれ)既にあれば触らない — 言い換えを
    誤検出して二重記述を作らないため、判定は「カテゴリの有無」のみで行う。"""
    if not baseline_hairstyle:
        return intents
    repaired = []
    for line in intents:
        m = re.search(r"STATE:\s*(.+)$", line)
        if not m or _has_word(m.group(1).lower(), "hair"):
            repaired.append(line)
            continue
        new_state = m.group(1).rstrip() + f", {baseline_hairstyle}"
        repaired.append(line[:m.start()] + f"STATE: {new_state}")
    return repaired


_STATE_REF_RE = re.compile(r"^same as (?:segment\s+)?(\d+|previous|prior)\.?\s*", re.IGNORECASE)


def _resolve_state_references(intents: list[str]) -> list[str]:
    """Pass0のSTATE CONTINUITY RULEは「変化が無ければ前セグメントのSTATEをそのままコピーする」
    指示だが、ローカルLLMが実体を運ばず"Same as 11."のような参照だけを書くことがある
    (セグメント数が多い長尺タイムラインで顕在化しやすい、2026-07-10実データで確認: STATE本文が
    "Same as N."のみになり、水着の描写が参照元の1セグメントにしか残らない/夜のシーンの一部が
    朝になる、という2つの症状で発覚)。この参照テキストがそのまま下流(C17衣装チェック・
    Scene Writerの時間帯authority判定)に渡ると、その回はSTATEが実質空扱いになる。
    番号参照・"previous"/"prior"参照の両方を実際のSTATE本文に解決し、後続の追記
    (", loose, slightly frizzy waves"等)は保持する。`_repair_missing_hairstyle`と同じ
    「Pass0直後のPython決定論的リペア」設計にそろえ、その前に呼ぶ(髪型欠落判定が
    参照テキストでなく解決済みの実体を見られるようにするため)。"""
    states: list[str] = []
    resolved: list[str] = []
    for i, line in enumerate(intents):
        m = re.search(r"STATE:\s*(.+)$", line)
        if not m:
            resolved.append(line)
            states.append("")
            continue
        state_text = m.group(1).strip()
        ref = _STATE_REF_RE.match(state_text)
        if ref:
            token = ref.group(1).lower()
            target_idx = i - 1 if token in ("previous", "prior") else int(token) - 1
            base = states[target_idx] if 0 <= target_idx < len(states) else ""
            if base:
                trailing = state_text[ref.end():].strip().lstrip(",").strip()
                state_text = f"{base}, {trailing}" if trailing else base
        states.append(state_text)
        resolved.append(line[:m.start()] + f"STATE: {state_text}")
    return resolved


_NO_CHANGE_RE = re.compile(
    r"^(?:no change|none mentioned|none)\.?$"
    r"|^same\s+(?:location|outfit|scene|time|position|appearance)\b",
    re.IGNORECASE,
)


def _resolve_state_continuity(state_lines: list[str]) -> list[str]:
    """State Tracker(Pass0b、V6で新設)の出力を実際のSTATE全文に解決する。

    V6ではPass0を「創造(INTENT/HIGHLIGHT/TEMPO)」と「継続性記録(STATE)」の2つの専属パスに
    分割した(2026-07-10、ユーザー指摘: LTX書式変換+ローマ字変換を1回のLLM呼び出しに
    詰め込んだら不安定だったが2パスに分けたら確実に動いた、という過去の実体験と同型の
    問題——INTENT/HIGHLIGHT/TEMPOという創造的判断とSTATEという地味な継続記録を同じ呼び出しに
    同居させていたため、後者だけが省略・空欄という形で壊れていた)。

    State Trackerの契約は二択のみ: リテラル`NO CHANGE`(前セグメントと完全に同一)か、
    完全な状態記述(セグメント1、または何かが変化した場合)。中間的な省略・参照は
    プロンプト上禁止しているが、LLMが契約に従わず`_resolve_state_references`時代の
    旧パターン(「Same as N」等)に戻った場合の保険として`_STATE_REF_RE`の検出も残す。

    **2026-07-12追記**: 「Same as N」とも異なる**第3の変種**が実データで発覚した——
    "Same location and time; same outfit and accessories; black hair in a messy low
    ponytail with side-swept bangs."のような、地の文で「変わっていません」と言い換える
    パターン。`_NO_CHANGE_RE`(旧: リテラル`no change`等の完全一致のみ)にはマッチせず、
    かといって全く新しい完全な記述でもないため、そのまま(具体的な服装名を持たない曖昧な
    文として)下流のGround truth/Keyframe生成に渡ってしまい、キーフレーム側のLLMが
    具体的な服装語を持たないまま独自に(グローバル説明にあった別候補の衣装等を)補ってしまう
    事故につながった(浴衣タイムラインのテストで、クロップトップのはずのセグメントに
    浴衣が混入)。`_NO_CHANGE_RE`を「no change系の完全一致」に加えて「Same
    location/outfit/scene/time/position/appearance で始まる行」も検出するよう拡張し、
    前セグメントの解決済み全文で置き換える(パラフレーズ変種もNO CHANGEと同じ扱いにする)。"""
    resolved: list[str] = []
    for i, raw in enumerate(state_lines):
        text = raw.strip()
        ref = _STATE_REF_RE.match(text)
        if ref:
            token = ref.group(1).lower()
            target_idx = i - 1 if token in ("previous", "prior") else int(token) - 1
            base = resolved[target_idx] if 0 <= target_idx < len(resolved) else ""
            trailing = text[ref.end():].strip().lstrip(",").strip()
            resolved.append(f"{base}, {trailing}" if (base and trailing) else (base or text))
        elif not text or _NO_CHANGE_RE.match(text):
            resolved.append(resolved[i - 1] if i > 0 else "")
        else:
            resolved.append(text)
    return resolved


def _trim_character_for_scale(character_line: str, direction: str) -> str:
    """キャラ記述を「そのショットで構図的に見える衣装だけ」にスケーリングする(2026-07-04)。
    全身衣装文を全ショットに入れると、LTXが「書いてある=映すべき」と解釈して
    どのショットも全身正面の引き構図に寄ってしまう。逆に見える衣装を消すと
    その部位が裸で生成される(足元ショットでジーンズが消えた事故)。

    直交する2軸で決める:
      1) フレーミング → 可視衣装: feet=下半身のみ / close-up・バストアップ=上半身のみ / それ以外=全部
      2) 動物が主役 → 髪の記述のみ追加削除(猫のアップに「black wavy hair」が同居すると
         猫の毛が黒ウェービーになる attribute bleed 対策。衣装の可視判定には関与しない)
    """
    if not character_line:
        return character_line
    d = direction.lower()

    # 軸2: 動物主役なら髪の記述を先に削る(bleed対策)
    if _animals_in(d):
        character_line = re.sub(r"\s+with\s+.*?(?=\s+wears\b)", "", character_line)

    parts = re.split(r",\s*(?:and\s+)?", character_line.rstrip("."))

    # 軸1: フレーミングによる可視衣装の選択
    if "feet" in d:
        lower = [p for p in parts if any(_has_word(p.lower(), k) for k in _LOWER_BODY_ITEMS)]
        if lower:
            return "She wears " + ", ".join(
                re.sub(r"^.*?\bwears\s+", "", p) for p in lower
            ).rstrip(".") + "."
        # character_lineは身元情報のみ(国籍/年齢/髪色/体格)で衣装を持たないため、
        # 下半身衣装が1つも無い場合のフォールバックは「未加工のまま返す」ではなく空にする。
        # feet-onlyショットには身元情報(髪色等)が一切映らないため、"copy verbatim"指示が
        # feet-only用の顔/髪語禁止ルールと衝突する事故が実データで発覚(2026-07-07)
        return ""

    is_tight = "close-up" in d or "close up" in d or "bust" in d
    if is_tight:
        # 削るのはfootwearのみ: 靴が構図を全身に引かせる主犯。
        # 腰衣装(pants等)は残す — 画像モデルが指定より広くフレーミングした時に
        # 未記述の下半身が下着/裸で生成される事故の保険(2026-07-05)
        kept = [p for p in parts if not any(_has_word(p.lower(), k) for k in _FOOTWEAR_ITEMS)]
        if kept and kept != parts:
            return ", ".join(kept).rstrip(".") + "."
    return character_line


_GARMENT_WORD_RE = re.compile(
    r"\b(shirt|blouse|top|tank|tee|t-shirt|sweater|cardigan|jacket|coat|raincoat|dress|"
    r"jeans|pants|trousers?|skirt|shorts|leggings?|sneakers?|shoes?|sandals?|boots?|"
    r"necklace|earrings?|scarf|hat|cap|uniform|swimsuit|bikini|apron|"
    r"sunglasses|glasses|eyeglasses|spectacles)\b",
    re.IGNORECASE,
)


def _garments_missing(text: str, state: str) -> list[str]:
    """STATE(現在の衣装を保持)にある衣装名のうちtextに存在しないものを返す。
    character_lineは衣装を持たないため、stateが唯一の情報源。
    トークン数判定(旧方式)は髪・国籍語だけで通過してしまい、wideショットで衣装未記述
    →服装がモデルの創作になる事故があった。衣装は名前単位で要求する。"""
    t = text.lower()
    expected = {g.lower() for g in _GARMENT_WORD_RE.findall(state)}
    return sorted(g for g in expected if not _has_word(t, g))


def _enforce_garments_present(text: str, state: str, direction: str) -> str:
    """C17(GARMENTS PRESENT)/KF2衣装のLLM fixerが従わず衣装語が直りきらないことがあるため、
    最終防衛としてPythonで欠落した衣装をSTATEの原文節そのまま追記して保証する
    (`_enforce_character_line`と同じ「検出はPython・生成はLLM・LLMが従わない時の最終防衛も
    Python」という設計、2026-07-10。実データで「yellow raincoat」がt2v/i2v両方で
    fixer任せの一発勝負のまま欠落し、コンソールログにしか出ない事故が発覚したため)。
    C17検出(`_detect_violations`)と同じtight/feet-onlyフィルタをここでも適用し、
    tightショットに下半身衣装を、feet-onlyショットに上半身衣装を混入させない。"""
    if not state:
        return text
    missing = _garments_missing(text, state)
    if _is_tight_scale(direction):
        missing = [g for g in missing if not any(item in g for item in _LOWER_BODY_ITEMS)]
    elif "feet" in direction.lower():
        missing = [g for g in missing if any(item in g for item in _LOWER_BODY_ITEMS)]
    if not missing:
        return text
    clauses = [c.strip() for c in state.split(",") if c.strip()]
    to_append = [c for c in clauses if any(_has_word(c.lower(), g) for g in missing)]
    if not to_append:
        return text
    addition = ". ".join(to_append).rstrip(".") + "."
    addition = addition[0].upper() + addition[1:]
    return text.rstrip() + " " + addition


# 明示的にアニメ/イラスト系スタイルが参照文(global_desc)に要求されていない限り、生成は
# 写実系にデフォルトする(2026-07-10、ユーザー指摘: 「明示的にアニメ、イラストと書かれてない
# 限りはリアル系で」)。日本語プロンプトファイルも多いため英語・カタカナ/漢字の両方で判定する
_ANIME_STYLE_RE = re.compile(
    r"anime|manga|illustrat\w*|cartoon|2d[\s-]?(?:animat\w*|sticker)|cel[\s-]?shad\w*|"
    r"アニメ|イラスト|カートゥーン|漫画|マンガ|二次元",
    re.IGNORECASE,
)
_REALISM_HINT_RE = re.compile(r"photorealistic|photo-realistic|\brealistic\b|documentary|実写", re.IGNORECASE)


def _enforce_realism_default(text: str, global_desc: str) -> str:
    """参照文(global_desc)が明示的にアニメ/イラスト系スタイルを要求していない限り、
    最終テキストに写実系の語が無ければ"Photorealistic."を末尾に追記して保証する。
    LLMの指示追従だけに頼らないPython決定論的な最終防衛(2026-07-10)。"""
    if _ANIME_STYLE_RE.search(global_desc) or _REALISM_HINT_RE.search(text):
        return text
    return text.rstrip() + " Photorealistic."


def _state_details_missing(text: str, state: str) -> list[str]:
    """STATEの各カンマ区切り断片(場所を除く — 場所は別ルールで担当)のうち、
    半数未満の単語しかtextに反映されていない断片を返す。
    衣装名の固定語彙チェック(_garments_missing)は髪型(ponytail/bangs等)のような
    STATEの「見た目全部」定義の他の属性をカバーできないため、属性を列挙せず
    断片単位で汎用的に検品する(STATEは絶対、という設計判断に基づく)。「black wavy hair」
    のように一部の単語(character_line由来)だけ一致してしまうケースを拾うため、
    閾値は「1語でも一致」ではなく「半数以上一致」で判定する。"""
    if not state:
        return []
    clauses = [c.strip() for c in state.split(",") if c.strip()]
    if len(clauses) <= 1:
        return []
    t = text.lower()
    missing: list[str] = []
    for clause in clauses[1:]:  # 先頭(場所)は別ルールで担当済み
        words = [w.strip(".();:'\"") for w in clause.lower().split()]
        words = [w for w in words if len(w) >= 3 and w not in _CHAR_STOPWORDS]
        if not words:
            continue
        present = sum(1 for w in words if _has_word(t, w))
        if present < len(words) / 2:
            missing.append(clause)
    return missing


_GENDER_WORDS = ("woman", "man", "girl", "boy", "female", "male")
_GENDER_SYNONYMS = {
    "woman": ("woman", "female"), "female": ("woman", "female"),
    "man": ("man", "male"), "male": ("man", "male"),
    "girl": ("girl",), "boy": ("boy",),
}
_AGE_PATTERN_RE = re.compile(r"\b(?:early|mid|late)[\s-]?\d{2}s\b|\b\d{2}s\b|\b\d{1,3}-year-old\b")
_BUST_WORDS_RE = re.compile(r"\b(cleavage|bust|breasts?)\b", re.IGNORECASE)
_BODY_BUILD_WORDS_RE = re.compile(
    r"\b(slender|skinny|slim|thin|petite|curvy|athletic|toned|muscular|plump|chubby|"
    r"voluptuous|hourglass|stocky|plus-size|plus size)\b",
    re.IGNORECASE,
)


def _character_tokens_missing(text: str, character_line: str) -> bool:
    """キャラ記述の特徴トークンがtextに十分含まれていない場合True。
    国籍・性別・年齢は_extract_character_lineが常に抽出を試みる固定フィールド(それ以外の
    髪色・体格等と違い、抜けると別人種/別性別に見える致命度が高い)のため、髪色等と同じ
    「トークンが4つ一致すればOK」という固定個数の網ではなく個別にmustチェックする
    (国籍・性別・年齢はmust。固定個数だと character_line が短い場合に髪色トークンだけで
    4つに達し、国籍・年齢が抜けても見逃されていた、2026-07-08)。"""
    if not character_line:
        return False
    t = text.lower()
    cl_lower = character_line.lower()
    words_raw = [w.strip(".,;:()「」\"'") for w in cl_lower.split()]

    # 性別語 + その直前の国籍/民族形容詞(character_lineの「{Nationality} {gender}」構造に対応)
    for i, w in enumerate(words_raw):
        if w in _GENDER_WORDS:
            if not any(_has_word(t, s) for s in _GENDER_SYNONYMS.get(w, (w,))):
                return True
            if i > 0 and len(words_raw[i - 1]) >= 3 and words_raw[i - 1] not in _CHAR_STOPWORDS:
                if not _has_word(t, words_raw[i - 1]):
                    return True
            break

    # 年齢(character_lineに明示されている場合のみmust)
    age_m = _AGE_PATTERN_RE.search(cl_lower)
    if age_m and age_m.group(0) not in t:
        return True

    # 胸のサイズ(character_lineに明示されている場合のみmust、2026-07-14)。
    # 髪色・体格等と同じ一致率判定に任せると、髪型関連トークンの数が多いため
    # "large cleavage"のような少数語だけが完全欠落しても閾値を超えて見逃される事故が
    # 実データで発覚した(国籍・性別・年齢と同じく欠落の致命度が高いため個別チェックする)。
    bust_m = _BUST_WORDS_RE.search(cl_lower)
    if bust_m and not _has_word(t, bust_m.group(0)):
        return True

    # 体型(slender/skinny等、character_lineに明示されている場合のみmust、2026-07-14)。
    # 胸のサイズと同じ理由(髪型トークンに埋もれて欠落が見逃される)に加え、実際に
    # _extract_character_line自身が「slender build」を抽出時点で気まぐれに落とすことが
    # 実データで確認された(体型を表す語は1〜2語しかなく、一致率判定に任せると
    # 他の語に埋もれて欠落が見逃されやすい)。
    build_m = _BODY_BUILD_WORDS_RE.search(cl_lower)
    if build_m and not _has_word(t, build_m.group(0)):
        return True

    # それ以外(髪色等)は固定個数でなく一致率で判定
    tokens = [w for w in words_raw if len(w) >= 4 and w not in _CHAR_STOPWORDS]
    if not tokens:
        return False
    uniq = set(tokens)
    present = sum(1 for w in uniq if _has_word(t, w))
    return present < max(1, len(uniq) // 2)


def _extract_state_from_intent(intent: str) -> str:
    """Pass0(Creative Director)の出力行("INTENT: ... | HIGHLIGHT: ... | TEMPO: ... | STATE: ...")
    からSTATE部分だけを取り出す。STATEは常に最後のフィールドとして出力される。"""
    m = re.search(r"STATE:\s*(.+)$", intent)
    return m.group(1).strip() if m else ""


# ============================================================
# Pass0(Creative Director)/ Pass1(Shot Director)/ Pass1.5(Variety Auditor)/ Pass2(Scene Writer)
#
# これらのシステムプロンプト定数(_CREATIVE_DIRECTOR_SYSTEM等)はi2v/t2vで内容が実際に
# 食い違っている(t2vが一部シーン中立化・言い回し改善を受け取っていない箇所がある)ため、
# 関数はここに置くがシステムプロンプト文字列は各V5ファイル側から引数で渡す。
# ============================================================

async def _plan_creative_intent(global_desc: str, segments: list[dict], orientation: str, system_prompt: str) -> list[str]:
    """Pass0: クリエイティブディレクターが全体の演出意図・見せ場・テンポを一括計画する。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    seg_list = "\n".join(
        f"{i}. ({seg['duration']}s) {seg['action']}"
        for i, seg in enumerate(segments, 1)
    )
    orient_note = "16:9 widescreen landscape" if orientation == "horizontal" else "9:16 vertical portrait"
    user_msg = (
        f"/no_think\n"
        f"## Video style / setting\n{global_desc}\n\n"
        f"## Orientation\n{orient_note}\n\n"
        f"## Segments ({len(segments)} total)\n{seg_list}"
    )
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.8,
        # STATE追加で1行あたりの出力が伸びたため、固定1024だと長いタイムライン(22セグメント等)
        # で末尾の数セグメントのINTENT/STATE行が丸ごと欠落する事故が実データで発覚した。
        # 4096に固定(ユーザー指定、他のLLM呼び出しと同じ値)
        max_tokens=4096,
    )
    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        raw = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()

    return _parse_numbered_lines(raw, len(segments), "")


async def _plan_shot_directions(global_desc: str, segments: list[dict], orientation: str, intents: list[str], system_prompt: str) -> list[str]:
    """Pass1: 全セグメントのカメラ構図をショットディレクターLLMが一括で計画する。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    seg_list = "\n".join(
        f"{i}. ({seg['duration']}s) {seg['action']}"
        + (f"\n   Creative intent: {intent}" if intent else "")
        for i, (seg, intent) in enumerate(zip(segments, intents), 1)
    )
    orient_note = (
        "16:9 WIDESCREEN LANDSCAPE — wide lateral space: LATERAL crossing and horizontal compositions work well"
        if orientation == "horizontal" else
        "9:16 VERTICAL PORTRAIT — very narrow lateral space: LATERAL crossing is FORBIDDEN; "
        "for walking use toward/away from lens, tracking follow, or tight patterns; "
        "favor vertical compositions (low/high angle, sky above and ground below)"
    )
    user_msg = (
        f"/no_think\n"
        f"## Video style / setting\n{global_desc}\n\n"
        f"## Orientation\n{orient_note}\n\n"
        f"## Segments ({len(segments)} total)\n{seg_list}"
    )
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.8,
        max_tokens=1024,
    )
    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        raw = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()

    raw_dirs = _parse_numbered_lines(raw, len(segments), "eye level, static handheld, medium")
    return [_enforce_shot_rules(seg["action"], d) for seg, d in zip(segments, raw_dirs)]


async def _audit_shot_variety(segments: list[dict], directions: list[str], orientation: str, system_prompt: str) -> list[str]:
    """Pass1.5: 全構図を俯瞰し、向き・カメラ位置・接近の単調さを監査して書き直す。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    dir_list = "\n".join(
        f"{i}. [{seg['action']}] {d}"
        for i, (seg, d) in enumerate(zip(segments, directions), 1)
    )
    orient_note = (
        "16:9 WIDESCREEN LANDSCAPE" if orientation == "horizontal"
        else "9:16 VERTICAL PORTRAIT (no lateral crossing patterns)"
    )
    user_msg = (
        f"/no_think\n"
        f"## Orientation\n{orient_note}\n\n"
        f"## Shot list ({len(segments)} segments, format: N. [action] direction)\n{dir_list}"
    )
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.4,
        max_tokens=1024,
    )
    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        raw = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()

    audited = _parse_numbered_lines(raw, len(segments), "")
    # パース失敗・空行のセグメントは監査前のdirectionを維持し、
    # 書き直されたものは actionの先頭タグ [.....] が残っていれば除去 → 安全ルールを再適用
    result: list[str] = []
    for seg, orig, aud in zip(segments, directions, audited):
        d = re.sub(r"^\[[^\]]*\]\s*", "", aud).strip() or orig
        result.append(_enforce_shot_rules(seg["action"], d))
    return result


# ============================================================
# Pass0/1/1.5 JSON構造化出力版(2026-07-16新設)
#
# 上の自由記述版(_parse_numbered_lines・_plan_creative_intent・_plan_shot_directions・
# _audit_shot_variety)は無編集のまま残し、こちらは並行する別経路として追加する。
# 理由: 自由記述版は「行の出現位置=セグメント番号」という前提でパースしており、
# 紛れ込んだ数字付き行がずれを起こすと後続セグメント全部の帰属がずれる事故が
# 実データで発覚した(_parse_numbered_linesのdocstring参照)。各要素にsegment番号を
# 明示させるJSON配列にすれば、この位置依存の誤帰属というクラスのバグを構造的に排除できる。
# 呼び出し元を新旧どちらの経路に向けるかだけで切り替えられるよう、関数・システムプロンプトとも
# 完全に別名で用意する(問題があれば呼び出し元を旧関数に戻すだけで即座に復帰できる)。
# ============================================================

# 構造パース失敗時のリトライ上限。品質改善ループ用の_LINT_MAX_ATTEMPTS(5回・最終的に
# best-effort受理)とは意味が異なる別concernのため使い回さない——こちらは「JSONの
# フェンス混入・コメント混入等の軽微な形式ミス」を1回だけ厳しいリマインダーを添えて
# 再送し、それでも直らなければ_auto_segment_narrativeと同じ方針(パース失敗は無言で
# fallbackに逃げず、生テキストを添えてraiseする)を貫く。
_JSON_PARSE_MAX_ATTEMPTS = 2


def _extract_json_array_text(raw: str) -> str:
    """Markdownコードフェンス(```json ... ```)が付いていれば剥がす。"""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_segments(raw: str, count: int) -> list[dict]:
    """LLM出力のJSON配列をパースし、各要素の"segment"キーが1..countと過不足なく
    一致することを検証する(要素の並び順は問わない、segmentキーで紐付ける)。
    位置依存の誤帰属(_parse_numbered_linesが実際に踏んだ罠)を構造的に排除するため、
    不正な出力は無言で埋めず、生テキストを添えてValueErrorを送出する
    (_auto_segment_narrativeと同じ「パースは1回、失敗は即エラー」方針)。"""
    text = _extract_json_array_text(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            raise ValueError(f"JSON配列が見つかりません:\n{raw}")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON構文エラー({e}):\n{raw}")

    if not isinstance(data, list):
        raise ValueError(f"JSON配列(list)ではありません:\n{raw}")

    seen: set[int] = set()
    for item in data:
        if not isinstance(item, dict) or "segment" not in item:
            raise ValueError(f"各要素に整数の\"segment\"キーが必要です:\n{raw}")
        seen.add(item["segment"])

    if seen != set(range(1, count + 1)):
        raise ValueError(f"segment番号が1..{count}と一致しません(実際: {sorted(seen)}):\n{raw}")

    return sorted(data, key=lambda d: d["segment"])


async def _chat_json(
    client: AsyncOpenAI, model: str, system_prompt: str, user_msg: str,
    count: int, temperature: float, max_tokens: int,
) -> list[dict]:
    """JSON構造化出力Pass共通の呼び出し+パース+リトライ。1回目のパースに失敗したら、
    より強い「JSON配列だけを出力せよ」というリマインダーを添えて1回だけ再送する
    (_JSON_PARSE_MAX_ATTEMPTS回で打ち切り)。それでも失敗すれば最後のエラーをそのまま送出する。"""
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    last_error: ValueError | None = None
    for attempt in range(1, _JSON_PARSE_MAX_ATTEMPTS + 1):
        resp = await client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            raw = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
        try:
            return _parse_json_segments(raw, count)
        except ValueError as e:
            last_error = e
            if attempt < _JSON_PARSE_MAX_ATTEMPTS:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": "/no_think\nOutput ONLY the corrected JSON array. No markdown code "
                                "fences, no commentary, no explanation before or after it.",
                })
    assert last_error is not None
    raise last_error


async def _plan_creative_intent_json(
    global_desc: str, segments: list[dict], orientation: str, system_prompt: str,
) -> list[dict]:
    """Pass0(JSON版): _plan_creative_intentの自由記述版と対になる新経路。
    Creative Director/State Trackerどちらの呼び出しにも使う(system_promptで切替、旧版と同じ構造)。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    seg_list = "\n".join(
        f"{i}. ({seg['duration']}s) {seg['action']}"
        for i, seg in enumerate(segments, 1)
    )
    orient_note = "16:9 widescreen landscape" if orientation == "horizontal" else "9:16 vertical portrait"
    user_msg = (
        f"/no_think\n"
        f"## Video style / setting\n{global_desc}\n\n"
        f"## Orientation\n{orient_note}\n\n"
        f"## Segments ({len(segments)} total)\n{seg_list}"
    )
    return await _chat_json(client, cfg.LLM_MODEL, system_prompt, user_msg, len(segments), 0.8, 4096)


async def _plan_shot_directions_json(
    global_desc: str, segments: list[dict], orientation: str, intents: list[str], system_prompt: str,
) -> list[str]:
    """Pass1(JSON版): _plan_shot_directionsの自由記述版と対になる新経路。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    seg_list = "\n".join(
        f"{i}. ({seg['duration']}s) {seg['action']}"
        + (f"\n   Creative intent: {intent}" if intent else "")
        for i, (seg, intent) in enumerate(zip(segments, intents), 1)
    )
    orient_note = (
        "16:9 WIDESCREEN LANDSCAPE — wide lateral space: LATERAL crossing and horizontal compositions work well"
        if orientation == "horizontal" else
        "9:16 VERTICAL PORTRAIT — very narrow lateral space: LATERAL crossing is FORBIDDEN; "
        "for walking use toward/away from lens, tracking follow, or tight patterns; "
        "favor vertical compositions (low/high angle, sky above and ground below)"
    )
    user_msg = (
        f"/no_think\n"
        f"## Video style / setting\n{global_desc}\n\n"
        f"## Orientation\n{orient_note}\n\n"
        f"## Segments ({len(segments)} total)\n{seg_list}"
    )
    items = await _chat_json(client, cfg.LLM_MODEL, system_prompt, user_msg, len(segments), 0.8, 1024)
    raw_dirs = [(d.get("direction") or "").strip() or "eye level, static handheld, medium" for d in items]
    return [_enforce_shot_rules(seg["action"], d) for seg, d in zip(segments, raw_dirs)]


async def _audit_shot_variety_json(
    segments: list[dict], directions: list[str], orientation: str, system_prompt: str,
) -> list[str]:
    """Pass1.5(JSON版): _audit_shot_varietyの自由記述版と対になる新経路。
    directionが独立したJSONフィールドになるため、旧版が対処していた「actionタグの
    漏れ混入」(re.sub `^\\[[^\\]]*\\]\\s*`)は構造的に発生しなくなり不要。
    ただし「空/パース対象外のセグメントは監査前のdirectionを維持する」フォールバックは維持する。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    dir_list = "\n".join(
        f"{i}. action: {seg['action']} | current direction: {d}"
        for i, (seg, d) in enumerate(zip(segments, directions), 1)
    )
    orient_note = (
        "16:9 WIDESCREEN LANDSCAPE" if orientation == "horizontal"
        else "9:16 VERTICAL PORTRAIT (no lateral crossing patterns)"
    )
    user_msg = (
        f"/no_think\n"
        f"## Orientation\n{orient_note}\n\n"
        f"## Shot list ({len(segments)} segments)\n{dir_list}"
    )
    items = await _chat_json(client, cfg.LLM_MODEL, system_prompt, user_msg, len(segments), 0.4, 1024)
    result: list[str] = []
    for seg, orig, item in zip(segments, directions, items):
        d = (item.get("direction") or "").strip() or orig
        result.append(_enforce_shot_rules(seg["action"], d))
    return result


async def _write_scene_description(
    global_desc: str, action: str, direction: str, duration: int, orientation: str,
    system_prompt: str, intent: str = "", reserved_props: list[str] | None = None, retry_note: str = "",
) -> str:
    """Pass 2: カメラ方向と動作から、スクリーン上の空間的現実を記述する。"""
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    orient_note = "16:9 widescreen landscape (1280×720)" if orientation == "horizontal" else "9:16 vertical portrait (720×1280)"
    intent_block = (
        f"## Creative intent (what this shot must communicate — its STATE line is the authoritative "
        f"time-of-day/lighting/weather/outfit for this shot, overriding the reference's baseline)\n{intent}\n\n"
        if intent else ""
    )
    reserved_block = (
        f"## Props reserved for other segments (must NOT appear in this shot)\n{', '.join(reserved_props)}\n\n"
        if reserved_props else ""
    )
    retry_block = f"## Previous attempt was REJECTED (must fix)\n{retry_note}\n\n" if retry_note else ""
    user_msg = (
        f"/no_think\n"
        f"## Character & Setting\n{global_desc}\n\n"
        f"{intent_block}"
        f"{reserved_block}"
        f"{retry_block}"
        f"## Camera direction (from shot director)\n{direction}\n\n"
        f"## Shot action ({duration}s)\n{action}\n\n"
        f"## Frame orientation\n{orient_note}"
    )
    resp = await client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.7,
        max_tokens=1024,
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        content = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
    return content
