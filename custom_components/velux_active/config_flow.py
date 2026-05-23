"""Config flow for the Velux ACTIVE integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import VeluxActiveApi, VeluxActiveAuthError, VeluxActiveConnectionError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HASH_SIGN_KEY,
    CONF_SIGN_KEY_ID,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .signing import VeluxSigningError, validate_signing_material

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)

STEP_ADVANCED_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CLIENT_ID, default=DEFAULT_CLIENT_ID): str,
        vol.Optional(CONF_CLIENT_SECRET, default=DEFAULT_CLIENT_SECRET): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class VeluxActiveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Velux ACTIVE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._username: str = ""
        self._password: str = ""
        self._client_id: str = DEFAULT_CLIENT_ID
        self._client_secret: str = DEFAULT_CLIENT_SECRET
        self._homes: list[dict[str, Any]] = []
        self._api: VeluxActiveApi | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step (username & password)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                homes, api = await self._async_validate_credentials(
                    self._username,
                    self._password,
                    self._client_id,
                    self._client_secret,
                )
            except VeluxActiveAuthError:
                errors["base"] = "invalid_auth"
            except VeluxActiveConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during config flow")
                errors["base"] = "unknown"
            else:
                self._homes = homes
                self._api = api
                if len(homes) == 1:
                    return await self._async_create_entry(homes[0])
                return await self.async_step_select_home()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_select_home(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user select a home when there are multiple."""
        if user_input is not None:
            home_id = user_input["home_id"]
            home = next(h for h in self._homes if h["id"] == home_id)
            return await self._async_create_entry(home)

        home_options = {h["id"]: h.get("name", h["id"]) for h in self._homes}
        return self.async_show_form(
            step_id="select_home",
            data_schema=vol.Schema(
                {vol.Required("home_id"): vol.In(home_options)}
            ),
        )

    async def _async_create_entry(self, home: dict[str, Any]) -> ConfigFlowResult:
        """Create the config entry for the given home."""
        await self.async_set_unique_id(home["id"])
        self._abort_if_unique_id_configured()

        entry_data = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_CLIENT_ID: self._client_id,
            CONF_CLIENT_SECRET: self._client_secret,
            "home_id": home["id"],
        }
        
        if self._api:
            entry_data["token_data"] = {
                "access_token": self._api.access_token,
                "refresh_token": self._api.refresh_token,
                "token_expires_at": self._api.token_expires_at,
            }

        return self.async_create_entry(
            title=home.get("name", home["id"]),
            data=entry_data,
        )

    async def _async_validate_credentials(
        self,
        username: str,
        password: str,
        client_id: str,
        client_secret: str,
    ) -> tuple[list[dict[str, Any]], VeluxActiveApi]:
        """Validate credentials and return list of homes and api client."""
        session = async_get_clientsession(self.hass)
        api = VeluxActiveApi(session, username, password, client_id, client_secret)
        await api.async_authenticate()
        homes_data = await api.async_get_homes_data()
        homes: list[dict[str, Any]] = homes_data.get("body", {}).get("homes", [])
        if not homes:
            raise VeluxActiveConnectionError("No homes found")
        return homes, api

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return VeluxActiveOptionsFlow(config_entry)


class VeluxActiveOptionsFlow(OptionsFlow):
    """Handle options for Velux ACTIVE."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # Validate the signing material if either field was provided.
            # We accept "both empty" (signing disabled) or "both present
            # and parse-clean"; rejecting "one set, one empty" prevents
            # silent half-configurations.
            hash_key = (user_input.get(CONF_HASH_SIGN_KEY) or "").strip()
            sign_id = (user_input.get(CONF_SIGN_KEY_ID) or "").strip()
            normalized_sign_id = sign_id
            if bool(hash_key) ^ bool(sign_id):
                errors["base"] = "sign_material_partial"
            elif hash_key and sign_id:
                try:
                    _, normalized_sign_id = validate_signing_material(
                        hash_key, sign_id
                    )
                except VeluxSigningError as err:
                    _LOGGER.warning("Invalid sign material: %s", err)
                    if "expected 32" in str(err) or "expected 16" in str(err):
                        errors["base"] = "sign_material_bad_length"
                    else:
                        errors["base"] = "sign_material_invalid"
            if not errors:
                # Store normalised empty-string-to-absent so the API
                # client's ``has_signing_material`` check stays simple.
                cleaned = {
                    "update_interval": user_input.get(
                        "update_interval", UPDATE_INTERVAL
                    ),
                }
                if hash_key:
                    cleaned[CONF_HASH_SIGN_KEY] = hash_key
                if sign_id:
                    cleaned[CONF_SIGN_KEY_ID] = normalized_sign_id
                return self.async_create_entry(title="", data=cleaned)

        current_interval = self._config_entry.options.get(
            "update_interval", UPDATE_INTERVAL
        )
        current_hash_key = self._config_entry.options.get(
            CONF_HASH_SIGN_KEY, ""
        )
        current_sign_id = self._config_entry.options.get(
            CONF_SIGN_KEY_ID, ""
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "update_interval", default=current_interval
                    ): vol.All(int, vol.Range(min=10, max=3600)),
                    vol.Optional(
                        CONF_HASH_SIGN_KEY, default=current_hash_key
                    ): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(
                        CONF_SIGN_KEY_ID, default=current_sign_id
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "extracting_guide": (
                    "See docs/EXTRACTING_SIGN_KEY.md for how to obtain "
                    "HashSignKey and SignKeyId."
                )
            },
        )
