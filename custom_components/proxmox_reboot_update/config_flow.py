"""Config flow for Proxmox Reboot Update."""

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.webhook import (
    async_generate_id as webhook_generate_id,
    async_generate_url as webhook_generate_url,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .button_monitor import get_reboot_button
from .const import CONF_REBOOT_BUTTON, CONF_WEBHOOK_ID, DOMAIN


class ProxmoxRebootUpdateConfigFlow(
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    """Handle a config flow for Proxmox Reboot Update."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reboot_button: str | None = None
        self._webhook_id: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "ProxmoxRebootUpdateOptionsFlow":
        """Return the options flow."""
        return ProxmoxRebootUpdateOptionsFlow()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle initial setup."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            self._reboot_button = user_input[CONF_REBOOT_BUTTON]
            self._webhook_id = webhook_generate_id()

            return await self.async_step_webhook()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_REBOOT_BUTTON
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="button"
                        )
                    ),
                }
            ),
        )

    async def async_step_webhook(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the generated webhook URL and finish setup."""
        if self._reboot_button is None or self._webhook_id is None:
            return self.async_abort(reason="setup_error")

        webhook_url = webhook_generate_url(
            self.hass,
            self._webhook_id,
        )

        if user_input is not None:
            return self.async_create_entry(
                title="Proxmox Reboot Update",
                data={
                    CONF_REBOOT_BUTTON: self._reboot_button,
                    CONF_WEBHOOK_ID: self._webhook_id,
                },
            )

        return self.async_show_form(
            step_id="webhook",
            data_schema=vol.Schema({}),
            description_placeholders={
                "webhook_url": webhook_url,
            },
        )


class ProxmoxRebootUpdateOptionsFlow(OptionsFlowWithReload):
    """Handle options for Proxmox Reboot Update."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the helper options."""
        webhook_url = webhook_generate_url(
            self.hass,
            self.config_entry.data[CONF_WEBHOOK_ID],
        )

        current_button = get_reboot_button(self.config_entry)

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_REBOOT_BUTTON:
                        user_input[CONF_REBOOT_BUTTON],
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_REBOOT_BUTTON,
                        default=current_button,
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="button"
                        )
                    ),
                }
            ),
            description_placeholders={
                "webhook_url": webhook_url,
            },
        )
