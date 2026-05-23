"""Tests for the Velux ACTIVE API client."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.velux_active.api import (
    VeluxActiveApi,
    VeluxActiveAuthError,
    VeluxActiveCommandError,
    VeluxActiveConnectionError,
)
from tests.conftest import (
    MOCK_CLIENT_ID,
    MOCK_CLIENT_SECRET,
    MOCK_HOME_ID,
    MOCK_MODULE_ID,
    MOCK_BRIDGE_ID,
    MOCK_PASSWORD,
    MOCK_TOKEN_DATA,
    MOCK_USERNAME,
)


def _make_mock_response(status: int, json_data: dict) -> MagicMock:
    """Create a mock aiohttp response."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.ok = status < 400
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def _make_api(
    session: MagicMock,
    *,
    hash_sign_key: str | None = None,
    sign_key_id: str | None = None,
) -> VeluxActiveApi:
    return VeluxActiveApi(
        session,
        MOCK_USERNAME,
        MOCK_PASSWORD,
        MOCK_CLIENT_ID,
        MOCK_CLIENT_SECRET,
        hash_sign_key=hash_sign_key,
        sign_key_id=sign_key_id,
    )


# A fabricated key + the captured-from-iOS sign_key_id, used purely to
# exercise the signed code path in unit tests. The HashSignKey is NOT
# a real one — see test_signing.py for the algorithm pinning.
_TEST_HASH_KEY = "00112233445566778899aabbccddeeff" * 2
_TEST_SIGN_KEY_ID = "69cfc57175bf211466038503"


class TestAuthentication:
    """Tests for VeluxActiveApi authentication."""

    @pytest.mark.asyncio
    async def test_authenticate_success(self) -> None:
        """Test successful authentication."""
        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(200, MOCK_TOKEN_DATA))
        api = _make_api(session)

        result = await api.async_authenticate()

        assert result["access_token"] == "mock_access_token"
        assert api.access_token == "mock_access_token"
        assert api.refresh_token == "mock_refresh_token"
        assert api.token_expires_at > time.time()

    @pytest.mark.asyncio
    async def test_authenticate_invalid_credentials(self) -> None:
        """Test authentication with invalid credentials raises VeluxActiveAuthError."""
        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(401, {}))
        api = _make_api(session)

        with pytest.raises(VeluxActiveAuthError):
            await api.async_authenticate()

    @pytest.mark.asyncio
    async def test_authenticate_connection_error(self) -> None:
        """Test that connection errors are wrapped."""
        import aiohttp

        session = MagicMock()
        session.post = MagicMock(side_effect=aiohttp.ClientError("timeout"))
        api = _make_api(session)

        with pytest.raises(VeluxActiveConnectionError):
            await api.async_authenticate()

    @pytest.mark.asyncio
    async def test_refresh_token_success(self) -> None:
        """Test successful token refresh."""
        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(200, MOCK_TOKEN_DATA))
        api = _make_api(session)
        api.restore_tokens("old_token", "old_refresh", time.time() - 1)

        await api.async_refresh_token()

        assert api.access_token == "mock_access_token"

    @pytest.mark.asyncio
    async def test_tokens_persisted_after_refresh(self) -> None:
        """Test token persistence callback after token refresh."""
        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(200, MOCK_TOKEN_DATA))
        on_tokens_updated = MagicMock()
        api = VeluxActiveApi(
            session,
            MOCK_USERNAME,
            MOCK_PASSWORD,
            MOCK_CLIENT_ID,
            MOCK_CLIENT_SECRET,
            on_tokens_updated=on_tokens_updated,
        )
        api.restore_tokens("old_token", "old_refresh", time.time() - 1)

        before = time.time()
        await api.async_refresh_token()
        after = time.time()

        assert on_tokens_updated.call_count == 1
        payload = on_tokens_updated.call_args.args[0]
        assert payload["access_token"] == MOCK_TOKEN_DATA["access_token"]
        assert payload["refresh_token"] == MOCK_TOKEN_DATA["refresh_token"]
        assert before <= payload["token_expires_at"] <= after + MOCK_TOKEN_DATA["expires_in"]

    @pytest.mark.asyncio
    async def test_tokens_persisted_after_authenticate(self) -> None:
        """Test token persistence callback after password grant."""
        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(200, MOCK_TOKEN_DATA))
        on_tokens_updated = MagicMock()
        api = VeluxActiveApi(
            session,
            MOCK_USERNAME,
            MOCK_PASSWORD,
            MOCK_CLIENT_ID,
            MOCK_CLIENT_SECRET,
            on_tokens_updated=on_tokens_updated,
        )

        before = time.time()
        await api.async_authenticate()
        after = time.time()

        assert on_tokens_updated.call_count == 1
        payload = on_tokens_updated.call_args.args[0]
        assert payload["access_token"] == MOCK_TOKEN_DATA["access_token"]
        assert payload["refresh_token"] == MOCK_TOKEN_DATA["refresh_token"]
        assert before <= payload["token_expires_at"] <= after + MOCK_TOKEN_DATA["expires_in"]

    @pytest.mark.asyncio
    async def test_concurrent_ensure_token_only_refreshes_once(self) -> None:
        """Test only one refresh request is done under concurrent callers."""
        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(200, MOCK_TOKEN_DATA))
        api = _make_api(session)
        api.restore_tokens("expired_token", "refresh", time.time() - 1)

        await asyncio.gather(api._ensure_token(), api._ensure_token())  # noqa: SLF001

        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_refresh_transient_network_error_raises_no_password_grant(
        self,
    ) -> None:
        """Test transient refresh errors bubble up without password grant."""
        session = MagicMock()
        session.post = MagicMock(
            side_effect=aiohttp.ClientConnectorError(MagicMock(), OSError("dns"))
        )
        api = _make_api(session)
        api.restore_tokens("expired_token", "refresh", time.time() - 1)

        with pytest.raises(VeluxActiveConnectionError):
            await api.async_refresh_token()

        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_refresh_400_falls_back_to_password_grant_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test refresh 400 fallback performs password grant and logs reason."""
        session = MagicMock()
        refresh_resp = _make_mock_response(400, {})
        auth_resp = _make_mock_response(200, MOCK_TOKEN_DATA)
        session.post = MagicMock(side_effect=[refresh_resp, auth_resp])
        api = _make_api(session)
        api.restore_tokens("old_token", "old_refresh", time.time() - 1)

        with caplog.at_level("INFO"):
            await api.async_refresh_token()

        assert "Falling back to password grant: refresh token rejected with HTTP 400" in caplog.text
        assert session.post.call_count == 2
        assert session.post.call_args_list[0].kwargs["data"]["grant_type"] == "refresh_token"
        assert session.post.call_args_list[1].kwargs["data"]["grant_type"] == "password"

    @pytest.mark.asyncio
    async def test_valid_restored_token_skips_refresh(self) -> None:
        """Test valid restored token does not trigger refresh on startup."""
        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(200, MOCK_TOKEN_DATA))
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api._ensure_token()  # noqa: SLF001

        assert session.post.call_count == 0

    @pytest.mark.asyncio
    async def test_refresh_token_expired_falls_back_to_password(self) -> None:
        """Test that a 401 on refresh falls back to password grant."""
        session = MagicMock()
        refresh_resp = _make_mock_response(401, {})
        auth_resp = _make_mock_response(200, MOCK_TOKEN_DATA)
        session.post = MagicMock(side_effect=[refresh_resp, auth_resp])
        api = _make_api(session)
        api.restore_tokens("old_token", "old_refresh", time.time() - 1)

        await api.async_refresh_token()

        assert api.access_token == "mock_access_token"

    def test_restore_tokens(self) -> None:
        """Test restoring tokens from stored data."""
        session = MagicMock()
        api = _make_api(session)
        expires_at = time.time() + 3600

        api.restore_tokens("access", "refresh", expires_at)

        assert api.access_token == "access"
        assert api.refresh_token == "refresh"
        assert api.token_expires_at == expires_at

    def test_token_valid_when_not_expired(self) -> None:
        """Test that token validity is checked correctly."""
        session = MagicMock()
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        assert api._is_token_valid() is True

    def test_token_invalid_when_expired(self) -> None:
        """Test that expired token is detected."""
        session = MagicMock()
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() - 1)

        assert api._is_token_valid() is False


class TestApiMethods:
    """Tests for VeluxActiveApi data fetching methods."""

    @pytest.mark.asyncio
    async def test_get_homes_data(self) -> None:
        """Test fetching homes data."""
        from tests.conftest import MOCK_HOMES_DATA

        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(200, MOCK_HOMES_DATA))
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        result = await api.async_get_homes_data()

        assert result["body"]["homes"][0]["id"] == MOCK_HOME_ID

    @pytest.mark.asyncio
    async def test_get_home_status(self) -> None:
        """Test fetching home status."""
        from tests.conftest import MOCK_HOME_STATUS

        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(200, MOCK_HOME_STATUS))
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        result = await api.async_get_home_status(MOCK_HOME_ID)

        modules = result["body"]["home"]["modules"]
        assert modules[0]["id"] == MOCK_MODULE_ID

    @pytest.mark.asyncio
    async def test_set_cover_position(self) -> None:
        """Test setting a cover position.

        Uses position=0 (close) because position>0 now requires the
        HMAC signing material (see signing.py + test_signing.py).
        The signed-path behaviour is asserted separately in
        :class:`TestSignedSetCoverPosition`.
        """
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_set_cover_position(MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, 0)

        assert session.post.called

    @pytest.mark.asyncio
    async def test_stop_movements(self) -> None:
        """Test stopping all movements."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_stop_movements(MOCK_HOME_ID, MOCK_BRIDGE_ID)

        assert session.post.called

    @pytest.mark.asyncio
    async def test_set_silent_mode(self) -> None:
        """Test setting silent mode."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_set_silent_mode(MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, True)

        assert session.post.called

    @pytest.mark.asyncio
    async def test_set_persons_away(self) -> None:
        """Test setting persons away."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_set_persons_away(MOCK_HOME_ID)

        assert session.post.called

    @pytest.mark.asyncio
    async def test_set_persons_home(self) -> None:
        """Test setting persons home."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_set_persons_home(MOCK_HOME_ID)

        assert session.post.called

    @pytest.mark.asyncio
    async def test_get_homes_data_auth_error(self) -> None:
        """Test that 403 raises VeluxActiveAuthError."""
        session = MagicMock()
        session.post = MagicMock(return_value=_make_mock_response(403, {}))
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        with pytest.raises(VeluxActiveAuthError):
            await api.async_get_homes_data()


class TestSetstateErrorSurfacing:
    """Regression tests for the silent-failure bug.

    The Velux cloud often returns HTTP 200 with a body describing a per-module
    rejection (or a non-ok top-level status). Earlier versions of this
    integration treated any HTTP 200 as success, which masked the root cause
    of "HA accepts the command but the actuator never moves". These tests
    pin the new behaviour: any per-command rejection must raise
    VeluxActiveCommandError so the user (and HA's service-call layer) sees it.
    """

    @pytest.mark.asyncio
    async def test_set_cover_position_raises_on_errors_array(self) -> None:
        """HTTP 200 with body.errors must raise VeluxActiveCommandError."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(
                200,
                {
                    "body": {
                        "errors": [
                            {"id": MOCK_MODULE_ID, "code": 6}
                        ]
                    },
                    "status": "ok",
                },
            )
        )
        api = _make_api(
            session,
            hash_sign_key=_TEST_HASH_KEY,
            sign_key_id=_TEST_SIGN_KEY_ID,
        )
        api.restore_tokens("token", "refresh", time.time() + 3600)

        with pytest.raises(VeluxActiveCommandError):
            await api.async_set_cover_position(
                MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, 75
            )

    @pytest.mark.asyncio
    async def test_set_cover_position_raises_on_non_ok_status(self) -> None:
        """HTTP 200 with status != 'ok' must raise VeluxActiveCommandError."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "rejected"})
        )
        api = _make_api(
            session,
            hash_sign_key=_TEST_HASH_KEY,
            sign_key_id=_TEST_SIGN_KEY_ID,
        )
        api.restore_tokens("token", "refresh", time.time() + 3600)

        with pytest.raises(VeluxActiveCommandError):
            await api.async_set_cover_position(
                MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, 50
            )

    @pytest.mark.asyncio
    async def test_set_cover_position_ok_body_still_succeeds(self) -> None:
        """A plain {\"status\":\"ok\"} body must NOT raise."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(
            session,
            hash_sign_key=_TEST_HASH_KEY,
            sign_key_id=_TEST_SIGN_KEY_ID,
        )
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_set_cover_position(
            MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, 50
        )

    @pytest.mark.asyncio
    async def test_stop_movements_raises_on_errors_array(self) -> None:
        """stop_movements must also surface per-bridge rejections."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(
                200,
                {"body": {"errors": [{"id": MOCK_BRIDGE_ID, "code": 13}]}},
            )
        )
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        with pytest.raises(VeluxActiveCommandError):
            await api.async_stop_movements(MOCK_HOME_ID, MOCK_BRIDGE_ID)

    @pytest.mark.asyncio
    async def test_set_silent_mode_raises_on_errors_array(self) -> None:
        """set_silent_mode must also surface per-module rejections."""
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(
                200,
                {"body": {"errors": [{"id": MOCK_MODULE_ID, "code": 6}]}},
            )
        )
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        with pytest.raises(VeluxActiveCommandError):
            await api.async_set_silent_mode(
                MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, True
            )

    def test_command_error_is_not_connection_error(self) -> None:
        """A per-command rejection must not be confused with a transport failure.

        The coordinator catches VeluxActiveConnectionError and turns it into an
        UpdateFailed; if VeluxActiveCommandError inherited from it, per-module
        rejections from setstate would be silently misclassified as connection
        failures. This regression test pins the separation.
        """
        assert not issubclass(VeluxActiveCommandError, VeluxActiveConnectionError)

    def test_extract_setstate_errors_is_public(self) -> None:
        """The helper is re-used by scripts/diag_setstate.py and tests."""
        from custom_components.velux_active.api import extract_setstate_errors

        assert extract_setstate_errors({"body": {"errors": [{"code": 6}]}}) == [
            {"code": 6}
        ]
        assert extract_setstate_errors({"errors": [{"code": 6}]}) == [{"code": 6}]
        assert extract_setstate_errors({"status": "ok"}) == []
        assert extract_setstate_errors(None) == []


class TestSignedSetCoverPosition:
    """Pin the signed-path behaviour for window-OPEN commands.

    These are unit tests at the API client boundary; the algorithm
    itself is pinned in tests/test_signing.py. Here we verify that:

    1. Opening (position > 0) without sign material raises a clear
       configuration error rather than hitting the cloud and getting a
       silent code-9 rejection.
    2. Opening with sign material adds the four required fields
       (timestamp, nonce, sign_key_id, hash_target_position) to the
       per-module dict on the wire.
    3. Closing (position == 0) never carries signature fields, even
       when sign material is configured — byte-identical to the
       legacy IngmarStein wire format.
    """

    @pytest.mark.asyncio
    async def test_open_without_sign_material_raises_clear_error(self) -> None:
        from custom_components.velux_active.signing import VeluxSigningError

        session = MagicMock()
        # Even though the cloud would 200, we should fail-fast locally.
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(session)  # no sign material
        api.restore_tokens("token", "refresh", time.time() + 3600)

        with pytest.raises(VeluxSigningError):
            await api.async_set_cover_position(
                MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, 50
            )
        # The HTTP layer should not have been touched.
        assert not session.post.called

    @pytest.mark.asyncio
    async def test_open_with_sign_material_sends_signed_payload(self) -> None:
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(
            session,
            hash_sign_key=_TEST_HASH_KEY,
            sign_key_id=_TEST_SIGN_KEY_ID,
        )
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_set_cover_position(
            MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, 50
        )

        # Recover the JSON we sent. aiohttp's session.post(json=...)
        # forwards to MagicMock so we inspect the kwargs.
        assert session.post.called
        _, kwargs = session.post.call_args
        body = kwargs["json"]
        module = body["home"]["modules"][0]
        # Required signed fields
        assert module["target_position"] == 50
        assert "hash_target_position" in module
        assert len(module["hash_target_position"]) == 88
        assert isinstance(module["timestamp"], int) and module["timestamp"] > 0
        assert module["nonce"] == 0
        assert module["sign_key_id"] == "AAAAAGnPxXF1vyEUZgOFAw=="

    @pytest.mark.asyncio
    async def test_close_never_carries_signature_fields(self) -> None:
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(
            session,
            hash_sign_key=_TEST_HASH_KEY,
            sign_key_id=_TEST_SIGN_KEY_ID,
        )
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_set_cover_position(
            MOCK_HOME_ID, MOCK_BRIDGE_ID, MOCK_MODULE_ID, 0
        )

        _, kwargs = session.post.call_args
        module = kwargs["json"]["home"]["modules"][0]
        assert module["target_position"] == 0
        # All signature fields MUST be absent for closes — the legacy
        # ha-velux-active wire format had no concept of them and Velux
        # accepts the unsigned form.
        for forbidden in (
            "hash_target_position",
            "sign_key_id",
            "nonce",
            "timestamp",
        ):
            assert forbidden not in module, (
                f"close command must not include {forbidden!r}"
            )

    @pytest.mark.asyncio
    async def test_shutter_open_without_sign_material_stays_unsigned(self) -> None:
        session = MagicMock()
        session.post = MagicMock(
            return_value=_make_mock_response(200, {"status": "ok"})
        )
        api = _make_api(session)
        api.restore_tokens("token", "refresh", time.time() + 3600)

        await api.async_set_cover_position(
            MOCK_HOME_ID,
            MOCK_BRIDGE_ID,
            MOCK_MODULE_ID,
            50,
            velux_type="shutter",
        )

        assert session.post.called
        _, kwargs = session.post.call_args
        module = kwargs["json"]["home"]["modules"][0]
        assert module["target_position"] == 50
        for forbidden in (
            "hash_target_position",
            "sign_key_id",
            "nonce",
            "timestamp",
        ):
            assert forbidden not in module
