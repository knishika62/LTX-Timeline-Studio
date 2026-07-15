#!/bin/bash
# LTX-timeline ハーネスのセットアップ(冪等、何度実行してもOK)
#
# - Pythonコード(生成CLI+import一式)は ../x-post へのシンボリックリンク(更新管理を一元化)
# - workflows/ と CASS/ は実体コピー(ユーザー指示)。CASSの作業サブフォルダは空で作り直す
# - 作業フォルダ(prompt/ generated/ uploads/ 等)は本フォルダ直下の実ディレクトリ
# - .env は x-post の .env からハーネスに必要なキーだけを抽出して新規作成(既存があれば触らない)
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"
XPOST="$BASE/../x-post"

if [ ! -d "$XPOST" ]; then
  echo "ERROR: $XPOST が見つかりません" >&2
  exit 1
fi

# --- 1. Pythonコードのシンボリックリンク -------------------------------------
PY_LINKS=(
  t2v_timeline_cliV6.py
  i2v_timeline_cliV6.py
  timeline_common.py
  comfyui_client.py
  pipeline_config.py
  prompt_generator.py
  bgm_generate_cli.py
)
for f in "${PY_LINKS[@]}"; do
  ln -sfn "../x-post/$f" "$BASE/$f"
done
echo "symlinks: ${PY_LINKS[*]}"

# --- 2. workflows/ と CASS/ のコピー -----------------------------------------
rsync -a --delete "$XPOST/workflows/" "$BASE/workflows/"
echo "copied: workflows/"

# CASS はコード・チェックポイントのみコピーし、作業サブフォルダの中身は持ち込まない
rsync -a \
  --exclude '__pycache__' \
  --exclude 'output/*' \
  --exclude 'tmp/*' \
  --exclude 'input/*' \
  --exclude 'bgm/*' \
  "$XPOST/CASS/" "$BASE/CASS/"
mkdir -p "$BASE/CASS/output" "$BASE/CASS/tmp" "$BASE/CASS/input" "$BASE/CASS/bgm"
echo "copied: CASS/ (output/tmp/input/bgm は空の作業フォルダとして作成)"

# --- 3. 作業フォルダ ----------------------------------------------------------
mkdir -p "$BASE/prompt" "$BASE/generated" "$BASE/uploads"
echo "workdirs: prompt/ generated/ uploads/"

# --- 4. .env(必要キーのみ抽出、既存は保護) ----------------------------------
ENV_KEYS=(
  LLM_BASE_URL LLM_MODEL LLM_API_KEY
  COMFYUI_LLM_BASE_URL COMFYUI_LLM_MODEL COMFYUI_LLM_API_KEY
  COMFYUI_IMAGE_URL COMFYUI_VIDEO_URL
  IMAGE_PROMPT_PREFIX
  KEYFRAME_LORA_NAME KEYFRAME_LORA_STRENGTH
  KEYFRAME_WORKFLOW_JSON KEYFRAME_SIZE_SCALE
  I2V_VIDEO_ENGINE FADE_OUT_ENABLED
  ACESTEP_URL ACESTEP_MODEL
)
if [ -f "$BASE/.env" ]; then
  echo ".env: 既に存在するため変更しません"
else
  {
    echo "# LTX-timeline ハーネス用 .env(setup.sh が x-post/.env から必要キーのみ抽出して生成)"
    echo "# 元の全設定は ../x-post/.env を参照"
    echo
    for key in "${ENV_KEYS[@]}"; do
      line="$(grep -E "^${key}=" "$XPOST/.env" | tail -n 1 || true)"
      if [ -n "$line" ]; then
        echo "$line"
      fi
    done
  } > "$BASE/.env"
  echo ".env: 生成しました($(grep -c '=' "$BASE/.env") キー)"
fi

echo "done."
