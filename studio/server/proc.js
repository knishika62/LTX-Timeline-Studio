import { spawn } from "node:child_process";
import { BASE_DIR, CONDA } from "./config.js";

/** conda env 内で python スクリプトを起動する引数列を作る(-u で無バッファ化。
 * パイプ接続時の Python stdout はフルバッファリングされ、ログが「終了時に一括」に
 * なってしまう——Gradio版と同じ理由で必須)。 */
export function condaPythonArgs(env, script, args = []) {
  return [CONDA, ["run", "--no-capture-output", "-n", env, "python", "-u", script, ...args]];
}

/** conda env 内で python モジュールとして起動する(`python -m`)。
 * 生成CLIをシンボリックリンク経由で `python script.py` として直接実行すると、Python 3.11+ が
 * メインスクリプトのパスを realpath 解決してしまい、sys.path[0] がリンク先の実体ディレクトリになる
 * (= pipeline_config 等のimportも generated/ の書き込み先もリンク先基準になってしまう、実機で踏んだ罠)。
 * `-m` なら sys.path[0] = cwd(本フォルダ)のままなので、この問題が起きない。 */
export function condaPythonModuleArgs(env, module, args = []) {
  return [CONDA, ["run", "--no-capture-output", "-n", env, "python", "-u", "-m", module, ...args]];
}

/** コマンドを実行し、stdout+stderr を行コールバックへ流す。resolve は exit code。
 * detached: true でプロセスグループを新設する(Stop時に conda run だけでなく、その配下で
 * 実際にGPU処理を行う python 子プロセスまで確実に止めるため——conda run はSIGTERMを
 * 子へ転送しないことがあり、pid一つだけkillしても実処理が動き続ける事故があった)。 */
export function streamCommand(cmd, args, { cwd = BASE_DIR, onLine, signal, detached = false } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd, args, { cwd, env: process.env, detached });
    if (signal) signal.proc = proc;
    let buf = "";
    const feed = (chunk) => {
      buf += chunk.toString("utf-8");
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        onLine?.(buf.slice(0, i + 1));
        buf = buf.slice(i + 1);
      }
    };
    proc.stdout.on("data", feed);
    proc.stderr.on("data", feed);
    proc.on("error", reject);
    proc.on("close", (code) => {
      if (buf) onLine?.(buf);
      resolve(code);
    });
  });
}

/** コマンドを実行して stdout を文字列で返す(非0終了は stderr 込みで reject)。 */
export function runCommand(cmd, args, { cwd = BASE_DIR, stdin } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(cmd, args, { cwd, env: process.env });
    let out = "";
    let err = "";
    proc.stdout.on("data", (c) => (out += c));
    proc.stderr.on("data", (c) => (err += c));
    proc.on("error", reject);
    proc.on("close", (code) => {
      if (code === 0) resolve(out);
      else {
        const e = new Error(`${cmd} ${args.join(" ")} failed (exit ${code}): ${err || out}`);
        e.stdout = out; // bridge はエラー時も stdout に {"ok": false, "error": ...} を出す
        reject(e);
      }
    });
    if (stdin != null) proc.stdin.write(stdin);
    proc.stdin.end();
  });
}

/** bridge.py のサブコマンドを呼び、JSONを返す。失敗時はPython側の error メッセージで throw
 * (condaラッパーの定型エラー文ではなく、stdout のJSONから本当の原因を取り出す)。 */
export async function runBridge(args, { stdin } = {}) {
  const [cmd, fullArgs] = condaPythonArgs("x-post", "bridge.py", args);
  let out;
  try {
    out = await runCommand(cmd, fullArgs, { stdin });
  } catch (e) {
    const lines = (e.stdout || "").trim().split("\n");
    for (const line of lines.reverse()) {
      try {
        const parsed = JSON.parse(line);
        if (parsed && parsed.ok === false) throw new Error(parsed.error);
      } catch (inner) {
        if (inner.message !== "Unexpected token" && !(inner instanceof SyntaxError)) throw inner;
      }
    }
    throw e;
  }
  const parsed = JSON.parse(out.trim().split("\n").pop());
  if (!parsed.ok) throw new Error(parsed.error);
  return parsed;
}

/** ffprobe で動画の尺(秒)を返す。 */
export async function ffprobeDuration(filePath) {
  const out = await runCommand("ffprobe", [
    "-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", filePath,
  ]);
  return parseFloat(out.trim());
}
