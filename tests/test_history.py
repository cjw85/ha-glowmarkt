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


def test_aggregate_half_hour_readings_to_hourly_statistics_applies_starting_sum() -> None:
    timezone = dt.timezone.utc
    readings = [
        fake_reading(dt.datetime(2026, 7, 1, 1, 0, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 1, 1, 30, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 1, 2, 0, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 1, 2, 30, tzinfo=timezone), 0.5),
    ]

    statistics = history._aggregate_half_hour_readings_to_hourly_statistics(
        readings,
        starting_sum=1.0,
    )

    assert statistics == [
        {"start": dt.datetime(2026, 7, 1, 1, 0, tzinfo=timezone), "sum": 2.0},
        {"start": dt.datetime(2026, 7, 1, 2, 0, tzinfo=timezone), "sum": 3.0},
    ]


def test_aggregate_half_hour_readings_to_hourly_statistics_scales_values() -> None:
    timezone = dt.timezone.utc
    readings = [
        fake_reading(dt.datetime(2026, 7, 1, 1, 0, tzinfo=timezone), 10, "pence"),
        fake_reading(dt.datetime(2026, 7, 1, 1, 30, tzinfo=timezone), 20, "pence"),
    ]

    statistics = history._aggregate_half_hour_readings_to_hourly_statistics(
        readings,
        value_scale=0.01,
    )

    assert statistics == [
        {"start": dt.datetime(2026, 7, 1, 1, 0, tzinfo=timezone), "sum": 0.3},
    ]


def test_useful_history_end_truncates_to_last_complete_hour() -> None:
    timezone = dt.timezone(dt.timedelta(hours=1))
    requested_end = dt.datetime(2026, 7, 5, 17, 0, tzinfo=timezone)

    assert history._useful_history_end(
        requested_end,
        dt.datetime(2026, 7, 5, 15, 30, tzinfo=dt.timezone.utc),
    ) == dt.datetime(2026, 7, 5, 17, 0, tzinfo=timezone)
    assert history._useful_history_end(
        requested_end,
        dt.datetime(2026, 7, 5, 15, 0, tzinfo=dt.timezone.utc),
    ) == dt.datetime(2026, 7, 5, 16, 0, tzinfo=timezone)
    assert history._useful_history_end(requested_end, None) == requested_end


async def test_async_import_history_statistics_writes_to_recorder(
    async_setup_recorder_instance,
    hass,
) -> None:
    timezone = dt.timezone(dt.timedelta(hours=1))
    end = dt.datetime(2026, 7, 2, 0, 0, tzinfo=timezone)
    electricity_import = FakeGlowResource(
        "dcc-import",
        const.ELEC_CONSUMPTION_CLASSIFIER,
        "electricity consumption DCC SM profile reads",
    )
    electricity_export = FakeGlowResource(
        "dcc-export",
        const.ELEC_EXPORT_CLASSIFIER,
        "electricity export DCC SM profile reads",
    )
    electricity_cost = FakeGlowResource(
        "dcc-import-cost",
        const.ELEC_COST_CLASSIFIER,
        "electricity cost DCC SM profile reads",
        base_unit="pence",
    )
    gas_usage = FakeGlowResource(
        "dcc-gas",
        const.GAS_CONSUMPTION_CLASSIFIER,
        "gas consumption DCC SM profile reads",
    )
    gas_cost = FakeGlowResource(
        "dcc-gas-cost",
        const.GAS_COST_CLASSIFIER,
        "gas cost DCC SM profile reads",
        base_unit="pence",
    )

    client = FakeGlowClient(
        [
            FakeGlowVirtualEntity(
                "ve-1",
                "Site 1",
                resources=[
                    electricity_import,
                    electricity_export,
                    electricity_cost,
                    gas_usage,
                    gas_cost,
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
        "dcc-import-cost": [
            fake_reading(dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone), 12, "pence"),
            fake_reading(dt.datetime(2026, 7, 1, 0, 30, tzinfo=timezone), 18, "pence"),
        ],
        "dcc-gas": [
            fake_reading(dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone), 1.2),
            fake_reading(dt.datetime(2026, 7, 1, 0, 30, tzinfo=timezone), 0.8),
        ],
        "dcc-gas-cost": [
            fake_reading(dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone), 20, "pence"),
            fake_reading(dt.datetime(2026, 7, 1, 0, 30, tzinfo=timezone), 25, "pence"),
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
        "glowmarkt:site_1_ve_1_electricity_cost",
        "glowmarkt:site_1_ve_1_electricity_export",
        "glowmarkt:site_1_ve_1_gas_usage",
        "glowmarkt:site_1_ve_1_gas_cost",
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
    electricity_cost_stats = await hass.async_add_executor_job(
        partial(
            get_last_statistics,
            hass,
            1,
            "glowmarkt:site_1_ve_1_electricity_cost",
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
    gas_cost_stats = await hass.async_add_executor_job(
        partial(
            get_last_statistics,
            hass,
            1,
            "glowmarkt:site_1_ve_1_gas_cost",
            True,
            {"sum"},
        )
    )

    assert import_stats["glowmarkt:site_1_ve_1_electricity_import"][0]["sum"] == 1.25
    assert export_stats["glowmarkt:site_1_ve_1_electricity_export"][0]["sum"] == 0.5
    assert (
        electricity_cost_stats["glowmarkt:site_1_ve_1_electricity_cost"][0]["sum"]
        == 0.3
    )
    assert gas_stats["glowmarkt:site_1_ve_1_gas_usage"][0]["sum"] == 2.0
    assert gas_cost_stats["glowmarkt:site_1_ve_1_gas_cost"][0]["sum"] == 0.45
    assert metadata["glowmarkt:site_1_ve_1_electricity_cost"][1][
        "unit_of_measurement"
    ] == "GBP"
    assert metadata["glowmarkt:site_1_ve_1_gas_cost"][1]["unit_of_measurement"] == (
        "GBP"
    )
    assert electricity_import.catchup_calls == 1
    assert electricity_import.last_time_calls == 1
    assert electricity_export.catchup_calls == 0
    assert electricity_export.last_time_calls == 0
    assert electricity_cost.catchup_calls == 0
    assert electricity_cost.last_time_calls == 0
    assert gas_usage.catchup_calls == 1
    assert gas_usage.last_time_calls == 1
    assert gas_cost.catchup_calls == 0
    assert gas_cost.last_time_calls == 0


async def test_async_import_history_statistics_skips_catchup_for_non_dcc_resources(
    async_setup_recorder_instance,
    hass,
) -> None:
    timezone = dt.timezone.utc
    adhoc_resource = FakeGlowResource(
        "adhoc-import",
        const.ELEC_CONSUMPTION_CLASSIFIER,
        "electricity consumption adhoc",
    )
    client = FakeGlowClient(
        [
            FakeGlowVirtualEntity(
                "ve-1",
                "Site 1",
                resources=[adhoc_resource],
            )
        ]
    )

    await async_setup_recorder_instance(hass)

    with (
        patch.object(
            history,
            "_fetch_half_hour_history",
            return_value=[
                fake_reading(dt.datetime(2026, 7, 1, 0, 0, tzinfo=timezone), 0.5),
                fake_reading(dt.datetime(2026, 7, 1, 0, 30, tzinfo=timezone), 0.5),
            ],
        ),
        patch.object(
            history,
            "_history_window_end",
            return_value=dt.datetime(2026, 7, 2, 0, 0, tzinfo=timezone),
        ),
    ):
        assert await history.async_import_history_statistics(hass, client) is True

    assert adhoc_resource.catchup_calls == 0
    assert adhoc_resource.last_time_calls == 0


async def test_async_import_history_statistics_truncates_dcc_fetch_to_last_time(
    async_setup_recorder_instance,
    hass,
) -> None:
    timezone = dt.timezone.utc
    resource = FakeGlowResource(
        "dcc-import",
        const.ELEC_CONSUMPTION_CLASSIFIER,
        "electricity consumption DCC SM profile reads",
        last_time=dt.datetime(2026, 7, 2, 13, 0, tzinfo=timezone),
    )
    client = FakeGlowClient(
        [FakeGlowVirtualEntity("ve-1", "Site 1", resources=[resource])]
    )
    calls = []

    def fake_fetch(fetch_resource, start, end, *, chunk_days=10):
        del chunk_days
        calls.append((fetch_resource.id, start, end))
        return [
            fake_reading(dt.datetime(2026, 7, 2, 12, 0, tzinfo=timezone), 0.5),
            fake_reading(dt.datetime(2026, 7, 2, 12, 30, tzinfo=timezone), 0.5),
        ]

    await async_setup_recorder_instance(hass)

    with (
        patch.object(history, "_INITIAL_HISTORY_DAYS", 5),
        patch.object(history, "_fetch_half_hour_history", side_effect=fake_fetch),
        patch.object(
            history,
            "_history_window_end",
            return_value=dt.datetime(2026, 7, 5, 16, 0, tzinfo=timezone),
        ),
    ):
        assert await history.async_import_history_statistics(hass, client) is True

    assert resource.catchup_calls == 1
    assert resource.last_time_calls == 1
    assert calls == [
        (
            "dcc-import",
            dt.datetime(2026, 6, 30, 16, 0, tzinfo=timezone),
            dt.datetime(2026, 7, 2, 13, 0, tzinfo=timezone),
        )
    ]


async def test_async_import_history_statistics_uses_import_context_for_export(
    async_setup_recorder_instance,
    hass,
) -> None:
    timezone = dt.timezone.utc
    import_resource = FakeGlowResource(
        "dcc-import",
        const.ELEC_CONSUMPTION_CLASSIFIER,
        "electricity consumption DCC SM profile reads",
        last_time=dt.datetime(2026, 7, 2, 13, 0, tzinfo=timezone),
    )
    export_resource = FakeGlowResource(
        "export-resource",
        const.ELEC_EXPORT_CLASSIFIER,
        "electricity energy from active export power",
    )
    client = FakeGlowClient(
        [
            FakeGlowVirtualEntity(
                "ve-1",
                "Site 1",
                resources=[import_resource, export_resource],
            )
        ]
    )
    calls = []

    def fake_fetch(fetch_resource, start, end, *, chunk_days=10):
        del chunk_days
        calls.append((fetch_resource.id, start, end))
        return [
            fake_reading(dt.datetime(2026, 7, 2, 12, 0, tzinfo=timezone), 0.5),
            fake_reading(dt.datetime(2026, 7, 2, 12, 30, tzinfo=timezone), 0.5),
        ]

    await async_setup_recorder_instance(hass)

    with (
        patch.object(history, "_INITIAL_HISTORY_DAYS", 5),
        patch.object(history, "_fetch_half_hour_history", side_effect=fake_fetch),
        patch.object(
            history,
            "_history_window_end",
            return_value=dt.datetime(2026, 7, 5, 16, 0, tzinfo=timezone),
        ),
    ):
        assert await history.async_import_history_statistics(hass, client) is True

    assert import_resource.catchup_calls == 1
    assert import_resource.last_time_calls == 1
    assert export_resource.catchup_calls == 0
    assert export_resource.last_time_calls == 0
    assert calls == [
        (
            "dcc-import",
            dt.datetime(2026, 6, 30, 16, 0, tzinfo=timezone),
            dt.datetime(2026, 7, 2, 13, 0, tzinfo=timezone),
        ),
        (
            "export-resource",
            dt.datetime(2026, 6, 30, 16, 0, tzinfo=timezone),
            dt.datetime(2026, 7, 2, 13, 0, tzinfo=timezone),
        ),
    ]


async def test_async_import_history_statistics_preserves_cumulative_sum_on_refresh(
    async_setup_recorder_instance,
    hass,
) -> None:
    timezone = dt.timezone.utc
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
                    )
                ],
            )
        ]
    )

    initial_rows = [
        fake_reading(dt.datetime(2026, 7, 2, 23, 0, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 2, 23, 30, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 3, 0, 0, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 3, 0, 30, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 3, 1, 0, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 3, 1, 30, tzinfo=timezone), 0.5),
    ]
    refresh_rows = [
        fake_reading(dt.datetime(2026, 7, 3, 0, 0, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 3, 0, 30, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 3, 1, 0, tzinfo=timezone), 0.5),
        fake_reading(dt.datetime(2026, 7, 3, 1, 30, tzinfo=timezone), 0.5),
    ]

    call_count = 0

    def fake_fetch(resource, start, end, *, chunk_days=10):
        nonlocal call_count
        del resource, end, chunk_days
        call_count += 1
        if call_count == 1:
            assert start == dt.datetime(2026, 6, 30, 0, 0, tzinfo=timezone)
            return initial_rows
        assert start == dt.datetime(2026, 7, 3, 0, 0, tzinfo=timezone)
        return refresh_rows

    await async_setup_recorder_instance(hass)

    with (
        patch.object(history, "_INITIAL_HISTORY_DAYS", 5),
        patch.object(history, "_RECENT_HISTORY_DAYS", 2),
        patch.object(history, "_fetch_half_hour_history", side_effect=fake_fetch),
        patch.object(
            history,
            "_history_window_end",
            return_value=dt.datetime(2026, 7, 5, 0, 0, tzinfo=timezone),
        ),
    ):
        assert await history.async_import_history_statistics(hass, client) is True
        await async_wait_recording_done(hass)
        assert await history.async_import_history_statistics(hass, client) is True

    await async_wait_recording_done(hass)

    stats = await hass.async_add_executor_job(
        partial(
            get_last_statistics,
            hass,
            3,
            "glowmarkt:site_1_ve_1_electricity_import",
            True,
            {"sum"},
        )
    )

    rows = stats["glowmarkt:site_1_ve_1_electricity_import"]

    assert [row["sum"] for row in rows] == [3.0, 2.0, 1.0]
    assert [row["start"] for row in rows] == [
        dt.datetime(2026, 7, 3, 1, 0, tzinfo=dt.timezone.utc).timestamp(),
        dt.datetime(2026, 7, 3, 0, 0, tzinfo=dt.timezone.utc).timestamp(),
        dt.datetime(2026, 7, 2, 23, 0, tzinfo=dt.timezone.utc).timestamp(),
    ]
