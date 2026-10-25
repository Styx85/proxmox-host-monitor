"""Health and SMART sensors for the disks of the Proxmox host."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    UnitOfDataRate,
    EntityCategory,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from . import ProxmoxHostConfigEntry
from .alerts import find_pool_device
from .const import DISK_STATUSES, DOMAIN, OPERATION_SUFFIX, OPERATIONS, POOL_PREFIX
from .entity import (
    ProxmoxDiskEntity,
    ProxmoxHostInfoEntity,
    ProxmoxHostSummaryEntity,
    ProxmoxPoolEntity,
    async_setup_disk_platform,
    async_setup_host_platform,
    async_setup_pool_platform,
)
from .helpers import format_problems, free_share, pool_operation
from .status import health_summary

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
    _counter("io_errors_24h"),
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

POOL_OPERATION = SensorEntityDescription(
    key="operation",
    translation_key="pool_operation",
    device_class=SensorDeviceClass.ENUM,
    options=list(OPERATIONS),
)

POOL_PROGRESS = SensorEntityDescription(
    key="progress",
    translation_key="pool_progress",
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
)

POOL_SCRUB = SensorEntityDescription(
    key="last_scrub",
    translation_key="pool_last_scrub",
    device_class=SensorDeviceClass.TIMESTAMP,
)

POOL_DEVICES = SensorEntityDescription(
    key="devices",
    translation_key="pool_devices",
    state_class=SensorStateClass.MEASUREMENT,
)


@dataclass(frozen=True, kw_only=True)
class ProxmoxHostSensorDescription(SensorEntityDescription):
    """Describes a value of the host part of a report."""

    value_fn: Callable[[dict[str, Any]], Any]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _timestamp(value: Any) -> datetime | None:
    return dt_util.utc_from_timestamp(value) if isinstance(value, (int, float)) else None


HOST_SENSORS = (
    ProxmoxHostSensorDescription(
        key="failed_units",
        translation_key="failed_units",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda host: len(host.get("failed_units", [])),
        attributes_fn=lambda host: {"units": host.get("failed_units", [])},
    ),
    ProxmoxHostSensorDescription(
        key="last_backup",
        translation_key="last_backup",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda host: _timestamp(host.get("backup", {}).get("last")),
        attributes_fn=lambda host: {
            "jobs": host.get("backup", {}).get("jobs"),
            "status": host.get("backup", {}).get("last_status"),
        },
    ),
    ProxmoxHostSensorDescription(
        key="certificate_expires",
        translation_key="certificate_expires",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda host: _timestamp(host.get("certificate_expires")),
    ),
    ProxmoxHostSensorDescription(
        key="unattended_upgrades",
        translation_key="unattended_upgrades",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda host: _timestamp((host.get("unattended_upgrades") or {}).get("last")),
        attributes_fn=lambda host: {
            "success": (host.get("unattended_upgrades") or {}).get("success")
        },
    ),
)

HOST_TEMPERATURE = SensorEntityDescription(
    key="temperature",
    translation_key="host_temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT,
)

HOST_FAN = SensorEntityDescription(
    key="fan",
    translation_key="host_fan",
    native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
    state_class=SensorStateClass.MEASUREMENT,
)

THIN_POOL = SensorEntityDescription(
    key="thin_pool",
    translation_key="thin_pool",
    native_unit_of_measurement=PERCENTAGE,
    suggested_display_precision=1,
    state_class=SensorStateClass.MEASUREMENT,
)

NIC_SPEED = SensorEntityDescription(
    key="nic",
    translation_key="nic_speed",
    device_class=SensorDeviceClass.DATA_RATE,
    native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
    entity_category=EntityCategory.DIAGNOSTIC,
)


def _host_entities(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
) -> Callable[[dict[str, Any]], list[SensorEntity]]:
    """Return a factory for the host sensors of the current report."""
    ent_reg = er.async_get(hass)

    def _known(fan: dict[str, Any]) -> bool:
        """Return whether a fan that stands still now had a sensor before."""
        unique_id = f"host_{HOST_FAN.key}_{slugify(f'{fan.get('chip')} {fan.get('label')}')}"
        return ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id) is not None

    def _entities(host: dict[str, Any]) -> list[SensorEntity]:
        entities: list[SensorEntity] = [
            ProxmoxHostValueSensor(entry, description) for description in HOST_SENSORS
        ]
        for kind, description, items, label in (
            ("temperatures", HOST_TEMPERATURE, host.get("temperatures", []), "sensor"),
            # A fan gets its sensor once it has turned; unused headers read 0.
            ("fans", HOST_FAN, [f for f in host.get("fans", []) if f.get("rpm") or _known(f)], "sensor"),
            ("thin_pools", THIN_POOL, host.get("thin_pools", []), "name"),
            # Unused ports without a link would only add empty sensors.
            ("nics", NIC_SPEED, [n for n in host.get("nics", []) if n.get("state") == "up"], "name"),
        ):
            for item in items:
                name = item.get("name") or f"{item.get('chip')} {item.get('label')}"
                entities.append(
                    ProxmoxHostListSensor(
                        entry,
                        description,
                        unique_id=f"host_{description.key}_{slugify(name)}",
                        placeholders={label: name},
                        kind=kind,
                        name=name,
                    )
                )
        return entities

    return _entities


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
            ProxmoxPoolOperationSensor(entry, uuid, POOL_OPERATION),
            ProxmoxPoolOperationSensor(entry, uuid, POOL_PROGRESS),
            ProxmoxPoolScrubSensor(entry, uuid, POOL_SCRUB),
        ],
    )
    async_setup_host_platform(hass, entry, async_add_entities, _host_entities(hass, entry))


class ProxmoxDisksHealthSensor(ProxmoxHostSummaryEntity, SensorEntity):
    """Worst health of all disks of the host."""

    @property
    def native_value(self) -> str:
        """Return the worst disk status."""
        return health_summary(self.present_disks)["status"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return which disks are critical and which have warnings."""
        summary = health_summary(self.present_disks)
        return {key: summary[key] for key in ("critical", "warning")}


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
        return health_summary(self.member_disks)["status"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return which disks are critical and which have warnings."""
        summary = health_summary(self.member_disks)
        return {key: summary[key] for key in ("critical", "warning")}


class ProxmoxPoolOperationSensor(ProxmoxPoolEntity, SensorEntity):
    """The running operation of a Btrfs filesystem or its progress."""

    def __init__(
        self,
        entry: ProxmoxHostConfigEntry,
        uuid: str,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry, uuid, description)
        self._context_key = f"{POOL_PREFIX}{uuid}{OPERATION_SUFFIX}"

    @property
    def native_value(self) -> str | int | None:
        """Return the operation or the progress of a balance."""
        return pool_operation(self.pool, self._entry.runtime_data.disks)[
            self.entity_description.key
        ]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the disks being removed."""
        if self.entity_description.key != "operation":
            return None
        return {"disks": pool_operation(self.pool, self._entry.runtime_data.disks)["disks"]}


class ProxmoxPoolDevicesSensor(ProxmoxPoolEntity, SensorEntity):
    """Number of disks in a Btrfs filesystem."""

    @property
    def native_value(self) -> int:
        """Return the number of devices."""
        return len(self.pool.get("devices", []))


class ProxmoxPoolScrubSensor(ProxmoxPoolEntity, SensorEntity):
    """Time of the last scrub of a Btrfs filesystem, with its result."""

    @property
    def native_value(self) -> datetime | None:
        """Return when the last scrub started."""
        return _timestamp((self.pool.get("scrub") or {}).get("last"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the result of the last scrub."""
        scrub = self.pool.get("scrub") or {}
        return {
            key: scrub.get(key)
            for key in ("status", "duration", "errors", "corrected", "uncorrectable")
        }


class ProxmoxHostValueSensor(ProxmoxHostInfoEntity, SensorEntity):
    """A value of the host part of a report."""

    entity_description: ProxmoxHostSensorDescription

    @property
    def native_value(self) -> Any:
        """Return the reported value."""
        return self.entity_description.value_fn(self.host)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return details of the value."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.host)


class ProxmoxHostListSensor(ProxmoxHostInfoEntity, SensorEntity):
    """A temperature, fan, thin pool or network interface of the host."""

    def __init__(
        self,
        entry: ProxmoxHostConfigEntry,
        description: SensorEntityDescription,
        *,
        unique_id: str,
        placeholders: dict[str, str],
        kind: str,
        name: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry, description, unique_id, placeholders)
        self._kind = kind
        self._name = name

    @property
    def _item(self) -> dict[str, Any] | None:
        for item in self.host.get(self._kind, []):
            if (item.get("name") or f"{item.get('chip')} {item.get('label')}") == self._name:
                return item
        return None

    @property
    def available(self) -> bool:
        """Return whether the sensor is part of the latest report."""
        return super().available and self._item is not None

    @property
    def native_value(self) -> float | int | None:
        """Return the temperature, speed, usage or link speed."""
        item = self._item or {}
        return {
            "temperatures": item.get("value"),
            "fans": item.get("rpm"),
            "thin_pools": item.get("data_percent"),
            "nics": item.get("speed"),
        }[self._kind]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return limits, metadata usage or interface details."""
        item = self._item or {}
        if self._kind == "temperatures":
            return {key: item[key] for key in ("max", "crit") if key in item} or None
        if self._kind == "thin_pools":
            return {"metadata_percent": item.get("metadata_percent")}
        if self._kind == "nics":
            return {
                "state": item.get("state"),
                "rx_errors": item.get("rx_errors"),
                "tx_errors": item.get("tx_errors"),
            }
        return None
