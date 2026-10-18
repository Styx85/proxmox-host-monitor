"""Tests for the reboot button repair issue."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import ATTR_RESTORED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.proxmox_reboot_update.const import (
    CONF_REBOOT_BUTTON,
    CONF_WEBHOOK_ID,
    DOMAIN,
    ISSUE_REBOOT_BUTTON_UNAVAILABLE,
)
from custom_components.proxmox_reboot_update.repairs import async_create_fix_flow

BUTTON = "button.pve_reboot"
NEW_BUTTON = "button.pve_reboot_new"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=DOMAIN,
        data={CONF_REBOOT_BUTTON: BUTTON, CONF_WEBHOOK_ID: "test_webhook"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REBOOT_BUTTON_UNAVAILABLE)


async def test_no_issue_for_working_button(hass: HomeAssistant):
    """A provided button does not raise an issue, even while offline."""
    hass.states.async_set(BUTTON, "unknown")
    await _setup(hass)
    assert _issue(hass) is None

    # The host being offline is no reason to reconfigure.
    hass.states.async_set(BUTTON, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert _issue(hass) is None


@pytest.mark.parametrize("restored", [False, True])
async def test_issue_for_missing_button(hass: HomeAssistant, restored: bool):
    """A missing or no longer provided button raises a fixable issue."""
    if restored:
        hass.states.async_set(BUTTON, STATE_UNAVAILABLE, {ATTR_RESTORED: True})
    entry = await _setup(hass)

    issue = _issue(hass)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_placeholders == {"entity_id": BUTTON}
    assert issue.data == {"entry_id": entry.entry_id}

    # The issue clears once the integration provides the button again.
    hass.states.async_set(BUTTON, "unknown")
    await hass.async_block_till_done()
    assert _issue(hass) is None


async def test_issue_removed_with_entry(hass: HomeAssistant):
    """Removing the integration also removes its issue."""
    entry = await _setup(hass)
    assert _issue(hass) is not None

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass) is None


async def test_repair_flow_selects_new_button(hass: HomeAssistant):
    """The repair flow switches to a new button and clears the issue."""
    entry = await _setup(hass)
    hass.states.async_set(NEW_BUTTON, "unknown")

    flow = await async_create_fix_flow(
        hass, ISSUE_REBOOT_BUTTON_UNAVAILABLE, {"entry_id": entry.entry_id}
    )
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_button"
    assert result["description_placeholders"] == {"entity_id": BUTTON}

    result = await flow.async_step_select_button({CONF_REBOOT_BUTTON: NEW_BUTTON})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_REBOOT_BUTTON] == NEW_BUTTON
    assert _issue(hass) is None


async def test_repair_flow_without_entry(hass: HomeAssistant):
    """The repair flow aborts if the integration was removed meanwhile."""
    flow = await async_create_fix_flow(
        hass, ISSUE_REBOOT_BUTTON_UNAVAILABLE, {"entry_id": "missing"}
    )
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"
