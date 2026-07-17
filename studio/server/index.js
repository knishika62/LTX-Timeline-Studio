// studio専用サーバー(port 7865)。studio/*.js配下にロジックを自己完結させている
// (2026-07-17: server/・client/(旧ハーネス)をgitignore対象にしたため、GitHub公開clone環境に
// server/が存在せずimportが壊れる問題への対応。server/*.jsをここへ複製、以後はstudio独自に
// 保守する。config.jsのBASE_DIR算出だけ階層差分を吸収済み、他ファイルは無変更で複製)。
import fs from "node:fs";
import path from "node:path";
import express from "express";
import multer from "multer";

import { BASE_DIR, UPLOADS_DIR, GENERATED_DIR, PROMPT_DIR, PREFIXES, readEnv } from "./config.js";
import {
  listRuns, runSnapshot, listGeneratedVideos, listBgmFiles,
  listLibraryRuns, libraryRunDetail, presetToSinceMs, deleteLibraryRun,
  scanSegmentVideos,
} from "./scan.js";
import {
  listPromptFiles, readPromptFile, savePromptFile, validatePrompt,
  readSegmentPrompt, writeSegmentPrompt, resolvePromptSourceForVideo,
} from "./prompts.js";
import { draftTimelinePrompt, summarizePromptJa, draftBgmPrompt } from "./llm.js";
import { getJob, sseHandler, startGeneration, currentGeneration } from "./jobs.js";
import { trimSegment, editPreview, editCommit } from "./edit.js";
import { startCass, startBgmGenerate, startUpscale } from "./cass.js";
import { mediaHandler } from "./media.js";
import { ffprobeDuration, runBridge, runCommand } from "./proc.js";

const STUDIO_PORT = Number(process.env.STUDIO_PORT) || 7865;

const app = express();
app.use(express.json({ limit: "5mb" }));

const upload = multer({ dest: UPLOADS_DIR });

const wrap = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

// ---------- Write Prompt ----------
app.get("/api/prompt-files", (req, res) => res.json(listPromptFiles()));
app.get("/api/prompt-files/:name", wrap(async (req, res) => {
  const text = readPromptFile(req.params.name);
  res.json({ name: req.params.name, path: path.join(PROMPT_DIR, req.params.name), text, validation: await validatePrompt(text) });
}));
app.post("/api/prompt-files", wrap(async (req, res) => {
  const { name, text } = req.body;
  if (!text?.trim()) return res.status(400).json({ error: "nothing to save" });
  const saved = savePromptFile(name || `harness_${tsName()}`, text);
  res.json({ ...saved, validation: await validatePrompt(text) });
}));
app.post("/api/draft", wrap(async (req, res) => {
  const { keywords, durationHint = 20, minBeat = 3 } = req.body;
  if (!keywords?.trim()) return res.status(400).json({ error: "enter some keywords first" });
  const prompt = await draftTimelinePrompt(keywords, Math.round(durationHint), Math.round(minBeat));
  const [summary, validation] = await Promise.all([summarizePromptJa(prompt), validatePrompt(prompt)]);
  res.json({ prompt, summary, validation });
}));
app.post("/api/summarize", wrap(async (req, res) => {
  if (!req.body.text?.trim()) return res.status(400).json({ error: "prompt is empty" });
  res.json({ summary: await summarizePromptJa(req.body.text) });
}));
app.post("/api/validate", wrap(async (req, res) => {
  res.json(await validatePrompt(req.body.text || ""));
}));

// ---------- Runs / scan ----------
app.get("/api/runs/:engine", (req, res) => res.json(listRuns(req.params.engine)));
app.get("/api/runs/:engine/:runId", (req, res) => res.json(runSnapshot(req.params.engine, req.params.runId)));
app.get("/api/runs/:engine/:runId/seg/:num/prompt", wrap(async (req, res) => {
  const seg = await readSegmentPrompt(req.params.engine, req.params.runId, parseInt(req.params.num, 10));
  if (!seg) return res.status(404).json({ error: "segment not found" });
  res.json(seg);
}));
app.get("/api/videos", (req, res) => res.json(listGeneratedVideos()));
app.get("/api/bgm-files", (req, res) => res.json(listBgmFiles()));

app.get("/api/i2v-engine", (req, res) => {
  res.json({ engine: readEnv().I2V_VIDEO_ENGINE || "default" });
});

app.get("/api/acestep-config", (req, res) => {
  const env = readEnv();
  res.json({ configured: Boolean(env.ACESTEP_URL?.trim() && env.ACESTEP_MODEL?.trim()) });
});

// ---------- Library ----------
// t2vはi2vと違いkeyframe画像が無いため一覧サムネイルが常にnull(server/scan.jsの仕様通り)。
// studio専用に、seg01動画からffmpegで1フレーム抜き出しGENERATED_DIRへキャッシュする
// (ファイル名は既存の _kf.png / *_old*.mp4 系スキャン正規表現と一切被らない接尾辞にして、
// 本体harness側のGenerate/Retry/Editのスキャン結果に影響が出ないようにしている)。
async function ensureT2vThumbnail(runId) {
  const thumbPath = path.join(GENERATED_DIR, `t2v6_${runId}_seg01_thumb.png`);
  if (fs.existsSync(thumbPath)) return thumbPath;
  const segs = scanSegmentVideos("t2v6", runId);
  const first = segs.find((s) => s.num === 1) ?? segs[0];
  if (!first) return null;
  try {
    await runCommand("ffmpeg", ["-y", "-loglevel", "error", "-i", first.path, "-frames:v", "1", "-q:v", "3", thumbPath]);
    return fs.existsSync(thumbPath) ? thumbPath : null;
  } catch {
    return null;
  }
}

app.get("/api/library/runs", wrap(async (req, res) => {
  const { engine, since, q } = req.query;
  if (engine && !PREFIXES[engine]) return res.status(400).json({ error: `unknown engine: ${engine}` });
  const result = listLibraryRuns({ engine, sinceMs: presetToSinceMs(since), q: q ? String(q) : "" });
  await Promise.all(result.runs.map(async (r) => {
    if (r.engine === "t2v" && !r.thumbnail) r.thumbnail = await ensureT2vThumbnail(r.runId);
  }));
  res.json(result);
}));
app.get("/api/library/runs/:engine/:runId", (req, res) => {
  if (!PREFIXES[req.params.engine]) return res.status(400).json({ error: `unknown engine: ${req.params.engine}` });
  res.json(libraryRunDetail(req.params.engine, req.params.runId));
});
app.delete("/api/library/runs/:engine/:runId", (req, res) => {
  if (!PREFIXES[req.params.engine]) return res.status(400).json({ error: `unknown engine: ${req.params.engine}` });
  const result = deleteLibraryRun(req.params.engine, req.params.runId);
  deleteEditStage(req.params.engine, req.params.runId); // studio専用sidecarもrunと一緒に消す
  // t2vのstudio専用サムネイル(ensureT2vThumbnailが生成)も、実harness側のdeleteLibraryRunの
  // 削除対象(既存スキャン正規表現ベース)には含まれないので道連れで消す
  // (2026-07-17ユーザー指摘: run削除時に関連サムネイルが残っていた)。
  if (req.params.engine === "t2v") {
    const thumbPath = path.join(GENERATED_DIR, `t2v6_${req.params.runId}_seg01_thumb.png`);
    if (fs.existsSync(thumbPath)) fs.unlinkSync(thumbPath);
  }
  res.json(result);
});
app.get("/api/duration", wrap(async (req, res) => {
  res.json({ duration: await ffprobeDuration(String(req.query.p)) });
}));

// ---------- Generate / Retry ----------
app.post("/api/generate", wrap(async (req, res) => {
  const { promptPath, orientation = "--h", engine = "i2v", direct } = req.body;
  if (!promptPath) return res.status(400).json({ error: "select a prompt file first" });
  if (!PREFIXES[engine]) return res.status(400).json({ error: `unknown engine: ${engine}` });
  const extraArgs = [];
  if (direct != null) {
    const seconds = Number(direct);
    if (!Number.isFinite(seconds) || seconds <= 0) return res.status(400).json({ error: "direct must be a positive number of seconds" });
    extraArgs.push("--direct", String(seconds));
  }
  const job = startGeneration(engine, { promptPath, orientation, extraArgs });
  res.json({ jobId: job.id });
}));

app.post("/api/retry", wrap(async (req, res) => {
  const { engine = "i2v", runId, segs = [], keep = false, norefine = false, editPrompt = "", editKfPrompt = "" } = req.body;
  if (!runId || !segs.length) return res.status(400).json({ error: "select a run and at least one segment" });

  let editWarning = null;
  if (segs.length === 1 && editPrompt.trim()) {
    const ok = writeSegmentPrompt(engine, runId, segs[0], editPrompt, engine === "i2v" ? editKfPrompt : null);
    if (!ok) editWarning = `プロンプトの書き戻しに失敗しました(seg${segs[0]})、編集前の内容でリトライを続行します`;
  }

  const extra = ["--retry", runId, "--seg", segs.join(",")];
  if (engine === "i2v" && keep) extra.push("--keep");
  if (engine === "i2v" && norefine) extra.push("--norefine");
  const job = startGeneration(engine, { extraArgs: extra, knownRunId: runId });
  if (editWarning) job.appendLog(`⚠️ ${editWarning}\n`);
  res.json({ jobId: job.id, warning: editWarning });
}));

app.post("/api/stop", (req, res) => {
  const job = currentGeneration();
  if (job && job.stop()) return res.json({ stopped: true, message: "Stop signal sent" });
  res.json({ stopped: false, message: "No process is running" });
});

// ---------- Jobs (SSE) ----------
app.get("/api/jobs/:id", (req, res) => {
  const job = getJob(req.params.id);
  if (!job) return res.status(404).json({ error: "no such job" });
  res.json(job.toJSON());
});
app.get("/api/jobs/:id/events", sseHandler);
app.post("/api/jobs/:id/stop", (req, res) => {
  const job = getJob(req.params.id);
  if (!job) return res.status(404).json({ error: "no such job" });
  res.json({ stopped: job.stop() });
});

// ---------- Edit ----------
app.post("/api/edit/trim-preview", wrap(async (req, res) => {
  const { path: p, trimStart = 0, trimEnd = 0 } = req.body;
  res.json({ out: await trimSegment(p, trimStart, trimEnd) });
}));
app.post("/api/edit/preview", wrap(async (req, res) => {
  res.json({ out: await editPreview(req.body.segments || []) });
}));
app.post("/api/edit/commit", wrap(async (req, res) => {
  const { engine, runId, segments } = req.body;
  if (!runId || !segments?.length) return res.status(400).json({ error: "load a run first" });
  const out = await editCommit(engine, runId, segments);
  // sidecarはcommitしても削除しない(2026-07-17ユーザー訂正: 並び順・remove・trimは
  // 「commitするまでの一時staging」ではなく、このrunの現在の編集結果を表す永続的な記録。
  // runを明示的に削除するまで残す)。
  res.json({ out });
}));

// ---------- Edit staging(studio専用、実harnessは無関係) ----------
// remove/reorder/trimは翌日以降も復元できるよう、run単位の別ファイルへ保存する
// (generated/{prefix}_{runId}_studio_edit.json、既存スキャン正規表現とは命名で衝突しない)。
function editStagePath(engine, runId) {
  const prefix = PREFIXES[engine];
  return path.join(GENERATED_DIR, `${prefix}_${runId}_studio_edit.json`);
}
function deleteEditStage(engine, runId) {
  if (!PREFIXES[engine]) return;
  const p = editStagePath(engine, runId);
  if (fs.existsSync(p)) fs.unlinkSync(p);
}
app.get("/api/edit/stage/:engine/:runId", (req, res) => {
  if (!PREFIXES[req.params.engine]) return res.status(400).json({ error: `unknown engine: ${req.params.engine}` });
  const p = editStagePath(req.params.engine, req.params.runId);
  if (!fs.existsSync(p)) return res.json(null);
  try {
    res.json(JSON.parse(fs.readFileSync(p, "utf-8")));
  } catch {
    res.json(null);
  }
});
app.post("/api/edit/stage", (req, res) => {
  const { engine, runId, order, removed, trims } = req.body;
  if (!PREFIXES[engine] || !runId) return res.status(400).json({ error: "engine/runId required" });
  fs.writeFileSync(editStagePath(engine, runId), JSON.stringify({ order, removed, trims }, null, 2), "utf-8");
  res.json({ ok: true });
});

// ---------- CASS / BGM / Upscale ----------
app.post("/api/cass", wrap(async (req, res) => {
  const { videoPath, bgmPath = null, volume = 0.6 } = req.body;
  if (!videoPath) return res.status(400).json({ error: "select or upload a video first" });
  res.json({ jobId: startCass({ videoPath, bgmPath, volume }).id });
}));
app.post("/api/bgm/draft", wrap(async (req, res) => {
  const source = resolvePromptSourceForVideo(req.body.videoPath);
  if (!source) {
    return res.json({ prompt: "", message: "⚠️ この動画は元プロンプトファイルに紐付けられません(アップロード動画やCASSリミックス後の動画等)。説明文を手入力してください" });
  }
  // Timeline: ヘッダー/タイムスタンプの無い(V6の自動セグメント化に任せる)プロンプトはparse_promptが
  // 必ず失敗する(致命的にDraftが作れなくなっていた、2026-07-17)。その場合は元ファイルの生テキストを
  // そのままglobal descとして使う(Pythonの共有パーサー自体には手を入れない)。
  let globalDesc;
  let usedFallback = false;
  try {
    const parsed = await runBridge(["parse_prompt", source]);
    globalDesc = parsed.global_desc;
  } catch {
    try {
      globalDesc = fs.readFileSync(source, "utf-8");
      usedFallback = true;
    } catch (e2) {
      return res.json({ prompt: "", message: `⚠️ 元プロンプト(${path.basename(source)})の読み込みに失敗しました: ${e2.message}` });
    }
  }
  const prompt = await draftBgmPrompt(globalDesc);
  const message = usedFallback
    ? `✅ ${path.basename(source)}(Timelineヘッダー無し、全文をそのまま使用)から下書きしました(編集してからGenerateしてください)`
    : `✅ ${path.basename(source)} の全体設定から下書きしました(編集してからGenerateしてください)`;
  res.json({ prompt, message });
}));
app.post("/api/bgm/generate", wrap(async (req, res) => {
  const { prompt, duration = 60 } = req.body;
  if (!prompt?.trim()) return res.status(400).json({ error: "enter a BGM description first" });
  res.json({ jobId: startBgmGenerate({ prompt: prompt.trim(), duration }).id });
}));
app.post("/api/upscale", wrap(async (req, res) => {
  if (!req.body.videoPath) return res.status(400).json({ error: "select or upload a video first" });
  res.json({ jobId: startUpscale({ videoPath: req.body.videoPath }).id });
}));

// ---------- Upload / media ----------
app.post("/api/upload", upload.single("file"), (req, res) => {
  if (!req.file) return res.status(400).json({ error: "no file" });
  const safe = `${Date.now()}_${req.file.originalname.replace(/[^\w.\-()\[\] ]/g, "_")}`;
  const dest = path.join(UPLOADS_DIR, safe);
  fs.renameSync(req.file.path, dest);
  res.json({ path: dest, name: req.file.originalname });
});
app.get("/media", mediaHandler);

// ---------- static frontend (studio/client/dist) ----------
const DIST = path.join(BASE_DIR, "studio", "client", "dist");
app.use(express.static(DIST));
app.get(/^\/(?!api\/|media).*/, (req, res) => {
  const index = path.join(DIST, "index.html");
  if (fs.existsSync(index)) return res.sendFile(index);
  res.status(503).send("studio frontend not built yet — run: npm run studio:build");
});

// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error(err);
  res.status(500).json({ error: String(err.message || err) });
});

function tsName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

process.on("uncaughtException", (e) => console.error("[uncaught]", e));
process.on("unhandledRejection", (e) => console.error("[unhandled]", e));

app.listen(STUDIO_PORT, "0.0.0.0", () => {
  console.log(`LTX-timeline studio (backend-connected) listening on http://0.0.0.0:${STUDIO_PORT}`);
});
