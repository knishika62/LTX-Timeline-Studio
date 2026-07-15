import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import dotenv from "dotenv";

export const BASE_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const PROMPT_DIR = path.join(BASE_DIR, "prompt");
export const GENERATED_DIR = path.join(BASE_DIR, "generated");
export const CASS_DIR = path.join(BASE_DIR, "CASS");
export const BGM_DIR = path.join(CASS_DIR, "bgm");
export const CASS_OUTPUT_DIR = path.join(CASS_DIR, "output");
export const UPLOADS_DIR = path.join(BASE_DIR, "uploads");
export const EDIT_TMP_DIR = path.join(GENERATED_DIR, "_edit_tmp");

export const PORT = 7864;

export const SCRIPTS = { t2v: "t2v_timeline_cliV6.py", i2v: "i2v_timeline_cliV6.py" };
export const PREFIXES = { t2v: "t2v6", i2v: "i2v6" };

// conda はシェル関数なので Node の spawn からは実バイナリを直接叩く
export const CONDA = process.env.CONDA_EXE || "/opt/miniconda3/bin/conda";

// .env は process.env に読み込まず、使う瞬間に毎回パースする。
// 子プロセス(生成CLI)にも .env 由来のキーを一切注入しない——子の load_dotenv() が
// 常に最新の .env を読むため、Gradio版で踏んだ「ハーネス起動中の .env 変更が
// 子プロセスに反映されない罠」(x-post/CLAUDE.md 2026-07-14)が構造的に起きない。
export function readEnv() {
  try {
    return dotenv.parse(fs.readFileSync(path.join(BASE_DIR, ".env")));
  } catch {
    return {};
  }
}
