import fs from "node:fs";
import path from "node:path";
import { GENERATED_DIR, EDIT_TMP_DIR, PREFIXES } from "./config.js";
import { runCommand, runBridge, ffprobeDuration } from "./proc.js";

/** トリム無し(両方0)なら元ファイルをそのまま返す。ありなら再エンコードで正確にカット
 * (Gradio版 _trim_segment と同一のffmpeg引数。元ファイルは一切変更しない)。 */
export async function trimSegment(filePath, trimStart, trimEnd, outDir = EDIT_TMP_DIR) {
  const ts = Math.max(0, Number(trimStart) || 0);
  const te = Math.max(0, Number(trimEnd) || 0);
  if (ts <= 0 && te <= 0) return filePath;
  fs.mkdirSync(outDir, { recursive: true });
  const dur = await ffprobeDuration(filePath);
  const end = Math.max(ts + 0.1, dur - te);
  const out = path.join(outDir, `${path.parse(filePath).name}_trimmed.mp4`);
  await runCommand("ffmpeg", [
    "-y", "-loglevel", "error", "-i", filePath, "-ss", String(ts), "-to", String(end),
    "-c:v", "libx264", "-c:a", "aac", out,
  ]);
  return out;
}

/** removed以外のセグメントを現在の並び順でトリム→連結(_concat_segmentsはbridge経由、
 * 末尾フェードアウト等の挙動をPython実装のまま維持)。 */
async function assembleEdit(segments, outPath) {
  const active = segments.filter((s) => !s.removed);
  if (!active.length) throw new Error("all segments are removed");
  fs.mkdirSync(EDIT_TMP_DIR, { recursive: true });
  const trimmed = [];
  for (const s of active) {
    trimmed.push(await trimSegment(s.path, s.trimStart, s.trimEnd));
  }
  await runBridge(["concat"], { stdin: JSON.stringify({ paths: trimmed, out: outPath }) });
  return outPath;
}

/** 全体の並び替え・トリム結果を仮組み(保存しない) */
export async function editPreview(segments) {
  const out = path.join(EDIT_TMP_DIR, `preview_${Date.now()}.mp4`);
  fs.mkdirSync(EDIT_TMP_DIR, { recursive: true });
  await assembleEdit(segments, out);
  return out;
}

/** runの final.mp4 を上書き(既存は _oldN 退避——i2v_timeline_cliV6._run_retry と同じパターン) */
export async function editCommit(engine, runId, segments) {
  const prefix = PREFIXES[engine];
  const finalPath = path.join(GENERATED_DIR, `${prefix}_${runId}_final.mp4`);
  if (fs.existsSync(finalPath)) {
    let k = 1;
    let backup;
    do {
      backup = path.join(GENERATED_DIR, `${prefix}_${runId}_final_old${k}.mp4`);
      k += 1;
    } while (fs.existsSync(backup));
    fs.renameSync(finalPath, backup);
  }
  await assembleEdit(segments, finalPath);
  return finalPath;
}
