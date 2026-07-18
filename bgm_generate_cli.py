"""ACE-Step-1.5 API(localhost:8001)でインストゥルメンタルBGMを生成するCLI。

CASS/process.py(音声分離+BGMミックス)へ渡すBGM素材を、手動Suno生成の代わりに
自動生成するための独立ツール。パイプライン本体(pipeline_server.py)・v6_harness.py
には未統合(2026-07-13時点、まず単体スクリプトとして動作確認する方針)。

常にボーカル無し(lyrics="[Instrumental]"固定)。歌詞付き曲の生成はサポートしない。

使い方(リポジトリ直下のvenvを有効化した状態で実行):
  python bgm_generate_cli.py --prompt "warm lo-fi piano and acoustic guitar, calm cozy mood" --duration 120
  python bgm_generate_cli.py --prompt "..." --duration 60 --takes 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.parse
from datetime import datetime
from pathlib import Path

import httpx

from modules import pipeline_config as cfg

_POLL_INTERVAL_S = 5.0
_POLL_TIMEOUT_S = 600.0
_INIT_TIMEOUT_S = 300.0
_RETRY_COUNT = 3
_RETRY_WAIT_S = 3.0


async def _retry_request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(_RETRY_COUNT):
        try:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < _RETRY_COUNT - 1:
                print(f"[bgm] 通信エラー、{_RETRY_WAIT_S}秒後にリトライ({attempt + 1}/{_RETRY_COUNT}): {exc}")
                await asyncio.sleep(_RETRY_WAIT_S)
    raise last_exc  # type: ignore[misc]


async def _ensure_model_loaded(client: httpx.AsyncClient, base_url: str, model: str) -> None:
    resp = await _retry_request(client, "GET", f"{base_url}/health")
    health = resp.json()["data"]
    if health.get("models_initialized") and health.get("loaded_model") == model:
        return
    print(f"[bgm] モデル未ロードのため初期化中: {model}(初回は数分かかる場合があります)")
    await _retry_request(
        client, "POST", f"{base_url}/v1/init",
        json={"model": model}, timeout=_INIT_TIMEOUT_S,
    )
    print("[bgm] モデル初期化完了")


async def _submit_job(client: httpx.AsyncClient, base_url: str, prompt: str, duration_s: float, takes: int | None) -> str:
    body: dict = {
        "prompt": prompt,
        "lyrics": "[Instrumental]",
        "task_type": "text2music",
        "thinking": True,
        "audio_duration": duration_s,
        "vocal_language": "unknown",
        "audio_format": "mp3",
    }
    if takes is not None:
        body["batch_size"] = takes
    resp = await _retry_request(client, "POST", f"{base_url}/release_task", json=body)
    data = resp.json()["data"]
    task_id = data["task_id"]
    print(f"[bgm] ジョブ投入: task_id={task_id}")
    return task_id


async def _poll_result(client: httpx.AsyncClient, base_url: str, task_id: str) -> list[dict]:
    elapsed = 0.0
    last_progress = ""
    while elapsed < _POLL_TIMEOUT_S:
        resp = await _retry_request(
            client, "POST", f"{base_url}/query_result", json={"task_id_list": [task_id]},
        )
        item = resp.json()["data"][0]
        status = item.get("status")
        progress = item.get("progress_text", "")
        if progress and progress != last_progress:
            print(f"[bgm] {progress}")
            last_progress = progress
        if status == 1:
            return json.loads(item["result"])
        if status == 2:
            raise RuntimeError(f"生成失敗(task_id={task_id}): {progress}")
        await asyncio.sleep(_POLL_INTERVAL_S)
        elapsed += _POLL_INTERVAL_S
    raise TimeoutError(f"生成がタイムアウトしました(task_id={task_id}, {_POLL_TIMEOUT_S}秒)")


async def _download_take(client: httpx.AsyncClient, base_url: str, file_ref: str, out_path: Path) -> None:
    url = file_ref if file_ref.startswith("http") else f"{base_url}{file_ref}"
    resp = await _retry_request(client, "GET", url, timeout=60.0)
    out_path.write_bytes(resp.content)


async def generate_bgm(prompt: str, duration_s: float, takes: int | None, out_dir: Path | None = None) -> list[Path]:
    base_url = cfg.ACESTEP_URL.rstrip("/")
    dest_dir = out_dir if out_dir is not None else cfg.GENERATED_DIR
    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_model_loaded(client, base_url, cfg.ACESTEP_MODEL)
        task_id = await _submit_job(client, base_url, prompt, duration_s, takes)
        results = await _poll_result(client, base_url, task_id)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_paths: list[Path] = []
        for i, item in enumerate(results, start=1):
            file_ref = item.get("file", "")
            if not file_ref:
                continue
            suffix = Path(urllib.parse.urlparse(file_ref).path).suffix or ".mp3"
            out_path = dest_dir / f"bgm_{ts}_take{i}{suffix}"
            await _download_take(client, base_url, file_ref, out_path)
            out_paths.append(out_path)
            print(f"[bgm] 保存: {out_path}")
        return out_paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ACE-Step-1.5でインストゥルメンタルBGMを生成するCLI")
    parser.add_argument("--prompt", required=True, help="曲の説明文(英語推奨、スタイル・楽器・雰囲気・テンポ等)")
    parser.add_argument("--duration", type=float, default=60.0, help="長さ(秒)。デフォルト60")
    parser.add_argument("--takes", type=int, default=None, help="生成本数(省略時はサーバーのデフォルト、通常2)")
    args = parser.parse_args()

    asyncio.run(generate_bgm(args.prompt, args.duration, args.takes))
