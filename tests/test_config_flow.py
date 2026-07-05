"""Tests for the Glowmarkt config and options flows."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import requests

from custom_components.glowmarkt.const import (
    CONF_DAILY_INTERVAL,
    CONF_TARIFF_INTERVAL,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER

from tests.fakes import http_error

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _start_user_flow(hass, *, client_side_effect) -> dict:
    """Start the user config flow with a patched Glow client."""
    with patch(
        "custom_components.glowmarkt.config_flow.GlowClient",
        side_effect=client_side_effect,
    ):
        return await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "user@example.com", "password": "secret"},
        )


async def test_user_flow_creates_entry(hass) -> None:
    user_input = {"username": "user@example.com", "password": "secret"}

    with patch(
        "custom_components.glowmarkt.config_flow.GlowClient",
        return_value=SimpleNamespace(url="https://example.test/api/v0-1/"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data=user_input,
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "Glowmarkt"
    assert result["data"] == user_input
    assert result["options"] == {
        CONF_DAILY_INTERVAL: 15,
        CONF_TARIFF_INTERVAL: 60,
    }


@pytest.mark.parametrize(
    ("client_side_effect", "expected_errors"),
    [
        (http_error(401, "Unauthorized"), {"base": "invalid_auth"}),
        (requests.Timeout("boom"), {"base": "timeout_connect"}),
    ],
)
async def test_user_flow_maps_connection_errors_to_form_errors(
    hass,
    client_side_effect,
    expected_errors,
) -> None:
    result = await _start_user_flow(hass, client_side_effect=client_side_effect)

    assert result["type"] == "form"
    assert result["errors"] == expected_errors


async def test_options_flow_rejects_low_polling_intervals(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DAILY_INTERVAL: 15,
            CONF_TARIFF_INTERVAL: 60,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_DAILY_INTERVAL: 4,
            CONF_TARIFF_INTERVAL: 5,
        },
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "interval_too_low"}
