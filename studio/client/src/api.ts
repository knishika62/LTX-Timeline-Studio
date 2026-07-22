import { useEffect, useRef, useState } from "react";

export type Engine = "i2v" | "t2v";
export type PromptFile = { name: string; path: string };
export type Validation = { ok: boolean; segments?: number; totalSeconds?: number; error?: string };
export type Keyframe = { path: string; seg: number; caption: string; mt?: number };
export type SegVideo = { num: number; label: string; path: string; mt?: number };
export type RunInfo = { runId: string; source: string; label: string };
export type ExpectedSeg = { num: number; label: string };
export type RunSnapshot = {
  runId: string | null;
  expected?: ExpectedSeg[];
  keyframes: Keyframe[];
  segments: SegVideo[];
  final: string | null;
  finalMtime: string | null;
  orientation: string | null;
};
export type VideoItem = { name: string; path: string; mt?: number };
export type SegPrompt = { num: number; label: string; duration: number; prompt: string; kf_prompt?: string };

// ---- Library(閲覧専用) ----
export type LibraryRunInfo = {
  engine: Engine; runId: string; source: string; label: string; mt: number; thumbnail: string | null;
};
export type LibraryKeyframe = { seg: number; path: string; mt: number; variant: string | null };
export type LibrarySegVideo = { num: number; label: string; path: string; mt: number; variant: string | null };
export type LibraryFinal = { path: string; mt: number; isFHD: boolean; variant: string | null };
export type LibraryCassVideo = { path: string; name: string; mt: number };
export type LibraryCassStem = { path: string; kind: string; group: string; mt: number };
export type LibraryRunDetail = {
  engine: Engine; runId: string;
  header: Record<string, string>;
  sourceRaw: string | null;
  promptsRaw: string;
  keyframes: LibraryKeyframe[];
  segments: LibrarySegVideo[];
  finals: LibraryFinal[];
  cass: { videos: LibraryCassVideo[]; stems: LibraryCassStem[] };
  expected: ExpectedSeg[];
};
export type LibraryPeriod = "today" | "7d" | "30d" | "all";
export type LibraryRunsResponse = { runs: LibraryRunInfo[]; truncated: boolean; total: number };

/** メディア配信URL(サーバー側でパス許可リスト検査+Range対応) */
export const media = (p: string, v?: number | string | null) =>
  `/media?p=${encodeURIComponent(p)}${v ? `&v=${encodeURIComponent(v)}` : ""}`;

export async function api<T = unknown>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const opts: RequestInit = { ...init };
  if (init?.json !== undefined) {
    opts.method = init.method ?? "POST";
    opts.headers = { "Content-Type": "application/json", ...init.headers };
    opts.body = JSON.stringify(init.json);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data as { error?: string }).error || `${res.status} ${res.statusText}`);
  return data as T;
}

export async function uploadFile(file: File): Promise<{ path: string; name: string }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: form });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "upload failed");
  return data;
}

export type JobStatus = "idle" | "running" | "done" | "error" | "stopped";

export type JobState = {
  // generation job
  engine?: Engine;
  runId?: string | null;
  keyframes?: Keyframe[];
  segments?: SegVideo[];
  expected?: ExpectedSeg[];
  final?: string | null;
  finalMtime?: string | null;
  orientation?: string | null;
  // cass job
  voice?: string;
  sfx?: string;
  bgmOrig?: string;
  result?: string;
  // bgm job
  takes?: string[];
};

/** ジョブのSSE購読。切断時はEventSourceが自動再接続し、サーバーは接続のたびに
 * ログ+状態を全量リプレイするので、タブのバックグラウンド化等で取りこぼしても
 * 必ず完全な状態に復元される(Gradio版の「更新が止まって見える」問題の対策)。 */
export function useJob(jobId: string | null) {
  const [log, setLog] = useState("");
  const [state, setState] = useState<JobState>({});
  const [status, setStatus] = useState<JobStatus>("idle");
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!jobId) {
      setStatus("idle");
      // stateを残したままだと、次にjobId(nullでない値)が確定した直後の同一コミット内で
      // 古いstate(前回jobの残骸、runId等)を読んでしまうレースが起きる(2026-07-17実機で
      // 確認: 新規Generate直後に前回runへ誤って切り替わり、以後正しいrunIdへ二度と
      // 更新されなくなる致命的バグの原因だった)。
      setState({});
      setLog("");
      return;
    }
    setLog("");
    setState({});
    setStatus("running");
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    esRef.current = es;
    es.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      if (d.type === "replay") {
        setLog(d.log);
        setState(d.state);
        if (d.status !== "running") {
          setStatus(d.status);
          es.close();
        }
      } else if (d.type === "log") {
        setLog((l) => l + d.line);
      } else if (d.type === "state") {
        setState(d.state);
      } else if (d.type === "end") {
        setStatus(d.status);
        es.close();
      }
    };
    return () => es.close();
  }, [jobId]);

  return { log, state, status };
}

/** ジョブの生ログ(文字列、useJobの.log)の増分を、行単位でonLogへ転記する。
 * \r(進捗バーの上書き)も改行とみなして区切る。jobId切り替え時は転記済み長さを
 * リセットする(useJob自体がlogを""に戻すため、こちらも0から数え直す)。 */
export function useJobLogMirror(jobLog: string, jobId: string | null, onLog: (line: string) => void) {
  const lastLen = useRef(0);
  useEffect(() => { lastLen.current = 0; }, [jobId]);
  useEffect(() => {
    if (jobLog.length > lastLen.current) {
      const added = jobLog.slice(lastLen.current);
      lastLen.current = jobLog.length;
      added.split(/\r\n|\r|\n/).map((s) => s.trim()).filter(Boolean).forEach((line) => onLog(line));
    }
  }, [jobLog, onLog]);
}
