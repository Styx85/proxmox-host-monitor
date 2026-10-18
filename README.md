# Proxmox Reboot Update

Home Assistant helper integration that exposes a pending Proxmox VE host reboot as a normal `update` entity.

## What it does

- Receives reboot status directly through its own Home Assistant webhook.
- Shows a pending host reboot in Home Assistant's normal Updates UI.
- Uses the selected Proxmox reboot button as the update action.
- Links the update entity to the selected Proxmox device.
- No `input_boolean` and no Home Assistant webhook automation are required.

## Setup

Install through HACS, restart Home Assistant, then add **Proxmox Reboot Update** under **Settings → Devices & services**.

Select the real Proxmox reboot button. The setup flow displays a generated webhook URL.

You can retrieve the webhook URL later by editing the existing **Proxmox Reboot Update** helper.

## Proxmox status script

Create the status script on the Proxmox host:

```bash
nano /usr/local/sbin/report-reboot-status-to-ha
```

Use the following content and replace `GENERATED_ID` with the webhook URL shown by Home Assistant:

```bash
#!/bin/bash

HA_WEBHOOK_URL="http://homeassistant.example:8123/api/webhook/GENERATED_ID"

if [ -f /var/run/reboot-required ]; then
    REQUIRED=true
else
    REQUIRED=false
fi

curl \
    --fail \
    --silent \
    --show-error \
    --max-time 10 \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{\"required\":${REQUIRED}}" \
    "${HA_WEBHOOK_URL}"
```

Make the script executable:

```bash
chmod 750 /usr/local/sbin/report-reboot-status-to-ha
```

You can trigger an immediate status report with:

```bash
/usr/local/sbin/report-reboot-status-to-ha
```

## systemd service

Create the systemd service:

```bash
nano /etc/systemd/system/proxmox-reboot-status.service
```

Use:

```ini
[Unit]
Description=Report Proxmox reboot status to Home Assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/report-reboot-status-to-ha
```

## systemd timer

Create the timer:

```bash
nano /etc/systemd/system/proxmox-reboot-status.timer
```

Use:

```ini
[Unit]
Description=Synchronize Proxmox reboot status with Home Assistant

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

Reload systemd and enable the timer:

```bash
systemctl daemon-reload
systemctl enable --now proxmox-reboot-status.timer
```

Verify that the timer is active:

```bash
systemctl status proxmox-reboot-status.timer
systemctl list-timers proxmox-reboot-status.timer
```

The Proxmox host will now report its reboot status to Home Assistant every five minutes and shortly after boot.

## Behavior

- The last known status is restored after a Home Assistant restart.
- If the host has not reported for 15 minutes, the update entity becomes unavailable.
- Installing the update presses the reboot button and shows the update as in progress until the host reports that no reboot is required anymore (at most 30 minutes).
- The webhook only accepts requests from the local network.

## Testing

You can test the complete status path manually.

Simulate a required reboot:

```bash
touch /var/run/reboot-required
/usr/local/sbin/report-reboot-status-to-ha
```

Home Assistant should now show the Proxmox reboot as pending in the normal Updates UI.

Reset the state:

```bash
rm /var/run/reboot-required
/usr/local/sbin/report-reboot-status-to-ha
```

The pending update should disappear again.

## Upgrading from versions before 1.1.1

Versions before 1.1.1 did not expose the webhook configuration correctly through the Home Assistant helper UI.

After upgrading to 1.1.1 or later:

1. Open **Settings → Devices & services → Helpers**.
2. Open the existing **Proxmox Reboot Update** helper and choose **Edit**.
3. Copy the displayed webhook URL.
4. Replace the old webhook URL in `/usr/local/sbin/report-reboot-status-to-ha`.
5. Run the status script once and verify that the update entity becomes available.

Do **not** try to add Proxmox Reboot Update a second time. Only one instance is supported. On older versions this could result in the message `single_instance_allowed`.

If upgrading from 1.0.x, the previous `input_boolean` and Home Assistant webhook automation can be removed after the new integrated webhook has been tested successfully.

## Translations

English (`en.json`) is the reference translation. German is maintained with the integration, and additional community translations are welcome.

To add a translation:

1. Copy `custom_components/proxmox_reboot_update/translations/en.json`.
2. Rename the copy using the appropriate Home Assistant language code, for example `fr.json`, `nl.json`, or `es.json`.
3. Translate text values only. Do not change JSON keys.
4. Preserve placeholders such as `{webhook_url}` exactly.
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

This project was created with the help of ChatGPT by OpenAI, including assistance with architecture, implementation, documentation, and debugging.

Final testing, review, and publication were performed by the repository maintainer.

## License

MIT
