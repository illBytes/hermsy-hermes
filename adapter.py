"""Hermsy platform for Hermes: the phone app's half of Hermes.

- ``deliver: hermsy`` (cron results, send_message) files each message in
  the plugin's store and wakes the person's phones.
- The app talks to the plugin through Hermes's own API server, ``POST
  /api/platforms/hermsy/events``, with the same API key it uses for chat:
  the Inbox, the Brief, phone registration, cloud voices and setup. No
  separate server, port or proxy route.

Optional env:
    HERMSY_PUSH_URL      Push service (default: https://push.hermsy.illbytes.com;
                         "off" stores messages without waking phones)
    HERMSY_HOME_CHANNEL  Home channel for ``deliver: hermsy`` (default: phone)
"""

import asyncio
import hmac
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .core import MAX_MESSAGE_LENGTH, Core
from .push import DEFAULT_PUSH_URL, Waker
from .setup import Setup
from .store import Store

logger = logging.getLogger(__name__)

DEFAULT_HOME = "phone"
RETENTION_DAYS = 60
PRUNE_EVERY_SECONDS = 6 * 3600


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def secret(name: str) -> str:
    """A key the way Hermes reads it: its .env or a secret source such as
    Bitwarden, scoped to the running profile."""
    try:
        from agent.secret_scope import get_secret

        return (get_secret(name, "") or "").strip()
    except Exception:  # noqa: BLE001
        return _env(name)


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        return Path(_env("HERMES_HOME") or Path.home() / ".hermes")


def push_url() -> str:
    url = _env("HERMSY_PUSH_URL", DEFAULT_PUSH_URL)
    return "" if url.lower() in ("", "off", "none", "false") else url


def api_server_key() -> str:
    """The key the app sends: the API server's own."""
    key = secret("API_SERVER_KEY")
    if key:
        return key
    try:
        from gateway.config import load_gateway_config

        config = load_gateway_config().platforms.get(Platform.API_SERVER)
        return str((config.extra or {}).get("key") or "").strip() if config else ""
    except Exception:  # noqa: BLE001
        return ""


_cores: Dict[str, Core] = {}


def core() -> Core:
    """One per Hermes home in this process (the gateway, or a CLI send)."""
    home = hermes_home()
    found = _cores.get(str(home))
    if found is None:
        data = home / "hermsy"
        data.mkdir(parents=True, exist_ok=True)
        store = Store(str(data / "hermsy.db"))
        waker = Waker(push_url(), store.device_tokens, store.remove_device)
        found = _cores[str(home)] = Core(store, waker, secret=secret, setup=Setup(home, store))
    return found


def check_requirements() -> bool:
    return HTTPX_AVAILABLE


def is_connected(config) -> bool:
    return HTTPX_AVAILABLE


def validate_config(config) -> bool:
    return HTTPX_AVAILABLE


class HermsyAdapter(BasePlatformAdapter):
    """Nothing to listen to: messages go out through the store, and the app
    comes in through the API server's event route."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("hermsy"))
        self._pruner: Optional[asyncio.Task] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx not installed", self.name)
            return False
        hermsy = core()
        try:
            hermsy.setup.install_files()
        except OSError as error:
            logger.warning("[%s] could not install skills and scripts: %s", self.name, error)
        if self._pruner is None:
            self._pruner = asyncio.ensure_future(_prune_forever(hermsy.store))
        self._mark_connected()
        logger.info(
            "[%s] Ready: the app connects through the API server%s", self.name,
            "" if hermsy.waker.enabled else " (push off: HERMSY_PUSH_URL)",
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._pruner is not None:
            self._pruner.cancel()
            self._pruner = None

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        result = core().deliver(content)
        if result.get("success"):
            return SendResult(success=True, message_id=result["message_id"])
        logger.warning("[%s] %s", self.name, result["error"])
        return SendResult(success=False, error=result["error"], retryable=False)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Hermsy", "type": "dm"}

    # -- the app, through POST /api/platforms/hermsy/events ------------------------

    def verify_http_event_request(self, auth_header: str) -> Tuple[bool, str]:
        expected = api_server_key()
        if not expected:
            return False, "hermsy_no_api_server_key"
        given = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        if not given or not hmac.compare_digest(given.encode(), expected.encode()):
            return False, "invalid_api_key"
        return True, ""

    async def dispatch_http_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await core().handle(payload)


async def _prune_forever(store: Store) -> None:
    while True:
        removed = await asyncio.to_thread(store.prune, RETENTION_DAYS)
        if removed:
            logger.info("hermsy: removed %d messages older than %d days", removed, RETENTION_DAYS)
        await asyncio.sleep(PRUNE_EVERY_SECONDS)


def _env_enablement() -> Optional[dict]:
    home = _env("HERMSY_HOME_CHANNEL", DEFAULT_HOME) or DEFAULT_HOME
    return {"home_channel": {"chat_id": home, "name": "Hermsy"}}


async def _standalone_send(
    pconfig, chat_id: str, message: str, *,
    thread_id: Optional[str] = None, media_files: Optional[List[str]] = None, force_document: bool = False,
) -> Dict[str, Any]:
    """Cron / send_message delivery when no gateway adapter is live (a CLI
    process): stores the message and waits briefly for the wake."""
    if media_files:
        logger.info("hermsy: %d attachment(s) not forwarded (text only for now)", len(media_files))
    hermsy = core()
    result = hermsy.deliver(message)
    if not result.get("success"):
        return {"error": result["error"]}
    try:
        await asyncio.wait_for(hermsy.waker.idle(), timeout=20)
    except asyncio.TimeoutError:
        logger.warning("hermsy: phones not woken yet; the app catches up when opened")
    return {"success": True, "platform": "hermsy", "chat_id": chat_id, "message_id": result["message_id"]}


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin system at startup."""
    # `deliver: hermsy` resolves its home channel from this env var.
    os.environ.setdefault("HERMSY_HOME_CHANNEL", DEFAULT_HOME)
    ctx.register_platform(
        name="hermsy", label="Hermsy", adapter_factory=lambda cfg: HermsyAdapter(cfg),
        check_fn=check_requirements, validate_config=validate_config, is_connected=is_connected,
        required_env=[],
        install_hint="pip install httpx   # already a Hermes dependency",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="HERMSY_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH, emoji="📱",
        pii_safe=True,  # the only chat id is a fixed label
        allow_update_command=False,
        platform_hint=(
            "You are messaging the user's private phone app, Hermsy, as a push "
            "notification. The first line becomes the notification preview, so "
            "lead with the point. Markdown renders in the app."
        ))
