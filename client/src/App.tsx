import { useCallback, useMemo, useState } from "react";
import { LightboxProvider } from "./Lightbox";
import { SyncCtx, type SyncState } from "./sync";
import WritePrompt from "./tabs/WritePrompt";
import Generate from "./tabs/Generate";
import Retry from "./tabs/Retry";
import Edit from "./tabs/Edit";
import Cass from "./tabs/Cass";
import Upscale from "./tabs/Upscale";

const TABS = [
  { id: "write", label: "① Write Prompt" },
  { id: "generate", label: "② Generate" },
  { id: "retry", label: "③ Retry" },
  { id: "edit", label: "④ Edit" },
  { id: "cass", label: "⑤ CASS" },
  { id: "upscale", label: "⑥ Upscale" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [tab, setTab] = useState<TabId>("write");
  const [sync, setSync] = useState<SyncState>({
    engine: "i2v", runId: null, video: null, promptPath: null, promptsBump: 0, videosBump: 0, busy: {},
  });
  const update = useCallback((patch: Partial<SyncState>) => setSync((s) => ({ ...s, ...patch })), []);
  const setBusy = useCallback(
    (t: string, running: boolean) => setSync((s) => (s.busy[t] === running ? s : { ...s, busy: { ...s.busy, [t]: running } })),
    [],
  );
  const ctx = useMemo(() => ({ sync, update, setBusy }), [sync, update, setBusy]);

  return (
    <SyncCtx.Provider value={ctx}>
      <LightboxProvider>
        <div className="app">
          <header className="app-header">
            <h1><span>LTX</span> Timeline Harness</h1>
            <nav className="tabs">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  className={`tab-btn ${tab === t.id ? "active" : ""}`}
                  onClick={() => setTab(t.id)}
                >
                  {t.label}
                  {sync.busy[t.id] && <span className="badge" />}
                </button>
              ))}
            </nav>
          </header>
          <main className="app-body">
            {/* タブはアンマウントせず hidden 切替にする——実行中のSSE購読・入力中の
                編集状態をタブ移動で失わないため(ブラウザreload無しで作業を続けられる前提) */}
            <div hidden={tab !== "write"}><WritePrompt /></div>
            <div hidden={tab !== "generate"}><Generate /></div>
            <div hidden={tab !== "retry"}><Retry /></div>
            <div hidden={tab !== "edit"}><Edit /></div>
            <div hidden={tab !== "cass"}><Cass /></div>
            <div hidden={tab !== "upscale"}><Upscale /></div>
          </main>
        </div>
      </LightboxProvider>
    </SyncCtx.Provider>
  );
}
