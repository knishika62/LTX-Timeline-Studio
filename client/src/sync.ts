import { createContext, useContext } from "react";
import type { Engine } from "./api";

/** タブ跨ぎの自動同期(生成完了→Retry/Edit/CASS/Upscaleの対象が追従する)。
 * Gradio版では dropdown の choices/value 整合で多発したバグ領域を、
 * SPAのクライアント状態ひとつに集約して構造的に解消する。 */
export type SyncState = {
  engine: Engine;
  runId: string | null;
  video: string | null; // CASS/Upscaleタブのデフォルト対象動画
  promptPath: string | null; // Write Promptで保存した直後のファイル(Generateタブへ自動設定)
  promptsBump: number; // prompt/ 一覧の再取得トリガー(保存時にインクリメント)
  videosBump: number; // run一覧・動画一覧の再取得トリガー(生成・commit・CASS・upscale完了時)
  busy: Record<string, boolean>; // タブごとの実行中フラグ(ヘッダーの点滅LED表示用)
};

export type SyncCtxType = {
  sync: SyncState;
  update: (patch: Partial<SyncState>) => void;
  setBusy: (tab: string, running: boolean) => void;
};

export const SyncCtx = createContext<SyncCtxType>({
  sync: { engine: "i2v", runId: null, video: null, promptPath: null, promptsBump: 0, videosBump: 0, busy: {} },
  update: () => {},
  setBusy: () => {},
});

export const useSync = () => useContext(SyncCtx);
