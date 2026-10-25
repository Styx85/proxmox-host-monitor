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
SIGNAL_NEW_POOLS = f"{DOMAIN}_new_pools"

# Fired for disk failures, low space and disk removals (for notifications).
EVENT_ALERT = f"{DOMAIN}_alert"
# Fired when the health or the problems of a disk change; the new states are
# written in its context, so the logbook shows the reason.
EVENT_DISK_STATUS = f"{DOMAIN}_disk_status"
# The same for the whole host and for every Btrfs filesystem.
EVENT_HEALTH_STATUS = f"{DOMAIN}_health_status"
# Fired when a balance or disk removal on a Btrfs filesystem starts or ends.
EVENT_OPERATION = f"{DOMAIN}_operation"

OPERATION_IDLE = "idle"
OPERATION_BALANCE = "balance"
OPERATION_REMOVAL = "removal"
OPERATIONS = (OPERATION_IDLE, OPERATION_BALANCE, OPERATION_REMOVAL)
# Context key suffix of the operation entities of a filesystem.
OPERATION_SUFFIX = "_operation"

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
ISSUE_DISK_FAILING = "disk_failing_{serial}"
ISSUE_POOL_SPACE_LOW = "pool_space_low_{uuid}"
ISSUE_REMOVAL_FAILED = "removal_failed_{serial}"

# A Btrfs pool with less free space than this share of its size is low on space.
POOL_FREE_WARNING = 0.10

ACTION_BTRFS_REMOVE = "btrfs_remove"

# Device identifier prefix of Btrfs pools (disks use their serial number).
POOL_PREFIX = "pool_"
# Key of the whole host in the health summaries.
HOST_SCOPE = "host"
