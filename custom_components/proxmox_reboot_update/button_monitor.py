"""Watch the configured Proxmox reboot button and raise a repair issue."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_RESTORED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started

from .const import CONF_REBOOT_BUTTON, DOMAIN, ISSUE_REBOOT_BUTTON_UNAVAILABLE


def get_reboot_button(entry: ConfigEntry) -> str:
    """Return the configured reboot button entity ID."""
    return entry.options.get(
        CONF_REBOOT_BUTTON,
        entry.data[CONF_REBOOT_BUTTON],
    )


def reboot_button_usable(hass: HomeAssistant, entity_id: str) -> bool:
    """Return whether the reboot button exists and is provided by its integration.

    A temporarily unavailable button (host offline) still counts as usable;
    only a missing button or one its integration no longer provides does not.
    """
    state = hass.states.get(entity_id)
    return state is not None and not state.attributes.get(ATTR_RESTORED, False)


@callback
def async_setup_button_monitor(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create or clear the repair issue whenever the reboot button changes."""
    reboot_button = get_reboot_button(entry)

    @callback
    def _async_check(*_: object) -> None:
        if reboot_button_usable(hass, reboot_button):
            ir.async_delete_issue(hass, DOMAIN, ISSUE_REBOOT_BUTTON_UNAVAILABLE)
            return

        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_REBOOT_BUTTON_UNAVAILABLE,
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_REBOOT_BUTTON_UNAVAILABLE,
            translation_placeholders={"entity_id": reboot_button},
            data={"entry_id": entry.entry_id},
        )

    @callback
    def _async_on_started(_hass: HomeAssistant) -> None:
        # Other integrations may still be loading before Home Assistant has
        # started, so the button can legitimately be missing until then.
        _async_check()
        entry.async_on_unload(
            async_track_state_change_event(
                hass,
                [reboot_button],
                _async_on_state_change,
            )
        )

    @callback
    def _async_on_state_change(_event: Event[EventStateChangedData]) -> None:
        _async_check()

    entry.async_on_unload(async_at_started(hass, _async_on_started))
