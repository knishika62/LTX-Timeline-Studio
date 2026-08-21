"""ComfyUI API クライアント — 画像生成・動画生成・ファイルアップロード。"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from pathlib import Path

import httpx

from . import pipeline_config as cfg

POLL_INTERVAL_S = 3
IMAGE_TIMEOUT_S = 900
VIDEO_TIMEOUT_S = 900
CLIENT_ID = "pipeline"
RETRY_COUNT = 3
RETRY_DELAY_S = 3


def _load_workflow(name: str) -> dict:
    print(f"[comfyui] workflow: {name}")
    path = cfg.WORKFLOWS_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


async def _retry_request(fn):
    """一時的な接続エラー(LAN越しの通信で稀に発生する)をリトライする。"""
    last_exc: httpx.HTTPError | None = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            return await fn()
        except httpx.HTTPError as e:
            last_exc = e
            print(f"[comfyui] 接続エラー(試行{attempt}/{RETRY_COUNT}): {e}")
            if attempt < RETRY_COUNT:
                await asyncio.sleep(RETRY_DELAY_S)
    raise last_exc


async def _queue_prompt(workflow: dict, server_url: str) -> str:
    """ワークフローをキューに入れてprompt_idを返す。"""
    payload = {"prompt": workflow, "client_id": CLIENT_ID}

    async def _do() -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{server_url}/prompt", json=payload)
            r.raise_for_status()
            return r.json()["prompt_id"]

    return await _retry_request(_do)


async def _wait_for_output(prompt_id: str, server_url: str, timeout_s: int) -> dict:
    """historyをポーリングしてoutputsを返す。一時的な接続エラーはリトライする(LAN越しのポーリングで稀に発生するため)。

    ComfyUI側で実行エラーが起きた場合、historyのstatus.messagesに`execution_error`が入るが、
    従来はこれを見ずに`entry["outputs"]`をそのまま返していたため、呼び出し元では出力ノードが
    空のoutputsしか得られず「出力が見つかりません」という実際の原因(モデル未配置・ノード不整合等)
    を隠してしまう無関係なエラーになっていた(2026-07-09、新規追加ワークフローのテスト中に発覚)。
    completed=Trueかつexecution_errorがあれば、そちらを実際の原因としてここで送出する。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    async with httpx.AsyncClient(timeout=15) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await client.get(f"{server_url}/history/{prompt_id}")
                data = r.json()
            except httpx.HTTPError as e:
                print(f"[comfyui] history取得で一時エラー、リトライします: {e}")
                await asyncio.sleep(POLL_INTERVAL_S)
                continue
            entry = data.get(prompt_id, {})
            status = entry.get("status", {})
            if status.get("completed"):
                for msg in status.get("messages", []):
                    if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
                        info = msg[1]
                        raise RuntimeError(
                            f"ComfyUI execution error (node {info.get('node_id')} [{info.get('node_type')}]): "
                            f"{info.get('exception_message')}")
                return entry.get("outputs", {})
            await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"ComfyUI generation timed out after {timeout_s}s")


async def _download_output(filename: str, subfolder: str, server_url: str, dest: Path) -> Path:
    """ComfyUI /view からファイルをダウンロードして保存する。"""
    params = {"filename": filename, "subfolder": subfolder, "type": "output"}

    async def _do() -> Path:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(f"{server_url}/view", params=params)
            r.raise_for_status()
            dest.write_bytes(r.content)
        return dest

    return await _retry_request(_do)


async def generate_image(prompt: str, width: int = 1024, height: int = 1536,
                         seed: int | None = None, lora_name: str = "", lora_strength: float = -1) -> Path:
    """画像を生成してローカルに保存したPathを返す。

    seed: 指定時はランダム化せずその値を使用(i2v_timeline_cliが全キーフレームで同一seedを使いキャラ一貫性を上げる用途)
    lora_name / lora_strength: node 76 の lora_01/strength_01 を上書き(空/-1 = ワークフローJSONのままの値。strength=0でキャラLoRA無効化)
    ※Negative prompt(node 71)への追記機能は実装しないこと: image.jsonはCFG=1.0のためnegativeは効かない
    ワークフローJSONは.envのKEYFRAME_WORKFLOW_JSONで差し替え可能(デフォルトimage.json、2026-07-08)。
    checkpoint変更・LoRA追加等を試す用途。ノード構成が想定と異なる場合のエラーはユーザー側の責任。
    """
    workflow = _load_workflow(cfg.KEYFRAME_WORKFLOW_JSON)

    # プロンプト注入 (node 67 = Positive CLIPTextEncode)
    workflow["67"]["inputs"]["text"] = prompt
    # サイズ上書き (node 68 = EmptySD3LatentImage)
    workflow["68"]["inputs"]["width"] = width
    workflow["68"]["inputs"]["height"] = height
    # seed (node 69 = KSampler)
    workflow["69"]["inputs"]["seed"] = seed if seed is not None else random.randint(0, 2**32 - 1)
    # キャラLoRAの上書き (node 76 = Lora Loader Stack の lora_01)
    if lora_name:
        workflow["76"]["inputs"]["lora_01"] = lora_name
    if lora_strength >= 0:
        workflow["76"]["inputs"]["strength_01"] = lora_strength

    server = cfg.COMFYUI_IMAGE_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, IMAGE_TIMEOUT_S)

    # node 9 = SaveImage
    img_info = outputs["9"]["images"][0]
    filename = img_info["filename"]
    subfolder = img_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)


async def upload_image_to_comfyui(img_path: Path) -> str:
    """画像をComfyUI動画サーバーにアップロードしてサーバー側ファイル名を返す。"""
    server = cfg.COMFYUI_VIDEO_URL

    async def _do() -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            with img_path.open("rb") as f:
                r = await client.post(
                    f"{server}/upload/image",
                    files={"image": (img_path.name, f, "image/png")},
                    data={"type": "input", "overwrite": "true"},
                )
                r.raise_for_status()
                return r.json()["name"]

    return await _retry_request(_do)


async def upload_video_to_comfyui(video_path: Path) -> str:
    """動画をComfyUI動画サーバーにアップロードしてサーバー側ファイル名を返す。"""
    server = cfg.COMFYUI_VIDEO_URL

    async def _do() -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            with video_path.open("rb") as f:
                r = await client.post(
                    f"{server}/upload/image",
                    files={"image": (video_path.name, f, "video/mp4")},
                    data={"type": "input", "overwrite": "true"},
                )
                r.raise_for_status()
                return r.json()["name"]

    return await _retry_request(_do)


async def upscale_video(video_server_filename: str) -> Path:
    """RTX Video Super Resolutionで動画をフルHD相当にアップスケールし、ローカルに保存したPathを返す。"""
    workflow = _load_workflow("rtx_video_upscale.json")
    workflow["6"]["inputs"]["file"] = video_server_filename

    server = cfg.COMFYUI_VIDEO_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, VIDEO_TIMEOUT_S)

    # node 9 = SaveVideo(出力キー名は未確認のため候補を順に探す)
    node_out = outputs.get("9", {})
    files = node_out.get("videos") or node_out.get("images") or node_out.get("gifs") or []
    if not files:
        raise RuntimeError(f"Upscale video output not found: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)


async def generate_t2v_video(prompt: str, width: int = 720, height: int = 1280, duration_s: int = 5,
                             keyframe_server_filename: str | None = None, workflow_json: str | None = None) -> Path:
    """テキストから動画を生成してローカルに保存したPathを返す(T2V、LTX-2.5ワークフロー)。

    keyframe_server_filename: 指定時はI2Vモードに切り替え(node 414=False)、node 395(LoadImage)に
    1stフレーム画像を設定する(T2V/I2Vは同一ワークフローでboolスイッチのみの違い)。未指定なら従来通りT2V。

    ワークフローJSONは.envのT2V_VIDEO_ENGINEで差し替え可能(デフォルトvideo.json)。i2v側は
    workflow_json引数で.envのI2V_VIDEO_ENGINEを明示的に渡せる(値が"default"以外の場合のみ——
    それ以外は_generate_i2v_video()が振り分ける)。
    **指定するJSONはvideo.jsonと同じノードID体系(410/417/419/413/437/414/395等)を持つことが必須**。
    存在しない、またはノード構成が異なるJSONを指定した場合のエラーはユーザー側の責任
    (ここではバリデーションしない、KEYFRAME_WORKFLOW_JSONと同じ方針、2026-07-16)。
    """
    workflow = _load_workflow(workflow_json or cfg.T2V_VIDEO_ENGINE)

    workflow["410"]["inputs"]["value"] = prompt      # Prompt: テキストプロンプト
    workflow["417"]["inputs"]["value"] = width        # Width(px)
    workflow["419"]["inputs"]["value"] = height       # Height(px)
    workflow["413"]["inputs"]["value"] = duration_s   # Duration(秒)
    workflow["437"]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)  # 1段目seedランダム化
    # 446(2段目、latent upscale pass)は固定42のまま
    if keyframe_server_filename:
        workflow["414"]["inputs"]["value"] = False                        # T2V→I2Vスイッチ
        workflow["395"]["inputs"]["image"] = keyframe_server_filename     # Load First Frame

    server = cfg.COMFYUI_VIDEO_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, VIDEO_TIMEOUT_S)

    # node 75 = SaveVideo
    node_out = outputs.get("75", {})
    files = node_out.get("videos") or node_out.get("images") or node_out.get("gifs") or []
    if not files:
        raise RuntimeError(f"T2V video output not found: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)


# MiniMax-H3は高解像度だと生成が大幅に遅くなるため(2026-08-03実機確認)、t2v/i2v共通で
# 0.4MP・16:9・multiple=32のResolutionSelector実測値(864x480、SD)に固定する。ワークフロー内の
# ResolutionSelector(node 115)は接続を切って読まない(WFファイル側のmegapixels値に依存すると、
# t2v/i2vで別々の値になっていて食い違う事故が起きたため、2026-08-03)。
# .envのMiniMaxH3_HD=1で0.9MP相当(1280x736、HD)へ切り替え可能(2026-08-03)
_H3_LANDSCAPE_RESOLUTION_SD = (864, 480)
_H3_LANDSCAPE_RESOLUTION_HD = (1280, 736)


def h3_landscape_resolution() -> tuple[int, int]:
    """.envのMiniMaxH3_HDに応じてH3のベース解像度(landscape基準)を返す。generate_video_h3()と
    i2v_timeline_cliV6.pyの_keyframe_size()の両方から呼ばれる共有ヘルパー(2026-08-03、
    SD/HD切り替えの分岐ロジックを2箇所に重複させないため)。"""
    return _H3_LANDSCAPE_RESOLUTION_HD if cfg.MiniMaxH3_HD else _H3_LANDSCAPE_RESOLUTION_SD


async def generate_video_h3(prompt: str, workflow_json: str, width: int, height: int, duration_s: float,
                             keyframe_server_filename: str | None = None) -> Path:
    """MiniMax-H3ワークフローで動画を生成してローカルに保存したPathを返す
    (workflows/minimax_h3_t2v.json / minimax_h3_i2v.json)。.envのMiniMaxH3_T2V/MiniMaxH3_I2V
    で有効化(空なら呼ばれない、_generate_i2v_video()/_generate_t2v_video_dispatch()が
    I2V_VIDEO_ENGINE/T2V_VIDEO_ENGINEより優先で振り分ける)。

    width/height: node 105:104(MiniMaxH3ImageToVideo)のwidth/height inputは元々ワークフロー内の
    ResolutionSelector(node 115)からのリンクだったが、node 115は9:16固定で--h(横)/--v(縦)の
    向きを反映できなかった(2026-08-03、実機で「i2vを--hにしたのに縦で生成される」不具合として発覚)。
    リンクは生の数値で上書きし、実際のピクセル数はh3_landscape_resolution()(SD=864x480/
    HD=1280x736、.envのMiniMaxH3_HDで切替)固定を使う。呼び出し元のwidth/heightは
    **向き(landscapeかportraitか)の判定にのみ**使い、
    landscapeならそのまま、portraitなら幅高を入れ替える。node 115自体は接続を切られた無害な
    未使用ノードとして残る。

    duration_s: 生の秒数のままnode 105:111へ書き込む。node 105:107のComfyMathExpressionが
    秒→フレーム数への変換とquantize(frames % 17 == 5に切り上げ)をワークフロー内部で行うため、
    Python側でフレーム数へ事前変換しないこと(尺が伸びると超線形にコストが増えるのはこの
    quantizeが原因、既知の挙動でバグではない)。

    keyframe_server_filename: 指定時はi2vワークフロー側のnode 114(LoadImage)に1stフレームを
    設定する。t2vワークフローにはnode 114が存在しないため未指定のまま呼ぶこと。
    """
    workflow = _load_workflow(workflow_json)

    landscape_w, landscape_h = h3_landscape_resolution()
    width, height = (landscape_w, landscape_h) if width >= height else (landscape_h, landscape_w)

    workflow["105:104"]["inputs"]["prompt"] = prompt                       # MiniMaxH3ImageToVideo
    workflow["105:104"]["inputs"]["width"] = width                         # ResolutionSelectorリンクを上書き
    workflow["105:104"]["inputs"]["height"] = height                       # 同上
    if keyframe_server_filename:
        workflow["114"]["inputs"]["image"] = keyframe_server_filename      # LoadImage(i2vのみ存在)
    workflow["105:111"]["inputs"]["value"] = duration_s                    # PrimitiveFloat(秒数のまま)
    workflow["105:15"]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)  # RandomNoise

    server = cfg.COMFYUI_VIDEO_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, VIDEO_TIMEOUT_S)

    # node 92 = SaveVideo(ネイティブComfyUIノード、10E/refineのVHS_VideoCombineとは別物)
    node_out = outputs.get("92", {})
    files = node_out.get("videos") or node_out.get("images") or node_out.get("gifs") or []
    if not files:
        raise RuntimeError(f"MiniMax-H3 video output not found: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)
