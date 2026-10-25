#!/usr/bin/env python3
"""Validate custom integration translation files against en.json."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

TRANSLATIONS_DIR = Path(
    "custom_components/proxmox_host_monitor/translations"
)
BASE_FILE = TRANSLATIONS_DIR / "en.json"

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def load_json(path: Path) -> dict[str, Any]:
    """Load and validate a JSON translation file."""
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as err:
        raise ValueError(
            f"{path}: invalid JSON: "
            f"line {err.lineno}, column {err.colno}: {err.msg}"
        ) from err

    if not isinstance(data, dict):
        raise ValueError(f"{path}: root element must be an object")

    return data


def flatten(
    value: Any,
    prefix: str = "",
) -> dict[str, Any]:
    """Flatten nested dictionaries into dotted paths."""
    result: dict[str, Any] = {}

    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            result.update(flatten(child, path))
    else:
        result[prefix] = value

    return result


def placeholders(value: Any) -> set[str]:
    """Return Home Assistant-style placeholders from a string."""
    if not isinstance(value, str):
        return set()

    return set(PLACEHOLDER_RE.findall(value))


def validate_translation(
    base: dict[str, Any],
    translated: dict[str, Any],
    path: Path,
) -> list[str]:
    """Validate one translation against the English base."""
    errors: list[str] = []

    base_flat = flatten(base)
    translated_flat = flatten(translated)

    base_keys = set(base_flat)
    translated_keys = set(translated_flat)

    for key in sorted(base_keys - translated_keys):
        errors.append(f"{path}: missing key: {key}")

    for key in sorted(translated_keys - base_keys):
        errors.append(f"{path}: unexpected key: {key}")

    for key in sorted(base_keys & translated_keys):
        base_value = base_flat[key]
        translated_value = translated_flat[key]

        if type(base_value) is not type(translated_value):
            errors.append(
                f"{path}: type mismatch at {key}: "
                f"expected {type(base_value).__name__}, "
                f"got {type(translated_value).__name__}"
            )
            continue

        if isinstance(translated_value, str):
            if not translated_value.strip():
                errors.append(
                    f"{path}: empty translation at {key}"
                )

            expected_placeholders = placeholders(base_value)
            actual_placeholders = placeholders(translated_value)

            if expected_placeholders != actual_placeholders:
                errors.append(
                    f"{path}: placeholder mismatch at {key}: "
                    f"expected {sorted(expected_placeholders)}, "
                    f"got {sorted(actual_placeholders)}"
                )

    return errors


def main() -> int:
    """Validate every translation file."""
    if not BASE_FILE.exists():
        print(f"ERROR: base translation not found: {BASE_FILE}")
        return 1

    try:
        base = load_json(BASE_FILE)
    except ValueError as err:
        print(f"ERROR: {err}")
        return 1

    translation_files = sorted(TRANSLATIONS_DIR.glob("*.json"))

    if not translation_files:
        print("ERROR: no translation files found")
        return 1

    errors: list[str] = []

    for path in translation_files:
        if path == BASE_FILE:
            continue

        try:
            translated = load_json(path)
        except ValueError as err:
            errors.append(str(err))
            continue

        errors.extend(
            validate_translation(
                base,
                translated,
                path,
            )
        )

    if errors:
        print("\nTranslation validation failed:\n")
        for error in errors:
            print(f"  - {error}")
        print(f"\n{len(errors)} error(s) found.")
        return 1

    checked = len(translation_files) - 1
    print(
        f"Translation validation successful: "
        f"{checked} translation file(s) checked against en.json."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
