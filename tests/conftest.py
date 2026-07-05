"""Shared pytest fixtures for the Glowmarkt test suite."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


async def _history_loop_stub(hass, glow_client) -> None:
    """Prevent the background history task from running during setup tests."""
    del hass, glow_client


async def _immediate_first_refresh(coordinator, delay: int = 5) -> None:
    """Run the first coordinator refresh immediately in tests."""
    del delay
    await coordinator.async_request_refresh()


def _fake_daily_readings(resource, t_from, t_to, period, func="sum", nulls=False):
    """Return a single daily reading row for the fake resource."""
    del t_to, period, func, nulls
    if resource.daily_total is None:
        return []
    return [
        [
            t_from,
            SimpleNamespace(
                value=resource.daily_total,
                unit=lambda: resource.base_unit,
            ),
        ]
    ]


@pytest.fixture
def sensor_runtime_patches():
    """Patch sensor setup to use deterministic fake Glow responses."""
    with (
        patch(
            "custom_components.glowmarkt.async_history_statistics_loop",
            new=_history_loop_stub,
        ),
        patch(
            "custom_components.glowmarkt.sensor._delayed_first_refresh",
            new=_immediate_first_refresh,
        ),
        patch(
            "custom_components.glowmarkt.sensor._get_resource_readings",
            side_effect=_fake_daily_readings,
        ),
    ):
        yield
