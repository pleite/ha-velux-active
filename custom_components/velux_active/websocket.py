"""Real-time state push channel for Velux ACTIVE.

The official iOS/Android app subscribes to ``wss://app-ws.velux-active.com/ws/``
to receive sub-second push notifications when a module's state changes.
That endpoint was observed in a live MITM capture during the diagnostic
work for this integration (see ``docs/SIGNING.md``). Connecting to it
removes the need for the 60-second polling loop that has historically
caused two annoying problems:

1. **Sluggish UI** — moving a window via the app takes up to 60 s to be
   reflected in Home Assistant.
2. **Optimistic-state drift** — without push updates the integration is
   tempted to write a "command sent" position locally, which gets
   overwritten on the next poll if the command actually failed (the
   pre-fix root cause of the silent ``code 9`` bug).

The websocket is **best-effort**: we never let a connection failure
disable the integration, because the HTTP polling loop is still the
ground truth. If the socket drops we reconnect with exponential backoff;
if it stays down we degrade silently to poll-only.

Protocol notes
==============

Observed handshake::

    >>> {
    ...     "app_type": "app_velux",
    ...     "version": "3.2.3",
    ...     "platform": "Apple",
    ...     "action": "Subscribe",
    ...     "filter": "silent",
    ...     "access_token": "<bearer>"
    ... }
    <<< {"status": "ok", "time_exec": 0.009, "time_server": 1779486928}

Subsequent server pushes look like Netatmo "homestatus delta" events:
a JSON object with an ``event_type`` (or ``push_type``) field, a ``home``
block, and the module(s) that changed. We treat any push containing a
module dict with an ``id`` and one of {``current_position``,
``target_position``, ``reachable``} as a state update and forward the
parsed dict to the coordinator via the registered callback.

The exact filter token semantics are not publicly documented. ``silent``
is what the iOS app uses; ``all`` also works (we observed both during
diagnosis). We default to ``all`` because we want every event the cloud
is willing to give us — the polling loop will still de-duplicate.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

WEBSOCKET_URL = "wss://app-ws.velux-active.com/ws/"

# How long we wait between reconnect attempts. Starts low so a transient
# network blip recovers fast; caps so we don't hammer Velux's servers
# during a sustained outage. Each attempt applies random jitter to avoid
# synchronized reconnect storms from multiple clients.
_INITIAL_BACKOFF_S = 2.0
_MAX_BACKOFF_S = 300.0  # 5 minutes
_BACKOFF_MULTIPLIER = 2.0

# Server-side connections seem to expire silently after a while; sending
# a keepalive every minute is well under any TCP/proxy idle timeout and
# also lets us notice half-open connections quickly.
_PING_INTERVAL_S = 60.0

PushCallback = Callable[[dict[str, Any]], Awaitable[None]]


class VeluxActiveWebsocket:
    """Manages a long-lived websocket subscription to Velux ACTIVE.

    Lifecycle is parented to a :class:`asyncio.Task` started by
    :meth:`async_start` and cancelled by :meth:`async_stop`. Multiple
    callbacks may be registered; each is awaited per push.

    This class deliberately does **not** import anything from Home
    Assistant so the standalone ``scripts/diag_websocket.py`` harness can
    re-use it for off-instance debugging.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_provider: Callable[[], Awaitable[str]],
        *,
        filter_value: str = "all",
        app_version: str = "3.2.3",
    ) -> None:
        self._session = session
        # We take a callable rather than a token string because the
        # bearer token rotates every ~3 h via the OAuth refresh flow.
        # Asking for a fresh one at (re)connect time guarantees we never
        # try to subscribe with an expired credential.
        self._token_provider = token_provider
        self._filter = filter_value
        self._app_version = app_version
        self._callbacks: list[PushCallback] = []
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def register_callback(self, callback: PushCallback) -> Callable[[], None]:
        """Register a push callback. Returns a deregistration handle."""
        self._callbacks.append(callback)

        def _unregister() -> None:
            with contextlib.suppress(ValueError):
                self._callbacks.remove(callback)

        return _unregister

    async def async_start(self) -> None:
        """Start the background reconnect loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_forever(), name="velux_active_ws"
        )

    async def async_stop(self) -> None:
        """Stop the background loop. Idempotent."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_forever(self) -> None:
        backoff = _INITIAL_BACKOFF_S
        while not self._stop_event.is_set():
            sleep_for = backoff
            try:
                await self._run_once()
                # Clean disconnect — reset backoff before reconnecting.
                backoff = _INITIAL_BACKOFF_S
                sleep_for = backoff
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — websocket lib raises
                # everything from ServerHandshakeError to ClientOSError
                # here; we log and back off uniformly.
                sleep_for = min(backoff, _MAX_BACKOFF_S) * random.uniform(0.5, 1.5)
                _LOGGER.warning(
                    "Velux websocket disconnected (%s); reconnecting in %.1fs",
                    err,
                    sleep_for,
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=sleep_for
                )
                return  # stop signalled during backoff
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_S)

    async def _run_once(self) -> None:
        token = await self._token_provider()
        async with self._session.ws_connect(
            WEBSOCKET_URL,
            headers={
                # Mimic the iOS app UA so any cloud-side filtering by
                # client doesn't drop us.
                "User-Agent": (
                    f"Velux/{self._app_version} (com.velux.active; build:251; "
                    "iOS 26.5.0)"
                ),
            },
            heartbeat=_PING_INTERVAL_S,
        ) as ws:
            subscribe = {
                "app_type": "app_velux",
                "version": self._app_version,
                "platform": "Apple",
                "action": "Subscribe",
                "filter": self._filter,
                "access_token": token,
            }
            await ws.send_json(subscribe)
            _LOGGER.info(
                "Velux websocket connected (filter=%r)", self._filter
            )
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await self._dispatch(msg.data.decode("utf-8", "ignore"))
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise ConnectionError(
                        f"Websocket closed (type={msg.type!r}, "
                        f"data={msg.data!r})"
                    )

    async def _dispatch(self, raw: str) -> None:
        try:
            event: Any = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Velux websocket non-JSON frame: %s", raw[:200])
            return
        # Drop the trivial subscribe-ack and keepalive frames so
        # downstream callbacks don't have to filter them.
        if isinstance(event, dict) and event.get("status") == "ok" and (
            "time_server" in event or "time_exec" in event
        ):
            _LOGGER.debug("Velux websocket ack: %s", event)
            return
        _LOGGER.debug("Velux websocket push: %s", event)
        for cb in list(self._callbacks):
            try:
                await cb(event if isinstance(event, dict) else {"raw": event})
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Velux websocket callback raised; continuing"
                )
