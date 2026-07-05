"""Sensor platform for the Glowmarkt integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging

from requests.exceptions import ConnectionError, HTTPError, Timeout

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DAILY_INTERVAL,
    CONF_TARIFF_INTERVAL,
    DOMAIN,
    GAS_CONSUMPTION_CLASSIFIER,
)
from .glow_api import get_resource_readings, glow_request_offset_minutes
from .mapping import (
    ELECTRICITY_SUPPLY,
    MeterPlan,
    device_name,
    plan_virtual_entity_meters,
)

_LOGGER = logging.getLogger(__name__)


class DataCoordinator(DataUpdateCoordinator):
    """Data update coordinator for daily usage and cost sensors."""

    def __init__(self, hass: HomeAssistant, resource, daily_interval: int) -> None:
        self.resource = resource
        super().__init__(
            hass,
            _LOGGER,
            name=f"Daily Data {resource.classifier}",
            update_interval=timedelta(minutes=daily_interval),
        )

    async def _async_update_data(self):
        """Fetch data from the daily usage API endpoint."""
        _LOGGER.debug(
            "DataCoordinator updating for resource %s", self.resource.classifier
        )
        try:
            value = await daily_data(self.hass, self.resource)
            if value is None:
                return None
            return value
        except HTTPError as ex:
            raise UpdateFailed(
                f"HTTP error fetching daily data: {ex}, status {ex.response.status_code}"
            ) from ex
        except Timeout as ex:
            raise UpdateFailed(f"Timeout fetching daily data: {ex}") from ex
        except ConnectionError as ex:
            raise UpdateFailed(f"Connection error fetching daily data: {ex}") from ex
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception fetching daily data: %s", ex)
            raise UpdateFailed(f"Unknown error fetching daily data: {ex}") from ex


class TariffCoordinator(DataUpdateCoordinator):
    """Data update coordinator for tariff-derived sensors."""

    def __init__(self, hass: HomeAssistant, resource, tariff_interval: int) -> None:
        self.resource = resource
        super().__init__(
            hass,
            _LOGGER,
            name=f"Tariff Data {resource.classifier}",
            update_interval=timedelta(minutes=tariff_interval),
        )

    async def _async_update_data(self):
        """Fetch data from the tariff API endpoint."""
        _LOGGER.debug(
            "TariffCoordinator updating for resource %s", self.resource.classifier
        )
        try:
            tariff = await tariff_data(self.hass, self.resource)
            if tariff is None:
                raise UpdateFailed(
                    f"No tariff data received for {self.resource.classifier}"
                )
            return tariff
        except HTTPError as ex:
            _LOGGER.error(
                "HTTP error fetching tariff data for %s: %s, status %s",
                self.resource.classifier,
                ex,
                ex.response.status_code,
            )
            raise UpdateFailed(f"Failed to fetch tariff data: {ex}") from ex
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception(
                "Error fetching tariff data for %s: %s", self.resource.classifier, ex
            )
            raise UpdateFailed(f"Failed to fetch tariff data: {ex}") from ex


@dataclass(frozen=True, slots=True)
class GlowSensorSpec:
    """One Home Assistant sensor shape backed by a coordinator."""

    unique_id_suffix: str
    name: str
    native_unit: str | None
    value_extractor: Callable[[object], object | None]
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    icon_fn: Callable[[object], str | None] | None = None
    enabled_default: bool = True


def _usage_icon(resource) -> str | None:
    """Return the icon override for a usage resource."""
    if resource.classifier == GAS_CONSUMPTION_CLASSIFIER:
        return "mdi:fire"
    return None


def _round_usage_value(data) -> float:
    """Round a daily kWh reading to the entity precision."""
    return round(float(data), 2)


def _round_cost_value(data) -> float:
    """Convert pence to GBP and round to the entity precision."""
    return round(float(data) / 100, 2)


def _standing_charge_value(data) -> float:
    """Extract the standing charge from the tariff payload."""
    return round(float(data.current_rates.standing_charge.value) / 100, 4)


def _rate_value(data) -> float:
    """Extract the unit rate from the tariff payload."""
    return round(float(data.current_rates.rate.value) / 100, 4)


USAGE_SENSOR = GlowSensorSpec(
    unique_id_suffix="usage_today",
    name="Usage (today)",
    native_unit=UnitOfEnergy.KILO_WATT_HOUR,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    icon_fn=_usage_icon,
    value_extractor=_round_usage_value,
)

EXPORT_SENSOR = GlowSensorSpec(
    unique_id_suffix="export_today",
    name="Export (today)",
    native_unit=UnitOfEnergy.KILO_WATT_HOUR,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    value_extractor=_round_usage_value,
)

COST_SENSOR = GlowSensorSpec(
    unique_id_suffix="cost_today",
    name="Cost (today)",
    native_unit="GBP",
    device_class=SensorDeviceClass.MONETARY,
    state_class=SensorStateClass.TOTAL,
    value_extractor=_round_cost_value,
)

STANDING_SENSOR = GlowSensorSpec(
    unique_id_suffix="standing_charge",
    name="Standing charge",
    native_unit="GBP",
    device_class=SensorDeviceClass.MONETARY,
    enabled_default=False,
    value_extractor=_standing_charge_value,
)

RATE_SENSOR = GlowSensorSpec(
    unique_id_suffix="rate",
    name="Rate",
    native_unit="GBP/kWh",
    icon="mdi:cash-multiple",
    enabled_default=False,
    value_extractor=_rate_value,
)


class GlowSensor(CoordinatorEntity, SensorEntity):
    """Generic coordinator-backed Glow sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        resource,
        virtual_entity,
        spec: GlowSensorSpec,
        *,
        device_resource=None,
    ) -> None:
        super().__init__(coordinator)
        self.resource = resource
        self.virtual_entity = virtual_entity
        self.spec = spec
        self.device_resource = device_resource or resource

        self._attr_unique_id = f"{resource.id}_{spec.unique_id_suffix}"
        self._attr_name = spec.name
        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.native_unit
        self._attr_state_class = spec.state_class

        icon = spec.icon
        if spec.icon_fn is not None:
            icon = spec.icon_fn(resource)
        if icon is not None:
            self._attr_icon = icon
        if not spec.enabled_default:
            self._attr_entity_registry_enabled_default = False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the physical meter device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_resource.id)},
            manufacturer="Hildebrand",
            model="Glowmarkt",
            name=device_name(self.device_resource, self.virtual_entity),
        )

    @property
    def native_value(self):
        """Return the current sensor value from coordinator data."""
        if self.coordinator.data is None:
            return None

        try:
            return self.spec.value_extractor(self.coordinator.data)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug(
                "Failed to extract %s value for %s (%s): %s",
                self.spec.name,
                self.resource.classifier,
                self.resource.id,
                ex,
            )
            return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


def _get_resource_readings(
    resource,
    t_from: date | datetime,
    t_to: date | datetime,
    period: str,
    func: str = "sum",
    nulls: bool = False,
):
    """Fetch readings via the internal Glow client."""
    try:
        return get_resource_readings(resource, t_from, t_to, period, func, nulls)
    except HTTPError as ex:
        body = getattr(getattr(ex, "response", None), "text", "").strip()
        if len(body) > 300:
            body = f"{body[:300]}..."
        raise RuntimeError(
            "Request failed for resource "
            f"{resource.id} ({getattr(resource, 'classifier', 'unknown')}) "
            f"with status {getattr(getattr(ex, 'response', None), 'status_code', 'unknown')}: "
            f"{body or 'empty response'}"
        ) from ex


async def daily_data(hass: HomeAssistant, resource) -> float | None:
    """Return today's cumulative usage or cost for a Glow resource."""
    now = dt_util.now()
    t_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t_to = now.replace(second=0, microsecond=0)
    _LOGGER.debug("Glow offset is: %s", glow_request_offset_minutes(now))
    _LOGGER.debug(
        "Get readings from %s to %s for %s when now=%s",
        t_from,
        t_to,
        resource.classifier,
        now,
    )

    try:
        readings = await hass.async_add_executor_job(
            _get_resource_readings, resource, t_from, t_to, "P1D", "sum"
        )
    except HTTPError as ex:
        _LOGGER.error(
            "HTTP error fetching daily data for %s: %s, status %s",
            resource.classifier,
            ex,
            ex.response.status_code,
        )
        return None
    except Timeout as ex:
        _LOGGER.error("Timeout fetching daily data for %s: %s", resource.id, ex)
        return None
    except ConnectionError as ex:
        _LOGGER.error(
            "Connection error fetching daily data for %s: %s", resource.id, ex
        )
        return None
    except RuntimeError as ex:
        _LOGGER.warning("Daily readings request failed for %s: %s", resource.id, ex)
        return None
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception("Unexpected exception fetching daily data: %s", ex)
        return None

    _LOGGER.debug(
        "Successfully got %s daily readings for resource id %s",
        len(readings),
        resource.id,
    )
    if not readings:
        return None

    value = float(readings[0][1].value)
    if len(readings) > 1:
        value += float(readings[1][1].value)
    return value


async def tariff_data(hass: HomeAssistant, resource):
    """Get tariff data from the API."""
    try:
        tariff = await hass.async_add_executor_job(resource.get_tariff)
        _LOGGER.debug(
            "Successful GET to https://api.glowmarkt.com/api/v0-1/resource/%s/tariff",
            resource.id,
        )
        return tariff
    except HTTPError as ex:
        _LOGGER.error(
            "HTTP error fetching tariff data for %s: %s, status %s",
            resource.classifier,
            ex,
            ex.response.status_code,
        )
        return None
    except Timeout as ex:
        _LOGGER.error("Timeout fetching tariff data for %s: %s", resource.id, ex)
        return None
    except ConnectionError as ex:
        _LOGGER.error(
            "Connection error fetching tariff data for %s: %s", resource.id, ex
        )
        return None
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception(
            "Unexpected exception fetching tariff data for %s: %s",
            resource.classifier,
            ex,
        )
        return None


async def _delayed_first_refresh(coordinator: DataUpdateCoordinator, delay: int = 5):
    """Perform first refresh after a delay."""
    _LOGGER.debug(
        "Scheduling delayed first refresh for %s in %d seconds", coordinator.name, delay
    )
    await asyncio.sleep(delay)
    await coordinator.async_request_refresh()
    _LOGGER.debug("Completed delayed first refresh for %s", coordinator.name)


def _cached_coordinator(
    hass: HomeAssistant,
    resource,
    cache: dict[str, DataUpdateCoordinator],
    create: Callable[[object], DataUpdateCoordinator],
) -> DataUpdateCoordinator:
    """Return a cached coordinator for a resource, creating it on first use."""
    coordinator = cache.get(resource.id)
    if coordinator is None:
        coordinator = create(resource)
        cache[resource.id] = coordinator
        hass.async_create_task(_delayed_first_refresh(coordinator, 5))
    return coordinator


def _daily_coordinator_for_resource(
    hass: HomeAssistant,
    resource,
    daily_interval: int,
    daily_coordinators: dict[str, DataCoordinator],
) -> DataCoordinator:
    """Return a cached daily coordinator for a resource."""
    coordinator = _cached_coordinator(
        hass,
        resource,
        daily_coordinators,
        lambda current_resource: DataCoordinator(
            hass, current_resource, daily_interval
        ),
    )
    return coordinator


def _tariff_coordinator_for_resource(
    hass: HomeAssistant,
    resource,
    tariff_interval: int,
    tariff_coordinators: dict[str, TariffCoordinator],
) -> TariffCoordinator:
    """Return a cached tariff coordinator for a resource."""
    coordinator = _cached_coordinator(
        hass,
        resource,
        tariff_coordinators,
        lambda current_resource: TariffCoordinator(
            hass, current_resource, tariff_interval
        ),
    )
    return coordinator


def _build_meter_entities(
    hass: HomeAssistant,
    plan: MeterPlan,
    *,
    daily_interval: int,
    tariff_interval: int,
    daily_coordinators: dict[str, DataCoordinator],
    tariff_coordinators: dict[str, TariffCoordinator],
) -> list[GlowSensor]:
    """Build all entities for one canonical physical meter."""
    meter_resource = plan.usage_resource
    usage_sensor = GlowSensor(
        _daily_coordinator_for_resource(
            hass, plan.usage_resource, daily_interval, daily_coordinators
        ),
        plan.usage_resource,
        plan.virtual_entity,
        USAGE_SENSOR,
    )
    entities = [usage_sensor]

    tariff_coordinator = _tariff_coordinator_for_resource(
        hass, plan.usage_resource, tariff_interval, tariff_coordinators
    )
    entities.extend(
        [
            GlowSensor(
                tariff_coordinator,
                plan.usage_resource,
                plan.virtual_entity,
                STANDING_SENSOR,
                device_resource=meter_resource,
            ),
            GlowSensor(
                tariff_coordinator,
                plan.usage_resource,
                plan.virtual_entity,
                RATE_SENSOR,
                device_resource=meter_resource,
            ),
        ]
    )

    if plan.cost_resource is not None:
        entities.append(
            GlowSensor(
                _daily_coordinator_for_resource(
                    hass,
                    plan.cost_resource,
                    daily_interval,
                    daily_coordinators,
                ),
                plan.cost_resource,
                plan.virtual_entity,
                COST_SENSOR,
                device_resource=meter_resource,
            )
        )

    if plan.supply == ELECTRICITY_SUPPLY and plan.export_resource is not None:
        entities.append(
            GlowSensor(
                _daily_coordinator_for_resource(
                    hass,
                    plan.export_resource,
                    daily_interval,
                    daily_coordinators,
                ),
                plan.export_resource,
                plan.virtual_entity,
                EXPORT_SENSOR,
                device_resource=meter_resource,
            )
        )

    return entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> bool:
    """Set up the sensor platform."""
    entities: list[GlowSensor] = []
    daily_coordinators: dict[str, DataCoordinator] = {}
    tariff_coordinators: dict[str, TariffCoordinator] = {}

    glow_client = hass.data[DOMAIN][entry.entry_id]["client"]
    daily_interval = hass.data[DOMAIN][entry.entry_id].get(CONF_DAILY_INTERVAL, 15)
    tariff_interval = hass.data[DOMAIN][entry.entry_id].get(CONF_TARIFF_INTERVAL, 60)

    try:
        virtual_entities = await hass.async_add_executor_job(
            glow_client.list_virtual_entities
        )
        _LOGGER.debug("Successful GET to %svirtualentity", glow_client.url)
    except HTTPError as ex:
        _LOGGER.error(
            "HTTP error fetching virtual entities: status %s - %s",
            ex.response.status_code,
            ex,
        )
        return False
    except (Timeout, ConnectionError) as ex:
        _LOGGER.error("Failed to get virtual entities: %s", ex)
        return False
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception("Unexpected exception fetching virtual entities: %s", ex)
        return False

    for virtual_entity in virtual_entities:
        _LOGGER.debug("Found virtual entity: %s", virtual_entity.name)
        try:
            resources = await hass.async_add_executor_job(virtual_entity.list_resources)
            _LOGGER.debug(
                "Successful GET to %svirtualentity/%s/resources",
                glow_client.url,
                virtual_entity.id,
            )
        except HTTPError as ex:
            _LOGGER.error(
                "HTTP error fetching resources for %s: status %s - %s",
                virtual_entity.name,
                ex.response.status_code,
                ex,
            )
            continue
        except (Timeout, ConnectionError) as ex:
            _LOGGER.error("Failed to get resources for %s: %s", virtual_entity.id, ex)
            continue
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception(
                "Unexpected exception fetching resources for %s: %s",
                virtual_entity.id,
                ex,
            )
            continue

        for plan in plan_virtual_entity_meters(
            virtual_entity, resources, logger=_LOGGER
        ):
            entities.extend(
                _build_meter_entities(
                    hass,
                    plan,
                    daily_interval=daily_interval,
                    tariff_interval=tariff_interval,
                    daily_coordinators=daily_coordinators,
                    tariff_coordinators=tariff_coordinators,
                )
            )

    _LOGGER.debug("Calling async_add_entities with %s entities", len(entities))
    async_add_entities(entities)
    return True
