"""Tests for integration setup behaviour."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.glowmarkt as integration
from custom_components.glowmarkt.const import DOMAIN
from homeassistant.exceptions import ConfigEntryAuthFailed

from tests.fakes import http_error


async def test_async_setup_entry_raises_auth_failed_for_stored_credentials(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "user@example.com", "password": "secret"},
    )

    with patch.object(
        integration,
        "GlowClient",
        side_effect=http_error(401, "Unauthorized"),
    ):
        with pytest.raises(ConfigEntryAuthFailed):
            await integration.async_setup_entry(hass, entry)
