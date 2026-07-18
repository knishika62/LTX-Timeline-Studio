"""mp4 + BGM(任意) を渡すだけで「音声抽出→BandIt v2で3ステム分離→speech/sfxフルボリューム+BGM
(指定%)を映像のフェードアウトに同期させてミックス→mp4へ合成」まで一括で行う。
BGMを省略した場合はspeech/sfxのみをミックスする(musicステムは使わない設計、docs/blog.md参照)。

使い方:
  python process.py <video.mp4> [bgm.mp3] [bgm_volume(0-1, デフォルト0.6)] [out.mp4] [fade_duration_seconds(デフォルト1.0、0でフェード無効)]

音楽ステム(music.wav)は使わず、新規BGMに完全差し替える設計。

旧 process.sh(bash)からの移植(2026-07-18)。ネイティブWindowsではbash/WSL/Git for Windowsが
必須になってしまうため、リポジトリ全体をvenv化するのに合わせてPythonへ完全移植した。
ffmpegのフィルタ構成・フェード計算・分岐ロジックは元のスクリプトと同一、呼び出し方法だけを変更している。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _run(args: list[str], **kwargs) -> None:
    subprocess.run(args, check=True, **kwargs)


def _ffprobe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(out.strip())


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("video")
    parser.add_argument("bgm", nargs="?", default=None)
    parser.add_argument("bgm_volume", nargs="?", type=float, default=0.6)
    parser.add_argument("out", nargs="?", default=None)
    parser.add_argument("fade_duration", nargs="?", type=float, default=1.0)
    args = parser.parse_args()

    video = Path(args.video)
    bgm = Path(args.bgm) if args.bgm else None
    out = Path(args.out) if args.out else SCRIPT_DIR / "output" / f"{video.stem}_remixed.mp4"

    print(f"[process] video: {video}", flush=True)
    if bgm:
        print(f"[process] bgm: {bgm} (volume={args.bgm_volume})", flush=True)
    else:
        print("[process] bgm: 無し(speech+sfxのみでミックス)", flush=True)

    tmp_root = SCRIPT_DIR / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="process_", dir=str(tmp_root)) as workdir_str:
        workdir = Path(workdir_str)

        print("[process] 1/3 音声抽出(48kHz mono)...", flush=True)
        audio_tmp = workdir / "audio_48k_mono.wav"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
              "-vn", "-ac", "1", "-ar", "48000", str(audio_tmp)])

        print("[process] 2/3 BandIt v2で3ステム分離(speech/sfxのみ使用、"
              "musicは新規BGMに差し替えのため破棄)...", flush=True)
        stems_dir = workdir / "stems"
        _run([sys.executable, "-u", "separate.py",
              "--audio", str(audio_tmp), "--out", str(stems_dir)], cwd=SCRIPT_DIR)

        duration = _ffprobe_duration(video)
        out.parent.mkdir(parents=True, exist_ok=True)

        speech = stems_dir / "speech.wav"
        sfx = stems_dir / "sfx.wav"

        if bgm:
            if args.fade_duration > 0:
                fade_start = max(0.0, duration - args.fade_duration)
                bgm_filter = (
                    f"[3:a]atrim=0:{duration},volume={args.bgm_volume},"
                    f"afade=t=out:st={fade_start}:d={args.fade_duration}[bgm]"
                )
                print(f"[process] 3/3 ミックス+mp4合成(尺={duration}s、"
                      f"フェード開始={fade_start}s、フェード長={args.fade_duration}s)...", flush=True)
            else:
                bgm_filter = f"[3:a]atrim=0:{duration},volume={args.bgm_volume}[bgm]"
                print(f"[process] 3/3 ミックス+mp4合成(尺={duration}s、フェード無効)...", flush=True)

            filter_complex = (
                "[1:a]pan=stereo|c0=c0|c1=c0[speech_st];"
                "[2:a]pan=stereo|c0=c0|c1=c0[sfx_st];"
                f"{bgm_filter};"
                "[speech_st][sfx_st][bgm]amix=inputs=3:duration=longest:normalize=0[aout]"
            )
            _run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(video), "-i", str(speech), "-i", str(sfx), "-i", str(bgm),
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", str(out),
            ])
        else:
            print(f"[process] 3/3 ミックス+mp4合成(尺={duration}s、BGM無し)...", flush=True)
            filter_complex = (
                "[1:a]pan=stereo|c0=c0|c1=c0[speech_st];"
                "[2:a]pan=stereo|c0=c0|c1=c0[sfx_st];"
                "[speech_st][sfx_st]amix=inputs=2:duration=longest:normalize=0[aout]"
            )
            _run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(video), "-i", str(speech), "-i", str(sfx),
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", str(out),
            ])

    print(f"[process] 完了: {out}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"使い方: {sys.argv[0]} <video.mp4> [bgm.mp3] [bgm_volume(0-1, デフォルト0.6)] "
              "[out.mp4] [fade_duration_seconds(デフォルト1.0、0でフェード無効)]", file=sys.stderr)
        print("  bgm.mp3 を省略すると BGM 無し(speech+sfxのみ)でミックスする", file=sys.stderr)
        sys.exit(1)
    main()
