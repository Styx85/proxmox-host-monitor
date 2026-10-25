"""Repair issues and alert events for failing disks and Btrfs pools."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import slugify

from .const import (
    DISK_STATUS_CRITICAL,
    DOMAIN,
    EVENT_ALERT,
    ISSUE_DISK_FAILING,
    ISSUE_HOST_PROBLEM,
    ISSUE_POOL_SPACE_LOW,
    ISSUE_REMOVAL_FAILED,
    POOL_FREE_WARNING,
)
from .helpers import format_problems, free_share, host_problem_key

if TYPE_CHECKING:
    from . import ProxmoxHostConfigEntry, ProxmoxHostRuntimeData

GB = 1000**3

# Removal states that hide the disk failure issue.
REMOVAL_ACTIVE = ("requested", "running")


def find_pool_device(
    runtime_data: ProxmoxHostRuntimeData, serial: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the Btrfs pool and pool device of a disk."""
    for pool in runtime_data.pools.values():
        for device in pool.get("devices", []):
            if device.get("serial") == serial:
                return pool, device
    return None, None


def removal_possible(runtime_data: ProxmoxHostRuntimeData, serial: str) -> bool | None:
    """Return whether a disk can leave its pool, or None outside a pool."""
    _, device = find_pool_device(runtime_data, serial)
    if device is None:
        return None
    return not device.get("missing_after_removal")


def _gb(value: float) -> int:
    return math.ceil(value / GB)


def recommended_size(missing: int) -> int:
    """Return a disk size in GB that covers the missing space with headroom."""
    # Round up to the next 500 GB with a quarter extra for growth.
    return max(500, math.ceil(missing * 1.25 / (500 * GB)) * 500)


@callback
def _fire(hass: HomeAssistant, alert: str, **data: Any) -> None:
    hass.bus.async_fire(EVENT_ALERT, {"type": alert, **data})


@callback
def async_update_alerts(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    command_results: list[dict[str, Any]] | None = None,
) -> None:
    """Create or clear repair issues and fire events after a report."""
    runtime_data = entry.runtime_data
    active: set[str] = set()

    for result in command_results or []:
        serial = str(result.get("serial", ""))
        if result.get("result") != "started" and serial in runtime_data.removals:
            runtime_data.removals.pop(serial)
            _async_removal_failed(hass, entry, serial, str(result.get("result", "")))

    for pool in runtime_data.pools.values():
        for operation in pool.get("operations", []):
            _async_track_removal(hass, entry, pool, operation)

    for serial, disk in runtime_data.disks.items():
        issue_id = ISSUE_DISK_FAILING.format(serial=serial)
        if (
            serial not in runtime_data.present
            or disk["status"] != DISK_STATUS_CRITICAL
            or runtime_data.removals.get(serial) in REMOVAL_ACTIVE
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue

        pool, device = find_pool_device(runtime_data, serial)
        missing = int(device.get("missing_after_removal", 0)) if device else 0
        if pool is None:
            key = "disk_failing"
        elif missing:
            key = "disk_failing_no_space"
        elif pool.get("balance_running"):
            key = "disk_failing_wait"
        else:
            key = "disk_failing_removable"

        problems = ", ".join(format_problems(disk))
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=key == "disk_failing_removable",
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=key,
            translation_placeholders={
                "disk": disk["name"],
                "model": disk.get("model") or "",
                "serial": serial,
                "problems": problems,
                "pool": pool["mount"] if pool else "",
                "missing": str(_gb(missing)),
                "recommended": str(recommended_size(missing)),
            },
            data={
                "entry_id": entry.entry_id,
                "serial": serial,
                "pool": pool["uuid"] if pool else "",
            },
        )

        # Notify once per failure and again when the situation changes.
        alert_key = f"{key}:{serial}"
        active.add(alert_key)
        # After a finished removal "removal_finished" already told the user.
        if (
            alert_key not in runtime_data.alerted
            and runtime_data.removals.get(serial) != "done"
        ):
            _fire(
                hass,
                "disk_failing",
                disk=disk["name"],
                model=disk.get("model") or "",
                serial=serial,
                problems=format_problems(disk),
                pool=pool["mount"] if pool else None,
                removable=removal_possible(runtime_data, serial),
                balance_running=bool(pool and pool.get("balance_running")),
                missing_gb=_gb(missing),
                recommended_gb=recommended_size(missing) if missing else None,
            )

    for uuid, pool in runtime_data.pools.items():
        issue_id = ISSUE_POOL_SPACE_LOW.format(uuid=uuid)
        share = free_share(pool)
        if share is None or share >= POOL_FREE_WARNING:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        free = pool["free"]

        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="pool_space_low",
            translation_placeholders={
                "pool": pool["mount"],
                "free": str(_gb(free)),
                "percentage": str(round(share * 100)),
            },
        )
        alert_key = f"pool_space_low:{uuid}"
        active.add(alert_key)
        if alert_key not in runtime_data.alerted:
            _fire(
                hass,
                "pool_space_low",
                pool=pool["mount"],
                free_gb=_gb(free),
                percentage=round(share * 100),
            )

    _async_update_host_issues(hass, entry, active)
    runtime_data.alerted = active


@callback
def _async_update_host_issues(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    active: set[str],
) -> None:
    """Raise a repair issue per host problem and alert once per problem."""
    runtime_data = entry.runtime_data
    current: set[str] = set()
    for problem in runtime_data.host.get("problems", []):
        if not isinstance(problem, dict) or "code" not in problem:
            continue
        code, item, value = problem["code"], problem.get("item") or "", problem.get("value")
        issue_id = ISSUE_HOST_PROBLEM.format(code=code, item=slugify(item) or "host")
        current.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR
            if problem.get("severity") == DISK_STATUS_CRITICAL
            else ir.IssueSeverity.WARNING,
            translation_key=f"host_{code}",
            translation_placeholders={
                "item": item,
                "value": "" if value is None else str(value),
            },
        )
        alert_key = f"host:{host_problem_key(problem)}:{problem.get('severity')}"
        active.add(alert_key)
        if alert_key not in runtime_data.alerted:
            _fire(
                hass,
                "host_problem",
                code=code,
                item=item,
                value=value,
                severity=problem.get("severity"),
            )

    for issue_id in runtime_data.host_issues - current:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    runtime_data.host_issues = current


@callback
def _async_track_removal(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    pool: dict[str, Any],
    operation: dict[str, Any],
) -> None:
    """Follow a disk removal reported by the host."""
    runtime_data = entry.runtime_data
    serial = str(operation.get("serial", ""))
    state = operation.get("state")
    previous = runtime_data.removals.get(serial)
    if state == previous or previous is None and state != "running":
        # Old units of earlier removals stay listed until the next host reboot.
        return

    runtime_data.removals[serial] = state
    disk = runtime_data.disks.get(serial, {})
    data = {"disk": disk.get("name", ""), "serial": serial, "pool": pool["mount"]}
    if state == "running":
        ir.async_delete_issue(hass, DOMAIN, ISSUE_REMOVAL_FAILED.format(serial=serial))
        _fire(hass, "removal_started", **data)
    elif state == "done":
        _fire(hass, "removal_finished", **data)
    elif state == "failed":
        runtime_data.removals.pop(serial)
        _async_removal_failed(hass, entry, serial, "btrfs device remove failed")


@callback
def _async_removal_failed(
    hass: HomeAssistant,
    entry: ProxmoxHostConfigEntry,
    serial: str,
    reason: str,
) -> None:
    disk = entry.runtime_data.disks.get(serial, {})
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_REMOVAL_FAILED.format(serial=serial),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="removal_failed",
        translation_placeholders={
            "disk": disk.get("name", ""),
            "serial": serial,
            "reason": reason,
        },
    )
    _fire(
        hass,
        "removal_failed",
        disk=disk.get("name", ""),
        serial=serial,
        reason=reason,
    )
