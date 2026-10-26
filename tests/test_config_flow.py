"""Tests for the config flow, the installer and the legacy import."""

from http import HTTPStatus
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.proxmox_host_monitor.const import (
    CONF_HOST_KEY,
    CONF_REBOOT_BUTTON,
    CONF_WEBHOOK_ID,
    DOMAIN,
    LEGACY_DOMAIN,
)
from custom_components.proxmox_host_monitor.installer import (
    ExistingItem,
    InstallError,
    build_install_script,
)

BUTTON = "button.pve_reboot"
SSH_INPUT = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 22,
    CONF_USERNAME: "root",
    CONF_PASSWORD: "secret",
}
INSTALLER = "custom_components.proxmox_host_monitor.config_flow"


@pytest.fixture(autouse=True)
async def setup_http(hass: HomeAssistant) -> None:
    """Provide the HTTP server and a local URL."""
    assert await async_setup_component(hass, "http", {})
    await hass.config.async_update(internal_url="http://192.0.2.1:8123")
    hass.states.async_set(BUTTON, "unknown")


async def _start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REBOOT_BUTTON: BUTTON}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "install"
    return result


async def test_manual_install(hass: HomeAssistant, hass_client_no_auth) -> None:
    """The manual path shows a command whose script can be downloaded."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    assert result["step_id"] == "manual"
    command = result["description_placeholders"]["command"]
    assert command.startswith("curl -fsSL http://192.0.2.1:8123/api/proxmox_host_monitor/install/")

    # The installer is served while the flow is still open.
    client = await hass_client_no_auth()
    path = command.split("8123", 1)[1].split(" ", 1)[0]
    resp = await client.get(path)
    assert resp.status == HTTPStatus.OK
    assert "/api/webhook/" in await resp.text()
    assert (await client.get("/api/proxmox_host_monitor/install/unknown")).status == HTTPStatus.NOT_FOUND

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REBOOT_BUTTON] == BUTTON
    assert path.endswith(result["data"][CONF_WEBHOOK_ID])
    assert CONF_HOST_KEY not in result["data"]


async def test_ssh_install_asks_before_replacing(hass: HomeAssistant) -> None:
    """Existing installations are listed and only replaced after confirming."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ssh"}
    )
    assert result["step_id"] == "ssh"

    existing = [ExistingItem("legacy", "Proxmox Reboot Update", "/usr/local/sbin/x")]
    with (
        patch(f"{INSTALLER}.async_check_via_ssh", return_value=(existing, "SHA256:abc")),
        patch(f"{INSTALLER}.async_install_via_ssh", return_value="SHA256:abc") as install,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], SSH_INPUT
        )
        assert result["step_id"] == "confirm_replace"
        assert "**remove**: Proxmox Reboot Update" in result["description_placeholders"]["items"]
        install.assert_not_called()

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST_KEY] == "SHA256:abc"
    target = install.call_args.args[2]
    assert target.host_key == "SHA256:abc"
    # The password is dropped after the installation.
    assert target.password == ""


async def test_ssh_install_without_existing(hass: HomeAssistant) -> None:
    """Without existing installations the scripts are installed directly."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ssh"}
    )
    with (
        patch(f"{INSTALLER}.async_check_via_ssh", return_value=([], "SHA256:abc")),
        patch(f"{INSTALLER}.async_install_via_ssh", return_value="SHA256:abc"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], SSH_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "reason", ["cannot_connect", "invalid_auth", "host_key_mismatch", "install_failed"]
)
async def test_ssh_errors(hass: HomeAssistant, reason: str) -> None:
    """SSH errors are shown on the form without storing the password."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ssh"}
    )
    with patch(f"{INSTALLER}.async_check_via_ssh", side_effect=InstallError(reason, "boom")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], SSH_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}
    assert result["description_placeholders"]["details"] == "boom"


async def test_import_legacy(hass: HomeAssistant) -> None:
    """Proxmox Reboot Update is taken over including its update entity."""
    legacy = MockConfigEntry(
        domain=LEGACY_DOMAIN,
        version=2,
        unique_id=LEGACY_DOMAIN,
        data={CONF_REBOOT_BUTTON: "button.old", CONF_WEBHOOK_ID: "legacy_webhook"},
        options={CONF_REBOOT_BUTTON: BUTTON},
    )
    legacy.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "update",
        LEGACY_DOMAIN,
        "proxmox_reboot_required_update",
        config_entry=legacy,
        suggested_object_id="proxmox_neustart_erforderlich",
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "import_legacy"
    assert result["description_placeholders"] == {"reboot_button": BUTTON}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "install"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_REBOOT_BUTTON: BUTTON,
        CONF_WEBHOOK_ID: "legacy_webhook",
    }
    assert not hass.config_entries.async_entries(LEGACY_DOMAIN)

    entity = ent_reg.async_get("update.proxmox_neustart_erforderlich")
    assert entity.platform == DOMAIN
    assert entity.config_entry_id == result["result"].entry_id
    assert hass.states.get("update.proxmox_neustart_erforderlich") is not None


async def test_single_instance(hass: HomeAssistant) -> None:
    """Only one instance can be configured."""
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT


def test_install_script() -> None:
    """The generated installer embeds both reporters and the webhook URL."""
    script = build_install_script("http://ha:8123/api/webhook/abc", "2.0.0")
    assert "WEBHOOK_URL=http://ha:8123/api/webhook/abc" in script
    assert 'VERSION="2.0.0"' in script
    for marker in ("@VERSION@", "@WEBHOOK_URL@", "@REBOOT_SCRIPT@", "@DISK_SCRIPT@", "@GUEST_SCRIPT@"):
        assert marker not in script
    assert "smartctl" in script
    assert "uname -r" in script
    assert "The timer retries automatically" in script
    assert script.count("<<'PHM_EOF'") == script.count("\nPHM_EOF\n")


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id=DOMAIN,
        data={
            CONF_REBOOT_BUTTON: BUTTON,
            CONF_WEBHOOK_ID: "test_webhook",
            CONF_HOST_KEY: "SHA256:abc",
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_options_change_button(hass: HomeAssistant) -> None:
    """The reboot button can be changed in the options."""
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    hass.states.async_set("button.new", "unknown")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_REBOOT_BUTTON: "button.new"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_REBOOT_BUTTON: "button.new"}


async def test_options_reinstall_checks_host_key(hass: HomeAssistant) -> None:
    """A reinstall over SSH uses the stored host key."""
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ssh"}
    )
    own = [ExistingItem("own", "Proxmox Host Monitor 2.0.0 (update to 2.0.0)", "/x")]
    with (
        patch(f"{INSTALLER}.async_check_via_ssh", return_value=(own, "SHA256:abc")) as check,
        patch(f"{INSTALLER}.async_install_via_ssh", return_value="SHA256:abc"),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], SSH_INPUT
        )
        assert check.call_args.args[2].host_key == "SHA256:abc"
        assert result["step_id"] == "confirm_replace"
        assert "**update**" in result["description_placeholders"]["items"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_HOST_KEY] == "SHA256:abc"
