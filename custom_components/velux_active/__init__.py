"""The Velux ACTIVE integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VeluxActiveApi, VeluxActiveAuthError, VeluxActiveConnectionError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HASH_SIGN_KEY,
    CONF_SIGN_KEY_ID,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    DOMAIN,
)
from .coordinator import VeluxActiveCoordinator
from .websocket import VeluxActiveWebsocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER, Platform.SENSOR, Platform.SWITCH, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Velux ACTIVE from a config entry."""
    username: str = entry.data[CONF_USERNAME]
    password: str = entry.data[CONF_PASSWORD]
    client_id: str = entry.data.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID)
    client_secret: str = entry.data.get(CONF_CLIENT_SECRET, DEFAULT_CLIENT_SECRET)
    # Signing material lives in options (added via options flow) so the
    # user can paste it without re-running the config flow. We fall back
    # to entry.data for backwards-compat with any out-of-band install
    # that hand-edited the entry.
    hash_sign_key: str | None = (
        entry.options.get(CONF_HASH_SIGN_KEY)
        or entry.data.get(CONF_HASH_SIGN_KEY)
    )
    sign_key_id: str | None = (
        entry.options.get(CONF_SIGN_KEY_ID)
        or entry.data.get(CONF_SIGN_KEY_ID)
    )

    session = async_get_clientsession(hass)
    api = VeluxActiveApi(
        session,
        username,
        password,
        client_id,
        client_secret,
        hash_sign_key=hash_sign_key,
        sign_key_id=sign_key_id,
    )

    # Restore cached tokens if available
    if token_data := entry.data.get("token_data"):
        api.restore_tokens(
            token_data["access_token"],
            token_data["refresh_token"],
            token_data["token_expires_at"],
        )

    home_id: str = entry.data["home_id"]
    coordinator = VeluxActiveCoordinator(hass, api, home_id)

    await coordinator.async_config_entry_first_refresh()

    # Best-effort real-time push channel. We wire it up *after* the
    # first poll succeeded so the entity registry is fully populated;
    # an offline websocket never blocks setup (the polling loop is the
    # source of truth).
    async def _token_provider() -> str:
        # Force a refresh check so we never subscribe with an expired
        # bearer. The API client handles the actual refresh logic.
        await api._ensure_token()  # noqa: SLF001 — intentional reuse
        token = api.access_token
        if not token:
            raise RuntimeError("Velux API has no access token to subscribe with")
        return token

    async def _on_push(event: dict[str, Any]) -> None:
        # Cheap fan-out: every push triggers a coordinator refresh so
        # the existing data plumbing stays the single source of truth.
        # When event parsing matures we can apply deltas in-process
        # and skip the HTTP round-trip; for now the refresh latency
        # (~200 ms) is still 300x faster than the 60 s polling floor.
        _LOGGER.debug("Velux push received, triggering refresh: %s", event)
        await coordinator.async_request_refresh()

    websocket = VeluxActiveWebsocket(session, _token_provider)
    websocket.register_callback(_on_push)
    await websocket.async_start()

    # Stash auxiliary objects on the coordinator so existing platforms
    # that do ``hass.data[DOMAIN][entry.entry_id]`` and treat the value
    # as a coordinator keep working without modification.
    coordinator.websocket = websocket  # type: ignore[attr-defined]
    coordinator.api = api  # type: ignore[attr-defined]
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # React to options-flow changes (e.g. user pastes a new sign key)
    # without forcing a full reload of the integration. The websocket
    # subscription keeps running; we just swap the signing material on
    # the live API client.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Apply options-flow changes to the live API client."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not coordinator:
        return
    api: VeluxActiveApi = coordinator.api  # type: ignore[attr-defined]
    api.update_signing_material(
        entry.options.get(CONF_HASH_SIGN_KEY)
        or entry.data.get(CONF_HASH_SIGN_KEY),
        entry.options.get(CONF_SIGN_KEY_ID)
        or entry.data.get(CONF_SIGN_KEY_ID),
    )
    _LOGGER.info(
        "Velux signing material updated (configured=%s)",
        api.has_signing_material,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        websocket: VeluxActiveWebsocket | None = getattr(
            coordinator, "websocket", None
        )
        if websocket is not None:
            await websocket.async_stop()
    return unload_ok
