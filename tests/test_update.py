"""Tests for the Proxmox Reboot Update entity."""

import asyncio
from http import HTTPStatus

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
    mock_restore_cache,
)

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.proxmox_reboot_update.const import (
    CONF_REBOOT_BUTTON,
    CONF_WEBHOOK_ID,
    DOMAIN,
    REBOOT_TIMEOUT,
    STATUS_TIMEOUT,
)

ENTITY_ID = "update.reboot_required"
WEBHOOK_ID = "test_webhook"
BUTTON = "button.pve_reboot"


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the integration."""
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=DOMAIN,
        title="Proxmox Reboot Update",
        data={CONF_REBOOT_BUTTON: BUTTON, CONF_WEBHOOK_ID: WEBHOOK_ID},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _post(hass_client_no_auth, payload) -> int:
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", json=payload)
    return resp.status


def _entity_id(hass: HomeAssistant) -> str:
    entity_ids = hass.states.async_entity_ids("update")
    assert len(entity_ids) == 1
    return entity_ids[0]


async def test_unavailable_until_first_report(hass, entry, hass_client_no_auth):
    """The entity is unavailable until the host reports."""
    entity_id = _entity_id(hass)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    assert await _post(hass_client_no_auth, {"required": True}) == HTTPStatus.OK
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON


@pytest.mark.parametrize(
    "payload",
    [[1, 2], "true", {"required": "yes"}, {}],
)
async def test_invalid_payload(hass, entry, hass_client_no_auth, payload):
    """Malformed payloads are rejected without a server error."""
    assert await _post(hass_client_no_auth, payload) == HTTPStatus.BAD_REQUEST
    assert hass.states.get(_entity_id(hass)).state == STATE_UNAVAILABLE


async def test_status_timeout(hass, entry, hass_client_no_auth):
    """The entity becomes unavailable when reports stop."""
    entity_id = _entity_id(hass)
    await _post(hass_client_no_auth, {"required": False})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_OFF

    async_fire_time_changed(hass, dt_util.utcnow() + STATUS_TIMEOUT)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def test_restore_state(hass: HomeAssistant):
    """A known state survives a Home Assistant restart."""
    mock_restore_cache(hass, [State(ENTITY_ID, STATE_ON)])
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=DOMAIN,
        data={CONF_REBOOT_BUTTON: BUTTON, CONF_WEBHOOK_ID: WEBHOOK_ID},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass))
    assert state.entity_id == ENTITY_ID
    assert state.state == STATE_ON

    # The restored state expires if the host never reports.
    async_fire_time_changed(hass, dt_util.utcnow() + STATUS_TIMEOUT)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


async def _settle() -> None:
    """Let pending callbacks run while the install task keeps waiting."""
    for _ in range(20):
        await asyncio.sleep(0)


def _start_install(hass: HomeAssistant, entity_id: str):
    """Start the install service without blocking the test on the reboot."""
    return hass.async_create_background_task(
        hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        ),
        "install",
    )


async def test_install_progress(hass, entry, hass_client_no_auth):
    """Installing presses the button and shows progress until the host is back."""
    presses = async_mock_service(hass, "button", "press")
    entity_id = _entity_id(hass)
    await _post(hass_client_no_auth, {"required": True})
    await hass.async_block_till_done()

    install = _start_install(hass, entity_id)
    await _settle()
    assert len(presses) == 1
    assert presses[0].data["entity_id"] == BUTTON
    assert hass.states.get(entity_id).attributes["in_progress"] is True

    # A report that still requires a reboot keeps the progress.
    await _post(hass_client_no_auth, {"required": True})
    await _settle()
    assert hass.states.get(entity_id).attributes["in_progress"] is True
    assert not install.done()

    await _post(hass_client_no_auth, {"required": False})
    await install
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["in_progress"] is False


async def test_install_progress_timeout(hass, entry, hass_client_no_auth):
    """Progress ends if the host never confirms the reboot."""
    async_mock_service(hass, "button", "press")
    entity_id = _entity_id(hass)
    await _post(hass_client_no_auth, {"required": True})
    await hass.async_block_till_done()

    install = _start_install(hass, entity_id)
    await _settle()
    assert hass.states.get(entity_id).attributes["in_progress"] is True

    async_fire_time_changed(hass, dt_util.utcnow() + REBOOT_TIMEOUT)
    await install

    # The host comes back still requiring a reboot; progress stays cleared.
    await _post(hass_client_no_auth, {"required": True})
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes["in_progress"] is False
