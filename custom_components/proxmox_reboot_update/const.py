"""Constants for Proxmox Reboot Update."""

from datetime import timedelta

DOMAIN = "proxmox_reboot_update"

CONF_REBOOT_BUTTON = "reboot_button"
CONF_WEBHOOK_ID = "webhook_id"

LEGACY_CONF_SOURCE_ENTITY = "source_entity"

SIGNAL_REBOOT_STATUS = f"{DOMAIN}_reboot_status"

# The Proxmox timer reports every five minutes; allow two missed reports.
STATUS_TIMEOUT = timedelta(minutes=15)

# Give up waiting for the host to come back after this long.
REBOOT_TIMEOUT = timedelta(minutes=30)

ISSUE_REBOOT_BUTTON_UNAVAILABLE = "reboot_button_unavailable"
