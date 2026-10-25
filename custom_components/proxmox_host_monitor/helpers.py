"""Helpers shared by entities and alerts."""

from typing import Any


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
