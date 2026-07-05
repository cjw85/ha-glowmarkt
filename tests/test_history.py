"""Tests for Glow half-hour history import into Home Assistant statistics."""

from __future__ import annotations

import datetime as dt
from functools import partial
from unittest.mock import patch

from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.glowmarkt import const, history
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    get_metadata,
)

from tests.fakes import (
    FakeGlowClient,
    FakeGlowResource,
    FakeGlowVirtualEntity,
    fake_reading,
)


def test_fetch_half_hour_history_uses_exclusive_end_boundary() -> None:
    resource = FakeGlowResource("resource-1", "electricity.consumption", "import")
    timezone = dt.timezone(dt.timedelta(hours=1))
    start = dt.datetime(2026, 7, 2, 15, 0, tzinfo=timezone)
    end = dt.datetime(2026, 7, 5, 15, 0, tzinfo=timezone)
    calls = []

    def fake_get_resource_readings(resource, t_from, t_to, period, func="sum"):
        calls.append((t_from, t_to, period, func))
        return []

    with patch.object(
        history, "get_resource_readings", side_effect=fake_get_resource_readings
    ):
        history._fetch_half_hour_history(resource, start, end, chunk_days=10)

    assert len(calls) == 1
    assert calls[0][0] == start
    assert calls[0][1] == dt.datetime(2026, 7, 5, 14, 59, 59, tzinfo=timezone)


def test_fetch_half_hour_history_chunks_without_overlapping_boundaries() -> None:
    resource = FakeGlowResource("resource-1", "electricity.consumption", "import")
    timezone = dt.timezone.utc
    start = dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone)
    end = dt.datetime(2026, 7, 21, 0, 0, tzinfo=timezone)
    calls = []

    def fake_get_resource_readings(resource, t_from, t_to, period, func="sum"):
        calls.append((t_from, t_to, period, func))
        return []

    with patch.object(
        history, "get_resource_readings", side_effect=fake_get_resource_readings
    ):
        history._fetch_half_hour_history(resource, start, end)

    assert len(calls) == 2
    assert calls[0][0] == dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone)
    assert calls[0][1] == dt.datetime(2026, 7, 10, 23, 59, 59, tzinfo=timezone)
    assert calls[1][0] == dt.datetime(2026, 7, 11, 0, 0, tzinfo=timezone)
    assert calls[1][1] == dt.datetime(2026, 7, 20, 23, 59, 59, tzinfo=timezone)


async def test_async_import_history_statistics_writes_to_recorder(
    async_setup_recorder_instance,
    hass,
) -> None:
    timezone = dt.timezone(dt.timedelta(hours=1))
    end = dt.datetime(2026, 7, 2, 0, 0, tzinfo=timezone)

    client = FakeGlowClient(
        [
            FakeGlowVirtualEntity(
                "ve-1",
                "Site 1",
                resources=[
                    FakeGlowResource(
                        "dcc-import",
                        const.ELEC_CONSUMPTION_CLASSIFIER,
                        "electricity consumption DCC SM profile reads",
                    ),
                    FakeGlowResource(
                        "dcc-export",
                        const.ELEC_EXPORT_CLASSIFIER,
                        "electricity export DCC SM profile reads",
                    ),
                    FakeGlowResource(
                        "dcc-gas",
                        const.GAS_CONSUMPTION_CLASSIFIER,
                        "gas consumption DCC SM profile reads",
                    ),
                ],
            )
        ]
    )

    history_rows = {
        "dcc-import": [
            fake_reading(dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone), 0.5),
            fake_reading(dt.datetime(2026, 7, 1, 0, 30, tzinfo=timezone), 0.75),
        ],
        "dcc-export": [
            fake_reading(dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone), 0.2),
            fake_reading(dt.datetime(2026, 7, 1, 0, 30, tzinfo=timezone), 0.3),
        ],
        "dcc-gas": [
            fake_reading(dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone), 1.2),
            fake_reading(dt.datetime(2026, 7, 1, 0, 30, tzinfo=timezone), 0.8),
        ],
    }

    def fake_fetch(resource, start, end, *, chunk_days=10):
        del start, end, chunk_days
        return history_rows[resource.id]

    await async_setup_recorder_instance(hass)

    with (
        patch.object(history, "_fetch_half_hour_history", side_effect=fake_fetch),
        patch.object(history, "_history_window_end", return_value=end),
    ):
        assert await history.async_import_history_statistics(hass, client) is True

    await async_wait_recording_done(hass)

    statistic_ids = {
        "glowmarkt:site_1_ve_1_electricity_import",
        "glowmarkt:site_1_ve_1_electricity_export",
        "glowmarkt:site_1_ve_1_gas_usage",
    }
    metadata = await hass.async_add_executor_job(
        partial(
            get_metadata,
            hass,
            statistic_ids=statistic_ids,
        )
    )

    assert statistic_ids <= set(metadata)

    import_stats = await hass.async_add_executor_job(
        partial(
            get_last_statistics,
            hass,
            1,
            "glowmarkt:site_1_ve_1_electricity_import",
            True,
            {"sum"},
        )
    )
    export_stats = await hass.async_add_executor_job(
        partial(
            get_last_statistics,
            hass,
            1,
            "glowmarkt:site_1_ve_1_electricity_export",
            True,
            {"sum"},
        )
    )
    gas_stats = await hass.async_add_executor_job(
        partial(
            get_last_statistics,
            hass,
            1,
            "glowmarkt:site_1_ve_1_gas_usage",
            True,
            {"sum"},
        )
    )

    assert import_stats["glowmarkt:site_1_ve_1_electricity_import"][0]["sum"] == 1.25
    assert export_stats["glowmarkt:site_1_ve_1_electricity_export"][0]["sum"] == 0.5
    assert gas_stats["glowmarkt:site_1_ve_1_gas_usage"][0]["sum"] == 2.0
