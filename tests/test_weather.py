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
