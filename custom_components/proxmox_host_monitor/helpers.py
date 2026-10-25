"""Helpers shared by entities and alerts."""

from typing import Any

from .const import OPERATION_BALANCE, OPERATION_IDLE, OPERATION_REMOVAL, OPERATION_SCRUB


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
    """Return what runs on a Btrfs filesystem: a removal, a balance, a scrub or nothing."""
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
    scrub = pool.get("scrub") or {}
    if scrub.get("status") == "running":
        return {"operation": OPERATION_SCRUB, "disks": [], "progress": scrub.get("progress")}
    return {"operation": OPERATION_IDLE, "disks": [], "progress": None}


def host_problem_key(problem: dict[str, Any]) -> str:
    """Return a stable key of a host problem: its code and the affected item."""
    return f"{problem.get('code')}:{problem.get('item') or ''}"


def format_host_problems(host: dict[str, Any]) -> list[str]:
    """Return the host problems as short, language-neutral strings."""
    result = []
    for problem in host.get("problems", []):
        if not isinstance(problem, dict) or "code" not in problem:
            continue
        text = problem["code"]
        if problem.get("item"):
            text += f" {problem['item']}"
        if problem.get("value") is not None:
            text += f": {problem['value']}"
        result.append(text)
    return result


def scrub_result(scrub: dict[str, Any]) -> str:
    """Return ok, repaired, uncorrectable or aborted for a finished scrub."""
    if scrub.get("uncorrectable"):
        return "uncorrectable"
    if scrub.get("status") != "finished":
        return "aborted"
    if scrub.get("errors") or scrub.get("corrected"):
        return "repaired"
    return "ok"
