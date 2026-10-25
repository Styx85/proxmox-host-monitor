"""Repair flows for Proxmox Host Monitor."""

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from . import async_save
from .button_monitor import get_reboot_button
from .const import ACTION_BTRFS_REMOVE, CONF_REBOOT_BUTTON, EVENT_ALERT


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


class DiskRemovalRepairFlow(RepairsFlow):
    """Confirm removing a failing disk from its Btrfs pool."""

    def __init__(self, entry_id: str, serial: str, pool: str) -> None:
        """Initialize the repair flow."""
        self._entry_id = entry_id
        self._serial = serial
        self._pool = pool

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,
    ) -> FlowResult:
        """Start the repair flow."""
        return await self.async_step_confirm_removal()

    async def async_step_confirm_removal(
        self,
        user_input: dict[str, str] | None = None,
    ) -> FlowResult:
        """Queue the removal for the host after confirmation."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or not hasattr(entry, "runtime_data"):
            return self.async_abort(reason="entry_not_found")

        runtime_data = entry.runtime_data
        disk = runtime_data.disks.get(self._serial, {})
        pool = runtime_data.pools.get(self._pool)
        if pool is None:
            return self.async_abort(reason="pool_not_found")

        if user_input is not None:
            command = {
                "action": ACTION_BTRFS_REMOVE,
                "serial": self._serial,
                "pool": self._pool,
            }
            if command not in runtime_data.commands:
                runtime_data.commands.append(command)
            runtime_data.removals[self._serial] = "requested"
            async_save(entry)
            self.hass.bus.async_fire(
                EVENT_ALERT,
                {
                    "type": "removal_requested",
                    "disk": disk.get("name", ""),
                    "serial": self._serial,
                    "pool": pool["mount"],
                },
            )
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm_removal",
            data_schema=vol.Schema({}),
            description_placeholders={
                "disk": disk.get("name", ""),
                "model": disk.get("model") or "",
                "serial": self._serial,
                "pool": pool["mount"],
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the repair flow for an issue."""
    assert data is not None
    if issue_id.startswith("disk_failing_"):
        return DiskRemovalRepairFlow(
            str(data["entry_id"]), str(data["serial"]), str(data["pool"])
        )
    return RebootButtonRepairFlow(str(data["entry_id"]))
