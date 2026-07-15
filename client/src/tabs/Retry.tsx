import { useEffect, useState } from "react";
import { api, useJob, type Engine, type RunInfo, type RunSnapshot, type SegPrompt } from "../api";
import { FinalVideo, KeyframeGallery, LogPanel, SegRadio, SegVideoGrid, type KfDisplay, type SegDisplay } from "../common";
import { useSync } from "../sync";
import { Icon } from "../Icon";

export default function Retry() {
  const { sync, update, setBusy } = useSync();
  const [engine, setEngine] = useState<Engine>("i2v");
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [runId, setRunId] = useState("");
  const [snap, setSnap] = useState<RunSnapshot | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [keep, setKeep] = useState(false);
  const [norefine, setNorefine] = useState(false);
  const [editPrompt, setEditPrompt] = useState("");
  const [editKfPrompt, setEditKfPrompt] = useState("");
  const [editLoaded, setEditLoaded] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const { log, state, status } = useJob(jobId);
  const running = status === "running";

  useEffect(() => setBusy("retry", running), [running, setBusy]);

  // 生成完了時の自動同期(Generateタブ側が sync.runId/engine を更新する)
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

  const loadSnapshot = () => {
    if (!runId) {
      setSnap(null);
      return;
    }
    api<RunSnapshot>(`/api/runs/${engine}/${runId}`).then(setSnap);
  };
  useEffect(() => {
    setSelected([]);
    loadSnapshot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine, runId]);
  // リトライ完了時に再取得
  useEffect(() => {
    if (status === "done" || status === "error") loadSnapshot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  // ちょうど1セグメント選択時のみ、保存済みプロンプトを読み込んで編集アコーディオンを出す
  useEffect(() => {
    setEditLoaded(false);
    setEditPrompt("");
    setEditKfPrompt("");
    if (selected.length !== 1 || !runId) return;
    api<SegPrompt>(`/api/runs/${engine}/${runId}/seg/${selected[0]}/prompt`)
      .then((seg) => {
        setEditPrompt(seg.prompt);
        setEditKfPrompt(seg.kf_prompt ?? "");
        setEditLoaded(true);
      })
      .catch(() => setEditLoaded(false));
  }, [selected, engine, runId]);

  const toggleSeg = (num: number) =>
    setSelected((s) => (s.includes(num) ? s.filter((n) => n !== num) : [...s, num].sort((a, b) => a - b)));

  const run = async () => {
    if (!runId || !selected.length) {
      setMessage("(select a run and at least one segment)");
      return;
    }
    setMessage("");
    try {
      const r = await api<{ jobId: string; warning: string | null }>("/api/retry", {
        json: { engine, runId, segs: selected, keep, norefine, editPrompt: editLoaded ? editPrompt : "", editKfPrompt },
      });
      setJobId(r.jobId);
      if (r.warning) setMessage(`⚠️ ${r.warning}`);
    } catch (e) {
      setMessage(`Error: ${(e as Error).message}`);
    }
  };

  // リトライ完了時、CASS/Upscaleの対象動画を更新後のfinalへ同期
  useEffect(() => {
    if (status === "done" && state.final) {
      update({ video: state.final, videosBump: sync.videosBump + 1 });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  // 表示するセグメント/キーフレーム: リトライ中はスキャンから消えても枠を消さず、
  // prompts.txt由来の期待セグメント一覧(expected)を基準に「生成中…」プレースホルダを出す
  // (数とセグメント番号は分かっているため——ユーザー要望 2026-07-15)
  // ジョブのstateは「そのジョブが対象にしたrun」を選択中かつ実行中の間だけ表示に合成する。
  // これが無いと、一度リトライ完了(status=done)した後に別のrunを選んでも、
  // 古いジョブのsegments/keyframes/finalが新しいsnapを上書きし続ける(2026-07-15ユーザー報告)
  const jobIsCurrent = running && state.runId === runId;
  const live = jobIsCurrent ? { ...snap, ...state } : snap;
  let segments: SegDisplay[] = live?.segments ?? [];
  let keyframes: KfDisplay[] = live?.keyframes ?? [];
  const expected = live?.expected ?? [];
  if (jobIsCurrent && expected.length) {
    const liveSegs = state.segments ?? [];
    segments = expected.map((b) => liveSegs.find((s) => s.num === b.num) ?? { ...b, path: null });
    if (engine === "i2v") {
      const liveKfs = state.keyframes ?? [];
      keyframes = expected.map(
        (b) => liveKfs.find((k) => k.seg === b.num)
          ?? { seg: b.num, caption: `seg${String(b.num).padStart(2, "0")}`, path: null },
      );
    }
  }

  return (
    <div className="tab-page">
      <div className="panel">
        <h2>③ Retry</h2>
        <div className="row">
          <select className="grow" value={runId} onChange={(e) => setRunId(e.target.value)}>
            <option value="">— select run —</option>
            {runs.map((r) => <option key={r.runId} value={r.runId}>{r.label}</option>)}
          </select>
          <button className="icon" title="Refresh list" onClick={() => update({ videosBump: sync.videosBump + 1 })}><Icon name="refresh" size={14} /></button>
          <SegRadio options={["i2v", "t2v"] as const} value={engine} onChange={setEngine} />
        </div>

        <KeyframeGallery keyframes={keyframes} />
        {/* リトライ対象の選択はセグメント動画カードで行う(t2vにはキーフレームが無いため) */}
        <SegVideoGrid segments={segments} selectable={!running} selected={selected} onToggle={toggleSeg} />

        {engine === "i2v" && (
          <div className="row">
            <label className={`check-chip ${keep ? "checked" : ""}`}>
              <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
              --keep (reuse existing keyframe)
            </label>
            <label className={`check-chip ${norefine ? "checked" : ""}`}>
              <input type="checkbox" checked={norefine} onChange={(e) => setNorefine(e.target.checked)} />
              --norefine
            </label>
          </div>
        )}

        {selected.length === 1 && editLoaded && (
          <details className="log" open>
            <summary>✏️ Edit prompt (seg{String(selected[0]).padStart(2, "0")}) — 保存済みプロンプトを直接編集してリトライ</summary>
            <div className="panel" style={{ border: "none" }}>
              {engine === "i2v" && (
                <label className="field">
                  Keyframe prompt
                  <textarea rows={4} value={editKfPrompt} onChange={(e) => setEditKfPrompt(e.target.value)} spellCheck={false} />
                </label>
              )}
              <label className="field">
                {engine === "i2v" ? "Motion prompt" : "LTX prompt"}
                <textarea rows={6} value={editPrompt} onChange={(e) => setEditPrompt(e.target.value)} spellCheck={false} />
              </label>
            </div>
          </details>
        )}

        <div className="row">
          <button className="primary" onClick={run} disabled={running || !selected.length}>
            {running && <span className="spinner" />}Run retry{selected.length > 0 ? `(seg ${selected.join(", ")})` : ""}
          </button>
          {status === "done" && <span className="status ok">✅ finished</span>}
          {status === "error" && <span className="status error">❌ failed — check the log</span>}
          {message && <span className="status">{message}</span>}
        </div>

        <LogPanel log={log} running={running} />
        <FinalVideo path={live?.final ?? null} mtime={live?.finalMtime ?? null} />
      </div>
    </div>
  );
}
