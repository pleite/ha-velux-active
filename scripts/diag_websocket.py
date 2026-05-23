#!/usr/bin/env python3
"""Standalone websocket sniffer for VELUX ACTIVE.

Connects to wss://app-ws.velux-active.com/ws/ using credentials from
your already-configured Home Assistant config entry and prints every
push frame to stdout. Useful for:

* Confirming the integration's push channel is alive
* Reverse-engineering new event types as Velux rolls out firmware
  updates
* Triggering ``retrieve_key`` and watching whether the cloud pushes the
  sign key (it does not — see ``docs/SIGNING.md`` — but the experiment
  is reproducible from here)

Usage::

    python scripts/diag_websocket.py --token-from /config/.storage/core.config_entries

By default it subscribes with ``filter=all`` (broader than the iOS
app's ``silent``) so you see absolutely everything the cloud is
willing to push.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

import aiohttp

# This script must run standalone; we re-implement the minimum needed
# rather than import from the integration to avoid pulling Home
# Assistant into the dependency graph of the diagnostic harness.
WEBSOCKET_URL = "wss://app-ws.velux-active.com/ws/"


def load_token_from_config_entries(path: Path) -> str:
    """Pull a fresh access_token out of /config/.storage/core.config_entries.

    The entry shape is::

        {"data": {"entries": [
            {"domain": "velux_active",
             "data": {"token_data": {"access_token": "..."}}}
        ]}}

    We deliberately do not refresh the token — if it has expired, the
    caller should either run a quick HA REST refresh or just bounce
    HA so its in-memory client reissues one.
    """
    blob = json.loads(path.read_text())
    for entry in blob["data"]["entries"]:
        if entry.get("domain") == "velux_active":
            return entry["data"]["token_data"]["access_token"]
    raise RuntimeError("No velux_active config entry found")


async def run(token: str, filter_value: str) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            WEBSOCKET_URL,
            headers={
                "User-Agent": (
                    "Velux/3.2.3 (com.velux.active; build:251; iOS 26.5.0)"
                ),
            },
            heartbeat=60,
        ) as ws:
            await ws.send_json(
                {
                    "app_type": "app_velux",
                    "version": "3.2.3",
                    "platform": "Apple",
                    "action": "Subscribe",
                    "filter": filter_value,
                    "access_token": token,
                }
            )
            print(f"# subscribed with filter={filter_value!r}", file=sys.stderr)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    print(msg.data, flush=True)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    print(f"# connection closed: {msg!r}", file=sys.stderr)
                    return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--token",
        help="Bearer token to subscribe with",
    )
    src.add_argument(
        "--token-from",
        type=Path,
        help="Path to HA's /config/.storage/core.config_entries file",
    )
    parser.add_argument(
        "--filter",
        default="all",
        choices=("all", "silent"),
        help="Subscription filter (default: all)",
    )
    args = parser.parse_args()
    token = args.token or load_token_from_config_entries(args.token_from)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(token, args.filter))


if __name__ == "__main__":
    main()
