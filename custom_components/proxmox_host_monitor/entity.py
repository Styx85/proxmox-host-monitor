"""Base entities for the disks reported by the Proxmox host."""

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ProxmoxHostConfigEntry
from .const import (
    DOMAIN,
    HOST_SCOPE,
    POOL_PREFIX,
    SIGNAL_DISK_REPORT,
    SIGNAL_NEW_DISKS,
    SIGNAL_NEW_POOLS,
)


def _via_node(entry: ProxmoxHostConfigEntry) -> dict[str, Any]:
    """Link a disk to the Proxmox node device.

    Newer Home Assistant versions replaced via_device by via_device_id.
    """
    node = entry.runtime_data.node_device
    if node is None:
        return {}
    if "via_device_id" in DeviceInfo.__annotations__:
        return {"via_device_id": node.id}
    return {"via_device": next(iter(node.identifiers))}


class ProxmoxHostEntity(Entity):
    """Entity updated by the disk reports of the Proxmox host."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: ProxmoxHostConfigEntry,
        description: EntityDescription,
    ) -> None:
        """Initialize the entity."""
        self._entry = entry
        self.entity_description = description

    # Key of the health change whose context the state is written in.
    _context_key: str | None = None

    async def async_added_to_hass(self) -> None:
        """Update the state whenever the host reports."""
        await super().async_added_to_hass()
        # Also the first state after a restart shows its reason.
        self._async_use_context()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_DISK_REPORT}_{self._entry.entry_id}",
                self._async_handle_report,
            )
        )

    @callback
    def _async_use_context(self) -> None:
        contexts = self._entry.runtime_data.contexts
        if self._context_key is not None and (context := contexts.get(self._context_key)):
            self.async_set_context(context)

    @callback
    def _async_handle_report(self) -> None:
        # The logbook then shows the health event with its reason as cause.
        self._async_use_context()
        self.async_write_ha_state()


class ProxmoxHostSummaryEntity(ProxmoxHostEntity):
    """Entity summarising all disks, attached to the Proxmox node."""

    def __init__(
        self,
        entry: ProxmoxHostConfigEntry,
        description: EntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry, description)
        self._attr_unique_id = description.key
        self._context_key = HOST_SCOPE
        if (node := entry.runtime_data.node_device) is not None:
            self.device_entry = node

    @property
    def available(self) -> bool:
        """Return whether a recent disk report exists."""
        return self._entry.runtime_data.disks_current

    @property
    def present_disks(self) -> list[dict[str, Any]]:
        """Return the disks of the latest report."""
        runtime_data = self._entry.runtime_data
        return [
            disk
            for serial, disk in runtime_data.disks.items()
            if serial in runtime_data.present
        ]


class ProxmoxDiskEntity(ProxmoxHostEntity):
    """Entity of a single disk, grouped in a device per disk."""

    def __init__(
        self,
        entry: ProxmoxHostConfigEntry,
        serial: str,
        description: EntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry, description)
        self._serial = serial
        self._context_key = serial
        self._attr_unique_id = f"disk_{serial}_{description.key}"

        model = self.disk.get("model") or "Disk"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"{model} {serial}",
            model=self.disk.get("model") or None,
            model_id=self.disk.get("model_family") or None,
            sw_version=self.disk.get("firmware") or None,
            serial_number=serial,
            **_via_node(entry),
        )

    @property
    def disk(self) -> dict[str, Any]:
        """Return the latest data of this disk."""
        return self._entry.runtime_data.disks[self._serial]

    @property
    def available(self) -> bool:
        """Return whether the disk was part of a recent report."""
        return self._entry.runtime_data.disk_available(self._serial)



class ProxmoxPoolEntity(ProxmoxHostEntity):
    """Entity of a Btrfs filesystem on the host."""

    def __init__(
        self,
        entry: ProxmoxHostConfigEntry,
        uuid: str,
        description: EntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry, description)
        self._uuid = uuid
        self._context_key = f"{POOL_PREFIX}{uuid}"
        self._attr_unique_id = f"{POOL_PREFIX}{uuid}_{description.key}"
        pool = self.pool
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{POOL_PREFIX}{uuid}")},
            name=f"Btrfs {pool.get('label') or pool['mount']}",
            model="Btrfs",
            **_via_node(entry),
        )

    @property
    def pool(self) -> dict[str, Any]:
        """Return the latest data of this filesystem."""
        return self._entry.runtime_data.pools[self._uuid]

    @property
    def member_disks(self) -> list[dict[str, Any]]:
        """Return the reported disks that belong to this filesystem."""
        runtime_data = self._entry.runtime_data
        serials = [device.get("serial") for device in self.pool.get("devices", [])]
        return [
            runtime_data.disks[serial]
            for serial in serials
            if serial in runtime_data.present
        ]

    @property
    def available(self) -> bool:
        """Return whether the filesystem was part of a recent report."""
        runtime_data = self._entry.runtime_data
        return runtime_data.disks_current and self._uuid in runtime_data.pools


@callback
def async_setup_pool_platform(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    async_add_entities: AddEntitiesCallback,
    pool_entities: Callable[[str], list[Entity]],
) -> None:
    """Add entities for known Btrfs filesystems and new ones as they appear."""

    @callback
    def _add_pools(uuids: list[str]) -> None:
        if entities := [entity for uuid in uuids for entity in pool_entities(uuid)]:
            async_add_entities(entities)

    _add_pools(list(entry.runtime_data.pools))
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{SIGNAL_NEW_POOLS}_{entry.entry_id}", _add_pools
        )
    )


@callback
def async_setup_disk_platform(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    async_add_entities: AddEntitiesCallback,
    summary_entities: Callable[[], list[Entity]],
    disk_entities: Callable[[str], list[Entity]],
) -> None:
    """Add entities for known disks and add missing ones after every report.

    Disks restored after a restart are created from the last stored report;
    values a newer host script reports later still get their entities.
    """
    added: set[str | None] = set()

    @callback
    def _add_disks(serials: list[str]) -> None:
        candidates: list[Entity] = []
        if serials:
            candidates.extend(summary_entities())
        for serial in serials:
            candidates.extend(disk_entities(serial))
        entities = [entity for entity in candidates if entity.unique_id not in added]
        added.update(entity.unique_id for entity in entities)
        if entities:
            async_add_entities(entities)

    @callback
    def _add_reported() -> None:
        _add_disks(sorted(entry.runtime_data.present))

    _add_disks(list(entry.runtime_data.disks))
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{SIGNAL_NEW_DISKS}_{entry.entry_id}", _add_disks
        )
    )
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{SIGNAL_DISK_REPORT}_{entry.entry_id}", _add_reported
        )
    )
