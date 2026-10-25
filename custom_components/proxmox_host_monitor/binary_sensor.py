"""Problem sensors for the disks of the Proxmox host."""

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ProxmoxHostConfigEntry
from .const import DISK_STATUS_OK
from .entity import (
    ProxmoxDiskEntity,
    ProxmoxHostSummaryEntity,
    async_setup_disk_platform,
    format_problems,
)

SUMMARY_PROBLEM = BinarySensorEntityDescription(
    key="disk_problem",
    translation_key="disk_problem",
    device_class=BinarySensorDeviceClass.PROBLEM,
)

DISK_PROBLEM = BinarySensorEntityDescription(
    key="problem",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the disk problem sensors."""
    async_setup_disk_platform(
        hass,
        entry,
        async_add_entities,
        lambda: [ProxmoxDisksProblemSensor(entry, SUMMARY_PROBLEM)],
        lambda serial: [ProxmoxDiskProblemSensor(entry, serial, DISK_PROBLEM)],
    )


class ProxmoxDisksProblemSensor(ProxmoxHostSummaryEntity, BinarySensorEntity):
    """On while any disk of the host reports a problem."""

    @property
    def is_on(self) -> bool:
        """Return whether any disk has a problem."""
        return any(disk["status"] != DISK_STATUS_OK for disk in self.present_disks)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the problems of all disks."""
        return {
            "problems": [
                f"{disk['name']} {problem}"
                for disk in self.present_disks
                for problem in format_problems(disk)
            ]
        }


class ProxmoxDiskProblemSensor(ProxmoxDiskEntity, BinarySensorEntity):
    """On while a disk reports a warning or a critical problem."""

    @property
    def is_on(self) -> bool:
        """Return whether the disk has a problem."""
        return self.disk["status"] != DISK_STATUS_OK

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the device path, severity and problems."""
        return {
            "device": f"/dev/{self.disk['name']}",
            "status": self.disk["status"],
            "problems": format_problems(self.disk),
        }
