import { useEffect, useRef, useState } from "react";
import { api, media, type Engine, type RunInfo, type RunSnapshot } from "../api";
import { NumberField, SegRadio } from "../common";
import { useLightbox } from "../Lightbox";
import { Icon } from "../Icon";
import { useSync } from "../sync";

type EditSeg = {
  segNum: number;
  label: string;
  path: string;
  mt?: number;
  trimStart: number;
  trimEnd: number;
  removed: boolean;
};

/** トリム用モーダル(動画を見ながらデュアルレンジスライダー+数値入力——
 * Gradioでは実現できず保留していたUI。Preview trimでその場で単体トリム結果を確認できる) */
function TrimModal({ seg, onApply, onClose }: {
  seg: EditSeg;
  onApply: (trimStart: number, trimEnd: number) => void;
  onClose: () => void;
}) {
  const [duration, setDuration] = useState<number | null>(null);
  const [trimStart, setTrimStart] = useState(seg.trimStart);
  const [trimEnd, setTrimEnd] = useState(seg.trimEnd);
  const [previewSrc, setPreviewSrc] = useState(media(seg.path));
  const [working, setWorking] = useState(false);
  const [status, setStatus] = useState("");

  useEffect(() => {
    api<{ duration: number }>(`/api/duration?p=${encodeURIComponent(seg.path)}`)
      .then((r) => setDuration(Math.round(r.duration * 10) / 10))
      .catch(() => setDuration(null));
  }, [seg.path]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dur = duration ?? 10;
  const keepStart = Math.min(trimStart, dur);
  const keepEnd = Math.max(keepStart + 0.1, dur - trimEnd);

  const preview = async () => {
    setWorking(true);
    setStatus("");
    try {
      const r = await api<{ out: string }>("/api/edit/trim-preview", {
        json: { path: seg.path, trimStart, trimEnd },
      });
      setPreviewSrc(`${media(r.out)}&v=${Date.now()}`);
      setStatus(`Preview: ${(dur - trimStart - trimEnd).toFixed(1)}s(元 ${dur.toFixed(1)}s)`);
    } catch (e) {
      setStatus(`Error: ${(e as Error).message}`);
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <h2>Trim seg{String(seg.segNum).padStart(2, "0")} ({seg.label})</h2>
        <video src={previewSrc} controls autoPlay muted />
        <div className="trim-slider">
          <div className="track" />
          <div
            className="range"
            style={{ left: `${(keepStart / dur) * 100}%`, width: `${((keepEnd - keepStart) / dur) * 100}%` }}
          />
          <input
            type="range" min={0} max={dur} step={0.1} value={keepStart}
            onChange={(e) => setTrimStart(Math.min(Number(e.target.value), dur - trimEnd - 0.1))}
          />
          <input
            type="range" min={0} max={dur} step={0.1} value={keepEnd}
            onChange={(e) => setTrimEnd(Math.max(0, Math.min(dur - Number(e.target.value), dur - trimStart - 0.1)))}
          />
        </div>
        <div className="row">
          <label className="field">
            Trim start (s)
            <NumberField value={trimStart} min={0} onChange={setTrimStart} style={{ width: 110 }} />
          </label>
          <label className="field">
            Trim end (s)
            <NumberField value={trimEnd} min={0} onChange={setTrimEnd} style={{ width: 110 }} />
          </label>
          <div className="status" style={{ alignSelf: "flex-end" }}>
            {duration != null ? `元の尺: ${dur.toFixed(1)}s → トリム後: ${Math.max(0.1, dur - trimStart - trimEnd).toFixed(1)}s` : "…"}
          </div>
        </div>
        <div className="row">
          <button onClick={preview} disabled={working}>{working && <span className="spinner" />}Preview trim</button>
          <button className="primary" onClick={() => onApply(trimStart, trimEnd)}>Apply</button>
          <button onClick={onClose}>Cancel</button>
          {status && <span className="status">{status}</span>}
        </div>
      </div>
    </div>
  );
}

export default function Edit() {
  const { sync, update, setBusy } = useSync();
  const [engine, setEngine] = useState<Engine>("i2v");
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [runId, setRunId] = useState("");
  const [segs, setSegs] = useState<EditSeg[]>([]);
  const [trimTarget, setTrimTarget] = useState<EditSeg | null>(null);
  const [resultVideo, setResultVideo] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [working, setWorking] = useState(false);
  const dragIndex = useRef<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);
  const lightbox = useLightbox();

  useEffect(() => setBusy("edit", working), [working, setBusy]);

  useEffect(() => {
    if (sync.runId) {
      setEngine(sync.engine);
      setRunId(sync.runId);
    }
  }, [sync.runId, sync.engine]);

  useEffect(() => {
    api<RunInfo[]>(`/api/runs/${engine}`).then((list) => {
      setRuns(list);
      setRunId((r) => (r && list.some((x) => x.runId === r) ? r : (list[0]?.runId ?? "")));
    });
  }, [engine, sync.videosBump]);

  useEffect(() => {
    setResultVideo(null);
    setStatus("");
    if (!runId) {
      setSegs([]);
      return;
    }
    api<RunSnapshot>(`/api/runs/${engine}/${runId}`).then((snap) =>
      setSegs(snap.segments.map((s) => ({ segNum: s.num, label: s.label, path: s.path, mt: s.mt, trimStart: 0, trimEnd: 0, removed: false }))),
    );
  }, [engine, runId, sync.videosBump]);

  // --- 本物のドラッグ&ドロップ並び替え(HTML5 DnD、Gradio版の▲▼ボタンの置き換え) ---
  const onDrop = (to: number) => {
    const from = dragIndex.current;
    dragIndex.current = null;
    setDragOver(null);
    if (from == null || from === to) return;
    setSegs((list) => {
      const next = [...list];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next;
    });
  };

  const toggleRemove = (i: number) =>
    setSegs((list) => list.map((s, j) => (j === i ? { ...s, removed: !s.removed } : s)));

  const payloadSegs = () =>
    segs.map((s) => ({ path: s.path, trimStart: s.trimStart, trimEnd: s.trimEnd, removed: s.removed }));

  const preview = async () => {
    setWorking(true);
    setStatus("");
    try {
      const r = await api<{ out: string }>("/api/edit/preview", { json: { segments: payloadSegs() } });
      setResultVideo(`${media(r.out)}&v=${Date.now()}`);
      setStatus("Preview built (not saved yet)");
    } catch (e) {
      setStatus(`Error: ${(e as Error).message}`);
    } finally {
      setWorking(false);
    }
  };

  const commit = async () => {
    setWorking(true);
    setStatus("");
    try {
      const r = await api<{ out: string }>("/api/edit/commit", { json: { engine, runId, segments: payloadSegs() } });
      setResultVideo(`${media(r.out)}&v=${Date.now()}`);
      setStatus(`✅ Committed to ${r.out.split("/").pop()}(旧ファイルは _oldN として退避済み)`);
      update({ video: r.out, videosBump: sync.videosBump + 1 });
    } catch (e) {
      setStatus(`Error: ${(e as Error).message}`);
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="tab-page">
      <div className="panel">
        <h2>④ Edit — 並び替え(ドラッグ&ドロップ)・トリム・削除(再生成なし)</h2>
        <div className="row">
          <select className="grow" value={runId} onChange={(e) => setRunId(e.target.value)}>
            <option value="">— select run —</option>
            {runs.map((r) => <option key={r.runId} value={r.runId}>{r.label}</option>)}
          </select>
          <button className="icon" title="Refresh list" onClick={() => update({ videosBump: sync.videosBump + 1 })}><Icon name="refresh" size={14} /></button>
          <SegRadio options={["i2v", "t2v"] as const} value={engine} onChange={setEngine} />
        </div>
        <div className="status">カードをドラッグで並び替え。Commitするまで元ファイルには一切触れません。</div>

        <div className="edit-grid">
          {segs.map((s, i) => (
            <div
              key={s.path}
              className={`edit-card ${s.removed ? "removed" : ""} ${dragOver === i ? "drag-over" : ""}`}
              draggable
              onDragStart={() => (dragIndex.current = i)}
              onDragOver={(e) => { e.preventDefault(); setDragOver(i); }}
              onDragLeave={() => setDragOver((d) => (d === i ? null : d))}
              onDrop={() => onDrop(i)}
              onDragEnd={() => { dragIndex.current = null; setDragOver(null); }}
            >
              <video src={media(s.path, s.mt)} controls preload="metadata" muted />
              <div className="seg-head">
                <span className="name" title="drag to reorder">
                  ⠿ seg{String(s.segNum).padStart(2, "0")}
                  {(s.trimStart > 0 || s.trimEnd > 0) && <b> ✂{s.trimStart.toFixed(1)}/{s.trimEnd.toFixed(1)}</b>}
                </span>
                <span className="row" style={{ gap: 4 }}>
                  <button title="Expand" onClick={() => {
                    const items = segs.map((x) => ({
                      path: x.path,
                      caption: `seg${String(x.segNum).padStart(2, "0")} (${x.label})`,
                      kind: "video" as const,
                      v: x.mt,
                    }));
                    lightbox.open(items, i);
                  }}><Icon name="search" size={14} /></button>
                  <button title="Trim" onClick={() => setTrimTarget(s)} disabled={s.removed}><Icon name="scissors" size={14} /></button>
                  <button className="danger" title={s.removed ? "Restore" : "Remove"} onClick={() => toggleRemove(i)}>
                    <Icon name={s.removed ? "undo" : "trash"} size={14} />
                  </button>
                </span>
              </div>
            </div>
          ))}
          {!segs.length && <div className="status">(no segments loaded — pick a run above)</div>}
        </div>

        <div className="row">
          <button onClick={preview} disabled={working || !segs.length}>{working && <span className="spinner" />}<Icon name="play" size={14} /> Preview (not saved)</button>
          <button className="primary" onClick={commit} disabled={working || !segs.length}><Icon name="save" size={14} /> Commit to final.mp4</button>
          {status && <span className="status">{status}</span>}
        </div>
        {resultVideo && <video className="final-video" src={resultVideo} controls />}
      </div>

      {trimTarget && (
        <TrimModal
          seg={trimTarget}
          onClose={() => setTrimTarget(null)}
          onApply={(ts, te) => {
            setSegs((list) => list.map((s) => (s.path === trimTarget.path ? { ...s, trimStart: ts, trimEnd: te } : s)));
            setTrimTarget(null);
          }}
        />
      )}
    </div>
  );
}
