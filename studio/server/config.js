import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import dotenv from "dotenv";

// studio/server/config.js はリポジトリ直下から2階層下(studio/server/)にあるため、
// server/config.js(1階層下)とは異なり ".." を2回たどってBASE_DIRを算出する必要がある
// (2026-07-17 studio自己完結化: server/*.jsをstudio/server/へ複製した際の唯一の差分)。
export const BASE_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const PROMPT_DIR = path.join(BASE_DIR, "prompt");
export const GENERATED_DIR = path.join(BASE_DIR, "generated");
export const CASS_DIR = path.join(BASE_DIR, "CASS");
export const BGM_DIR = path.join(CASS_DIR, "bgm");
export const CASS_OUTPUT_DIR = path.join(CASS_DIR, "output");
export const UPLOADS_DIR = path.join(BASE_DIR, "uploads");
export const EDIT_TMP_DIR = path.join(GENERATED_DIR, "_edit_tmp");

export const SCRIPTS = { t2v: "t2v_timeline_cliV6.py", i2v: "i2v_timeline_cliV6.py" };
export const PREFIXES = { t2v: "t2v6", i2v: "i2v6" };

// リポジトリ直下の venv(python -m venv venv / uv venv venv、どちらも同じフォルダ構造)を
// 直接叩く。v6本体・CASSの依存は衝突しないため単一venvで両方賄う(2026-07-18検証済み)。
// conda/uv等のツールをNode側は一切意識しない。既定の場所以外にvenvを置きたい場合のみ
// .envのPYTHON_BINで上書きする(パス変更はサーバー再起動が前提でよいためprocess.envを直読み)。
const isWin = process.platform === "win32";
export const MAIN_PYTHON = process.env.PYTHON_BIN ||
  path.join(BASE_DIR, "venv", isWin ? "Scripts" : "bin", isWin ? "python.exe" : "python");

// .env は process.env に読み込まず、使う瞬間に毎回パースする。
// 子プロセス(生成CLI)にも .env 由来のキーを一切注入しない——子の load_dotenv() が
// 常に最新の .env を読むため、「ハーネス起動中の .env 変更が子プロセスに反映されない」
// という、process.env経由で一度読み込んでしまう実装だと起きがちな罠が構造的に起きない。
export function readEnv() {
  try {
    return dotenv.parse(fs.readFileSync(path.join(BASE_DIR, ".env")));
  } catch {
    return {};
  }
}
