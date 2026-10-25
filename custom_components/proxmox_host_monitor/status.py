"""Announce health changes so the logbook shows their reason.

Every change of a disk, a Btrfs filesystem or the whole host fires an event.
The affected entities write their next state in the context of that event,
so Home Assistant shows the event as the cause of the state change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, HomeAssistant, callback

from .const import (
    DISK_STATUS_CRITICAL,
    DISK_STATUS_OK,
    DISK_STATUS_WARNING,
    DISK_STATUSES,
    EVENT_DISK_STATUS,
    EVENT_HEALTH_STATUS,
    EVENT_OPERATION,
    HOST_SCOPE,
    OPERATION_IDLE,
    OPERATION_SUFFIX,
    POOL_PREFIX,
)
from .helpers import format_problems, pool_operation

if TYPE_CHECKING:
    from . import ProxmoxHostConfigEntry


def _problem_codes(disk: dict[str, Any] | None) -> list[str]:
    return [
        problem.get("code", "")
        for problem in (disk or {}).get("problems", [])
        if isinstance(problem, dict)
    ]


def health_summary(disks: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the worst status and the critical and warning disks."""
    return {
        "status": max(
            (disk["status"] for disk in disks),
            key=DISK_STATUSES.index,
            default=DISK_STATUS_OK,
        ),
        DISK_STATUS_CRITICAL: [
            disk["name"] for disk in disks if disk["status"] == DISK_STATUS_CRITICAL
        ],
        DISK_STATUS_WARNING: [
            disk["name"] for disk in disks if disk["status"] == DISK_STATUS_WARNING
        ],
    }


def _scopes(entry: ProxmoxHostConfigEntry) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Return the host and every filesystem with their reported disks."""
    runtime_data = entry.runtime_data
    present = [
        runtime_data.disks[serial]
        for serial in sorted(runtime_data.present)
        if serial in runtime_data.disks
    ]
    node = runtime_data.node_device
    scopes = {
        HOST_SCOPE: ((node.name_by_user or node.name) if node else "Proxmox", present)
    }
    for uuid, pool in runtime_data.pools.items():
        serials = {device.get("serial") for device in pool.get("devices", [])}
        scopes[f"{POOL_PREFIX}{uuid}"] = (
            f"Btrfs {pool.get('label') or pool.get('mount')}",
            [disk for disk in present if disk["serial"] in serials],
        )
    return scopes


@callback
def async_announce_changes(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    previous: dict[str, dict[str, Any]] | None,
) -> None:
    """Fire events for changed health; previous=None announces after a restart.

    After a restart only problems are announced, so the restored states of
    unhealthy disks and filesystems show their reason as well.
    """
    runtime_data = entry.runtime_data

    for serial in sorted(runtime_data.present):
        disk = runtime_data.disks.get(serial)
        if disk is None:
            continue
        if previous is None:
            changed = disk["status"] != DISK_STATUS_OK
            before = None
        else:
            before = previous.get(serial)
            changed = before is None or (before["status"], _problem_codes(before)) != (
                disk["status"],
                _problem_codes(disk),
            )
        if changed:
            _fire(
                hass,
                entry,
                serial,
                EVENT_DISK_STATUS,
                {
                    "disk": disk["name"],
                    "model": disk.get("model") or "",
                    "serial": serial,
                    "status": disk["status"],
                    "previous": before["status"] if before else None,
                    "problems": format_problems(disk),
                },
            )

    for key, (name, disks) in _scopes(entry).items():
        summary = health_summary(disks)
        if previous is None:
            changed = summary["status"] != DISK_STATUS_OK
        else:
            changed = runtime_data.summaries.get(key) != summary
        runtime_data.summaries[key] = summary
        if changed:
            _fire(hass, entry, key, EVENT_HEALTH_STATUS, {"scope": name, **summary})

    for uuid, pool in runtime_data.pools.items():
        key = f"{POOL_PREFIX}{uuid}{OPERATION_SUFFIX}"
        operation = pool_operation(pool, runtime_data.disks)
        # The progress changes with every report and is no reason to announce.
        state = {"operation": operation["operation"], "disks": operation["disks"]}
        if previous is None:
            changed = state["operation"] != OPERATION_IDLE
        else:
            changed = runtime_data.summaries.get(key) != state
        runtime_data.summaries[key] = state
        if changed:
            _fire(
                hass,
                entry,
                key,
                EVENT_OPERATION,
                {"scope": f"Btrfs {pool.get('label') or pool.get('mount')}", **operation},
            )


@callback
def _fire(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    key: str,
    event_type: str,
    data: dict[str, Any],
) -> None:
    context = Context()
    entry.runtime_data.contexts[key] = context
    hass.bus.async_fire(event_type, data, context=context)
