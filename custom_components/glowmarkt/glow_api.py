"""Spec-first Glowmarkt API client used by the integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from types import SimpleNamespace
from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.glowmarkt.com/api/v0-1/"
# Bright individual-user API spec shows this applicationId in the documented
# auth and virtual-entity request examples.
DEFAULT_APPLICATION_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"
DEFAULT_REQUEST_TIMEOUT = 30
_REQUEST_SPLIT_EPSILON = timedelta(seconds=1)


def _snake_case(value: str) -> str:
    """Convert Glow payload keys into snake_case attributes."""
    value = value.replace("-", "_")
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return value.lower()


def _normalize_value(value: Any) -> Any:
    """Recursively normalize Glow payload keys."""
    if isinstance(value, dict):
        return {_snake_case(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def _namespace(value: Any) -> Any:
    """Convert nested dictionaries into attribute-access objects."""
    normalized = _normalize_value(value)
    if isinstance(normalized, dict):
        return SimpleNamespace(
            **{key: _namespace(item) for key, item in normalized.items()}
        )
    if isinstance(normalized, list):
        return [_namespace(item) for item in normalized]
    return normalized


def _tariff_value_object(value: Any) -> Any:
    """Wrap scalar tariff values in the nested structure the integration expects."""
    if value is None or isinstance(value, dict):
        return value
    return {"value": value}


def _canonical_tariff_payload(value: Any) -> Any:
    """Lift Glow tariff variants into one stable top-level current_rates shape."""
    normalized = _normalize_value(value)
    if not isinstance(normalized, dict):
        return normalized

    current_rates = normalized.get("current_rates")
    if not isinstance(current_rates, dict):
        for item in normalized.get("data", []):
            if isinstance(item, dict) and isinstance(item.get("current_rates"), dict):
                current_rates = item["current_rates"]
                break

    if not isinstance(current_rates, dict):
        return normalized

    canonical_current_rates = dict(current_rates)
    for key in ("standing_charge", "rate"):
        if key in canonical_current_rates:
            canonical_current_rates[key] = _tariff_value_object(
                canonical_current_rates[key]
            )

    canonical_payload = dict(normalized)
    canonical_payload["current_rates"] = canonical_current_rates
    return canonical_payload


def _coerce_glow_datetime(value: date | datetime) -> datetime:
    """Return a datetime suitable for Glow request parameter formatting."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    raise TypeError("Glow reading bounds must be date or datetime instances")


def glow_request_offset_minutes(value: date | datetime) -> int:
    """Return the Glow API offset in minutes for a local date/datetime."""
    when = _coerce_glow_datetime(value)
    offset = when.utcoffset()
    if offset is None:
        return 0
    return -int(offset.total_seconds() / 60)


def _glow_time_string(value: date | datetime) -> str:
    """Return the local wall-clock timestamp string expected by Glow."""
    when = _coerce_glow_datetime(value)
    if when.tzinfo is not None:
        when = when.replace(tzinfo=None)
    return when.isoformat()


def _glow_response_timezone(offset_minutes: int) -> timezone:
    """Return the fixed local timezone represented by a Glow offset value."""
    return timezone(-timedelta(minutes=offset_minutes))


def _epoch_to_utc_datetime(value: int | float | None) -> datetime | None:
    """Convert a Glow epoch timestamp to a UTC datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _iso_datetime_string_to_datetime(value: str) -> datetime:
    """Convert a Glow ISO-like timestamp string to a timezone-aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _coerce_reading_value(value: Any) -> float | int | str:
    """Convert a Glow reading value to a numeric type when possible."""
    if isinstance(value, (float, int)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _local_day_boundary_after(value: datetime) -> datetime:
    """Return the next local midnight after a timezone-aware datetime."""
    return (value + timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _find_offset_transition(start: datetime, end: datetime) -> datetime | None:
    """Return the first instant in a range where the UTC offset changes."""
    start_offset = start.utcoffset()
    end_offset = end.utcoffset()
    if start_offset is None or end_offset is None or start_offset == end_offset:
        return None

    local_timezone = start.tzinfo
    low = start.astimezone(timezone.utc)
    high = end.astimezone(timezone.utc)
    while high - low > timedelta(seconds=1):
        midpoint = low + (high - low) / 2
        if midpoint.astimezone(local_timezone).utcoffset() == start_offset:
            low = midpoint
        else:
            high = midpoint

    return high.astimezone(local_timezone).replace(microsecond=0)


def _iter_glow_request_windows(
    start: datetime,
    end: datetime,
):
    """Yield request windows split only where the local UTC offset changes."""
    if start >= end:
        return

    if start.utcoffset() is None or end.utcoffset() is None:
        yield start, end
        return

    window_start = start
    cursor = start
    current_offset = start.utcoffset()

    while cursor < end:
        next_boundary = min(_local_day_boundary_after(cursor), end)
        if next_boundary.utcoffset() == current_offset:
            cursor = next_boundary
            continue

        transition = _find_offset_transition(cursor, next_boundary)
        if transition is None or transition <= cursor:
            cursor = next_boundary
            current_offset = cursor.utcoffset()
            continue

        window_end = (
            transition.astimezone(timezone.utc) - _REQUEST_SPLIT_EPSILON
        ).astimezone(transition.tzinfo)
        if window_end >= window_start:
            yield window_start, window_end

        window_start = transition
        cursor = transition
        current_offset = transition.utcoffset()

    if window_start <= end:
        yield window_start, end


def decode_glow_reading_timestamp(
    timestamp: int | float, offset_minutes: int
) -> datetime:
    """Decode a Glow reading timestamp as a local wall-clock bucket boundary."""
    wall_clock = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return wall_clock.replace(tzinfo=_glow_response_timezone(offset_minutes))


def _truncate_body(response) -> str:
    """Return a short response body for error reporting."""
    body = getattr(response, "text", "").strip()
    if len(body) > 300:
        body = f"{body[:300]}..."
    return body or "empty response"


def _raise_http_error(method: str, url: str, response) -> None:
    """Raise a requests HTTPError with the response attached."""
    message = (
        f"{method} {url} failed with status {response.status_code}: "
        f"{_truncate_body(response)}"
    )
    raise requests.exceptions.HTTPError(message, response=response)


def _extract_postal_code(payload: dict[str, Any]) -> str | None:
    """Best-effort extraction of a virtual entity postal code."""
    candidates = (
        payload.get("postalCode"),
        payload.get("postal_code"),
        payload.get("postcode"),
        payload.get("postCode"),
    )
    for candidate in candidates:
        if candidate:
            return candidate

    address = payload.get("address")
    if isinstance(address, dict):
        for key in ("postalCode", "postal_code", "postcode", "postCode"):
            if address.get(key):
                return address[key]
    return None


def _resource_source_text(resource) -> str:
    """Return lower-cased resource text for source heuristics."""
    return " ".join(
        filter(
            None,
            [getattr(resource, "name", None), getattr(resource, "description", None)],
        )
    ).lower()


@dataclass(slots=True)
class GlowReadingValue:
    """One Glow reading value with a Home Assistant-friendly unit method."""

    value: float | int | str
    unit_name: str

    def unit(self) -> str:
        """Return the unit name for a reading value."""
        return self.unit_name


class GlowClient:
    """Minimal Glowmarkt client for the Bright individual-user API."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        application: str = DEFAULT_APPLICATION_ID,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.username = username
        self.password = password
        self.url = base_url if base_url.endswith("/") else f"{base_url}/"
        self.application = application
        self.session = session or requests.Session()
        self.timeout = timeout
        self.token: str | None = None
        self.authenticate()

    def authenticate(self) -> None:
        """Authenticate against Glow and store the returned token."""
        headers = {
            "Content-Type": "application/json",
            "applicationId": self.application,
        }
        payload = {"username": self.username, "password": self.password}
        url = f"{self.url}auth"
        response = self.session.post(
            url,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code != 200:
            _raise_http_error("POST", url, response)

        body = response.json()
        self.token = body.get("token")
        if not self.token:
            raise ValueError("Glow authentication response did not include a token")

    def _headers(self, *, auth_required: bool) -> dict[str, str]:
        """Build common Glow request headers."""
        headers = {
            "Content-Type": "application/json",
            "applicationId": self.application,
        }
        if auth_required:
            if not self.token:
                raise ValueError("Glow client is not authenticated")
            headers["token"] = self.token
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth_required: bool = True,
        allow_empty: bool = False,
        allow_reauth: bool = True,
    ) -> Any:
        """Make one Glow API request and decode its JSON payload."""
        url = f"{self.url}{path.lstrip('/')}"
        response = self.session.request(
            method,
            url,
            headers=self._headers(auth_required=auth_required),
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        if auth_required and response.status_code in (401, 403) and allow_reauth:
            self.authenticate()
            return self._request_json(
                method,
                path,
                params=params,
                json_body=json_body,
                auth_required=auth_required,
                allow_empty=allow_empty,
                allow_reauth=False,
            )
        if response.status_code != 200:
            _raise_http_error(method, url, response)

        if allow_empty and not getattr(response, "text", "").strip():
            return {}

        try:
            return response.json()
        except ValueError:
            if allow_empty:
                return {}
            raise

    def list_virtual_entities(self) -> list["GlowVirtualEntity"]:
        """Fetch all virtual entities visible to the authenticated user."""
        payload = self._request_json("GET", "virtualentity")
        return [GlowVirtualEntity(self, item) for item in payload or []]


class GlowVirtualEntity:
    """One Glow virtual entity, with lazy resource expansion."""

    def __init__(self, client: GlowClient, payload: dict[str, Any]) -> None:
        self.client = client
        self.id = payload.get("veId") or payload.get("ve_id") or payload.get("id")
        self.name = payload.get("name")
        self.postal_code = _extract_postal_code(payload)
        self._resources: list[GlowResource] | None = None

        embedded_resources = payload.get("resources") or []
        if embedded_resources and all(
            isinstance(resource, dict)
            and (
                "classifier" in resource
                or "baseUnit" in resource
                or "base_unit" in resource
            )
            for resource in embedded_resources
        ):
            self._resources = [
                GlowResource(self.client, self, resource)
                for resource in embedded_resources
            ]

    def list_resources(self) -> list["GlowResource"]:
        """Fetch full resource definitions for this virtual entity."""
        if self._resources is None:
            payload = self.client._request_json(
                "GET", f"virtualentity/{self.id}/resources"
            )
            resources = (
                payload.get("resources", payload)
                if isinstance(payload, dict)
                else payload
            )
            self._resources = [
                GlowResource(self.client, self, resource)
                for resource in resources or []
            ]
        return list(self._resources)


class GlowResource:
    """A single Glow resource."""

    def __init__(
        self,
        client: GlowClient,
        virtual_entity: GlowVirtualEntity | None,
        payload: dict[str, Any],
    ) -> None:
        self.client = client
        self.virtual_entity = virtual_entity
        self.id = (
            payload.get("resourceId") or payload.get("resource_id") or payload.get("id")
        )
        self.name = payload.get("name")
        self.classifier = payload.get("classifier")
        self.description = payload.get("description")
        self.base_unit = payload.get("baseUnit") or payload.get("base_unit")

    @property
    def is_dcc_sourced(self) -> bool:
        """Return whether this resource looks DCC-backed."""
        text = _resource_source_text(self)
        return "dcc" in text or "profile read" in text

    def _resource_json(self, suffix: str):
        """Fetch and normalize a resource-specific JSON document."""
        return self.client._request_json("GET", f"resource/{self.id}/{suffix}")

    def _resource_timestamp(self, suffix: str, field_name: str) -> datetime | None:
        """Fetch a resource timestamp endpoint and decode the epoch field."""
        payload = self._resource_json(suffix)
        data = payload.get("data") or {}
        return _epoch_to_utc_datetime(data.get(field_name))

    def get_current(self):
        """Fetch the current reading payload for this resource."""
        return _namespace(self._resource_json("current"))

    def get_first_time(self) -> datetime | None:
        """Fetch the first available reading timestamp for this resource."""
        return self._resource_timestamp("first-time", "firstTs")

    def get_last_time(self) -> datetime | None:
        """Fetch the most recent available reading timestamp for this resource."""
        return self._resource_timestamp("last-time", "lastTs")

    def get_meter_reading(self):
        """Fetch the resource's cumulative meter reading payload."""
        return _namespace(self._resource_json("meterread"))

    def get_tariff(self):
        """Fetch the current tariff document for this resource."""
        return _namespace(_canonical_tariff_payload(self._resource_json("tariff")))

    def get_tariff_list(self):
        """Fetch the resource's tariff-history payload."""
        return _namespace(self._resource_json("tariff-list"))

    def catch_up(self):
        """Trigger one DCC catch-up request for this resource."""
        return _namespace(self._resource_json("catchup"))

    def get_daily_consumption_log(self):
        """Fetch the DCC daily consumption log for this resource."""
        payload = self._resource_json("daily-consumption-log")
        unit_name = payload.get("units") or getattr(self, "base_unit", "unknown")
        return [
            [
                _iso_datetime_string_to_datetime(timestamp),
                GlowReadingValue(_coerce_reading_value(value), unit_name),
            ]
            for timestamp, value in payload.get("data", [])
        ]

    def get_readings(
        self,
        t_from: date | datetime,
        t_to: date | datetime,
        period: str,
        func: str = "sum",
        nulls: bool = False,
    ):
        """Fetch time-series readings for this resource."""
        return get_resource_readings(
            self,
            t_from,
            t_to,
            period,
            func,
            nulls,
        )


def get_resource_readings(
    resource,
    t_from: date | datetime,
    t_to: date | datetime,
    period: str,
    func: str = "sum",
    nulls: bool = False,
):
    """Fetch readings directly from Glow with the correct local offset."""
    client = resource.client
    from_dt = _coerce_glow_datetime(t_from)
    to_dt = _coerce_glow_datetime(t_to)
    resource_id = resource.id
    unit_name = getattr(resource, "base_unit", "unknown")
    readings = []

    for window_start, window_end in _iter_glow_request_windows(from_dt, to_dt):
        window_offset = glow_request_offset_minutes(window_end)
        params = {
            "from": _glow_time_string(window_start),
            "to": _glow_time_string(window_end),
            "period": period,
            "offset": window_offset,
            "function": func,
            "nulls": 1 if nulls else 0,
        }

        payload = client._request_json(
            "GET", f"resource/{resource_id}/readings", params=params
        )

        unit_name = payload.get("units") or unit_name
        readings.extend(
            [
                [
                    decode_glow_reading_timestamp(timestamp, window_offset),
                    GlowReadingValue(value, unit_name),
                ]
                for timestamp, value in payload.get("data", [])
                if value is not None
            ]
        )

    return sorted(readings, key=lambda reading: reading[0])
