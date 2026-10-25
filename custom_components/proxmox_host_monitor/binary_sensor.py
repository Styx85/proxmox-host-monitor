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
from .const import (
    DISK_STATUS_CRITICAL,
    DISK_STATUS_OK,
    DISK_STATUS_WARNING,
    OPERATION_IDLE,
    OPERATION_SUFFIX,
    POOL_PREFIX,
)
from .alerts import find_pool_device
from .entity import (
    ProxmoxDiskEntity,
    ProxmoxHostSummaryEntity,
    ProxmoxPoolEntity,
    async_setup_disk_platform,
    async_setup_pool_platform,
)
from .helpers import format_problems, pool_operation

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

POOL_PROBLEM = BinarySensorEntityDescription(
    key="problem",
    translation_key="pool_problem",
    device_class=BinarySensorDeviceClass.PROBLEM,
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
    runtime_data = entry.runtime_data
    removable_added: set[str] = set()

    def _removable(serials: list[str]) -> list[BinarySensorEntity]:
        """Add the removable sensor once a disk is known and in a pool."""
        entities: list[BinarySensorEntity] = []
        for serial in serials:
            if (
                serial in runtime_data.disks
                and serial not in removable_added
                and find_pool_device(runtime_data, serial)[1] is not None
            ):
                removable_added.add(serial)
                entities.append(ProxmoxDiskRemovableSensor(entry, serial, DISK_REMOVABLE))
        return entities

    async_setup_disk_platform(
        hass,
        entry,
        async_add_entities,
        lambda: [ProxmoxDisksProblemSensor(entry, SUMMARY_PROBLEM)],
        lambda serial: [ProxmoxDiskProblemSensor(entry, serial, DISK_PROBLEM)]
        + _removable([serial]),
    )
    async_setup_pool_platform(
        hass,
        entry,
        async_add_entities,
        lambda uuid: [
            ProxmoxPoolOperationSensor(entry, uuid, POOL_OPERATION),
            ProxmoxPoolProblemSensor(entry, uuid, POOL_PROBLEM),
        ]
        + _removable(
            [device.get("serial", "") for device in runtime_data.pools[uuid].get("devices", [])]
        ),
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

    def __init__(
        self,
        entry: ProxmoxHostConfigEntry,
        uuid: str,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry, uuid, description)
        self._context_key = f"{POOL_PREFIX}{uuid}{OPERATION_SUFFIX}"

    @property
    def is_on(self) -> bool:
        """Return whether a long-running operation is active."""
        return self._operation["operation"] != OPERATION_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return what is running and how far it is."""
        return self._operation

    @property
    def _operation(self) -> dict[str, Any]:
        return pool_operation(self.pool, self._entry.runtime_data.disks)


class ProxmoxPoolProblemSensor(ProxmoxPoolEntity, BinarySensorEntity):
    """On while a disk of the filesystem reports a problem."""

    @property
    def is_on(self) -> bool:
        """Return whether any disk of the filesystem has a problem."""
        return any(disk["status"] != DISK_STATUS_OK for disk in self.member_disks)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the critical and warning disks and their problems."""
        disks = self.member_disks
        return {
            DISK_STATUS_CRITICAL: [
                disk["name"] for disk in disks if disk["status"] == DISK_STATUS_CRITICAL
            ],
            DISK_STATUS_WARNING: [
                disk["name"] for disk in disks if disk["status"] == DISK_STATUS_WARNING
            ],
            "problems": [
                f"{disk['name']} {problem}"
                for disk in disks
                for problem in format_problems(disk)
            ],
        }
