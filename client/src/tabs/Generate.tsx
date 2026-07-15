import { useEffect, useRef, useState } from "react";
import { api, useJob, type Engine, type PromptFile } from "../api";
import { FinalVideo, KeyframeGallery, LogPanel, SegRadio, SegVideoGrid, type KfDisplay, type SegDisplay } from "../common";
import { useSync } from "../sync";
import { Icon } from "../Icon";

export default function Generate() {
  const { sync, update, setBusy } = useSync();
  const [files, setFiles] = useState<PromptFile[]>([]);
  const [promptPath, setPromptPath] = useState("");
  const [orientation, setOrientation] = useState<"--h" | "--v">("--h");
  const [engine, setEngine] = useState<Engine>("i2v");
  const [jobId, setJobId] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const { log, state, status } = useJob(jobId);
  const running = status === "running";

  useEffect(() => {
    api<PromptFile[]>("/api/prompt-files").then((list) => {
      setFiles(list);
      setPromptPath((p) => p || (list[0]?.path ?? ""));
    });
  }, [sync.promptsBump]);

  // Write Promptで保存したファイルを自動選択(ユーザー要望 2026-07-15)
  useEffect(() => {
    if (sync.promptPath) setPromptPath(sync.promptPath);
  }, [sync.promptPath]);

  useEffect(() => setBusy("generate", running), [running, setBusy]);

  // 生成完了時、他タブ(Retry/Edit/CASS/Upscale)の対象を自動同期(finalが出た時のみ——
  // run_idだけ確定した段階で同期するとファイルの無い状態を読むレースになる、Gradio版の教訓)
  const syncedRef = useRef(false);
  useEffect(() => {
    if (status === "done" && !syncedRef.current && state.runId && state.final) {
      syncedRef.current = true;
      update({ engine, runId: state.runId, video: state.final, videosBump: sync.videosBump + 1 });
    }
    if (running) syncedRef.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, state.runId, state.final, running]);

  const start = async () => {
    if (!promptPath) {
      setMessage("(select a prompt file first)");
      return;
    }
    setMessage("");
    try {
      const r = await api<{ jobId: string }>("/api/generate", { json: { promptPath, orientation, engine } });
      setJobId(r.jobId); // useJobがログ・表示を全リセットして新runの購読を開始する
    } catch (e) {
      setMessage(`Error: ${(e as Error).message}`);
    }
  };

  const stop = async () => {
    const r = await api<{ message: string }>("/api/stop", { json: {} });
    setMessage(r.message);
  };

  // 生成中: prompts.txt由来の期待セグメント一覧(state.expected)を土台に、
  // まだファイルが無いセグメントは「生成中…」プレースホルダで枠を出す
  let segments: SegDisplay[] = state.segments ?? [];
  let keyframes: KfDisplay[] = state.keyframes ?? [];
  if (running && state.expected?.length) {
    const liveSegs = state.segments ?? [];
    segments = state.expected.map((b) => liveSegs.find((s) => s.num === b.num) ?? { ...b, path: null });
    if (engine === "i2v") {
      const liveKfs = state.keyframes ?? [];
      keyframes = state.expected.map(
        (b) => liveKfs.find((k) => k.seg === b.num)
          ?? { seg: b.num, caption: `seg${String(b.num).padStart(2, "0")}`, path: null },
      );
    }
  }

  return (
    <div className="tab-page">
      <div className="panel">
        <h2>② Generate</h2>
        <div className="row">
          <select className="grow" value={promptPath} onChange={(e) => setPromptPath(e.target.value)}>
            <option value="">— select prompt file —</option>
            {files.map((f) => <option key={f.path} value={f.path}>{f.name}</option>)}
          </select>
          <button className="icon" title="Refresh list" onClick={() => update({ promptsBump: sync.promptsBump + 1 })}><Icon name="refresh" size={14} /></button>
        </div>
        <div className="row">
          <SegRadio
            options={["--h", "--v"] as const}
            labels={{ "--h": "Horizontal (--h)", "--v": "Vertical (--v)" }}
            value={orientation}
            onChange={setOrientation}
          />
          <SegRadio options={["i2v", "t2v"] as const} value={engine} onChange={setEngine} />
          <button className="primary" onClick={start} disabled={running}>
            {running && <span className="spinner" />}Start generation
          </button>
          <button className="danger" onClick={stop} disabled={!running}>Stop</button>
          {status === "done" && <span className="status ok">✅ finished</span>}
          {status === "error" && <span className="status error">❌ failed — check the log</span>}
        </div>
        {message && <div className="status">{message}</div>}
        <LogPanel log={log} running={running} />
        <KeyframeGallery keyframes={keyframes} />
        <SegVideoGrid segments={segments} />
        <FinalVideo path={state.final ?? null} mtime={state.finalMtime ?? null} />
      </div>
    </div>
  );
}
