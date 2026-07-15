"""Home Assistant integration tests for Glowmarkt sensor setup."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glowmarkt.const import (
    CONF_DAILY_INTERVAL,
    CONF_TARIFF_INTERVAL,
    DOMAIN,
    ELEC_CONSUMPTION_CLASSIFIER,
    ELEC_COST_CLASSIFIER,
    ELEC_EXPORT_CLASSIFIER,
    GAS_CONSUMPTION_CLASSIFIER,
    GAS_COST_CLASSIFIER,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from tests.fakes import FakeGlowClient, FakeGlowResource, FakeGlowVirtualEntity

pytestmark = [
    pytest.mark.usefixtures("enable_custom_integrations"),
    pytest.mark.usefixtures("sensor_runtime_patches"),
]


async def _setup_integration(hass, client: FakeGlowClient) -> MockConfigEntry:
    """Set up the Glowmarkt config entry against the fake client."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "user@example.com", "password": "secret"},
        version=6,
        options={
            CONF_DAILY_INTERVAL: 15,
            CONF_TARIFF_INTERVAL: 60,
        },
    )
    entry.add_to_hass(hass)

    with patch("custom_components.glowmarkt.GlowClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    return entry


def _entity_id(hass, unique_id: str) -> str | None:
    """Look up an entity id by unique id."""
    entity_registry = er.async_get(hass)
    return entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)


async def test_setup_attaches_export_to_the_canonical_electricity_meter(
    hass,
) -> None:
    client = FakeGlowClient(
        [
            FakeGlowVirtualEntity(
                "ve-1",
                "Site 1",
                resources=[
                    FakeGlowResource(
                        "adhoc-usage",
                        ELEC_CONSUMPTION_CLASSIFIER,
                        "adhoc electricity consumption",
                        daily_total=99.9,
                    ),
                    FakeGlowResource(
                        "dcc-usage",
                        ELEC_CONSUMPTION_CLASSIFIER,
                        "electricity consumption DCC SM profile reads",
                        daily_total=12.34,
                    ),
                    FakeGlowResource(
                        "adhoc-cost",
                        ELEC_COST_CLASSIFIER,
                        "adhoc electricity cost",
                        base_unit="pence",
                        daily_total=999.0,
                    ),
                    FakeGlowResource(
                        "dcc-cost",
                        ELEC_COST_CLASSIFIER,
                        "electricity cost DCC SM profile reads",
                        base_unit="pence",
                        daily_total=123.0,
                    ),
                    FakeGlowResource(
                        "dcc-export",
                        ELEC_EXPORT_CLASSIFIER,
                        "electricity export DCC SM profile reads",
                        daily_total=5.67,
                    ),
                    FakeGlowResource(
                        "gas-usage",
                        GAS_CONSUMPTION_CLASSIFIER,
                        "gas consumption DCC SM profile reads",
                        daily_total=8.5,
                    ),
                    FakeGlowResource(
                        "gas-cost",
                        GAS_COST_CLASSIFIER,
                        "gas cost DCC SM profile reads",
                        base_unit="pence",
                        daily_total=89.0,
                    ),
                ],
            )
        ]
    )

    await _setup_integration(hass, client)

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    usage_entity_id = _entity_id(hass, "dcc-usage_usage_today")
    cost_entity_id = _entity_id(hass, "dcc-cost_cost_today")
    export_entity_id = _entity_id(hass, "dcc-export_export_today")
    gas_usage_entity_id = _entity_id(hass, "gas-usage_usage_today")
    gas_cost_entity_id = _entity_id(hass, "gas-cost_cost_today")
    standing_entity_id = _entity_id(hass, "dcc-cost_standing_charge")
    rate_entity_id = _entity_id(hass, "dcc-cost_rate")

    assert usage_entity_id is not None
    assert cost_entity_id is not None
    assert export_entity_id is not None
    assert gas_usage_entity_id is not None
    assert gas_cost_entity_id is not None
    assert standing_entity_id is not None
    assert rate_entity_id is not None

    electricity_device = device_registry.async_get_device(
        identifiers={(DOMAIN, "dcc-usage")}
    )
    gas_device = device_registry.async_get_device(identifiers={(DOMAIN, "gas-usage")})
    export_device = device_registry.async_get_device(
        identifiers={(DOMAIN, "dcc-export")}
    )

    assert electricity_device is not None
    assert gas_device is not None
    assert export_device is None

    usage_entry = entity_registry.async_get(usage_entity_id)
    cost_entry = entity_registry.async_get(cost_entity_id)
    export_entry = entity_registry.async_get(export_entity_id)
    gas_usage_entry = entity_registry.async_get(gas_usage_entity_id)
    gas_cost_entry = entity_registry.async_get(gas_cost_entity_id)
    standing_entry = entity_registry.async_get(standing_entity_id)
    rate_entry = entity_registry.async_get(rate_entity_id)

    assert usage_entry is not None
    assert cost_entry is not None
    assert export_entry is not None
    assert gas_usage_entry is not None
    assert gas_cost_entry is not None
    assert standing_entry is not None
    assert rate_entry is not None

    assert usage_entry.original_name == "Usage (today)"
    assert export_entry.original_name == "Export (today)"
    assert usage_entry.device_id == electricity_device.id
    assert export_entry.device_id == electricity_device.id
    assert usage_entry.disabled_by == RegistryEntryDisabler.INTEGRATION
    assert cost_entry.disabled_by == RegistryEntryDisabler.INTEGRATION
    assert export_entry.disabled_by == RegistryEntryDisabler.INTEGRATION
    assert gas_usage_entry.disabled_by == RegistryEntryDisabler.INTEGRATION
    assert gas_cost_entry.disabled_by == RegistryEntryDisabler.INTEGRATION
    assert standing_entry.disabled_by is None
    assert rate_entry.disabled_by is None
    assert hass.states.get(usage_entity_id) is None
    assert hass.states.get(cost_entity_id) is None
    assert hass.states.get(export_entity_id) is None
    assert hass.states.get(gas_usage_entity_id) is None
    assert hass.states.get(gas_cost_entity_id) is None

    standing_state = hass.states.get(standing_entity_id)
    rate_state = hass.states.get(rate_entity_id)
    assert standing_state is not None
    assert rate_state is not None
    assert float(standing_state.state) == 0.479
    assert standing_state.attributes["unit_of_measurement"] == "GBP/day"
    assert float(rate_state.state) == 0.245
    assert rate_state.attributes["unit_of_measurement"] == "GBP/kWh"


async def test_setup_skips_ambiguous_electricity_without_guessing(
    hass,
    caplog,
) -> None:
    client = FakeGlowClient(
        [
            FakeGlowVirtualEntity(
                "ve-1",
                "Site 1",
                resources=[
                    FakeGlowResource(
                        "dcc-usage-1",
                        ELEC_CONSUMPTION_CLASSIFIER,
                        "electricity consumption DCC SM profile reads",
                        daily_total=1.0,
                    ),
                    FakeGlowResource(
                        "dcc-usage-2",
                        ELEC_CONSUMPTION_CLASSIFIER,
                        "electricity consumption DCC SM profile reads",
                        daily_total=2.0,
                    ),
                    FakeGlowResource(
                        "dcc-export",
                        ELEC_EXPORT_CLASSIFIER,
                        "electricity export DCC SM profile reads",
                        daily_total=3.0,
                    ),
                ],
            )
        ]
    )

    with caplog.at_level(logging.WARNING):
        await _setup_integration(hass, client)

    assert _entity_id(hass, "dcc-usage-1_usage_today") is None
    assert _entity_id(hass, "dcc-usage-2_usage_today") is None
    assert _entity_id(hass, "dcc-export_export_today") is None
    assert "Skipping electricity secondary sensors" in caplog.text


async def test_setup_keeps_non_export_accounts_working(
    hass,
) -> None:
    client = FakeGlowClient(
        [
            FakeGlowVirtualEntity(
                "ve-1",
                "Site 1",
                resources=[
                    FakeGlowResource(
                        "electricity-usage",
                        ELEC_CONSUMPTION_CLASSIFIER,
                        "electricity consumption DCC SM profile reads",
                        daily_total=4.2,
                    ),
                    FakeGlowResource(
                        "electricity-cost",
                        ELEC_COST_CLASSIFIER,
                        "electricity cost DCC SM profile reads",
                        base_unit="pence",
                        daily_total=77.0,
                    ),
                    FakeGlowResource(
                        "gas-usage",
                        GAS_CONSUMPTION_CLASSIFIER,
                        "gas consumption DCC SM profile reads",
                        daily_total=6.3,
                    ),
                    FakeGlowResource(
                        "gas-cost",
                        GAS_COST_CLASSIFIER,
                        "gas cost DCC SM profile reads",
                        base_unit="pence",
                        daily_total=55.0,
                    ),
                ],
            )
        ]
    )

    await _setup_integration(hass, client)

    assert _entity_id(hass, "electricity-usage_usage_today") is not None
    assert _entity_id(hass, "electricity-cost_cost_today") is not None
    assert _entity_id(hass, "gas-usage_usage_today") is not None
    assert _entity_id(hass, "gas-cost_cost_today") is not None
    assert _entity_id(hass, "electricity-export_export_today") is None


async def test_tariff_entities_fall_back_to_usage_resource_without_cost(
    hass,
) -> None:
    client = FakeGlowClient(
        [
            FakeGlowVirtualEntity(
                "ve-1",
                "Site 1",
                resources=[
                    FakeGlowResource(
                        "electricity-usage",
                        ELEC_CONSUMPTION_CLASSIFIER,
                        "electricity consumption DCC SM profile reads",
                        daily_total=4.2,
                    )
                ],
            )
        ]
    )

    await _setup_integration(hass, client)

    usage_entity_id = _entity_id(hass, "electricity-usage_usage_today")
    standing_entity_id = _entity_id(hass, "electricity-usage_standing_charge")
    rate_entity_id = _entity_id(hass, "electricity-usage_rate")

    assert usage_entity_id is not None
    assert standing_entity_id is not None
    assert rate_entity_id is not None
    assert hass.states.get(usage_entity_id) is None

    standing_state = hass.states.get(standing_entity_id)
    rate_state = hass.states.get(rate_entity_id)
    assert standing_state is not None
    assert rate_state is not None
    assert float(standing_state.state) == 0.479
    assert standing_state.attributes["unit_of_measurement"] == "GBP/day"
    assert float(rate_state.state) == 0.245
