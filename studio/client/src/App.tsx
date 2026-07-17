import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { Icon } from "./Icon";
import { useLightbox } from "./Lightbox";
import {
  api, media, uploadFile, useJob,
  type Engine, type LibraryRunInfo, type LibraryRunDetail, type LibraryRunsResponse,
  type LibraryPeriod, type PromptFile, type SegPrompt, type Validation,
} from "./api";

type ViewMode = "segment" | "final" | "cass" | "upscale";
type Selection =
  | { kind: "new" }
  | { kind: "run"; engine: Engine; runId: string; segmentNums: number[]; view: ViewMode };

const PERIODS = ["today", "7d", "30d", "all"] as const satisfies readonly LibraryPeriod[];
type Period = (typeof PERIODS)[number];
const PERIOD_LABELS: Record<Period, string> = { today: "Today", "7d": "7 days", "30d": "30 days", all: "All" };

const ENGINE_FILTERS = ["all", "i2v", "t2v"] as const;
type EngineFilter = (typeof ENGINE_FILTERS)[number];

type UiStatus = "queued" | "generating" | "done" | "error";

interface UiSegment {
  num: number;
  label: string;
  path: string | null;
  mt?: number;
  status: UiStatus;
  keyframePath: string | null;
  removed: boolean;
  trimStart: number;
  trimEnd: number;
}

/** common.tsxのSegRadioと同じAPI(options/value/onChange/labels)のセグメントボタン群。 */
function SegRadio<T extends string>({ options, value, onChange, labels }: {
  options: readonly T[]; value: T; onChange: (v: T) => void; labels?: Record<string, string>;
}) {
  return (
    <div className="seg-radio">
      {options.map((o) => (
        <button key={o} className={o === value ? "active" : ""} onClick={() => onChange(o)}>
          {labels?.[o] ?? o}
        </button>
      ))}
    </div>
  );
}

/** New/RetryのプロンプトテキストA−/A+と共通のfont-size(localStorage永続化)。
 * コンポーネントごとに独立したstateだが、同じキーを読むためマウント時点の値は揃う。 */
function usePromptFontSize() {
  const [fontSize, setFontSize] = useState(() => Number(localStorage.getItem("studioPromptFontSize")) || 14);
  const changeFontSize = (d: number) => {
    setFontSize((prev) => {
      const v = Math.min(24, Math.max(10, prev + d));
      localStorage.setItem("studioPromptFontSize", String(v));
      return v;
    });
  };
  return { fontSize, changeFontSize };
}

/** ラベル+readonlyテキストエリア+右上オーバーレイのCopyボタン(Libraryタブと同じパターン)。 */
function CopyableField({ label, value, rows, fontSize, onChange }: {
  label?: string; value: string; rows: number; fontSize?: number; onChange?: (v: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="field">
      {label && <label>{label}</label>}
      <div style={{ position: "relative" }}>
        <button
          className="icon" title={copied ? "Copied!" : "Copy to clipboard"}
          style={{ position: "absolute", top: 6, right: 6, padding: "3px 5px" }}
          onClick={() => {
            navigator.clipboard.writeText(value).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
        >
          <Icon name="copy" size={13} />
        </button>
        <textarea
          className="script" rows={rows} readOnly={!onChange} value={value}
          onChange={onChange ? (e) => onChange(e.target.value) : undefined}
          style={fontSize ? { fontSize } : undefined}
        />
      </div>
    </div>
  );
}

/** Web Audio APIで実際にデコードしてcanvasへ波形描画(client/src/common.tsxのAudioWithWaveformを
 * 移植)+再生位置の縦線。ラベルは波形の左上に重ねる(2026-07-17ユーザー指摘: 波形が無かった)。 */
function LabeledWaveform({ label, path }: { label: string; path?: string | null }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const playheadRef = useRef<HTMLDivElement>(null);
  const height = 28;

  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    (async () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      try {
        const buf = await (await fetch(media(path))).arrayBuffer();
        const ctx = new AudioContext();
        const audioBuf = await ctx.decodeAudioData(buf);
        ctx.close();
        if (cancelled) return;
        const dpr = window.devicePixelRatio || 1;
        const w = canvas.clientWidth || 300;
        canvas.width = w * dpr;
        canvas.height = height * dpr;
        const g = canvas.getContext("2d")!;
        g.scale(dpr, dpr);
        const data = audioBuf.getChannelData(0);
        const buckets = Math.max(100, Math.floor(w / 2));
        const step = Math.floor(data.length / buckets) || 1;
        g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0ea5e9";
        for (let i = 0; i < buckets; i++) {
          let peak = 0;
          const start = i * step;
          for (let j = start; j < start + step && j < data.length; j += 16) {
            const v = Math.abs(data[j]);
            if (v > peak) peak = v;
          }
          const h = Math.max(1, peak * (height - 4));
          g.fillRect((i / buckets) * w, (height - h) / 2, Math.max(1, w / buckets - 1), h);
        }
      } catch {
        /* デコード失敗時は波形なしでaudio要素のみ */
      }
    })();
    return () => { cancelled = true; };
  }, [path]);

  useEffect(() => {
    const el = audioRef.current;
    const line = playheadRef.current;
    if (!el || !line) return;
    const update = () => {
      const frac = el.duration > 0 ? el.currentTime / el.duration : 0;
      line.style.left = `${Math.min(1, Math.max(0, frac)) * 100}%`;
      line.style.opacity = el.currentTime > 0 ? "1" : "0";
    };
    el.addEventListener("timeupdate", update);
    el.addEventListener("seeking", update);
    el.addEventListener("play", update);
    el.addEventListener("ended", update);
    return () => {
      el.removeEventListener("timeupdate", update);
      el.removeEventListener("seeking", update);
      el.removeEventListener("play", update);
      el.removeEventListener("ended", update);
    };
  }, [path]);

  if (!path) {
    return (
      <div className="waveform-box">
        <span className="waveform-label">{label}</span>
      </div>
    );
  }
  return (
    <div className="waveform-wrap">
      <div className="waveform-canvas-wrap" style={{ height }}>
        <span className="waveform-label">{label}</span>
        <canvas ref={canvasRef} className="waveform" style={{ height }} />
        <div ref={playheadRef} className="waveform-playhead" style={{ height, opacity: 0 }} />
      </div>
      <audio ref={audioRef} src={media(path)} controls />
    </div>
  );
}

/** run一覧の小さいサムネイル。読み込み失敗時(生成直後の一時的な欠落・破損等)は
 * 壊れた画像アイコンを出さずfilmアイコンへ静かにフォールバックする(2026-07-17指摘:
 * 「サムネイルにエラーが出て表示しない」)。 */
function RunThumb({ path, mt }: { path: string | null; mt: number }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [path]);
  if (!path || failed) return <Icon name="film" size={16} />;
  return <img src={media(path, mt)} alt="" onError={() => setFailed(true)} />;
}

/** modules/timeline_common.py の _parse_prompt は日本語でraiseする(実harnessとの共有ロジックの
 * ため直接編集はしない)。2026-07-17ユーザー報告の該当メッセージのみ英訳する。未知の文言は
 * そのまま出す(黙って握りつぶさない)。 */
function translateParseError(msg: string): string {
  if (msg.includes("ヘッダーもタイムスタンプも見つかりません")) {
    return "No 'Timeline:' header or timestamp found.";
  }
  return msg;
}

/** server側のtsName()(studio/server/index.js)と同じ命名規則。Save後に次回用のファイル名を
 * ここで進める(サーバーには問い合わせない、次に押されるSaveまでは実ファイルを作らない)。 */
function newHarnessFilename(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `harness_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.txt`;
}

function statusLabel(s: UiStatus) {
  return { done: "done", generating: "generating…", queued: "queued", error: "failed" }[s];
}

function dateGroupKey(ms: number): string {
  const d = new Date(ms);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = today.getTime() - 24 * 60 * 60 * 1000;
  const dayStart = new Date(d).setHours(0, 0, 0, 0);
  if (dayStart === today.getTime()) return "Today";
  if (dayStart === yesterday) return "Yesterday";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** LibraryRunDetail(閲覧専用の静的スナップショット)からUI用segment配列を組み立てる。
 * detail.segments/keyframesはLibraryの設計上、_old1/_old2等のバックアップも同じnumで
 * 複数件含む(閲覧用に隠さず全部返す仕様)。ここではeditの対象になる「現在有効な1本」
 * (variant === null)だけに絞り込む — 絞らないと同じnumが重複しstudioのnum単位の選択・
 * Edit配列がすべて壊れる(2026-07-17実機調査で発覚)。 */
function segmentsFromDetail(detail: LibraryRunDetail): UiSegment[] {
  const currentSegs = detail.segments.filter((s) => s.variant === null);
  const currentKfs = detail.keyframes.filter((k) => k.variant === null);
  return currentSegs.map((s) => ({
    num: s.num,
    label: s.label,
    path: s.path,
    mt: s.mt,
    status: "done" as UiStatus,
    keyframePath: currentKfs.find((k) => k.seg === s.num)?.path ?? null,
    removed: false,
    trimStart: 0,
    trimEnd: 0,
  }));
}

type EditStage = { order?: number[]; removed?: number[]; trims?: Record<string, { trimStart: number; trimEnd: number }> } | null;

/** sidecar(generated/{prefix}_{runId}_studio_edit.json)の内容をsegmentsFromDetail結果へ適用する。
 * numが一致しないもの(staged.orderに無い新規seg等)は末尾へ残す。 */
function applyStagedEdit(segs: UiSegment[], staged: EditStage): UiSegment[] {
  if (!staged) return segs;
  const byNum = new Map(segs.map((s) => [s.num, s]));
  const removedSet = new Set(staged.removed ?? []);
  const ordered = (staged.order ?? []).map((n) => byNum.get(n)).filter((s): s is UiSegment => !!s);
  const missing = segs.filter((s) => !ordered.includes(s));
  return [...ordered, ...missing].map((s) => ({
    ...s,
    removed: removedSet.has(s.num),
    trimStart: staged.trims?.[String(s.num)]?.trimStart ?? 0,
    trimEnd: staged.trims?.[String(s.num)]?.trimEnd ?? 0,
  }));
}

/** 現在有効なfinal(variant===nullのうち最新=配列先頭、mtime降順ソート済み)。 */
function currentFinalOf(detail: LibraryRunDetail | null): { path: string; mt: number } | null {
  if (!detail) return null;
  const f = detail.finals.find((f) => f.variant === null);
  return f ? { path: f.path, mt: f.mt } : null;
}

/** CASS/Upscaleの対象として選べる動画一覧(final各variant + CASS成果物、mtime降順)。
 * 2026-07-17ユーザー指摘: CASS済みのrunもあるので、final.mp4決め打ちでなく選べるようにする。 */
/** ファイル名から {prefix}_{runId}_ の接頭辞を外し、意味のある部分だけをラベルにする
 * (2026-07-17ユーザー指摘: フルファイル名の表記は不要、run自体は選択済みで自明のため)。 */
function shortFinalLabel(name: string): string {
  const rest = name.replace(/^\w+6_\d{8}_\d{6}_/, "").replace(/\.mp4$/, "");
  if (rest === "final") return "Final";
  if (rest === "final_FHD") return "Final (FHD)";
  if (rest === "final_remixed") return "CASS mix";
  if (rest === "final_remixed_FHD") return "CASS mix (FHD)";
  const oldM = /^final(_remixed)?(_FHD)?_old(\d+)$/.exec(rest);
  if (oldM) return `${oldM[1] ? "CASS mix" : "Final"}${oldM[2] ? " (FHD)" : ""} — old${oldM[3]}`;
  return rest;
}

function finalOptions(detail: LibraryRunDetail | null): { path: string; mt: number; label: string; isFHD: boolean }[] {
  if (!detail) return [];
  const finals = detail.finals.map((f) => {
    const name = f.path.split("/").pop() ?? f.path;
    return { path: f.path, mt: f.mt, label: shortFinalLabel(name), isFHD: f.isFHD };
  });
  const cass = detail.cass.videos.map((v) => ({ path: v.path, mt: v.mt, label: shortFinalLabel(v.name), isFHD: v.name.includes("_FHD") }));
  return [...finals, ...cass].sort((a, b) => b.mt - a.mt);
}

/** 生成中run向け: SSEのstate(expected/segments/keyframes)からUI用segment配列を組み立てる
 * (Generate.tsx/Retry.tsxと同じ「expectedを土台に、まだ無いものはプレースホルダ」ロジック)。 */
function segmentsFromJobState(state: {
  expected?: { num: number; label: string }[];
  segments?: { num: number; label: string; path: string; mt?: number }[];
  keyframes?: { seg: number; path: string; mt?: number }[];
}): UiSegment[] {
  const expected = state.expected ?? [];
  const liveSegs = state.segments ?? [];
  const liveKfs = state.keyframes ?? [];
  return expected.map((b) => {
    const live = liveSegs.find((s) => s.num === b.num);
    const kf = liveKfs.find((k) => k.seg === b.num);
    return {
      num: b.num,
      label: live?.label || b.label,
      path: live?.path ?? null,
      mt: live?.mt,
      status: (live ? "done" : kf ? "generating" : "queued") as UiStatus,
      keyframePath: kf?.path ?? null,
      removed: false,
      trimStart: 0,
      trimEnd: 0,
    };
  });
}

export default function App() {
  const [period, setPeriod] = useState<Period>("7d");
  const [engineFilter, setEngineFilter] = useState<EngineFilter>("all");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [libraryRuns, setLibraryRuns] = useState<LibraryRunInfo[]>([]);
  const [runsTotal, setRunsTotal] = useState(0);
  const [runsTruncated, setRunsTruncated] = useState(false);
  const [confirmDeleteKey, setConfirmDeleteKey] = useState<string | null>(null);
  // --keep/--norefineはSegmentInspector側のローカルstateだと再マウントのたびにリセットされる。
  // 何度もretryを繰り返す運用のため、Appへ持ち上げて維持する(2026-07-17ユーザー指摘)。
  const [retryKeep, setRetryKeep] = useState(false);
  const [retryNorefine, setRetryNorefine] = useState(false);
  // 「+ New」を押した瞬間、次のSave用ファイル名を必ず新規発番する(2026-07-17ユーザー指摘:
  // 旧ハーネスはタブ行き来があったので工夫が要ったが、studioは必ず+Newを経由するのでここで
  // 十分)。NewInspectorはhiddenで永続マウントのため、この値の変化をuseEffectで監視させる。
  const [newSessionKey, setNewSessionKey] = useState(0);

  const [selection, setSelection] = useState<Selection>({ kind: "new" });
  const [detail, setDetail] = useState<LibraryRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  /** 生成中/リトライ中のジョブ。selection.runIdと一致する間だけSSEのstateをsegmentsに反映する。 */
  const [activeJob, setActiveJob] = useState<{ jobId: string; engine: Engine; runId: string } | null>(null);
  const jobHook = useJob(activeJob?.jobId ?? null);
  const lastJobLogLen = useRef(0);

  const [anchorNum, setAnchorNum] = useState<number | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [log, setLog] = useState<string[]>(["$ studio — connected to server/*.js via studio/server (port 7865)"]);
  const logPreRef = useRef<HTMLPreElement>(null);
  const dragIndex = useRef<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);
  const [trimming, setTrimming] = useState<{ num: number; trimStart: number; trimEnd: number } | null>(null);
  const [trimPreviewSrc, setTrimPreviewSrc] = useState<string | null>(null);
  const [trimPreviewV, setTrimPreviewV] = useState(0);
  const [trimPreviewForNum, setTrimPreviewForNum] = useState<number | null>(null);
  const [trimPreviewing, setTrimPreviewing] = useState(false);
  const [editPreviewing, setEditPreviewing] = useState(false);
  const [editCommitting, setEditCommitting] = useState(false);

  /** remove/reorder/trimの未保存編集状態。runを選び直すたびdetailから再初期化する。 */
  const [editSegs, setEditSegs] = useState<UiSegment[]>([]);
  const [finalPreviewPath, setFinalPreviewPath] = useState<string | null>(null);
  const [cassPreviewPath, setCassPreviewPath] = useState<string | null>(null);
  const [upscalePreviewPath, setUpscalePreviewPath] = useState<string | null>(null);

  const [i2vEngine, setI2vEngine] = useState<"default" | "10E" | "refine">("default");
  const [acestepConfigured, setAcestepConfigured] = useState(false);

  const appendLog = (line: string) => setLog((l) => [...l, line]);

  // ログ追記のたび自動で一番下までスクロールする(実harnessのLogPanelと同じ、2026-07-17
  // ユーザー指摘: スクロールしない=新しい行が見えないまま止まって見えていた)。
  useEffect(() => {
    const el = logPreRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  /** copyボタンと同じ「隅にオーバーレイ」方式のDownload用(2026-07-17ユーザー指摘:
   * ボタンを映像の下に並べると縦幅を食って映像が縮む)。実際にファイルを落とす。 */
  const downloadFile = (path: string, mtime?: number) => {
    const a = document.createElement("a");
    a.href = media(path, mtime);
    a.download = path.split("/").pop() || "download";
    a.click();
    appendLog(`[studio] Download — ${path}`);
  };

  useEffect(() => {
    api<{ engine: "default" | "10E" | "refine" }>("/api/i2v-engine").then((r) => setI2vEngine(r.engine)).catch(() => {});
    api<{ configured: boolean }>("/api/acestep-config").then((r) => setAcestepConfigured(r.configured)).catch(() => {});
  }, []);

  // 検索は300msデバウンス
  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const refreshRuns = () => {
    const params = new URLSearchParams();
    if (period !== "all") params.set("since", period);
    if (engineFilter !== "all") params.set("engine", engineFilter);
    if (search) params.set("q", search);
    api<LibraryRunsResponse>(`/api/library/runs?${params}`).then((r) => {
      setLibraryRuns(r.runs);
      setRunsTotal(r.total);
      setRunsTruncated(r.truncated);
    }).catch((e) => appendLog(`[studio] Error loading runs: ${e.message}`));
  };
  useEffect(refreshRuns, [period, engineFilter, search]);

  const deleteRun = async (run: LibraryRunInfo) => {
    try {
      await api(`/api/library/runs/${run.engine}/${run.runId}`, { method: "DELETE" });
      setLibraryRuns((prev) => prev.filter((r) => !(r.engine === run.engine && r.runId === run.runId)));
      setRunsTotal((t) => Math.max(0, t - 1));
      setConfirmDeleteKey(null);
      if (selection.kind === "run" && selection.engine === run.engine && selection.runId === run.runId) {
        setSelection({ kind: "new" });
      }
      appendLog(`[studio] Deleted run ${run.engine}/${run.runId}`);
    } catch (e) {
      appendLog(`[studio] Error deleting run: ${(e as Error).message}`);
    }
  };

  /** 生成中のrunはまだ /api/library/runs の結果に含まれない(完了時にrefreshRunsするまで)。
   * その間、左のrun一覧にも一致するカードが無くハイライトできない(2026-07-17ユーザー指摘:
   * 「左ハーネスは1のまま」)。runIdが判明した時点で仮カードを差し込み、完了後は
   * refreshRunsが返す本物のエントリに自然に置き換わる。 */
  const displayRuns = useMemo(() => {
    if (activeJob?.runId && !libraryRuns.some((r) => r.engine === activeJob.engine && r.runId === activeJob.runId)) {
      const ghost: LibraryRunInfo = {
        engine: activeJob.engine, runId: activeJob.runId, source: "", label: activeJob.runId,
        mt: Date.now(), thumbnail: null,
      };
      return [ghost, ...libraryRuns];
    }
    return libraryRuns;
  }, [libraryRuns, activeJob]);

  const runGroups = useMemo(() => {
    const map = new Map<string, LibraryRunInfo[]>();
    for (const r of displayRuns) {
      const key = dateGroupKey(r.mt);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(r);
    }
    return [...map.entries()];
  }, [displayRuns]);

  // run選択時: 静的detailを取得。実行中ジョブがこのrunを指していればSSE側を優先する
  useEffect(() => {
    if (selection.kind !== "run") { setDetail(null); return; }
    if (activeJob && activeJob.runId === selection.runId) return; // ジョブ側のeffectが処理する
    setDetailLoading(true);
    Promise.all([
      api<LibraryRunDetail>(`/api/library/runs/${selection.engine}/${selection.runId}`),
      api<EditStage>(`/api/edit/stage/${selection.engine}/${selection.runId}`).catch(() => null),
    ])
      .then(([d, staged]) => {
        setDetail(d);
        setEditSegs(applyStagedEdit(segmentsFromDetail(d), staged));
        setFinalPreviewPath(null);
        setCassPreviewPath(null);
        setUpscalePreviewPath(null);
      })
      .catch((e) => appendLog(`[studio] Error loading run detail: ${e.message}`))
      .finally(() => setDetailLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection.kind === "run" ? selection.runId : null]);

  // アクティブジョブのSSE state → segmentsに反映。ログもここでミラーする
  useEffect(() => {
    if (!activeJob) return;
    lastJobLogLen.current = 0;
  }, [activeJob?.jobId]);
  useEffect(() => {
    if (!activeJob) return;
    if (jobHook.log.length > lastJobLogLen.current) {
      const added = jobHook.log.slice(lastJobLogLen.current);
      lastJobLogLen.current = jobHook.log.length;
      added.split("\n").filter(Boolean).forEach((line) => appendLog(line));
    }
  }, [jobHook.log, activeJob]);
  useEffect(() => {
    if (!activeJob) return;
    if (selection.kind === "run" && selection.runId === activeJob.runId) {
      const segs = segmentsFromJobState(jobHook.state);
      setEditSegs(segs);
      setDetail((d) => (d ? { ...d, segments: segs.filter((s) => s.path).map((s) => ({ num: s.num, label: s.label, path: s.path!, mt: s.mt ?? 0, variant: null })) } : d));
    }
    if (jobHook.status !== "running" && jobHook.status !== "idle") {
      appendLog(`[studio] Job ${activeJob.jobId} finished: ${jobHook.status}`);
      const finishedRunId = activeJob.runId;
      const finishedEngine = activeJob.engine;
      setActiveJob(null);
      refreshRuns();
      if (selection.kind === "run" && selection.runId === finishedRunId) {
        Promise.all([
          api<LibraryRunDetail>(`/api/library/runs/${finishedEngine}/${finishedRunId}`),
          api<EditStage>(`/api/edit/stage/${finishedEngine}/${finishedRunId}`).catch(() => null),
        ])
          .then(([d, staged]) => { setDetail(d); setEditSegs(applyStagedEdit(segmentsFromDetail(d), staged)); })
          .catch(() => {});
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobHook.state, jobHook.status, activeJob]);

  const selectedSegments: UiSegment[] =
    selection.kind === "run" ? editSegs.filter((s) => selection.segmentNums.includes(s.num)) : [];

  /** 現在有効なfinal(未保存のEdit previewがあればそちらを優先)。左のrun一覧から選んだ時点で
   * 中央FinalタブへあらかじめFinal動画を出す(2026-07-17ユーザー指摘: 選ぶ手段がない)。 */
  const currentFinal = currentFinalOf(detail);
  const displayFinalPath = finalPreviewPath ?? currentFinal?.path ?? null;
  const displayFinalMt = finalPreviewPath ? undefined : currentFinal?.mt;

  /** CASS/Upscaleも中央上部の独立タブとして出す(2026-07-17ユーザー指摘: Finalの右パネル
   * サブタブではなくSegment/Finalと並ぶタブの方がよい)。完了直後はジョブ結果を優先表示、
   * 既存runを選んだ時はdetail(実ファイルスキャン)から最新のものを出す。 */
  const latestCass = detail?.cass.videos[0] ?? null;
  const displayCassPath = cassPreviewPath ?? latestCass?.path ?? null;
  const displayCassMt = cassPreviewPath ? undefined : latestCass?.mt;
  const latestUpscale = detail?.finals.find((f) => f.isFHD) ?? null;
  const displayUpscalePath = upscalePreviewPath ?? latestUpscale?.path ?? null;
  const displayUpscaleMt = upscalePreviewPath ? undefined : latestUpscale?.mt;

  const selectRun = (run: LibraryRunInfo) => {
    setSelection({ kind: "run", engine: run.engine, runId: run.runId, segmentNums: [], view: "final" });
    setAnchorNum(null);
    setTrimming(null);
  };

  const selectSegment = (seg: UiSegment, e: MouseEvent) => {
    if (selection.kind !== "run") return;
    setTrimming((t) => (t && t.num === seg.num ? t : null));
    if (e.shiftKey && anchorNum != null) {
      const nums = editSegs.map((s) => s.num);
      const a = nums.indexOf(anchorNum);
      const b = nums.indexOf(seg.num);
      const [lo, hi] = a < b ? [a, b] : [b, a];
      setSelection({ ...selection, segmentNums: nums.slice(lo, hi + 1), view: "segment" });
    } else if (e.metaKey || e.ctrlKey) {
      const has = selection.segmentNums.includes(seg.num);
      const next = has ? selection.segmentNums.filter((n) => n !== seg.num) : [...selection.segmentNums, seg.num];
      setSelection({ ...selection, segmentNums: next, view: "segment" });
      setAnchorNum(seg.num);
    } else {
      setSelection({ ...selection, segmentNums: [seg.num], view: "segment" });
      setAnchorNum(seg.num);
    }
  };
  const setView = (view: ViewMode) => {
    if (selection.kind !== "run") return;
    setSelection({ ...selection, view });
  };

  /** remove/reorder/trimをrun単位の別ファイルへ保存(2026-07-17ユーザー承認: sidecar方式)。
   * 翌日以降このrunを開き直しても復元できるようにする。ベストエフォートで、失敗してもUI操作
   * 自体は止めない。 */
  const stageEdit = (segs: UiSegment[]) => {
    if (selection.kind !== "run") return;
    api("/api/edit/stage", {
      json: {
        engine: selection.engine,
        runId: selection.runId,
        order: segs.map((s) => s.num),
        removed: segs.filter((s) => s.removed).map((s) => s.num),
        trims: Object.fromEntries(
          segs.filter((s) => s.trimStart > 0 || s.trimEnd > 0).map((s) => [s.num, { trimStart: s.trimStart, trimEnd: s.trimEnd }]),
        ),
      },
    }).catch(() => {});
  };

  const setSegmentsRemoved = (nums: number[], removed: boolean) => {
    setEditSegs((prev) => {
      const next = prev.map((s) => (nums.includes(s.num) ? { ...s, removed } : s));
      stageEdit(next);
      return next;
    });
    setFinalPreviewPath(null);
  };

  const reorderSegments = (fromIndex: number, toIndex: number) => {
    setEditSegs((prev) => {
      if (fromIndex === toIndex) return prev;
      const segs = [...prev];
      const [moved] = segs.splice(fromIndex, 1);
      segs.splice(toIndex, 0, moved);
      stageEdit(segs);
      return segs;
    });
    setFinalPreviewPath(null);
  };

  const commitTrimLocal = () => {
    if (!trimming) return;
    setEditSegs((prev) => {
      const next = prev.map((s) => (
        s.num === trimming.num ? { ...s, trimStart: trimming.trimStart, trimEnd: trimming.trimEnd } : s
      ));
      stageEdit(next);
      return next;
    });
    appendLog(`[studio] Trim set — seg ${trimming.num} (start=${trimming.trimStart.toFixed(1)}s, end=${trimming.trimEnd.toFixed(1)}s), not yet committed`);
    // trimPreviewSrcは消さない(2026-07-17ユーザー指摘: Setしても結果が反映されない=
    // トリム前の元動画に戻って見えていた)。Segment表示側で「このセグメントの直近preview」
    // として引き続き表示する(trimPreviewForNumで対象segを紐付け)。
    setTrimPreviewForNum(trimming.num);
    setTrimming(null);
    setFinalPreviewPath(null);
  };

  /** トリムの実プレビューは中央プレビューエリアで再生する(2026-07-17ユーザー指摘:
   * 右のInspectorではなく中央に出すべき)。ボタン自体はTrimInspector(右)にあるが、
   * 表示先が別のため、fetch結果をここ(App)で保持して中央へ渡す。 */
  const previewTrim = async () => {
    if (!trimming) return;
    const seg = editSegs.find((s) => s.num === trimming.num);
    if (!seg?.path) return;
    setTrimPreviewing(true);
    try {
      const r = await api<{ out: string }>("/api/edit/trim-preview", {
        json: { path: seg.path, trimStart: trimming.trimStart, trimEnd: trimming.trimEnd },
      });
      setTrimPreviewSrc(r.out);
      setTrimPreviewV(Date.now());
    } catch (e) {
      appendLog(`[studio] Error previewing trim: ${(e as Error).message}`);
    } finally {
      setTrimPreviewing(false);
    }
  };

  // 別のセグメントをトリムし始めたら古いpreviewを捨てる(Setで閉じた時=trimming→nullは
  // 保持したいので対象外、2026-07-17ユーザー指摘: Setしても結果が反映されなかった)
  useEffect(() => {
    if (trimming && trimming.num !== trimPreviewForNum) {
      setTrimPreviewSrc(null);
      setTrimPreviewForNum(null);
    }
  }, [trimming, trimPreviewForNum]);

  const startGeneration = async (promptPath: string, engine: Engine, orientation: "--h" | "--v", direct?: number) => {
    try {
      const r = await api<{ jobId: string }>("/api/generate", { json: { promptPath, orientation, engine, ...(direct ? { direct } : {}) } });
      // runIdはまだ不明(jobが確定させる)。stateにruIdが乗ってきたらselectionを合わせる
      setActiveJob({ jobId: r.jobId, engine, runId: "" });
      appendLog(`[studio] Generate started — job ${r.jobId}`);
    } catch (e) {
      appendLog(`[studio] Error starting generate: ${(e as Error).message}`);
    }
  };

  // ジョブのstate.runIdが確定したらselectionをそのrunへ合わせる(初回のみ)
  useEffect(() => {
    if (!activeJob || activeJob.runId) return;
    const runId = jobHook.state.runId;
    if (runId) {
      setActiveJob({ ...activeJob, runId });
      // セグメント1を選択状態にしておく(2026-07-17ユーザー指摘: 生成中は選択しない限り
      // previewが出ないため、進捗を目視できるようデフォルトで1個目を選んでおく)。
      setSelection({ kind: "run", engine: activeJob.engine, runId, segmentNums: [1], view: "segment" });
      setDetail(null);
      setEditSegs([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobHook.state.runId, activeJob]);

  const stopGeneration = async () => {
    if (!activeJob) return;
    try {
      const r = await api<{ stopped: boolean }>(`/api/jobs/${activeJob.jobId}/stop`, { json: {} });
      appendLog(`[studio] Stop clicked — stopped=${r.stopped}`);
    } catch (e) {
      appendLog(`[studio] Error stopping job: ${(e as Error).message}`);
    }
  };

  const retry = async (nums: number[], opts: { keep: boolean; norefine: boolean; editPrompt?: string; editKfPrompt?: string }) => {
    if (selection.kind !== "run") return;
    try {
      const r = await api<{ jobId: string; warning: string | null }>("/api/retry", {
        json: {
          engine: selection.engine, runId: selection.runId, segs: nums, keep: opts.keep, norefine: opts.norefine,
          editPrompt: opts.editPrompt ?? "", editKfPrompt: opts.editKfPrompt ?? "",
        },
      });
      if (r.warning) appendLog(`[studio] ⚠️ ${r.warning}`);
      setActiveJob({ jobId: r.jobId, engine: selection.engine, runId: selection.runId });
      appendLog(`[studio] Retry started — job ${r.jobId}, segs ${nums.join(",")}`);
    } catch (e) {
      appendLog(`[studio] Error starting retry: ${(e as Error).message}`);
    }
  };

  const editPayload = () => editSegs.map((s) => ({ path: s.path, trimStart: s.trimStart, trimEnd: s.trimEnd, removed: s.removed }));

  const previewFinal = async () => {
    setEditPreviewing(true);
    try {
      const r = await api<{ out: string }>("/api/edit/preview", { json: { segments: editPayload() } });
      setFinalPreviewPath(r.out);
      appendLog(`[studio] Preview edit — ${r.out}`);
    } catch (e) {
      appendLog(`[studio] Error previewing edit: ${(e as Error).message}`);
    } finally {
      setEditPreviewing(false);
    }
  };
  /** CASS/Upscale完了時にdetailを更新する(finals/cass.videosの一覧を再スキャン)。
   * 2026-07-17ユーザー指摘: CASSした動画がUpscaleのファイル一覧に出ないバグ — onCassResult/
   * onUpscaleResultがdetailを再取得していなかったため、finalOptions(detail)が古いままだった。 */
  const refreshDetail = async () => {
    if (selection.kind !== "run") return;
    try {
      const d = await api<LibraryRunDetail>(`/api/library/runs/${selection.engine}/${selection.runId}`);
      setDetail(d);
    } catch (e) {
      appendLog(`[studio] Error refreshing run detail: ${(e as Error).message}`);
    }
  };

  const commitFinal = async () => {
    if (selection.kind !== "run") return;
    setEditCommitting(true);
    try {
      const r = await api<{ out: string }>("/api/edit/commit", { json: { engine: selection.engine, runId: selection.runId, segments: editPayload() } });
      appendLog(`[studio] Commit to final.mp4 — ${r.out}`);
      const d = await api<LibraryRunDetail>(`/api/library/runs/${selection.engine}/${selection.runId}`);
      setDetail(d);
      // editSegs(並び順・removed・trim)はcommit後もそのまま保持する(2026-07-17ユーザー訂正:
      // commitは「今の編集結果をfinal.mp4へ焼き込む」だけの操作で、staging状態のライフサイクル
      // には影響しない。sidecar(/api/edit/stage)側もcommitで削除しなくなったため、表示との
      // 整合を保つにはここでリセットしないのが正しい)。
      setFinalPreviewPath(null);
    } catch (e) {
      appendLog(`[studio] Error committing edit: ${(e as Error).message}`);
    } finally {
      setEditCommitting(false);
    }
  };

  return (
    <div className="studio">
      <header className="studio-header">
        <h1><span>LTX</span> Timeline Studio</h1>
      </header>

      <div className="studio-body">
        {/* ---------- 左: Browser ---------- */}
        <div className="pane-browser">
          <div className="pane-browser-new">
            <button className="primary" onClick={() => { setSelection({ kind: "new" }); setNewSessionKey((k) => k + 1); }}>
              <Icon name="plus" size={14} /> New
            </button>
          </div>
          <div className="pane-browser-filters">
            <div className="chip-row">
              {PERIODS.map((p) => (
                <button key={p} className={period === p ? "active" : ""} onClick={() => setPeriod(p)}>{PERIOD_LABELS[p]}</button>
              ))}
            </div>
            <div className="chip-row">
              {ENGINE_FILTERS.map((e) => (
                <button key={e} className={engineFilter === e ? "active" : ""} onClick={() => setEngineFilter(e)}>{e}</button>
              ))}
            </div>
            <input
              type="text" placeholder="search run_id / source…" value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
            />
          </div>
          <div className="pane-browser-list">
            {runsTruncated && (
              <div style={{ color: "var(--warn)", fontSize: 11, padding: "6px" }}>
                Over {libraryRuns.length} runs ({runsTotal} total) — narrow the filter
              </div>
            )}
            {!libraryRuns.length && (
              <div style={{ color: "var(--text-dim)", fontSize: 12, padding: "12px 6px" }}>
                (no runs match this filter)
              </div>
            )}
            {runGroups.map(([groupKey, groupRuns]) => (
              <div key={groupKey}>
                <div className="pane-browser-section-label">{groupKey}</div>
                {groupRuns.map((run) => {
                  const rowKey = `${run.engine}-${run.runId}`;
                  const confirming = confirmDeleteKey === rowKey;
                  return (
                    <div
                      key={rowKey}
                      className={`run-card${selection.kind === "run" && selection.runId === run.runId ? " active" : ""}`}
                      onClick={() => !confirming && selectRun(run)}
                    >
                      {confirming ? (
                        <div className="run-card-confirm" onClick={(e) => e.stopPropagation()}>
                          <span>Delete run {run.runId}?</span>
                          <div className="row">
                            <button className="danger" onClick={() => deleteRun(run)}>
                              <Icon name="trash" size={12} /> Delete
                            </button>
                            <button className="ghost" onClick={() => setConfirmDeleteKey(null)}>Cancel</button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className="run-card-thumb">
                            <RunThumb path={run.thumbnail} mt={run.mt} />
                          </div>
                          <div className="run-card-body">
                            <div className="run-card-top">
                              <span className={`run-card-engine ${run.engine}`}>{run.engine}</span>
                              <span className="run-card-id">{run.runId}</span>
                            </div>
                            <div className="run-card-meta">
                              <span>{new Date(run.mt).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</span>
                              {activeJob?.runId === run.runId && <span className="run-card-status generating">generating…</span>}
                            </div>
                          </div>
                          <button
                            className="icon run-card-delete" title="Delete run"
                            onClick={(e) => { e.stopPropagation(); setConfirmDeleteKey(rowKey); }}
                          >
                            <Icon name="trash" size={13} />
                          </button>
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>

        {/* ---------- 中央: Timeline + Preview ---------- */}
        <div className="pane-center">
          {selection.kind === "run" && (
            <div className="preview-tabs">
              <button className={selection.view === "segment" ? "active" : ""} onClick={() => setView("segment")}>Segment</button>
              <button className={selection.view === "final" ? "active" : ""} onClick={() => setView("final")}>Final</button>
              {!!displayCassPath && (
                <button className={selection.view === "cass" ? "active" : ""} onClick={() => setView("cass")}>CASS</button>
              )}
              {!!displayUpscalePath && (
                <button className={selection.view === "upscale" ? "active" : ""} onClick={() => setView("upscale")}>Upscale</button>
              )}
            </div>
          )}
          <div className="preview-area">
            {selection.kind === "new" && (
              <div className="preview-placeholder">
                <Icon name="film" size={32} />
                <span>Create a new prompt in the Inspector on the right</span>
              </div>
            )}
            {selection.kind === "run" && detailLoading && (
              <div className="preview-placeholder"><span>Loading…</span></div>
            )}
            {selection.kind === "run" && selection.view === "segment" && selectedSegments.length === 1 && trimming?.num === selectedSegments[0].num && (
              trimPreviewSrc ? (
                <div className="final-assembly-preview" style={{ width: "100%", height: "100%" }}>
                  <div style={{ position: "relative", flex: 1, minHeight: 0, width: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
                    <video key={trimPreviewV} className="final-video" style={{ maxHeight: "100%", maxWidth: "100%", display: "block" }}
                      src={media(trimPreviewSrc, trimPreviewV)} controls autoPlay />
                    <div style={{
                      position: "absolute", bottom: 6, right: 6, fontSize: 11, color: "var(--warn)",
                      background: "rgba(16, 16, 20, 0.7)", padding: "2px 8px", borderRadius: 4,
                    }}>
                      Trim preview (not saved) — start={trimming.trimStart.toFixed(1)}s end={trimming.trimEnd.toFixed(1)}s
                    </div>
                  </div>
                </div>
              ) : (
                <div className="preview-placeholder">
                  <Icon name="scissors" size={32} />
                  {trimPreviewing ? (
                    <span><span className="spinner" /> Rendering preview…</span>
                  ) : (
                    <span>Trimming: {selectedSegments[0].label} — start={trimming.trimStart.toFixed(1)}s end={trimming.trimEnd.toFixed(1)}s (unsaved) — click Preview on the right</span>
                  )}
                </div>
              )
            )}
            {selection.kind === "run" && selection.view === "segment" && selectedSegments.length === 1 && trimming?.num !== selectedSegments[0].num && (
              trimPreviewForNum === selectedSegments[0].num && trimPreviewSrc ? (
                <div className="final-assembly-preview" style={{ width: "100%", height: "100%" }}>
                  <div style={{ position: "relative", flex: 1, minHeight: 0, width: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
                    <video key={trimPreviewV} className="final-video" style={{ maxHeight: "100%", maxWidth: "100%", display: "block" }}
                      src={media(trimPreviewSrc, trimPreviewV)} controls />
                    <div style={{
                      position: "absolute", bottom: 6, right: 6, fontSize: 11, color: "var(--warn)",
                      background: "rgba(16, 16, 20, 0.7)", padding: "2px 8px", borderRadius: 4,
                    }}>
                      Trim set (not committed) — start={selectedSegments[0].trimStart.toFixed(1)}s end={selectedSegments[0].trimEnd.toFixed(1)}s
                    </div>
                  </div>
                </div>
              ) : selectedSegments[0].path ? (
                <video key={selectedSegments[0].path} className="final-video" style={{ maxHeight: "100%", maxWidth: "100%" }}
                  src={media(selectedSegments[0].path, selectedSegments[0].mt)} controls />
              ) : (
                <div className="preview-placeholder">
                  <Icon name="play" size={32} />
                  <span>{selectedSegments[0].label} ({statusLabel(selectedSegments[0].status)})</span>
                </div>
              )
            )}
            {selection.kind === "run" && selection.view === "segment" && selectedSegments.length > 1 && (
              <div className="preview-placeholder">
                <Icon name="layers" size={32} />
                <span>{selectedSegments.length} segments selected</span>
              </div>
            )}
            {selection.kind === "run" && selection.view === "segment" && selectedSegments.length === 0 && (
              <div className="preview-placeholder">
                <Icon name="film" size={32} />
                <span>Select a segment from the timeline below</span>
              </div>
            )}
            {selection.kind === "run" && selection.view === "final" && !displayFinalPath && (
              <div className="preview-placeholder">
                <Icon name="layers" size={32} />
                <span>No final.mp4 for this run yet</span>
              </div>
            )}
            {selection.kind === "run" && selection.view === "final" && displayFinalPath && (
              <div className="final-assembly-preview" style={{ width: "100%", height: "100%" }}>
                <div style={{ position: "relative", flex: 1, minHeight: 0, width: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <video key={displayFinalPath} className="final-video" style={{ maxHeight: "100%", maxWidth: "100%", display: "block" }}
                    src={media(displayFinalPath, displayFinalMt)} controls autoPlay={!!finalPreviewPath} />
                  <button
                    className="icon" title="Download"
                    style={{ position: "absolute", top: 6, right: 6 }}
                    onClick={() => downloadFile(displayFinalPath!, displayFinalMt)}
                  >
                    <Icon name="save" size={14} />
                  </button>
                  {finalPreviewPath && (
                    <div style={{
                      position: "absolute", bottom: 6, right: 6, fontSize: 11, color: "var(--warn)",
                      background: "rgba(16, 16, 20, 0.7)", padding: "2px 8px", borderRadius: 4,
                    }}>
                      Preview — not the run's current final.mp4
                    </div>
                  )}
                </div>
                {!finalPreviewPath && (editSegs.some((s) => s.removed || s.trimStart > 0 || s.trimEnd > 0)) && (
                  <div style={{ fontSize: 11, color: "var(--text-dim)" }}>Showing the last committed final — pending edits below aren't reflected yet, click Preview</div>
                )}
              </div>
            )}
            {selection.kind === "run" && selection.view === "cass" && displayCassPath && (
              <div className="final-assembly-preview" style={{ width: "100%", height: "100%" }}>
                <div style={{ position: "relative", flex: 1, minHeight: 0, width: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <video key={displayCassPath} className="final-video" style={{ maxHeight: "100%", maxWidth: "100%", display: "block" }}
                    src={media(displayCassPath, displayCassMt)} controls autoPlay={!!cassPreviewPath} />
                  <button
                    className="icon" title="Download"
                    style={{ position: "absolute", top: 6, right: 6 }}
                    onClick={() => downloadFile(displayCassPath!, displayCassMt)}
                  >
                    <Icon name="save" size={14} />
                  </button>
                </div>
              </div>
            )}
            {selection.kind === "run" && selection.view === "upscale" && displayUpscalePath && (
              <div className="final-assembly-preview" style={{ width: "100%", height: "100%" }}>
                <div style={{ position: "relative", flex: 1, minHeight: 0, width: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <video key={displayUpscalePath} className="final-video" style={{ maxHeight: "100%", maxWidth: "100%", display: "block" }}
                    src={media(displayUpscalePath, displayUpscaleMt)} controls autoPlay={!!upscalePreviewPath} />
                  <button
                    className="icon" title="Download"
                    style={{ position: "absolute", top: 6, right: 6 }}
                    onClick={() => downloadFile(displayUpscalePath!, displayUpscaleMt)}
                  >
                    <Icon name="save" size={14} />
                  </button>
                </div>
              </div>
            )}
          </div>
          {selection.kind === "run" && (
            <div className="timeline-hint" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span>Cmd/Ctrl+click: multi-select · Shift+click: range select · drag: reorder</span>
              {activeJob?.runId === selection.runId && (
                <button className="danger icon" onClick={stopGeneration} title="Stop generation">
                  <Icon name="stop" size={12} /> Stop
                </button>
              )}
            </div>
          )}
          <div className="timeline-strip">
            {selection.kind === "run" ? (
              editSegs.length ? editSegs.map((seg, i) => (
                <div
                  key={seg.num}
                  className={`clip${selection.segmentNums.includes(seg.num) ? " active" : ""}${seg.removed ? " removed" : ""}${dragOver === i ? " drag-over" : ""}`}
                  onClick={(e) => selectSegment(seg, e)}
                  title={seg.removed ? "Excluded (click to select, restore from the Inspector on the right)" : "Drag to reorder"}
                  draggable
                  onDragStart={() => (dragIndex.current = i)}
                  onDragOver={(e) => { e.preventDefault(); setDragOver(i); }}
                  onDragLeave={() => setDragOver((d) => (d === i ? null : d))}
                  onDrop={() => {
                    if (dragIndex.current != null) reorderSegments(dragIndex.current, i);
                    dragIndex.current = null;
                    setDragOver(null);
                  }}
                  onDragEnd={() => { dragIndex.current = null; setDragOver(null); }}
                >
                  <div className="clip-thumb">
                    <div className={`clip-status-bar ${seg.status}`} />
                    {selection.engine === "i2v" ? (
                      seg.keyframePath ? (
                        <img src={media(seg.keyframePath, seg.mt)} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                      ) : (
                        <span style={{ color: "var(--text-dim)", fontSize: 10 }}>awaiting keyframe</span>
                      )
                    ) : seg.path ? (
                      <video src={media(seg.path, seg.mt)} muted style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                    ) : (
                      <Icon name="film" size={20} />
                    )}
                  </div>
                  <div className="clip-info">
                    <div className="clip-label">{seg.label}</div>
                    <div className="clip-dur">{statusLabel(seg.status)}</div>
                  </div>
                </div>
              )) : (
                <div style={{ color: "var(--text-dim)", padding: 8, fontSize: 12 }}>(no segments)</div>
              )
            ) : (
              <div style={{ color: "var(--text-dim)", padding: 8, fontSize: 12 }}>
                (select a run, or create a new one — segments will appear here after Generate)
              </div>
            )}
          </div>
        </div>

        {/* ---------- 右: Inspector ---------- */}
        <div className="pane-inspector">
          <NewInspector onGenerate={startGeneration} hidden={selection.kind !== "new"} running={!!activeJob} resetKey={newSessionKey} />
          {selection.kind === "run" && selection.view === "segment" && selectedSegments.length === 1 && trimming?.num === selectedSegments[0].num && (
            <TrimInspector
              seg={selectedSegments[0]}
              trimStart={trimming.trimStart}
              trimEnd={trimming.trimEnd}
              onChange={(trimStart, trimEnd) => setTrimming({ num: selectedSegments[0].num, trimStart, trimEnd })}
              onCommit={commitTrimLocal}
              onCancel={() => setTrimming(null)}
              previewing={trimPreviewing}
              onPreview={previewTrim}
            />
          )}
          {selection.kind === "run" && selection.view === "segment" && selectedSegments.length === 1 && trimming?.num !== selectedSegments[0].num && (
            <SegmentInspector
              key={selectedSegments[0].num}
              seg={selectedSegments[0]}
              allSegments={editSegs}
              engine={selection.engine}
              runId={selection.runId}
              refineEnabled={i2vEngine === "refine"}
              onRetry={(opts) => retry([selectedSegments[0].num], opts)}
              onToggleRemove={() => {
                setSegmentsRemoved([selectedSegments[0].num], !selectedSegments[0].removed);
                appendLog(`[studio] ${selectedSegments[0].removed ? "Restore" : "Remove"} — seg ${selectedSegments[0].num} (not yet committed)`);
              }}
              onStartTrim={() => setTrimming({
                num: selectedSegments[0].num,
                trimStart: selectedSegments[0].trimStart,
                trimEnd: selectedSegments[0].trimEnd,
              })}
              keep={retryKeep}
              setKeep={setRetryKeep}
              norefine={retryNorefine}
              setNorefine={setRetryNorefine}
            />
          )}
          {selection.kind === "run" && selection.view === "segment" && selectedSegments.length > 1 && (
            <MultiSegmentInspector
              segments={selectedSegments}
              engine={selection.engine}
              refineEnabled={i2vEngine === "refine"}
              onRetryAll={(opts) => retry(selectedSegments.map((s) => s.num), opts)}
              keep={retryKeep}
              setKeep={setRetryKeep}
              norefine={retryNorefine}
              setNorefine={setRetryNorefine}
            />
          )}
          {selection.kind === "run" && selection.view === "segment" && selectedSegments.length === 0 && (
            <div className="inspector-empty">Select a segment</div>
          )}
          {selection.kind === "run" && selection.view !== "segment" && detail && (
            <FinalInspector
              engine={selection.engine}
              runId={selection.runId}
              detail={detail}
              editSegs={editSegs}
              previewed={!!finalPreviewPath}
              onPreview={previewFinal}
              onCommit={commitFinal}
              onLog={appendLog}
              onCassResult={(path) => { setCassPreviewPath(path); setView("cass"); refreshDetail(); }}
              onUpscaleResult={(path) => { setUpscalePreviewPath(path); setView("upscale"); refreshDetail(); }}
              activeView={selection.view}
              previewing={editPreviewing}
              committing={editCommitting}
            />
          )}
        </div>
      </div>

      <details className="log-console" open={logOpen} onToggle={(e) => setLogOpen((e.target as HTMLDetailsElement).open)}>
        <summary>Log ({log.length})</summary>
        <pre ref={logPreRef}>{log.join("\n")}</pre>
      </details>
    </div>
  );
}

/** hiddenで表示切替(アンマウントしない)。生成開始でselectionがrunへ移った後、New に
 * 戻った時にprompt本文・engine/orientation/direct設定が消えないようにする(2026-07-17
 * ユーザー指摘: 一度生成→映像確認→設定だけ変えて試行錯誤、が壊れていた)。 */
function NewInspector({ onGenerate, hidden, running, resetKey }: {
  onGenerate: (promptPath: string, engine: Engine, orientation: "--h" | "--v", direct?: number) => void;
  hidden: boolean; running: boolean; resetKey: number;
}) {
  const { fontSize, changeFontSize } = usePromptFontSize();
  const [mode, setMode] = useState<"draft" | "file">("draft");
  const [keywords, setKeywords] = useState("");
  const [durationHint, setDurationHint] = useState(20);
  const [minBeat, setMinBeat] = useState(3);
  const [files, setFiles] = useState<PromptFile[]>([]);
  const [selectedFile, setSelectedFile] = useState("");
  const [filename, setFilename] = useState("");
  const [prompt, setPrompt] = useState("");
  const [summary, setSummary] = useState("");
  const [validation, setValidation] = useState<Validation | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [savedPath, setSavedPath] = useState("");
  const [savedPrompt, setSavedPrompt] = useState("");
  const [saveStatus, setSaveStatus] = useState("");
  const [engine, setEngine] = useState<Engine>("i2v");
  const [orientation, setOrientation] = useState<"h" | "v">("h");
  const [directMode, setDirectMode] = useState(false);
  const [directSeconds, setDirectSeconds] = useState(10);

  useEffect(() => {
    api<PromptFile[]>("/api/prompt-files").then(setFiles).catch(() => {});
  }, []);

  // 「+ New」を押すたび(=resetKeyが変わるたび)、次のSave用ファイル名を新規発番し、
  // 古いsavedPath/savedPromptは無効化する(2026-07-17ユーザー指摘: Save後の1回だけでなく、
  // Newを開いた時点で必ず新しいファイル名にしておくべき)。prompt本文・engine/orientation/
  // directの設定は意図的に維持する(既存の「試行錯誤」対応と両立させるため)。
  useEffect(() => {
    setFilename(newHarnessFilename());
    setSavedPath("");
    setSavedPrompt("");
  }, [resetKey]);

  // Newの内容を全部クリアするリセットボタン用
  const clearAll = () => {
    setMode("draft");
    setKeywords("");
    setDurationHint(20);
    setMinBeat(3);
    setSelectedFile("");
    setFilename(newHarnessFilename());
    setPrompt("");
    setSummary("");
    setValidation(null);
    setSavedPath("");
    setSavedPrompt("");
    setSaveStatus("");
  };

  const draft = async () => {
    if (!keywords.trim()) return;
    setDrafting(true);
    try {
      const r = await api<{ prompt: string; summary: string; validation: Validation }>("/api/draft", {
        json: { keywords, durationHint, minBeat },
      });
      setPrompt(r.prompt);
      setSummary(r.summary);
      setValidation(r.validation);
    } catch (e) {
      setSaveStatus(`Error: ${(e as Error).message}`);
    } finally {
      setDrafting(false);
    }
  };

  const loadFile = async (name: string) => {
    setSelectedFile(name);
    if (!name) return;
    try {
      const r = await api<{ name: string; path: string; text: string; validation: Validation }>(`/api/prompt-files/${encodeURIComponent(name)}`);
      setFilename(r.name);
      setPrompt(r.text);
      setSavedPath(r.path);
      setSavedPrompt(r.text);
      setValidation(r.validation);
      setSummary("");
    } catch (e) {
      setSaveStatus(`Error: ${(e as Error).message}`);
    }
  };

  const refreshSummary = async () => {
    if (!prompt.trim()) return;
    setBusy(true);
    try {
      const r = await api<{ summary: string }>("/api/summarize", { json: { text: prompt } });
      setSummary(r.summary);
      setValidation(await api<Validation>("/api/validate", { json: { text: prompt } }));
    } catch (e) {
      setSummary(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!prompt.trim()) return;
    setBusy(true);
    try {
      const r = await api<{ name: string; path: string; validation: Validation }>("/api/prompt-files", {
        json: { name: filename, text: prompt },
      });
      setSavedPath(r.path);
      setSavedPrompt(prompt);
      setValidation(r.validation);
      setSaveStatus(`Saved: prompt/${r.name}`);
      // 次のSaveで同じファイルを黙って上書きしないよう、入力欄は新しいタイムスタンプへ
      // 進めておく(実harnessのWrite Promptと同じ挙動、2026-07-17ユーザー指摘の連続作成対策)。
      setFilename(newHarnessFilename());
    } catch (e) {
      setSaveStatus(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div hidden={hidden}>
      <div className="inspector-section">
        <div className="inspector-row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>New Prompt</h3>
          <div className="inspector-row" style={{ flex: "none", gap: 2 }}>
            <button className="icon" title="Clear all" onClick={clearAll}><Icon name="trash" size={13} /></button>
            <button className="icon" title={`${fontSize}px`} onClick={() => changeFontSize(-1)}>A−</button>
            <button className="icon" title={`${fontSize}px`} onClick={() => changeFontSize(1)}>A+</button>
          </div>
        </div>
        <div style={{ marginBottom: 10 }}>
          <SegRadio options={["draft", "file"] as const} value={mode} onChange={setMode}
            labels={{ draft: "Draft", file: "File" }} />
        </div>
        {mode === "draft" ? (
          <>
            <div className="field">
              <label>Keywords / idea</label>
              <textarea rows={4} value={keywords} onChange={(e) => setKeywords(e.target.value)}
                placeholder="e.g. a woman drinking coffee at a morning cafe" style={{ fontSize }} />
            </div>
            <div className="inspector-row" style={{ marginBottom: 10 }}>
              <label className="field" style={{ flex: 1 }}>
                Duration hint (s)
                <input type="number" value={durationHint} min={5} onChange={(e) => setDurationHint(Number(e.target.value) || 0)} />
              </label>
              <label className="field" style={{ flex: 1 }}>
                Min beat (s)
                <input type="number" value={minBeat} min={1} onChange={(e) => setMinBeat(Number(e.target.value) || 0)} />
              </label>
            </div>
            <div className="inspector-row" style={{ marginBottom: 10 }}>
              <button onClick={draft} disabled={drafting || !keywords.trim()}>
                {drafting && <span className="spinner" />}<Icon name="pencil" size={14} /> Draft
              </button>
            </div>
          </>
        ) : (
          <div className="field" style={{ marginBottom: 10 }}>
            <label>Load an existing file</label>
            <select value={selectedFile} onChange={(e) => loadFile(e.target.value)}>
              <option value="">Select…</option>
              {files.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
            </select>
          </div>
        )}
        <div className="field">
          <label>Timeline prompt</label>
          <textarea className="script" rows={7} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder="00:00–00:04 She waves at the camera.&#10;00:04–00:09 She pours coffee into a mug."
            style={{ fontSize }} />
        </div>
        <div className="inspector-row" style={{ marginBottom: 8 }}>
          <button onClick={refreshSummary} disabled={busy || !prompt.trim()}>
            {busy && <span className="spinner" />}Refresh summary
          </button>
        </div>
        <div className="field">
          <label>Japanese summary</label>
          <textarea rows={2} value={summary} readOnly placeholder="Summary appears here after Draft/File/Refresh" style={{ fontSize }} />
        </div>
        {validation && (
          validation.ok
            ? <div className="status ok"><Icon name="check" size={12} /> {validation.segments} segments, {validation.totalSeconds}s</div>
            : <div className="status error">{translateParseError(validation.error ?? "")}</div>
        )}
        <div className="inspector-row" style={{ marginTop: 10 }}>
          <label className="field" style={{ flex: 1 }}>
            Filename
            <input type="text" value={filename} onChange={(e) => setFilename(e.target.value)} placeholder="harness_....txt" />
          </label>
          <button onClick={save} disabled={!prompt.trim() || busy} style={{ alignSelf: "flex-end" }}>
            <Icon name="save" size={14} /> Save
          </button>
        </div>
        {saveStatus && <div className={`status ${saveStatus.startsWith("Error") ? "error" : "ok"}`}>{saveStatus}</div>}
      </div>
      <div className="inspector-section">
        <h3>Generation settings</h3>
        <div className="inspector-row" style={{ marginBottom: 10 }}>
          <label className="field" style={{ flex: 1 }}>
            Engine
            <select value={engine} onChange={(e) => setEngine(e.target.value as Engine)}>
              <option value="i2v">i2v</option>
              <option value="t2v">t2v</option>
            </select>
          </label>
          <label className="field" style={{ flex: 1 }}>
            Orientation
            <select value={orientation} onChange={(e) => setOrientation(e.target.value as "h" | "v")}>
              <option value="h">1280×720</option>
              <option value="v">720×1280</option>
            </select>
          </label>
        </div>
        <label className={`check-chip ${directMode ? "checked" : ""}`} style={{ marginBottom: 10 }}>
          <input type="checkbox" checked={directMode} onChange={(e) => setDirectMode(e.target.checked)} />
          Direct (skip LLM)
        </label>
        {directMode && (
          <label className="field" style={{ marginBottom: 10 }}>
            Direct seconds
            <input type="number" value={directSeconds} min={1} onChange={(e) => setDirectSeconds(Number(e.target.value) || 0)} />
          </label>
        )}
        {running && (
          <div className="status" style={{ color: "var(--warn)", marginBottom: 8 }}>
            <span className="spinner" /> Preparing… (only one run at a time)
          </div>
        )}
        <div className="inspector-row">
          <button
            className="primary"
            onClick={() => onGenerate(savedPath, engine, orientation === "h" ? "--h" : "--v", directMode ? directSeconds : undefined)}
            disabled={!savedPath || prompt !== savedPrompt || running}
            title={
              running ? "A generation is already running"
                : !savedPath ? "Save the prompt first"
                : prompt !== savedPrompt ? "Text changed since last save — Save again first"
                : undefined
            }
          >
            <Icon name="play" size={14} /> Generate
          </button>
        </div>
      </div>
    </div>
  );
}

function SegmentInspector({ seg, allSegments, engine, runId, refineEnabled, onRetry, onToggleRemove, onStartTrim, keep, setKeep, norefine, setNorefine }: {
  seg: UiSegment; allSegments: UiSegment[]; engine: Engine; runId: string; refineEnabled: boolean;
  onRetry: (opts: { keep: boolean; norefine: boolean; editPrompt: string; editKfPrompt: string }) => void;
  onToggleRemove: () => void; onStartTrim: () => void;
  keep: boolean; setKeep: (v: boolean) => void; norefine: boolean; setNorefine: (v: boolean) => void;
}) {
  const { open } = useLightbox();
  const [segPrompt, setSegPrompt] = useState<SegPrompt | null>(null);
  const [promptLoading, setPromptLoading] = useState(false);
  const { fontSize, changeFontSize } = usePromptFontSize();
  // Retry前にLTX/Keyframe promptを編集できるようにする(2026-07-17ユーザー指摘: 修正できない
  // 致命的バグ)。/api/retryはsegs.length===1の時だけこの内容をprompts.txtへ書き戻してから
  // リトライする(実harnessのRetry.tsxと同じ契約)。
  const [editPrompt, setEditPrompt] = useState("");
  const [editKfPrompt, setEditKfPrompt] = useState("");

  useEffect(() => {
    setSegPrompt(null);
    setPromptLoading(true);
    api<SegPrompt>(`/api/runs/${engine}/${runId}/seg/${seg.num}/prompt`)
      .then((r) => {
        setSegPrompt(r);
        setEditPrompt(r.prompt);
        setEditKfPrompt(r.kf_prompt ?? "");
      })
      .catch(() => setSegPrompt(null))
      .finally(() => setPromptLoading(false));
  }, [engine, runId, seg.num]);

  const readySegments = allSegments.filter((s) => (engine === "i2v" ? !!s.keyframePath : !!s.path));
  const openLightbox = () => {
    const idx = readySegments.findIndex((s) => s.num === seg.num);
    if (idx < 0) return;
    const items = readySegments.map((s) => ({
      path: (engine === "i2v" ? s.keyframePath : s.path)!,
      caption: s.label,
      kind: (engine === "i2v" ? "image" : "video") as "image" | "video",
      v: s.mt,
    }));
    open(items, idx);
  };

  return (
    <>
      <div className="inspector-section">
        <h3>Segment</h3>
        <div style={{ fontSize: 12, color: "var(--text-dim)", marginBottom: 8 }}>
          {seg.label} · {statusLabel(seg.status)}{seg.removed && " · excluded"}
          {(seg.trimStart > 0 || seg.trimEnd > 0) && (
            <> · <Icon name="scissors" size={11} /> {seg.trimStart.toFixed(1)}/{seg.trimEnd.toFixed(1)}</>
          )}
        </div>
        {engine === "i2v" && (
          <div className="check-chip-row">
            <label className={`check-chip ${keep ? "checked" : ""}`} title="Reuse existing keyframe">
              <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
              --keep
            </label>
            {refineEnabled && (
              <label className={`check-chip ${norefine ? "checked" : ""}`}>
                <input type="checkbox" checked={norefine} onChange={(e) => setNorefine(e.target.checked)} />
                --norefine
              </label>
            )}
          </div>
        )}
        <div className="inspector-row">
          <button onClick={() => onRetry({ keep, norefine, editPrompt, editKfPrompt })}><Icon name="refresh" size={14} /> Retry</button>
          <button className="ghost" disabled={seg.removed || !seg.path} onClick={onStartTrim}><Icon name="scissors" size={14} /> Trim</button>
          <button className="ghost" onClick={onToggleRemove}>
            <Icon name={seg.removed ? "undo" : "trash"} size={14} /> {seg.removed ? "Restore" : "Remove"}
          </button>
        </div>
      </div>
      {engine === "i2v" && (
        <div className="inspector-section">
          <h3>Keyframe</h3>
          {seg.keyframePath ? (
            <div className="kf-preview" onClick={openLightbox} title="Click to enlarge (arrow keys for other segments)" style={{ backgroundImage: `url(${media(seg.keyframePath, seg.mt)})`, backgroundSize: "cover", backgroundPosition: "center" }} />
          ) : (
            <div className="inspector-empty" style={{ padding: "10px 0" }}>Not generated yet</div>
          )}
        </div>
      )}
      {engine === "t2v" && (
        <div className="inspector-section">
          <h3>1st frame</h3>
          {seg.path ? (
            <div className="kf-preview" onClick={openLightbox} title="Click to enlarge (arrow keys for other segments)">
              <span className="clip-kf-badge frame">1st</span>
              <span style={{ color: "var(--text-dim)", fontSize: 11 }}>(click to enlarge, opens the segment video)</span>
            </div>
          ) : (
            <div className="inspector-empty" style={{ padding: "10px 0" }}>Extracted once the video is done</div>
          )}
        </div>
      )}
      <div className="inspector-section">
        <div className="inspector-row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>LTX prompt</h3>
          <div className="inspector-row" style={{ flex: "none", gap: 2 }}>
            <button className="icon" title={`${fontSize}px`} onClick={() => changeFontSize(-1)}>A−</button>
            <button className="icon" title={`${fontSize}px`} onClick={() => changeFontSize(1)}>A+</button>
          </div>
        </div>
        {promptLoading ? (
          <div className="inspector-empty">Loading…</div>
        ) : segPrompt ? (
          <CopyableField value={editPrompt} rows={6} fontSize={fontSize} onChange={setEditPrompt} />
        ) : (
          <div className="inspector-empty">(unavailable)</div>
        )}
      </div>
      {engine === "i2v" && segPrompt?.kf_prompt != null && (
        <div className="inspector-section">
          <h3>Keyframe prompt</h3>
          <CopyableField value={editKfPrompt} rows={3} fontSize={fontSize} onChange={setEditKfPrompt} />
        </div>
      )}
    </>
  );
}

/** Trim中はプレビュー(中央)とスライダー(右)が同じセグメントを指す。ffprobeで実尺を取得し、
 * 実際のtrim-preview APIの結果は中央プレビューエリアで再生する(2026-07-17ユーザー指摘:
 * 右のInspector内ではなく中央に出すべき — previewSrc自体はApp側で保持し、ここではボタンのみ)。 */
function TrimInspector({ seg, trimStart, trimEnd, onChange, onCommit, onCancel, previewing, onPreview }: {
  seg: UiSegment; trimStart: number; trimEnd: number;
  onChange: (trimStart: number, trimEnd: number) => void; onCommit: () => void; onCancel: () => void;
  previewing: boolean; onPreview: () => void;
}) {
  const [dur, setDur] = useState<number | null>(null);

  useEffect(() => {
    if (!seg.path) return;
    api<{ duration: number }>(`/api/duration?p=${encodeURIComponent(seg.path)}`)
      .then((r) => setDur(Math.round(r.duration * 10) / 10))
      .catch(() => setDur(null));
  }, [seg.path]);

  const d = dur ?? 10;
  const keepStart = Math.min(trimStart, d);
  const keepEnd = Math.max(keepStart + 0.1, d - trimEnd);
  const kept = Math.max(0, d - trimStart - trimEnd);

  return (
    <div className="inspector-section">
      <h3>Trim — {seg.label}</h3>
      <div style={{ fontSize: 12, color: "var(--text-dim)", marginBottom: 10 }}>
        keeping {kept.toFixed(1)}s (of {d}s) · unsaved
      </div>
      <div className="trim-slider">
        <div className="track" />
        <div className="range" style={{ left: `${(keepStart / d) * 100}%`, width: `${((keepEnd - keepStart) / d) * 100}%` }} />
        <input
          type="range" min={0} max={d} step={0.1} value={keepStart}
          onChange={(e) => onChange(Math.min(Number(e.target.value), d - trimEnd - 0.1), trimEnd)}
        />
        <input
          type="range" min={0} max={d} step={0.1} value={keepEnd}
          onChange={(e) => onChange(trimStart, Math.max(0, d - Math.max(Number(e.target.value), keepStart + 0.1)))}
        />
      </div>
      <div className="inspector-row" style={{ marginTop: 10 }}>
        <button onClick={onPreview} disabled={previewing || !seg.path}>
          {previewing && <span className="spinner" />}<Icon name="play" size={14} /> Preview
        </button>
      </div>
      <div className="inspector-row" style={{ marginTop: 6 }}>
        <button className="ghost" onClick={onCancel}><Icon name="close" size={14} /> Cancel</button>
        <button className="primary" onClick={onCommit}><Icon name="save" size={14} /> Set</button>
      </div>
    </div>
  );
}

/** 複数segment選択時はRetryのみ(まとめて別seedで再生成)。Trim/Remove/Restoreは常に単一segment対象
 * (2026-07-17ユーザー指摘: Editは複数選択不要、並べ替えはタイムライン側でD&D)。 */
function MultiSegmentInspector({ segments, engine, refineEnabled, onRetryAll, keep, setKeep, norefine, setNorefine }: {
  segments: UiSegment[]; engine: Engine; refineEnabled: boolean; onRetryAll: (opts: { keep: boolean; norefine: boolean }) => void;
  keep: boolean; setKeep: (v: boolean) => void; norefine: boolean; setNorefine: (v: boolean) => void;
}) {
  return (
    <div className="inspector-section">
      <h3>{segments.length} segments selected</h3>
      <ul style={{ margin: "0 0 10px", padding: "0 0 0 18px", fontSize: 12, color: "var(--text-dim)" }}>
        {segments.map((s) => <li key={s.num}>{s.label}</li>)}
      </ul>
      {engine === "i2v" && (
        <div className="check-chip-row" style={{ marginTop: 0, marginBottom: 10 }}>
          <label className={`check-chip ${keep ? "checked" : ""}`} title="Reuse existing keyframe">
            <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
            --keep
          </label>
          {refineEnabled && (
            <label className={`check-chip ${norefine ? "checked" : ""}`}>
              <input type="checkbox" checked={norefine} onChange={(e) => setNorefine(e.target.checked)} />
              --norefine
            </label>
          )}
        </div>
      )}
      <div className="inspector-row">
        <button className="primary" onClick={() => onRetryAll({ keep, norefine })}><Icon name="refresh" size={14} /> Retry selected</button>
      </div>
    </div>
  );
}

const FINAL_TABS = ["prompt", "edit", "cass", "upscale"] as const;
type FinalTab = (typeof FINAL_TABS)[number];
const FINAL_TAB_LABELS: Record<FinalTab, string> = {
  prompt: "Prompt", edit: "Edit", cass: "CASS", upscale: "Upscale",
};

function FinalInspector({ engine, runId, detail, editSegs, previewed, onPreview, onCommit, onLog, onCassResult, onUpscaleResult, activeView, previewing, committing }: {
  engine: Engine; runId: string; detail: LibraryRunDetail; editSegs: UiSegment[];
  previewed: boolean; onPreview: () => void; onCommit: () => void; onLog: (line: string) => void;
  onCassResult: (path: string) => void; onUpscaleResult: (path: string) => void; activeView: ViewMode;
  previewing: boolean; committing: boolean;
}) {
  const [tab, setTab] = useState<FinalTab>("edit");
  const [promptTab, setPromptTab] = useState<"source" | "generated">("source");
  const removedCount = editSegs.filter((s) => s.removed).length;
  const trimmedCount = editSegs.filter((s) => s.trimStart > 0 || s.trimEnd > 0).length;

  // 中央がCASS/Upscaleへ切り替わったら、右パネルの機能タブも追従させる(2026-07-17
  // ユーザー指摘: CASS完了後、中央にCASS映像が出ても右がブランクのままだった)。
  useEffect(() => {
    if (activeView === "cass" || activeView === "upscale") setTab(activeView);
  }, [activeView]);

  return (
    <>
      <div className="subtabs" style={{ padding: "0 12px" }}>
        {FINAL_TABS.map((t) => (
          <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{FINAL_TAB_LABELS[t]}</button>
        ))}
      </div>
      {tab === "prompt" && (
        <div className="inspector-section">
          <div className="subtabs" style={{ marginBottom: 8 }}>
            <button className={promptTab === "source" ? "active" : ""} title={detail.header.source} onClick={() => setPromptTab("source")}>
              Source
            </button>
            <button className={promptTab === "generated" ? "active" : ""} title="prompts.txt" onClick={() => setPromptTab("generated")}>
              Generated
            </button>
          </div>
          <CopyableField value={(promptTab === "source" ? detail.sourceRaw : detail.promptsRaw) ?? "(unavailable)"} rows={8} />
        </div>
      )}
      {tab === "edit" && (
        <div className="inspector-section">
          <h3>Apply remove / reorder / trim</h3>
          <div style={{ fontSize: 12, color: "var(--text-dim)", marginBottom: 8 }}>
            {runId} · {editSegs.length} segments
            {removedCount > 0 && ` · ${removedCount} removed`}
            {trimmedCount > 0 && ` · ${trimmedCount} trimmed`}
          </div>
          <div className="inspector-row">
            <button onClick={onPreview} disabled={previewing || committing}>
              {previewing && <span className="spinner" />}<Icon name="play" size={14} /> Preview
            </button>
            <button className="primary" disabled={!previewed || previewing || committing} onClick={onCommit}>
              {committing && <span className="spinner" />}<Icon name="save" size={14} /> Commit
            </button>
          </div>
          {previewing && <div className="status" style={{ marginTop: 6 }}><span className="spinner" /> Rendering preview…</div>}
          {committing && <div className="status" style={{ marginTop: 6 }}><span className="spinner" /> Committing…</div>}
          {!previewed && !previewing && !committing && editSegs.some((s) => s.removed || s.trimStart > 0 || s.trimEnd > 0) && (
            <div style={{ fontSize: 11, color: "var(--warn)", marginTop: 6 }}>
              Preview before committing (Commit is disabled until previewed)
            </div>
          )}
        </div>
      )}
      {tab === "cass" && (
        <CassPanel
          options={detail.finals.map((f) => ({ path: f.path, mt: f.mt, label: shortFinalLabel(f.path.split("/").pop() ?? f.path) }))}
          onLog={onLog}
          onResult={onCassResult}
        />
      )}
      {tab === "upscale" && <UpscalePanel options={finalOptions(detail).filter((o) => !o.isFHD)} onLog={onLog} onResult={onUpscaleResult} />}
    </>
  );
}

function CassPanel({ options, onLog, onResult }: {
  options: { path: string; mt: number; label: string }[]; onLog: (line: string) => void; onResult: (path: string) => void;
}) {
  const [acestepConfigured, setAcestepConfigured] = useState(false);
  const [bgmMode, setBgmMode] = useState<"File" | "Generate">("File");
  const [bgmFiles, setBgmFiles] = useState<{ name: string; path: string }[]>([]);
  const [bgmPath, setBgmPath] = useState("");
  const [bgmPrompt, setBgmPrompt] = useState("");
  const [bgmDuration, setBgmDuration] = useState(60);
  const [drafting, setDrafting] = useState(false);
  const [bgmJobId, setBgmJobId] = useState<string | null>(null);
  const [chosenTake, setChosenTake] = useState<string | null>(null);
  const [volume, setVolume] = useState(0.6);
  const [jobId, setJobId] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const videoPath = selected || options[0]?.path || "";

  // 対象は既定でmtime最新。runを切り替えたら選択をリセットする。
  useEffect(() => setSelected(""), [options.map((o) => o.path).join(",")]);

  const bgmJob = useJob(bgmJobId);
  const cassJob = useJob(jobId);
  const takes = bgmJob.state.takes ?? [];
  const notifiedJobRef = useRef<string | null>(null);

  useEffect(() => {
    api<{ configured: boolean }>("/api/acestep-config").then((r) => setAcestepConfigured(r.configured)).catch(() => {});
    api<{ name: string; path: string }[]>("/api/bgm-files").then(setBgmFiles).catch(() => {});
  }, []);

  // 完了したらFinal(中央プレビュー)へ出す。右パネルには小さい動画を出さない(2026-07-17指摘)。
  // onResult/onLogは親の再レンダリングのたび新しい関数参照になるため、依存配列に入れたままだと
  // このeffectが無関係な再レンダリングでも再発火し、ログへ永遠に同じ行を追記し続けていた
  // (2026-07-17ユーザー指摘)。jobId単位で一度だけ通知するようrefでガードする。
  useEffect(() => {
    if (cassJob.status === "done" && cassJob.state.result && notifiedJobRef.current !== jobId) {
      notifiedJobRef.current = jobId;
      onResult(cassJob.state.result);
    }
  }, [cassJob.status, cassJob.state.result, jobId, onResult]);

  const draftBgm = async () => {
    if (!videoPath) return;
    setDrafting(true);
    try {
      const r = await api<{ prompt: string; message: string }>("/api/bgm/draft", { json: { videoPath } });
      if (r.prompt) setBgmPrompt(r.prompt);
      onLog(`[studio] BGM draft — ${r.message}`);
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
  const run = async () => {
    if (!videoPath) return;
    const bgm = bgmMode === "Generate" ? chosenTake : (bgmPath || null);
    const r = await api<{ jobId: string }>("/api/cass", { json: { videoPath, bgmPath: bgm, volume } });
    setJobId(r.jobId);
  };

  return (
    <div className="inspector-section">
      {!options.length && <div className="inspector-empty">No final video for this run yet</div>}
      {!!options.length && (
        <div className="field" style={{ marginBottom: 10 }}>
          <label>Target</label>
          <select value={videoPath} onChange={(e) => setSelected(e.target.value)}>
            {options.map((o) => <option key={o.path} value={o.path}>{o.label}</option>)}
          </select>
          <video key={videoPath} src={media(videoPath)} controls style={{ width: "100%", borderRadius: 6, maxHeight: 160, marginTop: 6 }} />
        </div>
      )}
      <div style={{ marginBottom: 10 }}>
        <SegRadio
          options={acestepConfigured ? (["File", "Generate"] as const) : (["File"] as const)}
          value={bgmMode} onChange={setBgmMode}
          labels={{ File: "File", Generate: "Generate" }}
        />
      </div>
      {bgmMode === "File" && (
        <>
          <div className="field" style={{ marginBottom: 10 }}>
            <select value={bgmPath} onChange={(e) => setBgmPath(e.target.value)}>
              <option value="">— no BGM (speech+sfx only) —</option>
              {bgmFiles.map((f) => <option key={f.path} value={f.path}>{f.name}</option>)}
            </select>
          </div>
          {bgmPath && <div style={{ marginBottom: 10 }}><LabeledWaveform label={bgmFiles.find((f) => f.path === bgmPath)?.name ?? ""} path={bgmPath} /></div>}
        </>
      )}
      {bgmMode === "Generate" && (
        <div style={{ marginBottom: 10 }}>
          <div className="field">
            <label>BGM description (English)</label>
            <textarea rows={2} value={bgmPrompt} onChange={(e) => setBgmPrompt(e.target.value)}
              placeholder="warm lo-fi piano and acoustic guitar, calm cozy mood" />
          </div>
          <div className="inspector-row" style={{ margin: "8px 0" }}>
            <button onClick={draftBgm} disabled={drafting || !videoPath}>
              {drafting && <span className="spinner" />}<Icon name="pencil" size={14} /> Draft from prompt file
            </button>
          </div>
          {drafting && <div className="status" style={{ marginBottom: 8 }}><span className="spinner" /> Drafting…</div>}
          <div className="inspector-row" style={{ marginBottom: 10 }}>
            <label className="field" style={{ flex: 1 }}>
              Duration (s)
              <input type="number" value={bgmDuration} min={5} onChange={(e) => setBgmDuration(Number(e.target.value) || 0)} />
            </label>
            <button className="primary" onClick={generateBgm} disabled={bgmJob.status === "running" || !bgmPrompt.trim()} style={{ alignSelf: "flex-end" }}>
              {bgmJob.status === "running" && <span className="spinner" />}<Icon name="music" size={14} /> Generate
            </button>
          </div>
          {bgmJob.status === "running" && <div className="status" style={{ marginBottom: 8 }}><span className="spinner" /> Generating BGM…</div>}
          {takes.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {takes.map((t, i) => (
                <div key={t} style={{ border: `1px solid ${chosenTake === t ? "var(--accent)" : "var(--border)"}`, borderRadius: 6, padding: 4 }}>
                  <LabeledWaveform label={`Take ${i + 1}`} path={t} />
                  <button onClick={() => setChosenTake(t)} style={{ width: "100%", justifyContent: "center", marginTop: 4 }}>
                    {chosenTake === t ? <><Icon name="check" size={13} /> using this take</> : `Use Take ${i + 1}`}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <label className="field" style={{ marginBottom: 10 }}>
        BGM volume: {volume.toFixed(2)}
        <input type="range" min={0} max={2} step={0.05} value={volume} onChange={(e) => setVolume(Number(e.target.value))} />
      </label>
      <div className="inspector-row">
        <button className="primary" onClick={run} disabled={cassJob.status === "running" || !videoPath}>
          {cassJob.status === "running" && <span className="spinner" />}<Icon name="music" size={14} /> Run CASS
        </button>
      </div>
      {(cassJob.state.voice || cassJob.state.sfx || cassJob.state.bgmOrig) && (
        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 4 }}>
          {cassJob.state.voice && <LabeledWaveform label="Voice" path={cassJob.state.voice} />}
          {cassJob.state.sfx && <LabeledWaveform label="SFX" path={cassJob.state.sfx} />}
          {cassJob.state.bgmOrig && <LabeledWaveform label="BGM (original)" path={cassJob.state.bgmOrig} />}
        </div>
      )}
      {/* 声/SFX/BGM分離後もBGMマージ+再ミックスが続くため、resultが出るまでスピナーを出し続ける
         (2026-07-17ユーザー指摘: ステム表示後にスピナーが止まって見えた)。 */}
      {cassJob.status === "running" && <div className="status" style={{ marginTop: 8 }}><span className="spinner" /> Mixing…</div>}
      {cassJob.status === "done" && cassJob.state.result && (
        <div className="status ok" style={{ marginTop: 10 }}><Icon name="check" size={12} /> Mixed — see the CASS preview above</div>
      )}
    </div>
  );
}

function UpscalePanel({ options, onLog, onResult }: {
  options: { path: string; mt: number; label: string }[]; onLog: (line: string) => void; onResult: (path: string) => void;
}) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const job = useJob(jobId);
  const videoPath = selected || options[0]?.path || "";
  const notifiedJobRef = useRef<string | null>(null);

  // 対象は既定でmtime最新(従来のlatestFinal相当)。runを切り替えたら選択をリセットする。
  useEffect(() => setSelected(""), [options.map((o) => o.path).join(",")]);

  const run = async () => {
    if (!videoPath) return;
    const r = await api<{ jobId: string }>("/api/upscale", { json: { videoPath } });
    setJobId(r.jobId);
  };
  // onLog/onResultは親の再レンダリングのたび新しい関数参照になるため、jobId単位で一度だけ
  // 通知する(2026-07-17ユーザー指摘: ログへ同じ行が延々と追記され続けていた)。
  useEffect(() => {
    if (job.status === "done" && job.state.result && notifiedJobRef.current !== jobId) {
      notifiedJobRef.current = jobId;
      onLog(`[studio] Upscale done — ${job.state.result}`);
      onResult(job.state.result);
    }
  }, [job.status, job.state.result, jobId, onLog, onResult]);
  return (
    <div className="inspector-section">
      {!options.length && <div className="inspector-empty">No final video for this run yet</div>}
      {!!options.length && (
        <div className="field" style={{ marginBottom: 10 }}>
          <label>Target</label>
          <select value={videoPath} onChange={(e) => setSelected(e.target.value)}>
            {options.map((o) => <option key={o.path} value={o.path}>{o.label}</option>)}
          </select>
          <video key={videoPath} src={media(videoPath)} controls style={{ width: "100%", borderRadius: 6, maxHeight: 160, marginTop: 6 }} />
        </div>
      )}
      <div className="inspector-row">
        <button onClick={run} disabled={job.status === "running" || !videoPath}>
          {job.status === "running" && <span className="spinner" />}<Icon name="layers" size={14} /> Upscale to FHD
        </button>
      </div>
      {job.status === "running" && <div className="status" style={{ marginTop: 8 }}><span className="spinner" /> Upscaling…</div>}
      {job.status === "done" && job.state.result && (
        <div className="status ok"><Icon name="check" size={12} /> Done — see the Upscale preview above</div>
      )}
    </div>
  );
}
