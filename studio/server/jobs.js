import { spawn } from "node:child_process";
import { PREFIXES, SCRIPTS, MAIN_PYTHON } from "./config.js";
import { pythonModuleArgs, streamCommand } from "./proc.js";
import { findRunId, runSnapshot } from "./scan.js";

// ============================================================
// ジョブ管理 + SSE配信。
// ログ・状態はサーバー側に保持し、SSE接続時は全量リプレイしてから差分配信する。
// ブラウザ側の接続が切れても(タブのバックグラウンド化等)、再接続すれば
// 完全な状態に復元できる——Gradio版で未解決だった「Per-segment videosの更新が
// 止まって見える」問題(ストリーミング接続断疑い)への構造的な対策。
// ============================================================

const jobs = new Map();
let nextId = 1;

class Job {
  constructor(type) {
    this.id = String(nextId++);
    this.type = type;
    this.status = "running"; // running | done | error | stopped
    this.stopRequested = false;
    this.error = null;
    this.log = [];
    this.state = {};
    this.clients = new Map(); // res → heartbeat interval
    this.signal = {}; // streamCommand が .proc を挿す
    this.startedAt = Date.now();
    jobs.set(this.id, this);
  }

  broadcast(event) {
    const data = `data: ${JSON.stringify(event)}\n\n`;
    for (const res of this.clients.keys()) {
      try {
        res.write(data);
      } catch {
        /* 切断済みクライアントへの書き込みでプロセスを落とさない */
      }
    }
  }

  appendLog(line) {
    this.log.push(line);
    this.broadcast({ type: "log", line });
  }

  setState(patch) {
    Object.assign(this.state, patch);
    this.broadcast({ type: "state", state: this.state });
  }

  finish(status, error = null) {
    if (this.status !== "running") return;
    this.status = status;
    this.error = error;
    this.broadcast({ type: "end", status, error });
    // ハートビートを必ず止めてから閉じる(終了済みresへのsetInterval書き込みは
    // stream の error イベント経由でプロセスごと落とし得るため)
    for (const [res, hb] of this.clients) {
      clearInterval(hb);
      try {
        res.end();
      } catch {
        /* already closed */
      }
    }
    this.clients.clear();
  }

  stop() {
    const proc = this.signal.proc;
    if (this.status === "running" && proc && proc.exitCode === null) {
      this.stopRequested = true;
      // python(venvバイナリを直接spawn)がさらにffmpeg等の子プロセスを起動することがあるため、
      // proc.pidだけkillしてもその子孫が生き残ることがある。detachedで作ったプロセスグループ
      // ごと殺す(pid同様に負のpidを渡すとグループ全体に届く)。
      // (2026-07-18以前はconda run経由で起動しており、conda run自身がSIGTERMを子プロセスへ
      // 転送しないという別の理由でも同じ対策が必要だった。venv直呼びに変えた現在もこの対策自体は
      // 引き続き有効なため残している)
      // Windowsには「負のpidでプロセスグループへ届く」というPOSIXの概念自体が無く、
      // process.kill(-pid, ...)は失敗する(fallbackのproc.kill()は対象プロセス単体しか
      // 殺せずffmpeg等の子孫が生き残る)。taskkillの/Tでプロセスツリーごと強制終了する。
      if (process.platform === "win32") {
        spawn("taskkill", ["/pid", String(proc.pid), "/T", "/F"], { windowsHide: true });
      } else {
        try {
          process.kill(-proc.pid, "SIGTERM");
        } catch {
          proc.kill("SIGTERM"); // グループkillが失敗した場合の保険
        }
        // 一定時間待って死んでいなければ、同じくグループへSIGKILLでエスカレーション
        setTimeout(() => {
          if (proc.exitCode === null) {
            try {
              process.kill(-proc.pid, "SIGKILL");
            } catch {
              /* すでに終了済み */
            }
          }
        }, 5000);
      }
      return true;
    }
    return false;
  }

  toJSON() {
    return { id: this.id, type: this.type, status: this.status, error: this.error, state: this.state };
  }
}

export function getJob(id) {
  return jobs.get(id);
}

/** SSEエンドポイント: 接続時に全ログ+状態をリプレイし、以後は差分をpush */
export function sseHandler(req, res) {
  const job = jobs.get(req.params.id);
  if (!job) return res.status(404).json({ error: "no such job" });
  res.set({
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  res.flushHeaders();
  res.write(`data: ${JSON.stringify({ type: "replay", log: job.log.join(""), state: job.state, status: job.status, error: job.error })}\n\n`);
  if (job.status !== "running") return res.end();

  const heartbeat = setInterval(() => {
    try {
      res.write(": ping\n\n");
    } catch {
      /* ignore */
    }
  }, 15000);
  job.clients.set(res, heartbeat);
  req.on("close", () => {
    clearInterval(heartbeat);
    job.clients.delete(res);
  });
}

/** 汎用の非同期ジョブ(CASS・BGM・Upscale等)。runner(job) を実行し、例外は error 終了に落とす */
export function createJob(type, runner) {
  const job = new Job(type);
  (async () => {
    try {
      await runner(job);
      job.finish("done");
    } catch (e) {
      job.appendLog(`\nError: ${e.message}\n`);
      job.finish("error", String(e.message));
    }
  })();
  return job;
}

// ============================================================
// 生成ジョブ(t2v/i2v CLI subprocess)。Gradio版同様、同時実行は1本のみ。
// ============================================================

let currentGenJob = null;

export function currentGeneration() {
  return currentGenJob && currentGenJob.status === "running" ? currentGenJob : null;
}

/** 既存CLIをsubprocess起動し、ログ+1秒間隔のファイルスキャンをジョブへ流す。
 * knownRunId 指定時(リトライ)は run_id 検出をスキップ(Gradio版 run_generation と同じ)。 */
export function startGeneration(engine, { promptPath = "", orientation = "--h", extraArgs = [], knownRunId = null } = {}) {
  if (currentGeneration()) throw new Error("a generation is already running");
  const script = SCRIPTS[engine];
  const prefix = PREFIXES[engine];

  const cliArgs = promptPath ? [orientation, "--f", promptPath, ...extraArgs] : extraArgs;
  const [cmd, args] = pythonModuleArgs(MAIN_PYTHON, script.replace(/\.py$/, ""), cliArgs);

  const job = new Job("generation");
  job.engine = engine;
  currentGenJob = job;

  const launchTs = Date.now();
  let runId = knownRunId;
  job.appendLog(`$ python ${script} ${cliArgs.join(" ")}\n\n`);
  job.setState({ engine, ...runSnapshot(engine, runId) });

  const poll = setInterval(() => {
    if (runId == null) runId = findRunId(prefix, launchTs);
    if (runId) job.setState({ engine, ...runSnapshot(engine, runId) });
  }, 1000);

  (async () => {
    try {
      const code = await streamCommand(cmd, args, {
        onLine: (line) => job.appendLog(line),
        signal: job.signal,
        detached: true,
      });
      clearInterval(poll);
      if (runId == null) runId = findRunId(prefix, launchTs);
      if (runId) job.setState({ engine, ...runSnapshot(engine, runId) });
      if (code === 0) job.finish("done");
      else if (job.stopRequested) job.finish("stopped");
      else job.finish("error", `CLI exited with code ${code}`);
    } catch (e) {
      clearInterval(poll);
      job.finish("error", String(e.message));
    }
  })();

  return job;
}
