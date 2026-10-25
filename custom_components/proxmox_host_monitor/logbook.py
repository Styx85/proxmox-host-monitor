"""Describe Proxmox Host Monitor events in the logbook."""

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    EVENT_ALERT,
    EVENT_DISK_STATUS,
    EVENT_HEALTH_STATUS,
    EVENT_HOST_STATUS,
    EVENT_OPERATION,
)

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
        "scrub": "scrub running",
        "scrub_progress": "scrub running ({progress} % done)",
        "scrub_finished": "scrub of {pool} finished: {result_text}",
        "scrub_ok": "no errors",
        "scrub_repaired": "errors repaired",
        "scrub_uncorrectable": "uncorrectable errors",
        "scrub_aborted": "not completed",
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
        "io_errors": "I/O errors (24 h)",
        "host_problem": "host problem: {code} {item}",
        "failed_unit": "service failed",
        "scrub_never": "never scrubbed",
        "scrub_overdue": "scrub overdue (days)",
        "scrub_uncorrectable": "scrub: uncorrectable errors",
        "scrub_errors": "scrub: errors",
        "backup_missing": "no backup job",
        "backup_never": "no backup yet",
        "backup_failed": "last backup failed",
        "backup_overdue": "last backup too old (days)",
        "thin_pool_full": "thin pool full (%)",
        "nic_errors": "network errors",
        "certificate_expired": "certificate expired",
        "certificate_expiring": "certificate expires (days)",
        "time_not_synchronized": "clock not synchronized",
        "unattended_upgrades_failed": "automatic updates failed",
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
        "scrub": "Scrub läuft",
        "scrub_progress": "Scrub läuft ({progress} % erledigt)",
        "scrub_finished": "Scrub von {pool} fertig: {result_text}",
        "scrub_ok": "ohne Fehler",
        "scrub_repaired": "Fehler repariert",
        "scrub_uncorrectable": "nicht reparierbare Fehler",
        "scrub_aborted": "nicht abgeschlossen",
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
        "io_errors": "I/O-Fehler (24 h)",
        "host_problem": "Host-Problem: {code} {item}",
        "failed_unit": "Dienst fehlgeschlagen",
        "scrub_never": "noch nie gescrubbt",
        "scrub_overdue": "Scrub überfällig (Tage)",
        "scrub_uncorrectable": "Scrub: nicht reparierbare Fehler",
        "scrub_errors": "Scrub: Fehler",
        "backup_missing": "kein Backup-Job",
        "backup_never": "noch keine Sicherung",
        "backup_failed": "letzte Sicherung fehlgeschlagen",
        "backup_overdue": "letzte Sicherung zu alt (Tage)",
        "thin_pool_full": "Thin-Pool voll (%)",
        "nic_errors": "Netzwerkfehler",
        "certificate_expired": "Zertifikat abgelaufen",
        "certificate_expiring": "Zertifikat läuft ab (Tage)",
        "time_not_synchronized": "Uhr nicht synchronisiert",
        "unattended_upgrades_failed": "automatische Updates fehlgeschlagen",
    },
}


def describe_problem(texts: dict[str, str], problem: str) -> str:
    """Translate a problem like "pending_sectors: 1414" or "scrub_never /mnt"."""
    head, _, value = problem.partition(":")
    code, _, item = head.partition(" ")
    label = texts.get(code, code) + (f" {item}" if item else "")
    return f"{label}:{value}" if value else label


def _linked(data: dict[str, Any], described: dict[str, str]) -> dict[str, str]:
    """Show the entry in the activity of the entity it explains."""
    if entity_id := data.get("entity_id"):
        described["entity_id"] = entity_id
    return described


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
        return _linked(
            data,
            {
                "name": f"{data.get('model', '')} {data['disk']}".strip(),
                "message": f"{status}: {', '.join(problems) or texts['no_problems']}",
            },
        )

    @callback
    def describe_alert(event: Event) -> dict[str, str]:
        data = {"disk": "", "pool": "", "reason": "", "item": "", **event.data}
        if data["type"] == "host_problem":
            data["code"] = texts.get(data.get("code", ""), data.get("code", ""))
        if data["type"] == "scrub_finished":
            data["result_text"] = texts.get(f"scrub_{data.get('result')}", data.get("result"))
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
        return _linked(
            data,
            {
                "name": data["scope"],
                "message": f"{status}: {' · '.join(details) or texts['all_ok']}",
            },
        )

    @callback
    def describe_operation(event: Event) -> dict[str, str]:
        data: dict[str, Any] = event.data
        operation = data["operation"]
        if operation in ("balance", "scrub") and data.get("progress") is not None:
            message = texts[f"{operation}_progress"].format(progress=data["progress"])
        else:
            message = texts[operation].format(disks=", ".join(data.get("disks", [])))
        return _linked(data, {"name": data["scope"], "message": message})

    @callback
    def describe_host(event: Event) -> dict[str, str]:
        data: dict[str, Any] = event.data
        problems = [describe_problem(texts, p) for p in data.get("problems", [])]
        status = texts["status"].format(status=texts.get(data["status"], data["status"]))
        return _linked(
            data,
            {
                "name": data["scope"],
                "message": f"{status}: {', '.join(problems) or texts['no_problems']}",
            },
        )

    async_describe_event(DOMAIN, EVENT_DISK_STATUS, describe_status)
    async_describe_event(DOMAIN, EVENT_HOST_STATUS, describe_host)
    async_describe_event(DOMAIN, EVENT_HEALTH_STATUS, describe_health)
    async_describe_event(DOMAIN, EVENT_OPERATION, describe_operation)
    async_describe_event(DOMAIN, EVENT_ALERT, describe_alert)
