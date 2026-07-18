# CASS

[BandIt v2](https://github.com/kwatcharasupat/bandit-v2)(Cinematic Audio Source Separation)を使って、動画・音声を **speech(セリフ) / music(音楽) / sfx(効果音)** の3ステムに分離するツール。

これでできること:

- 動画や音声ファイルから、セリフ・BGM・環境効果音(足音、波音など)をきれいに分離する
- 分離したセリフ・効果音はそのまま残しつつ、BGMだけ別の音源に差し替えた動画を作る(`process.py`、映像の末尾フェードに音量も自動同期)

Mel-Band RoFormerのような一般的な音源分離モデルは「ボーカル/伴奏」という音楽向けの2ステム構成が中心で、足音や環境音のような効果音を専用のステムとして残すことができない。BandIt v2はCinematic Audio Source Separation(映画・映像のポストプロダクション向け音源分離)という別系統のモデルで、speech/music/effectsの3ステムに対応しているため、BGMだけ差し替えてセリフと効果音を両方とも残す、という使い方ができる。

本家リポジトリの`requirements.txt`は学習用の依存関係が大量に含まれておりそのままでは重すぎる/入らないため、ここでは推論に本当に必要な部分だけを`separate.py`に直接実装し、`CASS/repo/`(本家clone)から`src/models/bandit`・`src/system/inference_handler`のロジックのみを利用している。

## 構成

| ファイル/フォルダ | 役割 |
|---|---|
| `separate.py` | 分離本体。`Bandit`モデル+チャンク推論を直接実装(hydra/ray/Trainer不使用) |
| `process.py` | 動画+BGMファイルを渡すだけで、音声抽出→分離→ミックス→動画合成まで一括実行 |
| `requirements.txt` | 推論に必要な依存のみ(本家の学習用パッケージは含まない)。**リポジトリ直下の`venv`に追加インストールする**(後述) |
| `repo/` | 本家`bandit-v2`のclone(モデル定義コードの参照元、gitは触らない) |
| `checkpoints/` | 学習済み重み([Zenodo](https://zenodo.org/records/12701995)、CC BY-SA 4.0、初回実行時に`separate.py`が自動DL。`~/.cache`等のグローバルキャッシュは使わずこのフォルダ配下に保存、git管理外) |
| `input/` | 分離したい音声・動画(m4a/wav/mp4等) |
| `output/` | 分離結果・合成済み動画 |
| `tmp/` | `process.py`が処理中に使う作業用ディレクトリ(処理ごとに一時フォルダを作成、処理終了時に自動削除。git管理外) |

## 環境

- **Pythonバージョンは不問**(3.10〜3.13で依存解決・起動を確認済み。実際に動作確認したのは3.10.20だが、`requirements.txt`の全パッケージが3.10〜3.13いずれもプリビルドwheelを持つため、手元のPythonをそのまま使ってよい)
- **専用の仮想環境は不要**——このリポジトリ直下の`venv`(本体の[README.md](../README.md)参照)にそのままインストールする。`requirements.txt`(openai/httpx/python-dotenv)とパッケージの重複・バージョン衝突が無いことを確認済み
- 動作確認機種: **M4 Max Mac(Apple Silicon, MPSバックエンド)**。CUDA機は未検証だが`separate.py`はCUDA→MPS→CPUの優先順位で自動選択するため動くはず
- `torch==2.13.0` / `torchaudio==2.11.0`という比較的新しいバージョンで動作確認済み(本家の`torch==2.0.0+cu118`という古い固定バージョンは使っていない。CUDA向けの古い固定はMac/MPSと無関係なので追従不要と判断)
- **`process.py`を使う場合は`ffmpeg`/`ffprobe`が必須**(音声抽出・ミックス・動画合成に使用。`separate.py`単体の利用なら仮想環境内の依存だけで足りる)

## セットアップ

このリポジトリ直下の`venv`(本体の[README.md](../README.md)「CLIの設定」参照、無ければ先に作成すること)を
有効化した状態で、`CASS/requirements.txt`を追加インストールするだけでよい(CASS専用の仮想環境は作らない)。

```bash
# リポジトリ直下で venv を有効化(Linux/Mac: source venv/bin/activate、Windows: venv\Scripts\activate.bat)

pip install -r CASS/requirements.txt

# 本家リポジトリのclone(モデルコードのみ利用、requirements.txtは使わない)
cd CASS
git clone --depth 1 https://github.com/kwatcharasupat/bandit-v2.git repo
```

別の場所に仮想環境を置きたい場合は、このリポジトリ直下の`.env`の`PYTHON_BIN`にPythonバイナリの
フルパスを指定すれば上書きできる(本体・CASS共通)。

学習済み重み(Zenodo、CC BY-SA 4.0)は手動DL不要。`separate.py`が`--ckpt`のパスに重みが無ければ**自動でダウンロード**する(`checkpoints/`フォルダ配下、`~/.cache`等のグローバルキャッシュは使わない)。デフォルトは多言語版(`checkpoint-multi.ckpt`、約450MB)。他言語版が必要な場合は`--ckpt checkpoints/checkpoint-<cmn|deu|eng|fao|fra|spa>.ckpt`を指定すれば同様に自動DLされる。

## 使い方

### 音声の3ステム分離だけ行う場合

BandIt v2は**48kHz・モノラル**入力を前提としているため、事前にffmpegで変換する。

```bash
# venvを有効化(Linux/Mac: source venv/bin/activate、Windows: venv\Scripts\activate.bat)
ffmpeg -y -i input/your_audio.m4a -ac 1 -ar 48000 input/your_audio_48k_mono.wav

python separate.py --audio input/your_audio_48k_mono.wav
```

`--audio`が48kHz以外の場合は`separate.py`側で自動リサンプルするが、モノラル変換は自動化していないので事前にffmpegで変換すること(例: 48kHzのステレオ音源をモノラル化するだけなら`ffmpeg -y -i input/stereo.wav -ac 1 input/stereo_mono.wav`)。

#### オプション

| オプション | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `--audio` | ○ | なし | 入力音声ファイル(48kHz推奨、それ以外は自動リサンプル) |
| `--ckpt` | – | `checkpoints/checkpoint-multi.ckpt` | チェックポイントのパス。無ければファイル名から自動判定してZenodoから自動DL(`checkpoint-<multi\|cmn\|deu\|eng\|fao\|fra\|spa>.ckpt`という名前である必要あり) |
| `--out` | – | `output/` | 分離結果の出力先フォルダ。`{out}/speech.wav` / `{out}/music.wav` / `{out}/sfx.wav`を保存 |

普段は`--audio`だけ指定すればよく、他言語版チェックポイントを試したい時だけ`--ckpt`を追加する。

### 動画のBGMを差し替える場合

```bash
python process.py <video.mp4> <bgm.mp3> [bgm_volume(0-1, デフォルト0.6)] [出力先.mp4] [fade_duration秒(デフォルト1.0、0でフェード無効)]
```

動画とBGMファイルを渡すだけで、音声抽出→分離(speech/sfxのみ使用、musicは新規BGMに差し替えのため破棄)→speech/sfxをフルボリューム・ステレオにpan→BGMを指定音量でミックス→動画へ合成、まで一括で行う。途中の一時ファイルは自動生成・自動削除される(旧`process.sh`をPythonへ完全移植したもの、ffmpegの処理内容自体は同一。bash/WSL/Git for Windowsが不要になり、ネイティブWindowsでもそのまま動く)。

`fade_duration`はBGMの末尾フェードアウトの長さ(秒)。**動画の末尾がフェードアウトする映像でない限り、本来は`0`(フェード無効)を指定するのが妥当**——音声だけフェードして映像が変わらないと不自然になるため。デフォルト値が`1.0`(有効)になっているのは、この`process.py`を最初に使ったワークフロー(末尾1秒フェードアウトが自動で付く動画)に合わせたもので、汎用的な既定値ではない。手元の動画に合わせて明示的に指定することを推奨する。

## 実機確認済み(M4 Max、MPS)

- 実データ(足音・セリフ・軽いBGMを含む30秒程度の音声)でspeech/music/sfxがきれいに分離できることを確認済み
- 対応デバイスは`separate.py`が自動選択(CUDA → MPS → CPU)。**動作確認はMPS(Apple Silicon)のみ、CUDA環境では未検証**

### 詰まったポイント(解決済み)

- `torchaudio.io.StreamReader`は最新torchaudioに存在しない(streaming系クラスは未使用のため直接移植で回避)
- `torchaudio.load()`が`torchcodec`必須(requirements.txtに含めている)
- MPSはfloat64バッファ非対応 → `model.to(dtype=torch.float32)`してから`.to("mps")`する順序が必要
- チェックポイントのstate_dict keyは`model.`プレフィックス付き(元がpytorch-lightningの`System`配下だったため、ロード時に剥がしている)

## ライセンス

- **コード(`repo/`、`separate.py`が参照するモデル定義部分)**: 本家`bandit-v2`は**Apache License 2.0**
- **学習済み重み(`checkpoints/`)**: **CC BY-SA 4.0**(表示-継承、[Zenodo](https://zenodo.org/records/12701995))。商用利用は可能(NonCommercial条項なし)。**重みファイル自体を(改変有無問わず)再配布する場合は帰属表示+同ライセンスでの配布が必須**。分離結果(speech/music/sfx wav)を使うだけなら通常は重みの二次的著作物とは見なされないため継承義務はかからない(写真編集ソフトがCC BY-SAでも編集後の写真にCC BY-SAが継承されないのと同じ考え方)
