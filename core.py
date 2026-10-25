"""What the hermsy plugin does, without Hermes's gateway: files what Hermes
delivers, wakes the phones, and answers the app.

The app reaches it through Hermes's own API server, ``POST
/api/platforms/hermsy/events``, with the same API key it uses for chat.
Each request is one JSON object naming an ``op``; the answer is a JSON
object, with ``error`` (and an HTTP-like ``status``) when it failed.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import httpx

from .brief import BRIEF_JOBS, parse_brief
from .push import Waker
from .speech import MAX_TEXT as MAX_SPEECH, SAMPLE_RATE, ElevenLabsSpeech, OpenAiSpeech, SpeechError
from .store import Store

log = logging.getLogger(__name__)

VERSION = "2.0.1"

# The plugin accepts up to 100k characters; the app shows long results in full.
MAX_MESSAGE_LENGTH = 100_000

# Hermes wraps cron output (unless cron.wrap_response is false) in a header
# naming the job and a footer about stopping it. On a phone the job name is
# the notification title and the footer is noise, so both are lifted off.
_CRON_WRAPPER = re.compile(
    r"\ACronjob Response: (?P<name>[^\n]*)\n"
    r"\(job_id: (?P<job_id>[^)\n]*)\)\n"
    r"-{3,}\n\n"
    r"(?P<body>.*?)"
    r"(?:\n\nTo stop or manage this job, send me a new message[^\n]*)?\Z",
    re.DOTALL,
)


def split_cron_wrapper(content: str) -> Dict[str, Optional[str]]:
    """``{"text", "job_name", "job_id", "source"}`` for a delivered message."""
    match = _CRON_WRAPPER.match(content)
    if not match:
        return {"text": content, "job_name": None, "job_id": None, "source": "agent"}
    return {
        "text": match.group("body").strip() or content,
        "job_name": match.group("name").strip() or None,
        "job_id": match.group("job_id").strip() or None,
        "source": "cron",
    }


class Failure(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def _int(payload: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError):
        raise Failure(400, f"{key} must be a number") from None
    return max(low, min(high, value))


def _str(payload: dict, key: str, *, low: int = 1, high: int = 4096) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise Failure(400, f"{key} is missing or the wrong length")
    return value.strip()


class Core:
    """``secret(name)`` reads a key the way Hermes does (its .env, or a
    secret source such as Bitwarden), so cloud voices need no setup of
    their own."""

    def __init__(
        self,
        store: Store,
        waker: Waker,
        *,
        secret: Callable[[str], str],
        setup: Any = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.store = store
        self.waker = waker
        self._secret = secret
        self.setup = setup
        self._client = client
        self._voices: dict[str, Any] = {}

    # -- from Hermes -------------------------------------------------------------

    def deliver(self, content: str) -> dict:
        """Files one delivered message (or Brief) and wakes the phones.
        Returns ``{"success", "message_id"}`` or ``{"error"}``."""
        text = (content or "").strip()
        if not text:
            return {"error": "hermsy: empty message"}
        body = split_cron_wrapper(text)
        if body["job_name"] in BRIEF_JOBS:
            try:
                brief = self.store.add_brief(parse_brief(body["text"]))
            except ValueError as error:
                # Not a usable Brief: keep it as a message so nothing is lost,
                # with a first line a person can read in the notification.
                log.warning("hermsy: Brief reply was not a Brief (%s); kept as a message", error)
                body["text"] = (
                    "This Brief could not be read, so the Brief page still shows the last one. "
                    "The reply is below.\n\n```\n" + body["text"] + "\n```"
                )
            else:
                self.waker.wake_all()
                return {"success": True, "message_id": brief["id"]}
        message = self.store.add_message(
            text=body["text"][:MAX_MESSAGE_LENGTH],
            title=body["job_name"] or "Hermsy",
            source=body["source"],
            job_name=body["job_name"],
            job_id=body["job_id"],
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.waker.wake_all()
        return {"success": True, "message_id": message.id}

    # -- from the app ------------------------------------------------------------

    async def handle(self, payload: dict) -> dict:
        op = payload.get("op")
        handler = getattr(self, f"_op_{op}", None) if isinstance(op, str) and op.isidentifier() else None
        if handler is None:
            return {"error": f"unknown op {op!r}", "status": 400}
        try:
            return await handler(payload)
        except Failure as failure:
            return {"error": failure.error, "status": failure.status}

    async def _op_hello(self, payload: dict) -> dict:
        """What this plugin can do, for the app's setup screen."""
        answer = {
            "plugin": "hermsy",
            "version": VERSION,
            "store": self.store.id,
            "push": self.waker.enabled,
            "voices": sorted(self._speech()),
            "timezone": self.store.get("timezone"),
        }
        if self.setup is not None:
            answer["setup"] = self.setup.status()
        return answer

    async def _op_register_device(self, payload: dict) -> dict:
        token = _str(payload, "token", low=20)
        platform = payload.get("platform", "android")
        if platform not in ("android", "ios"):
            raise Failure(400, "platform must be android or ios")
        self.store.upsert_device(token, platform)
        return {"ok": True}

    async def _op_remove_device(self, payload: dict) -> dict:
        self.store.remove_device(_str(payload, "token"))
        return {"ok": True}

    async def _op_messages(self, payload: dict) -> dict:
        since = _int(payload, "since", 0, 0, 2**62)
        limit = _int(payload, "limit", 50, 1, 200)
        messages = self.store.messages_since(since, limit)
        return {
            "store": self.store.id,
            "messages": [m.to_json() for m in messages],
            "next": messages[-1].seq if messages else since,
        }

    async def _op_message(self, payload: dict) -> dict:
        message = self.store.get_message(_str(payload, "id", high=100))
        if message is None:
            raise Failure(404, "no such message")
        return message.to_json()

    async def _op_brief(self, payload: dict) -> dict:
        return {"brief": self.store.latest_brief()}

    def _speech(self) -> dict:
        """The cloud voices Hermes has keys for, rebuilt when a key changes
        (a rotated key reaches the next sentence)."""
        built = {}
        for key, name, cls in (
            ("openai", "OPENAI_API_KEY", OpenAiSpeech),
            ("elevenlabs", "ELEVENLABS_API_KEY", ElevenLabsSpeech),
        ):
            secret = (self._secret(name) or "").strip()
            if not secret:
                continue
            cached = self._voices.get(key)
            if cached is None or cached[0] != secret:
                cached = self._voices[key] = (secret, cls(secret, self._http()))
            built[key] = cached[1]
        return built

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def _op_voices(self, payload: dict) -> dict:
        providers = {}
        for key, provider in self._speech().items():
            try:
                voices = [voice.to_json() for voice in await provider.voices()]
                providers[key] = {"name": provider.name, "voices": voices}
            except (SpeechError, httpx.HTTPError) as error:
                providers[key] = {"name": provider.name, "voices": [], "error": str(error)}
        return {"providers": providers, "sample_rate": SAMPLE_RATE}

    async def _op_speak(self, payload: dict) -> dict:
        """One sentence as raw 16-bit mono PCM at 24 kHz, base64-encoded."""
        provider = self._speech().get(payload.get("provider"))
        if provider is None:
            raise Failure(404, f"Hermes has no API key for {payload.get('provider')!r}")
        text = _str(payload, "text", high=MAX_SPEECH)
        voice = _str(payload, "voice", high=100)
        try:
            speed = max(0.5, min(2.0, float(payload.get("speed", 1.0))))
        except (TypeError, ValueError):
            raise Failure(400, "speed must be a number") from None
        try:
            audio = await provider.synthesize(text, voice, speed)
        except SpeechError as error:
            raise Failure(error.status, error.detail) from error
        except httpx.HTTPError as error:
            raise Failure(502, f"{provider.name} unreachable: {error}") from error
        return {"audio": base64.b64encode(audio).decode("ascii"), "sample_rate": SAMPLE_RATE}

    async def _op_setup(self, payload: dict) -> dict:
        """Creates the jobs the person asked for, at times in their time zone."""
        if self.setup is None:
            raise Failure(503, "setup is not available here")
        zone = payload.get("timezone")
        if zone is not None and not isinstance(zone, str):
            raise Failure(400, "timezone must be a name like Europe/London")
        try:
            return self.setup.run(
                brief=bool(payload.get("brief")),
                chief_of_staff=bool(payload.get("chief_of_staff")),
                zone=zone,
            )
        except ValueError as error:
            raise Failure(400, str(error)) from error

    async def close(self) -> None:
        await self.waker.close()
        if self._client is not None:
            await self._client.aclose()
