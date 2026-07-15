import { useEffect, useState } from "react";
import { api, type PromptFile, type Validation } from "../api";
import { NumberField, ValidationStatus } from "../common";
import { useSync } from "../sync";
import { Icon } from "../Icon";

function tsName() {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `harness_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.txt`;
}

/** New/File共通の下段(プロンプト本文・要約・保存)。
 * ファイル名欄はNew/Fileで完全に別インスタンス(誤上書き防止、Gradio版の設計を踏襲)。 */
function PromptEditor(props: {
  text: string;
  setText: (t: string) => void;
  summary: string;
  setSummary: (s: string) => void;
  validation: Validation | null;
  setValidation: (v: Validation | null) => void;
  filename: string;
  setFilename: (f: string) => void;
  onSaved: (path: string) => void;
  fontSize: number;
}) {
  const [busy, setBusy] = useState(false);
  const [saveStatus, setSaveStatus] = useState("");

  const resummarize = async () => {
    if (!props.text.trim()) return;
    setBusy(true);
    try {
      const r = await api<{ summary: string }>("/api/summarize", { json: { text: props.text } });
      props.setSummary(r.summary);
      props.setValidation(await api<Validation>("/api/validate", { json: { text: props.text } }));
    } catch (e) {
      props.setSummary(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!props.text.trim()) {
      setSaveStatus("(nothing to save)");
      return;
    }
    setBusy(true);
    try {
      const r = await api<{ name: string; path: string; validation: Validation }>("/api/prompt-files", {
        json: { name: props.filename, text: props.text },
      });
      props.setValidation(r.validation);
      setSaveStatus(`Saved: prompt/${r.name}(②Generateタブに設定済み)`);
      props.onSaved(r.path);
    } catch (e) {
      setSaveStatus(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <label className="field">
        Timeline prompt (editable)
        <textarea rows={16} value={props.text} onChange={(e) => props.setText(e.target.value)} spellCheck={false}
          style={{ fontSize: props.fontSize }} />
      </label>
      <label className="field">
        Japanese summary
        <textarea rows={4} value={props.summary} readOnly style={{ fontSize: props.fontSize }} />
      </label>
      <div className="row">
        <button onClick={resummarize} disabled={busy || !props.text.trim()}>Refresh summary (from current text)</button>
        <ValidationStatus v={props.validation} />
      </div>
      <div className="row">
        <input type="text" className="grow" value={props.filename} onChange={(e) => props.setFilename(e.target.value)} placeholder="filename.txt" />
        <button className="primary" onClick={save} disabled={busy}>Save to prompt/</button>
      </div>
      {saveStatus && <div className="status">{saveStatus}</div>}
    </>
  );
}

export default function WritePrompt() {
  const { sync, update, setBusy } = useSync();
  const [sub, setSub] = useState<"new" | "file">("new");
  // 編集エリアの文字サイズ(ユーザー要望 2026-07-15)。localStorageに永続化
  const [fontSize, setFontSize] = useState(() => Number(localStorage.getItem("promptFontSize")) || 14);
  const changeFontSize = (d: number) => {
    const v = Math.min(24, Math.max(10, fontSize + d));
    setFontSize(v);
    localStorage.setItem("promptFontSize", String(v));
  };
  const onSaved = (path: string) => update({ promptPath: path, promptsBump: sync.promptsBump + 1 });
  // 保存後、Newタブのファイル名を新しいタイムスタンプへ更新する(続けて次のプロンプトを
  // 作った時に、直前のファイルを意図せず上書きしないため——ユーザー報告 2026-07-15)
  const onSavedNew = (path: string) => {
    onSaved(path);
    setNewFilename(tsName());
  };

  // --- New ---
  const [keywords, setKeywords] = useState("");
  const [duration, setDuration] = useState(20);
  const [minBeat, setMinBeat] = useState(3);
  const [drafting, setDrafting] = useState(false);
  const [newText, setNewText] = useState("");
  const [newSummary, setNewSummary] = useState("");
  const [newValidation, setNewValidation] = useState<Validation | null>(null);
  const [newFilename, setNewFilename] = useState(tsName());
  const [draftError, setDraftError] = useState("");

  // --- File ---
  const [files, setFiles] = useState<PromptFile[]>([]);
  const [selected, setSelected] = useState("");
  const [fileText, setFileText] = useState("");
  const [fileSummary, setFileSummary] = useState("");
  const [fileValidation, setFileValidation] = useState<Validation | null>(null);
  const [fileFilename, setFileFilename] = useState("");

  const refreshFiles = async () => {
    const list = await api<PromptFile[]>("/api/prompt-files");
    setFiles(list);
    if (!selected && list.length) setSelected(list[0].name);
  };
  useEffect(() => {
    refreshFiles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sync.promptsBump]);

  const draft = async () => {
    if (!keywords.trim()) return;
    setDrafting(true);
    setBusy("write", true);
    setDraftError("");
    try {
      const r = await api<{ prompt: string; summary: string; validation: Validation }>("/api/draft", {
        json: { keywords, durationHint: duration, minBeat },
      });
      setNewText(r.prompt);
      setNewSummary(r.summary);
      setNewValidation(r.validation);
      setNewFilename(tsName());
    } catch (e) {
      setDraftError(`Error: ${(e as Error).message}`);
    } finally {
      setDrafting(false);
      setBusy("write", false);
    }
  };

  const loadFile = async () => {
    if (!selected) return;
    const r = await api<{ name: string; text: string; validation: Validation }>(`/api/prompt-files/${encodeURIComponent(selected)}`);
    setFileText(r.text);
    setFileSummary("");
    setFileValidation(r.validation);
    setFileFilename(r.name);
  };

  return (
    <div className="tab-page">
      <div className="panel">
        <h2>① Write Prompt</h2>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="subtabs">
            <button className={sub === "new" ? "active" : ""} onClick={() => setSub("new")}>New</button>
            <button className={sub === "file" ? "active" : ""} onClick={() => setSub("file")}>File</button>
          </div>
          <div className="row" title="編集エリアの文字サイズ">
            <button className="icon" onClick={() => changeFontSize(-1)}>A−</button>
            <span className="status">{fontSize}px</span>
            <button className="icon" onClick={() => changeFontSize(1)}>A+</button>
          </div>
        </div>

        {sub === "new" && (
          <>
            <label className="field">
              Keywords / idea
              <textarea
                rows={2}
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
                placeholder="e.g. walking home from a summer festival, playing with a cat, yukata"
              />
            </label>
            <div className="row">
              <label className="field">
                Target duration (s)
                <NumberField value={duration} min={5} onChange={setDuration} style={{ width: 120 }} />
              </label>
              <label className="field">
                Min beat duration (s)
                <NumberField value={minBeat} min={1} onChange={setMinBeat} style={{ width: 120 }} />
              </label>
              <button className="primary" onClick={draft} disabled={drafting || !keywords.trim()} style={{ alignSelf: "flex-end" }}>
                {drafting && <span className="spinner" />}Draft
              </button>
            </div>
            {draftError && <div className="status error">{draftError}</div>}
            <PromptEditor
              text={newText} setText={setNewText}
              summary={newSummary} setSummary={setNewSummary}
              validation={newValidation} setValidation={setNewValidation}
              filename={newFilename} setFilename={setNewFilename}
              onSaved={onSavedNew} fontSize={fontSize}
            />
          </>
        )}

        {sub === "file" && (
          <>
            <div className="row">
              <select className="grow" value={selected} onChange={(e) => setSelected(e.target.value)}>
                <option value="">— select a file in prompt/ —</option>
                {files.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
              </select>
              <button className="icon" onClick={refreshFiles} title="Refresh list"><Icon name="refresh" size={14} /></button>
              <button onClick={loadFile} disabled={!selected}>Load</button>
            </div>
            <PromptEditor
              text={fileText} setText={setFileText}
              summary={fileSummary} setSummary={setFileSummary}
              validation={fileValidation} setValidation={setFileValidation}
              filename={fileFilename} setFilename={setFileFilename}
              onSaved={onSaved} fontSize={fontSize}
            />
          </>
        )}
      </div>
    </div>
  );
}
