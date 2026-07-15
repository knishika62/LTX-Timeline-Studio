# LTX Timeline Harness (Node.js)

`../x-post/v6_harness_v2.py`(Gradio版、port 7863)の Node.js 作り直し版。
t2v/i2v V6 タイムライン生成CLIをブラウザから操作する6タブのWebアプリ(port **7864**)。

機能一覧は `../x-post/docs/v6_harness_v2.md` を踏襲し、Gradioで実現できなかったUI
(セグメントの本物のドラッグ&ドロップ並び替え・スライダー付きトリムモーダル・
ビューポート一杯のライトボックス)を追加している。

## 起動

```bash
./setup.sh        # 初回のみ(シンボリックリンク・CASS/workflowsコピー・.env生成。冪等)
npm install       # 初回のみ(client側も自動でinstallされる)
npm run build     # フロントエンドのビルド(client/src変更時に再実行)
npm start         # → http://localhost:7864
```

開発時は `npm run dev`(サーバー自動再起動)+ `npm --prefix client run dev`(Vite HMR、port 5173)。

## 構成

| パス | 実体 | 説明 |
|---|---|---|
| `*.py`(CLI 7本) | **../x-post へのシンボリックリンク** | 生成ロジックは x-post 側で一元管理。コード変更はx-post側で行う |
| `workflows/` `CASS/` | コピー | `setup.sh` 再実行で x-post 側から更新反映 |
| `prompt/` `generated/` `uploads/` `CASS/{output,bgm,input,tmp}/` | 実ディレクトリ | **作業・入出力はすべてこのフォルダ直下**(x-postとは分離) |
| `.env` | 実ファイル | ハーネスに必要なキーのみ抽出(元は ../x-post/.env)。編集は即時反映(再起動不要) |
| `bridge.py` | 実ファイル | パース・連結・アップスケール・BGM生成をPython実装のまま呼ぶJSONシム |
| `server/` | 実ファイル | Express(ESM)。SSEでログ・生成状況をライブ配信 |
| `client/` | 実ファイル | Vite + React + TS のSPA(ダークテーマ、6タブ) |

## 実装メモ(ハマりどころ)

- **生成CLIは `python -m モジュール名` で起動する**(`server/proc.js` の `condaPythonModuleArgs`)。
  シンボリックリンクのスクリプトを `python script.py` で直接実行すると、Python 3.11+ が
  スクリプトパスをrealpath解決して `sys.path[0]` が x-post になり、import・`generated/` が
  全て x-post 側に解決されてしまう(実機で確認済み)。`-m` なら cwd 基準で解決される
- **.env はサーバーの process.env に読み込まない**(`server/config.js` の `readEnv()` が都度パース)。
  子プロセスにも .env 由来キーを注入しないため、ハーネス起動中の .env 変更が常に
  次回のsubprocessへ反映される(Gradio版で踏んだ罠の構造的解消、x-post/CLAUDE.md 2026-07-14 参照)
- **SSEは接続のたびログ+状態を全量リプレイ**(`server/jobs.js`)。タブのバックグラウンド化で
  接続が切れてもEventSourceの自動再接続で完全復元される(Gradio版の「Per-segment videosの
  更新が止まって見える」問題への対策)
- conda はシェル関数のため、Node からは `CONDA_EXE`(/opt/miniconda3/bin/conda)を直接spawnする

## Gradio版との対応

| Gradio版(x-post) | 本フォルダ |
|---|---|
| `v6_harness_v2.py` の各`on_*`ハンドラ | `server/index.js` のREST API |
| `_run_llm` + システムプロンプト5本 | `server/llm.js`(忠実にコピー) |
| `_scan_*` / `_list_runs` / `_find_run_id` | `server/scan.js`(JS移植) |
| `_write_segment_prompt` | `server/prompts.js`(JS移植) |
| `_parse_prompt` / `_parse_prompts_txt` / `_concat_segments` / upscale / `generate_bgm` | `bridge.py`(Python実装をそのまま呼ぶ) |
| `_trim_segment`(ffmpeg) | `server/edit.js`(同一引数) |
| Editタブ ▲▼ボタン | ドラッグ&ドロップ |
| Trim数値入力のみ | モーダル+デュアルレンジスライダー+単体プレビュー |
| ライトボックス(CSSハック) | ネイティブ実装(◀▶・矢印キー・Escape対応、Gradio版と同操作) |
