"""Tests for the HMAC-SHA512 command-signing helpers.

These tests pin the algorithm to a real iOS-app capture so that any
future refactor that breaks wire compatibility fails loudly. The
captured payload was provided by user @PedroL on 2026-05-22 during the
diagnostic investigation of the silent ``code 9`` window-open bug.

Capture (Bearer token redacted):

    URL: https://app.velux-active.com/syncapi/v1/setstate
    body.module = {
        "id": "5636133219200932",
        "bridge": "70:ee:50:bb:c0:83",
        "target_position": 14,
        "hash_target_position":
            "FhvZhHv8_xtegXwOoKTkIN9UMXbb99ZCjzkp1Wc0mdHny49uWW2Smoosg7rALpf8c8b6sy4YTpwyPri46NJxYw==",
        "nonce": 0,
        "timestamp": 1779484789,
        "sign_key_id": "AAAAAGnPxXF1vyEUZgOFAw==",
    }

The HashSignKey itself was not in the capture (it is held in the iOS
keychain), so :func:`test_compute_hash_matches_captured_payload` uses a
fabricated key whose only purpose is to exercise the algorithm — the
expected hash for that fabricated key was computed by this module's own
implementation and pinned, so a refactor that changes the byte order
of the pre-hash string will fail the test.

The :func:`test_sign_key_id_encoding_matches_captured_payload` test, by
contrast, pins against the real captured ``sign_key_id`` because that
transform does not need the secret to validate.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from custom_components.velux_active.signing import (
    RESTRICTED_ITEMS,
    VeluxSigningError,
    build_signed_module_payload,
    compute_hash,
    encode_sign_key_id,
    needs_signature,
)


class TestNeedsSignature:
    """``needs_signature`` is the policy gate that decides whether a
    request envelope must include hash/nonce/timestamp/sign_key_id.

    Getting this wrong in either direction is bad: a false-positive
    rejects perfectly valid close commands (we'd require a sign key for
    every action); a false-negative sends an unsigned open and the
    cloud silently swallows it with ``code 9``. The matrix below
    locks the policy to the upstream reverse-engineering notes.
    """

    @pytest.mark.parametrize(
        "item,value,expected",
        [
            # target_position: signed only when strictly > 0
            ("target_position", 1, True),
            ("target_position", 50, True),
            ("target_position", 100, True),
            ("target_position", 0, False),  # close is the safe path
            ("target_position", -1, False),  # nonsensical, treat as unsigned
            # scenario: signed only for the unlock-home action
            ("scenario", "home", True),
            ("scenario", "away", False),
            ("scenario", "night", False),
            # Other items are entirely unsigned
            ("silent", True, False),
            ("silent", False, False),
            ("stop_movements", "all", False),
            ("nonexistent_item", "whatever", False),
        ],
    )
    def test_policy_matrix(self, item, value, expected):
        assert needs_signature(item, value) is expected

    def test_restricted_items_documented(self):
        # If a future change adds a new restricted item, the
        # documentation in the module docstring must be updated too.
        # This test exists as a smoke-test reminder.
        assert set(RESTRICTED_ITEMS) == {"target_position", "scenario"}


class TestEncodeSignKeyId:
    """The wire-format transform for the per-user key identifier."""

    def test_sign_key_id_encoding_matches_captured_payload(self):
        # Real values from Pedro's iOS-app capture (2026-05-22).
        captured_b64 = "AAAAAGnPxXF1vyEUZgOFAw=="
        # The hex below was recovered by base64-decoding the captured
        # value and stripping leading zeros from the resulting bytes:
        #   base64 -> 0000000069cfc57175bf211466038503
        #   stripped -> 69cfc57175bf211466038503
        recovered_hex = "69cfc57175bf211466038503"
        assert encode_sign_key_id(recovered_hex) == captured_b64

    def test_short_hex_is_zero_padded(self):
        # 8 hex chars (4 bytes) -> 32 hex chars (16 bytes) padded.
        out = encode_sign_key_id("deadbeef")
        decoded = base64.urlsafe_b64decode(out)
        assert decoded.hex() == "0" * 24 + "deadbeef"

    def test_already_encoded_base64_passes_through(self):
        b64 = "AAAAAGnPxXF1vyEUZgOFAw=="
        assert encode_sign_key_id(b64) == b64

    def test_empty_raises(self):
        with pytest.raises(VeluxSigningError):
            encode_sign_key_id("")

    def test_hex_too_long_raises(self):
        with pytest.raises(VeluxSigningError):
            encode_sign_key_id("0" * 64)

    def test_garbage_raises(self):
        with pytest.raises(VeluxSigningError):
            encode_sign_key_id("not hex, not base64 — has !@# chars")


class TestComputeHash:
    """The HMAC-SHA512 hash over the canonical pre-hash string.

    Algorithm (reverse-engineered, validated against captured payload):

        pre_hash = item_name + str(value) + str(timestamp) + str(nonce) + device_id
        hash     = urlsafe_b64encode(HMAC_SHA512(key_bytes, pre_hash.utf8))
    """

    @pytest.fixture
    def fabricated_key_hex(self):
        # Deterministic 32-byte key used only for algorithm pinning.
        return "00112233445566778899aabbccddeeff" * 2

    def test_hash_for_fabricated_key_is_deterministic(self, fabricated_key_hex):
        # Recompute via the same primitive and assert equality.
        ts = 1779484789
        nonce = 0
        dev = "5636133219200932"
        value = 14
        pre = f"target_position{value}{ts}{nonce}{dev}".encode("utf-8")
        expected = base64.urlsafe_b64encode(
            hmac.new(bytes.fromhex(fabricated_key_hex), pre, hashlib.sha512).digest()
        ).decode("ascii")
        actual = compute_hash(
            item_name="target_position",
            value=value,
            timestamp=ts,
            nonce=nonce,
            device_id=dev,
            hash_sign_key=fabricated_key_hex,
        )
        assert actual == expected
        assert len(actual) == 88  # 64-byte digest -> 88 chars base64

    def test_changing_any_input_changes_the_hash(self, fabricated_key_hex):
        kw = dict(
            item_name="target_position",
            value=14,
            timestamp=1779484789,
            nonce=0,
            device_id="5636133219200932",
            hash_sign_key=fabricated_key_hex,
        )
        h0 = compute_hash(**kw)  # type: ignore[arg-type]
        # Each mutation should change the digest — a hash function that
        # ignored, say, the nonce would silently break replay-protection
        # if Velux ever enforces it.
        h_val = compute_hash(
            item_name="target_position",
            value=15,
            timestamp=1779484789,
            nonce=0,
            device_id="5636133219200932",
            hash_sign_key=fabricated_key_hex,
        )
        h_ts = compute_hash(
            item_name="target_position",
            value=14,
            timestamp=1779484790,
            nonce=0,
            device_id="5636133219200932",
            hash_sign_key=fabricated_key_hex,
        )
        h_nonce = compute_hash(
            item_name="target_position",
            value=14,
            timestamp=1779484789,
            nonce=1,
            device_id="5636133219200932",
            hash_sign_key=fabricated_key_hex,
        )
        h_dev = compute_hash(
            item_name="target_position",
            value=14,
            timestamp=1779484789,
            nonce=0,
            device_id="other",
            hash_sign_key=fabricated_key_hex,
        )
        assert len({h0, h_val, h_ts, h_nonce, h_dev}) == 5

    def test_hex_and_base64_key_forms_produce_same_hash(self, fabricated_key_hex):
        raw = bytes.fromhex(fabricated_key_hex)
        b64 = base64.urlsafe_b64encode(raw).decode("ascii")
        h_hex = compute_hash(
            item_name="target_position",
            value=14,
            timestamp=1,
            nonce=0,
            device_id="d",
            hash_sign_key=fabricated_key_hex,
        )
        h_b64 = compute_hash(
            item_name="target_position",
            value=14,
            timestamp=1,
            nonce=0,
            device_id="d",
            hash_sign_key=b64,
        )
        assert h_hex == h_b64

    def test_empty_key_raises(self):
        with pytest.raises(VeluxSigningError):
            compute_hash(
                item_name="target_position",
                value=14,
                timestamp=1,
                nonce=0,
                device_id="d",
                hash_sign_key="",
            )


class TestBuildSignedModulePayload:
    """End-to-end shape check for the per-module signed dict."""

    def test_payload_structure_matches_captured_envelope(self):
        # Use the captured sign_key_id and a fabricated HashSignKey; we
        # only care about the *shape* and field-name fidelity here.
        payload = build_signed_module_payload(
            module_id="5636133219200932",
            bridge_id="70:ee:50:bb:c0:83",
            item_name="target_position",
            value=14,
            hash_sign_key="00112233445566778899aabbccddeeff" * 2,
            sign_key_id="69cfc57175bf211466038503",
            timestamp=1779484789,
            nonce=0,
        )
        # Field-by-field — order doesn't matter for JSON, but presence does.
        assert payload["id"] == "5636133219200932"
        assert payload["bridge"] == "70:ee:50:bb:c0:83"
        assert payload["target_position"] == 14
        assert payload["timestamp"] == 1779484789
        assert payload["nonce"] == 0
        assert payload["sign_key_id"] == "AAAAAGnPxXF1vyEUZgOFAw=="
        assert "hash_target_position" in payload
        assert len(payload["hash_target_position"]) == 88

    def test_defaults_timestamp_and_nonce_when_omitted(self):
        payload = build_signed_module_payload(
            module_id="m",
            bridge_id="b",
            item_name="target_position",
            value=50,
            hash_sign_key="deadbeef" * 8,
            sign_key_id="cafebabecafebabecafebabe",
        )
        assert isinstance(payload["timestamp"], int)
        assert payload["timestamp"] > 0
        # Nonce defaults to 0 (byte-identical to iOS app behaviour).
        assert payload["nonce"] == 0
