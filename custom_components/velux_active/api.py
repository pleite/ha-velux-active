"""Velux ACTIVE API client."""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from .const import (
    AUTH_URL,
    HOME_STATUS_URL,
    HOMES_DATA_URL,
    SET_STATE_URL,
    SET_PERSONS_AWAY_URL,
    SET_PERSONS_HOME_URL,
)
from .signing import (
    VeluxSigningError,
    build_signed_module_payload,
    needs_signature,
)

_LOGGER = logging.getLogger(__name__)


class VeluxActiveAuthError(Exception):
    """Authentication error."""


class VeluxActiveConnectionError(Exception):
    """Connection error."""


class VeluxActiveCommandError(Exception):
    """The cloud accepted the request (HTTP 200) but reported a per-command error.

    Velux's syncapi/v1/setstate frequently returns HTTP 200 with a body of the form
    ``{"body": {"errors": [...]}}`` or ``{"status": "<not-ok>"}`` when a command
    is rejected by the cloud or by the KIX 300 gateway. Previously these were
    silently treated as success, which masked the root cause of "command accepted
    but device never moves" bugs. This exception surfaces them.

    Note: this intentionally does NOT inherit from VeluxActiveConnectionError —
    the connection was fine; the cloud just refused the command. Conflating the
    two would mean any ``except VeluxActiveConnectionError`` handler (notably
    the one in the coordinator) would silently swallow per-module rejections
    as generic transport failures.
    """


def extract_setstate_errors(body: Any) -> list[Any]:
    """Return the list of per-command errors from a setstate response body, if any."""
    if not isinstance(body, dict):
        return []
    inner = body.get("body")
    if isinstance(inner, dict):
        errs = inner.get("errors")
        if isinstance(errs, list) and errs:
            return errs
    # Some Netatmo-compatible endpoints put errors at the top level
    top_errs = body.get("errors")
    if isinstance(top_errs, list) and top_errs:
        return top_errs
    return []


def _raise_for_setstate_body(action: str, status: int, body: Any) -> None:
    """Inspect a parsed setstate response body and raise if the cloud rejected it.

    Velux returns HTTP 200 on rejections, so we cannot rely on ``resp.ok`` alone.
    """
    _LOGGER.debug("%s response (HTTP %s): %s", action, status, body)
    errors = extract_setstate_errors(body)
    if errors:
        raise VeluxActiveCommandError(
            f"{action} rejected by Velux cloud: {errors}"
        )
    if isinstance(body, dict):
        top_status = body.get("status")
        if top_status is not None and top_status != "ok":
            raise VeluxActiveCommandError(
                f"{action} returned status={top_status!r}: {body}"
            )


class VeluxActiveApi:
    """Velux ACTIVE API client using OAuth2 password grant."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        client_id: str,
        client_secret: str,
        hash_sign_key: str | None = None,
        sign_key_id: str | None = None,
    ) -> None:
        """Initialize the API client.

        ``hash_sign_key`` and ``sign_key_id`` are optional. When both are
        provided, the client signs the commands the Velux cloud
        cryptographically requires (currently ``target_position > 0`` on
        window modules and ``scenario == "home"``). When either is
        missing, signed commands will raise :class:`VeluxSigningError`
        at request time — unsigned commands (close, shutter, stop,
        silent, scenario != "home") work either way. See
        ``custom_components/velux_active/signing.py`` for the protocol.
        """
        self._session = session
        self._username = username
        self._password = password
        self._client_id = client_id
        self._client_secret = client_secret
        self._hash_sign_key = hash_sign_key
        self._sign_key_id = sign_key_id
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def has_signing_material(self) -> bool:
        """Return True when signed commands can be built."""
        return bool(self._hash_sign_key and self._sign_key_id)

    def update_signing_material(
        self, hash_sign_key: str | None, sign_key_id: str | None
    ) -> None:
        """Replace the in-memory signing material at runtime.

        Called by the options flow when the user pastes (or rotates) the
        keys. Storing ``None`` for either field is allowed and disables
        signed commands.
        """
        self._hash_sign_key = hash_sign_key
        self._sign_key_id = sign_key_id

    @property
    def access_token(self) -> str | None:
        """Return the current access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    @property
    def token_expires_at(self) -> float:
        """Return the token expiry timestamp."""
        return self._token_expires_at

    def restore_tokens(
        self,
        access_token: str,
        refresh_token: str,
        token_expires_at: float,
    ) -> None:
        """Restore tokens from stored data."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = token_expires_at

    def _is_token_valid(self) -> bool:
        """Return True if the access token is still valid."""
        return (
            self._access_token is not None
            and time.time() < self._token_expires_at - 30
        )

    async def async_authenticate(self) -> dict[str, Any]:
        """Authenticate with username/password and return token data."""
        try:
            async with self._session.post(
                AUTH_URL,
                data={
                    "grant_type": "password",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "username": self._username,
                    "password": self._password,
                    "user_prefix": "velux",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status == 401:
                    raise VeluxActiveAuthError("Invalid credentials")
                if not resp.ok:
                    raise VeluxActiveConnectionError(
                        f"Authentication failed with status {resp.status}"
                    )
                data: dict[str, Any] = await resp.json()
        except aiohttp.ClientError as err:
            raise VeluxActiveConnectionError(
                f"Cannot connect to Velux ACTIVE: {err}"
            ) from err

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 10800)
        return data

    async def async_refresh_token(self) -> None:
        """Refresh the access token using the refresh token."""
        if self._refresh_token is None:
            await self.async_authenticate()
            return
        try:
            async with self._session.post(
                AUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status in (400, 401):
                    # Refresh token expired – fall back to password grant
                    await self.async_authenticate()
                    return
                if not resp.ok:
                    raise VeluxActiveConnectionError(
                        f"Token refresh failed with status {resp.status}"
                    )
                data: dict[str, Any] = await resp.json()
        except aiohttp.ClientError as err:
            raise VeluxActiveConnectionError(
                f"Cannot connect to Velux ACTIVE: {err}"
            ) from err

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 10800)

    async def _ensure_token(self) -> None:
        """Ensure we have a valid access token."""
        if not self._is_token_valid():
            await self.async_refresh_token()

    async def async_get_homes_data(self) -> dict[str, Any]:
        """Fetch homes and modules data."""
        await self._ensure_token()
        try:
            async with self._session.post(
                HOMES_DATA_URL,
                data={"access_token": self._access_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status == 403:
                    raise VeluxActiveAuthError("Access denied")
                if not resp.ok:
                    raise VeluxActiveConnectionError(
                        f"Failed to get homes data: {resp.status}"
                    )
                return await resp.json()
        except aiohttp.ClientError as err:
            raise VeluxActiveConnectionError(
                f"Cannot connect to Velux ACTIVE: {err}"
            ) from err

    async def async_get_home_status(self, home_id: str) -> dict[str, Any]:
        """Fetch the current status of a home."""
        await self._ensure_token()
        try:
            async with self._session.post(
                HOME_STATUS_URL,
                json={"home_id": home_id},
                headers={"Authorization": f"Bearer {self._access_token}"},
            ) as resp:
                if resp.status == 403:
                    raise VeluxActiveAuthError("Access denied")
                if not resp.ok:
                    raise VeluxActiveConnectionError(
                        f"Failed to get home status: {resp.status}"
                    )
                return await resp.json()
        except aiohttp.ClientError as err:
            raise VeluxActiveConnectionError(
                f"Cannot connect to Velux ACTIVE: {err}"
            ) from err

    async def async_set_cover_position(
        self, home_id: str, bridge_id: str, module_id: str, position: int
    ) -> None:
        """Set the target position of a cover module (0–100).

        Window modules (and other safety-restricted devices) require an
        HMAC-SHA512 signature on any ``position > 0`` command — without
        it the cloud silently rejects the request with
        ``{"errors": [{"code": 9, ...}]}`` and the device never moves.
        See ``custom_components/velux_active/signing.py``.

        We only attempt to sign when :func:`needs_signature` returns
        True; closing (``position == 0``) and roller-shutter moves stay
        unsigned for byte-identical wire compatibility with the
        long-standing IngmarStein code path.
        """
        await self._ensure_token()
        if needs_signature("target_position", position):
            if not self.has_signing_material:
                raise VeluxSigningError(
                    "This command (target_position > 0 on a window module) "
                    "requires HashSignKey and SignKeyId to be configured. "
                    "See docs/EXTRACTING_SIGN_KEY.md."
                )
            module_payload = build_signed_module_payload(
                module_id=module_id,
                bridge_id=bridge_id,
                item_name="target_position",
                value=position,
                hash_sign_key=self._hash_sign_key,  # type: ignore[arg-type]
                sign_key_id=self._sign_key_id,  # type: ignore[arg-type]
            )
            # The iOS app also sends app_identifier; harmless but
            # included for wire-fidelity with captured traffic.
            envelope: dict[str, Any] = {
                "app_identifier": "app_velux",
                "home": {"id": home_id, "modules": [module_payload]},
            }
        else:
            envelope = {
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
        try:
            async with self._session.post(
                SET_STATE_URL,
                json=envelope,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self._access_token}",
                },
            ) as resp:
                if resp.status == 403:
                    raise VeluxActiveAuthError("Access denied")
                if not resp.ok:
                    raise VeluxActiveConnectionError(
                        f"Failed to set cover position: {resp.status}"
                    )
                # Velux returns HTTP 200 even when the gateway rejects the
                # command; inspect the body and raise VeluxActiveCommandError
                # if there is a per-module error or non-ok status.
                try:
                    body = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                _raise_for_setstate_body(
                    f"set_cover_position(module={module_id}, pos={position}, "
                    f"signed={needs_signature('target_position', position)})",
                    resp.status,
                    body,
                )
        except aiohttp.ClientError as err:
            raise VeluxActiveConnectionError(
                f"Cannot connect to Velux ACTIVE: {err}"
            ) from err

    async def async_set_silent_mode(
        self, home_id: str, bridge_id: str, module_id: str, silent: bool
    ) -> None:
        """Set the silent mode of a module."""
        await self._ensure_token()
        payload = {
            "home": {
                "id": home_id,
                "modules": [
                    {
                        "bridge": bridge_id,
                        "id": module_id,
                        "silent": silent,
                    }
                ],
            }
        }
        try:
            async with self._session.post(
                SET_STATE_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self._access_token}",
                },
            ) as resp:
                if resp.status == 403:
                    raise VeluxActiveAuthError("Access denied")
                if not resp.ok:
                    raise VeluxActiveConnectionError(
                        f"Failed to set silent mode: {resp.status}"
                    )
                try:
                    body = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                _raise_for_setstate_body(
                    f"set_silent_mode(module={module_id}, silent={silent})",
                    resp.status,
                    body,
                )
        except aiohttp.ClientError as err:
            raise VeluxActiveConnectionError(
                f"Cannot connect to Velux ACTIVE: {err}"
            ) from err

    async def async_stop_movements(self, home_id: str, bridge_id: str) -> None:
        """Stop all movements on the given bridge."""
        await self._ensure_token()
        payload = {
            "home": {
                "id": home_id,
                "modules": [
                    {
                        "id": bridge_id,
                        "stop_movements": "all",
                    }
                ],
            }
        }
        try:
            async with self._session.post(
                SET_STATE_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self._access_token}",
                },
            ) as resp:
                if resp.status == 403:
                    raise VeluxActiveAuthError("Access denied")
                if not resp.ok:
                    raise VeluxActiveConnectionError(
                        f"Failed to stop movements: {resp.status}"
                    )
                try:
                    body = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                _raise_for_setstate_body(
                    f"stop_movements(bridge={bridge_id})",
                    resp.status,
                    body,
                )
        except aiohttp.ClientError as err:
            raise VeluxActiveConnectionError(
                f"Cannot connect to Velux ACTIVE: {err}"
            ) from err
