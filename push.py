"""Wakes phones through the Hermsy push service.

A push carries nothing: no text, no title, no id. It only tells the phone
to check its own Hermes for anything new. The push service exists because
Google only takes pushes for the Hermsy app signed with illbytes' Firebase
key; it sees a phone's push token and when it was woken, nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import httpx

log = logging.getLogger(__name__)

DEFAULT_PUSH_URL = "https://push.hermsy.illbytes.com"

# Seconds between tries for errors worth retrying (429, 5xx, network).
RETRY_DELAYS = (2, 10, 30, 120, 600)


class Waker:
    """Wakes every registered phone, retrying in the background so the
    delivery that triggered it is not held up."""

    def __init__(
        self,
        url: str,
        tokens: Callable[[], list[str]],
        forget: Callable[[str], object],
        *,
        client: Optional[httpx.AsyncClient] = None,
        delays=RETRY_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url.rstrip("/")
        self._tokens = tokens
        self._forget = forget
        self._client = client
        self._delays = delays
        self._sleep = sleep
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def wake_all(self) -> int:
        if not self.enabled:
            return 0
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("hermsy: no event loop to wake phones from; the app catches up when opened")
            return 0
        tokens = self._tokens()
        for token in tokens:
            task = loop.create_task(self._wake(token))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return len(tokens)

    async def _post(self, token: str) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(f"{self._url}/v1/wake", json={"token": token}, timeout=15.0)
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.post(f"{self._url}/v1/wake", json={"token": token})

    async def _wake(self, token: str) -> None:
        short = token[-8:]
        for attempt in range(len(self._delays) + 1):
            retry_after = None
            try:
                response = await self._post(token)
            except (httpx.TransportError, OSError) as error:
                detail = f"push service unreachable: {error}"
            else:
                if response.status_code == 200:
                    log.info("hermsy: woke …%s", short)
                    return
                if response.status_code == 410:
                    # Google says the app is gone from that phone.
                    self._forget(token)
                    log.info("hermsy: forgot phone …%s (app uninstalled)", short)
                    return
                detail = f"push service HTTP {response.status_code}: {response.text[:120]}"
                if response.status_code != 429 and response.status_code < 500:
                    log.error("hermsy: waking …%s failed: %s", short, detail)
                    return
                retry_after = _retry_after(response)
            if attempt == len(self._delays):
                log.error("hermsy: waking …%s failed: %s", short, detail)
                return
            delay = retry_after if retry_after is not None else self._delays[attempt]
            log.warning("hermsy: waking …%s again in %ss: %s", short, delay, detail)
            await self._sleep(delay)

    async def idle(self) -> None:
        """Waits for every wake in flight (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None
