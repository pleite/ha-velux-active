# Diagnosing "HA says it's moving but the window doesn't"

This document explains the silent-failure class of bugs in the
`velux_active` integration and how to use the new tooling in this branch
to confirm whether you're hitting one of them.

## Symptom

You ask Home Assistant to open / close / set position on a Velux ACTIVE
cover. The UI briefly shows "Opening" / "Closing" and then snaps back to
the previous position. The actuator never actually moves. Both the
official **VELUX ACTIVE** mobile app and **Apple Home** can still move
the same cover. No error is shown in the HA log.

## Why it happens (three layered root causes)

### 1. The Velux cloud lies in its HTTP status code

`POST https://app.velux-active.com/syncapi/v1/setstate` returns **HTTP
200** even when the gateway rejects the command. The real verdict is in
the body:

```json
{
  "status": "ok",
  "body": {
    "errors": [
      { "id": "<module_id>", "code": 6 }
    ]
  }
}
```

The pre-fix `api.py` only inspected `resp.status` (and `resp.ok`), so
every per-module error was silently dropped. The integration thought
the command had succeeded.

### 2. `cover.py` was writing an optimistic position to HA state

`async_open_cover`, `async_close_cover` and `async_set_cover_position`
all updated `_attr_current_cover_position` and called
`async_write_ha_state()` *before* the next coordinator poll. So even
when the actuator never moved, HA was briefly told "you are at 100%".
~60 s later, `_async_update_data` returned the real (unchanged)
position and the UI snapped back. That snap-back is the symptom users
report.

### 3. The `bridge` id used in setstate was cached at startup

`_names_fetched` was a one-shot flag on `coordinator.py`. After a
re-pairing on the KIX 300 (or some firmware updates), the bridge id
rotates. The integration kept posting setstate to the *old* bridge id
and the cloud routed those messages to nowhere — again with HTTP 200,
again silently.

## What changed on this branch

| File | Change |
|------|--------|
| `custom_components/velux_active/api.py` | New `VeluxActiveCommandError` exception. New helper `_extract_setstate_errors` parses the body of every setstate response. `async_set_cover_position`, `async_stop_movements`, `async_set_silent_mode` now raise when the cloud reports per-module/per-bridge errors or a non-`ok` top-level status. All three methods also `_LOGGER.debug` the full body so you can see the rejection reason. |
| `custom_components/velux_active/cover.py` | Removed the optimistic `_attr_current_cover_position` writes. Added `_current_bridge_id` property that prefers the freshest bridge id from the coordinator payload, falls back to a per-module cache, and only as a last resort to the value cached at entity construction. |
| `custom_components/velux_active/coordinator.py` | `_names_fetched` is now a *timestamp*, not a boolean. Homesdata (the source of truth for bridge↔module mapping) is re-fetched every 30 minutes. Module entries in `data` are now decorated with the latest `bridge` id. New `module_bridges` mapping exposed for `cover.py`. |
| `tests/test_api.py` | Five new regression tests pin the new error-surfacing behaviour. |
| `scripts/diag_setstate.py` | Standalone diagnostic CLI that bypasses HA and prints the raw setstate response body. |

## Reproducing the failure mode in isolation

```bash
python scripts/diag_setstate.py
```

The tool will:
1. Authenticate using your VELUX ACTIVE credentials (entered
   interactively; nothing is saved).
2. Dump every NXO cover with `id` / `velux_type` / `bridge` / `room_id`.
3. Let you pick one and a target position.
4. POST setstate and **print the full response body** — including any
   `body.errors` array the production integration used to swallow.
5. Poll homestatus every 5 s for 60 s so you can see whether
   `current_position` ever actually changes.

If you see `body.errors` come back with a numeric code, that's the
smoking gun for root cause #1 above. Capture that error code and open
an issue — we can build a code → human-readable mapping over time.

## Enabling debug logs in Home Assistant

Add to `configuration.yaml` and restart HA:

```yaml
logger:
  default: warning
  logs:
    custom_components.velux_active: debug
    aiohttp.client: debug
```

With this branch installed, every setstate call now logs its full
request + response body. Search the log for `setstate` after a failed
move attempt.

## What this branch does NOT change

* It does **not** alter authentication, polling cadence, or anything in
  config flow.
* It does **not** silently retry rejected commands. Surfacing the error
  is intentional — once we know what error codes the cloud actually
  returns under which physical conditions, we can build proper
  user-facing messaging (e.g. "shutter must be open before window can
  pass position 8 %").
* It does **not** require touching the KIX 300 bridge itself.
