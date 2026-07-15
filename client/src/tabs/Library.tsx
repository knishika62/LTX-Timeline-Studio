import { useEffect, useMemo, useState } from "react";
import {
  api, media, type Engine, type LibraryPeriod, type LibraryRunDetail,
  type LibraryRunInfo, type LibraryRunsResponse,
} from "../api";
import { AudioWithWaveform, KeyframeGallery, SegRadio, SegVideoGrid } from "../common";
import { useLightbox } from "../Lightbox";

function fmtDate(mt: number) {
  return new Date(mt).toLocaleString();
}

/** runのmtimeから日付グループ見出しキーを算出(Today / Yesterday / YYYY-MM-DD)。
 * 一覧が長くなってもスキャンしやすいように、期間絞り込みと併用してグルーピングする。 */
function dateGroupKey(mt: number): string {
  const d = new Date(mt);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = today.getTime() - 24 * 60 * 60 * 1000;
  const dayStart = new Date(d).setHours(0, 0, 0, 0);
  if (dayStart === today.getTime()) return "Today";
  if (dayStart === yesterday) return "Yesterday";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** final動画・CASS出力動画共通のカードグリッド(seg-cardと同じ見た目、🔍でライトボックス拡大)。
 * SegVideoGrid(common.tsx)はセグメント専用の型なので、Library固有の「任意の動画+ラベル」用に
 * 同じ視覚パターンをここで軽量に再実装する。 */
function VideoCardGrid({ title, items }: { title: string; items: { path: string; label: string; mt: number }[] }) {
  const lightbox = useLightbox();
  if (!items.length) return null;
  const lbItems = items.map((it) => ({ path: it.path, caption: it.label, kind: "video" as const, v: it.mt }));
  return (
    <div>
      <h3>{title}</h3>
      <div className="seg-grid" style={{ marginTop: 8 }}>
        {items.map((it, i) => (
          <div key={it.path} className="seg-card">
            <video src={media(it.path, it.mt)} controls preload="metadata" />
            <div className="seg-head">
              <span>{it.label}</span>
              <button onClick={() => lightbox.open(lbItems, i)}>🔍</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function RunCard({ run, active, onClick }: { run: LibraryRunInfo; active: boolean; onClick: () => void }) {
  return (
    <div className={`library-run-card ${active ? "active" : ""}`} onClick={onClick}>
      {run.thumbnail ? (
        <img className="library-run-thumb" src={media(run.thumbnail, run.mt)} alt="" loading="lazy" />
      ) : (
        <div className="library-run-thumb placeholder">{run.engine}</div>
      )}
      <div className="library-run-meta">
        <div className="library-run-title">
          <span className={`engine-badge ${run.engine}`}>{run.engine}</span>
          {run.runId}
        </div>
        <div className="library-run-source">{run.source || "(no source)"}</div>
        <div className="library-run-date">{fmtDate(run.mt)}</div>
      </div>
    </div>
  );
}

const PERIODS: readonly LibraryPeriod[] = ["today", "7d", "30d", "all"] as const;
const PERIOD_LABELS: Record<LibraryPeriod, string> = { today: "Today", "7d": "7 days", "30d": "30 days", all: "All time" };
const ENGINE_FILTERS = ["all", "i2v", "t2v"] as const;
type EngineFilter = (typeof ENGINE_FILTERS)[number];

export default function Library() {
  const [runs, setRuns] = useState<LibraryRunInfo[]>([]);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [period, setPeriod] = useState<LibraryPeriod>("7d");
  const [engineFilter, setEngineFilter] = useState<EngineFilter>("all");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<{ engine: Engine; runId: string } | null>(null);
  const [detail, setDetail] = useState<LibraryRunDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [promptTab, setPromptTab] = useState<"source" | "generated">("source");

  // 検索テキストは300msデバウンスしてから実際のクエリに反映(キー入力のたびに叩かない)
  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const refresh = () => {
    const params = new URLSearchParams();
    if (period !== "all") params.set("since", period);
    if (engineFilter !== "all") params.set("engine", engineFilter);
    if (search) params.set("q", search);
    api<LibraryRunsResponse>(`/api/library/runs?${params}`).then((r) => {
      setRuns(r.runs);
      setTotal(r.total);
      setTruncated(r.truncated);
    });
  };
  useEffect(refresh, [period, engineFilter, search]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    setLoading(true);
    api<LibraryRunDetail>(`/api/library/runs/${selected.engine}/${selected.runId}`)
      .then(setDetail)
      .finally(() => setLoading(false));
  }, [selected]);

  // mtime降順のrunsを日付グループ(Today/Yesterday/YYYY-MM-DD)に振り分ける。
  // runsは既にサーバー側でmtime降順なので、出現順にグループを積むだけで並び順が保たれる。
  const groups = useMemo(() => {
    const map = new Map<string, LibraryRunInfo[]>();
    for (const r of runs) {
      const key = dateGroupKey(r.mt);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(r);
    }
    return [...map.entries()];
  }, [runs]);

  const keyframes = (detail?.keyframes ?? []).map((k) => ({
    seg: k.seg, path: k.path, mt: k.mt,
    caption: `seg${String(k.seg).padStart(2, "0")}${k.variant ? ` (${k.variant})` : ""}`,
  }));
  const segments = (detail?.segments ?? []).map((s) => ({
    num: s.num, path: s.path, mt: s.mt,
    label: s.variant ? `${s.label} (${s.variant})` : s.label,
  }));
  const finals = (detail?.finals ?? []).map((f) => ({
    path: f.path, mt: f.mt,
    label: [f.isFHD ? "FHD" : "final", f.variant ?? "current"].join(" "),
  }));
  const cassVideos = (detail?.cass.videos ?? []).map((v) => ({ path: v.path, mt: v.mt, label: v.name }));

  return (
    <div className="tab-page">
      <div className="panel">
        <h2>⑦ Library — 過去に生成した全runの閲覧</h2>

        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            <SegRadio options={PERIODS} value={period} onChange={setPeriod} labels={PERIOD_LABELS} />
            <SegRadio options={ENGINE_FILTERS} value={engineFilter} onChange={setEngineFilter}
              labels={{ all: "All", i2v: "i2v", t2v: "t2v" }} />
            <input
              type="text" placeholder="search run_id / source…" value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)} style={{ width: 200 }}
            />
          </div>
          <div className="row">
            <span className="status">{runs.length} / {total} runs</span>
            <button className="icon" title="Refresh" onClick={refresh}>🔄</button>
          </div>
        </div>
        {truncated && (
          <div className="status warn">
            ⚠️ 上限{runs.length}件を超えています(全{total}件)。絞り込みを強めてください
          </div>
        )}

        <div className="two-col">
          <div className="library-run-list">
            {groups.map(([groupKey, groupRuns]) => (
              <div key={groupKey}>
                <div className="library-date-header">{groupKey}</div>
                {groupRuns.map((r) => (
                  <RunCard
                    key={`${r.engine}-${r.runId}`}
                    run={r}
                    active={selected?.engine === r.engine && selected?.runId === r.runId}
                    onClick={() => setSelected({ engine: r.engine, runId: r.runId })}
                  />
                ))}
              </div>
            ))}
            {!runs.length && <div className="status">(no runs match this filter)</div>}
          </div>

          <div className="library-detail">
            {!selected && <div className="status">左の一覧からrunを選んでください</div>}
            {selected && loading && <div className="status">読み込み中…</div>}
            {selected && !loading && detail && (
              <>
                <h3>Prompt</h3>
                <div className="subtabs">
                  <button className={promptTab === "source" ? "active" : ""} onClick={() => setPromptTab("source")}>
                    元 ({detail.header.source || "?"})
                  </button>
                  <button className={promptTab === "generated" ? "active" : ""} onClick={() => setPromptTab("generated")}>
                    生成後 (prompts.txt)
                  </button>
                </div>
                <pre className="library-prompt-raw">
                  {promptTab === "source"
                    ? (detail.sourceRaw ?? "(元プロンプトファイルが見つかりません — 削除・リネームされた可能性があります)")
                    : (detail.promptsRaw || "(prompts.txt が見つかりません)")}
                </pre>

                <KeyframeGallery keyframes={keyframes} />
                <SegVideoGrid segments={segments} />
                <VideoCardGrid title="Final variants" items={finals} />
                <VideoCardGrid title="CASS outputs" items={cassVideos} />

                {detail.cass.stems.length > 0 && (
                  <div>
                    <h3>Separated stems (CASS)</h3>
                    <div className="audio-row" style={{ marginTop: 8 }}>
                      {detail.cass.stems.map((s) => (
                        <div key={s.path} className="audio-card">
                          <div className="cap">{s.kind} — {s.group}</div>
                          <AudioWithWaveform src={media(s.path, s.mt)} />
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
