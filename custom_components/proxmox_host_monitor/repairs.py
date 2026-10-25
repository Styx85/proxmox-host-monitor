"""Repair flows for Proxmox Host Monitor."""

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .button_monitor import get_reboot_button
from .const import CONF_REBOOT_BUTTON


class RebootButtonRepairFlow(RepairsFlow):
    """Let the user pick a working Proxmox reboot button."""

    def __init__(self, entry_id: str) -> None:
        """Initialize the repair flow."""
        self._entry_id = entry_id

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,
    ) -> FlowResult:
        """Start the repair flow."""
        return await self.async_step_select_button()

    async def async_step_select_button(
        self,
        user_input: dict[str, str] | None = None,
    ) -> FlowResult:
        """Select a new reboot button and reload the integration."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        current_button = get_reboot_button(entry)

        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                entry,
                options={
                    **entry.options,
                    CONF_REBOOT_BUTTON: user_input[CONF_REBOOT_BUTTON],
                },
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="select_button",
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
                "entity_id": current_button,
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the repair flow for a broken reboot button."""
    assert data is not None
    return RebootButtonRepairFlow(str(data["entry_id"]))
