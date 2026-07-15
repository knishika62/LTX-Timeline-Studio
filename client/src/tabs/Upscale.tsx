import { useEffect, useState } from "react";
import { api, media, useJob } from "../api";
import { LogPanel } from "../common";
import { useSync } from "../sync";
import { VideoPicker } from "./Cass";

export default function Upscale() {
  const { sync, update, setBusy } = useSync();
  const [videoPath, setVideoPath] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const { log, state, status } = useJob(jobId);
  const running = status === "running";

  useEffect(() => setBusy("upscale", running), [running, setBusy]);

  // 生成/CASS完了時の自動同期(CASS後があればそのmp4を優先——Gradio版のユーザー指定挙動)
  useEffect(() => {
    if (sync.video) setVideoPath(sync.video);
  }, [sync.video]);

  const run = async () => {
    if (!videoPath) {
      setMessage("(select or upload a video first)");
      return;
    }
    setMessage("");
    try {
      const r = await api<{ jobId: string }>("/api/upscale", { json: { videoPath } });
      setJobId(r.jobId);
    } catch (e) {
      setMessage(`Error: ${(e as Error).message}`);
    }
  };

  useEffect(() => {
    if (status === "done" && state.result) update({ videosBump: sync.videosBump + 1 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  return (
    <div className="tab-page">
      <div className="panel">
        <h2>⑥ Upscale — RTX Video Super Resolution(1.5x ULTRA → FHD)</h2>
        <VideoPicker value={videoPath} onChange={setVideoPath} onRefresh={() => update({ videosBump: sync.videosBump + 1 })} />
        <div className="row">
          <button className="primary" onClick={run} disabled={running || !videoPath}>
            {running && <span className="spinner" />}Run
          </button>
          {status === "done" && <span className="status ok">✅ finished</span>}
          {status === "error" && <span className="status error">❌ failed — check the log</span>}
          {message && <span className="status">{message}</span>}
        </div>
        <LogPanel log={log} running={running} />
        {state.result && (
          <div>
            <h3>Upscaled result video</h3>
            <video className="final-video" src={media(state.result, jobId)} controls style={{ marginTop: 8 }} />
          </div>
        )}
      </div>
    </div>
  );
}
