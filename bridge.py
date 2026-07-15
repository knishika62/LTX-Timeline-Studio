"""Node.jsハーネス用のPythonシム。パース・連結・アップスケールはPython実装が正典なので
JSに移植せず、既存モジュール(シンボリックリンク経由)をそのまま呼んでJSONで返す。

使い方(cwd=LTX-timeline、conda env x-post):
  conda run -n x-post python bridge.py parse_prompt <file.txt|->   # "-" でstdinから読む
  conda run -n x-post python bridge.py parse_prompts_txt <t2v|i2v> <prompts.txt>
  conda run -n x-post python bridge.py concat            # stdinにJSON {"paths": [...], "out": "..."}
  conda run -n x-post python bridge.py upscale <video>   # {stem}_FHD{suffix} を同じ場所に保存
  conda run -n x-post python bridge.py bgm               # stdinにJSON {"prompt": "...", "duration": 60, "takes": 2, "out_dir": "..."}

出力は常にstdoutへJSON1個。失敗時は {"ok": false, "error": "..."} + exit 1。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def _out(obj: dict, code: int = 0) -> None:
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


def _fail(msg: str) -> None:
    _out({"ok": False, "error": msg}, 1)


def cmd_parse_prompt(path: str) -> None:
    from timeline_common import _parse_prompt

    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as e:
        _fail(f"read failed: {e}")
    try:
        global_desc, segments, audio = _parse_prompt(text)
    except ValueError as e:
        _fail(str(e))
    _out({"ok": True, "global_desc": global_desc, "segments": segments, "audio": audio})


def cmd_parse_prompts_txt(engine: str, path: str) -> None:
    if engine == "i2v":
        from i2v_timeline_cliV6 import _parse_prompts_txt
    elif engine == "t2v":
        from t2v_timeline_cliV6 import _parse_prompts_txt
    else:
        _fail(f"unknown engine: {engine}")
    try:
        header, segments = _parse_prompts_txt(Path(path))
    except (OSError, ValueError) as e:
        _fail(str(e))
    _out({"ok": True, "header": header, "segments": segments})


def cmd_concat() -> None:
    from timeline_common import _concat_segments

    try:
        req = json.loads(sys.stdin.read())
        paths = [Path(p) for p in req["paths"]]
        out = Path(req["out"])
    except (json.JSONDecodeError, KeyError) as e:
        _fail(f"bad request: {e}")
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        _fail(f"missing inputs: {missing}")
    ok = _concat_segments(paths, out, "[edit]")
    if not ok or not out.exists():
        _fail("concat failed")
    _out({"ok": True, "out": str(out)})


def cmd_upscale(video: str) -> None:
    from comfyui_client import upload_video_to_comfyui, upscale_video

    src = Path(video)
    if not src.exists():
        _fail(f"not found: {src}")

    async def _run() -> Path:
        server_name = await upload_video_to_comfyui(src)
        return await upscale_video(server_name)

    try:
        upscaled = asyncio.run(_run())
    except Exception as e:
        _fail(f"upscale failed: {e}")
    dest = src.with_name(src.stem + "_FHD" + src.suffix)
    upscaled.rename(dest)
    _out({"ok": True, "out": str(dest)})


def cmd_bgm() -> None:
    from bgm_generate_cli import generate_bgm

    try:
        req = json.loads(sys.stdin.read())
        prompt = req["prompt"]
        duration = float(req["duration"])
        takes = req.get("takes", 2)
        out_dir = Path(req["out_dir"]) if req.get("out_dir") else None
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        _fail(f"bad request: {e}")
    try:
        paths = asyncio.run(generate_bgm(prompt, duration, takes=takes, out_dir=out_dir))
    except Exception as e:
        _fail(f"bgm generation failed: {e}")
    _out({"ok": True, "takes": [str(p) for p in paths]})


def main() -> None:
    args = sys.argv[1:]
    if not args:
        _fail("usage: bridge.py <parse_prompt|parse_prompts_txt|concat|upscale> ...")
    cmd, rest = args[0], args[1:]
    if cmd == "parse_prompt" and len(rest) == 1:
        cmd_parse_prompt(rest[0])
    elif cmd == "parse_prompts_txt" and len(rest) == 2:
        cmd_parse_prompts_txt(rest[0], rest[1])
    elif cmd == "concat" and not rest:
        cmd_concat()
    elif cmd == "upscale" and len(rest) == 1:
        cmd_upscale(rest[0])
    elif cmd == "bgm" and not rest:
        cmd_bgm()
    else:
        _fail(f"bad arguments for {cmd}")


if __name__ == "__main__":
    main()
