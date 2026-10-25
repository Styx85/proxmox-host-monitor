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
from .alerts import find_pool_device
from .entity import (
    ProxmoxDiskEntity,
    ProxmoxHostSummaryEntity,
    ProxmoxPoolEntity,
    async_setup_disk_platform,
    async_setup_pool_platform,
)
from .helpers import format_problems

SUMMARY_PROBLEM = BinarySensorEntityDescription(
    key="disk_problem",
    translation_key="disk_problem",
    device_class=BinarySensorDeviceClass.PROBLEM,
)

DISK_PROBLEM = BinarySensorEntityDescription(
    key="problem",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


DISK_REMOVABLE = BinarySensorEntityDescription(
    key="removable",
    translation_key="removable",
)

POOL_OPERATION = BinarySensorEntityDescription(
    key="operation",
    translation_key="pool_operation",
    device_class=BinarySensorDeviceClass.RUNNING,
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
        lambda serial: [ProxmoxDiskProblemSensor(entry, serial, DISK_PROBLEM)]
        + (
            [ProxmoxDiskRemovableSensor(entry, serial, DISK_REMOVABLE)]
            if find_pool_device(entry.runtime_data, serial)[1] is not None
            else []
        ),
    )
    async_setup_pool_platform(
        hass,
        entry,
        async_add_entities,
        lambda uuid: [ProxmoxPoolOperationSensor(entry, uuid, POOL_OPERATION)],
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


class ProxmoxDiskRemovableSensor(ProxmoxDiskEntity, BinarySensorEntity):
    """On while the data of a disk fits on the other disks of its pool."""

    @property
    def is_on(self) -> bool | None:
        """Return whether the disk can leave its Btrfs pool."""
        _, device = find_pool_device(self._entry.runtime_data, self._serial)
        if device is None:
            return None
        return not device.get("missing_after_removal")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the pool and the space that would be missing."""
        pool, device = find_pool_device(self._entry.runtime_data, self._serial)
        return {
            "pool": pool["mount"] if pool else None,
            "missing_bytes": device.get("missing_after_removal") if device else None,
            "removal": self._entry.runtime_data.removals.get(self._serial),
        }


class ProxmoxPoolOperationSensor(ProxmoxPoolEntity, BinarySensorEntity):
    """On while a balance or a disk removal runs on the filesystem."""

    @property
    def is_on(self) -> bool:
        """Return whether a long-running operation is active."""
        return bool(self.pool.get("balance_running")) or any(
            operation.get("state") == "running"
            for operation in self.pool.get("operations", [])
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return what is running."""
        return {
            "balance": bool(self.pool.get("balance_running")),
            "removals": [
                operation.get("serial")
                for operation in self.pool.get("operations", [])
                if operation.get("state") == "running"
            ],
        }
