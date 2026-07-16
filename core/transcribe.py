"""Audio transcription for AgentOS (faster-whisper).

A single injectable transcriber used by lecture capture (Phase 10) and Telegram voice
notes (Phase 8). faster-whisper runs on the droplet CPU (small int8 model) and handles
long audio via its own VAD/segmentation. The heavy import is lazy so this module loads
without the library; callers can inject a fake transcriber for offline tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.logging_setup import get_logger

log = get_logger("core.transcribe")

Transcriber = Callable[[str], str]

_model = None


def _get_model(model_size: str = "small"):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _model


def transcribe_audio(path: str | Path, *, language: str | None = None) -> str:
    """Transcribe an audio file to text. Long files are segmented by faster-whisper itself."""
    p = str(path)
    if not Path(p).exists():
        raise FileNotFoundError(p)
    try:
        model = _get_model()
        segments, _info = model.transcribe(p, language=language, vad_filter=True)
        return " ".join(seg.text for seg in segments).strip()
    except Exception:  # noqa: BLE001
        log.exception("transcription failed for %s", p)
        return ""
