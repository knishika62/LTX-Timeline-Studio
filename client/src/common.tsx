import { useEffect, useRef, useState, type CSSProperties } from "react";
import { media, type Validation } from "./api";
import { useLightbox } from "./Lightbox";
import { Icon } from "./Icon";

/** 数値入力(type="text" + inputMode="decimal")。type="number"を直接controlledで使うと、
 * 入力途中(空文字・"1."等)がNumber()で0や整数に丸められてstateへ即反映され、その値で
 * 再レンダリングされたvalueがDOMへ強制的に書き戻される際にブラウザのnumber inputウィジェット
 * 側でカーソル位置・入力継続性が壊れ、「触ると0になり以降入力できない」症状につながる
 * (2026-07-15ユーザー報告、既知のReact controlled number input問題)。
 * 表示用のテキストは自前でバッファし、フォーカス中は外部value変更で上書きしない。
 * 確定(blur)時にmin/maxへクランプして親へ反映する。 */
export function NumberField({ value, onChange, min, max, style, className }: {
  value: number; onChange: (v: number) => void;
  min?: number; max?: number; style?: CSSProperties; className?: string;
}) {
  const [text, setText] = useState(String(value));
  const focused = useRef(false);

  useEffect(() => {
    if (!focused.current) setText(String(value));
  }, [value]);

  const clamp = (n: number) => {
    let v = n;
    if (min != null) v = Math.max(min, v);
    if (max != null) v = Math.min(max, v);
    return v;
  };

  return (
    <input
      type="text" inputMode="decimal" className={className} style={style}
      value={text}
      onFocus={() => { focused.current = true; }}
      onChange={(e) => {
        const raw = e.target.value;
        setText(raw);
        // 未確定の途中入力(空・"-"・末尾の"."のみ等)はNumber()に丸めず反映しない
        if (raw === "" || raw === "-" || raw === "." || raw === "-.") return;
        const n = Number(raw);
        if (!Number.isNaN(n)) onChange(n);
      }}
      onBlur={() => {
        focused.current = false;
        const n = Number(text);
        const v = clamp(Number.isNaN(n) ? value : n);
        setText(String(v));
        if (v !== value) onChange(v);
      }}
    />
  );
}

/** 開閉式ログパネル(常時自動スクロール——Gradio版のsetIntervalハック相当をネイティブに) */
export function LogPanel({ log, running }: { log: string; running: boolean }) {
  const preRef = useRef<HTMLPreElement>(null);
  useEffect(() => {
    const el = preRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);
  if (!log) return null;
  return (
    <details className="log" open={running}>
      <summary>{running ? <><span className="spinner" />Log (running…)</> : "Log"}</summary>
      <pre ref={preRef}>{log}</pre>
    </details>
  );
}

/** path=null は「生成中」プレースホルダ(retry中、スキャンから一時的に消える間も
 * 数とセグメント番号が分かっているので枠を維持する——ユーザー要望 2026-07-15) */
export type KfDisplay = { path: string | null; seg: number; caption: string; mt?: number };
export type SegDisplay = { num: number; label: string; path: string | null; mt?: number };

/** i2vキーフレームGallery(クリックでライトボックス) */
export function KeyframeGallery({ keyframes }: { keyframes: KfDisplay[] }) {
  const lightbox = useLightbox();
  if (!keyframes.length) return null;
  const items = keyframes
    .filter((k) => k.path)
    .map((k) => ({ path: k.path!, caption: k.caption, kind: "image" as const, v: k.mt }));
  return (
    <div>
      <h3>Keyframes</h3>
      <div className="kf-gallery" style={{ marginTop: 8 }}>
        {keyframes.map((k) => (
          k.path ? (
            <div
              key={`${k.seg}-${k.path}`} className="kf-item"
              onClick={() => lightbox.open(items, items.findIndex((it) => it.path === k.path))}
            >
              <img src={media(k.path, k.mt)} alt={k.caption} loading="lazy" />
              <div className="cap">{k.caption}</div>
            </div>
          ) : (
            <div key={`${k.seg}-pending`} className="kf-item pending">
              <div className="pending-box"><span className="spinner" />生成中…</div>
              <div className="cap">{k.caption}</div>
            </div>
          )
        ))}
      </div>
    </div>
  );
}

/** セグメント別動画グリッド(キーフレームと同じ幅、🔍またはサムネイルでライトボックス拡大)。
 * selectable=true でカード自体がリトライ対象の選択UIになる(チェックボックス+枠ハイライト。
 * t2vにはキーフレームが無いため、選択はセグメント動画側で行う——ユーザー指定 2026-07-15)。 */
export function SegVideoGrid({ segments, selectable = false, selected = [], onToggle }: {
  segments: SegDisplay[];
  selectable?: boolean;
  selected?: number[];
  onToggle?: (num: number) => void;
}) {
  const lightbox = useLightbox();
  const items = segments
    .filter((s) => s.path)
    .map((s) => ({
      path: s.path!,
      caption: `seg${String(s.num).padStart(2, "0")} (${s.label})`,
      kind: "video" as const,
      v: s.mt,
    }));
  return (
    <div>
      <h3>Per-segment videos — <Icon name="search" size={13} />で拡大{selectable ? " / カードクリックでリトライ対象を選択" : ""}</h3>
      {segments.length === 0 ? (
        <div className="status">(no segment videos yet)</div>
      ) : (
        <div className="seg-grid" style={{ marginTop: 8 }}>
          {segments.map((s) => {
            const isSel = selected.includes(s.num);
            const label = `seg${String(s.num).padStart(2, "0")} ${s.label}`;
            return (
              <div
                key={`${s.num}-${s.path ?? "pending"}`}
                className={`seg-card ${selectable ? "selectable" : ""} ${isSel ? "selected" : ""}`}
                onClick={selectable ? () => onToggle?.(s.num) : undefined}
              >
                {s.path ? (
                  <video src={media(s.path, s.mt)} controls preload="metadata"
                    onClick={(e) => e.stopPropagation()} />
                ) : (
                  <div className="pending-box"><span className="spinner" />生成中…</div>
                )}
                <div className="seg-head" style={{ justifyContent: "flex-start", gap: 6, whiteSpace: "nowrap" }}>
                  {s.path && (
                    <button onClick={(e) => {
                      e.stopPropagation();
                      lightbox.open(items, items.findIndex((it) => it.path === s.path));
                    }}><Icon name="search" size={14} /></button>
                  )}
                  <span>{selectable && <span className="sel-mark">{isSel ? "☑" : "☐"} </span>}{label}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function ValidationStatus({ v }: { v: Validation | null }) {
  if (!v) return null;
  return v.ok ? (
    <div className="status ok">✅ Parse OK: {v.segments} segments / {v.totalSeconds}s total</div>
  ) : (
    <div className="status warn">⚠️ Parse error: {v.error} (the generation CLI can't read this as-is, please fix it)</div>
  );
}

/** 「Horizontal / Vertical」「i2v / t2v」等のセグメント式ラジオ */
export function SegRadio<T extends string>({
  options, value, onChange, labels,
}: { options: readonly T[]; value: T; onChange: (v: T) => void; labels?: Record<string, string> }) {
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

export function FinalVideo({ path, mtime }: { path: string | null; mtime: string | null }) {
  if (!path) return <div className="status">(final video not yet generated)</div>;
  // 同名ファイルの上書き更新でもブラウザに再取得させるため、mtimeをクエリに含める
  const src = `${media(path)}&v=${encodeURIComponent(mtime ?? "")}`;
  return (
    <div>
      <h3>Final (concatenated) video</h3>
      <video className="final-video" src={src} controls style={{ marginTop: 8 }} />
      <div className="status">Final video last updated: {mtime ? new Date(mtime).toLocaleTimeString() : "-"}</div>
    </div>
  );
}

/** 音声波形(Web Audio APIでデコードしてcanvas描画)+ネイティブ<audio>を1セットにまとめ、
 * 再生位置を示す縦線を波形上に重ねる(2026-07-15ユーザー要望)。ACE-Step 1.5は指定尺でも
 * 後半が無音のことがあるため、波形自体もパッと見で分かるように(同日追加済み)。
 * 縦線は波形の再描画とは別レイヤー(絶対配置div)で動かす——timeupdateのたびにcanvasを
 * 再描画するとデコード済み波形の再合成コストがかかるため、位置(left%)だけを更新する。 */
export function AudioWithWaveform({ src, height = 44 }: { src: string; height?: number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const playheadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      try {
        const buf = await (await fetch(src)).arrayBuffer();
        const ctx = new AudioContext();
        const audio = await ctx.decodeAudioData(buf);
        ctx.close();
        if (cancelled) return;

        const dpr = window.devicePixelRatio || 1;
        const w = canvas.clientWidth || 300;
        canvas.width = w * dpr;
        canvas.height = height * dpr;
        const g = canvas.getContext("2d")!;
        g.scale(dpr, dpr);

        const data = audio.getChannelData(0);
        const buckets = Math.max(100, Math.floor(w / 2));
        const step = Math.floor(data.length / buckets) || 1;
        g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#7c6cf0";
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
  }, [src, height]);

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
  }, [src]);

  return (
    <div className="waveform-wrap">
      <div className="waveform-canvas-wrap" style={{ height }}>
        <canvas ref={canvasRef} className="waveform" style={{ height }} />
        <div ref={playheadRef} className="waveform-playhead" style={{ height, opacity: 0 }} />
      </div>
      <audio ref={audioRef} src={src} controls />
    </div>
  );
}
