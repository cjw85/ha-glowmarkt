"""Tests for the internal Glow API client."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from custom_components.glowmarkt import glow_api


class FakeResponse:
    """Minimal fake requests response."""

    def __init__(self, status_code: int, payload=None, text: str | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else ""

    def json(self):
        return self._payload


class FakeSession:
    """Queued fake requests session."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def _next(self) -> FakeResponse:
        if not self.responses:
            raise AssertionError("No fake responses left in session queue")
        return self.responses.pop(0)

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return self._next()

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
                "timeout": timeout,
            }
        )
        return self._next()


def test_authenticates_and_lists_virtual_entities_and_resources() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "token": "token-123",
                },
            ),
            FakeResponse(
                200,
                [
                    {
                        "veId": "ve-1",
                        "name": "Site 1",
                        "postalCode": "EN20",
                        "resources": [
                            {
                                "resourceId": "resource-summary-1",
                            }
                        ],
                    }
                ],
            ),
            FakeResponse(
                200,
                {
                    "veId": "ve-1",
                    "resources": [
                        {
                            "resourceId": "resource-1",
                            "name": "electricity",
                            "classifier": "electricity.consumption",
                            "description": "electricity consumption",
                            "baseUnit": "kWh",
                        }
                    ],
                },
            ),
        ]
    )
    client = glow_api.GlowClient(
        "user@example.com",
        "secret",
        base_url="https://example.test/api/v0-1/",
        session=session,
    )

    entities = client.list_virtual_entities()
    resources = entities[0].list_resources()

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == "https://example.test/api/v0-1/auth"
    assert (
        session.calls[0]["headers"]["applicationId"] == glow_api.DEFAULT_APPLICATION_ID
    )
    assert session.calls[0]["json"] == {
        "username": "user@example.com",
        "password": "secret",
    }
    assert session.calls[0]["timeout"] == glow_api.DEFAULT_REQUEST_TIMEOUT

    assert len(entities) == 1
    assert entities[0].id == "ve-1"
    assert entities[0].postal_code == "EN20"
    assert resources[0].id == "resource-1"
    assert resources[0].classifier == "electricity.consumption"
    assert resources[0].base_unit == "kWh"


def test_reauthenticates_once_on_401() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"token": "token-1"}),
            FakeResponse(401, text='{"error":"expired token"}'),
            FakeResponse(200, {"token": "token-2"}),
            FakeResponse(200, []),
        ]
    )
    client = glow_api.GlowClient(
        "user@example.com",
        "secret",
        base_url="https://example.test/api/v0-1/",
        session=session,
    )

    entities = client.list_virtual_entities()

    assert entities == []
    assert client.token == "token-2"
    assert [call["method"] for call in session.calls] == ["POST", "GET", "POST", "GET"]


def test_resource_readings_use_spec_params_and_preserve_wall_clock() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"token": "token-123"}),
            FakeResponse(
                200,
                {
                    "units": "kWh",
                    "data": [
                        [
                            int(
                                dt.datetime(
                                    2026, 7, 5, 13, 30, tzinfo=dt.timezone.utc
                                ).timestamp()
                            ),
                            1.25,
                        ],
                        [
                            int(
                                dt.datetime(
                                    2026, 7, 5, 14, 0, tzinfo=dt.timezone.utc
                                ).timestamp()
                            ),
                            0.75,
                        ],
                    ],
                },
            ),
        ]
    )
    client = glow_api.GlowClient(
        "user@example.com",
        "secret",
        base_url="https://example.test/api/v0-1/",
        session=session,
    )
    resource = glow_api.GlowResource(
        client,
        None,
        {
            "resourceId": "resource-1",
            "name": "electricity",
            "classifier": "electricity.export",
            "description": "electricity export",
            "baseUnit": "kWh",
        },
    )

    timezone = dt.timezone(dt.timedelta(hours=1))
    start = dt.datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone)
    end = dt.datetime(2026, 7, 5, 10, 15, 30, tzinfo=timezone)
    readings = resource.get_readings(start, end, "PT30M", "sum")

    assert session.calls[1]["method"] == "GET"
    assert (
        session.calls[1]["url"]
        == "https://example.test/api/v0-1/resource/resource-1/readings"
    )
    assert session.calls[1]["params"]["from"] == "2026-07-05T00:00:00"
    assert session.calls[1]["params"]["to"] == "2026-07-05T10:15:30"
    assert session.calls[1]["params"]["offset"] == -60
    assert session.calls[1]["params"]["function"] == "sum"
    assert session.calls[1]["params"]["nulls"] == 0
    assert session.calls[1]["timeout"] == glow_api.DEFAULT_REQUEST_TIMEOUT

    assert readings[0][0] == dt.datetime(2026, 7, 5, 13, 30, tzinfo=timezone)
    assert readings[1][0] == dt.datetime(2026, 7, 5, 14, 0, tzinfo=timezone)
    assert readings[0][1].unit() == "kWh"


def test_resource_readings_split_at_dst_transition() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"token": "token-123"}),
            FakeResponse(
                200,
                {
                    "units": "kWh",
                    "data": [
                        [
                            int(
                                dt.datetime(
                                    2026, 3, 29, 0, 30, tzinfo=dt.timezone.utc
                                ).timestamp()
                            ),
                            0.5,
                        ]
                    ],
                },
            ),
            FakeResponse(
                200,
                {
                    "units": "kWh",
                    "data": [
                        [
                            int(
                                dt.datetime(
                                    2026, 3, 29, 2, 0, tzinfo=dt.timezone.utc
                                ).timestamp()
                            ),
                            0.75,
                        ]
                    ],
                },
            ),
        ]
    )
    client = glow_api.GlowClient(
        "user@example.com",
        "secret",
        base_url="https://example.test/api/v0-1/",
        session=session,
    )
    resource = glow_api.GlowResource(
        client,
        None,
        {
            "resourceId": "resource-1",
            "name": "electricity",
            "classifier": "electricity.export",
            "description": "electricity export",
            "baseUnit": "kWh",
        },
    )

    timezone = ZoneInfo("Europe/London")
    start = dt.datetime(2026, 3, 29, 0, 0, 0, tzinfo=timezone)
    end = dt.datetime(2026, 3, 29, 4, 0, 0, tzinfo=timezone)
    readings = resource.get_readings(start, end, "PT30M", "sum")

    assert len(session.calls) == 3
    assert session.calls[1]["params"]["from"] == "2026-03-29T00:00:00"
    assert session.calls[1]["params"]["to"] == "2026-03-29T00:59:59"
    assert session.calls[1]["params"]["offset"] == 0
    assert session.calls[2]["params"]["from"] == "2026-03-29T02:00:00"
    assert session.calls[2]["params"]["to"] == "2026-03-29T04:00:00"
    assert session.calls[2]["params"]["offset"] == -60

    assert readings == [
        [
            dt.datetime(2026, 3, 29, 0, 30, tzinfo=dt.timezone.utc),
            glow_api.GlowReadingValue(0.5, "kWh"),
        ],
        [
            dt.datetime(
                2026,
                3,
                29,
                2,
                0,
                tzinfo=dt.timezone(dt.timedelta(hours=1)),
            ),
            glow_api.GlowReadingValue(0.75, "kWh"),
        ],
    ]


def test_tariff_payload_is_normalized_to_snake_case_attributes() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"token": "token-123"}),
            FakeResponse(
                200,
                {
                    "currentRates": {
                        "standingCharge": {"value": 34.5},
                        "rate": {"value": 12.3},
                    }
                },
            ),
        ]
    )
    client = glow_api.GlowClient(
        "user@example.com",
        "secret",
        base_url="https://example.test/api/v0-1/",
        session=session,
    )
    resource = glow_api.GlowResource(
        client,
        None,
        {
            "resourceId": "resource-1",
            "name": "electricity",
            "classifier": "electricity.consumption",
            "description": "electricity consumption",
            "baseUnit": "kWh",
        },
    )

    tariff = resource.get_tariff()

    assert tariff.current_rates.standing_charge.value == 34.5
    assert tariff.current_rates.rate.value == 12.3


def test_additional_resource_endpoints_decode_timestamps_and_catchup() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"token": "token-123"}),
            FakeResponse(200, {"data": {"lastTs": 1751731200}}),
            FakeResponse(200, {"data": {"firstTs": 1751328000}}),
            FakeResponse(200, {"data": {"valid": True}}),
        ]
    )
    client = glow_api.GlowClient(
        "user@example.com",
        "secret",
        base_url="https://example.test/api/v0-1/",
        session=session,
    )
    resource = glow_api.GlowResource(
        client,
        None,
        {
            "resourceId": "resource-1",
            "name": "electricity",
            "classifier": "electricity.consumption",
            "description": "electricity consumption DCC SM profile reads",
            "baseUnit": "kWh",
        },
    )

    assert resource.is_dcc_sourced is True
    assert resource.get_last_time() == dt.datetime(
        2025, 7, 5, 16, 0, tzinfo=dt.timezone.utc
    )
    assert resource.get_first_time() == dt.datetime(
        2025, 7, 1, 0, 0, tzinfo=dt.timezone.utc
    )

    catchup = resource.catch_up()

    assert catchup.data.valid is True
    assert [call["url"] for call in session.calls[1:]] == [
        "https://example.test/api/v0-1/resource/resource-1/last-time",
        "https://example.test/api/v0-1/resource/resource-1/first-time",
        "https://example.test/api/v0-1/resource/resource-1/catchup",
    ]


def test_current_meter_and_tariff_list_payloads_are_normalized() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"token": "token-123"}),
            FakeResponse(
                200,
                {
                    "resourceTypeId": "type-1",
                    "data": [[1751731200, 321]],
                },
            ),
            FakeResponse(
                200,
                {
                    "units": "kWh",
                    "data": [[1751731200, 12345.6]],
                },
            ),
            FakeResponse(
                200,
                {
                    "data": [
                        {
                            "displayName": "Standard",
                            "effectiveDate": "2026-07-01 00:00:00",
                        }
                    ]
                },
            ),
        ]
    )
    client = glow_api.GlowClient(
        "user@example.com",
        "secret",
        base_url="https://example.test/api/v0-1/",
        session=session,
    )
    resource = glow_api.GlowResource(
        client,
        None,
        {
            "resourceId": "resource-1",
            "name": "electricity",
            "classifier": "electricity.consumption",
            "description": "electricity consumption",
            "baseUnit": "kWh",
        },
    )

    current = resource.get_current()
    meter_reading = resource.get_meter_reading()
    tariff_list = resource.get_tariff_list()

    assert current.resource_type_id == "type-1"
    assert current.data[0][1] == 321
    assert meter_reading.units == "kWh"
    assert meter_reading.data[0][1] == 12345.6
    assert tariff_list.data[0].display_name == "Standard"
    assert tariff_list.data[0].effective_date == "2026-07-01 00:00:00"


def test_daily_consumption_log_is_decoded() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"token": "token-123"}),
            FakeResponse(
                200,
                {
                    "units": "Wh",
                    "data": [["2026-07-04T00:00:00.00000Z", "7777.0"]],
                },
            ),
        ]
    )
    client = glow_api.GlowClient(
        "user@example.com",
        "secret",
        base_url="https://example.test/api/v0-1/",
        session=session,
    )
    resource = glow_api.GlowResource(
        client,
        None,
        {
            "resourceId": "resource-1",
            "name": "electricity",
            "classifier": "electricity.consumption",
            "description": "electricity consumption DCC SM profile reads",
            "baseUnit": "Wh",
        },
    )

    log_rows = resource.get_daily_consumption_log()

    assert log_rows == [
        [
            dt.datetime(2026, 7, 4, 0, 0, tzinfo=dt.timezone.utc),
            glow_api.GlowReadingValue(7777.0, "Wh"),
        ]
    ]
