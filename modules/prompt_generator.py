"""i2vキーフレームプロンプト(Krea2系)の軽量補正パス。"""

from __future__ import annotations

from openai import AsyncOpenAI

from . import pipeline_config as cfg

_KREA2_EXPAND_SYSTEM = """\
You are a minimal touch-up editor for a keyframe photo-caption prompt fed to the
Krea 2 Turbo image model. The input is a short natural photo caption (~40-80 words).
Preserve it as a single flowing caption — do not add labels, line breaks, or bullet
points. Do NOT change any scene fact: location, wardrobe items, props, pose, character
identity, or lighting condition. This is a light touch-up, NOT a rewrite — your edit
budget is small (add or change roughly 10-20% of the words, never more).

Apply ONLY the rules below, and ONLY when their trigger condition is already true in
the input. Never invent a condition that isn't there.

1. FULL-BODY REINFORCEMENT — trigger: the caption already describes a full-body/
   full-length shot AND has more than a brief face/hair/eye/lip description (this
   model over-weights face detail, cropping to upper body despite full-body intent).
   Action: add 1 short reinforcing phrase (pick one: "full-body shot", "full-length
   portrait", "head-to-toe visible", "both feet visible", "camera pulled back enough",
   "no crop"). Skip if equivalent wording already exists, or if the shot isn't full-body.

2. POSITIVE BODY-DESCRIPTOR REPHRASING — trigger: the caption uses body-type words like
   "very thin", "minimal", "petite", "slender" describing the subject's build (this
   model over-interprets these toward "small/frail"). Action: swap in 1 positive-framed
   equivalent (pick one: "slim but healthy", "softly curvy", "naturally balanced
   proportions", "not underweight", "realistic adult body proportions") without changing
   the character's actual body type register (still slim, just not frail).

3. LIGHT-FABRIC INTERACTION — trigger: the caption already names BOTH a lighting
   condition/source (sun, window, backlight, lamp, candlelight, etc.) AND a garment
   fabric. Action: add a short clause naming how that light physically interacts with
   that fabric (transparency, sheen, shadow-printing) — e.g. "sheer fabric glowing as
   backlight soaks through" / "satin catching a sharp highlight". Do NOT invent a light
   source or fabric that isn't already mentioned.

Output ONLY the edited caption text. No explanations, no preamble, no markdown.
"""


async def expand_krea2_prompt(prompt: str) -> str:
    """Krea2専用の軽量プロンプト補正パス(2026-07-13追加)。docs/Krea2.mdの2件のXポスト
    (設計思想: 顔説明過多で上半身クロップに寄る/体型語の過剰解釈、ライティングサンプル:
    光の衣装素材への作用を明示すると効果的)から抽出した3ルールを、条件が既に成立している
    場合のみ軽く適用する。i2v_timeline_cliV6の40-80語キーフレームキャプション専用
    (i2v_timeline_cliV6._format_keyframe_prompt()の末尾から、KEYFRAME_WORKFLOW_JSONが
    Krea2系の時のみ呼ばれる)。40-80語キーフレームの肥大化が実際のアーティファクト・
    顔2つ等の破綻を引き起こした前例があるため、出力語数を入力語数比でガードし、
    失敗時・ガード抵触時はrawを返す(fail-open)。"""
    raw = (prompt or "").strip()
    if not raw:
        return prompt
    client = AsyncOpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
    try:
        resp = await client.chat.completions.create(
            model=cfg.LLM_MODEL,
            messages=[
                {"role": "system", "content": _KREA2_EXPAND_SYSTEM},
                {"role": "user", "content": f"/no_think\n{raw}"},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        result = (resp.choices[0].message.content or "").strip()
        if not result:
            return prompt

        in_words = len(raw.split())
        out_words = len(result.split())
        max_words = max(in_words * 1.25, in_words + 20)
        if out_words > max_words or out_words < in_words * 0.9:
            print(f"[prompt] krea2 expand rejected (length guard {in_words}→{out_words} words), using original")
            return prompt
        return result
    except Exception as e:
        print(f"[prompt] krea2 expand failed, using original: {e}")
        return prompt
