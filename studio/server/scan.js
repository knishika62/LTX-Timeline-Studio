import fs from "node:fs";
import path from "node:path";
import { GENERATED_DIR, CASS_OUTPUT_DIR, BGM_DIR, PREFIXES, PROMPT_DIR } from "./config.js";

const MAX_LIST_ITEMS = 50;

const KF_SEG_RE = /_seg(\d+)_kf\.png$/;
const SEG_VIDEO_RE = /_seg(\d+)_(.+)\.mp4$/;
// 完成品のみ(final / final_FHD / final_oldN)。セグメント別動画は大量にあるため除外
// (Gradio版で「選んでも意味の無い候補で埋まる」とユーザー指摘があった仕様を踏襲)
const FINAL_VIDEO_RE = /_final(_FHD)?(_old\d+)?\.mp4$/;

function listDirSafe(dir) {
  try {
    return fs.readdirSync(dir);
  } catch {
    return [];
  }
}

function mtimeOf(p) {
  try {
    return fs.statSync(p).mtimeMs;
  } catch {
    return 0;
  }
}

/** i2v のキーフレーム画像一覧: [{path, seg, caption}] (seg番号順) */
export function scanKeyframes(prefix, runId) {
  if (prefix !== "i2v6" || !runId) return [];
  const out = [];
  for (const name of listDirSafe(GENERATED_DIR)) {
    if (!name.startsWith(`${prefix}_${runId}_seg`) || !name.endsWith("_kf.png")) continue;
    if (name.includes("_old")) continue;
    const m = KF_SEG_RE.exec(name);
    if (!m) continue;
    const seg = parseInt(m[1], 10);
    const p = path.join(GENERATED_DIR, name);
    out.push({ path: p, seg, caption: `seg${String(seg).padStart(2, "0")}`, mt: mtimeOf(p) });
  }
  out.sort((a, b) => a.seg - b.seg);
  return out;
}

/** セグメント別動画一覧: [{num, label, path}] (num順) */
export function scanSegmentVideos(prefix, runId) {
  if (!runId) return [];
  const out = [];
  for (const name of listDirSafe(GENERATED_DIR)) {
    if (!name.startsWith(`${prefix}_${runId}_seg`) || !name.endsWith(".mp4")) continue;
    if (name.replace(/\.mp4$/, "").includes("_old")) continue;
    const m = SEG_VIDEO_RE.exec(name);
    if (!m) continue;
    const p = path.join(GENERATED_DIR, name);
    // mt はブラウザ側のキャッシュバスター(retryで同名ファイルが上書きされても再取得させる)
    out.push({ num: parseInt(m[1], 10), label: m[2], path: p, mt: mtimeOf(p) });
  }
  out.sort((a, b) => a.num - b.num);
  return out;
}

export function finalVideoPath(prefix, runId) {
  if (!runId) return null;
  const p = path.join(GENERATED_DIR, `${prefix}_${runId}_final.mp4`);
  return fs.existsSync(p) ? p : null;
}

export function finalVideoMtime(finalPath) {
  if (!finalPath || !fs.existsSync(finalPath)) return null;
  return new Date(fs.statSync(finalPath).mtimeMs).toISOString();
}

/** launch後に新規作成された prompts.txt から run_id を逆引き(標準出力のパース不要、Gradio版と同方式) */
export function findRunId(prefix, afterTsMs) {
  const re = new RegExp(`^${prefix}_(\\d{8}_\\d{6})_prompts\\.txt$`);
  let best = null;
  for (const name of listDirSafe(GENERATED_DIR)) {
    const m = re.exec(name);
    if (!m) continue;
    const mt = mtimeOf(path.join(GENERATED_DIR, name));
    if (mt >= afterTsMs && (!best || mt > best.mt)) best = { runId: m[1], mt };
  }
  return best ? best.runId : null;
}

/** prompts.txt のヘッダー行(先頭数行の key: value)を読む */
export function readPromptsHeader(prefix, runId) {
  const p = path.join(GENERATED_DIR, `${prefix}_${runId}_prompts.txt`);
  const header = {};
  try {
    const lines = fs.readFileSync(p, "utf-8").split("\n").slice(0, 8);
    for (const line of lines) {
      const i = line.indexOf(":");
      if (i > 0) header[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    }
  } catch {
    /* no prompts.txt */
  }
  return header;
}

/** run一覧: [{runId, source, label}](mtime降順、直近MAX_LIST_ITEMS件) */
export function listRuns(engine) {
  const prefix = PREFIXES[engine];
  const re = new RegExp(`^${prefix}_(\\d{8}_\\d{6})_prompts\\.txt$`);
  const cands = [];
  for (const name of listDirSafe(GENERATED_DIR)) {
    const m = re.exec(name);
    if (m) cands.push({ runId: m[1], mt: mtimeOf(path.join(GENERATED_DIR, name)), file: name });
  }
  cands.sort((a, b) => b.mt - a.mt);
  return cands.slice(0, MAX_LIST_ITEMS).map(({ runId, file }) => {
    let source = "";
    try {
      const firstLine = fs.readFileSync(path.join(GENERATED_DIR, file), "utf-8").split("\n", 1)[0];
      const sm = /^source:\s*(.+)/.exec(firstLine);
      if (sm) source = sm[1].trim();
    } catch {
      /* unreadable */
    }
    return { runId, source, label: source ? `${runId} (${source})` : runId };
  });
}

/** 完成品動画一覧(generated/ の final系 + CASS/output/ 全件、mtime降順、直近50件) */
export function listGeneratedVideos() {
  const items = [];
  for (const name of listDirSafe(GENERATED_DIR)) {
    if (name.endsWith(".mp4") && FINAL_VIDEO_RE.test(name)) {
      const p = path.join(GENERATED_DIR, name);
      items.push({ name, path: p, mt: mtimeOf(p) });
    }
  }
  for (const name of listDirSafe(CASS_OUTPUT_DIR)) {
    if (name.endsWith(".mp4")) {
      const p = path.join(CASS_OUTPUT_DIR, name);
      items.push({ name, path: p, mt: mtimeOf(p) });
    }
  }
  items.sort((a, b) => b.mt - a.mt);
  return items.slice(0, MAX_LIST_ITEMS).map(({ name, path: p, mt }) => ({ name, path: p, mt }));
}

/** CASS/bgm/ の再利用BGM一覧(mtime降順) */
export function listBgmFiles() {
  const exts = new Set([".mp3", ".wav", ".m4a"]);
  const items = [];
  for (const name of listDirSafe(BGM_DIR)) {
    if (exts.has(path.extname(name).toLowerCase())) {
      const p = path.join(BGM_DIR, name);
      items.push({ name, path: p, mt: mtimeOf(p) });
    }
  }
  items.sort((a, b) => b.mt - a.mt);
  return items.map(({ name, path: p, mt }) => ({ name, path: p, mt }));
}

// prompts.txt のセグメント見出し正規表現(唯一の定義、prompts.jsもここから読む)
export const SEG_HEADER_RE = /^\[(\d+)\/(\d+)\]\s+(\S+)\s+\(\d+(?:\.\d+)?s\)\s*$/gm; // 小数秒も許容(2026-07-18)

/** prompts.txt のセグメント見出し([N/total] LABEL (Xs))から期待セグメント一覧を得る。
 * 生成・リトライ中、まだファイルが無い/一時的に消えたセグメントの
 * 「生成中…」プレースホルダ表示に使う(数と番号は最初から分かっているため)。 */
export function expectedSegments(prefix, runId) {
  if (!runId) return [];
  try {
    const text = fs.readFileSync(path.join(GENERATED_DIR, `${prefix}_${runId}_prompts.txt`), "utf-8");
    return [...text.matchAll(SEG_HEADER_RE)].map((m) => ({ num: parseInt(m[1], 10), label: m[3] }));
  } catch {
    return [];
  }
}

// ============================================================
// Library タブ用(閲覧専用)。Generate/Retry/Editが使う scanKeyframes/scanSegmentVideos
// (_old系を意図的に除外した「現在有効な状態」)とは別に、_old系バックアップも含めた
// run内の全ファイルを一覧するための専用スキャン。
// ============================================================

const KF_ALL_RE = /_seg(\d+)_kf(?:_old(\d+))?\.png$/;
const SEG_VIDEO_ALL_RE = /_seg(\d+)_(.+?)(?:_old(\d+))?\.mp4$/;
const FINAL_ALL_RE = /_final(_FHD)?(?:_old(\d+))?\.mp4$/;

/** キーフレーム全件(_old込み)。variant: null(現行) | "oldN" */
export function scanAllKeyframes(prefix, runId) {
  if (prefix !== "i2v6" || !runId) return [];
  const out = [];
  for (const name of listDirSafe(GENERATED_DIR)) {
    if (!name.startsWith(`${prefix}_${runId}_seg`)) continue;
    const m = KF_ALL_RE.exec(name);
    if (!m) continue;
    const p = path.join(GENERATED_DIR, name);
    out.push({ seg: parseInt(m[1], 10), path: p, mt: mtimeOf(p), variant: m[2] ? `old${m[2]}` : null });
  }
  out.sort((a, b) => a.seg - b.seg || (a.variant ? 1 : 0) - (b.variant ? 1 : 0));
  return out;
}

/** セグメント動画全件(_old込み)。variant: null(現行) | "oldN" */
export function scanAllSegmentVideos(prefix, runId) {
  if (!runId) return [];
  const out = [];
  for (const name of listDirSafe(GENERATED_DIR)) {
    if (!name.startsWith(`${prefix}_${runId}_seg`)) continue;
    const m = SEG_VIDEO_ALL_RE.exec(name);
    if (!m) continue;
    const p = path.join(GENERATED_DIR, name);
    out.push({
      num: parseInt(m[1], 10), label: m[2], path: p, mt: mtimeOf(p),
      variant: m[3] ? `old${m[3]}` : null,
    });
  }
  out.sort((a, b) => a.num - b.num || (a.variant ? 1 : 0) - (b.variant ? 1 : 0));
  return out;
}

/** final動画全件(final / final_FHD / final_oldN / final_FHD_oldN)。isFHD・variantで区別 */
export function scanAllFinals(prefix, runId) {
  if (!runId) return [];
  const out = [];
  for (const name of listDirSafe(GENERATED_DIR)) {
    if (!name.startsWith(`${prefix}_${runId}_final`)) continue;
    const m = FINAL_ALL_RE.exec(name);
    if (!m) continue;
    const p = path.join(GENERATED_DIR, name);
    out.push({ path: p, mt: mtimeOf(p), isFHD: !!m[1], variant: m[2] ? `old${m[2]}` : null });
  }
  out.sort((a, b) => b.mt - a.mt);
  return out;
}

/** CASS/output/ 内でこのrunのfinal由来のファイルを収集。
 * ファイル名が `{prefix}_{runId}_final` から始まる全エントリを機械的に拾う——
 * 二重リミックス(_remixed_remixed.mp4)や旧final(_old1)由来のリミックスも
 * prefixマッチなので自然に含まれる。 */
export function scanCassOutputs(prefix, runId) {
  if (!runId) return { videos: [], stems: [] };
  const prefixName = `${prefix}_${runId}_final`;
  const videos = [];
  const stems = [];
  for (const name of listDirSafe(CASS_OUTPUT_DIR)) {
    if (!name.startsWith(prefixName)) continue;
    const full = path.join(CASS_OUTPUT_DIR, name);
    if (name.endsWith(".mp4")) {
      videos.push({ path: full, name, mt: mtimeOf(full) });
    } else if (name.endsWith("_stems")) {
      for (const kind of ["speech", "sfx", "music"]) {
        const p = path.join(full, `${kind}.wav`);
        if (fs.existsSync(p)) stems.push({ path: p, kind, group: name, mt: mtimeOf(p) });
      }
    }
  }
  videos.sort((a, b) => b.mt - a.mt);
  stems.sort((a, b) => b.mt - a.mt);
  return { videos, stems };
}

/** prompts.txt の生テキスト */
export function readPromptsRaw(prefix, runId) {
  try {
    return fs.readFileSync(path.join(GENERATED_DIR, `${prefix}_${runId}_prompts.txt`), "utf-8");
  } catch {
    return "";
  }
}

const LIBRARY_MAX_RESULTS = 200;

/** "today"|"7d"|"30d"|"all"|undefined をエポックms(この時刻以降のみ対象)に変換。
 * サーバー側で計算することでクライアントの時計ズレ・タイムゾーン差を気にしなくてよい。 */
export function presetToSinceMs(preset) {
  const now = Date.now();
  const DAY = 24 * 60 * 60 * 1000;
  switch (preset) {
    case "today": {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return d.getTime();
    }
    case "7d":
      return now - 7 * DAY;
    case "30d":
      return now - 30 * DAY;
    default:
      return null; // "all" 含め無指定はフィルタなし
  }
}

/** 全engine統合のrun一覧(mtime降順)。engine/sinceMs/q で絞り込み、件数急増に備え
 * 上限 LIBRARY_MAX_RESULTS 件でスライスする(過去の全履歴を見る用途なので他の一覧のように
 * 常時50件に切り詰めはしない——絞り込み後もなお超過する場合のみ安全弁として働く)。
 * カード用のサムネイル存在チェックも、絞り込み後の対象だけに行うため件数が増えても軽い。 */
export function listLibraryRuns({ engine, sinceMs, q } = {}) {
  const engines = engine && PREFIXES[engine] ? [[engine, PREFIXES[engine]]] : Object.entries(PREFIXES);
  const needle = q ? q.trim().toLowerCase() : "";
  const all = [];
  for (const [eng, prefix] of engines) {
    const re = new RegExp(`^${prefix}_(\\d{8}_\\d{6})_prompts\\.txt$`);
    for (const name of listDirSafe(GENERATED_DIR)) {
      const m = re.exec(name);
      if (!m) continue;
      const runId = m[1];
      const filePath = path.join(GENERATED_DIR, name);
      const mt = mtimeOf(filePath);
      if (sinceMs != null && mt < sinceMs) continue;

      let source = "";
      try {
        const firstLine = fs.readFileSync(filePath, "utf-8").split("\n", 1)[0];
        const sm = /^source:\s*(.+)/.exec(firstLine);
        if (sm) source = sm[1].trim();
      } catch {
        /* unreadable */
      }
      if (needle && !runId.toLowerCase().includes(needle) && !source.toLowerCase().includes(needle)) continue;

      const thumb = path.join(GENERATED_DIR, `${prefix}_${runId}_seg01_kf.png`);
      all.push({
        engine: eng, runId, source,
        label: source ? `${runId} (${source})` : runId,
        mt,
        thumbnail: eng === "i2v" && fs.existsSync(thumb) ? thumb : null,
      });
    }
  }
  all.sort((a, b) => b.mt - a.mt);
  const truncated = all.length > LIBRARY_MAX_RESULTS;
  return { runs: all.slice(0, LIBRARY_MAX_RESULTS), truncated, total: all.length };
}

/** 指定runの全ファイル一覧(Libraryタブの右ペイン用) */
export function libraryRunDetail(engine, runId) {
  const prefix = PREFIXES[engine];
  const header = readPromptsHeader(prefix, runId);
  // 大元のプロンプト(prompt/配下、Write Promptタブで書いた/読み込んだ生テキスト)。
  // prompts.txtはこれをPass0〜3で加工した結果なので別物——削除・リネーム済みなら取得できない。
  let sourceRaw = null;
  if (header.source) {
    try {
      sourceRaw = fs.readFileSync(path.join(PROMPT_DIR, header.source), "utf-8");
    } catch {
      sourceRaw = null;
    }
  }
  return {
    engine, runId,
    header,
    sourceRaw,
    promptsRaw: readPromptsRaw(prefix, runId),
    keyframes: scanAllKeyframes(prefix, runId),
    segments: scanAllSegmentVideos(prefix, runId),
    finals: scanAllFinals(prefix, runId),
    cass: scanCassOutputs(prefix, runId),
    expected: expectedSegments(prefix, runId),
  };
}

/** 指定runの生成物一式を削除(Libraryタブの削除機能)。libraryRunDetailが返すのと同じ
 * 4カテゴリ(keyframes/segments/finals/cass)+prompts.txt本体を対象にする——「右ペインで
 * 見えているもの全部」が消える、という分かりやすい対応関係にする。元プロンプトファイル
 * (prompt/配下)は対象外(他runでも再利用され得る、ユーザーが書いた入力のため)。 */
export function deleteLibraryRun(engine, runId) {
  const prefix = PREFIXES[engine];
  const cass = scanCassOutputs(prefix, runId);
  const files = [
    path.join(GENERATED_DIR, `${prefix}_${runId}_prompts.txt`),
    ...scanAllKeyframes(prefix, runId).map((k) => k.path),
    ...scanAllSegmentVideos(prefix, runId).map((s) => s.path),
    ...scanAllFinals(prefix, runId).map((f) => f.path),
    ...cass.videos.map((v) => v.path),
  ];
  const dirs = [...new Set(cass.stems.map((s) => path.dirname(s.path)))];

  let deletedFiles = 0;
  for (const f of files) {
    try {
      fs.unlinkSync(f);
      deletedFiles++;
    } catch {
      /* 既に無い等は無視 */
    }
  }
  let deletedDirs = 0;
  for (const d of dirs) {
    try {
      fs.rmSync(d, { recursive: true, force: true });
      deletedDirs++;
    } catch {
      /* 既に無い等は無視 */
    }
  }
  return { deletedFiles, deletedDirs };
}

/** run のスナップショット(Generate/Retryタブ表示用の一括取得) */
export function runSnapshot(engine, runId) {
  const prefix = PREFIXES[engine];
  const final = finalVideoPath(prefix, runId);
  const header = readPromptsHeader(prefix, runId);
  return {
    runId,
    keyframes: scanKeyframes(prefix, runId),
    segments: scanSegmentVideos(prefix, runId),
    expected: expectedSegments(prefix, runId),
    final,
    finalMtime: finalVideoMtime(final),
    orientation: header.orientation || null,
  };
}
