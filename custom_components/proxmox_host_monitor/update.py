"""Update entities for a pending Proxmox reboot and for the containers."""

import asyncio
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import ProxmoxHostConfigEntry, async_save
from .button_monitor import get_reboot_button
from .const import (
    ACTION_GUEST_UPDATE,
    DOMAIN,
    GUEST_PREFIX,
    REBOOT_TIMEOUT,
    SIGNAL_GUEST_REPORT,
    SIGNAL_NEW_GUESTS,
    SIGNAL_REBOOT_STATUS,
    STATUS_TIMEOUT,
)
from .entity import _via_node
from .guests import (
    app_update,
    async_queue_guest_command,
    update_available,
    update_running,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Proxmox reboot update entity and one per container."""
    async_add_entities([ProxmoxRebootUpdate(hass, entry)])

    added: set[str] = set()

    @callback
    def _add_guests(vmids: list[str]) -> None:
        new = [vmid for vmid in vmids if vmid not in added]
        added.update(new)
        async_add_entities([ProxmoxGuestUpdate(entry, vmid) for vmid in new])

    _add_guests(list(entry.runtime_data.guests))
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{SIGNAL_NEW_GUESTS}_{entry.entry_id}", _add_guests
        )
    )


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
        entry: ProxmoxHostConfigEntry,
    ) -> None:
        """Initialize the entity."""
        self._entry = entry
        self._reboot_button = get_reboot_button(entry)
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

        # Home Assistant silently skips unavailable entities on service calls,
        # which would leave the update in progress without a reboot.
        button_state = self.hass.states.get(self._reboot_button)
        if button_state is None or button_state.state == STATE_UNAVAILABLE:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="reboot_button_unavailable",
                translation_placeholders={"entity_id": self._reboot_button},
            )

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


# Release summaries are limited to 255 characters.
SUMMARY_LENGTH = 255


class ProxmoxGuestUpdate(UpdateEntity):
    """Pending package and app updates of a container.

    Installing takes a snapshot, upgrades the packages and runs the update of
    the Proxmox VE community scripts. The host picks up the request with its
    next report, so the update shows as in progress until the host is done.
    """

    _attr_translation_key = "guest_update"
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_auto_update = False
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, entry: ProxmoxHostConfigEntry, vmid: str) -> None:
        """Initialize the entity."""
        self._entry = entry
        self._vmid = vmid
        self._attr_unique_id = f"{GUEST_PREFIX}{vmid}_update"
        guest = self._guest
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{GUEST_PREFIX}{vmid}")},
            name=guest.get("name") or f"CT {vmid}",
            model="LXC",
            serial_number=vmid,
            sw_version=guest.get("os"),
            **_via_node(entry),
        )

    async def async_added_to_hass(self) -> None:
        """Update the state whenever the host reports."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_GUEST_REPORT}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )

    @property
    def _guest(self) -> dict[str, Any]:
        return self._entry.runtime_data.guests.get(self._vmid, {})

    @property
    def available(self) -> bool:
        """Return whether the container is running and was reported recently."""
        guest = self._guest
        return (
            self._entry.runtime_data.guests_current
            and guest.get("status") == "running"
            and "packages" in guest
        )

    @property
    def title(self) -> str | None:
        """Return the app or, without one, the operating system."""
        guest = self._guest
        return (guest.get("app") or {}).get("app") or guest.get("os")

    @property
    def installed_version(self) -> str | None:
        """Return the app version or the operating system."""
        guest = self._guest
        return (guest.get("app") or {}).get("installed") or guest.get("os")

    @property
    def latest_version(self) -> str | None:
        """Return the newest app version, marked with the pending packages."""
        guest = self._guest
        app = guest.get("app") or {}
        version = app["latest"] if app_update(guest) else self.installed_version
        if packages := len(guest.get("packages", [])):
            return f"{version} (+{packages})"
        return version

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Tell Home Assistant whether an update should be shown."""
        return update_available(self._guest)

    @property
    def release_summary(self) -> str | None:
        """Return the app change and the packages to upgrade."""
        guest = self._guest
        app = guest.get("app") or {}
        lines = []
        if app_update(guest):
            lines.append(f"{app.get('app')}: {app.get('installed')} → {app['latest']}")
        packages = guest.get("packages", [])
        if packages:
            lines.append(f"+{len(packages)}: " + ", ".join(p["name"] for p in packages))
        summary = "\n".join(lines)
        if len(summary) > SUMMARY_LENGTH:
            summary = summary[: SUMMARY_LENGTH - 1] + "…"
        return summary or None

    @property
    def release_url(self) -> str | None:
        """Return the release notes of the newest app version."""
        guest = self._guest
        return (guest.get("app") or {}).get("release_url") if app_update(guest) else None

    @property
    def in_progress(self) -> bool:
        """Return whether an update is requested or running on the host."""
        return update_running(self._entry, self._vmid)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the pending packages and the last update of the container."""
        guest = self._guest
        action = guest.get("last_action") or {}
        return {
            "vmid": int(self._vmid),
            "packages": len(guest.get("packages", [])),
            "last_action": action.get("action"),
            "last_action_state": action.get("state"),
            "last_action_finished": action.get("finished"),
            "last_action_error": action.get("error"),
            "snapshot": action.get("snapshot"),
        }

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Queue the update; the host runs it after its next report."""
        if not self.available:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="guest_unavailable",
                translation_placeholders={"guest": self._guest.get("name", self._vmid)},
            )
        if update_running(self._entry, self._vmid):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="guest_update_running",
                translation_placeholders={"guest": self._guest.get("name", self._vmid)},
            )
        async_queue_guest_command(self._entry, ACTION_GUEST_UPDATE, self._vmid)
        async_save(self._entry)
        self.async_write_ha_state()
