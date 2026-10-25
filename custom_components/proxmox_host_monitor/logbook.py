"""Describe Proxmox Host Monitor events in the logbook."""

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ALERT, EVENT_DISK_STATUS, EVENT_HEALTH_STATUS, EVENT_OPERATION

# The logbook does not translate custom event descriptions, so the few texts
# are kept here for the languages the integration ships.
TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "ok": "OK",
        "warning": "warning",
        "critical": "critical",
        "status": "health {status}",
        "no_problems": "no problems",
        "critical_disks": "critical: {disks}",
        "warning_disks": "warning: {disks}",
        "all_ok": "all disks OK",
        "balance": "balance running",
        "balance_progress": "balance running ({progress} % done)",
        "removal": "removing {disks}",
        "idle": "no operation running",
        "disk_failing": "disk {disk} is failing",
        "pool_space_low": "{pool} is running out of space",
        "removal_requested": "removal of {disk} requested",
        "removal_started": "removal of {disk} started",
        "removal_finished": "removal of {disk} finished",
        "removal_failed": "removal of {disk} failed: {reason}",
        "pending_sectors": "pending sectors",
        "offline_uncorrectable": "uncorrectable sectors",
        "reallocated_sectors": "reallocated sectors",
        "reported_uncorrectable": "reported uncorrectable errors",
        "command_timeouts": "command timeouts",
        "crc_errors": "CRC errors (cable)",
        "btrfs_errors": "Btrfs errors",
        "smart_failed": "SMART status failed",
        "failure_predicted": "failure predicted",
        "prefail_in_past": "attribute below threshold in the past",
        "error_log": "entries in the SMART error log",
        "self_test_failed": "self-test with errors",
        "self_test_overdue": "self-test overdue (hours)",
        "nvme_critical_warning": "NVMe critical warning",
        "media_errors": "NVMe media errors",
        "available_spare": "NVMe spare",
        "percentage_used": "NVMe wear",
        "temperature": "temperature",
    },
    "de": {
        "ok": "OK",
        "warning": "Warnung",
        "critical": "kritisch",
        "status": "Zustand {status}",
        "no_problems": "keine Probleme",
        "critical_disks": "kritisch: {disks}",
        "warning_disks": "Warnung: {disks}",
        "all_ok": "alle Platten OK",
        "balance": "Balance läuft",
        "balance_progress": "Balance läuft ({progress} % erledigt)",
        "removal": "Ausbau von {disks} läuft",
        "idle": "kein Vorgang mehr",
        "disk_failing": "Festplatte {disk} fällt aus",
        "pool_space_low": "{pool} wird voll",
        "removal_requested": "Ausbau von {disk} beauftragt",
        "removal_started": "Ausbau von {disk} gestartet",
        "removal_finished": "Ausbau von {disk} fertig",
        "removal_failed": "Ausbau von {disk} fehlgeschlagen: {reason}",
        "pending_sectors": "Pending-Sektoren",
        "offline_uncorrectable": "nicht korrigierbare Sektoren",
        "reallocated_sectors": "reallozierte Sektoren",
        "reported_uncorrectable": "gemeldete Lesefehler",
        "command_timeouts": "Befehls-Timeouts",
        "crc_errors": "CRC-Fehler (Kabel)",
        "btrfs_errors": "Btrfs-Fehler",
        "smart_failed": "SMART-Status FAILED",
        "failure_predicted": "Ausfall vorhergesagt",
        "prefail_in_past": "Attribut früher unter Grenzwert",
        "error_log": "Einträge im SMART-Fehlerprotokoll",
        "self_test_failed": "Selbsttest mit Fehlern",
        "self_test_overdue": "Selbsttest überfällig (Stunden)",
        "nvme_critical_warning": "NVMe Critical Warning",
        "media_errors": "NVMe Medienfehler",
        "available_spare": "NVMe Reserve",
        "percentage_used": "NVMe Verschleiß",
        "temperature": "Temperatur",
    },
}


def describe_problem(texts: dict[str, str], problem: str) -> str:
    """Translate a problem like "pending_sectors: 1414"."""
    code, _, value = problem.partition(":")
    label = texts.get(code, code)
    return f"{label}:{value}" if value else label


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe logbook events."""
    texts = TEXTS.get(hass.config.language.split("-")[0], TEXTS["en"])

    @callback
    def describe_status(event: Event) -> dict[str, str]:
        data: dict[str, Any] = event.data
        problems = [describe_problem(texts, p) for p in data.get("problems", [])]
        status = texts["status"].format(status=texts.get(data["status"], data["status"]))
        return {
            "name": f"{data.get('model', '')} {data['disk']}".strip(),
            "message": f"{status}: {', '.join(problems) or texts['no_problems']}",
        }

    @callback
    def describe_alert(event: Event) -> dict[str, str]:
        data = {"disk": "", "pool": "", "reason": "", **event.data}
        template = texts.get(data["type"], data["type"])
        return {"name": "Proxmox Host Monitor", "message": template.format(**data)}

    @callback
    def describe_health(event: Event) -> dict[str, str]:
        data: dict[str, Any] = event.data
        status = texts["status"].format(status=texts.get(data["status"], data["status"]))
        details = [
            texts[f"{kind}_disks"].format(disks=", ".join(data[kind]))
            for kind in ("critical", "warning")
            if data.get(kind)
        ]
        return {
            "name": data["scope"],
            "message": f"{status}: {' · '.join(details) or texts['all_ok']}",
        }

    @callback
    def describe_operation(event: Event) -> dict[str, str]:
        data: dict[str, Any] = event.data
        operation = data["operation"]
        if operation == "balance" and data.get("progress") is not None:
            message = texts["balance_progress"].format(progress=data["progress"])
        else:
            message = texts[operation].format(disks=", ".join(data.get("disks", [])))
        return {"name": data["scope"], "message": message}

    async_describe_event(DOMAIN, EVENT_DISK_STATUS, describe_status)
    async_describe_event(DOMAIN, EVENT_HEALTH_STATUS, describe_health)
    async_describe_event(DOMAIN, EVENT_OPERATION, describe_operation)
    async_describe_event(DOMAIN, EVENT_ALERT, describe_alert)
