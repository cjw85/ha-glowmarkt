"""Historical Glowmarkt energy history ingestion for Home Assistant statistics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
import logging
import re

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .glow_api import get_resource_readings
from .mapping import ELECTRICITY_SUPPLY, GAS_SUPPLY, plan_virtual_entity_meters

_LOGGER = logging.getLogger(__name__)

_HISTORY_CHUNK_DAYS = 10
_INITIAL_HISTORY_DAYS = 400
_RECENT_HISTORY_DAYS = 7
_REFRESH_INTERVAL = timedelta(minutes=30)
_READINGS_END_EPSILON = timedelta(seconds=1)


@dataclass(slots=True)
class HistoryStatisticTarget:
    """One external-statistics target sourced from a Glow resource."""

    virtual_entity: object
    resource: object
    supply: str
    role: str

    @property
    def statistic_id(self) -> str:
        """Return a stable Home Assistant statistics id."""
        return (
            f"{DOMAIN}:"
            f"{_history_object_id(self.virtual_entity, self.supply, self.role)}"
        )

    @property
    def name(self) -> str:
        """Return a human-readable statistics name."""
        prefix = getattr(self.virtual_entity, "name", None) or "Glowmarkt"
        return f"{prefix} {self.supply} {self.role}"


def _recorder_statistics_api():
    """Import recorder statistics helpers lazily."""
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
        get_metadata,
    )

    return async_add_external_statistics, get_metadata


def _slug_token(value: str | None) -> str:
    """Return a recorder-safe slug token."""
    slug = re.sub(r"[^0-9a-z_]+", "_", (value or "").lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "unknown"


def _history_object_id(virtual_entity, supply: str, role: str) -> str:
    """Return the object id used by Home Assistant statistics."""
    name = _slug_token(getattr(virtual_entity, "name", None))
    entity_id = _slug_token(getattr(virtual_entity, "id", None))
    return f"{name}_{entity_id}_{supply}_{role}"


def _history_metadata(target: HistoryStatisticTarget):
    """Build recorder metadata for one Glow history stream."""
    return {
        "has_mean": False,
        "has_sum": True,
        "name": target.name,
        "source": DOMAIN,
        "statistic_id": target.statistic_id,
        "unit_of_measurement": "kWh",
    }


def _history_window_end(now: datetime | None = None) -> datetime:
    """Return the most recent completed top-of-hour boundary."""
    when = now or dt_util.now()
    return when.replace(minute=0, second=0, microsecond=0)


def _fetch_half_hour_history(
    resource,
    start: datetime,
    end: datetime,
    *,
    chunk_days: int = _HISTORY_CHUNK_DAYS,
):
    """Fetch PT30M history in bounded chunks using an exclusive end boundary."""
    if start >= end:
        return []

    readings_by_start: dict[datetime, list] = {}
    window_start = start

    while window_start < end:
        window_end = min(window_start + timedelta(days=chunk_days), end)
        query_end = window_end - _READINGS_END_EPSILON
        if query_end < window_start:
            break
        chunk = get_resource_readings(resource, window_start, query_end, "PT30M", "sum")
        for when, value in chunk:
            readings_by_start[when] = [when, value]
        window_start = window_end

    return [readings_by_start[when] for when in sorted(readings_by_start)]


def _aggregate_half_hour_readings_to_hourly_statistics(readings):
    """Aggregate PT30M readings into hourly cumulative sums for recorder import."""
    if not readings:
        return []

    hourly_totals: dict[datetime, float] = {}
    for when, value in sorted(readings, key=lambda item: item[0]):
        hour_start = when.replace(minute=0, second=0, microsecond=0)
        hourly_totals[hour_start] = hourly_totals.get(hour_start, 0.0) + float(
            value.value
        )

    statistics = []
    cumulative_sum = 0.0
    for hour_start in sorted(hourly_totals):
        cumulative_sum += hourly_totals[hour_start]
        statistics.append(
            {
                "start": hour_start,
                "sum": round(cumulative_sum, 6),
            }
        )

    return statistics


def _select_history_targets(
    virtual_entity, resources: list
) -> list[HistoryStatisticTarget]:
    """Select canonical electricity and gas resources for history import."""
    targets: list[HistoryStatisticTarget] = []
    for plan in plan_virtual_entity_meters(virtual_entity, resources, logger=_LOGGER):
        if plan.supply == ELECTRICITY_SUPPLY:
            targets.append(
                HistoryStatisticTarget(
                    virtual_entity=virtual_entity,
                    resource=plan.usage_resource,
                    supply=plan.supply,
                    role="import",
                )
            )
            if plan.export_resource is not None:
                targets.append(
                    HistoryStatisticTarget(
                        virtual_entity=virtual_entity,
                        resource=plan.export_resource,
                        supply=plan.supply,
                        role="export",
                    )
                )
            continue

        if plan.supply == GAS_SUPPLY:
            targets.append(
                HistoryStatisticTarget(
                    virtual_entity=virtual_entity,
                    resource=plan.usage_resource,
                    supply=plan.supply,
                    role="usage",
                )
            )
    return targets


async def async_import_history_statistics(
    hass: HomeAssistant,
    glow_client,
) -> bool:
    """Import Glow electricity and gas history into Home Assistant statistics."""
    try:
        async_add_external_statistics, get_metadata = _recorder_statistics_api()
    except ImportError:
        _LOGGER.debug("Recorder statistics API unavailable; skipping Glow backfill.")
        return False

    try:
        virtual_entities = await hass.async_add_executor_job(
            glow_client.list_virtual_entities
        )
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning(
            "Failed to fetch Glow virtual entities for history import: %s", ex
        )
        return True

    targets: list[HistoryStatisticTarget] = []
    for virtual_entity in virtual_entities:
        try:
            resources = await hass.async_add_executor_job(virtual_entity.list_resources)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Failed to fetch resources for virtual entity %s (%s): %s",
                getattr(virtual_entity, "name", None) or "Unnamed",
                getattr(virtual_entity, "id", "unknown"),
                ex,
            )
            continue
        targets.extend(_select_history_targets(virtual_entity, resources))

    if not targets:
        _LOGGER.debug("No canonical electricity or gas history targets selected.")
        return True

    statistic_ids = {target.statistic_id for target in targets}
    try:
        existing_metadata = await hass.async_add_executor_job(
            partial(
                get_metadata,
                hass,
                statistic_ids=statistic_ids,
            )
        )
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Failed to query Home Assistant statistics metadata: %s", ex)
        return True

    end = _history_window_end()
    for target in targets:
        history_days = (
            _RECENT_HISTORY_DAYS
            if target.statistic_id in existing_metadata
            else _INITIAL_HISTORY_DAYS
        )
        start = end - timedelta(days=history_days)

        try:
            readings = await hass.async_add_executor_job(
                _fetch_half_hour_history, target.resource, start, end
            )
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Failed to fetch PT30M history for %s (%s): %s",
                target.statistic_id,
                target.resource.id,
                ex,
            )
            continue

        statistics = _aggregate_half_hour_readings_to_hourly_statistics(readings)
        if not statistics:
            _LOGGER.debug(
                "No PT30M history returned for %s in %s day window.",
                target.statistic_id,
                history_days,
            )
            continue

        async_add_external_statistics(
            hass,
            _history_metadata(target),
            statistics,
        )
        _LOGGER.debug(
            "Queued %s hourly statistics rows for %s from %s to %s.",
            len(statistics),
            target.statistic_id,
            start,
            end,
        )

    return True


async def async_history_statistics_loop(
    hass: HomeAssistant,
    glow_client,
) -> None:
    """Continuously refresh imported Glow energy history."""
    try:
        while True:
            if not await async_import_history_statistics(hass, glow_client):
                return
            await asyncio.sleep(_REFRESH_INTERVAL.total_seconds())
    except asyncio.CancelledError:
        _LOGGER.debug("Glow history statistics loop cancelled.")
        raise
