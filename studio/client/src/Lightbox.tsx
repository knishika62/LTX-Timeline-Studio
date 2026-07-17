import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { media } from "./api";
import { Icon } from "./Icon";

export type LightboxItem = { path: string; caption: string; kind: "image" | "video"; v?: number };

type LightboxCtx = { open: (items: LightboxItem[], index: number) => void };

const Ctx = createContext<LightboxCtx>({ open: () => {} });

export const useLightbox = () => useContext(Ctx);

/** キーフレーム画像・セグメント動画共通のライトボックス。
 * ブラウザビューポート一杯に表示し、◀▶/左右矢印キー(端でループ)・Escape/✕で閉じる。
 * Gradio版でCSSハック+JS注入で実装していたものをネイティブに再実装。 */
export function LightboxProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<LightboxItem[]>([]);
  const [index, setIndex] = useState(0);
  const [visible, setVisible] = useState(false);

  const open = useCallback((its: LightboxItem[], idx: number) => {
    if (!its.length) return;
    setItems(its);
    setIndex(idx);
    setVisible(true);
  }, []);

  const close = useCallback(() => setVisible(false), []);
  const nav = useCallback(
    (delta: number) => setIndex((i) => (items.length ? (i + delta + items.length) % items.length : 0)),
    [items.length],
  );

  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") nav(-1);
      else if (e.key === "ArrowRight") nav(1);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [visible, close, nav]);

  const item = items[index];

  return (
    <Ctx.Provider value={{ open }}>
      {children}
      {visible && item && (
        <div className="lightbox" onClick={(e) => e.target === e.currentTarget && close()}>
          {item.kind === "image" ? (
            <img src={media(item.path, item.v)} alt={item.caption} />
          ) : (
            <video key={item.path} src={media(item.path, item.v)} controls autoPlay />
          )}
          <div className="lb-caption">
            {item.caption} ({index + 1}/{items.length})
          </div>
          <button className="lb-close" onClick={close}><Icon name="close" size={18} /> Close</button>
          {items.length > 1 && (
            <>
              <button className="lb-nav lb-prev" onClick={() => nav(-1)}><Icon name="chevronLeft" size={26} /></button>
              <button className="lb-nav lb-next" onClick={() => nav(1)}><Icon name="chevronRight" size={26} /></button>
            </>
          )}
        </div>
      )}
    </Ctx.Provider>
  );
}
