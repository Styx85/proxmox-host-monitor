"""Helpers shared by entities and alerts."""

from typing import Any

from .const import OPERATION_BALANCE, OPERATION_IDLE, OPERATION_REMOVAL


def format_problems(disk: dict[str, Any]) -> list[str]:
    """Return the problems of a disk as short, language-neutral strings."""
    return [
        problem["code"]
        if problem.get("value") is None
        else f"{problem['code']}: {problem['value']}"
        for problem in disk.get("problems", [])
        if isinstance(problem, dict) and "code" in problem
    ]


def free_share(pool: dict[str, Any]) -> float | None:
    """Return the share of usable space that is still free in a Btrfs pool.

    Btrfs estimates the free space after redundancy, so it is compared with
    the used data, not with the raw size of all devices.
    """
    free, used = pool.get("free"), pool.get("used")
    if free is None or used is None or free + used <= 0:
        return None
    return free / (free + used)


def pool_operation(
    pool: dict[str, Any], disks: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Return what runs on a Btrfs filesystem: a disk removal, a balance or nothing."""
    removing = [
        disks.get(operation.get("serial"), {}).get("name") or operation.get("serial")
        for operation in pool.get("operations", [])
        if operation.get("state") == "running"
    ]
    if removing:
        return {"operation": OPERATION_REMOVAL, "disks": removing, "progress": None}
    if pool.get("balance_running"):
        return {
            "operation": OPERATION_BALANCE,
            "disks": [],
            "progress": pool.get("balance_progress"),
        }
    return {"operation": OPERATION_IDLE, "disks": [], "progress": None}
