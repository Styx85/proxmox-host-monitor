"""Health and SMART sensors for the disks of the Proxmox host."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ProxmoxHostConfigEntry
from .const import DISK_STATUSES
from .entity import (
    ProxmoxDiskEntity,
    ProxmoxHostSummaryEntity,
    async_setup_disk_platform,
)

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


def _counter(key: str, category: EntityCategory | None = None) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=category,
    )


# Created for a disk when its report contains the key.
DISK_SENSORS = (
    SensorEntityDescription(
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
    SensorEntityDescription(
        key="percentage_used",
        translation_key="percentage_used",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _counter("crc_errors", EntityCategory.DIAGNOSTIC),
    _counter("command_timeouts", EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(
        key="power_on_hours",
        translation_key="power_on_hours",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
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
            if disk.get(description.key) is not None
        ]

    async_setup_disk_platform(
        hass,
        entry,
        async_add_entities,
        lambda: [ProxmoxDisksHealthSensor(entry, SUMMARY_HEALTH)],
        _disk_entities,
    )


class ProxmoxDisksHealthSensor(ProxmoxHostSummaryEntity, SensorEntity):
    """Worst health of all disks of the host."""

    @property
    def native_value(self) -> str:
        """Return the worst disk status."""
        statuses = {disk["status"] for disk in self.present_disks}
        return max(statuses, key=DISK_STATUSES.index, default=DISK_STATUSES[0])


class ProxmoxDiskHealthSensor(ProxmoxDiskEntity, SensorEntity):
    """Health of a single disk."""

    @property
    def native_value(self) -> str:
        """Return the disk status."""
        return self.disk["status"]


class ProxmoxDiskValueSensor(ProxmoxDiskEntity, SensorEntity):
    """A SMART or Btrfs value of a single disk."""

    @property
    def native_value(self) -> int | float | None:
        """Return the reported value."""
        return self.disk.get(self.entity_description.key)
