#!/usr/bin/env python3
"""Smoke test the live Glowmarkt API using the integration's selection policy."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.glowmarkt import history, mapping
from custom_components.glowmarkt.glow_api import GlowClient


def _resource_summary(resource) -> str:
    """Return a readable one-line summary for a Glow resource."""
    return (
        f"{resource.id} | {resource.classifier} | "
        f"{resource.description} | unit={resource.base_unit}"
    )


def _reading_total(readings) -> float:
    """Sum the reading values returned by the internal Glow client."""
    return round(
        sum(reading[1].value for reading in readings if reading[1].value is not None), 3
    )


def _print_readings(prefix: str, readings, *, show_buckets: bool) -> None:
    """Print a compact summary of PT30M readings."""
    if not readings:
        print(f"{prefix}history: no readings returned")
        return

    total = _reading_total(readings)
    unit = readings[0][1].unit()
    first_bucket_start = readings[0][0]
    last_bucket_start = readings[-1][0]
    print(
        f"{prefix}history: {total} {unit} from {len(readings)} half-hour bucket(s) "
        f"with bucket starts from {first_bucket_start.isoformat()} through "
        f"{last_bucket_start.isoformat()}"
    )
    if show_buckets:
        for when, value in readings:
            print(f"{prefix}  - {when.isoformat()} -> {value.value} {value.unit()}")


def _print_recent_readings(
    prefix: str,
    readings,
    *,
    end: datetime,
    recent_hours: int,
) -> None:
    """Print half-hour buckets from the most recent N-hour window."""
    if recent_hours <= 0:
        return

    recent_start = end - timedelta(hours=recent_hours)
    recent_readings = [reading for reading in readings if reading[0] >= recent_start]
    if not recent_readings:
        print(
            f"{prefix}last {recent_hours}h buckets: none returned in "
            f"{recent_start.isoformat()} to {end.isoformat()}"
        )
        return

    recent_total = _reading_total(recent_readings)
    unit = recent_readings[0][1].unit()
    print(
        f"{prefix}last {recent_hours}h buckets: {recent_total} {unit} from "
        f"{len(recent_readings)} bucket(s) between "
        f"{recent_start.isoformat()} and {end.isoformat()}"
    )
    for when, value in recent_readings:
        bucket_end = when + timedelta(minutes=30)
        print(
            f"{prefix}  - {when.isoformat()} to {bucket_end.isoformat()} "
            f"-> {value.value} {value.unit()}"
        )


def _run_catchup_smoke(resource, *, dcc_context_resource=None) -> int:
    """Trigger one Glow DCC catchup request for a resource."""
    refresh_resource = dcc_context_resource
    if refresh_resource is None and getattr(resource, "is_dcc_sourced", False):
        refresh_resource = resource

    if refresh_resource is None:
        print("      catchup: skipped (resource is not identified as DCC-backed)")
        return 0

    try:
        result = refresh_resource.catch_up()
    except Exception as ex:  # pylint: disable=broad-except
        print(f"      catchup failed: {type(ex).__name__}: {ex}")
        return 1

    valid = getattr(getattr(result, "data", None), "valid", None)
    print(f"      catchup: requested (valid={valid})")
    return 1 if valid is False else 0


def _run_resource_smoke(
    history_module,
    resource,
    *,
    catchup: bool,
    dcc_context_resource,
    history_days: int,
    show_buckets: bool,
    recent_hours: int,
    statistic_unit: str | None = None,
    value_scale: float = 1.0,
) -> int:
    """Fetch historical PT30M readings for one Glow resource."""
    failures = 0
    if catchup:
        failures += _run_catchup_smoke(
            resource, dcc_context_resource=dcc_context_resource
        )

    try:
        end = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
        refresh_resource = dcc_context_resource
        if refresh_resource is None and getattr(resource, "is_dcc_sourced", False):
            refresh_resource = resource
        if refresh_resource is not None:
            if refresh_resource.id != resource.id:
                print(
                    "      freshness context: "
                    f"{refresh_resource.id} | {refresh_resource.classifier}"
                )
            last_time = refresh_resource.get_last_time()
            print(
                "      last-time: "
                f"{last_time.isoformat() if last_time is not None else 'none'}"
            )
            useful_end = history_module._useful_history_end(end, last_time)
            if useful_end != end:
                print(
                    "      useful history end: "
                    f"{useful_end.isoformat()} "
                    f"(requested end {end.isoformat()})"
                )
            end = useful_end
        start = end - timedelta(days=history_days)
        print(
            "      query window: "
            f"{start.isoformat()} to {end.isoformat()} (exclusive end)"
        )
        readings = history_module._fetch_half_hour_history(
            resource,
            start,
            end,
            chunk_days=min(history_days, history_module._HISTORY_CHUNK_DAYS) or 1,
        )
    except Exception as ex:  # pylint: disable=broad-except
        print(f"      readings failed: {type(ex).__name__}: {ex}")
        return failures + 1

    _print_readings("      ", readings, show_buckets=show_buckets)
    _print_recent_readings(
        "      ",
        readings,
        end=end,
        recent_hours=recent_hours,
    )
    hourly_statistics = (
        history_module._aggregate_half_hour_readings_to_hourly_statistics(
            readings,
            value_scale=value_scale,
        )
    )
    if hourly_statistics:
        statistic_unit = statistic_unit or (
            readings[0][1].unit() if readings else getattr(resource, "base_unit", "")
        )
        print(
            "      hourly statistics for HA: "
            f"{len(hourly_statistics)} row(s), latest cumulative sum "
            f"{hourly_statistics[-1]['sum']} {statistic_unit} at "
            f"{hourly_statistics[-1]['start'].isoformat()}"
        )
    else:
        print("      hourly statistics for HA: no importable hourly rows derived")
        failures += 1
    return failures


def main():
    """Run a live smoke test against the Glowmarkt API."""
    parser = argparse.ArgumentParser(
        description=(
            "Authenticate to Glowmarkt, apply the integration's canonical resource "
            "selection logic, and fetch historical PT30M electricity import/export "
            "and cost readings plus the derived hourly statistics shape used for "
            "HA import. Optionally trigger one DCC catchup request per selected "
            "resource before fetching history."
        )
    )
    parser.add_argument("--username", "-u", required=True, help="Bright username")
    parser.add_argument("--password", "-p", required=True, help="Bright password")
    parser.add_argument(
        "--history-days",
        type=int,
        default=7,
        help="How many days of PT30M history to request for each selected resource.",
    )
    parser.add_argument(
        "--show-all-resources",
        action="store_true",
        help="Print every resource returned for each virtual entity before selection.",
    )
    parser.add_argument(
        "--show-buckets",
        action="store_true",
        help="Print every half-hour bucket returned by Glow.",
    )
    parser.add_argument(
        "--show-last-hours",
        type=int,
        default=0,
        help=(
            "Print half-hour buckets from the most recent N-hour window within the "
            "requested history span. Use 24 to inspect the latest day."
        ),
    )
    parser.add_argument(
        "--catchup",
        action="store_true",
        help=(
            "Trigger one DCC catchup request for each selected canonical resource "
            "before requesting half-hour history."
        ),
    )
    args = parser.parse_args()

    client = GlowClient(args.username, args.password)
    virtual_entities = client.list_virtual_entities()

    if not virtual_entities:
        print("No virtual entities returned by Glowmarkt.")
        return 1

    failures = 0
    selected_count = 0

    for virtual_entity in virtual_entities:
        postal_code = getattr(virtual_entity, "postal_code", None) or "unknown"
        print(
            f"Virtual entity: {virtual_entity.name or 'Unnamed'} | "
            f"id={virtual_entity.id} | postal_code={postal_code}"
        )

        resources = virtual_entity.list_resources()
        if args.show_all_resources:
            print("  All resources:")
            for resource in resources:
                print(f"    - {_resource_summary(resource)}")

        canonical_resources = mapping.select_canonical_resources(
            virtual_entity, resources
        )
        plans_by_supply = {
            plan.supply: plan
            for plan in mapping.plan_virtual_entity_meters(virtual_entity, resources)
        }
        electricity_plan = plans_by_supply.get(mapping.ELECTRICITY_SUPPLY)
        gas_plan = plans_by_supply.get(mapping.GAS_SUPPLY)

        electricity = canonical_resources[
            (virtual_entity.id, mapping.ELECTRICITY_SUPPLY)
        ]
        print("  Canonical electricity selection:")
        if electricity.usage is None:
            print("    import: no canonical import resource selected")
            if electricity.cost is not None:
                print(
                    "    cost: present but skipped because no canonical import "
                    "resource was selected"
                )
            if electricity.export is not None:
                print(
                    "    export: present but skipped because no canonical import "
                    "resource was selected"
                )
        else:
            for role_name, resource in (
                ("import", electricity.usage),
                ("cost", electricity.cost),
                ("export", electricity.export),
            ):
                if resource is None:
                    print(f"    {role_name}: not selected")
                    continue

                selected_count += 1
                print(f"    {role_name}: {_resource_summary(resource)}")
                failures += _run_resource_smoke(
                    history,
                    resource,
                    catchup=args.catchup,
                    dcc_context_resource=(
                        mapping.dcc_context_resource(electricity_plan)
                        if electricity_plan is not None
                        else None
                    ),
                    history_days=args.history_days,
                    show_buckets=args.show_buckets,
                    recent_hours=args.show_last_hours,
                    statistic_unit="GBP" if role_name == "cost" else "kWh",
                    value_scale=0.01 if role_name == "cost" else 1.0,
                )

        gas = canonical_resources[(virtual_entity.id, mapping.GAS_SUPPLY)]
        print("  Canonical gas selection:")
        if gas.usage is None:
            print("    usage: no canonical gas resource selected")
            if gas.cost is not None:
                print(
                    "    cost: present but skipped because no canonical gas "
                    "resource was selected"
                )
            continue

        for role_name, resource in (("usage", gas.usage), ("cost", gas.cost)):
            if resource is None:
                print(f"    {role_name}: not selected")
                continue

            selected_count += 1
            print(f"    {role_name}: {_resource_summary(resource)}")
            failures += _run_resource_smoke(
                history,
                resource,
                catchup=args.catchup,
                dcc_context_resource=(
                    mapping.dcc_context_resource(gas_plan)
                    if gas_plan is not None
                    else None
                ),
                history_days=args.history_days,
                show_buckets=args.show_buckets,
                recent_hours=args.show_last_hours,
                statistic_unit="GBP" if role_name == "cost" else "kWh",
                value_scale=0.01 if role_name == "cost" else 1.0,
            )

    if selected_count == 0:
        print("No canonical resources were selected.")
        return 1

    if failures:
        print(f"Smoke test completed with {failures} failure(s).")
        return 1

    print("Smoke test completed without request failures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
