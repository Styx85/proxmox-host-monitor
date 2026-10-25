"""Proxmox Host Monitor integration."""

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any

from aiohttp import web
from aiohttp.hdrs import METH_POST

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .button_monitor import async_setup_button_monitor, get_reboot_button
from .const import (
    CONF_WEBHOOK_ID,
    DISK_REPORT_TIMEOUT,
    DISK_STATUSES,
    DOMAIN,
    ISSUE_REBOOT_BUTTON_UNAVAILABLE,
    LEGACY_DOMAIN,
    SIGNAL_DISK_REPORT,
    SIGNAL_NEW_DISKS,
    SIGNAL_REBOOT_STATUS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .installer import async_register_install_view

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.UPDATE]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class ProxmoxHostRuntimeData:
    """Runtime state for Proxmox Host Monitor."""

    store: Store[dict[str, Any]]
    reboot_required: bool = False
    status_received: bool = False
    # Every disk ever reported, keyed by serial number.
    disks: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Serial numbers contained in the latest disk report.
    present: set[str] = field(default_factory=set)
    disks_reported_at: datetime | None = None
    node_device: dr.DeviceEntry | None = None
    cancel_disk_timeout: CALLBACK_TYPE | None = None

    @property
    def disks_current(self) -> bool:
        """Return whether the latest disk report is recent enough."""
        return (
            self.disks_reported_at is not None
            and dt_util.utcnow() - self.disks_reported_at < DISK_REPORT_TIMEOUT
        )

    def disk_available(self, serial: str) -> bool:
        """Return whether a disk was part of a recent report."""
        return self.disks_current and serial in self.present


type ProxmoxHostConfigEntry = ConfigEntry[ProxmoxHostRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the install script endpoint."""
    async_register_install_view(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
) -> bool:
    """Set up Proxmox Host Monitor from a config entry."""
    await _async_import_legacy_entry(hass, entry)

    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    runtime_data = ProxmoxHostRuntimeData(store=store)
    if stored := await store.async_load():
        runtime_data.disks = stored.get("disks", {})
        runtime_data.present = set(stored.get("present", []))
        if reported_at := stored.get("reported_at"):
            runtime_data.disks_reported_at = dt_util.parse_datetime(reported_at)
    runtime_data.node_device = async_entity_id_to_device(
        hass, get_reboot_button(entry)
    )
    entry.runtime_data = runtime_data

    webhook.async_register(
        hass,
        DOMAIN,
        "Proxmox Host Monitor",
        entry.data[CONF_WEBHOOK_ID],
        async_handle_webhook,
        local_only=True,
        allowed_methods=[METH_POST],
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_button_monitor(hass, entry)
    _schedule_disk_timeout(hass, entry)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
        runtime_data = entry.runtime_data
        if runtime_data.cancel_disk_timeout is not None:
            runtime_data.cancel_disk_timeout()
        # A reload creates a new store; write pending changes first.
        await runtime_data.store.async_save(_stored_data(runtime_data))

    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove the repair issue and stored disks together with the entry."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_REBOOT_BUTTON_UNAVAILABLE)
    await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing disks that are no longer reported."""
    runtime_data = entry.runtime_data
    serials = [
        identifier
        for domain, identifier in device_entry.identifiers
        if domain == DOMAIN
    ]
    if not serials or any(serial in runtime_data.present for serial in serials):
        return False

    for serial in serials:
        runtime_data.disks.pop(serial, None)
    _async_save_disks(entry)
    return True


async def _async_import_legacy_entry(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
) -> None:
    """Take over a Proxmox Reboot Update installation.

    The config flow copies its reboot button and webhook ID, so existing host
    scripts keep working. The update entity keeps its entity ID and history.
    """
    for legacy_entry in hass.config_entries.async_entries(LEGACY_DOMAIN):
        if legacy_entry.data.get(CONF_WEBHOOK_ID) != entry.data[CONF_WEBHOOK_ID]:
            continue

        # Unloading frees the webhook ID and the entities for migration.
        await hass.config_entries.async_unload(legacy_entry.entry_id)

        ent_reg = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(
            ent_reg, legacy_entry.entry_id
        ):
            ent_reg.async_update_entity_platform(
                entity.entity_id,
                DOMAIN,
                new_config_entry_id=entry.entry_id,
            )

        await hass.config_entries.async_remove(legacy_entry.entry_id)
        _LOGGER.info("Imported the Proxmox Reboot Update configuration")


def _stored_data(runtime_data: ProxmoxHostRuntimeData) -> dict[str, Any]:
    """Return the disk state that is persisted across restarts."""
    reported_at = runtime_data.disks_reported_at
    return {
        "disks": runtime_data.disks,
        "present": sorted(runtime_data.present),
        "reported_at": reported_at.isoformat() if reported_at else None,
    }


@callback
def _async_save_disks(entry: ProxmoxHostConfigEntry) -> None:
    """Persist the known disks so their entities survive a restart."""
    runtime_data = entry.runtime_data
    runtime_data.store.async_delay_save(lambda: _stored_data(runtime_data), 10)


@callback
def _schedule_disk_timeout(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
) -> None:
    """Refresh the disk entities once the latest report expires."""
    runtime_data = entry.runtime_data
    if runtime_data.cancel_disk_timeout is not None:
        runtime_data.cancel_disk_timeout()
        runtime_data.cancel_disk_timeout = None
    if not runtime_data.disks_current:
        return

    @callback
    def _expired(_now: datetime) -> None:
        runtime_data.cancel_disk_timeout = None
        async_dispatcher_send(hass, f"{SIGNAL_DISK_REPORT}_{entry.entry_id}")

    remaining = DISK_REPORT_TIMEOUT - (
        dt_util.utcnow() - runtime_data.disks_reported_at
    )
    runtime_data.cancel_disk_timeout = async_call_later(hass, remaining, _expired)


def _valid_disks(disks: Any) -> bool:
    """Return whether a disk report has the expected shape."""
    return isinstance(disks, list) and all(
        isinstance(disk, dict)
        and isinstance(disk.get("serial"), str)
        and disk["serial"]
        and isinstance(disk.get("name"), str)
        and disk.get("status") in DISK_STATUSES
        and isinstance(disk.get("problems", []), list)
        for disk in disks
    )


async def async_handle_webhook(
    hass: HomeAssistant,
    webhook_id: str,
    request: web.Request,
) -> web.Response:
    """Handle reboot status and disk health reports from the Proxmox host."""
    entry: ProxmoxHostConfigEntry | None = next(
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

    if not isinstance(payload, dict) or not payload.keys() & {"required", "disks"}:
        return web.json_response(
            {"error": "Payload must be a JSON object with 'required' or 'disks'"},
            status=400,
        )

    required = payload.get("required")
    if "required" in payload and not isinstance(required, bool):
        return web.json_response(
            {"error": "'required' must be true or false"},
            status=400,
        )

    disks = payload.get("disks")
    if "disks" in payload and not _valid_disks(disks):
        return web.json_response(
            {"error": "'disks' must be a list of disk objects"},
            status=400,
        )

    runtime_data = entry.runtime_data
    response: dict[str, Any] = {"ok": True}

    if required is not None:
        runtime_data.reboot_required = required
        runtime_data.status_received = True
        response["required"] = required
        async_dispatcher_send(hass, f"{SIGNAL_REBOOT_STATUS}_{entry.entry_id}")

    if disks is not None:
        new_serials = [
            disk["serial"] for disk in disks if disk["serial"] not in runtime_data.disks
        ]
        for disk in disks:
            runtime_data.disks[disk["serial"]] = disk
        runtime_data.present = {disk["serial"] for disk in disks}
        runtime_data.disks_reported_at = dt_util.utcnow()
        _async_save_disks(entry)
        _schedule_disk_timeout(hass, entry)
        response["disks"] = len(disks)

        if new_serials:
            async_dispatcher_send(
                hass, f"{SIGNAL_NEW_DISKS}_{entry.entry_id}", new_serials
            )
        async_dispatcher_send(hass, f"{SIGNAL_DISK_REPORT}_{entry.entry_id}")

    return web.json_response(response)
