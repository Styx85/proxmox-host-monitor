"""Base entities for the disks reported by the Proxmox host."""

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ProxmoxHostConfigEntry
from .const import DOMAIN, SIGNAL_DISK_REPORT, SIGNAL_NEW_DISKS


def format_problems(disk: dict[str, Any]) -> list[str]:
    """Return the problems of a disk as short, language-neutral strings."""
    return [
        problem["code"]
        if problem.get("value") is None
        else f"{problem['code']}: {problem['value']}"
        for problem in disk.get("problems", [])
        if isinstance(problem, dict) and "code" in problem
    ]


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

    async def async_added_to_hass(self) -> None:
        """Update the state whenever the host reports."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_DISK_REPORT}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )


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
        self._attr_unique_id = f"disk_{serial}_{description.key}"

        model = self.disk.get("model") or "Disk"
        node = entry.runtime_data.node_device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"{model} {serial}",
            model=self.disk.get("model") or None,
            serial_number=serial,
            via_device_id=node.id if node else None,
        )

    @property
    def disk(self) -> dict[str, Any]:
        """Return the latest data of this disk."""
        return self._entry.runtime_data.disks[self._serial]

    @property
    def available(self) -> bool:
        """Return whether the disk was part of a recent report."""
        return self._entry.runtime_data.disk_available(self._serial)


@callback
def async_setup_disk_platform(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    async_add_entities: AddEntitiesCallback,
    summary_entities: Callable[[], list[Entity]],
    disk_entities: Callable[[str], list[Entity]],
) -> None:
    """Add entities for known disks now and for new disks as they appear."""
    summary_added = False

    @callback
    def _add_disks(serials: list[str]) -> None:
        nonlocal summary_added
        entities: list[Entity] = []
        if serials and not summary_added:
            summary_added = True
            entities.extend(summary_entities())
        for serial in serials:
            entities.extend(disk_entities(serial))
        if entities:
            async_add_entities(entities)

    _add_disks(list(entry.runtime_data.disks))
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{SIGNAL_NEW_DISKS}_{entry.entry_id}", _add_disks
        )
    )
