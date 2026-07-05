"""Canonical Glow resource mapping and meter planning."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from .const import (
    ELEC_CONSUMPTION_CLASSIFIER,
    ELEC_COST_CLASSIFIER,
    ELEC_EXPORT_CLASSIFIER,
    GAS_CONSUMPTION_CLASSIFIER,
    GAS_COST_CLASSIFIER,
)

_LOGGER = logging.getLogger(__name__)

ELECTRICITY_SUPPLY = "electricity"
GAS_SUPPLY = "gas"


@dataclass(slots=True)
class CanonicalSupplyResources:
    """Canonical resources selected for a supply within one virtual entity."""

    usage: object | None = None
    cost: object | None = None
    export: object | None = None


@dataclass(slots=True)
class MeterPlan:
    """Canonical entity-building plan for one physical meter."""

    virtual_entity: object
    supply: str
    usage_resource: object
    cost_resource: object | None = None
    export_resource: object | None = None


def supply_type(resource) -> str:
    """Return supply type."""
    if resource.classifier.startswith(f"{ELECTRICITY_SUPPLY}."):
        return ELECTRICITY_SUPPLY
    if resource.classifier.startswith(f"{GAS_SUPPLY}."):
        return GAS_SUPPLY
    _LOGGER.error("Unknown classifier: %s. Please open an issue", resource.classifier)
    return "unknown"


def device_name(resource, virtual_entity) -> str:
    """Return device name. Includes virtual entity name if present."""
    supply = supply_type(resource)
    if virtual_entity.name is not None:
        return f"{virtual_entity.name} smart {supply} meter"
    return f"Smart {supply} meter"


def _resource_selection_score(resource) -> int:
    """Rank a resource for canonical selection within a virtual entity."""
    text = " ".join(
        filter(
            None,
            [getattr(resource, "name", None), getattr(resource, "description", None)],
        )
    ).lower()
    if "dcc" in text or "profile read" in text:
        return 2
    if "adhoc" in text:
        return 0
    return 1


def _resource_summary(resource) -> str:
    """Return a short string for logging canonical-selection warnings."""
    description = getattr(resource, "description", None) or getattr(
        resource, "name", None
    )
    return f"{resource.id} ({description or 'no description'})"


def _select_canonical_resource(
    virtual_entity,
    resources: list,
    classifier: str,
    role: str,
    *,
    logger,
):
    """Select the best resource for a classifier or return None if ambiguous."""
    candidates = [
        resource for resource in resources if resource.classifier == classifier
    ]
    if not candidates:
        return None

    best_score = max(_resource_selection_score(resource) for resource in candidates)
    best_candidates = [
        resource
        for resource in candidates
        if _resource_selection_score(resource) == best_score
    ]

    if len(best_candidates) > 1:
        summaries = ", ".join(
            _resource_summary(resource) for resource in best_candidates
        )
        logger.warning(
            "Skipping %s %s selection for virtual entity %s (%s): multiple equally preferred %s resources: %s",
            supply_type(best_candidates[0]),
            role,
            virtual_entity.name or "Unnamed",
            virtual_entity.id,
            classifier,
            summaries,
        )
        return None

    return best_candidates[0]


def select_canonical_resources(
    virtual_entity,
    resources: list,
    *,
    logger=_LOGGER,
) -> dict[tuple[str, str], CanonicalSupplyResources]:
    """Choose canonical resources for each supply within a virtual entity."""
    return {
        (virtual_entity.id, ELECTRICITY_SUPPLY): CanonicalSupplyResources(
            usage=_select_canonical_resource(
                virtual_entity,
                resources,
                ELEC_CONSUMPTION_CLASSIFIER,
                "usage",
                logger=logger,
            ),
            cost=_select_canonical_resource(
                virtual_entity,
                resources,
                ELEC_COST_CLASSIFIER,
                "cost",
                logger=logger,
            ),
            export=_select_canonical_resource(
                virtual_entity,
                resources,
                ELEC_EXPORT_CLASSIFIER,
                "export",
                logger=logger,
            ),
        ),
        (virtual_entity.id, GAS_SUPPLY): CanonicalSupplyResources(
            usage=_select_canonical_resource(
                virtual_entity,
                resources,
                GAS_CONSUMPTION_CLASSIFIER,
                "usage",
                logger=logger,
            ),
            cost=_select_canonical_resource(
                virtual_entity,
                resources,
                GAS_COST_CLASSIFIER,
                "cost",
                logger=logger,
            ),
        ),
    }


def plan_virtual_entity_meters(
    virtual_entity,
    resources: list,
    *,
    logger=_LOGGER,
) -> list[MeterPlan]:
    """Build canonical meter plans for a virtual entity."""
    plans: list[MeterPlan] = []
    canonical_resources = select_canonical_resources(
        virtual_entity, resources, logger=logger
    )

    for (_, supply), selected_resources in canonical_resources.items():
        if selected_resources.usage is None:
            if (
                selected_resources.cost is not None
                or selected_resources.export is not None
            ):
                logger.warning(
                    "Skipping %s secondary sensors for virtual entity %s (%s): no canonical %s usage resource was selected",
                    supply,
                    virtual_entity.name or "Unnamed",
                    virtual_entity.id,
                    supply,
                )
            continue

        plans.append(
            MeterPlan(
                virtual_entity=virtual_entity,
                supply=supply,
                usage_resource=selected_resources.usage,
                cost_resource=selected_resources.cost,
                export_resource=selected_resources.export,
            )
        )

    return plans
