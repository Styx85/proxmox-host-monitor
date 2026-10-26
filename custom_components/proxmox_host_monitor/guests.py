"""Updates of the LXC containers on the Proxmox host."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    ACTION_GUEST_ROLLBACK,
    ACTION_GUEST_UPDATE,
    DOMAIN,
    EVENT_ALERT,
    ISSUE_GUEST_UPDATE_FAILED,
)

if TYPE_CHECKING:
    from . import ProxmoxHostConfigEntry

FINISHED = ("done", "failed")


def valid_guests(guests: Any) -> bool:
    """Return whether a guest report has the expected shape."""
    return isinstance(guests, list) and all(
        isinstance(guest, dict)
        and isinstance(guest.get("vmid"), int)
        and isinstance(guest.get("name"), str)
        and isinstance(guest.get("packages", []), list)
        and isinstance(guest.get("app", {}), dict)
        and isinstance(guest.get("last_action", {}), dict)
        for guest in guests
    )


def app_update(guest: dict[str, Any]) -> bool:
    """Return whether a newer app version than the installed one exists."""
    app = guest.get("app") or {}
    return bool(app.get("latest") and app.get("installed") != app["latest"])


def update_available(guest: dict[str, Any]) -> bool:
    """Return whether packages or the app of a container can be updated."""
    return app_update(guest) or bool(guest.get("packages"))


def update_running(entry: ProxmoxHostConfigEntry, vmid: str) -> bool:
    """Return whether an update or rollback was requested or is running."""
    runtime_data = entry.runtime_data
    action = runtime_data.guests.get(vmid, {}).get("last_action") or {}
    return vmid in runtime_data.guest_requests or action.get("state") == "running"


@callback
def async_queue_guest_command(
    entry: ProxmoxHostConfigEntry,
    action: str,
    vmid: str,
    **data: Any,
) -> None:
    """Queue an update or rollback for the next guest report of the host.

    The caller persists the queue with async_save.
    """
    runtime_data = entry.runtime_data
    command_id = uuid4().hex
    runtime_data.guest_commands = [
        command
        for command in runtime_data.guest_commands
        if str(command.get("vmid")) != vmid
    ]
    runtime_data.guest_commands.append(
        {"action": action, "vmid": int(vmid), "id": command_id, **data}
    )
    runtime_data.guest_requests[vmid] = command_id


@callback
def _fire(hass: HomeAssistant, alert: str, guest: dict[str, Any], **data: Any) -> None:
    hass.bus.async_fire(
        EVENT_ALERT,
        {"type": alert, "guest": guest["name"], "vmid": guest["vmid"], **data},
    )


@callback
def async_update_guest_alerts(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    command_results: list[dict[str, Any]] | None,
) -> None:
    """Follow requested updates and announce their results once."""
    runtime_data = entry.runtime_data

    for result in command_results or []:
        vmid = str(result.get("vmid", ""))
        if result.get("action") not in (ACTION_GUEST_UPDATE, ACTION_GUEST_ROLLBACK):
            continue
        if result.get("result") == "started" or vmid not in runtime_data.guests:
            continue
        runtime_data.guest_requests.pop(vmid, None)
        _fire(
            hass,
            f"{result['action']}_failed",
            runtime_data.guests[vmid],
            error=str(result.get("result", "")),
        )

    for vmid, guest in runtime_data.guests.items():
        action = guest.get("last_action") or {}
        action_id = action.get("id")
        if not action_id:
            continue
        if runtime_data.guest_requests.get(vmid) == action_id:
            # The host picked up the command.
            runtime_data.guest_requests.pop(vmid)
        if action.get("state") not in FINISHED or runtime_data.guest_handled.get(vmid) == action_id:
            continue
        runtime_data.guest_handled[vmid] = action_id
        _async_announce(hass, entry, guest, action)


@callback
def _async_announce(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    guest: dict[str, Any],
    action: dict[str, Any],
) -> None:
    """Fire the result event and raise or clear the repair issue."""
    issue_id = ISSUE_GUEST_UPDATE_FAILED.format(vmid=guest["vmid"])
    failed = action["state"] == "failed"
    data = {
        "snapshot": action.get("snapshot"),
        "error": action.get("error"),
    }
    if action.get("action") == "rollback":
        _fire(hass, "guest_rollback_failed" if failed else "guest_rollback_finished", guest, **data)
        if not failed:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    _fire(
        hass,
        "guest_update_failed" if failed else "guest_update_finished",
        guest,
        version_from=action.get("from"),
        version_to=action.get("to"),
        **data,
    )
    if not failed:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    snapshot = action.get("snapshot")
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=bool(snapshot),
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="guest_update_failed" if snapshot else "guest_update_failed_no_snapshot",
        translation_placeholders={
            "guest": guest["name"],
            "vmid": str(guest["vmid"]),
            "snapshot": snapshot or "",
            "error": action.get("error") or "",
        },
        data={
            "entry_id": entry.entry_id,
            "vmid": str(guest["vmid"]),
            "snapshot": snapshot or "",
        },
    )
