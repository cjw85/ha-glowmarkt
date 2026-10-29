"""Pure, conservative reconciliation of Glow interval data and recorder sums."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite

HOUR = timedelta(hours=1)
HALF_HOUR = timedelta(minutes=30)


class IncompleteHistory(ValueError):
    """The requested update cannot safely reconstruct cumulative history."""


def complete_hour_totals(readings, value_scale=1.0):
    """Use UTC keys so the two autumn 01:00 hours remain distinct.

    A single half-hour is not a complete hour. Keep any old hourly value until
    both halves arrive. Duplicate identical buckets are harmless; conflicts fail.
    """
    buckets = {}
    for when, value in readings:
        if when.tzinfo is None:
            raise IncompleteHistory("Naive reading timestamp")
        when = when.astimezone(timezone.utc)
        if when.minute not in (0, 30) or when.second or when.microsecond:
            raise IncompleteHistory("Reading is not on a half-hour boundary")
        amount = float(value.value) * value_scale
        if not isfinite(amount) or amount < 0:
            raise IncompleteHistory("Invalid interval amount")
        if when in buckets and buckets[when] != amount:
            raise IncompleteHistory("Conflicting duplicate half-hour")
        buckets[when] = amount
    return {
        when: round(amount + buckets[when + HALF_HOUR], 6)
        for when, amount in sorted(buckets.items())
        if when.minute == 0 and when + HALF_HOUR in buckets
    }


def reconcile_statistics(readings, existing_rows, value_scale=1.0):
    """Replace complete source hours and rebase every later stored row.

    Existing rows must include the entire series, not just the refresh window.
    No caller may publish a prefix until this whole operation succeeds.
    Missing hours and corrupt old deltas are never silently replaced by zero.
    """
    incoming = complete_hour_totals(readings, value_scale)
    if not incoming:
        return []
    old = {}
    for row in existing_rows:
        start = row["start"]
        if isinstance(start, (int, float)):
            start = datetime.fromtimestamp(start, timezone.utc)
        if start.tzinfo is None:
            raise IncompleteHistory("Naive stored timestamp")
        start = start.astimezone(timezone.utc)
        total = row.get("sum")
        if total is None or not isfinite(float(total)):
            raise IncompleteHistory("Missing stored cumulative sum")
        old[start] = float(total)

    first = min(incoming)
    preceding = [when for when in old if when < first]
    baseline_time = max(preceding) if preceding else None
    baseline = old[baseline_time] if baseline_time is not None else 0.0
    if baseline_time is not None and baseline_time + HOUR != first:
        raise IncompleteHistory("Source does not bridge the gap after the baseline")

    # Derive retained hourly amounts BEFORE replacing any sums. A gap between
    # old rows cannot be interpreted as one hour's consumption.
    old_amounts = {}
    previous = None
    for when in sorted(old):
        if previous is not None and when - previous == HOUR:
            delta = round(old[when] - old[previous], 6)
            if delta >= 0:
                old_amounts[when] = delta
        previous = when

    last = max(max(incoming), max(old) if old else first)
    total = baseline
    result = []
    when = first
    while when <= last:
        if when in incoming:
            amount = incoming[when]
        elif when in old_amounts:
            amount = old_amounts[when]
        else:
            raise IncompleteHistory(f"Need source readings for hour {when.isoformat()}")
        total = round(total + amount, 6)
        result.append({"start": when, "sum": total})
        when += HOUR
    return result
