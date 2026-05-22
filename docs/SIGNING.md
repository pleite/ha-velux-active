# VELUX ACTIVE — command-signing protocol

This document captures the reverse-engineering work that explains why
the **Pleite/ha-velux-active** integration (and its upstream
**IngmarStein/home-assistant-velux-active**) silently fails to open
window modules while close and shutter commands succeed.

## TL;DR

* Closing a window (`target_position = 0`) works unsigned.
* Opening a window (`target_position > 0`) is rejected with HTTP 200 + body `{"errors": [{"code": 9, ...}]}`.
* Shutters work entirely unsigned — they have no safety restriction.
* The official VELUX ACTIVE iOS/Android app attaches an HMAC-SHA512
  signature to safety-restricted commands. Velux's cloud requires it.
* The signing key (`HashSignKey`) is provisioned to the app at first
  launch and stored in the OS keychain; it never appears in routine
  REST traffic.
* Once the user supplies their `HashSignKey` and `SignKeyId` via the
  Home Assistant options flow, this integration computes the
  signature and the cloud accepts the command.

## Restricted items

The cloud enforces signatures on:

| `item_name`        | When signature required                |
|--------------------|----------------------------------------|
| `target_position`  | Only when value > 0 (i.e. opening)     |
| `scenario`         | Only when value == `"home"` (unlock)   |

Anything else (`silent`, `stop_movements`, `target_position=0`, etc.)
is accepted unsigned.

## Signature algorithm

```
pre_hash = item_name + str(value) + str(timestamp) + str(nonce) + device_id
hash     = base64url( HMAC_SHA512( hash_sign_key_bytes, pre_hash.utf8 ) )
```

* `item_name`: e.g. `"target_position"`
* `value`: the literal value, stringified (int or str)
* `timestamp`: current unix epoch seconds
* `nonce`: 32-bit unsigned integer; the iOS app uses `0` for every
  request — replay protection appears to rely on `timestamp` alone
* `device_id`: the target module ID (the **module**, not the bridge)
* `hash_sign_key`: 32 bytes; stored as either hex or base64url in the
  iOS keychain
* `base64url`: standard base64 with `+` → `-`, `/` → `_`; the iOS
  app does **not** strip padding (`=`)

The resulting 64-byte digest is exactly 88 base64 characters.

## Wire format

Window OPEN request body (captured from iOS app, 2026-05-22):

```json
{
  "app_identifier": "app_velux",
  "home": {
    "id": "<home_uuid>",
    "modules": [
      {
        "id": "5636133219200932",
        "bridge": "70:ee:50:bb:c0:83",
        "target_position": 14,
        "hash_target_position": "FhvZhHv8_xtegXwOoKTkIN9UMXbb99ZCjzkp1Wc0mdHny49uWW2Smoosg7rALpf8c8b6sy4YTpwyPri46NJxYw==",
        "nonce": 0,
        "timestamp": 1779484789,
        "sign_key_id": "AAAAAGnPxXF1vyEUZgOFAw=="
      }
    ]
  }
}
```

Window CLOSE — note no signature fields:

```json
{
  "home": {
    "id": "<home_uuid>",
    "modules": [
      {"id": "5636133219200932", "bridge": "70:ee:50:bb:c0:83", "target_position": 0}
    ]
  }
}
```

## `sign_key_id` encoding

`SignKeyId` lives in the keychain as a short hex string (24 chars in
the wild). To reach the wire format used in the `sign_key_id` JSON
field:

1. Left-pad the hex with zeros until it is 32 chars (16 bytes).
2. Hex-decode to 16 bytes.
3. base64url-encode.

Example:

```
hex          = "69cfc57175bf211466038503"
padded       = "0000000069cfc57175bf211466038503"
bytes        = b"\x00\x00\x00\x00\x69\xcf\xc5\x71\x75\xbf\x21\x14\x66\x03\x85\x03"
base64url    = "AAAAAGnPxXF1vyEUZgOFAw=="
```

## Endpoint differences

The iOS app uses `POST https://app.velux-active.com/syncapi/v1/setstate`.
The legacy IngmarStein API client (and this fork until PR #1) used
`POST https://app.velux-active.com/api/setstate`. As of PR #1 the
client honours whatever `SET_STATE_URL` is configured in
`const.py`; both endpoints accept identical bodies.

## How signatures are pushed to the device

When the app first launches after install, it makes a normal
`setstate` request with `"retrieve_key": true` against the bridge
module. The Velux cloud responds `{"status": "ok"}` over HTTP and
**asynchronously delivers** the actual key over a separate channel —
likely the Netatmo NABU websocket bus at
`wss://app-ws.velux-active.com/ws/` — but only to the originally
paired device session.

This is the reason the integration cannot self-provision the key over
the REST API: from the cloud's perspective Home Assistant is not the
paired device. The user must extract the key from the app's storage
once (see `docs/EXTRACTING_SIGN_KEY.md`).

## Reverse-engineering credits

* `nougad/velux-cli` — first public protocol notes
* `ZTHawk/velux_active_patches` — APK smali patches that print the key to logcat
* `syepes/Hubitat` — Groovy port that confirmed the algorithm
* Live MITM capture by **PedroL**, 2026-05-22 — first independent confirmation of the wire format with an iOS app on iOS 26
