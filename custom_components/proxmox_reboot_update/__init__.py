"""Proxmox Reboot Update integration."""

from dataclasses import dataclass

from aiohttp import web
from aiohttp.hdrs import METH_POST

from homeassistant.components import webhook
from homeassistant.components.webhook import async_generate_id as webhook_generate_id
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_WEBHOOK_ID,
    DOMAIN,
    LEGACY_CONF_SOURCE_ENTITY,
    SIGNAL_REBOOT_STATUS,
)

PLATFORMS = [Platform.UPDATE]


@dataclass(slots=True)
class ProxmoxRebootRuntimeData:
    """Runtime state for Proxmox Reboot Update."""

    reboot_required: bool = False
    status_received: bool = False


type ProxmoxRebootConfigEntry = ConfigEntry[ProxmoxRebootRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxRebootConfigEntry,
) -> bool:
    """Set up Proxmox Reboot Update from a config entry."""
    entry.runtime_data = ProxmoxRebootRuntimeData()

    webhook.async_register(
        hass,
        DOMAIN,
        "Proxmox Reboot Update",
        entry.data[CONF_WEBHOOK_ID],
        async_handle_webhook,
        local_only=True,
        allowed_methods=[METH_POST],
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ProxmoxRebootConfigEntry,
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])

    return unload_ok


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Migrate an old config entry to the integrated-webhook format."""
    if entry.version == 1:
        new_data = dict(entry.data)
        new_data.pop(LEGACY_CONF_SOURCE_ENTITY, None)
        new_data.setdefault(CONF_WEBHOOK_ID, webhook_generate_id())

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=2,
        )

    return True


async def async_handle_webhook(
    hass: HomeAssistant,
    webhook_id: str,
    request: web.Request,
) -> web.Response:
    """Handle reboot status reports from the Proxmox host."""
    entry = next(
        (
            config_entry
            for config_entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if config_entry.data.get(CONF_WEBHOOK_ID) == webhook_id
        ),
        None,
    )

    if entry is None:
        return web.Response(status=404)

    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not isinstance(payload, dict):
        return web.json_response(
            {"error": "Payload must be a JSON object"},
            status=400,
        )

    required = payload.get("required")
    if not isinstance(required, bool):
        return web.json_response(
            {"error": "'required' must be true or false"},
            status=400,
        )

    runtime_data: ProxmoxRebootRuntimeData = entry.runtime_data
    runtime_data.reboot_required = required
    runtime_data.status_received = True

    async_dispatcher_send(
        hass,
        f"{SIGNAL_REBOOT_STATUS}_{entry.entry_id}",
    )

    return web.json_response({"ok": True, "required": required})
