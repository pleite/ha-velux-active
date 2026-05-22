"""Diagnostic harness for the Velux ACTIVE setstate endpoint.

Use this to determine *why* a cover command is accepted by Home Assistant
but the physical actuator never moves. It bypasses the HA event loop and
talks straight to the Velux cloud, printing the full request and response
so you can see whether the cloud is silently rejecting the command.

Usage (from the repo root):

    python scripts/diag_setstate.py

It will:
  1. Prompt for your VELUX ACTIVE email/password.
  2. Authenticate, dump homesdata, list every NXO (cover) with its bridge id.
  3. Let you pick a target module + position.
  4. POST setstate and pretty-print the full response body (which the
     production integration used to ignore).
  5. Poll homestatus every 5s for 60s and print position changes, so you can
     compare "what the API said" with "what the gateway actually did".
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
from typing import Any

# Make the custom_components package importable when run from the repo root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import aiohttp
from custom_components.velux_active.api import (
    VeluxActiveApi,
    VeluxActiveAuthError,
    VeluxActiveCommandError,
    VeluxActiveConnectionError,
    extract_setstate_errors,
)
from custom_components.velux_active.const import (
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    SET_STATE_URL,
)


def _input(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def _print_covers(homesdata: dict[str, Any], home_id: str) -> list[dict[str, Any]]:
    """Print and return the list of NXO modules for the given home."""
    homes = homesdata.get("body", {}).get("homes", [])
    covers: list[dict[str, Any]] = []
    for home in homes:
        if home.get("id") != home_id:
            continue
        for module in home.get("modules", []):
            if module.get("type") == "NXO":
                covers.append(module)
    print()
    print("Cover modules (NXO):")
    for idx, m in enumerate(covers):
        print(
            f"  [{idx}] id={m.get('id')} name={m.get('name')!r} "
            f"velux_type={m.get('velux_type')!r} bridge={m.get('bridge')!r} "
            f"room_id={m.get('room_id')!r}"
        )
    return covers


async def _post_setstate(
    session: aiohttp.ClientSession,
    token: str,
    home_id: str,
    bridge_id: str,
    module_id: str,
    position: int,
) -> tuple[int, Any]:
    payload = {
        "home": {
            "id": home_id,
            "modules": [
                {
                    "bridge": bridge_id,
                    "id": module_id,
                    "target_position": position,
                }
            ],
        }
    }
    print()
    print("→ POST", SET_STATE_URL)
    print("  payload:", json.dumps(payload, indent=2))
    async with session.post(
        SET_STATE_URL,
        json=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    ) as resp:
        try:
            body = await resp.json()
        except Exception:  # pragma: no cover - diagnostic only
            body = {"_raw_text": await resp.text()}
        print(f"← HTTP {resp.status}")
        print("  body:", json.dumps(body, indent=2))
        return resp.status, body


async def main() -> None:
    print("=== Velux Active setstate diagnostic ===")
    username = _input("Email address")
    password = getpass.getpass("Password: ")

    async with aiohttp.ClientSession() as session:
        api = VeluxActiveApi(
            session, username, password, DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET
        )
        try:
            await api.async_authenticate()
        except (VeluxActiveAuthError, VeluxActiveConnectionError) as err:
            print(f"Authentication failed: {err}")
            return
        print("Authenticated OK.")

        homesdata = await api.async_get_homes_data()
        homes = homesdata.get("body", {}).get("homes", [])
        if not homes:
            print("No homes returned.")
            return
        home_id = homes[0].get("id")
        print(f"Using home_id={home_id}")

        covers = _print_covers(homesdata, home_id)
        if not covers:
            print("No NXO covers found.")
            return

        idx = int(_input("Pick a cover index", "0"))
        target = covers[idx]
        position = int(_input("Target position (0-100)", "50"))

        status, body = await _post_setstate(
            session,
            api.access_token or "",
            home_id,
            target.get("bridge", ""),
            target.get("id", ""),
            position,
        )

        errors = extract_setstate_errors(body)
        if errors:
            print()
            print(
                "⚠️  Cloud returned errors despite HTTP 200 — this is the smoking gun"
            )
            print("    the production integration used to swallow:")
            for e in errors:
                print("   ", e)
        elif isinstance(body, dict) and body.get("status") not in (None, "ok"):
            print(f"⚠️  Non-ok status: {body.get('status')!r}")
        else:
            print("✓ Cloud accepted the command. Will now watch for movement...")

        # Poll homestatus to see whether the actuator actually moves
        print()
        print("Polling homestatus every 5s for 60s...")
        for _ in range(12):
            await asyncio.sleep(5)
            status_data = await api.async_get_home_status(home_id)
            for m in status_data.get("body", {}).get("home", {}).get("modules", []):
                if m.get("id") == target.get("id"):
                    print(
                        f"  current_position={m.get('current_position')} "
                        f"target_position={m.get('target_position')} "
                        f"reachable={m.get('reachable')}"
                    )


if __name__ == "__main__":
    asyncio.run(main())
