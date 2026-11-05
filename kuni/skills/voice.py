"""Голос: расшифровка входящих войсов (Whisper) и синтез своих (TTS).

TTS — любой OpenAI-совместимый /v1/audio/speech (например OmniVoice,
оттуда же клонирование голоса "clone:..."). Войс отправляется как
голосовое сообщение.
"""
from __future__ import annotations

import logging
import struct
import wave
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class VoiceService:
    def __init__(self, whisper_url: str, tts_url: str = "", tts_voice: str = "",
                 tts_model: str = "omniv") -> None:
        self.whisper_url = whisper_url
        self.tts_url = tts_url
        self.tts_voice = tts_voice
        self.tts_model = tts_model

    @property
    def can_transcribe(self) -> bool:
        return bool(self.whisper_url)

    @property
    def can_speak(self) -> bool:
        return bool(self.tts_url)

    # ---------------------------------------------------------- расшифровка

    async def transcribe(self, audio_path: Path) -> str:
        if not self.whisper_url:
            return ""
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(audio_path, "rb") as fp:
                resp = await client.post(
                    self.whisper_url,
                    files={"file": (audio_path.name, fp, "audio/ogg")},
                    data={"model": "whisper-1"},
                )
        resp.raise_for_status()
        return resp.json().get("text", "")

    # -------------------------------------------------------------- синтез

    async def synthesize(self, text: str, out_path: Path) -> Path:
        if not self.tts_url:
            raise RuntimeError("TTS не настроен ([voice].tts_url)")
        body: dict[str, Any] = {
            "model": self.tts_model,
            "input": text,
            "response_format": "wav",
        }
        if self.tts_voice:
            body["voice"] = self.tts_voice
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{self.tts_url.rstrip('/')}", json=body)
        resp.raise_for_status()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return out_path

    async def speak(self, speech_text: str, out_path: Path, prompt: str = "",
                    llm: Any = None) -> Path:
        """Озвучить то, что она хочет сказать.

        Если передан промпт record_audio_speech, LLM сначала очищает текст:
        из него убираются служебные пометки и описания интонации, чтобы в
        голос ушло только то, что человек услышит.
        """
        text = (speech_text or "").strip()
        if not text:
            raise RuntimeError("нечего озвучивать")
        if prompt and llm is not None:
            try:
                result = await llm.chat(
                    [{"role": "system", "content": prompt},
                     {"role": "user", "content": text}],
                    function_label="voice_script",
                    temperature=0.3,
                )
                cleaned = (result.content or "").strip()
                if cleaned:
                    text = cleaned
            except Exception as e:
                log.warning("скрипт голоса не очищен: %s", str(e)[:120])
        return await self.synthesize(text, out_path)

    @staticmethod
    def wav_duration(path: Path) -> int:
        try:
            with wave.open(str(path), "rb") as wav:
                return int(wav.getnframes() / max(1, wav.getframerate()))
        except (wave.Error, struct.error):
            return 0
