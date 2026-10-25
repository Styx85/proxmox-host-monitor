"""Tests for the host information: services, backups, temperatures and more."""

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.proxmox_host_monitor.const import (
    CONF_REBOOT_BUTTON,
    CONF_WEBHOOK_ID,
    DOMAIN,
    EVENT_ALERT,
    EVENT_HOST_STATUS,
)
from custom_components.proxmox_host_monitor.logbook import async_describe_events

WEBHOOK_ID = "test_webhook"
DISK = {"name": "sda", "model": "X", "serial": "S1", "status": "ok", "problems": []}


def _host(problems=None, **extra):
    host = {
        "status": "warning" if problems else "ok",
        "problems": problems or [],
        "failed_units": [],
        "backup": {"jobs": 0, "last": None, "last_status": None},
        "temperatures": [{"chip": "k10temp", "label": "Tctl", "value": 42.5}],
        "fans": [],
        "thin_pools": [{"name": "pve/data", "data_percent": 16.75, "metadata_percent": 1.01}],
        "nics": [{"name": "nic0", "state": "up", "speed": 1000, "rx_errors": 0, "tx_errors": 0}],
        "certificate_expires": 1855910807.0,
        "time_synchronized": True,
        "unattended_upgrades": {"last": 1792949746.0, "success": True},
    }
    return host | extra


BACKUP_MISSING = {"code": "backup_missing", "severity": "warning", "item": None, "value": None}
SCRUB_NEVER = {"code": "scrub_never", "severity": "warning", "item": "/mnt/storage", "value": None}


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the integration."""
    hass.states.async_set("button.pve_reboot", "unknown")
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id=DOMAIN,
        data={CONF_REBOOT_BUTTON: "button.pve_reboot", CONF_WEBHOOK_ID: WEBHOOK_ID},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def report(hass, hass_client_no_auth):
    """Send a report."""

    async def _report(host):
        client = await hass_client_no_auth()
        resp = await client.post(
            f"/api/webhook/{WEBHOOK_ID}", json={"disks": [DISK], "host": host}
        )
        await hass.async_block_till_done()
        return resp.status

    return _report


def _state(hass: HomeAssistant, domain: str, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return hass.states.get(entity_id)


async def test_host_entities(hass, entry, report):
    """Host information becomes sensors on the node."""
    await report(_host())

    assert _state(hass, "binary_sensor", "host_problem").state == "off"
    assert _state(hass, "sensor", "host_failed_units").state == "0"
    assert _state(hass, "sensor", "host_last_backup").attributes["jobs"] == 0
    temperature = _state(hass, "sensor", "host_temperature_k10temp_tctl")
    assert temperature.state == "42.5"
    assert temperature.name.endswith("Temperature k10temp Tctl")
    assert _state(hass, "sensor", "host_thin_pool_pve_data").state == "16.75"
    assert _state(hass, "sensor", "host_nic_nic0").state == "1000"
    assert _state(hass, "sensor", "host_certificate_expires").state.startswith("2028-10-23")


async def test_new_host_sensors_are_added(hass, entry, report):
    """Sensors that appear later get their entities; idle fan headers do not."""
    await report(_host(fans=[{"chip": "it8655", "label": "fan2", "rpm": 0}]))
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "host_fan_it8655_fan2") is None

    await report(_host(fans=[{"chip": "it8655", "label": "fan1", "rpm": 1051}]))
    assert _state(hass, "sensor", "host_fan_it8655_fan1").state == "1051"

    # A fan that stops keeps its sensor, also after a restart.
    await report(_host(fans=[{"chip": "it8655", "label": "fan1", "rpm": 0}]))
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "host_fan_it8655_fan1").state == "0"


async def test_host_problems(hass, entry, report):
    """Host problems raise issues, alert once and are logged with their reason."""
    alerts = async_capture_events(hass, EVENT_ALERT)
    status = async_capture_events(hass, EVENT_HOST_STATUS)
    await report(_host([BACKUP_MISSING, SCRUB_NEVER]))
    await report(_host([BACKUP_MISSING, SCRUB_NEVER]))

    problem = _state(hass, "binary_sensor", "host_problem")
    assert problem.state == "on"
    assert problem.attributes["problems"] == ["backup_missing", "scrub_never /mnt/storage"]
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "host_backup_missing_host").translation_key == (
        "host_backup_missing"
    )
    scrub = issues.async_get_issue(DOMAIN, "host_scrub_never_mnt_storage")
    assert scrub.translation_placeholders == {"item": "/mnt/storage", "value": ""}

    assert [(a.data["type"], a.data["code"]) for a in alerts] == [
        ("host_problem", "backup_missing"),
        ("host_problem", "scrub_never"),
    ]
    assert len(status) == 1
    assert problem.context.id == status[0].context.id

    described = {}
    hass.config.language = "de"
    async_describe_events(hass, lambda domain, event_type, fn: described.update({event_type: fn}))
    assert described[EVENT_HOST_STATUS](status[0])["message"] == (
        "Zustand Warnung: kein Backup-Job, noch nie gescrubbt /mnt/storage"
    )
    assert described[EVENT_ALERT](alerts[1])["message"] == (
        "Host-Problem: noch nie gescrubbt /mnt/storage"
    )

    await report(_host([BACKUP_MISSING]))
    assert issues.async_get_issue(DOMAIN, "host_scrub_never_mnt_storage") is None
    assert len(status) == 2


async def test_invalid_host(hass, entry, report):
    """Malformed host reports are rejected."""
    assert await report({"status": "broken"}) == 400
    assert await report({"problems": "none"}) == 400
