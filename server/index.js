import fs from "node:fs";
import path from "node:path";
import express from "express";
import multer from "multer";

import { BASE_DIR, UPLOADS_DIR, PORT, PREFIXES, readEnv } from "./config.js";
import {
  listRuns, runSnapshot, listGeneratedVideos, listBgmFiles,
  listLibraryRuns, libraryRunDetail, presetToSinceMs, deleteLibraryRun,
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
import { ffprobeDuration, runBridge } from "./proc.js";

const app = express();
app.use(express.json({ limit: "5mb" }));

const upload = multer({ dest: UPLOADS_DIR });

// asyncハンドラの例外を一律 500 JSON に落とす
const wrap = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

// ---------- Write Prompt ----------
app.get("/api/prompt-files", (req, res) => res.json(listPromptFiles()));
app.get("/api/prompt-files/:name", wrap(async (req, res) => {
  const text = readPromptFile(req.params.name);
  res.json({ name: req.params.name, text, validation: await validatePrompt(text) });
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

// i2vの動画生成エンジン(.envのI2V_VIDEO_ENGINE)。Retryタブの--norefine表示切替に使う
// (I2V_VIDEO_ENGINE=refine時のみ意味を持つオプションのため、それ以外では隠す)
app.get("/api/i2v-engine", (req, res) => {
  res.json({ engine: readEnv().I2V_VIDEO_ENGINE || "default" });
});

// ACE-Step-1.5(BGM生成)が.envに設定済みか。CASSタブの「Generate」選択肢表示切替に使う
// (未設定でも音源分離・BGM合成本体はFileモードで利用可能なため、その選択肢だけ隠す)
app.get("/api/acestep-config", (req, res) => {
  const env = readEnv();
  res.json({ configured: Boolean(env.ACESTEP_URL?.trim() && env.ACESTEP_MODEL?.trim()) });
});

// ---------- Library(閲覧専用: 全run横断ビューア) ----------
app.get("/api/library/runs", (req, res) => {
  const { engine, since, q } = req.query;
  if (engine && !PREFIXES[engine]) return res.status(400).json({ error: `unknown engine: ${engine}` });
  res.json(listLibraryRuns({ engine, sinceMs: presetToSinceMs(since), q: q ? String(q) : "" }));
});
app.get("/api/library/runs/:engine/:runId", (req, res) => {
  if (!PREFIXES[req.params.engine]) return res.status(400).json({ error: `unknown engine: ${req.params.engine}` });
  res.json(libraryRunDetail(req.params.engine, req.params.runId));
});
app.delete("/api/library/runs/:engine/:runId", (req, res) => {
  if (!PREFIXES[req.params.engine]) return res.status(400).json({ error: `unknown engine: ${req.params.engine}` });
  res.json(deleteLibraryRun(req.params.engine, req.params.runId));
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

  // ちょうど1セグメント選択+編集内容ありなら、リトライ前に prompts.txt を書き戻す
  // (Pass0〜4を回さず保存済みプロンプトを手で直して救う機能、Gradio版と同じ)。
  // 書き戻し失敗は警告扱いでリトライ自体は続行する。
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
  res.json({ out: await editCommit(engine, runId, segments) });
}));

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
  let parsed;
  try {
    parsed = await runBridge(["parse_prompt", source]);
  } catch (e) {
    return res.json({ prompt: "", message: `⚠️ 元プロンプト(${path.basename(source)})の解析に失敗しました: ${e.message}` });
  }
  const prompt = await draftBgmPrompt(parsed.global_desc);
  res.json({ prompt, message: `✅ ${path.basename(source)} の全体設定から下書きしました(編集してからGenerateしてください)` });
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
  // multer はランダム名で保存するので、拡張子付きの元名に寄せてリネームする
  const safe = `${Date.now()}_${req.file.originalname.replace(/[^\w.\-()\[\] ]/g, "_")}`;
  const dest = path.join(UPLOADS_DIR, safe);
  fs.renameSync(req.file.path, dest);
  res.json({ path: dest, name: req.file.originalname });
});
app.get("/media", mediaHandler);

// ---------- static frontend ----------
const DIST = path.join(BASE_DIR, "client", "dist");
app.use(express.static(DIST));
app.get(/^\/(?!api\/|media).*/, (req, res) => {
  const index = path.join(DIST, "index.html");
  if (fs.existsSync(index)) return res.sendFile(index);
  res.status(503).send("frontend not built yet — run: npm run build");
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

// 長時間稼働前提のため、単発の例外(切断済みSSEへの書き込み等)でプロセスを落とさない
process.on("uncaughtException", (e) => console.error("[uncaught]", e));
process.on("unhandledRejection", (e) => console.error("[unhandled]", e));

app.listen(PORT, "0.0.0.0", () => {
  console.log(`LTX-timeline harness listening on http://0.0.0.0:${PORT}`);
});
