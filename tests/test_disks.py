"""Tests for the disk health entities."""

from copy import deepcopy
from http import HTTPStatus

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.proxmox_host_monitor import async_remove_config_entry_device
from custom_components.proxmox_host_monitor.const import (
    CONF_REBOOT_BUTTON,
    CONF_WEBHOOK_ID,
    DISK_REPORT_TIMEOUT,
    DOMAIN,
)

WEBHOOK_ID = "test_webhook"
BUTTON = "button.pve_reboot"

HEALTHY = {
    "name": "sda",
    "model": "ST4000VX016",
    "serial": "WW6099YV",
    "status": "ok",
    "temperature": 37,
    "power_on_hours": 1241,
    "pending_sectors": 0,
    "reallocated_sectors": 0,
    "btrfs_errors": 0,
    "problems": [],
}
FAILING = {
    "name": "sde",
    "model": "WDC WD1001FALS",
    "serial": "WD-WMATV1460428",
    "status": "critical",
    "temperature": 40,
    "pending_sectors": 1414,
    "offline_uncorrectable": 127,
    "problems": [
        {"code": "self_test_failed", "severity": "warning", "value": None},
        {"code": "pending_sectors", "severity": "critical", "value": 1414},
    ],
}
NVME = {
    "name": "nvme0",
    "model": "Samsung SSD 970 EVO Plus",
    "serial": "S4EVNM0RC16689L",
    "status": "ok",
    "temperature": 39,
    "percentage_used": 0,
    "media_errors": 0,
    "problems": [],
}


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the integration."""
    hass.states.async_set(BUTTON, "unknown")
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id=DOMAIN,
        data={CONF_REBOOT_BUTTON: BUTTON, CONF_WEBHOOK_ID: WEBHOOK_ID},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _report(hass_client_no_auth, hass, disks) -> int:
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", json={"disks": disks})
    await hass.async_block_till_done()
    return resp.status


def _disk_device(hass: HomeAssistant, entry: MockConfigEntry, serial: str):
    dev_reg = dr.async_get(hass)
    if hasattr(dev_reg, "async_get_device_by_identifier"):
        return dev_reg.async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
    return dev_reg.async_get_device(identifiers={(DOMAIN, serial)})


def _entity_id(hass: HomeAssistant, unique_id: str, domain: str = "sensor") -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


async def test_no_disk_entities_without_report(hass, entry):
    """Hosts without the disk script only get the update entity."""
    assert hass.states.async_entity_ids("sensor") == []
    assert hass.states.async_entity_ids("binary_sensor") == []


async def test_disk_report_creates_entities(hass, entry, hass_client_no_auth):
    """A report creates a device per disk and summary entities."""
    assert await _report(hass_client_no_auth, hass, [HEALTHY, FAILING, NVME]) == HTTPStatus.OK

    summary = hass.states.get(_entity_id(hass, "disk_problem", "binary_sensor"))
    assert summary.state == STATE_ON
    assert summary.attributes["problems"] == [
        "sde self_test_failed",
        "sde pending_sectors: 1414",
    ]
    assert hass.states.get(_entity_id(hass, "disk_health")).state == "critical"

    problem = hass.states.get(
        _entity_id(hass, "disk_WD-WMATV1460428_problem", "binary_sensor")
    )
    assert problem.state == STATE_ON
    assert problem.attributes["device"] == "/dev/sde"
    assert problem.attributes["status"] == "critical"
    assert hass.states.get(
        _entity_id(hass, "disk_WD-WMATV1460428_pending_sectors")
    ).state == "1414"
    assert hass.states.get(
        _entity_id(hass, "disk_WW6099YV_problem", "binary_sensor")
    ).state == STATE_OFF
    assert hass.states.get(_entity_id(hass, "disk_WW6099YV_temperature")).state == "37"
    assert hass.states.get(_entity_id(hass, "disk_S4EVNM0RC16689L_percentage_used")).state == "0"

    # Only values contained in the report get an entity.
    assert er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "disk_WW6099YV_media_errors"
    ) is None

    device = _disk_device(hass, entry, "WD-WMATV1460428")
    assert device.name == "WDC WD1001FALS WD-WMATV1460428"
    assert device.serial_number == "WD-WMATV1460428"


async def test_new_and_removed_disks(hass, entry, hass_client_no_auth):
    """New disks are added, disks missing from a report become unavailable."""
    await _report(hass_client_no_auth, hass, [HEALTHY])
    assert hass.states.get(_entity_id(hass, "disk_health")).state == "ok"

    await _report(hass_client_no_auth, hass, [HEALTHY, FAILING])
    problem_id = _entity_id(hass, "disk_WD-WMATV1460428_problem", "binary_sensor")
    assert hass.states.get(problem_id).state == STATE_ON
    assert hass.states.get(_entity_id(hass, "disk_health")).state == "critical"

    await _report(hass_client_no_auth, hass, [HEALTHY])
    assert hass.states.get(problem_id).state == STATE_UNAVAILABLE
    assert hass.states.get(_entity_id(hass, "disk_health")).state == "ok"


async def test_values_update(hass, entry, hass_client_no_auth):
    """Later reports update the existing entities."""
    await _report(hass_client_no_auth, hass, [FAILING])
    worse = deepcopy(FAILING)
    worse["pending_sectors"] = 1500
    await _report(hass_client_no_auth, hass, [worse])
    assert hass.states.get(
        _entity_id(hass, "disk_WD-WMATV1460428_pending_sectors")
    ).state == "1500"


async def test_report_timeout(hass, entry, hass_client_no_auth, freezer):
    """Disk entities become unavailable when reports stop."""
    await _report(hass_client_no_auth, hass, [HEALTHY])
    temperature = _entity_id(hass, "disk_WW6099YV_temperature")
    assert hass.states.get(temperature).state == "37"

    freezer.tick(DISK_REPORT_TIMEOUT)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(temperature).state == STATE_UNAVAILABLE
    assert hass.states.get(_entity_id(hass, "disk_health")).state == STATE_UNAVAILABLE


async def test_disks_survive_reload(hass, entry, hass_client_no_auth):
    """Known disks and their last values are restored after a restart."""
    await _report(hass_client_no_auth, hass, [FAILING])
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(
        _entity_id(hass, "disk_WD-WMATV1460428_pending_sectors")
    ).state == "1414"


@pytest.mark.parametrize(
    "disks",
    [
        "sda",
        [{"name": "sda", "status": "ok"}],
        [{"name": "sda", "serial": "", "status": "ok"}],
        [{"name": "sda", "serial": "X", "status": "broken"}],
        [{"name": "sda", "serial": "X", "status": "ok", "problems": "none"}],
    ],
)
async def test_invalid_disk_report(hass, entry, hass_client_no_auth, disks):
    """Malformed disk reports are rejected."""
    assert await _report(hass_client_no_auth, hass, disks) == HTTPStatus.BAD_REQUEST
    assert hass.states.async_entity_ids("sensor") == []


async def test_combined_report(hass, entry, hass_client_no_auth):
    """Reboot status and disks can be sent in one report."""
    client = await hass_client_no_auth()
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}", json={"required": True, "disks": [HEALTHY]}
    )
    assert await resp.json() == {
        "ok": True,
        "required": True,
        "pending_commands": False,
        "disks": 1,
    }


async def test_remove_absent_disk_device(hass, entry, hass_client_no_auth):
    """Only disks missing from the latest report can be removed."""
    await _report(hass_client_no_auth, hass, [HEALTHY, FAILING])
    failing = _disk_device(hass, entry, "WD-WMATV1460428")
    assert not await async_remove_config_entry_device(hass, entry, failing)

    await _report(hass_client_no_auth, hass, [HEALTHY])
    assert await async_remove_config_entry_device(hass, entry, failing)
    assert "WD-WMATV1460428" not in entry.runtime_data.disks


async def test_disk_info(hass, entry, hass_client_no_auth):
    """General disk information becomes sensors, attributes and device info."""
    disk = deepcopy(HEALTHY) | {
        "model_family": "Seagate SkyHawk",
        "firmware": "CV10",
        "capacity": 4000787030016,
        "protocol": "ATA",
        "interface": "SATA 3.1",
        "rotation_rate": 5400,
        "form_factor": "3.5 inches",
        "smart_passed": True,
        "power_cycles": 45,
        "start_stop_count": 119,
        "load_cycles": 1285,
        "power_on_hours": 17532,
        "self_test": {"type": "Short offline", "result": "Completed without error", "passed": True, "hours_ago": 5},
    }
    await _report(hass_client_no_auth, hass, [disk])

    power_on = hass.states.get(_entity_id(hass, "disk_WW6099YV_power_on_hours"))
    assert power_on.attributes["unit_of_measurement"] == "d"
    assert round(float(power_on.state)) == 730
    assert hass.states.get(_entity_id(hass, "disk_WW6099YV_power_cycles")).state == "45"
    self_test = hass.states.get(_entity_id(hass, "disk_WW6099YV_self_test_age"))
    assert self_test.state == "5"
    assert self_test.attributes["result"] == "Completed without error"
    assert float(hass.states.get(_entity_id(hass, "disk_WW6099YV_capacity")).state) == pytest.approx(4000.787, 0.001)

    health = hass.states.get(_entity_id(hass, "disk_WW6099YV_health")).attributes
    assert health["device"] == "/dev/sda"
    assert health["rotation_rate"] == 5400
    assert health["interface"] == "SATA 3.1"

    device = _disk_device(hass, entry, "WW6099YV")
    assert device.sw_version == "CV10"
    assert device.model_id == "Seagate SkyHawk"


async def test_new_values_of_known_disks(hass, entry, hass_client_no_auth):
    """Values reported later by a newer host script get entities and device info."""
    await _report(hass_client_no_auth, hass, [HEALTHY])
    assert er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "disk_WW6099YV_capacity"
    ) is None

    newer = deepcopy(HEALTHY) | {"capacity": 4000787030016, "firmware": "CV10"}
    await _report(hass_client_no_auth, hass, [newer])
    assert hass.states.get(_entity_id(hass, "disk_WW6099YV_capacity")) is not None
    assert _disk_device(hass, entry, "WW6099YV").sw_version == "CV10"

    # Nothing is added twice.
    count = len(hass.states.async_entity_ids("sensor"))
    await _report(hass_client_no_auth, hass, [newer])
    assert len(hass.states.async_entity_ids("sensor")) == count


async def test_status_change_reason(hass, entry, hass_client_no_auth):
    """A health change fires an event whose context the new states carry."""
    from pytest_homeassistant_custom_component.common import async_capture_events

    from custom_components.proxmox_host_monitor.const import EVENT_DISK_STATUS
    from custom_components.proxmox_host_monitor.logbook import async_describe_events

    events = async_capture_events(hass, EVENT_DISK_STATUS)
    await _report(hass_client_no_auth, hass, [HEALTHY])
    assert [event.data["status"] for event in events] == ["ok"]

    overdue = deepcopy(HEALTHY) | {
        "status": "warning",
        "problems": [{"code": "self_test_overdue", "severity": "warning", "value": None}],
    }
    await _report(hass_client_no_auth, hass, [overdue])
    await _report(hass_client_no_auth, hass, [overdue])
    assert [event.data["status"] for event in events] == ["ok", "warning"]

    health = hass.states.get(_entity_id(hass, "disk_WW6099YV_health"))
    assert health.state == "warning"
    assert health.context.id == events[-1].context.id
    assert health.attributes["problems"] == ["self_test_overdue"]

    described = {}
    hass.config.language = "de"
    async_describe_events(hass, lambda domain, event_type, fn: described.update({event_type: fn}))
    assert described[EVENT_DISK_STATUS](events[-1]) == {
        "name": "ST4000VX016 sda",
        "message": "Zustand Warnung: Selbsttest überfällig (Stunden)",
    }
