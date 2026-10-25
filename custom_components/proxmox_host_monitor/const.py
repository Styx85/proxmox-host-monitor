"""Constants for Proxmox Host Monitor."""

from datetime import timedelta

DOMAIN = "proxmox_host_monitor"

# Predecessor integration whose configuration is imported on setup.
LEGACY_DOMAIN = "proxmox_reboot_update"

CONF_REBOOT_BUTTON = "reboot_button"
CONF_WEBHOOK_ID = "webhook_id"
CONF_HOST_KEY = "host_key"

SIGNAL_REBOOT_STATUS = f"{DOMAIN}_reboot_status"
SIGNAL_DISK_REPORT = f"{DOMAIN}_disk_report"
SIGNAL_NEW_DISKS = f"{DOMAIN}_new_disks"

# The Proxmox timer reports every five minutes; allow two missed reports.
STATUS_TIMEOUT = timedelta(minutes=15)

# Give up waiting for the host to come back after this long.
REBOOT_TIMEOUT = timedelta(minutes=30)

# The disk health timer reports every 15 minutes; allow two missed reports.
DISK_REPORT_TIMEOUT = timedelta(minutes=45)

STORAGE_KEY = f"{DOMAIN}.disks"
STORAGE_VERSION = 1

DISK_STATUS_OK = "ok"
DISK_STATUS_WARNING = "warning"
DISK_STATUS_CRITICAL = "critical"
DISK_STATUSES = (DISK_STATUS_OK, DISK_STATUS_WARNING, DISK_STATUS_CRITICAL)

INSTALL_URL = "/api/proxmox_host_monitor/install/{webhook_id}"
PENDING_INSTALLS = "pending_installs"

ISSUE_REBOOT_BUTTON_UNAVAILABLE = "reboot_button_unavailable"
