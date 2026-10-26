"""Tests for the container updates."""

from http import HTTPStatus

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.proxmox_host_monitor.const import (
    CONF_REBOOT_BUTTON,
    CONF_WEBHOOK_ID,
    DOMAIN,
    EVENT_ALERT,
)
from custom_components.proxmox_host_monitor.repairs import async_create_fix_flow

WEBHOOK_ID = "test_webhook"
BUTTON = "button.pve_reboot"
ENTITY_ID = "update.immich_update"

IMMICH = {
    "vmid": 103,
    "name": "immich",
    "status": "running",
    "os": "Debian GNU/Linux 13 (trixie)",
    "packages": [],
    "app": {
        "script": "immich",
        "app": "Immich",
        "installed": "3.3.5",
        "latest": "3.3.5",
        "release_url": "https://github.com/immich-app/immich/releases/tag/v3.3.5",
    },
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


async def _report(hass_client_no_auth, hass, payload) -> dict:
    client = await hass_client_no_auth()
    response = await client.post(f"/api/webhook/{WEBHOOK_ID}", json=payload)
    assert response.status == HTTPStatus.OK
    await hass.async_block_till_done()
    return await response.json()


def _guest(**changes) -> dict:
    guest = {**IMMICH, **changes}
    if "app" in changes:
        guest["app"] = {**IMMICH["app"], **changes["app"]}
    return guest


async def test_current_container(hass: HomeAssistant, entry, hass_client_no_auth):
    """A container without pending updates gets an update entity that is off."""
    await _report(hass_client_no_auth, hass, {"guests": [IMMICH]})

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_OFF
    assert state.attributes["title"] == "Immich"
    assert state.attributes["installed_version"] == "3.3.5"
    assert state.attributes["latest_version"] == "3.3.5"
    assert state.attributes["vmid"] == 103


async def test_app_and_package_updates(hass: HomeAssistant, entry, hass_client_no_auth):
    """A newer app version and pending packages show as an update."""
    await _report(
        hass_client_no_auth,
        hass,
        {"guests": [_guest(
            app={"latest": "3.4.0", "release_url": "https://example.org/v3.4.0"},
            packages=[{"name": "libc6", "installed": "1", "version": "2"}],
        )]},
    )

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes["latest_version"] == "3.4.0 (+1)"
    assert state.attributes["release_summary"] == "Immich: 3.3.5 → 3.4.0\n+1: libc6"
    assert state.attributes["release_url"] == "https://example.org/v3.4.0"


async def test_packages_only(hass: HomeAssistant, entry, hass_client_no_auth):
    """Without an app the operating system is the installed version."""
    guest = _guest(packages=[{"name": "jellyfin", "installed": "1", "version": "2"}])
    del guest["app"]
    await _report(hass_client_no_auth, hass, {"guests": [guest]})

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes["installed_version"] == "Debian GNU/Linux 13 (trixie)"
    assert state.attributes["latest_version"] == "Debian GNU/Linux 13 (trixie) (+1)"
    assert state.attributes["release_url"] is None


async def test_stopped_container_unavailable(hass: HomeAssistant, entry, hass_client_no_auth):
    """A stopped container cannot be checked or updated."""
    await _report(hass_client_no_auth, hass, {"guests": [{"vmid": 103, "name": "immich", "status": "stopped"}]})
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE

    # Home Assistant skips unavailable entities, so nothing is queued.
    await hass.services.async_call(
        "update", "install", {"entity_id": ENTITY_ID}, blocking=True
    )
    assert not entry.runtime_data.guest_commands


async def test_invalid_guest_report(hass: HomeAssistant, entry, hass_client_no_auth):
    """Malformed guest reports are rejected."""
    client = await hass_client_no_auth()
    response = await client.post(
        f"/api/webhook/{WEBHOOK_ID}", json={"guests": [{"vmid": "103"}]}
    )
    assert response.status == HTTPStatus.BAD_REQUEST


async def test_install_and_finish(hass: HomeAssistant, entry, hass_client_no_auth):
    """Installing queues a command for the host and follows its result."""
    events = async_capture_events(hass, EVENT_ALERT)
    await _report(hass_client_no_auth, hass, {"guests": [_guest(app={"latest": "3.4.0"})]})

    await hass.services.async_call("update", "install", {"entity_id": ENTITY_ID}, blocking=True)
    assert hass.states.get(ENTITY_ID).attributes["in_progress"] is True

    # The reboot status tells the host to fetch the command.
    response = await _report(hass_client_no_auth, hass, {"required": False})
    assert response["pending_guest_commands"] is True

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("update", "install", {"entity_id": ENTITY_ID}, blocking=True)

    response = await _report(hass_client_no_auth, hass, {"guests": [_guest(app={"latest": "3.4.0"})]})
    (command,) = response["guest_commands"]
    assert command["action"] == "guest_update"
    assert command["vmid"] == 103
    command_id = command["id"]

    # The command is delivered only once.
    response = await _report(hass_client_no_auth, hass, {"required": False})
    assert response["pending_guest_commands"] is False

    running = {"id": command_id, "action": "update", "state": "running", "snapshot": "phm_1"}
    await _report(
        hass_client_no_auth,
        hass,
        {
            "guests": [_guest(app={"latest": "3.4.0"}, last_action=running)],
            "command_results": [{**command, "result": "started"}],
        },
    )
    assert hass.states.get(ENTITY_ID).attributes["in_progress"] is True
    assert not events

    done = {**running, "state": "done", "from": "3.3.5", "to": "3.4.0"}
    for _ in range(2):
        await _report(
            hass_client_no_auth,
            hass,
            {"guests": [_guest(app={"installed": "3.4.0", "latest": "3.4.0"}, last_action=done)]},
        )
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_OFF
    assert state.attributes["in_progress"] is False
    assert state.attributes["last_action_state"] == "done"
    # Announced once.
    assert [event.data["type"] for event in events] == ["guest_update_finished"]
    assert events[0].data["version_to"] == "3.4.0"


async def test_failed_update_and_rollback(hass: HomeAssistant, entry, hass_client_no_auth):
    """A failed update raises a repair issue that rolls the container back."""
    events = async_capture_events(hass, EVENT_ALERT)
    failed = {
        "id": "abcdef0123456789",
        "action": "update",
        "state": "failed",
        "snapshot": "phm_20261026_120000",
        "error": "Immich is still 3.3.5",
    }
    await _report(hass_client_no_auth, hass, {"guests": [_guest(last_action=failed)]})
    assert [event.data["type"] for event in events] == ["guest_update_failed"]

    issue_id = "guest_update_failed_103"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_rollback"
    result = await flow.async_step_confirm_rollback({})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    response = await _report(hass_client_no_auth, hass, {"guests": [_guest(last_action=failed)]})
    (command,) = response["guest_commands"]
    assert command["action"] == "guest_rollback"
    assert command["snapshot"] == "phm_20261026_120000"

    rolled_back = {
        "id": command["id"],
        "action": "rollback",
        "state": "done",
        "snapshot": "phm_20261026_120000",
    }
    await _report(hass_client_no_auth, hass, {"guests": [_guest(last_action=rolled_back)]})
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert [event.data["type"] for event in events] == [
        "guest_update_failed",
        "guest_rollback_requested",
        "guest_rollback_finished",
    ]


async def test_refused_command(hass: HomeAssistant, entry, hass_client_no_auth):
    """A command the host refuses ends the update and is announced."""
    events = async_capture_events(hass, EVENT_ALERT)
    await _report(hass_client_no_auth, hass, {"guests": [_guest(app={"latest": "3.4.0"})]})
    await hass.services.async_call("update", "install", {"entity_id": ENTITY_ID}, blocking=True)
    response = await _report(hass_client_no_auth, hass, {"guests": [_guest(app={"latest": "3.4.0"})]})
    (command,) = response["guest_commands"]

    await _report(
        hass_client_no_auth,
        hass,
        {
            "guests": [_guest(app={"latest": "3.4.0"})],
            "command_results": [{**command, "result": "another update is running"}],
        },
    )
    assert hass.states.get(ENTITY_ID).attributes["in_progress"] is False
    assert events[-1].data["type"] == "guest_update_failed"
    assert events[-1].data["error"] == "another update is running"
