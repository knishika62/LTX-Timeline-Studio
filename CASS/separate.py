"""BandIt v2 (Cinematic Audio Source Separation) の最小推論スクリプト。

kwatcharasupat/bandit-v2 の requirements.txt は Netflix 社内インフラ
(ray, jasper, metaflow, metatron 等、pypi.netflix.net 経由でしか入らない
パッケージ)に強く依存しているため、そのままでは使えない。
このスクリプトは推論に本当に必要な部分(モデル定義 src/models/bandit と
チャンク推論ハンドラ src/system/inference_handler)だけを直接importし、
hydra/ray/pytorch_lightning Trainer/DataModule を経由せずに動かす。

出力: speech(セリフ) / music(音楽) / sfx(効果音) の3ステムをwavで保存。
"""

import argparse
import math
import sys
import urllib.request
from pathlib import Path
from typing import List

import torch
import torchaudio as ta
from torch import nn
from torch.nn import functional as F
from tqdm import tqdm

REPO_ROOT = Path(__file__).parent / "repo"
sys.path.insert(0, str(REPO_ROOT))

from src.models.bandit.bandit import Bandit  # noqa: E402

# `src/system/inference_handler.py` はトップレベルで `from torchaudio.io import
# StreamReader` を import しており、インストールしたtorchaudio(2.11.0)には
# `torchaudio.io` 自体が存在しない(FFmpegストリーミングAPIが別パッケージに
# 分離/削除された)ため、そのままimportできない。streaming系クラスは今回
# 使わないので、必要な2クラス(Standardのチャンク処理のみ)をここに直接移植する。


class BaseChunkedInferenceHandler(nn.Module):
    def __init__(
        self,
        chunk_size_seconds: float,
        hop_size_seconds: float,
        inference_batch_size: int,
        fs: int,
        window_fn: str = "hann_window",
        wkwargs: dict = None,
        pad_mode: str = "reflect",
        rank: int = 0,
    ):
        super().__init__()

        self.fs = fs
        self.chunk_size_samples = int(chunk_size_seconds * fs)
        self.hop_size_samples = int(hop_size_seconds * fs)
        self.overlap_samples = self.chunk_size_samples - self.hop_size_samples
        self.scaler = self.chunk_size_samples / (2 * self.hop_size_samples)

        window_fn = torch.__dict__[window_fn]
        if wkwargs is None:
            wkwargs = {}

        scaled_window = (
            window_fn(self.chunk_size_samples, **wkwargs)[None, None, :] / self.scaler
        )
        self.register_buffer("scaled_window", scaled_window)

        self.pad_mode = pad_mode
        self.inference_batch_size = inference_batch_size
        self.front_pad_samples = 2 * self.overlap_samples
        self.rank = rank

    def _get_n_chunks(self, n_samples: int):
        return (
            int(
                math.ceil(
                    (n_samples + 2 * self.front_pad_samples - self.chunk_size_samples)
                    / self.hop_size_samples
                )
            )
            + 1
        )

    def _get_end_pad_samples(self, n_samples: int, n_chunks: int):
        return (
            (n_chunks - 1) * self.hop_size_samples + self.chunk_size_samples - n_samples
        )

    def _get_padded_samples(self, n_samples: int, n_chunks: int, end_pad_samples: int):
        return n_samples + 2 * self.front_pad_samples + end_pad_samples

    def _unfold(self, segment: torch.Tensor):
        batch_size, n_channels, _ = segment.shape
        assert batch_size == 1
        segment = segment.reshape(n_channels, 1, -1, 1)
        unfolded_segment = F.unfold(
            segment,
            kernel_size=(self.chunk_size_samples, 1),
            stride=(self.hop_size_samples, 1),
        )
        unfolded_segment = unfolded_segment.permute(0, 2, 1)
        return unfolded_segment


class StandardTensorChunkedInferenceHandler(BaseChunkedInferenceHandler):
    def _fold(self, stem_output: torch.Tensor, n_samples: int, padded_samples: int):
        stem_output = stem_output * self.scaled_window.to(stem_output.device)
        stem_output = torch.permute(stem_output, (0, 2, 1))
        stem_output = F.fold(
            stem_output,
            output_size=(padded_samples, 1),
            kernel_size=(self.chunk_size_samples, 1),
            stride=(self.hop_size_samples, 1),
        )
        stem_output = stem_output[
            None, :, 0, self.front_pad_samples : self.front_pad_samples + n_samples, 0
        ]
        return stem_output

    def _cat_and_fold(
        self, stem_outputs: List[torch.Tensor], n_samples: int, padded_samples: int
    ):
        stem_output = torch.cat(stem_outputs, dim=1)
        return self._fold(stem_output, n_samples, padded_samples)

    def _pad_and_unfold(self, mixture: torch.Tensor):
        batch_size, _, n_samples = mixture.shape
        assert batch_size == 1

        n_chunks = self._get_n_chunks(n_samples)
        end_pad_samples = self._get_end_pad_samples(n_samples, n_chunks)
        padded_samples = self._get_padded_samples(n_samples, n_chunks, end_pad_samples)

        if self.front_pad_samples >= n_samples:
            reflect_pad = (n_samples - 1, n_samples - 1)
            remaining_pad = self.front_pad_samples - (n_samples - 1)
            constant_pad = (remaining_pad, remaining_pad + end_pad_samples)
        elif self.front_pad_samples + end_pad_samples >= n_samples:
            reflect_pad = (self.front_pad_samples, n_samples - 1)
            remaining_pad = self.front_pad_samples + end_pad_samples - (n_samples - 1)
            constant_pad = (0, remaining_pad)
        else:
            reflect_pad = (
                self.front_pad_samples,
                self.front_pad_samples + end_pad_samples,
            )
            constant_pad = None

        padded_mixture = F.pad(mixture, reflect_pad, mode=self.pad_mode)
        if constant_pad is not None:
            padded_mixture = F.pad(padded_mixture, constant_pad, mode="constant")

        unfolded_mixture = self._unfold(padded_mixture)
        return unfolded_mixture, n_samples, padded_samples

    def _tensor_forward(self, mixture: torch.Tensor, model: nn.Module):
        _, n_channels, n_samples = mixture.shape
        unfolded_mixture, n_samples, padded_samples = self._pad_and_unfold(mixture)

        n_chunks = unfolded_mixture.shape[1]
        n_batches = math.ceil(n_chunks / self.inference_batch_size)
        outputs = {stem: [] for stem in model.stems}

        for i in tqdm(range(n_batches), desc="separating"):
            start = i * self.inference_batch_size
            end = min((i + 1) * self.inference_batch_size, n_chunks)
            chunk = unfolded_mixture[:, start:end, :]
            input_dict = {
                "mixture": {"audio": chunk.reshape(-1, 1, self.chunk_size_samples)}
            }
            output = model(input_dict)
            del chunk

            for stem in model.stems:
                outputs[stem].append(
                    output["estimates"][stem]["audio"].reshape(
                        n_channels, -1, self.chunk_size_samples
                    )
                )
            del output

        final_outputs = {
            stem: {
                "audio": self._cat_and_fold(outputs[stem], n_samples, padded_samples)
            }
            for stem in model.stems
        }
        return {"estimates": final_outputs}

    def forward(self, mixture: torch.Tensor, model: nn.Module):
        return self._tensor_forward(mixture, model)


FS = 48000
STEMS = ["speech", "music", "sfx"]

# configs/models/bandit-mus64.yaml をそのまま反映
MODEL_KWARGS = dict(
    in_channels=1,
    band_type="musical",
    n_bands=64,
    normalize_channel_independently=False,
    treat_channel_as_feature=True,
    n_sqm_modules=8,
    emb_dim=128,
    rnn_dim=256,
    bidirectional=True,
    rnn_type="GRU",
    mlp_dim=512,
    hidden_activation="Tanh",
    hidden_activation_kwargs=None,
    complex_mask=True,
    use_freq_weights=True,
    n_fft=2048,
    win_length=2048,
    hop_length=512,
    window_fn="hann_window",
    wkwargs=None,
    power=None,
    center=True,
    normalized=True,
    pad_mode="reflect",
    onesided=True,
)

# configs/inference/chunked-tensor-a100.yaml をそのまま反映
INFERENCE_KWARGS = dict(
    chunk_size_seconds=8.0,
    hop_size_seconds=1.0,
    inference_batch_size=16,
)


# Zenodo (CC BY-SA 4.0, https://zenodo.org/records/12701995) の言語別チェックポイント。
# ~/.cache 等のグローバルキャッシュではなく、必ずこのフォルダ(checkpoints/)配下に保存する
# (プロジェクトの他モデル運用と同じ方針: mii-ttsV2もHF_HOMEをプロジェクト内に固定している)
CHECKPOINT_URLS = {
    name: f"https://zenodo.org/records/12701995/files/checkpoint-{name}.ckpt?download=1"
    for name in ("multi", "cmn", "deu", "eng", "fao", "fra", "spa")
}


def _download_with_progress(url: str, dest: Path) -> None:
    def _report(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            pct = min(100, downloaded * 100 // total_size)
            print(f"\r[separate] downloading {dest.name}: {pct}% "
                  f"({downloaded // (1024 * 1024)}MB/{total_size // (1024 * 1024)}MB)",
                  end="", flush=True)

    tmp_path = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp_path, reporthook=_report)
    print()
    tmp_path.rename(dest)


def ensure_checkpoint(ckpt_path: str) -> str:
    path = Path(ckpt_path)
    if path.exists():
        return str(path)

    stem = path.stem.removeprefix("checkpoint-")
    if stem not in CHECKPOINT_URLS:
        raise FileNotFoundError(
            f"{path} が見つからず、自動DL対象の名前({list(CHECKPOINT_URLS)})にも一致しません。"
            f"--ckpt に checkpoints/checkpoint-<{'|'.join(CHECKPOINT_URLS)}>.ckpt の形で指定してください。"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[separate] チェックポイントが見つからないため自動ダウンロードします: {path}")
    print(f"[separate] (Zenodo, CC BY-SA 4.0 https://zenodo.org/records/12701995)")
    _download_with_progress(CHECKPOINT_URLS[stem], path)
    return str(path)


def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_model(ckpt_path: str, device: str) -> Bandit:
    model = Bandit(fs=FS, stems=STEMS, **MODEL_KWARGS)

    state_dict = torch.load(ckpt_path, map_location="cpu")["state_dict"]
    # 元は System(pl.LightningModule) 配下の self.model だったため "model." prefix を剥がす
    stripped = {
        (k[len("model."):] if k.startswith("model.") else k): v
        for k, v in state_dict.items()
    }
    missing, unexpected = model.load_state_dict(stripped, strict=False)
    print(f"[separate] missing keys: {len(missing)}, unexpected keys: {len(unexpected)}")
    if missing:
        print(f"[separate]   missing (先頭5件): {missing[:5]}")
    if unexpected:
        print(f"[separate]   unexpected (先頭5件): {unexpected[:5]}")

    # MPSはfloat64バッファ非対応。float32に落としてからdevice移動する
    # (device移動と同時にdtype変換すると変換前にdevice側の型チェックで落ちるため)
    model.to(dtype=torch.float32)
    model.to(device)
    model.eval()
    return model


def main():
    p = argparse.ArgumentParser(description="BandIt v2 3ステム分離(speech/music/sfx)")
    p.add_argument("--audio", required=True, help="入力音声(48kHz推奨、それ以外は自動リサンプル)")
    p.add_argument("--ckpt", default=str(Path(__file__).parent / "checkpoints" / "checkpoint-multi.ckpt"))
    p.add_argument("--out", default=str(Path(__file__).parent / "output"))
    args = p.parse_args()

    device = pick_device()
    print(f"[separate] device: {device}")

    audio, fs = ta.load(args.audio)
    if fs != FS:
        print(f"[separate] resampling {fs} -> {FS}")
        audio = ta.functional.resample(audio, fs, FS)
    print(f"[separate] audio shape: {audio.shape}")

    ckpt_path = ensure_checkpoint(args.ckpt)
    model = load_model(ckpt_path, device)
    handler = StandardTensorChunkedInferenceHandler(fs=FS, **INFERENCE_KWARGS)
    handler.to(device)

    mixture = audio[None, :, :].to(device)

    with torch.inference_mode():
        output = handler(mixture, model)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for stem in STEMS:
        out_path = out_dir / f"{stem}.wav"
        ta.save(str(out_path), output["estimates"][stem]["audio"][0].cpu(), FS)
        print(f"[separate] saved: {out_path}")


if __name__ == "__main__":
    main()
