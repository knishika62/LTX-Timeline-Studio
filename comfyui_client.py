"""ComfyUI API クライアント — 画像生成・動画生成・ファイルアップロード。"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from pathlib import Path

import httpx

import pipeline_config as cfg

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
                            f"ComfyUI実行エラー(node {info.get('node_id')} [{info.get('node_type')}]): "
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


async def upload_audio_to_comfyui(audio_path: Path) -> str:
    """音声をComfyUI動画サーバーにアップロードしてサーバー側ファイル名を返す。"""
    server = cfg.COMFYUI_VIDEO_URL

    async def _do() -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            with audio_path.open("rb") as f:
                r = await client.post(
                    f"{server}/upload/image",
                    files={"image": (audio_path.name, f, "audio/wav")},
                    data={"type": "input", "overwrite": "true"},
                )
                r.raise_for_status()
                return r.json()["name"]

    return await _retry_request(_do)


async def generate_s2v_video(prompt: str, image_server_filename: str, audio_server_filename: str, duration_s: float) -> Path:
    """参照画像+音声からリップシンク動画(S2V)を生成してローカルに保存したPathを返す。"""
    workflow = _load_workflow("LTX2_S2V_V720p.json")

    workflow["240"]["inputs"]["image"] = image_server_filename       # input1: 参照画像
    workflow["243"]["inputs"]["audio"] = audio_server_filename       # input2: セリフ音声
    workflow["286"]["inputs"]["value"] = duration_s                  # input3: 動画の長さ(秒)
    workflow["169"]["inputs"]["text"] = prompt                       # input4: シーン・動きプロンプト
    workflow["178"]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)
    workflow["315"]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)

    server = cfg.COMFYUI_VIDEO_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, VIDEO_TIMEOUT_S)

    # node 190 = Video Combine(output)
    node_out = outputs.get("190", {})
    files = node_out.get("gifs") or node_out.get("videos") or []
    if not files:
        raise RuntimeError(f"S2V動画出力が見つかりません: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)


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
        raise RuntimeError(f"アップスケール動画出力が見つかりません: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)


async def generate_video_v2(prompt: str, keyframe_server_filename: str) -> Path:
    """動画を生成してローカルに保存したPathを返す(2026版LTX-2.3ワークフロー、generate_videoとは別物の検証用)。"""
    workflow = _load_workflow("2026_ltx2_3_i2v.json")

    # プロンプト注入 (node 373 = PrimitiveStringMultiline, input1)
    workflow["373"]["inputs"]["value"] = prompt
    # keyframe画像 (node 269 = LoadImage, input2)
    workflow["269"]["inputs"]["image"] = keyframe_server_filename
    # seedランダム化は1段目(node 331)のみ。2段目(node 330)は3stepsのupscaleのみなので固定値を維持
    workflow["331"]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)
    # ワークフロー内LLMノードのURLとモデルを.envで上書き
    for node_id in ("380", "381"):
        if node_id in workflow:
            workflow[node_id]["inputs"]["base_url"] = cfg.COMFYUI_LLM_BASE_URL
            workflow[node_id]["inputs"]["model"] = cfg.COMFYUI_LLM_MODEL
            if cfg.COMFYUI_LLM_API_KEY:
                workflow[node_id]["inputs"]["api_key"] = cfg.COMFYUI_LLM_API_KEY

    server = cfg.COMFYUI_VIDEO_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, VIDEO_TIMEOUT_S)

    # node 75 = SaveVideo(出力キー名は未確認のため候補を順に探す)
    node_out = outputs.get("75", {})
    files = node_out.get("videos") or node_out.get("images") or node_out.get("gifs") or []
    if not files:
        raise RuntimeError(f"動画出力が見つかりません: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)


async def generate_t2v_video(prompt: str, width: int = 720, height: int = 1280, duration_s: int = 5,
                             keyframe_server_filename: str | None = None, workflow_json: str | None = None) -> Path:
    """テキストから動画を生成してローカルに保存したPathを返す(T2V、2026版LTX-2.3ワークフロー)。

    keyframe_server_filename: 指定時はI2Vモードに切り替え(node 356=False)、node 269(LoadImage)に
    1stフレーム画像を設定する(T2V/I2Vは同一ワークフローでboolスイッチのみの違い)。未指定なら従来通りT2V。

    ワークフローJSONは.envのT2V_VIDEO_ENGINEで差し替え可能(デフォルトvideo.json)。i2v側は
    workflow_json引数で.envのI2V_VIDEO_ENGINEを明示的に渡せる(値が"default"/"10e"/"refine"以外の
    場合のみ——それらは_generate_i2v_video()が専用エンジンへ振り分ける)。
    **指定するJSONはvideo.jsonと同じノードID体系(373/366/353/355/331/356/269等)を持つことが必須**。
    存在しない、またはノード構成が異なるJSONを指定した場合のエラーはユーザー側の責任
    (ここではバリデーションしない、KEYFRAME_WORKFLOW_JSONと同じ方針、2026-07-16)。
    """
    workflow = _load_workflow(workflow_json or cfg.T2V_VIDEO_ENGINE)

    workflow["373"]["inputs"]["value"] = prompt      # input1: テキストプロンプト
    workflow["366"]["inputs"]["value"] = width        # Width(px)
    workflow["353"]["inputs"]["value"] = height       # Height(px)
    workflow["355"]["inputs"]["value"] = duration_s   # Duration(秒)
    workflow["331"]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)  # 1段目seedランダム化
    # 330(2段目、3stepのupscaleのみ)は固定42のまま
    if keyframe_server_filename:
        workflow["356"]["inputs"]["value"] = False                       # T2V→I2Vスイッチ
        workflow["269"]["inputs"]["image"] = keyframe_server_filename    # input2: 1stフレーム

    server = cfg.COMFYUI_VIDEO_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, VIDEO_TIMEOUT_S)

    # node 75 = SaveVideo
    node_out = outputs.get("75", {})
    files = node_out.get("videos") or node_out.get("images") or node_out.get("gifs") or []
    if not files:
        raise RuntimeError(f"T2V動画出力が見つかりません: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)


async def generate_video_10e(prompt: str, keyframe_server_filename: str, width: int, height: int, duration_s: float) -> Path:
    """動画を生成してローカルに保存したPathを返す(10Erosチェックポイント+DMD LoRAの検証用ワークフロー、
    workflows/10E_video.json)。i2v_timeline_cliV6のテスト用エンジン(.envのI2V_VIDEO_ENGINE=10eで有効化、
    デフォルトはgenerate_t2v_video()のまま)。触ってよいノードはユーザー指定の5つのみ(2026-07-09)、
    それ以外(LoRA有効/無効・negativeプロンプト等)はワークフローJSONの値をそのまま使う。
    """
    workflow = _load_workflow("10E_video.json")

    workflow["536"]["inputs"]["text"] = prompt                        # CLIP Text Encode (Prompt)
    workflow["837"]["inputs"]["image"] = keyframe_server_filename      # Load Image
    workflow["918"]["inputs"]["value"] = width                        # Video Width
    workflow["919"]["inputs"]["value"] = height                       # Video Height
    workflow["920"]["inputs"]["value"] = round(duration_s * 24)       # Length (Frame Count, X/24 = Seconds)
    workflow["524"]["inputs"]["seed"] = random.randint(0, 2**31 - 1)  # Seed (rgthree)
    # JSON既定は save_output=false(ComfyUI手動プレビュー用の値のまま)なので、パイプラインから
    # 呼ぶ時は明示的にtrueへ上書きしないと出力ファイルが保存されない
    workflow["597"]["inputs"]["save_output"] = True                   # Video Combine

    server = cfg.COMFYUI_VIDEO_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, VIDEO_TIMEOUT_S)

    # node 597 = VHS_VideoCombine
    node_out = outputs.get("597", {})
    files = node_out.get("gifs") or node_out.get("videos") or []
    if not files:
        raise RuntimeError(f"10Eros動画出力が見つかりません: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)


async def generate_video_refine_ltx23(prompt: str, keyframe_server_filename: str, width: int, height: int, duration_s: float, bypass_likeness: bool = False) -> Path:
    """動画を生成してローカルに保存したPathを返す(顔検出+同一性アンカー付き2段サンプリング検証用ワークフロー、
    workflows/refine_video.json)。i2v_timeline_cliV6のテスト用エンジン(.envのI2V_VIDEO_ENGINE=refineで有効化)。
    触ってよいノードはユーザー指定の6つのみ(2026-07-10)、それ以外(LoRA・likeness anchor各種strength・
    negativeプロンプト・サンプラー設定等)はワークフローJSONの値をそのまま使う。
    bypass_likeness=Trueで`354 LTX Likeness Anchor`をbypassする(`--norefine`用。顔が手/物で隠れる
    動作でこのノードが同一性を引き戻そうとして画像が破綻する対策、2026-07-10)。"""
    workflow = _load_workflow("refine_video.json")

    workflow["303"]["inputs"]["value"] = prompt                             # Prompt
    workflow["269"]["inputs"]["image"] = keyframe_server_filename           # LoadImage
    workflow["314"]["inputs"]["value"] = width                              # Width
    workflow["299"]["inputs"]["value"] = height                             # Height
    workflow["301"]["inputs"]["value"] = round(24 * duration_s) + 1        # Length (24fps*秒+1)
    workflow["275"]["inputs"]["noise_seed"] = random.randint(0, 2**31 - 1)  # RandomNoise (Generate Low Resolution)
    # JSON既定はTrueだが、10eワークフロー統合時にJSON既定falseで保存されない事故があったため、
    # ワークフロー側の値に関わらず防御的に明示上書きする
    workflow["351"]["inputs"]["save_output"] = True                        # Video Combine (終段)
    if bypass_likeness:
        workflow["354"]["inputs"]["bypass"] = True                         # LTX Likeness Anchor

    server = cfg.COMFYUI_VIDEO_URL
    prompt_id = await _queue_prompt(workflow, server)
    outputs = await _wait_for_output(prompt_id, server, VIDEO_TIMEOUT_S)

    # node 351 = VHS_VideoCombine (終段、音声付き)
    node_out = outputs.get("351", {})
    files = node_out.get("videos") or node_out.get("gifs") or node_out.get("images") or []
    if not files:
        raise RuntimeError(f"refine_ltx2_3動画出力が見つかりません: {node_out}")
    file_info = files[0]
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")

    dest = cfg.GENERATED_DIR / filename
    return await _download_output(filename, subfolder, server, dest)
