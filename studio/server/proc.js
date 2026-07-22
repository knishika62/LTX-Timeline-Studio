import { spawn } from "node:child_process";
import { BASE_DIR, MAIN_PYTHON } from "./config.js";

/** venv の python バイナリでスクリプトを起動する引数列を作る(-u で無バッファ化。
 * パイプ接続時の Python stdout はフルバッファリングされ、ログが「終了時に一括」に
 * なってしまう——Gradio版と同じ理由で必須)。pythonBin は呼び出し側が config.js の
 * MAIN_PYTHON を渡す(2026-07-18、conda run -n <env> ラッパーを撤廃しvenv直呼びに統一)。 */
export function pythonArgs(pythonBin, script, args = []) {
  return [pythonBin, ["-u", script, ...args]];
}

/** venv の python バイナリでモジュールとして起動する(`python -m`)。
 * 生成CLIをシンボリックリンク経由で `python script.py` として直接実行すると、Python 3.11+ が
 * メインスクリプトのパスを realpath 解決してしまい、sys.path[0] がリンク先の実体ディレクトリになる
 * (= pipeline_config 等のimportも generated/ の書き込み先もリンク先基準になってしまう、実機で踏んだ罠)。
 * `-m` なら sys.path[0] = cwd(本フォルダ)のままなので、この問題が起きない。 */
export function pythonModuleArgs(pythonBin, module, args = []) {
  return [pythonBin, ["-u", "-m", module, ...args]];
}

/** コマンドを実行し、stdout+stderr を行コールバックへ流す。resolve は exit code。
 * detached: true でプロセスグループを新設する(Stop時に、spawnしたプロセスがさらに起動する
 * 子プロセス(ffmpeg等)まで確実に止めるため。2026-07-18以前はconda run経由で起動しており、
 * conda run自身がSIGTERMを子へ転送せずpid一つだけkillしても実処理が動き続ける事故があった。
 * venv直呼びに変えた現在も、子孫プロセスを確実に道連れにするため対策自体は残している)。 */
export function streamCommand(cmd, args, { cwd = BASE_DIR, onLine, signal, detached = false } = {}) {
  return new Promise((resolve, reject) => {
    // Windowsではdetached:trueを指定すると、windowsHideの有無に関わらず子プロセスが
    // 強制的に別コンソールウィンドウで起動する(Node.jsの既知の制約)。detachedはPOSIXの
    // プロセスグループkill(process.kill(-pid, ...))のために必要だが、Windows側は既にjobs.js
    // の Stop 処理を taskkill /T(PIDの親子関係でプロセスツリーを辿る、プロセスグループ不要)に
    // 切り替え済みのため、Windowsではdetachedそのものが不要かつ実害(別ウィンドウ)しかない。
    const win = process.platform === "win32";
    const proc = spawn(cmd, args, { cwd, env: process.env, detached: win ? false : detached, windowsHide: true });
    if (signal) signal.proc = proc;
    let buf = "";
    const feed = (chunk) => {
      buf += chunk.toString("utf-8");
      let i;
      // \n だけでなく \r(tqdm等の進捗バーが同じ行を上書きする方式)も区切りとして扱う。
      // \r だけだと\nが来るまでバッファに溜まり続け、ダウンロード進捗等がリアルタイムに
      // 見えなくなるため。
      while ((i = buf.search(/[\r\n]/)) >= 0) {
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
    const proc = spawn(cmd, args, { cwd, env: process.env, windowsHide: true });
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
 * (プロセスの標準的なエラー文ではなく、stdout のJSONから本当の原因を取り出す)。 */
export async function runBridge(args, { stdin } = {}) {
  const [cmd, fullArgs] = pythonArgs(MAIN_PYTHON, "bridge.py", args);
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
