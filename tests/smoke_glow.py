#!/usr/bin/env python3
"""Smoke test the live Glowmarkt API using the integration's selection policy."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

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


def _run_resource_smoke(
    history_module,
    resource,
    *,
    history_days: int,
    show_buckets: bool,
) -> int:
    """Fetch historical PT30M readings for one Glow resource."""
    failures = 0
    try:
        end = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
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
    hourly_statistics = (
        history_module._aggregate_half_hour_readings_to_hourly_statistics(readings)
    )
    if hourly_statistics:
        print(
            "      hourly statistics: "
            f"{len(hourly_statistics)} row(s), latest cumulative sum "
            f"{hourly_statistics[-1]['sum']} kWh at "
            f"{hourly_statistics[-1]['start'].isoformat()}"
        )
    else:
        print("      hourly statistics: no importable hourly rows derived")
        failures += 1
    return failures


def main():
    """Run a live smoke test against the Glowmarkt API."""
    parser = argparse.ArgumentParser(
        description=(
            "Authenticate to Glowmarkt, apply the integration's canonical resource "
            "selection logic, and fetch historical PT30M electricity import/export "
            "and gas usage readings plus the derived hourly statistics shape used "
            "for HA import."
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

        electricity = canonical_resources[
            (virtual_entity.id, mapping.ELECTRICITY_SUPPLY)
        ]
        print("  Canonical electricity selection:")
        if electricity.usage is None:
            print("    import: no canonical import resource selected")
            if electricity.export is not None:
                print(
                    "    export: present but skipped because no canonical import "
                    "resource was selected"
                )
        else:
            for role_name, resource in (
                ("import", electricity.usage),
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
                    history_days=args.history_days,
                    show_buckets=args.show_buckets,
                )

        gas = canonical_resources[(virtual_entity.id, mapping.GAS_SUPPLY)]
        print("  Canonical gas selection:")
        if gas.usage is None:
            print("    usage: no canonical gas resource selected")
            continue

        selected_count += 1
        print(f"    usage: {_resource_summary(gas.usage)}")
        failures += _run_resource_smoke(
            history,
            gas.usage,
            history_days=args.history_days,
            show_buckets=args.show_buckets,
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
