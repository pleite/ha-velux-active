"""HMAC-SHA512 command signing for Velux ACTIVE.

The Velux ACTIVE cloud requires safety-critical commands on window modules
(``velux_type == "window"``) to be cryptographically signed. Unsigned
``target_position > 0`` commands are silently accepted (HTTP 200) but
rejected per-module with ``{"errors": [{"code": 9, "id": "<module>"}]}``,
and the physical window never moves. This was the root cause of the
"command accepted but window does not move" bug. See
``docs/SIGNING.md`` for the full protocol breakdown.

The algorithm was reverse-engineered by two independent projects whose
results we cross-validated against a real iOS app capture:

* `syepes/Hubitat`_ — Groovy driver for the same scheme
* `ZTHawk/velux_active_patches`_ — Android smali patches that expose the
  signing material via logcat (see ``docs/EXTRACTING_SIGN_KEY.md``)

.. _syepes/Hubitat: https://github.com/syepes/Hubitat/blob/master/Drivers/Netatmo/Netatmo%20-%20Velux%20-%20Gateway.groovy
.. _ZTHawk/velux_active_patches: https://github.com/ZTHawk/velux_active_patches

Hash spec
=========

For each restricted command, the app builds a concatenated string and
HMAC-SHA512s it with the per-user ``HashSignKey`` (a raw byte secret
provisioned during pairing and exchanged out-of-band; we do not have a
way to obtain it from the public API)::

    pre_hash = item_name + str(value) + str(timestamp_seconds) + str(nonce) + device_id

    hash_b64 = urlsafe_b64encode(
        hmac_sha512(HashSignKey, pre_hash.encode("utf-8"))
    )

The base64 alphabet uses ``-`` and ``_`` (URL-safe) instead of ``+`` and
``/``, with standard ``=`` padding preserved (an 88-char string).

The ``sign_key_id`` field sent alongside the hash is::

    sign_key_id = urlsafe_b64encode(
        bytes.fromhex(SignKeyId.zfill(32))
    )

where ``SignKeyId`` is the hex string the app received with the key.

Restricted commands today (per upstream protocol notes):

* ``target_position`` — only when ``value > 0`` (close is unsigned)
* ``scenario`` — only when ``value == "home"`` (unlock-home action)

Closing a window, opening/closing a shutter, and stop/silent commands are
all unsigned and therefore work without ``HashSignKey`` configured.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Items the Velux cloud enforces a signature on. Mapping value -> sentinel
# helper that returns whether a given value triggers the signature
# requirement. See module docstring for sources.
RESTRICTED_ITEMS: dict[str, Any] = {
    "target_position": lambda v: isinstance(v, int) and v > 0,
    "scenario": lambda v: v == "home",
}


class VeluxSigningError(Exception):
    """Raised when the signing material is malformed or missing.

    This is *not* a runtime command failure — it indicates a configuration
    problem (missing/garbled ``HashSignKey`` or ``SignKeyId``) that the
    user must fix in the integration's options before signed commands can
    succeed. We surface it as a distinct exception so the coordinator can
    distinguish "we tried but couldn't even build the request" from "we
    sent a request and the cloud rejected it".
    """


def needs_signature(item_name: str, value: Any) -> bool:
    """Return True if this (item_name, value) pair requires a signature.

    Examples
    --------
    >>> needs_signature("target_position", 50)
    True
    >>> needs_signature("target_position", 0)  # close is unsigned
    False
    >>> needs_signature("silent", True)        # silent mode is unsigned
    False
    """
    predicate = RESTRICTED_ITEMS.get(item_name)
    if predicate is None:
        return False
    return bool(predicate(value))


def _decode_hash_sign_key(hash_sign_key: str) -> bytes:
    """Decode the ``HashSignKey`` from its persisted form into raw bytes.

    The reverse-engineering projects log the key as a hex string. We
    accept both hex and (url-safe) base64 forms because users may grab
    the value from logcat (hex) or from a keychain dump (base64). The
    hex form is canonical.
    """
    cleaned = hash_sign_key.strip()
    if not cleaned:
        raise VeluxSigningError("HashSignKey is empty")
    # Try hex first — the canonical form from ZTHawk patches.
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        pass
    # Fall back to base64 (url-safe or standard) for keychain-dumped
    # values. We do not silently strip whitespace inside the string
    # because that could mask a real corruption.
    try:
        # Pad for standard b64 if needed.
        padded = cleaned + "=" * (-len(cleaned) % 4)
        return base64.urlsafe_b64decode(padded)
    except (ValueError, binascii.Error) as err:
        raise VeluxSigningError(
            "HashSignKey must be hex or base64 — got a value that is neither"
        ) from err


def encode_sign_key_id(sign_key_id_hex: str) -> str:
    """Convert the hex ``SignKeyId`` into the wire-format ``sign_key_id``.

    The app pads the hex string left with zeros to 32 chars (16 bytes),
    hex-decodes, and url-safe base64-encodes the result. We validated
    this transform against a real iOS app capture::

        "69cfc57175bf211466038503"
            -> "0000000069cfc57175bf211466038503"
            -> bytes.fromhex(...)
            -> "AAAAAGnPxXF1vyEUZgOFAw=="

    Accepts either the bare hex or an already-encoded base64 value
    (pass-through), so a user who copied the value from a MITM capture
    rather than from logcat does not have to convert it themselves.
    """
    cleaned = sign_key_id_hex.strip()
    if not cleaned:
        raise VeluxSigningError("SignKeyId is empty")
    # Already wire-format? Heuristic: contains '=' or '/' or '_' or '-'
    # and is not pure hex.
    is_hex = all(c in "0123456789abcdefABCDEF" for c in cleaned)
    if not is_hex:
        # Trust it as already-encoded base64; round-trip to validate.
        try:
            padded = cleaned + "=" * (-len(cleaned) % 4)
            base64.urlsafe_b64decode(padded)
        except (ValueError, binascii.Error) as err:
            raise VeluxSigningError(
                "SignKeyId looks neither like hex nor like base64"
            ) from err
        return cleaned
    if len(cleaned) > 32:
        raise VeluxSigningError(
            f"SignKeyId hex is too long ({len(cleaned)} chars > 32)"
        )
    padded_hex = cleaned.rjust(32, "0")
    return base64.urlsafe_b64encode(bytes.fromhex(padded_hex)).decode("ascii")


def compute_hash(
    item_name: str,
    value: Any,
    timestamp: int,
    nonce: int,
    device_id: str,
    hash_sign_key: str,
) -> str:
    """Compute the HMAC-SHA512 hash for a signed Velux command.

    The pre-hash string is the lexical concatenation of
    ``item_name + value + timestamp + nonce + device_id`` with no
    separator (we verified this against the captured payload — Pedro's
    sample: ``target_position`` + ``14`` + ``1779484789`` + ``0`` +
    ``5636133219200932`` → matched ``hash_target_position`` value).
    """
    key = _decode_hash_sign_key(hash_sign_key)
    pre_hash = f"{item_name}{value}{timestamp}{nonce}{device_id}"
    digest = hmac.new(key, pre_hash.encode("utf-8"), hashlib.sha512).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def build_signed_module_payload(
    *,
    module_id: str,
    bridge_id: str,
    item_name: str,
    value: Any,
    hash_sign_key: str,
    sign_key_id: str,
    timestamp: int | None = None,
    nonce: int | None = None,
) -> dict[str, Any]:
    """Build the per-module dict for a signed setstate request.

    The returned dict is meant to be embedded into the outer
    ``{"home": {"id": ..., "modules": [<this>]}}`` envelope by the
    caller.

    Caller MUST first check :func:`needs_signature` and skip this helper
    when the command does not require a signature, because an unsigned
    request with these fields *also* gets rejected (the cloud appears to
    validate the hash unconditionally when ``hash_<item>`` is present).
    """
    ts = timestamp if timestamp is not None else int(time.time())
    nc = nonce if nonce is not None else 0
    hash_value = compute_hash(
        item_name=item_name,
        value=value,
        timestamp=ts,
        nonce=nc,
        device_id=module_id,
        hash_sign_key=hash_sign_key,
    )
    payload: dict[str, Any] = {
        "id": module_id,
        "bridge": bridge_id,
        item_name: value,
        f"hash_{item_name}": hash_value,
        "timestamp": ts,
        "nonce": nc,
        "sign_key_id": encode_sign_key_id(sign_key_id),
    }
    return payload


def generate_nonce() -> int:
    """Return a fresh nonce for a signed request.

    The iOS app uses ``nonce = 0`` in every observed capture; the cloud
    appears to tolerate any integer as long as it matches the hash
    input. We default to ``0`` in :func:`build_signed_module_payload`
    for byte-identical wire compatibility with the app, but expose this
    helper so a future replay-protection hardening can switch to fresh
    nonces without touching the call sites.
    """
    return secrets.randbits(32)
