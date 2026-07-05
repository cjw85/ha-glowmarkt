"""Tests for canonical Glow resource selection."""

from __future__ import annotations

import logging

from custom_components.glowmarkt import const, mapping

from tests.fakes import FakeGlowResource, FakeGlowVirtualEntity


def test_select_canonical_resources_prefers_dcc_over_adhoc_duplicates() -> None:
    virtual_entity = FakeGlowVirtualEntity("ve-1", "Site 1")
    resources = [
        FakeGlowResource(
            "adhoc-usage",
            const.ELEC_CONSUMPTION_CLASSIFIER,
            "adhoc electricity consumption",
        ),
        FakeGlowResource(
            "dcc-usage",
            const.ELEC_CONSUMPTION_CLASSIFIER,
            "electricity consumption DCC SM profile reads",
        ),
        FakeGlowResource(
            "adhoc-cost",
            const.ELEC_COST_CLASSIFIER,
            "adhoc electricity cost",
            base_unit="pence",
        ),
        FakeGlowResource(
            "dcc-cost",
            const.ELEC_COST_CLASSIFIER,
            "electricity cost DCC SM profile reads",
            base_unit="pence",
        ),
    ]

    selected = mapping.select_canonical_resources(virtual_entity, resources)
    electricity = selected[(virtual_entity.id, mapping.ELECTRICITY_SUPPLY)]

    assert electricity.usage.id == "dcc-usage"
    assert electricity.cost.id == "dcc-cost"


def test_select_canonical_resources_keeps_virtual_entities_separate() -> None:
    first_entity = FakeGlowVirtualEntity("ve-1", "Site 1")
    second_entity = FakeGlowVirtualEntity("ve-2", "Site 2")

    selected = {}
    selected.update(
        mapping.select_canonical_resources(
            first_entity,
            [
                FakeGlowResource(
                    "site-1-electricity",
                    const.ELEC_CONSUMPTION_CLASSIFIER,
                    "electricity consumption DCC SM profile reads",
                )
            ],
        )
    )
    selected.update(
        mapping.select_canonical_resources(
            second_entity,
            [
                FakeGlowResource(
                    "site-2-electricity",
                    const.ELEC_CONSUMPTION_CLASSIFIER,
                    "electricity consumption DCC SM profile reads",
                )
            ],
        )
    )

    assert selected[(first_entity.id, mapping.ELECTRICITY_SUPPLY)].usage.id == (
        "site-1-electricity"
    )
    assert selected[(second_entity.id, mapping.ELECTRICITY_SUPPLY)].usage.id == (
        "site-2-electricity"
    )


def test_select_canonical_resources_skips_ambiguous_export(caplog) -> None:
    virtual_entity = FakeGlowVirtualEntity("ve-1", "Site 1")
    resources = [
        FakeGlowResource(
            "dcc-usage",
            const.ELEC_CONSUMPTION_CLASSIFIER,
            "electricity consumption DCC SM profile reads",
        ),
        FakeGlowResource(
            "export-1",
            const.ELEC_EXPORT_CLASSIFIER,
            "electricity export DCC SM profile reads",
        ),
        FakeGlowResource(
            "export-2",
            const.ELEC_EXPORT_CLASSIFIER,
            "electricity export DCC SM profile reads",
        ),
    ]

    with caplog.at_level(logging.WARNING):
        selected = mapping.select_canonical_resources(virtual_entity, resources)

    assert selected[(virtual_entity.id, mapping.ELECTRICITY_SUPPLY)].export is None
    assert "multiple equally preferred electricity.export resources" in caplog.text


def test_dcc_context_resource_uses_canonical_usage_resource() -> None:
    virtual_entity = FakeGlowVirtualEntity("ve-1", "Site 1")
    usage = FakeGlowResource(
        "dcc-usage",
        const.ELEC_CONSUMPTION_CLASSIFIER,
        "electricity consumption DCC SM profile reads",
    )
    export = FakeGlowResource(
        "export-resource",
        const.ELEC_EXPORT_CLASSIFIER,
        "electricity energy from active export power",
    )

    plan = mapping.plan_virtual_entity_meters(
        virtual_entity,
        [usage, export],
    )[0]

    assert mapping.dcc_context_resource(plan) is usage
