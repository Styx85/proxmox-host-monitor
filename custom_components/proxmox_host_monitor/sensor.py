"""Health and SMART sensors for the disks of the Proxmox host."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ProxmoxHostConfigEntry
from .alerts import find_pool_device
from .const import DISK_STATUSES
from .entity import (
    ProxmoxDiskEntity,
    ProxmoxHostSummaryEntity,
    ProxmoxPoolEntity,
    async_setup_disk_platform,
    async_setup_pool_platform,
)
from .helpers import format_problems, free_share

SUMMARY_HEALTH = SensorEntityDescription(
    key="disk_health",
    translation_key="disk_health",
    device_class=SensorDeviceClass.ENUM,
    options=list(DISK_STATUSES),
)

DISK_HEALTH = SensorEntityDescription(
    key="health",
    translation_key="health",
    device_class=SensorDeviceClass.ENUM,
    options=list(DISK_STATUSES),
)


@dataclass(frozen=True, kw_only=True)
class ProxmoxDiskSensorDescription(SensorEntityDescription):
    """Describes a value of a disk report."""

    value_fn: Callable[[dict[str, Any]], Any] | None = None

    def value(self, disk: dict[str, Any]) -> Any:
        """Return the value of this sensor for a disk report."""
        if self.value_fn is not None:
            return self.value_fn(disk)
        return disk.get(self.key)


def _counter(
    key: str,
    category: EntityCategory | None = None,
    state_class: SensorStateClass = SensorStateClass.MEASUREMENT,
) -> ProxmoxDiskSensorDescription:
    return ProxmoxDiskSensorDescription(
        key=key,
        translation_key=key,
        state_class=state_class,
        entity_category=category,
    )


# Created for a disk when its report contains a value.
DISK_SENSORS = (
    ProxmoxDiskSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _counter("pending_sectors"),
    _counter("offline_uncorrectable"),
    _counter("reallocated_sectors"),
    _counter("reported_uncorrectable"),
    _counter("btrfs_errors"),
    _counter("media_errors"),
    ProxmoxDiskSensorDescription(
        key="percentage_used",
        translation_key="percentage_used",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxDiskSensorDescription(
        key="self_test_age",
        translation_key="self_test_age",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda disk: disk.get("self_test", {}).get("hours_ago"),
    ),
    ProxmoxDiskSensorDescription(
        key="capacity",
        translation_key="capacity",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _counter("crc_errors", EntityCategory.DIAGNOSTIC),
    _counter("command_timeouts", EntityCategory.DIAGNOSTIC),
    _counter("power_cycles", EntityCategory.DIAGNOSTIC, SensorStateClass.TOTAL_INCREASING),
    _counter("start_stop_count", EntityCategory.DIAGNOSTIC, SensorStateClass.TOTAL_INCREASING),
    _counter("load_cycles", EntityCategory.DIAGNOSTIC, SensorStateClass.TOTAL_INCREASING),
    # SMART only counts the hours a disk ran, not its age since manufacture.
    ProxmoxDiskSensorDescription(
        key="power_on_hours",
        translation_key="power_on_hours",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)


def _data_size(key: str, translation_key: str) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
    )


POOL_FREE = _data_size("free", "pool_free")
POOL_DATA = _data_size("used", "pool_data")
POOL_SIZE = _data_size("size", "pool_size")

POOL_USED = SensorEntityDescription(
    key="used_percentage",
    translation_key="pool_used",
    native_unit_of_measurement=PERCENTAGE,
    suggested_display_precision=1,
    state_class=SensorStateClass.MEASUREMENT,
)

POOL_HEALTH = SensorEntityDescription(
    key="health",
    translation_key="pool_health",
    device_class=SensorDeviceClass.ENUM,
    options=list(DISK_STATUSES),
)

POOL_DEVICES = SensorEntityDescription(
    key="devices",
    translation_key="pool_devices",
    state_class=SensorStateClass.MEASUREMENT,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the disk sensors."""

    def _disk_entities(serial: str) -> list[SensorEntity]:
        disk = entry.runtime_data.disks[serial]
        return [ProxmoxDiskHealthSensor(entry, serial, DISK_HEALTH)] + [
            ProxmoxDiskValueSensor(entry, serial, description)
            for description in DISK_SENSORS
            if description.value(disk) is not None
        ]

    async_setup_disk_platform(
        hass,
        entry,
        async_add_entities,
        lambda: [ProxmoxDisksHealthSensor(entry, SUMMARY_HEALTH)],
        _disk_entities,
    )
    async_setup_pool_platform(
        hass,
        entry,
        async_add_entities,
        lambda uuid: [
            ProxmoxPoolValueSensor(entry, uuid, POOL_FREE),
            ProxmoxPoolValueSensor(entry, uuid, POOL_DATA),
            ProxmoxPoolValueSensor(entry, uuid, POOL_SIZE),
            ProxmoxPoolUsedSensor(entry, uuid, POOL_USED),
            ProxmoxPoolHealthSensor(entry, uuid, POOL_HEALTH),
            ProxmoxPoolDevicesSensor(entry, uuid, POOL_DEVICES),
        ],
    )


class ProxmoxDisksHealthSensor(ProxmoxHostSummaryEntity, SensorEntity):
    """Worst health of all disks of the host."""

    @property
    def native_value(self) -> str:
        """Return the worst disk status."""
        statuses = {disk["status"] for disk in self.present_disks}
        return max(statuses, key=DISK_STATUSES.index, default=DISK_STATUSES[0])


class ProxmoxDiskHealthSensor(ProxmoxDiskEntity, SensorEntity):
    """Health of a single disk, with general information as attributes."""

    @property
    def native_value(self) -> str:
        """Return the disk status."""
        return self.disk["status"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return general information about the disk."""
        disk = self.disk
        pool, device = find_pool_device(self._entry.runtime_data, self._serial)
        rotation = disk.get("rotation_rate")
        attributes = {
            "device": f"/dev/{disk['name']}",
            "model_family": disk.get("model_family"),
            "firmware": disk.get("firmware"),
            "protocol": disk.get("protocol"),
            "interface": disk.get("interface"),
            "rotation_rate": "SSD" if rotation == 0 else rotation,
            "form_factor": disk.get("form_factor"),
            "smart_passed": disk.get("smart_passed"),
            "problems": format_problems(disk),
            "self_test": disk.get("self_test"),
            "pool": pool["mount"] if pool else None,
            "pool_allocated": device.get("used") if device else None,
        }
        return {key: value for key, value in attributes.items() if value is not None}


class ProxmoxDiskValueSensor(ProxmoxDiskEntity, SensorEntity):
    """A SMART or Btrfs value of a single disk."""

    entity_description: ProxmoxDiskSensorDescription

    @property
    def native_value(self) -> int | float | None:
        """Return the reported value."""
        return self.entity_description.value(self.disk)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the details of the last self-test."""
        if self.entity_description.key != "self_test_age":
            return None
        test = self.disk.get("self_test", {})
        return {"type": test.get("type"), "result": test.get("result"), "passed": test.get("passed")}


class ProxmoxPoolValueSensor(ProxmoxPoolEntity, SensorEntity):
    """A size reported for a Btrfs filesystem."""

    @property
    def native_value(self) -> int | None:
        """Return the reported value."""
        return self.pool.get(self.entity_description.key)


class ProxmoxPoolUsedSensor(ProxmoxPoolEntity, SensorEntity):
    """Used share of a Btrfs filesystem, with its layout as attributes."""

    @property
    def native_value(self) -> float | None:
        """Return how much of the usable capacity is in use."""
        if (share := free_share(self.pool)) is None:
            return None
        return round((1 - share) * 100, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the mount point, profiles and devices."""
        return {
            "label": self.pool.get("label"),
            "mount": self.pool.get("mount"),
            "uuid": self.pool.get("uuid"),
            "profiles": self.pool.get("profiles"),
            "devices": [
                {
                    "name": device.get("name"),
                    "serial": device.get("serial"),
                    "size": device.get("size"),
                    "allocated": device.get("used"),
                }
                for device in self.pool.get("devices", [])
            ],
        }


class ProxmoxPoolHealthSensor(ProxmoxPoolEntity, SensorEntity):
    """Worst health of the disks of a Btrfs filesystem."""

    @property
    def native_value(self) -> str:
        """Return the worst status of the filesystem's disks."""
        statuses = {disk["status"] for disk in self.member_disks}
        return max(statuses, key=DISK_STATUSES.index, default=DISK_STATUSES[0])


class ProxmoxPoolDevicesSensor(ProxmoxPoolEntity, SensorEntity):
    """Number of disks in a Btrfs filesystem."""

    @property
    def native_value(self) -> int:
        """Return the number of devices."""
        return len(self.pool.get("devices", []))
