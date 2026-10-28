"""Shared test fixtures."""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_frontend():
    """The panel needs the real frontend, which tests do not load."""
    with patch(
        "custom_components.home_heat_control._async_register_frontend",
        return_value=None,
    ):
        yield


@pytest.fixture(autouse=True)
def fake_frontend(hass):
    """Mark the frontend and http dependencies as already loaded."""
    hass.config.components.add("frontend")
    hass.config.components.add("http")
    yield
