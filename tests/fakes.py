"""Small fake Glow objects for tests."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from types import SimpleNamespace

import requests


def fake_reading(when: dt.datetime, value: float, unit: str = "kWh"):
    """Create one reading row in the shape expected by the integration."""
    return [when, SimpleNamespace(value=value, unit=lambda: unit)]


def http_error(status_code: int, text: str = "") -> requests.exceptions.HTTPError:
    """Build a requests HTTPError with a minimal response object attached."""
    response = SimpleNamespace(status_code=status_code, text=text)
    return requests.exceptions.HTTPError(
        text or f"HTTP {status_code}", response=response
    )


@dataclass(slots=True)
class FakeGlowResource:
    """A minimal Glow resource used in tests."""

    id: str
    classifier: str
    description: str
    name: str | None = None
    base_unit: str = "kWh"
    daily_total: float | None = None
    standing_charge_pence: float = 47.9
    rate_pence: float = 24.5
    catchup_valid: bool = True
    last_time: dt.datetime | None = None
    catchup_calls: int = field(default=0, init=False)
    last_time_calls: int = field(default=0, init=False)

    @property
    def is_dcc_sourced(self) -> bool:
        """Return whether the fake resource should be treated as DCC-backed."""
        text = " ".join(filter(None, [self.name, self.description])).lower()
        return "dcc" in text or "profile read" in text

    def get_tariff(self):
        """Return a tariff payload in the shape expected by the sensor code."""
        return SimpleNamespace(
            current_rates=SimpleNamespace(
                standing_charge=SimpleNamespace(value=self.standing_charge_pence),
                rate=SimpleNamespace(value=self.rate_pence),
            )
        )

    def catch_up(self):
        """Return a minimal catchup payload and track invocations."""
        self.catchup_calls += 1
        return SimpleNamespace(data=SimpleNamespace(valid=self.catchup_valid))

    def get_last_time(self):
        """Return the configured latest-available reading timestamp."""
        self.last_time_calls += 1
        return self.last_time


@dataclass(slots=True)
class FakeGlowVirtualEntity:
    """A minimal virtual entity with attached resources."""

    id: str
    name: str
    resources: list[FakeGlowResource] = field(default_factory=list)
    postal_code: str | None = "EN20"

    def list_resources(self) -> list[FakeGlowResource]:
        """Return the resources attached to this virtual entity."""
        return list(self.resources)


@dataclass(slots=True)
class FakeGlowClient:
    """A minimal Glow client with a fixed set of virtual entities."""

    virtual_entities: list[FakeGlowVirtualEntity]
    url: str = "https://example.test/api/v0-1/"

    def list_virtual_entities(self) -> list[FakeGlowVirtualEntity]:
        """Return the configured virtual entities."""
        return list(self.virtual_entities)
