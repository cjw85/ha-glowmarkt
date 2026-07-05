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
from .mapping import (
    ELECTRICITY_SUPPLY,
    GAS_SUPPLY,
    dcc_context_resource,
    plan_virtual_entity_meters,
)

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.recorder.models.statistics import StatisticMeanType
except ImportError:  # pragma: no cover - older Home Assistant core
    StatisticMeanType = None


def _statistics_meta_supports_mean_type() -> bool:
    """Return whether the running Home Assistant recorder accepts mean_type."""
    if StatisticMeanType is None:
        return False

    try:
        from homeassistant.components.recorder.db_schema import StatisticsMeta
    except ImportError:  # pragma: no cover - recorder unavailable
        return False

    try:
        StatisticsMeta.from_meta(
            {
                "has_mean": False,
                "mean_type": int(StatisticMeanType.NONE),
                "has_sum": True,
                "name": None,
                "source": DOMAIN,
                "statistic_id": f"{DOMAIN}:compat_probe",
                "unit_of_measurement": None,
            }
        )
    except TypeError:
        return False

    return True


_SUPPORTS_MEAN_TYPE = _statistics_meta_supports_mean_type()

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
    dcc_context_resource: object | None = None
    statistic_unit: str = "kWh"
    value_scale: float = 1.0

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
        statistics_during_period,
    )

    return async_add_external_statistics, get_metadata, statistics_during_period


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
    metadata = {
        "has_mean": False,
        "has_sum": True,
        "name": target.name,
        "source": DOMAIN,
        "statistic_id": target.statistic_id,
        "unit_of_measurement": target.statistic_unit,
    }
    if _SUPPORTS_MEAN_TYPE:
        metadata["mean_type"] = int(StatisticMeanType.NONE)
    return metadata


def _history_window_end(now: datetime | None = None) -> datetime:
    """Return the most recent completed top-of-hour boundary."""
    when = now or dt_util.now()
    return when.replace(minute=0, second=0, microsecond=0)


def _floor_to_half_hour(value: datetime) -> datetime:
    """Return a datetime floored to the previous half-hour boundary."""
    minute = 30 if value.minute >= 30 else 0
    return value.replace(minute=minute, second=0, microsecond=0)


def _useful_history_end(
    requested_end: datetime,
    last_time: datetime | None,
) -> datetime:
    """Return the latest complete-hour boundary backed by real resource data."""
    if last_time is None:
        return requested_end

    localized_last_time = last_time
    if requested_end.tzinfo is not None and last_time.tzinfo is not None:
        localized_last_time = last_time.astimezone(requested_end.tzinfo)

    last_bucket_start = _floor_to_half_hour(localized_last_time)
    if last_bucket_start.minute == 30:
        useful_end = (
            last_bucket_start.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1)
        )
    else:
        useful_end = last_bucket_start

    return min(requested_end, useful_end)


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


def _aggregate_half_hour_readings_to_hourly_statistics(
    readings,
    *,
    value_scale: float = 1.0,
    starting_sum: float = 0.0,
):
    """Aggregate PT30M readings into hourly cumulative sums for recorder import."""
    if not readings:
        return []

    hourly_totals: dict[datetime, float] = {}
    for when, value in sorted(readings, key=lambda item: item[0]):
        hour_start = when.replace(minute=0, second=0, microsecond=0)
        hourly_totals[hour_start] = hourly_totals.get(hour_start, 0.0) + (
            float(value.value) * value_scale
        )

    statistics = []
    cumulative_sum = float(starting_sum)
    for hour_start in sorted(hourly_totals):
        cumulative_sum += hourly_totals[hour_start]
        statistics.append(
            {
                "start": hour_start,
                "sum": round(cumulative_sum, 6),
            }
        )

    return statistics


def _previous_sum_lookup_start(start: datetime) -> datetime:
    """Return a small lookback window for finding the prior hourly sum row."""
    return start - timedelta(hours=2)


async def _existing_statistic_sum_before(
    hass: HomeAssistant,
    statistics_during_period,
    statistic_id: str,
    start: datetime,
) -> float:
    """Return the latest known cumulative sum before a refresh window."""
    rows = await hass.async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            _previous_sum_lookup_start(start),
            start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
    )
    statistic_rows = rows.get(statistic_id, [])
    if not statistic_rows:
        return 0.0

    prior_sum = statistic_rows[-1].get("sum")
    if prior_sum is None:
        return 0.0
    return float(prior_sum)


def _select_history_targets(
    virtual_entity, resources: list
) -> list[HistoryStatisticTarget]:
    """Select canonical electricity and gas resources for history import."""
    targets: list[HistoryStatisticTarget] = []
    for plan in plan_virtual_entity_meters(virtual_entity, resources, logger=_LOGGER):
        refresh_resource = dcc_context_resource(plan)
        if plan.supply == ELECTRICITY_SUPPLY:
            targets.append(
                HistoryStatisticTarget(
                    virtual_entity=virtual_entity,
                    resource=plan.usage_resource,
                    supply=plan.supply,
                    role="import",
                    dcc_context_resource=refresh_resource,
                )
            )
            if plan.cost_resource is not None:
                targets.append(
                    HistoryStatisticTarget(
                        virtual_entity=virtual_entity,
                        resource=plan.cost_resource,
                        supply=plan.supply,
                        role="cost",
                        dcc_context_resource=refresh_resource,
                        statistic_unit="GBP",
                        value_scale=0.01,
                    )
                )
            if plan.export_resource is not None:
                targets.append(
                    HistoryStatisticTarget(
                        virtual_entity=virtual_entity,
                        resource=plan.export_resource,
                        supply=plan.supply,
                        role="export",
                        dcc_context_resource=refresh_resource,
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
                    dcc_context_resource=refresh_resource,
                )
            )
            if plan.cost_resource is not None:
                targets.append(
                    HistoryStatisticTarget(
                        virtual_entity=virtual_entity,
                        resource=plan.cost_resource,
                        supply=plan.supply,
                        role="cost",
                        dcc_context_resource=refresh_resource,
                        statistic_unit="GBP",
                        value_scale=0.01,
                    )
                )
    return targets


def _catchup_resources(targets: list[HistoryStatisticTarget]) -> list[object]:
    """Return canonical DCC-backed resources that should receive catchup."""
    resources: list[object] = []
    seen_resource_ids: set[str] = set()

    for target in targets:
        resource = target.dcc_context_resource
        resource_id = getattr(resource, "id", None)
        if resource_id is None or resource_id in seen_resource_ids:
            continue
        seen_resource_ids.add(resource_id)
        resources.append(resource)

    return resources


async def _async_trigger_catchup(
    hass: HomeAssistant,
    targets: list[HistoryStatisticTarget],
) -> None:
    """Best-effort trigger of Glow DCC catchup for selected resources."""
    for resource in _catchup_resources(targets):
        try:
            result = await hass.async_add_executor_job(resource.catch_up)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Failed to trigger Glow DCC catchup for %s (%s): %s",
                resource.id,
                getattr(resource, "classifier", "unknown"),
                ex,
            )
            continue

        valid = getattr(getattr(result, "data", None), "valid", None)
        if valid is False:
            _LOGGER.warning(
                "Glow DCC catchup returned invalid=false for %s (%s).",
                resource.id,
                getattr(resource, "classifier", "unknown"),
            )
            continue

        _LOGGER.debug(
            "Triggered Glow DCC catchup for %s (%s); valid=%s",
            resource.id,
            getattr(resource, "classifier", "unknown"),
            valid,
        )


async def _async_target_history_end(
    hass: HomeAssistant,
    target: HistoryStatisticTarget,
    requested_end: datetime,
) -> datetime:
    """Return the usable history end for one target."""
    resource = target.dcc_context_resource
    if resource is None:
        return requested_end

    try:
        last_time = await hass.async_add_executor_job(resource.get_last_time)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning(
            "Failed to fetch Glow last-time for %s (%s): %s",
            resource.id,
            getattr(resource, "classifier", "unknown"),
            ex,
        )
        return requested_end

    useful_end = _useful_history_end(requested_end, last_time)
    if last_time is None:
        _LOGGER.debug(
            "Glow last-time returned no timestamp for %s (%s); using requested end %s.",
            resource.id,
            getattr(resource, "classifier", "unknown"),
            requested_end,
        )
        return requested_end

    if useful_end < requested_end:
        _LOGGER.debug(
            "Truncating Glow history for %s (%s) from requested end %s to useful end %s based on last-time %s.",
            resource.id,
            getattr(resource, "classifier", "unknown"),
            requested_end,
            useful_end,
            last_time,
        )
    return useful_end


async def async_import_history_statistics(
    hass: HomeAssistant,
    glow_client,
) -> bool:
    """Import Glow electricity and gas history into Home Assistant statistics."""
    try:
        (
            async_add_external_statistics,
            get_metadata,
            statistics_during_period,
        ) = _recorder_statistics_api()
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

    await _async_trigger_catchup(hass, targets)

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
    target_end_by_context_resource_id: dict[str, datetime] = {}
    for target in targets:
        history_days = (
            _RECENT_HISTORY_DAYS
            if target.statistic_id in existing_metadata
            else _INITIAL_HISTORY_DAYS
        )
        context_resource = target.dcc_context_resource
        context_resource_id = getattr(context_resource, "id", None)
        if (
            context_resource_id is not None
            and context_resource_id in target_end_by_context_resource_id
        ):
            target_end = target_end_by_context_resource_id[context_resource_id]
        else:
            target_end = await _async_target_history_end(hass, target, end)
            if context_resource_id is not None:
                target_end_by_context_resource_id[context_resource_id] = target_end
        start = end - timedelta(days=history_days)
        if target_end <= start:
            _LOGGER.debug(
                "Skipping %s because useful history end %s is not after start %s.",
                target.statistic_id,
                target_end,
                start,
            )
            continue

        try:
            readings = await hass.async_add_executor_job(
                _fetch_half_hour_history, target.resource, start, target_end
            )
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Failed to fetch PT30M history for %s (%s): %s",
                target.statistic_id,
                target.resource.id,
                ex,
            )
            continue

        starting_sum = 0.0
        if target.statistic_id in existing_metadata:
            try:
                starting_sum = await _existing_statistic_sum_before(
                    hass,
                    statistics_during_period,
                    target.statistic_id,
                    start,
                )
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Failed to query existing statistics baseline for %s: %s",
                    target.statistic_id,
                    ex,
                )
                starting_sum = 0.0

        statistics = _aggregate_half_hour_readings_to_hourly_statistics(
            readings,
            value_scale=target.value_scale,
            starting_sum=starting_sum,
        )
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
            target_end,
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
