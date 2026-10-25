"""Config flow for Proxmox Host Monitor."""

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.webhook import async_generate_id as webhook_generate_id
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.network import NoURLAvailableError

from .button_monitor import get_reboot_button
from .const import (
    CONF_HOST_KEY,
    CONF_REBOOT_BUTTON,
    CONF_WEBHOOK_ID,
    DOMAIN,
    LEGACY_DOMAIN,
)
from .installer import (
    ExistingItem,
    InstallError,
    SSHTarget,
    async_allow_pending_install,
    async_check_via_ssh,
    async_install_via_ssh,
    install_command,
)

TITLE = "Proxmox Host Monitor"

ACTIONS = {"own": "update", "legacy": "remove", "foreign": "disable", "info": "keep"}


def _button_schema(default: str | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_REBOOT_BUTTON,
                **({"default": default} if default else {}),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="button")),
        }
    )


class SSHInstallMixin:
    """Install the host scripts over SSH, asking before replacing anything."""

    hass: Any
    _webhook_id: str | None
    _host_key: str | None
    _target: SSHTarget | None
    _existing: list[ExistingItem]

    def _ssh_schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        defaults = user_input or {}
        host = defaults.get(CONF_HOST) or next(
            (
                entry.data.get(CONF_HOST)
                for entry in self.hass.config_entries.async_entries("proxmoxve")
            ),
            None,
        )
        return vol.Schema(
            {
                vol.Required(CONF_HOST, **({"default": host} if host else {})): str,
                vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, 22)): vol.Coerce(int),
                vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "root")): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )

    async def _async_ssh_step(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Connect, look for existing installations and install if none."""
        errors: dict[str, str] = {}
        placeholders = {"details": ""}

        if user_input is not None:
            self._target = SSHTarget(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                host_key=self._host_key,
            )
            try:
                self._existing, fingerprint = await async_check_via_ssh(
                    self.hass, self._webhook_id, self._target
                )
                self._target.host_key = fingerprint
                if self._existing:
                    return await self.async_step_confirm_replace()
                return await self._async_install()
            except InstallError as err:
                errors["base"] = err.reason
                placeholders["details"] = err.details
            except NoURLAvailableError:
                errors["base"] = "no_url"

        return self.async_show_form(
            step_id="ssh",
            data_schema=self._ssh_schema(
                {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_confirm_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before replacing what is already installed on the host."""
        if user_input is not None:
            return await self._async_install()

        items = "\n".join(
            f"- **{ACTIONS.get(item.kind, 'keep')}**: {item.description} (`{item.path}`)"
            for item in self._existing
        )
        return self.async_show_form(
            step_id="confirm_replace",
            data_schema=vol.Schema({}),
            description_placeholders={"items": items},
        )

    async def _async_install(self) -> ConfigFlowResult:
        assert self._target is not None
        try:
            self._host_key = await async_install_via_ssh(
                self.hass, self._webhook_id, self._target
            )
        except InstallError as err:
            return self.async_show_form(
                step_id="ssh",
                data_schema=self._ssh_schema(
                    {
                        CONF_HOST: self._target.host,
                        CONF_PORT: self._target.port,
                        CONF_USERNAME: self._target.username,
                    }
                ),
                errors={"base": err.reason},
                description_placeholders={"details": err.details},
            )
        finally:
            # Never keep the password longer than needed.
            self._target.password = ""
        return await self._async_installed()

    async def _async_installed(self) -> ConfigFlowResult:
        raise NotImplementedError


class ProxmoxHostMonitorConfigFlow(
    SSHInstallMixin,
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    """Handle a config flow for Proxmox Host Monitor."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reboot_button: str | None = None
        self._webhook_id: str | None = None
        self._host_key: str | None = None
        self._target: SSHTarget | None = None
        self._existing: list[ExistingItem] = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "ProxmoxHostMonitorOptionsFlow":
        """Return the options flow."""
        return ProxmoxHostMonitorOptionsFlow()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle initial setup."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if self.hass.config_entries.async_entries(LEGACY_DOMAIN):
            return await self.async_step_import_legacy()

        if user_input is not None:
            self._reboot_button = user_input[CONF_REBOOT_BUTTON]
            self._webhook_id = webhook_generate_id()
            return await self.async_step_install()

        return self.async_show_form(step_id="user", data_schema=_button_schema())

    async def async_step_import_legacy(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Take over the configuration of Proxmox Reboot Update."""
        legacy = self.hass.config_entries.async_entries(LEGACY_DOMAIN)[0]
        if user_input is not None:
            self._reboot_button = legacy.options.get(
                CONF_REBOOT_BUTTON, legacy.data[CONF_REBOOT_BUTTON]
            )
            # Keeping the webhook ID keeps existing host scripts working.
            self._webhook_id = legacy.data[CONF_WEBHOOK_ID]
            return await self.async_step_install()

        return self.async_show_form(
            step_id="import_legacy",
            data_schema=vol.Schema({}),
            description_placeholders={
                "reboot_button": legacy.options.get(
                    CONF_REBOOT_BUTTON, legacy.data.get(CONF_REBOOT_BUTTON, "")
                )
            },
        )

    async def async_step_install(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose how to install the scripts on the Proxmox host."""
        return self.async_show_menu(
            step_id="install",
            menu_options=["ssh", "manual"],
        )

    async def async_step_ssh(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Install over SSH with credentials that are used once."""
        return await self._async_ssh_step(user_input)

    async def async_step_manual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the command to paste into the Proxmox shell."""
        assert self._webhook_id is not None
        if user_input is not None:
            return await self._async_installed()

        try:
            command = install_command(self.hass, self._webhook_id)
        except NoURLAvailableError:
            return self.async_abort(reason="no_url")
        async_allow_pending_install(self.hass, self._webhook_id)
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({}),
            description_placeholders={"command": command},
        )

    async def _async_installed(self) -> ConfigFlowResult:
        data = {
            CONF_REBOOT_BUTTON: self._reboot_button,
            CONF_WEBHOOK_ID: self._webhook_id,
        }
        if self._host_key:
            data[CONF_HOST_KEY] = self._host_key
        return self.async_create_entry(title=TITLE, data=data)


class ProxmoxHostMonitorOptionsFlow(SSHInstallMixin, OptionsFlowWithReload):
    """Change the reboot button or reinstall the host scripts."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._webhook_id: str | None = None
        self._host_key: str | None = None
        self._target: SSHTarget | None = None
        self._existing: list[ExistingItem] = []

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show what can be changed."""
        self._webhook_id = self.config_entry.data[CONF_WEBHOOK_ID]
        self._host_key = self.config_entry.data.get(CONF_HOST_KEY)
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "ssh", "manual"],
        )

    async def async_step_settings(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Change the reboot button."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_REBOOT_BUTTON: user_input[CONF_REBOOT_BUTTON]},
            )

        return self.async_show_form(
            step_id="settings",
            data_schema=_button_schema(get_reboot_button(self.config_entry)),
        )

    async def async_step_ssh(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Reinstall or update the host scripts over SSH."""
        return await self._async_ssh_step(user_input)

    async def async_step_manual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the install command again."""
        if user_input is not None:
            return self.async_create_entry(data=dict(self.config_entry.options))

        try:
            command = install_command(self.hass, self._webhook_id)
        except NoURLAvailableError:
            return self.async_abort(reason="no_url")
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({}),
            description_placeholders={"command": command},
        )

    async def _async_installed(self) -> ConfigFlowResult:
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_HOST_KEY: self._host_key},
        )
        return self.async_create_entry(data=dict(self.config_entry.options))
