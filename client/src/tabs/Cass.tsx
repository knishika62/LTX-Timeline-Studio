import { useEffect, useId, useState } from "react";
import { api, media, uploadFile, useJob, type VideoItem } from "../api";
import { AudioWithWaveform, LogPanel, NumberField, SegRadio } from "../common";
import { Icon } from "../Icon";
import { useSync } from "../sync";

/** 動画選択(一覧+アップロード+プレビュー)。CASS/Upscale共通。
 * 一覧更新(videosBump)のたびに直近のファイルをデフォルト選択にする
 * (アップロード選択中はそれを維持。「直近のをデフォルトへ」——ユーザー要望 2026-07-15)。 */
export function VideoPicker({ value, onChange, onRefresh }: {
  value: string;
  onChange: (path: string) => void;
  onRefresh: () => void;
}) {
  const { sync } = useSync();
  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [uploaded, setUploaded] = useState<{ path: string; name: string } | null>(null);
  const inputId = useId();

  useEffect(() => {
    api<VideoItem[]>("/api/videos").then((list) => {
      setVideos(list);
      if (uploaded && value === uploaded.path) return;
      onChange(list[0]?.path ?? "");
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sync.videosBump]);

  const upload = async (file: File | undefined) => {
    if (!file) return;
    const r = await uploadFile(file);
    setUploaded(r);
    onChange(r.path);
  };

  const mt = videos.find((v) => v.path === value)?.mt ?? sync.videosBump;

  return (
    <>
      <div className="row">
        <select className="grow" value={uploaded?.path === value ? "" : value} onChange={(e) => e.target.value && onChange(e.target.value)}>
          {uploaded && <option value="">⬆ {uploaded.name} (uploaded)</option>}
          {videos.map((v) => <option key={v.path} value={v.path}>{v.name}</option>)}
        </select>
        <button className="icon" title="Refresh list" onClick={onRefresh}><Icon name="refresh" size={14} /></button>
        <input type="file" accept=".mp4" style={{ display: "none" }} id={inputId} onChange={(e) => upload(e.target.files?.[0])} />
        <button onClick={() => document.getElementById(inputId)?.click()}><Icon name="upload" size={14} /> Upload…</button>
      </div>
      {value && (
        <video key={`${value}-${mt}`} className="final-video" style={{ maxHeight: 260 }}
          src={media(value, mt)} controls preload="metadata" />
      )}
    </>
  );
}

export default function Cass() {
  const { sync, update, setBusy } = useSync();
  const [videoPath, setVideoPath] = useState("");
  const [bgmMode, setBgmMode] = useState<"File" | "Generate">("File");
  const [bgmFiles, setBgmFiles] = useState<VideoItem[]>([]);
  const [bgmPath, setBgmPath] = useState("");
  const [bgmUploaded, setBgmUploaded] = useState<{ path: string; name: string } | null>(null);
  const [bgmPrompt, setBgmPrompt] = useState("");
  const [bgmDuration, setBgmDuration] = useState(60);
  const [bgmDraftStatus, setBgmDraftStatus] = useState("");
  const [bgmJobId, setBgmJobId] = useState<string | null>(null);
  const [chosenTake, setChosenTake] = useState<string | null>(null);
  const [volume, setVolume] = useState(0.6);
  const [jobId, setJobId] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [drafting, setDrafting] = useState(false);

  const bgmJob = useJob(bgmJobId);
  const { log, state, status } = useJob(jobId);
  const running = status === "running";
  const bgmRunning = bgmJob.status === "running";

  useEffect(() => setBusy("cass", running || bgmRunning || drafting), [running, bgmRunning, drafting, setBusy]);

  // 生成/リトライ/Edit commit完了時の自動同期
  useEffect(() => {
    if (sync.video) setVideoPath(sync.video);
  }, [sync.video]);

  const refreshBgm = () =>
    api<VideoItem[]>("/api/bgm-files").then((list) => {
      setBgmFiles(list);
      setBgmPath((p) => (p && list.some((x) => x.path === p) ? p : (list[0]?.path ?? "")));
    });
  useEffect(() => {
    refreshBgm();
  }, [sync.videosBump, bgmJob.status]);

  // Generateモードへの切替時、選択中動画の尺+2秒をdurationのデフォルトに
  // (process.shはBGMを動画尺にatrimするため動画尺以上あればよい、Gradio版と同じ)
  useEffect(() => {
    if (bgmMode !== "Generate" || !videoPath) return;
    api<{ duration: number }>(`/api/duration?p=${encodeURIComponent(videoPath)}`)
      .then((r) => setBgmDuration(Math.ceil(r.duration) + 2))
      .catch(() => {});
  }, [bgmMode, videoPath]);

  const draftBgm = async () => {
    setDrafting(true);
    try {
      const r = await api<{ prompt: string; message: string }>("/api/bgm/draft", { json: { videoPath } });
      if (r.prompt) setBgmPrompt(r.prompt);
      setBgmDraftStatus(r.message);
    } catch (e) {
      setBgmDraftStatus(`Error: ${(e as Error).message}`);
    } finally {
      setDrafting(false);
    }
  };

  const generateBgm = async () => {
    if (!bgmPrompt.trim()) return;
    setChosenTake(null);
    const r = await api<{ jobId: string }>("/api/bgm/generate", { json: { prompt: bgmPrompt, duration: bgmDuration } });
    setBgmJobId(r.jobId);
  };

  const uploadBgm = async (file: File | undefined) => {
    if (!file) return;
    const r = await uploadFile(file);
    setBgmUploaded(r);
  };

  const run = async () => {
    if (!videoPath) {
      setMessage("(select or upload a video first)");
      return;
    }
    // BGM解決順: Generateモード=選択テイク、Fileモード=アップロード優先→一覧選択(Gradio版と同じ)
    const bgm = bgmMode === "Generate" ? chosenTake : (bgmUploaded?.path || bgmPath || null);
    setMessage("");
    const r = await api<{ jobId: string }>("/api/cass", { json: { videoPath, bgmPath: bgm, volume } });
    setJobId(r.jobId);
  };

  // 完了時、Upscaleタブの対象動画をミックス済み出力へ自動同期
  useEffect(() => {
    if (status === "done" && state.result) update({ video: state.result, videosBump: sync.videosBump + 1 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const takes = bgmJob.state.takes ?? [];

  return (
    <div className="tab-page">
      <div className="panel">
        <h2>⑤ CASS — 音声分離+BGMミックス(BandIt v2)</h2>
        <VideoPicker value={videoPath} onChange={setVideoPath} onRefresh={() => update({ videosBump: sync.videosBump + 1 })} />

        <div className="row">
          <h3>BGM source</h3>
          <SegRadio
            options={["File", "Generate"] as const}
            labels={{ File: "File", Generate: "Generate (ACE-Step 1.5)" }}
            value={bgmMode}
            onChange={setBgmMode}
          />
        </div>

        {bgmMode === "File" && (
          <>
            <div className="row">
              <select className="grow" value={bgmUploaded ? "" : bgmPath} onChange={(e) => { setBgmUploaded(null); setBgmPath(e.target.value); }}>
                <option value="">— no BGM (speech+sfx only) —</option>
                {bgmUploaded && <option value="">⬆ {bgmUploaded.name} (uploaded)</option>}
                {bgmFiles.map((b) => <option key={b.path} value={b.path}>{b.name}</option>)}
              </select>
              <button className="icon" onClick={refreshBgm}><Icon name="refresh" size={14} /></button>
              <input type="file" accept=".mp3,.wav,.m4a" style={{ display: "none" }} id="bgm-upload" onChange={(e) => uploadBgm(e.target.files?.[0])} />
              <button onClick={() => document.getElementById("bgm-upload")?.click()}><Icon name="upload" size={14} /> Upload…</button>
            </div>
            {(bgmUploaded?.path || bgmPath) && (
              <AudioWithWaveform key={bgmUploaded?.path || bgmPath} src={media(bgmUploaded?.path || bgmPath)} />
            )}
          </>
        )}

        {bgmMode === "Generate" && (
          <div className="panel" style={{ background: "var(--bg-card)" }}>
            <label className="field">
              BGM description (English)
              <textarea rows={3} value={bgmPrompt} onChange={(e) => setBgmPrompt(e.target.value)}
                placeholder="warm lo-fi piano and acoustic guitar, calm cozy mood" />
            </label>
            <div className="row">
              <button onClick={draftBgm} disabled={drafting || !videoPath} style={{ alignSelf: "flex-end" }}>
                {drafting && <span className="spinner" />}<Icon name="pencil" size={14} /> Draft from prompt file
              </button>
              <label className="field">
                Duration (s)
                <NumberField value={bgmDuration} min={5} onChange={setBgmDuration} style={{ width: 100 }} />
              </label>
              <button className="primary" onClick={generateBgm} disabled={bgmRunning || !bgmPrompt.trim()} style={{ alignSelf: "flex-end" }}>
                {bgmRunning && <span className="spinner" />}<Icon name="music" size={14} /> Generate
              </button>
            </div>
            {bgmDraftStatus && <div className="status">{bgmDraftStatus}</div>}
            <LogPanel log={bgmJob.log} running={bgmRunning} />
            {takes.length > 0 && (
              <div className="audio-row">
                {takes.map((t, i) => (
                  <div key={t} className="audio-card"
                    style={chosenTake === t ? { borderColor: "var(--accent)" } : undefined}>
                    <div className="cap">Take {i + 1}{chosenTake === t ? "(使用中)" : ""}</div>
                    {/* 波形+再生位置の縦線: 後半が無音のテイクをパッと見て分かるように(2026-07-15) */}
                    <AudioWithWaveform src={media(t)} />
                    <button style={{ marginTop: 6 }} onClick={() => setChosenTake(t)}>
                      {chosenTake === t ? "✅ using this take" : `Use take ${i + 1}`}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <label className="field">
          BGM volume: {volume.toFixed(2)}
          <input type="range" min={0} max={2} step={0.05} value={volume} onChange={(e) => setVolume(Number(e.target.value))} />
        </label>

        <div className="row">
          <button className="primary" onClick={run} disabled={running || !videoPath}>
            {running && <span className="spinner" />}Run
          </button>
          {status === "done" && <span className="status ok">✅ finished</span>}
          {status === "error" && <span className="status error">❌ failed — check the log</span>}
          {message && <span className="status">{message}</span>}
        </div>
        <LogPanel log={log} running={running} />

        {(state.voice || state.sfx || state.bgmOrig) && (
          <div className="audio-row">
            {state.voice && <div className="audio-card"><div className="cap">Voice (separated)</div><AudioWithWaveform src={media(state.voice, jobId)} /></div>}
            {state.sfx && <div className="audio-card"><div className="cap">SFX (separated)</div><AudioWithWaveform src={media(state.sfx, jobId)} /></div>}
            {state.bgmOrig && <div className="audio-card"><div className="cap">BGM (separated from original)</div><AudioWithWaveform src={media(state.bgmOrig, jobId)} /></div>}
          </div>
        )}
        {state.result && (
          <div>
            <h3>Mixed result video</h3>
            <video className="final-video" src={media(state.result, jobId)} controls style={{ marginTop: 8 }} />
          </div>
        )}
      </div>
    </div>
  );
}
