"""
Offline speech-to-text using Moonshine (ONNX build). Telegram voice messages
arrive as ogg/opus; Moonshine wants a plain wav, so we shell out to ffmpeg
for the conversion (ffmpeg must be on PATH).
"""
import logging
import os
import subprocess
import tempfile

from config import MOONSHINE_MODEL

log = logging.getLogger("stt")

# Imported lazily on first use so `python -m py_compile` / unit tests that
# don't need real transcription don't require the (large) model download.
_moonshine = None


def _get_moonshine():
    global _moonshine
    if _moonshine is None:
        import moonshine_onnx as moonshine  # noqa: WPS433 (intentional lazy import)
        _moonshine = moonshine
    return _moonshine


def _convert_to_wav(src_path: str, dst_path: str):
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", src_path, "-ar", "16000", "-ac", "1", "-f", "wav", dst_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed converting voice note: {result.stderr.decode(errors='replace')}"
        )


def transcribe_voice(audio_bytes: bytes) -> str:
    """
    audio_bytes: raw bytes of the .ogg/.oga voice note as downloaded from Telegram.
    Returns the transcribed text (possibly empty string if nothing recognizable).
    """
    moonshine = _get_moonshine()

    with tempfile.TemporaryDirectory() as tmp_dir:
        ogg_path = os.path.join(tmp_dir, "voice.ogg")
        wav_path = os.path.join(tmp_dir, "voice.wav")

        with open(ogg_path, "wb") as f:
            f.write(audio_bytes)

        _convert_to_wav(ogg_path, wav_path)

        segments = moonshine.transcribe(wav_path, MOONSHINE_MODEL)

    text = " ".join(s.strip() for s in segments if s and s.strip())
    return text.strip()
