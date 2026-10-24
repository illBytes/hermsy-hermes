"""Cloud voices for Hermsy's calls: OpenAI and ElevenLabs.

The app sends one sentence at a time; the plugin asks the provider with the
key Hermes already has, and returns raw 16-bit mono PCM at 24 kHz, the same
shape for both, so the phone plays either the same way.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import httpx

SAMPLE_RATE = 24000
MAX_TEXT = 1000


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    description: str

    def to_json(self) -> dict:
        return asdict(self)


class SpeechError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _provider_error(name: str, response: httpx.Response) -> SpeechError:
    if response.status_code in (401, 403):
        return SpeechError(502, f"{name} rejected the API key")
    if response.status_code == 429:
        return SpeechError(429, f"{name} is rate limiting or out of credit")
    return SpeechError(502, f"{name} HTTP {response.status_code}: {response.text[:200]}")


class OpenAiSpeech:
    """gpt-4o-mini-tts. Speed is asked for in the style instructions: the
    speed parameter does not apply to this model."""

    name = "OpenAI"
    voices_ = [
        Voice("marin", "Marin", "Natural, warm · recommended"),
        Voice("cedar", "Cedar", "Natural, grounded · recommended"),
        Voice("coral", "Coral", "Bright, friendly"),
        Voice("sage", "Sage", "Calm, clear"),
        Voice("nova", "Nova", "Upbeat"),
        Voice("shimmer", "Shimmer", "Soft"),
        Voice("alloy", "Alloy", "Neutral"),
        Voice("ash", "Ash", "Relaxed, low"),
        Voice("ballad", "Ballad", "Gentle, expressive"),
        Voice("echo", "Echo", "Even, measured"),
        Voice("fable", "Fable", "Storyteller, British"),
        Voice("onyx", "Onyx", "Deep"),
        Voice("verse", "Verse", "Lively, expressive"),
    ]

    def __init__(self, key: str, client: httpx.AsyncClient, *, base: str = "https://api.openai.com") -> None:
        self._key = key
        self._client = client
        self._base = base.rstrip("/")

    async def voices(self) -> list[Voice]:
        return self.voices_

    async def synthesize(self, text: str, voice: str, speed: float) -> bytes:
        pace = "a natural pace"
        if speed < 0.97:
            pace = "a slightly slower, unhurried pace"
        elif speed > 1.12:
            pace = "a brisk, noticeably faster pace"
        elif speed > 1.03:
            pace = "a slightly faster pace"
        response = await self._client.post(
            f"{self._base}/v1/audio/speech",
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "model": "gpt-4o-mini-tts",
                "input": text,
                "voice": voice,
                "response_format": "pcm",
                "instructions": (
                    "You are a personal assistant on a phone call. Speak "
                    f"warmly and conversationally, at {pace}."
                ),
            },
            timeout=30.0,
        )
        if response.status_code != 200:
            raise _provider_error(self.name, response)
        return response.content


class ElevenLabsSpeech:
    """Flash v2.5: ElevenLabs' fast model for live conversation."""

    name = "ElevenLabs"
    _voices_ttl = 600.0

    def __init__(self, key: str, client: httpx.AsyncClient, *, base: str = "https://api.elevenlabs.io") -> None:
        self._key = key
        self._client = client
        self._base = base.rstrip("/")
        self._voices: list[Voice] | None = None
        self._voices_at = 0.0

    async def voices(self) -> list[Voice]:
        if self._voices is not None and time.monotonic() - self._voices_at < self._voices_ttl:
            return self._voices
        response = await self._client.get(
            f"{self._base}/v1/voices",
            headers={"xi-api-key": self._key},
            timeout=15.0,
        )
        if response.status_code != 200:
            raise _provider_error(self.name, response)
        voices = []
        for item in response.json().get("voices", []):
            labels = item.get("labels") or {}
            parts = [labels.get(k) for k in ("gender", "age", "accent", "description", "use_case")]
            description = " · ".join(p.replace("_", " ") for p in parts if p) or item.get("category", "")
            voices.append(Voice(item["voice_id"], item.get("name", "Voice"), description))
        self._voices = voices
        self._voices_at = time.monotonic()
        return voices

    async def synthesize(self, text: str, voice: str, speed: float) -> bytes:
        response = await self._client.post(
            f"{self._base}/v1/text-to-speech/{voice}",
            params={"output_format": f"pcm_{SAMPLE_RATE}"},
            headers={"xi-api-key": self._key},
            json={
                "text": text,
                "model_id": "eleven_flash_v2_5",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    # ElevenLabs accepts 0.7–1.2.
                    "speed": max(0.7, min(1.2, speed)),
                },
            },
            timeout=30.0,
        )
        if response.status_code != 200:
            raise _provider_error(self.name, response)
        return response.content
