"""パイプライン設定 — .env から読み込む。"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
import os

_BASE_DIR = Path(__file__).parent.parent  # modules/の1つ上=リポジトリ直下(2026-07-16、modules/移動時に対応)
load_dotenv(_BASE_DIR / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


LLM_BASE_URL: str = _get("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_MODEL: str = _get("LLM_MODEL", "Qwen3.6-35B-A3B-NVFP4")
LLM_API_KEY: str = _get("LLM_API_KEY", "dummy")

COMFYUI_IMAGE_URL: str = _get("COMFYUI_IMAGE_URL", "http://localhost:8188")
COMFYUI_VIDEO_URL: str = _get("COMFYUI_VIDEO_URL", "http://localhost:8188")

# i2v_timeline_cli のキーフレーム生成用: image.json の lora_01(キャラLoRA)の上書き設定。
# 名前が空なら image.json の値をそのまま使用。強度 -1 はそのまま、0 でLoRA無効化(任意キャラ生成用)
KEYFRAME_LORA_NAME: str = _get("KEYFRAME_LORA_NAME", "")
KEYFRAME_LORA_STRENGTH: float = float(_get("KEYFRAME_LORA_STRENGTH", "-1"))

# generate_image()が読み込むComfyUIワークフローJSON(workflows/配下のファイル名)。
# checkpoint変更・LoRA追加等を試すため .env で差し替え可能にする(2026-07-08)。
# デフォルトは image.json。差し替えたJSONの内容が generate_image() の期待するノード構成と
# 合わない場合のエラーはユーザー側の責任(ここではバリデーションしない)
KEYFRAME_WORKFLOW_JSON: str = _get("KEYFRAME_WORKFLOW_JSON", "image.json")

# i2v/t2vタイムラインCLIの最終連結動画の末尾フェードアウト(映像・音声とも1秒)。デフォルトON(2026-07-08)
FADE_OUT_ENABLED: bool = _get("FADE_OUT_ENABLED", "true").lower() == "true"

# i2v_timeline_cli のキーフレーム生成解像度の倍率(動画のwidth/heightに掛ける、アスペクト比は維持)。
# I2V側ワークフロー(node 344 ResizeImageMaskNode)がキーフレームを動画の最終解像度へ
# 自動リサイズするため、キーフレーム自体は動画より高解像度で生成しても問題ない。
# Krea2は1280x720ネイティブだと本領発揮しないため導入(2026-07-08、ユーザー確認: 1.2倍=1536x864/864x1536)
KEYFRAME_SIZE_SCALE: float = float(_get("KEYFRAME_SIZE_SCALE", "1.0"))

# i2v_timeline_cliV6 の動画生成エンジン切替(テスト用、2026-07-09)。"default" = 従来通り
# generate_t2v_video()(video.jsonのI2Vモード)。"10E" = generate_video_10e()
# (workflows/10E_video.json、10Erosチェックポイント+DMD LoRAの検証用ワークフロー)。
# "refine" = generate_video_refine_ltx23()(workflows/refine_video.json、顔検出+同一性アンカー付き
# 2段サンプリング検証用ワークフロー、2026-07-10)。
# t2v_timeline_cliV6は常にgenerate_t2v_video()のまま(このフラグの影響を受けない)
I2V_VIDEO_ENGINE: str = _get("I2V_VIDEO_ENGINE", "default")

# generate_t2v_video()が読み込むComfyUIワークフローJSON(workflows/配下のファイル名)。
# I2V_VIDEO_ENGINEとは別軸——こちらはKEYFRAME_WORKFLOW_JSONと同じ「ファイル名を直接差し替える」方式
# (エンジン名でノード構成を切り替えるI2V_VIDEO_ENGINEとは違い、node ID体系は同一のまま前提)。
# 指定JSONが存在しない、またはノード構成が異なる場合のエラーはユーザー側の責任(バリデーションしない、2026-07-16)
T2V_VIDEO_ENGINE: str = _get("T2V_VIDEO_ENGINE", "video.json")

IMAGE_PROMPT_PREFIX: str = _get(
    "IMAGE_PROMPT_PREFIX",
    "masterpiece, best quality, photorealistic, 8K, highly detailed, cinematic lighting, sharp focus, professional photography",
)

WORKFLOWS_DIR: Path = _BASE_DIR / "workflows"
GENERATED_DIR: Path = _BASE_DIR / "generated"
GENERATED_DIR.mkdir(exist_ok=True)

# ACE-Step-1.5 BGM生成APIサーバー(bgm_generate_cli.py専用、パイプライン本体には未統合、2026-07-13)
ACESTEP_URL: str = _get("ACESTEP_URL", "http://localhost:8001")
ACESTEP_MODEL: str = _get("ACESTEP_MODEL", "acestep-v15-sft")
