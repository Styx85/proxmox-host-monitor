<p align="center">
  <img src="custom_components/proxmox_host_monitor/brand/icon@2x.png" alt="Proxmox Host Monitor icon" width="160">
</p>

# Proxmox Host Monitor

Home Assistant integration that watches a Proxmox VE host from the inside: pending reboots, disk health and Btrfs storage, including removing a failing disk from Home Assistant.

It complements the core [Proxmox VE integration](https://www.home-assistant.io/integrations/proxmoxve/), which reads the Proxmox API. Proxmox Host Monitor covers what the API does not show, using two small scripts on the host that report to Home Assistant through a local webhook.

Proxmox Host Monitor replaces **Proxmox Reboot Update**. An existing installation is imported automatically, see [Migrating from Proxmox Reboot Update](#migrating-from-proxmox-reboot-update).

## Features

**Reboot management**

- Shows a pending host reboot in Home Assistant's normal Updates UI.
- Detects a newer installed kernel than the running one, and `/var/run/reboot-required`.
- Installing the update presses the Proxmox VE reboot button and shows progress until the host reports back (at most 30 minutes).
- A repair issue lets you select a new reboot button if the configured one disappears.

**Disk health**

- One device per disk (by serial number) with a problem sensor, health and the relevant SMART values: temperature, pending, reallocated and uncorrectable sectors, CRC errors, command timeouts, power-on time, NVMe wear and media errors.
- Btrfs device error counters per disk.
- Summary entities `Disk problem` and `Disk health` on the Proxmox node.
- New disks appear automatically. Removed disks become unavailable and can be deleted from the device page.
- smartd warnings are reported immediately, everything else every 15 minutes. Disks in standby are not woken up.

The evaluation is deliberately strict:

| Health | Triggered by |
|---|---|
| critical | SMART failed, failure predicted, pending or offline uncorrectable sectors, reported uncorrectable errors, Btrfs device errors, NVMe critical warning, media errors or spare below threshold, temperature ≥ 55 °C |
| warning | reallocated sectors, CRC errors, command timeouts, errors in the SMART error log, failed self-test, attribute below threshold in the past, NVMe wear ≥ 80 %, temperature ≥ 50 °C |

**Btrfs storage and failing disks**

- One device per Btrfs filesystem with free space, usage and whether a balance or a disk removal is running.
- Every report checks for each disk whether the data of its filesystem would still fit on the other disks (respecting the RAID profiles and a safety margin). The result is shown as *Removable* on the disk.
- A critical disk raises a repair issue under **Settings → System → Repairs**:
  - **Removable**: confirm the repair to remove the disk from the filesystem. The host moves its data to the other disks with `btrfs device remove` and reports the progress. The host checks again before it starts and refuses while a balance runs or if the space no longer suffices.
  - **Not enough space**: the issue tells you how much space is missing and the minimum size of a new disk to buy.
  - **Balance running**: removal becomes possible once the balance has finished.
- A filesystem with less than 10 % free space raises a warning.
- Home Assistant never connects to the host for this. The removal request is handed to the host with the response to its next report (within 5 minutes).

**Notifications**

Disk failures, low space and removals fire the event `proxmox_host_monitor_alert`, so you can send push notifications, see [Notifications](#notifications).

**Setup from Home Assistant**

- The host scripts are installed from the setup dialog, either automatically over SSH or with a one-line command.
- Existing installations and similar services are detected first, and nothing is replaced without your confirmation.

## Installation

1. In HACS, add `https://github.com/Styx85/proxmox-host-monitor` as a custom repository of type **Integration** and install **Proxmox Host Monitor**.
2. Restart Home Assistant.
3. Add **Proxmox Host Monitor** under **Settings → Devices & services**.
4. Select the Proxmox VE node reboot button.
5. Choose how to install the host scripts:
   - **Install automatically over SSH**: enter host, user (`root`) and password. The password is used only for this connection and is **not stored**. The SSH host key is remembered, so later updates only connect to the same host.
   - **Show a command**: run the displayed `curl … | bash` command as root in the Proxmox shell, for example **Datacenter → node → Shell** in the Proxmox web interface.

Both ways can be repeated later from the integration's options, for example after an update of the integration.

### Proxmox VE permissions

The Proxmox VE integration only creates node reboot buttons if its Proxmox user or API token has the `Sys.PowerMgmt` privilege on the node. Example for a dedicated user `ha-monitor@pve`:

```bash
pveum role add HAMonitor -privs "Sys.Audit,VM.Audit,Datastore.Audit,Sys.PowerMgmt,VM.PowerMgmt,VM.Snapshot"
pveum acl modify / -user ha-monitor@pve -role HAMonitor
```

An ACL on a deeper path such as `/nodes/<node>` replaces the privileges inherited from `/` for the same user. If you add a node-specific role there (for example `Sys.Modify` for the Proxmox VE package update entity), assign `HAMonitor` on that path as well.

## Notifications

The integration fires `proxmox_host_monitor_alert` once per situation:

| `type` | Data |
|---|---|
| `disk_failing` | `disk`, `model`, `serial`, `problems`, `pool`, `removable`, `balance_running`, `missing_gb`, `recommended_gb` |
| `pool_space_low` | `pool`, `free_gb`, `percentage` |
| `removal_requested`, `removal_started`, `removal_finished` | `disk`, `serial`, `pool` |
| `removal_failed` | `disk`, `serial`, `reason` |

Example automation for a push notification:

```yaml
triggers:
  - trigger: event
    event_type: proxmox_host_monitor_alert
actions:
  - action: notify.mobile_app_my_phone
    data:
      title: "Proxmox: {{ trigger.event.data.type | replace('_', ' ') }}"
      message: >
        {% set d = trigger.event.data %}
        {% if d.type == 'disk_failing' %}
          {{ d.disk }} ({{ d.model }}) is failing: {{ d.problems | join(', ') }}.
          {% if d.removable %}It can be removed from {{ d.pool }} under Settings → Repairs.
          {% elif d.missing_gb %}Buy a new disk of at least {{ d.recommended_gb }} GB.{% endif %}
        {% elif d.type == 'pool_space_low' %}
          Only {{ d.free_gb }} GB ({{ d.percentage }} %) free on {{ d.pool }}.
        {% else %}
          {{ d.disk }} {{ d.pool or '' }} {{ d.reason or '' }}
        {% endif %}
      data:
        priority: high
        ttl: 0
```

## What is installed on the host

| Path | Purpose |
|---|---|
| `/usr/local/sbin/proxmox-host-monitor-reboot` | reports the reboot status |
| `/usr/local/sbin/proxmox-host-monitor-disks` | reports SMART and Btrfs disk health and filesystems, runs confirmed disk removals (`--dry-run` prints the report) |
| `/etc/systemd/system/proxmox-host-monitor-{reboot,disks}.{service,timer}` | every 5 and 15 minutes |
| `/etc/smartmontools/run.d/60proxmox-host-monitor` | immediate report on smartd warnings |
| `/etc/proxmox-host-monitor/` | webhook URL and installed version |

`smartmontools` is installed if missing. Disk removals run as transient units `proxmox-host-monitor-remove-<serial>`.

### Existing installations

Before changing anything, the installer looks for:

| Found | Action after confirming |
|---|---|
| an earlier Proxmox Host Monitor | **update** |
| the Proxmox Reboot Update status script and timer | **remove** |
| other systemd services whose script posts to a Home Assistant webhook or calls `smartctl`, and other smartd hooks | **disable** (timer stopped and disabled, smartd hook made non-executable, files kept) |
| cron jobs posting to a Home Assistant webhook | **keep** (only listed) |

Over SSH, Home Assistant shows the list and waits for confirmation. The one-line command asks `Overwrite/update? [y/N]` in the terminal. `--check` only lists, `--yes` skips the question.

### Uninstall

```bash
systemctl disable --now proxmox-host-monitor-reboot.timer proxmox-host-monitor-disks.timer
rm -rf /usr/local/sbin/proxmox-host-monitor-* /etc/systemd/system/proxmox-host-monitor-* \
       /etc/smartmontools/run.d/60proxmox-host-monitor /etc/proxmox-host-monitor
systemctl daemon-reload
```

## Migrating from Proxmox Reboot Update

1. Install Proxmox Host Monitor and add it as described above. Keep Proxmox Reboot Update installed for now.
2. The setup detects Proxmox Reboot Update and takes over its reboot button, its webhook and its update entity, including entity ID and history. Proxmox Reboot Update's configuration is removed afterwards.
3. Install the host scripts. The installer replaces the old `report-reboot-status-to-ha` script and timer after you confirm.
4. Uninstall Proxmox Reboot Update in HACS.

## Webhook payload

Custom scripts can report to the same webhook (`POST`, JSON, local network only). All keys are optional, but `required` or `disks` is required:

```json
{
  "required": false,
  "disks": [
    {
      "name": "sda",
      "serial": "WW6099YV",
      "model": "ST4000VX016",
      "status": "ok",
      "temperature": 37,
      "pending_sectors": 0,
      "problems": [{"code": "pending_sectors", "severity": "critical", "value": 3}]
    }
  ]
}
```

`status` is `ok`, `warning` or `critical`. `pools` describes Btrfs filesystems, see `report-disk-health` for the format. The response to a disk report can contain `commands` for the host. Each disk report replaces the previous one; disks missing from it become unavailable. Without a report for 15 minutes (reboot) or 45 minutes (disks) the entities become unavailable.

## Translations

English (`en.json`) is the reference translation. German is maintained with the integration, and additional community translations are welcome.

To add a translation:

1. Copy `custom_components/proxmox_host_monitor/translations/en.json`.
2. Rename the copy using the appropriate Home Assistant language code, for example `fr.json`, `nl.json`, or `es.json`.
3. Translate text values only. Do not change JSON keys.
4. Preserve placeholders such as `{command}` exactly.
5. Run:

   ```bash
   python3 scripts/check_translations.py
   ```

6. Submit a pull request.

Pull requests are automatically checked for:

- valid JSON
- missing translation keys
- additional or invalid translation keys
- empty values
- placeholder mismatches

Home Assistant's central translation platform is not available for custom integrations, so community translations are maintained directly in this repository.

## Development

This project was created with the help of ChatGPT by OpenAI and Claude by Anthropic, including assistance with architecture, implementation, documentation, and debugging.

Final testing, review, and publication were performed by the repository maintainer.

## License

MIT
