"""Update entity representing a pending Proxmox reboot."""

import asyncio
from datetime import datetime
import logging

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import ProxmoxRebootConfigEntry
from .const import (
    CONF_REBOOT_BUTTON,
    REBOOT_TIMEOUT,
    SIGNAL_REBOOT_STATUS,
    STATUS_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxRebootConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Proxmox reboot update entity."""
    async_add_entities([ProxmoxRebootUpdate(hass, entry)])


class ProxmoxRebootUpdate(UpdateEntity):
    """Expose a pending Proxmox reboot as a Home Assistant update."""

    _attr_translation_key = "reboot_required"
    _attr_has_entity_name = True
    _attr_unique_id = "proxmox_reboot_required_update"
    _attr_title = "Proxmox VE"
    _attr_icon = "mdi:restart-alert"
    _attr_auto_update = False
    _attr_should_poll = False
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ProxmoxRebootConfigEntry,
    ) -> None:
        """Initialize the entity."""
        self._entry = entry
        self._reboot_button = entry.options.get(
            CONF_REBOOT_BUTTON,
            entry.data[CONF_REBOOT_BUTTON],
        )
        self._cancel_status_timeout: CALLBACK_TYPE | None = None
        self._reboot_finished: asyncio.Event | None = None

        self.device_entry = async_entity_id_to_device(
            hass,
            self._reboot_button,
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last known state and register listeners."""
        await super().async_added_to_hass()

        runtime_data = self._entry.runtime_data
        if not runtime_data.status_received:
            last_state = await self.async_get_last_state()
            if last_state is not None and last_state.state in (
                STATE_ON,
                STATE_OFF,
            ):
                runtime_data.reboot_required = last_state.state == STATE_ON
                runtime_data.status_received = True

        # A restored state only stays valid until the next report is due.
        if runtime_data.status_received:
            self._schedule_status_timeout()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_REBOOT_STATUS}_{self._entry.entry_id}",
                self._handle_status_update,
            )
        )
        self.async_on_remove(self._async_cleanup)

    @callback
    def _async_cleanup(self) -> None:
        """Cancel the status timer and stop waiting for a reboot."""
        if self._cancel_status_timeout is not None:
            self._cancel_status_timeout()
            self._cancel_status_timeout = None
        if self._reboot_finished is not None:
            self._reboot_finished.set()

    @callback
    def _schedule_status_timeout(self) -> None:
        """(Re)start the timer that marks the host as unreachable."""
        if self._cancel_status_timeout is not None:
            self._cancel_status_timeout()
        self._cancel_status_timeout = async_call_later(
            self.hass,
            STATUS_TIMEOUT,
            self._handle_status_timeout,
        )

    @callback
    def _handle_status_timeout(self, _now: datetime) -> None:
        """Mark the entity unavailable when the host stops reporting."""
        self._cancel_status_timeout = None
        self._entry.runtime_data.status_received = False
        self.async_write_ha_state()

    @callback
    def _handle_status_update(self) -> None:
        """Handle a reboot status update."""
        self._schedule_status_timeout()

        if (
            self._reboot_finished is not None
            and not self._entry.runtime_data.reboot_required
        ):
            self._reboot_finished.set()

        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether the Proxmox host has reported its status."""
        return self._entry.runtime_data.status_received

    @property
    def installed_version(self) -> str:
        """Return a language-neutral state token."""
        return "current"

    @property
    def latest_version(self) -> str:
        """Return a language-neutral state token."""
        if self._entry.runtime_data.reboot_required:
            return "reboot_required"
        return "current"

    @property
    def release_summary(self) -> str | None:
        """Return a warning token while a reboot is pending."""
        if self._entry.runtime_data.reboot_required:
            return "reboot_warning"
        return None

    def version_is_newer(
        self,
        latest_version: str,
        installed_version: str,
    ) -> bool:
        """Tell Home Assistant whether an update should be shown."""
        return self._entry.runtime_data.reboot_required

    async def async_install(
        self,
        version: str | None,
        backup: bool,
        **kwargs,
    ) -> None:
        """Press the Proxmox reboot button and wait for the host to return.

        Home Assistant shows the update as in progress until this returns.
        """
        if not self._entry.runtime_data.reboot_required:
            return

        self._reboot_finished = asyncio.Event()
        self._attr_in_progress = True
        self.async_write_ha_state()

        try:
            await self.hass.services.async_call(
                "button",
                "press",
                {"entity_id": self._reboot_button},
                blocking=True,
            )
            async with asyncio.timeout(REBOOT_TIMEOUT.total_seconds()):
                await self._reboot_finished.wait()
        except TimeoutError:
            _LOGGER.warning(
                "Proxmox host did not report a completed reboot within %s",
                REBOOT_TIMEOUT,
            )
        finally:
            self._reboot_finished = None
