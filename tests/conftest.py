"""Fixtures for Proxmox Host Monitor tests."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration."""
    yield
