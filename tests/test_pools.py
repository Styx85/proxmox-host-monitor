"""Tests for Btrfs pools, alerts and disk removal from Home Assistant."""

from copy import deepcopy

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.proxmox_host_monitor.const import (
    CONF_REBOOT_BUTTON,
    CONF_WEBHOOK_ID,
    DOMAIN,
    EVENT_ALERT,
)
from custom_components.proxmox_host_monitor.repairs import async_create_fix_flow

WEBHOOK_ID = "test_webhook"
GB = 1000**3
UUID = "ecdb9557"

GOOD = {"name": "sda", "model": "ST4000", "serial": "GOOD", "status": "ok", "problems": []}
BAD = {
    "name": "sde",
    "model": "WD1001",
    "serial": "BAD",
    "status": "critical",
    "pending_sectors": 1414,
    "problems": [{"code": "pending_sectors", "severity": "critical", "value": 1414}],
}


def _pool(missing: int = 0, balance: bool = False, operations=None, free=6000 * GB):
    return {
        "uuid": UUID,
        "mount": "/mnt/storage",
        "label": "storage",
        "size": 11000 * GB,
        "used": 900 * GB,
        "free": free,
        "profiles": ["raid1"],
        "balance_running": balance,
        "operations": operations or [],
        "devices": [
            {"devid": 1, "name": "sda", "serial": "GOOD", "missing_after_removal": 0},
            {"devid": 6, "name": "sde", "serial": "BAD", "missing_after_removal": missing},
        ],
    }


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
    """Send a report and return the response."""

    async def _report(payload):
        client = await hass_client_no_auth()
        resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", json=payload)
        await hass.async_block_till_done()
        return await resp.json()

    return _report


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def _state(hass: HomeAssistant, domain: str, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return hass.states.get(entity_id)


async def test_pool_entities(hass, entry, report):
    """A pool gets free space, usage and operation entities."""
    await report({"disks": [GOOD, BAD], "pools": [_pool(balance=True)]})

    assert float(_state(hass, "sensor", f"pool_{UUID}_free").state) == 6000
    assert _state(hass, "sensor", f"pool_{UUID}_used_percentage").state == "13.0"
    assert _state(hass, "binary_sensor", f"pool_{UUID}_operation").state == "on"
    assert _state(hass, "binary_sensor", "disk_BAD_removable").state == "on"


async def test_failing_disk_alerts_once(hass, entry, report):
    """A critical disk raises a fixable issue and one alert event."""
    events = async_capture_events(hass, EVENT_ALERT)
    await report({"disks": [GOOD, BAD], "pools": [_pool()]})
    await report({"disks": [GOOD, BAD], "pools": [_pool()]})

    issue = _issue(hass, "disk_failing_BAD")
    assert issue.translation_key == "disk_failing_removable"
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert [event.data["type"] for event in events] == ["disk_failing"]
    assert events[0].data["removable"] is True
    assert events[0].data["problems"] == ["pending_sectors: 1414"]

    healthy = deepcopy(BAD) | {"status": "ok", "problems": []}
    await report({"disks": [GOOD, healthy], "pools": [_pool()]})
    assert _issue(hass, "disk_failing_BAD") is None


@pytest.mark.parametrize(
    ("pool", "key", "fixable"),
    [
        (_pool(balance=True), "disk_failing_wait", False),
        (_pool(missing=250 * GB), "disk_failing_no_space", False),
    ],
)
async def test_failing_disk_not_removable(hass, entry, report, pool, key, fixable):
    """Without space or during a balance the disk cannot be removed yet."""
    events = async_capture_events(hass, EVENT_ALERT)
    await report({"disks": [GOOD, BAD], "pools": [pool]})
    issue = _issue(hass, "disk_failing_BAD")
    assert issue.translation_key == key
    assert issue.is_fixable is fixable
    if key == "disk_failing_no_space":
        assert issue.translation_placeholders["missing"] == "250"
        assert issue.translation_placeholders["recommended"] == "500"
        assert events[0].data["recommended_gb"] == 500


async def test_failing_disk_outside_pool(hass, entry, report):
    """A failing disk without a pool gets a plain issue."""
    await report({"disks": [BAD]})
    assert _issue(hass, "disk_failing_BAD").translation_key == "disk_failing"


async def test_removal_flow(hass, entry, report):
    """Confirming the repair queues the removal and follows its progress."""
    events = async_capture_events(hass, EVENT_ALERT)
    await report({"disks": [GOOD, BAD], "pools": [_pool()]})

    flow = await async_create_fix_flow(
        hass,
        "disk_failing_BAD",
        {"entry_id": entry.entry_id, "serial": "BAD", "pool": UUID},
    )
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["step_id"] == "confirm_removal"
    assert result["description_placeholders"]["pool"] == "/mnt/storage"
    result = await flow.async_step_confirm_removal({})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # The reboot report tells the host to fetch the command.
    assert (await report({"required": False}))["pending_commands"] is True

    # The disk report delivers it exactly once.
    response = await report({"disks": [GOOD, BAD], "pools": [_pool()]})
    assert response["commands"] == [
        {"action": "btrfs_remove", "serial": "BAD", "pool": UUID}
    ]
    assert _issue(hass, "disk_failing_BAD") is None
    response = await report({"disks": [GOOD, BAD], "pools": [_pool()]})
    assert "commands" not in response

    running = [{"action": "btrfs_remove", "serial": "BAD", "state": "running"}]
    await report({"disks": [GOOD, BAD], "pools": [_pool(operations=running)]})
    assert _state(hass, "binary_sensor", f"pool_{UUID}_operation").state == "on"
    done = [{"action": "btrfs_remove", "serial": "BAD", "state": "done"}]
    removed = _pool(operations=done)
    removed["devices"] = removed["devices"][:1]
    await report({"disks": [GOOD, BAD], "pools": [removed]})

    # The disk is still connected and failing, now outside the pool.
    assert _issue(hass, "disk_failing_BAD").translation_key == "disk_failing"

    assert [event.data["type"] for event in events] == [
        "disk_failing",
        "removal_requested",
        "removal_started",
        "removal_finished",
    ]


async def test_rejected_removal(hass, entry, report):
    """A removal the host refuses raises an issue and the failure issue returns."""
    await report({"disks": [GOOD, BAD], "pools": [_pool()]})
    flow = await async_create_fix_flow(
        hass,
        "disk_failing_BAD",
        {"entry_id": entry.entry_id, "serial": "BAD", "pool": UUID},
    )
    flow.hass = hass
    await flow.async_step_confirm_removal({})
    await report({"disks": [GOOD, BAD], "pools": [_pool()]})

    await report(
        {
            "disks": [GOOD, BAD],
            "pools": [_pool(balance=True)],
            "command_results": [
                {"action": "btrfs_remove", "serial": "BAD", "result": "a balance is running"}
            ],
        }
    )
    failed = _issue(hass, "removal_failed_BAD")
    assert failed.translation_placeholders["reason"] == "a balance is running"
    assert _issue(hass, "disk_failing_BAD").translation_key == "disk_failing_wait"


async def test_pool_space_low(hass, entry, report):
    """A nearly full pool raises a warning and one event."""
    events = async_capture_events(hass, EVENT_ALERT)
    await report({"disks": [GOOD], "pools": [_pool(free=50 * GB)]})
    issue = _issue(hass, f"pool_space_low_{UUID}")
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["free"] == "50"
    assert [event.data["type"] for event in events] == ["pool_space_low"]

    await report({"disks": [GOOD], "pools": [_pool()]})
    assert _issue(hass, f"pool_space_low_{UUID}") is None


async def test_invalid_pools(hass, entry, report):
    """Malformed pool reports are rejected."""
    assert "error" in await report({"disks": [GOOD], "pools": [{"mount": "/x"}]})
