import fs from "node:fs";
import path from "node:path";
import { GENERATED_DIR, CASS_OUTPUT_DIR, BGM_DIR, PREFIXES } from "./config.js";

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

const SEG_HEADER_RE = /^\[(\d+)\/(\d+)\]\s+(\S+)\s+\(\d+s\)\s*$/gm;

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
