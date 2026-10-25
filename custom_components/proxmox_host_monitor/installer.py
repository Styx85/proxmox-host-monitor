"""Install the reporting scripts on the Proxmox host."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from ipaddress import ip_address
import logging
from pathlib import Path
import shlex

from aiohttp import web
import asyncssh

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import get_url
from homeassistant.loader import async_get_integration
from homeassistant.util.network import is_local

from .const import CONF_WEBHOOK_ID, DOMAIN, INSTALL_URL, PENDING_INSTALLS

_LOGGER = logging.getLogger(__name__)

HOST_DIR = Path(__file__).parent / "host"


def build_install_script(webhook_url: str, version: str) -> str:
    """Return a bash script that installs the reporters on a Proxmox host."""
    template = (HOST_DIR / "install.sh").read_text(encoding="utf-8")
    replacements = {
        "@VERSION@": version,
        "@WEBHOOK_URL@": shlex.quote(webhook_url),
        "@REBOOT_SCRIPT@": (HOST_DIR / "report-reboot-status").read_text(encoding="utf-8"),
        "@DISK_SCRIPT@": (HOST_DIR / "report-disk-health").read_text(encoding="utf-8"),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def webhook_url_for(base_url: str, webhook_id: str) -> str:
    """Return the webhook URL the host reports to."""
    return f"{base_url.rstrip('/')}/api/webhook/{webhook_id}"


def install_command(hass: HomeAssistant, webhook_id: str) -> str:
    """Return the one-line command that installs the reporters on the host."""
    base_url = get_url(hass, allow_cloud=False, prefer_external=False)
    url = base_url.rstrip("/") + INSTALL_URL.format(webhook_id=webhook_id)
    return f"curl -fsSL {url} | bash"


@callback
def async_register_install_view(hass: HomeAssistant) -> None:
    """Register the installer download once."""
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("install_view"):
        data["install_view"] = True
        hass.http.register_view(InstallScriptView())


@callback
def async_allow_pending_install(hass: HomeAssistant, webhook_id: str) -> None:
    """Serve the installer for a webhook ID whose config flow is still open."""
    async_register_install_view(hass)
    hass.data[DOMAIN].setdefault(PENDING_INSTALLS, set()).add(webhook_id)


@dataclass(slots=True)
class ExistingItem:
    """Something the installer found on the host before changing anything."""

    kind: str
    description: str
    path: str


@dataclass(slots=True)
class SSHTarget:
    """Connection data for one installation; the password is never stored."""

    host: str
    port: int
    username: str
    password: str
    host_key: str | None


class InstallError(Exception):
    """Raised when the installation over SSH fails."""

    def __init__(self, reason: str, details: str = "") -> None:
        """Initialize with a translation key for the config flow."""
        super().__init__(reason)
        self.reason = reason
        self.details = details


async def _async_run_installer(
    hass: HomeAssistant,
    webhook_id: str,
    target: SSHTarget,
    option: str,
) -> tuple[str, str]:
    """Run the installer over SSH and return its output and the host key."""
    base_url = get_url(hass, allow_cloud=False, prefer_external=False)
    integration = await async_get_integration(hass, DOMAIN)
    script = await hass.async_add_executor_job(
        build_install_script,
        webhook_url_for(base_url, webhook_id),
        str(integration.version),
    )

    try:
        async with asyncssh.connect(
            target.host,
            port=target.port,
            username=target.username,
            password=target.password,
            known_hosts=None,
            client_keys=None,
            connect_timeout=15,
        ) as conn:
            fingerprint = conn.get_server_host_key().get_fingerprint()
            if target.host_key is not None and fingerprint != target.host_key:
                raise InstallError("host_key_mismatch", fingerprint)

            result = await conn.run(f"bash -s -- {option}", input=script, check=False)
    except asyncssh.PermissionDenied as err:
        raise InstallError("invalid_auth") from err
    except (OSError, asyncssh.Error) as err:
        _LOGGER.debug("SSH connection to %s failed", target.host, exc_info=True)
        raise InstallError("cannot_connect", str(err)) from err

    if result.exit_status != 0:
        details = str(result.stderr or result.stdout or "").strip()[-500:]
        _LOGGER.error("Installer on %s failed: %s", target.host, details)
        raise InstallError("install_failed", details)

    return str(result.stdout or ""), fingerprint


async def async_check_via_ssh(
    hass: HomeAssistant,
    webhook_id: str,
    target: SSHTarget,
) -> tuple[list[ExistingItem], str]:
    """List existing installations on the host without changing anything."""
    output, fingerprint = await _async_run_installer(
        hass, webhook_id, target, "--check"
    )
    items = [
        ExistingItem(*line.split("\t", 2))
        for line in output.splitlines()
        if line.count("\t") == 2
    ]
    return items, fingerprint


async def async_install_via_ssh(
    hass: HomeAssistant,
    webhook_id: str,
    target: SSHTarget,
) -> str:
    """Install the reporters over SSH and return the host key fingerprint.

    Existing installations listed by async_check_via_ssh are replaced. The host
    key is trusted on first use and must match on later installs.
    """
    _, fingerprint = await _async_run_installer(hass, webhook_id, target, "--yes")
    return fingerprint


class InstallScriptView(HomeAssistantView):
    """Serve the installer for the one-line install command."""

    url = INSTALL_URL
    name = f"api:{DOMAIN}:install"
    requires_auth = False

    async def get(self, request: web.Request, webhook_id: str) -> web.Response:
        """Return the install script for a configured entry."""
        hass: HomeAssistant = request.app["hass"]

        try:
            remote = ip_address(request.remote or "")
        except ValueError:
            return web.Response(status=HTTPStatus.FORBIDDEN)
        if not is_local(remote):
            return web.Response(status=HTTPStatus.FORBIDDEN)

        pending = hass.data.get(DOMAIN, {}).get(PENDING_INSTALLS, set())
        if webhook_id not in pending and not any(
            entry.data.get(CONF_WEBHOOK_ID) == webhook_id
            for entry in hass.config_entries.async_entries(DOMAIN)
        ):
            return web.Response(status=HTTPStatus.NOT_FOUND)

        # The host reaches Home Assistant through the URL it downloaded from.
        base_url = f"{request.scheme}://{request.host}"
        integration = await async_get_integration(hass, DOMAIN)
        script = await hass.async_add_executor_job(
            build_install_script,
            webhook_url_for(base_url, webhook_id),
            str(integration.version),
        )
        return web.Response(text=script, content_type="text/x-shellscript")
