"""语音转文字：飞书语音消息 → ffmpeg 转 PCM → faster-whisper 转写。

注意两个坑：
1. 模型从 HuggingFace 下，国内要走 hf-mirror，且新版 hub 的 Xet 协议在镜像上 401，必须禁用
2. faster-whisper 自带的 av 解码器与当前 av 版本不兼容（metadata_errors 参数），
   所以先用 ffmpeg 转成 numpy 数组再喂给模型，绕开 av
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

_model = None
_model_lock = threading.Lock()


def _load_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel

                _model = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _model


def warmup() -> None:
    """启动时后台加载模型，免得第一条语音等十几秒。"""
    def _run():
        try:
            _load_model()
            print("语音识别模型已就绪")
        except Exception as exc:
            print(f"语音识别模型加载失败：{type(exc).__name__}: {exc}")

    threading.Thread(target=_run, daemon=True).start()


def _ffmpeg_exe() -> str:
    """优先用 imageio-ffmpeg 自带的二进制（pip 装的，不依赖系统 PATH）。"""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def convert_to_wav(src: Path) -> Path:
    """ffmpeg 转成 16k 单声道 wav（whisper 要求）。"""
    dst = src.with_suffix(".wav")
    result = subprocess.run(
        [_ffmpeg_exe(), "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 转换失败：{(result.stderr or '')[-200:]}")
    return dst


def _wav_to_float32(wav: Path):
    """用 ffmpeg 解码成 16k 单声道 float32 数组，绕开 faster-whisper 的 av 依赖。"""
    import numpy as np

    result = subprocess.run(
        [_ffmpeg_exe(), "-i", str(wav), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, timeout=60,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("音频解码失败")
    pcm = np.frombuffer(result.stdout, dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def transcribe(audio_bytes: bytes, suffix: str = ".opus") -> str:
    """把音频字节流转成中文文字。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"input{suffix}"
        src.write_bytes(audio_bytes)
        wav = convert_to_wav(src)
        audio = _wav_to_float32(wav)

        model = _load_model()
        segments, _info = model.transcribe(audio, language="zh", beam_size=3)
        text = "".join(segment.text for segment in segments).strip()
        return text

