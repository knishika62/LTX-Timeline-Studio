import fs from "node:fs";
import path from "node:path";
import { PROMPT_DIR, GENERATED_DIR, PREFIXES } from "./config.js";
import { runBridge } from "./proc.js";
import { SEG_HEADER_RE as PROMPTS_SEG_HEADER_RE } from "./scan.js";

/** prompt/ の .txt 一覧(名前降順 = タイムスタンプ命名なら新しい順) */
export function listPromptFiles() {
  let names;
  try {
    names = fs.readdirSync(PROMPT_DIR).filter((n) => n.endsWith(".txt"));
  } catch {
    return [];
  }
  names.sort((a, b) => b.localeCompare(a));
  return names.map((name) => ({ name, path: path.join(PROMPT_DIR, name) }));
}

function safePromptPath(name) {
  const base = path.basename(name);
  if (!base || base !== name || !base.endsWith(".txt")) throw new Error(`invalid prompt filename: ${name}`);
  return path.join(PROMPT_DIR, base);
}

export function readPromptFile(name) {
  return fs.readFileSync(safePromptPath(name), "utf-8");
}

export function savePromptFile(name, text) {
  let n = name.trim();
  if (!n.endsWith(".txt")) n += ".txt";
  const p = safePromptPath(n);
  fs.mkdirSync(PROMPT_DIR, { recursive: true });
  fs.writeFileSync(p, text, "utf-8");
  return { name: n, path: p };
}

/** _parse_prompt(timeline_common.py)によるパース検証(bridge経由、テキストはstdinで渡す) */
export async function validatePrompt(text) {
  try {
    const res = await runBridge(["parse_prompt", "-"], { stdin: text });
    const last = res.segments[res.segments.length - 1];
    return { ok: true, segments: res.segments.length, totalSeconds: last.end, globalDesc: res.global_desc };
  } catch (e) {
    return { ok: false, error: String(e.message) };
  }
}

/** 指定セグメントの保存済みプロンプトを読む(既存CLIの_parse_prompts_txtをbridge経由で再利用) */
export async function readSegmentPrompt(engine, runId, segNum) {
  const prefix = PREFIXES[engine];
  const promptsPath = path.join(GENERATED_DIR, `${prefix}_${runId}_prompts.txt`);
  if (!fs.existsSync(promptsPath)) return null;
  const res = await runBridge(["parse_prompts_txt", engine, promptsPath]);
  return res.segments.find((s) => s.num === segNum) ?? null;
}

/** 指定セグメントの `--- LTX prompt ---`(i2vは `--- Keyframe prompt ---` も)を書き換える。
 * Gradio版 _write_segment_prompt のJS移植(ハーネス固有コード)。既存CLIの
 * _parse_prompts_txt がそのまま読めるフォーマットを維持し、他セグメント・ヘッダーは触らない。 */
export function writeSegmentPrompt(engine, runId, segNum, newPrompt, newKfPrompt = null) {
  const prefix = PREFIXES[engine];
  const promptsPath = path.join(GENERATED_DIR, `${prefix}_${runId}_prompts.txt`);
  if (!fs.existsSync(promptsPath)) return false;
  const text = fs.readFileSync(promptsPath, "utf-8");

  const heads = [...text.matchAll(PROMPTS_SEG_HEADER_RE)];
  const targetIdx = heads.findIndex((m) => parseInt(m[1], 10) === segNum);
  if (targetIdx < 0) return false;
  const start = heads[targetIdx].index + heads[targetIdx][0].length;
  const end = targetIdx + 1 < heads.length ? heads[targetIdx + 1].index : text.length;
  let block = text.slice(start, end);

  const ltxM = /--- LTX prompt ---\n/.exec(block);
  if (!ltxM) return false;
  if (engine === "i2v" && newKfPrompt != null) {
    const kfM = /--- Keyframe prompt ---\n/.exec(block);
    if (kfM) {
      block =
        block.slice(0, kfM.index + kfM[0].length) +
        newKfPrompt.trim() + "\n" +
        block.slice(ltxM.index, ltxM.index + ltxM[0].length) +
        newPrompt.trim() + "\n";
    } else {
      block = block.slice(0, ltxM.index + ltxM[0].length) + newPrompt.trim() + "\n";
    }
  } else {
    block = block.slice(0, ltxM.index + ltxM[0].length) + newPrompt.trim() + "\n";
  }

  fs.writeFileSync(promptsPath, text.slice(0, start) + block + text.slice(end), "utf-8");
  return true;
}

/** 動画パス → 元プロンプトファイル(prompt/配下)の逆引き。
 * `{prefix}_{run_id}_final...mp4` にマッチする場合のみ解決できる(Gradio版と同じ)。 */
export function resolvePromptSourceForVideo(videoPath) {
  if (!videoPath) return null;
  const name = path.basename(videoPath);
  for (const prefix of Object.values(PREFIXES)) {
    const m = new RegExp(`^${prefix}_(\\d{8}_\\d{6})_final`).exec(name);
    if (!m) continue;
    const promptsTxt = path.join(GENERATED_DIR, `${prefix}_${m[1]}_prompts.txt`);
    if (!fs.existsSync(promptsTxt)) return null;
    let firstLine;
    try {
      firstLine = fs.readFileSync(promptsTxt, "utf-8").split("\n", 1)[0];
    } catch {
      return null;
    }
    const sm = /^source:\s*(.+)/.exec(firstLine);
    if (!sm) return null;
    const sourcePath = path.join(PROMPT_DIR, sm[1].trim());
    return fs.existsSync(sourcePath) ? sourcePath : null;
  }
  return null;
}
