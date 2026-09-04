from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any


class VoiceTranscriptionError(RuntimeError):
    pass


class VoiceTranscriber:
    """Lazy CPU speech-to-text wrapper for Telegram voice messages."""

    def __init__(self, model_name: str, language: str | None) -> None:
        self._model_name = model_name
        self._language = language
        self._model: Any = None
        self._model_lock = threading.Lock()

    async def transcribe(self, audio_path: Path) -> str:
        try:
            return await asyncio.to_thread(self._transcribe_sync, audio_path)
        except VoiceTranscriptionError:
            raise
        except Exception as error:
            raise VoiceTranscriptionError("speech recognition failed") from error

    def _transcribe_sync(self, audio_path: Path) -> str:
        model = self._get_model()
        segments, _ = model.transcribe(
            str(audio_path),
            language=self._language,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            raise VoiceTranscriptionError("speech was not recognized")
        return text

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise VoiceTranscriptionError(
                    "voice support is not installed; install the 'voice' extra"
                ) from error
            cpu_threads = max(1, min(os.cpu_count() or 1, 8))
            self._model = WhisperModel(
                self._model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=cpu_threads,
            )
            return self._model
