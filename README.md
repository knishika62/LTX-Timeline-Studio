# LTX Timeline Harness (Node.js)

`../x-post/v6_harness_v2.py`(Gradio版、port 7863)の Node.js 作り直し版。
t2v/i2v V6 タイムライン生成CLIをブラウザから操作する7タブのWebアプリ(port **7864**)。

機能一覧は `../x-post/docs/v6_harness_v2.md` を踏襲し、Gradioで実現できなかったUI
(セグメントの本物のドラッグ&ドロップ並び替え・スライダー付きトリムモーダル・
ビューポート一杯のライトボックス)に加え、⑦ Library(過去の全run閲覧・削除)を追加している。

## 起動

```bash
npm install       # 初回のみ(client側も自動でinstallされる)
npm run build     # フロントエンドのビルド(client/src変更時に再実行)
npm start         # → http://localhost:7864
```

開発時は `npm run dev`(サーバー自動再起動)+ `npm --prefix client run dev`(Vite HMR、port 5173)。

## 構成

| パス | 実体 | 説明 |
|---|---|---|
| `t2v_timeline_cliV6.py` / `i2v_timeline_cliV6.py` / `timeline_common.py` / `comfyui_client.py` / `pipeline_config.py` / `prompt_generator.py` / `bgm_generate_cli.py` | 実ファイル | 生成ロジック本体。旧x-postからのシンボリックリンクは廃止済み(2026-07-15)、このリポジトリが正典 |
| `workflows/` `CASS/` | 実体コピー(x-post由来) | ComfyUIワークフロー・音声分離ツール一式 |
| `prompt/` `generated/` `uploads/` `CASS/{output,bgm,input,tmp}/` | 実ディレクトリ | 作業・入出力はすべてこのフォルダ直下 |
| `.env` | 実ファイル | ハーネス・CLI共通で必要なキーを保持。編集は即時反映(再起動不要) |
| `bridge.py` | 実ファイル | パース・連結・アップスケール・BGM生成をPython実装のまま呼ぶJSONシム(ハーネス用) |
| `server/` | 実ファイル | Express(ESM)。SSEでログ・生成状況をライブ配信 |
| `client/` | 実ファイル | Vite + React + TS のSPA(ダークテーマ、7タブ) |

## V6タイムラインCLIを直接使う(ハーネスを介さない)

`t2v_timeline_cliV6.py` / `i2v_timeline_cliV6.py` は、ブラウザのハーネス無しでターミナルから直接実行できる。
外部プロンプトファイルのタイムライン区間をセグメント単位でLLMがプロンプト展開 → ComfyUIで動画生成 → ffmpegで連結する。
実行は `x-post` conda env(`conda run -n x-post python ...`)、cwdはこのフォルダ(`LTX-timeline/`)。

- **t2v**(`t2v_timeline_cliV6.py`): キーフレームを経由せず、LTX-2.3のT2Vモードにテキストプロンプトだけで直接動画生成。出力prefixは`t2v6_`
- **i2v**(`i2v_timeline_cliV6.py`): 各セグメントの1stフレームをZ-Image/Krea2(`image.json`、`.env`の`KEYFRAME_WORKFLOW_JSON`で差し替え可)で先に生成し、LTX-2.3のI2Vで動かす方式。出力prefixは`i2v6_`(キーフレームは`..._seg{NN}_kf.png`として残る)

```bash
conda run -n x-post python t2v_timeline_cliV6.py --h --f prompt/example.txt          # 横 1280×720
conda run -n x-post python t2v_timeline_cliV6.py --v --f prompt/example.txt          # 縦 720×1280
conda run -n x-post python t2v_timeline_cliV6.py --h --f prompt/example.txt --debug  # プロンプト確認のみ

conda run -n x-post python i2v_timeline_cliV6.py --h --f prompt/example.txt          # 横 1280×720
conda run -n x-post python i2v_timeline_cliV6.py --h --f prompt/example.txt --debug  # プロンプト確認のみ
```

### オプション

| オプション | 値 | 備考 |
|---|---|---|
| `--h` | フラグ | 横向き 1280×720(16:9)。LLMへの構図指示も横構図に |
| `--v` | フラグ | 縦向き 720×1280(9:16)。LLMへの構図指示も縦構図に |
| `--f` | `FILE.txt` | プロンプトファイルのパス(`prompt/`配下推奨)。通常実行時必須 |
| `--debug` | フラグ | LLMパスのプロンプト生成のみ実行。ComfyUI/ffmpegをスキップ。反復改善に使う |
| `--retry` | `RUN_ID` | 既存runの指定セグメントだけ別seedで再生成→final再連結(下記参照)。`--f`とは排他 |
| `--seg` | `N[,N...]` | `--retry`で再生成するセグメント番号(1始まり、カンマ区切り)。`--retry`省略時は直近runを自動使用 |
| `--norefine` | フラグ | (i2vのみ)`.env`の`I2V_VIDEO_ENGINE=refine`時、`LTX Likeness Anchor`をbypassして生成(顔が手/物で隠れるセグメントの破綻対策)。`--f`・`--retry --seg`どちらでも使える。`refine`以外の時は警告のうえ無視 |
| `--keep` | フラグ | (i2vのみ)`--seg`専用。既存のキーフレーム画像をそのまま使い、動画生成だけをやり直す |
| `--direct` | `SECONDS` | デバッグ用。LLMパイプラインを一切通さず`--f`のファイル内容をそのままComfyUIに渡してSECONDS秒の動画を1本生成(下記参照) |
| `--upscale` | `[RUN_ID]` | 既存runの最終動画(`_final.mp4`)をRTX Video Super ResolutionでフルHD化(下記参照)。他の引数とは排他 |

### プロンプトファイル構造

```
[グローバル説明 — キャラクター/ロケーション/スタイル/カメラ等]

[タイムライン区間 × N]

Ambience: [アンビエンス/環境音の説明]
```

- `Timeline:` ヘッダーは省略可。最初のタイムスタンプ行より前のテキスト全体がグローバル説明になる
- `Audio:` でも `Ambience:` でも可。内容が同一行でも次行でも対応
- **タイムライン区間が無い1本の物語文でも可**(Pass -1が自動でビート分割)。各ビートは3秒以上、合計尺に上限は設けない

### タイムスタンプ形式(5種対応・混在可)

| 形式 | 例 |
|---|---|
| A — 秒・1行 | `0–2s: She walks down the alley` |
| B — MM:SS・1行(矢印/コロンは省略可) | `00:00–00:03 She waves at the camera` / `00:00–00:03 → She waves at the camera` |
| C — MM:SS単独行 + 次行に説明 | `00:00–00:02`(改行)`She sits on a step tying laces.` |
| D — ブラケット・1行 | `[0:03–0:06] She crouches to feed a cat` |
| E — **bold**タイムスタンプ + 次行 | `**00:00–00:02**`(改行)`She sits on a step tying laces.` |

### 出力

| ファイル | 内容 |
|---|---|
| `generated/{prefix}_YYYYMMDD_HHMMSS_seg{N}_{label}.mp4` | セグメント動画(`prefix`は`t2v6`/`i2v6`) |
| `generated/{prefix}_YYYYMMDD_HHMMSS_final.mp4` | ffmpeg連結済み最終動画。`.env`の`FADE_OUT_ENABLED`(デフォルトON)で末尾1秒フェードアウト付き |
| `generated/{prefix}_YYYYMMDD_HHMMSS_prompts.txt` | セグメント別LLM生成プロンプト記録 |
| `generated/i2v6_YYYYMMDD_HHMMSS_seg{NN}_kf.png` | (i2vのみ)キーフレーム画像 |

### セグメント単位リトライ(`--retry`)

`prompts.txt` に保存済みのプロンプトを逐語再利用して対象セグメントだけ別seedで再生成し、finalを再連結する(LLMパスは走らない)。i2vはキーフレーム画像から作り直す。

```bash
conda run -n x-post python t2v_timeline_cliV6.py --retry 20260704_080510 --seg 3,7
conda run -n x-post python t2v_timeline_cliV6.py --seg 3,7   # --retry省略で直近のrun
conda run -n x-post python i2v_timeline_cliV6.py --seg 3 --norefine        # refineエンジンの顔破綻対策
conda run -n x-post python i2v_timeline_cliV6.py --seg 3 --norefine --keep # キーフレームは既存のまま動画だけやり直す
```

旧takeは `..._old1.mp4` 等に退避され、比較試聴できる(concat対象外、Libraryタブでも「old」ラベル付きで確認可能)。

### 生テキストを直接ComfyUIに渡す(`--direct`、デバッグ用)

Pass0〜Pass4のLLMパイプラインを一切通さず、`--f`のファイル内容をそのまま1本のプロンプトとしてComfyUIへ渡し、指定秒数の動画を1本だけ生成する。「LLM加工が結果にどう影響しているか切り分けたい」「特定の文言がそのままどう出るか素で確認したい」というデバッグ用途。

```bash
conda run -n x-post python t2v_timeline_cliV6.py --direct 5 --h --f prompt/raw.txt
conda run -n x-post python i2v_timeline_cliV6.py --direct 4 --v --f prompt/raw.txt
```

- `--f`のファイルは**タイムライン形式である必要はない**(`_parse_prompt`を経由しないため)。ファイル全文がそのままプロンプトになる
- i2vのみ: ファイルに`--- Keyframe prompt ---`区切りがあればKeyframe用/Motion用に分割して使う。無ければ全文をKeyframe・Motion両方に使う
- `--retry` / `--seg` / `--upscale` / `--debug` とは同時指定できない(`parser.error`)。`--keep`は新規生成のため無効(指定時は警告のうえ無視)。`--norefine`(i2vのみ)は独立した軸のため通常通り使える
- 出力は通常runと同じ命名規則(`{prefix}_{run_id}_seg01_direct.mp4` / `_final.mp4` / `_prompts.txt`、`prompts.txt`に`direct: true`ヘッダー)で書かれるため、Generate/Retry/Edit/Libraryタブは無改修でdirectモードのrunを表示・操作できる
- **リトライも可能**: directモードのrunは常にセグメント1つのみのため、`--retry RUN_ID`だけで`--seg`を省略できる(自動的に`seg 1`扱い)。i2vなら`--keep`でキーフレームを再利用したまま動画だけやり直せる

```bash
conda run -n x-post python t2v_timeline_cliV6.py --retry 20260715_120000        # --seg省略可(directモードのrunのみ)
conda run -n x-post python i2v_timeline_cliV6.py --retry 20260715_120500 --keep # キーフレームは既存のまま動画だけやり直す
```

### 動画アップスケール(`--upscale`)

既存runの最終動画をRTX Video Super Resolution(`workflows/rtx_video_upscale.json`)でフルHD相当にアップスケール。

```bash
conda run -n x-post python t2v_timeline_cliV6.py --upscale                    # 直近runの_final.mp4
conda run -n x-post python t2v_timeline_cliV6.py --upscale 20260708_204212     # run_id指定
conda run -n x-post python i2v_timeline_cliV6.py --upscale
```

出力は`{run_id}_final_FHD.mp4`(元ファイルは残る)。

### .env 設定(i2vのキーフレーム関連)

| 変数 | 意味 |
|---|---|
| `KEYFRAME_LORA_NAME` / `KEYFRAME_LORA_STRENGTH` | `image.json` node76の`lora_01`/`strength_01`を上書き。名前が空=そのまま、強度`-1`=そのまま、`0`=キャラLoRA無効(任意キャラ用) |
| `KEYFRAME_WORKFLOW_JSON` | キーフレーム生成に使うComfyUIワークフローJSON(`workflows/`配下)。デフォルト`image.json` |
| `KEYFRAME_SIZE_SCALE` | キーフレーム生成解像度の倍率(動画のwidth/heightに対して、アスペクト比維持)。デフォルト`1.0`。Krea2は`1.2`推奨 |
| `FADE_OUT_ENABLED` | 最終連結動画の末尾フェードアウト(映像・音声とも1秒)。デフォルト`true` |
| `I2V_VIDEO_ENGINE` | `default` / `10e` / `refine`。i2vの動画生成エンジン切替(テスト用) |

### LLMアーキテクチャの概要

Pass0(Creative Director)→Pass1(Shot Director)→Pass1.5(Variety Auditor)→Pass2(Scene Writer)→Pass3(t2v: LTX Formatter / i2v: Keyframe+Motion Formatter)という構成。各Passの後にPython決定論チェックが入り、違反があれば専用fixerが最小修正する。開発経緯・各バグ調査の詳細は `CLAUDE.md` 参照。

## 実装メモ(ハマりどころ)

- **生成CLIは `python -m モジュール名` で起動する**(`server/proc.js` の `condaPythonModuleArgs`)。
  Python 3.11+ はメインスクリプトのパスをrealpath解決するため、`python script.py` で直接実行すると
  意図しないパス解決になるケースがある(実機で確認済み)。`-m` ならcwd基準で解決される
- **.env はサーバーの process.env に読み込まない**(`server/config.js` の `readEnv()` が都度パース)。
  子プロセスにも .env 由来キーを注入しないため、ハーネス起動中の .env 変更が常に
  次回のsubprocessへ反映される(Gradio版で踏んだ罠の構造的解消、x-post/CLAUDE.md 参照)
- **SSEは接続のたびログ+状態を全量リプレイ**(`server/jobs.js`)。タブのバックグラウンド化で
  接続が切れてもEventSourceの自動再接続で完全復元される
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
| (無し) | ⑦ Library(全run閲覧・期間/検索/engineフィルタ・削除) |
