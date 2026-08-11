"""Tests for the Open-Meteo client: canned JSON in, a Sample or nothing out."""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from arraysense.weather import open_meteo


def _payload(temp: object = 24.3, cloud: object = 60) -> bytes:
    return json.dumps({"current": {"temperature_2m": temp, "cloud_cover": cloud}}).encode()


def test_a_good_reply_becomes_a_weather_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        seen["url"] = url
        return _payload()

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    sample = open_meteo.fetch_current(35.2, -97.4)
    assert sample is not None
    assert sample.readings == {"outside_temperature_c": 24.3, "cloud_cover_pct": 60.0}
    assert sample.timestamp.tzinfo is not None
    assert "latitude=35.2" in seen["url"]
    assert "longitude=-97.4" in seen["url"]
    assert "temperature_2m" in seen["url"] and "cloud_cover" in seen["url"]


def test_a_network_failure_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, timeout: float) -> bytes:
        raise URLError("no route to host")

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    assert open_meteo.fetch_current(35.2, -97.4) is None


def test_malformed_json_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: b"<html>oops</html>")
    assert open_meteo.fetch_current(35.2, -97.4) is None


def test_a_reply_missing_the_block_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: b'{"elevation": 350}')
    assert open_meteo.fetch_current(35.2, -97.4) is None


def test_an_implausible_value_is_dropped_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    # 999 degrees is an API glitch, not weather. The registry bounds are the
    # plausibility rule, and since nothing validates at the store, the client
    # is where an implausible value becomes absent. The plausible half of the
    # reply is kept: a broken thermometer does not blind the cloud sensor.
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: _payload(temp=999))
    sample = open_meteo.fetch_current(35.2, -97.4)
    assert sample is not None
    assert sample.readings == {"cloud_cover_pct": 60.0}


def test_a_reply_with_nothing_plausible_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: _payload(temp=999, cloud=-5))
    assert open_meteo.fetch_current(35.2, -97.4) is None


def test_a_non_numeric_value_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: _payload(temp="warm"))
    sample = open_meteo.fetch_current(35.2, -97.4)
    assert sample is not None
    assert sample.readings == {"cloud_cover_pct": 60.0}


# -- fetch_radiation_forecast -------------------------------------------------
# The same Open-Meteo endpoint that serves current conditions also serves an
# hourly shortwave-radiation forecast. The function pairs each timestamp
# with its non-null, non-negative W/m² value and drops everything else.


def _forecast_payload(
    times: object | None = None,
    values: object | None = None,
) -> bytes:
    if times is None:
        times = [
            "2026-08-10T06:00",
            "2026-08-10T07:00",
            "2026-08-10T08:00",
            "2026-08-10T09:00",
        ]
    if values is None:
        values = [0.0, 150.3, 420.7, 680.1]
    return json.dumps({"hourly": {"time": times, "shortwave_radiation": values}}).encode()


def test_good_forecast_becomes_timed_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy reply returns (aware UTC datetime, W/m²) pairs for every hour."""
    seen: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        seen["url"] = url
        return _forecast_payload()

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    pairs = open_meteo.fetch_radiation_forecast(35.2, -97.4)
    assert pairs is not None
    assert len(pairs) == 4
    from datetime import UTC

    assert pairs[0][1] == 0.0
    assert pairs[1][1] == 150.3
    assert pairs[2][1] == 420.7
    assert pairs[3][1] == 680.1
    for ts, _ in pairs:
        assert ts.tzinfo is UTC
    assert "hourly=shortwave_radiation" in seen["url"]
    assert "forecast_days=2" in seen["url"]


def test_null_forecast_values_are_dropped_not_zeroed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null in the radiation column is absent, not a cloudy hour at 0 W/m²."""
    monkeypatch.setattr(
        open_meteo,
        "_http_get",
        lambda url, timeout: _forecast_payload(values=[0.0, None, 420.7, None]),
    )
    pairs = open_meteo.fetch_radiation_forecast(35.2, -97.4)
    assert pairs is not None
    assert len(pairs) == 2
    assert [v for _, v in pairs] == [0.0, 420.7]


def test_forecast_network_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, timeout: float) -> bytes:
        raise URLError("no route to host")

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    assert open_meteo.fetch_radiation_forecast(35.2, -97.4) is None


def test_malformed_forecast_json_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: b"<html>oops</html>")
    assert open_meteo.fetch_radiation_forecast(35.2, -97.4) is None


def test_forecast_missing_hourly_block_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: b'{"elevation": 350}')
    assert open_meteo.fetch_radiation_forecast(35.2, -97.4) is None


def test_forecast_with_no_surviving_pairs_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every value is null — nothing to return."""
    monkeypatch.setattr(
        open_meteo,
        "_http_get",
        lambda url, timeout: _forecast_payload(values=[None, None]),
    )
    assert open_meteo.fetch_radiation_forecast(35.2, -97.4) is None


def test_forecast_url_carries_required_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        seen["url"] = url
        return _forecast_payload()

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    open_meteo.fetch_radiation_forecast(35.2, -97.4)
    assert "hourly=shortwave_radiation" in seen["url"]
    assert "forecast_days=2" in seen["url"]
    assert "timezone=UTC" in seen["url"]


def test_the_client_writes_exactly_the_site_metrics() -> None:
    # Two truths, one fact: the registry classifies which metrics are the
    # site's (SITE_METRICS gates staleness and widens the store's writable
    # set), and the client maps API fields to the metrics it writes. If they
    # drift, either the store refuses a weather append or the staleness
    # witness starts counting sky rows as the inverter answering.
    from arraysense.metrics import SITE_METRICS
    from arraysense.weather import METRICS

    assert METRICS == SITE_METRICS


def test_the_current_fetch_carries_irradiance_and_converts_wind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Open-Meteo reports wind in km/h; Faiman wants m/s. The conversion happens
    # here, at the boundary, so no consumer can inherit the wrong unit.
    payload = json.dumps(
        {
            "current": {
                "temperature_2m": 24.0,
                "cloud_cover": 10,
                "shortwave_radiation": 700.0,
                "direct_normal_irradiance": 850.0,
                "diffuse_radiation": 120.0,
                "wind_speed_10m": 18.0,  # km/h
            }
        }
    ).encode()
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: payload)
    sample = open_meteo.fetch_current(35.2, -97.4)
    assert sample is not None
    assert sample.readings["ghi_wm2"] == 700.0
    assert sample.readings["dni_wm2"] == 850.0
    assert sample.readings["dhi_wm2"] == 120.0
    assert sample.readings["wind_speed_ms"] == pytest.approx(5.0, abs=0.01)  # 18/3.6
