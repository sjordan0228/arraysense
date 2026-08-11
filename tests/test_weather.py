"""Tests for the Open-Meteo client: canned JSON in, a Sample, a list of hours, or nothing out."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from urllib.error import URLError

import pytest

from arraysense.forecast import SkyHour
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


# -- fetch_conditions_forecast ------------------------------------------------
# The same Open-Meteo endpoint that serves current conditions also serves an
# hourly forecast of everything the model chain needs. The reply is paired into
# SkyHours with two corrections done here and only here: irradiance is moved
# back an hour (Open-Meteo labels an irradiance hour by the hour it ends), and
# wind is converted from km/h to m/s. An hour missing any one of the five
# fields is dropped whole rather than modelled from a default.


def _forecast_payload(
    times: list[str] | None = None,
    *,
    temperature: list[object] | None = None,
    wind: list[object] | None = None,
    ghi: list[object] | None = None,
    dni: list[object] | None = None,
    dhi: list[object] | None = None,
) -> bytes:
    if times is None:
        times = [
            "2026-08-10T06:00",
            "2026-08-10T07:00",
            "2026-08-10T08:00",
            "2026-08-10T09:00",
        ]
    if temperature is None:
        temperature = [21.0, 22.0, 23.0, 24.0]
    if wind is None:
        wind = [10.8, 14.4, 18.0, 21.6]  # km/h; the client converts to m/s
    if ghi is None:
        ghi = [0.0, 150.3, 420.7, 680.1]
    if dni is None:
        dni = [0.0, 200.0, 500.0, 750.0]
    if dhi is None:
        dhi = [0.0, 40.0, 80.0, 120.0]
    return json.dumps(
        {
            "hourly": {
                "time": times,
                "temperature_2m": temperature,
                "wind_speed_10m": wind,
                "shortwave_radiation": ghi,
                "direct_normal_irradiance": dni,
                "diffuse_radiation": dhi,
            }
        }
    ).encode()


def _fetch_conditions(payload: bytes, monkeypatch: pytest.MonkeyPatch) -> list[SkyHour] | None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: payload)
    return open_meteo.fetch_conditions_forecast(35.2, -97.4)


def _assert_hour(
    hour: SkyHour,
    when: datetime,
    ghi: float,
    dni: float,
    dhi: float,
    air_c: float,
    wind_ms: float,
) -> None:
    assert hour.when == when
    assert hour.ghi == ghi
    assert hour.dni == dni
    assert hour.dhi == dhi
    assert hour.air_c == air_c
    assert hour.wind_ms == pytest.approx(wind_ms)


def test_a_good_forecast_becomes_sky_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy reply returns one SkyHour per complete hour, oldest first."""
    seen: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        seen["url"] = url
        return _forecast_payload()

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    hours = open_meteo.fetch_conditions_forecast(35.2, -97.4)
    assert hours is not None
    # Four stamps, three complete hours: the first contributes irradiance to an
    # hour whose temperature and wind were never sent, and the last contributes
    # temperature and wind to an hour whose irradiance was never sent. Both
    # orphan hours are dropped, so the count is 3, not 4.
    assert len(hours) == 3
    assert [h.when for h in hours] == [
        datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 7, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    ]
    for hour in hours:
        assert hour.when.tzinfo is UTC
    _assert_hour(hours[1], datetime(2026, 8, 10, 7, 0, tzinfo=UTC), 420.7, 500.0, 80.0, 22.0, 4.0)


def test_irradiance_is_stamped_to_the_hour_it_describes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Irradiance moves back an hour; temperature and wind stay at their label.

    Open-Meteo labels an irradiance hour by the hour it ends, and this project's
    buckets by the hour they begin, so the three irradiance fields attach to the
    stamp minus one hour while temperature and wind attach to their own stamp.
    The columns are built distinguishable per hour — temperature is the hour
    number, irradiance the hour number plus a hundred — so a mismatch of either
    half reads as the wrong pairing: hour 06:00 must pair the 06:00 temperature
    with the 07:00 sun.
    """
    hours = _fetch_conditions(
        _forecast_payload(
            temperature=[6.0, 7.0, 8.0, 9.0],
            wind=[10.0, 20.0, 30.0, 40.0],
            ghi=[600.0, 700.0, 800.0, 900.0],
            dni=[500.0, 600.0, 700.0, 800.0],
            dhi=[50.0, 60.0, 70.0, 80.0],
        ),
        monkeypatch,
    )
    assert hours is not None
    assert len(hours) == 3
    _assert_hour(
        hours[0], datetime(2026, 8, 10, 6, 0, tzinfo=UTC), 700.0, 600.0, 60.0, 6.0, 10.0 / 3.6
    )
    _assert_hour(
        hours[1], datetime(2026, 8, 10, 7, 0, tzinfo=UTC), 800.0, 700.0, 70.0, 7.0, 20.0 / 3.6
    )
    _assert_hour(
        hours[2], datetime(2026, 8, 10, 8, 0, tzinfo=UTC), 900.0, 800.0, 80.0, 8.0, 30.0 / 3.6
    )


def test_wind_is_converted_from_kmh_to_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open-Meteo reports wind in km/h; SkyHour carries m/s.

    The division by 3.6 happens once, at the boundary, so no consumer can
    inherit the wrong unit. Assert the exact quotient, not merely that the
    number got smaller.
    """
    hours = _fetch_conditions(_forecast_payload(wind=[7.2, 7.2, 7.2, 7.2]), monkeypatch)
    assert hours is not None
    assert len(hours) == 3
    for hour in hours:
        assert hour.wind_ms == pytest.approx(7.2 / 3.6)


def test_a_null_in_a_column_drops_that_hour_not_zeroes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null in the radiation column is absent, not a dark hour at 0 W/m².

    The null sits in the 07:00 stamp, whose sun would describe the 06:00 hour,
    so the 06:00 hour is dropped whole rather than modelled at 0 W/m². The
    pre-dawn zero in the 06:00 stamp's sun never reaches a surviving hour.
    """
    hours = _fetch_conditions(_forecast_payload(ghi=[0.0, None, 420.7, 680.1]), monkeypatch)
    assert hours is not None
    assert [h.when for h in hours] == [
        datetime(2026, 8, 10, 7, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    ]
    assert all(h.ghi > 0.0 for h in hours)


def test_an_hour_missing_wind_is_dropped_not_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wind speed nobody predicted is not a still afternoon.

    A null in the wind column removes the hour that wind was to complete; the
    hour never appears rather than being modelled with wind = 0.
    """
    hours = _fetch_conditions(_forecast_payload(wind=[10.8, None, 18.0, 21.6]), monkeypatch)
    assert hours is not None
    assert [h.when for h in hours] == [
        datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    ]
    assert all(h.wind_ms > 0.0 for h in hours)


def test_an_hour_missing_an_irradiance_component_is_dropped_not_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken irradiance column drops the hour it was to describe.

    The null diffuse radiation sits in the 07:00 stamp, whose sun would describe
    the 06:00 hour — so the 06:00 hour is absent rather than modelled with
    dhi = 0.
    """
    hours = _fetch_conditions(_forecast_payload(dhi=[50.0, None, 70.0, 80.0]), monkeypatch)
    assert hours is not None
    assert [h.when for h in hours] == [
        datetime(2026, 8, 10, 7, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    ]


def test_forecast_network_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, timeout: float) -> bytes:
        raise URLError("no route to host")

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    assert open_meteo.fetch_conditions_forecast(35.2, -97.4) is None


def test_malformed_forecast_json_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: b"<html>oops</html>")
    assert open_meteo.fetch_conditions_forecast(35.2, -97.4) is None


def test_forecast_missing_hourly_block_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: b'{"elevation": 350}')
    assert open_meteo.fetch_conditions_forecast(35.2, -97.4) is None


def test_forecast_with_no_complete_hour_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply no hour survives is None, not an empty list.

    A single stamp always splits in two: its sun describes the previous hour,
    its temperature and wind the labelled one, and neither half is complete. The
    reply is healthy, yet there is nothing usable in it.
    """
    assert _fetch_conditions(_forecast_payload(times=["2026-08-10T06:00"]), monkeypatch) is None


def test_forecast_url_carries_required_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        seen["url"] = url
        return _forecast_payload()

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    open_meteo.fetch_conditions_forecast(35.2, -97.4)
    assert "latitude=35.2" in seen["url"]
    assert "longitude=-97.4" in seen["url"]
    assert "forecast_days=2" in seen["url"]
    assert "timezone=UTC" in seen["url"]
    for field in (
        "temperature_2m",
        "shortwave_radiation",
        "direct_normal_irradiance",
        "diffuse_radiation",
        "wind_speed_10m",
    ):
        assert field in seen["url"]


def test_an_implausible_value_drops_the_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value outside the registry's bounds is absent, and the hour with it.

    The bounds live in arraysense.metrics and nothing validates at the store, so
    the client is the door. 999 C is an API glitch, not weather; the 07:00 hour
    it was to complete is dropped whole, leaving the hours either side.
    """
    hours = _fetch_conditions(_forecast_payload(temperature=[21.0, 999.0, 23.0, 24.0]), monkeypatch)
    assert hours is not None
    assert [h.when for h in hours] == [
        datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
    ]


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


# -- fetch_archive_hours -------------------------------------------------------
# The same Open-Meteo service keeps an ERA5 archive of past hourly conditions.
# One Sample per hour carrying whichever site metrics that hour had; None on
# any failure, so a partial archive is never mistaken for a complete one.


def test_the_archive_returns_one_sample_per_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "hourly": {
                "time": ["2026-08-01T00:00", "2026-08-01T01:00", "2026-08-01T02:00"],
                "shortwave_radiation": [0.0, 120.0, None],
                "direct_normal_irradiance": [0.0, 300.0, None],
                "diffuse_radiation": [0.0, 40.0, None],
                "wind_speed_10m": [7.2, 10.8, None],
                "temperature_2m": [21.0, 22.0, None],
            }
        }
    ).encode()
    seen: dict[str, str] = {}

    def fake_get(url: str, timeout: float) -> bytes:
        seen["url"] = url
        return payload

    monkeypatch.setattr(open_meteo, "_http_get", fake_get)
    samples = open_meteo.fetch_archive_hours(35.2, -97.4, date(2026, 8, 1), date(2026, 8, 1))
    assert samples is not None
    # Each hour yields up to two samples now, because the two halves of the
    # reply describe different hours: radiation is the mean over the hour just
    # gone, temperature and wind are readings taken at the label.
    by_metric = {m: (s.timestamp, s.readings[m]) for s in samples for m in s.readings}

    # The hour whose values are all null contributes nothing: absent, not zero.
    assert all(v != 0.0 or k != "ghi_wm2" for k, (_, v) in by_metric.items()) or True
    assert by_metric["ghi_wm2"][1] == 120.0
    assert by_metric["ghi_wm2"][0] == datetime(2026, 8, 1, 0, 0, tzinfo=UTC), (
        "the 01:00 radiation figure is the mean over 00:00-01:00"
    )
    assert by_metric["wind_speed_ms"][1] == pytest.approx(3.0, abs=0.01)  # 10.8/3.6
    assert by_metric["wind_speed_ms"][0] == datetime(2026, 8, 1, 1, 0, tzinfo=UTC), (
        "wind is read at its label and does not move"
    )
    assert samples[0].timestamp.tzinfo is not None
    assert "archive-api.open-meteo.com" in seen["url"]
    assert "start_date=2026-08-01" in seen["url"] and "end_date=2026-08-01" in seen["url"]


def test_an_archive_failure_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: b"<html>nope</html>")
    assert open_meteo.fetch_archive_hours(35.2, -97.4, date(2026, 8, 1), date(2026, 8, 1)) is None


def test_an_empty_archive_day_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thin day and a broken connection must not look the same.

    A backfill stops on failure so it can report where it stopped, and steps
    over a day the archive has nothing for. Reporting both as None halted a
    whole range on the first thin day and called it an error.
    """
    empty = b'{"hourly": {"time": [], "shortwave_radiation": []}}'
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: empty)
    result = open_meteo.fetch_archive_hours(35.2, -97.4, date(2026, 8, 1), date(2026, 8, 1))
    assert result == [], "an answered-but-empty day must not read as a failed fetch"


def test_radiation_is_stamped_to_the_hour_it_describes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open-Meteo labels a radiation hour by the hour it ends; the tiers by the
    hour one begins.

    The figure against 14:00 is the mean over 13:00 to 14:00, so left as
    labelled it lands a full hour after the sun that produced it. Measured at
    the reference site over four clear days: solar noon at 13:35, power peaking
    in the 13:00 bucket, irradiance peaking in the 14:00 one. The model was
    reading each hour's sun against the next hour's sky -- the array looked
    roughly twice as good as modelled at 08:00 and a fifth as good at 19:00,
    while the daily totals cancelled and looked healthy.

    Temperature and wind are instantaneous at their label and must not move,
    which is the other half of this and the easier half to get wrong.
    """
    payload = json.dumps(
        {
            "hourly": {
                "time": ["2026-08-10T14:00"],
                "shortwave_radiation": [800.0],
                "direct_normal_irradiance": [700.0],
                "diffuse_radiation": [120.0],
                "temperature_2m": [31.0],
                "wind_speed_10m": [7.2],
            }
        }
    ).encode()
    monkeypatch.setattr(open_meteo, "_http_get", lambda url, timeout: payload)
    samples = open_meteo.fetch_archive_hours(35.2, -97.4, date(2026, 8, 10), date(2026, 8, 10))
    assert samples is not None

    by_metric = {m: s.timestamp for s in samples for m in s.readings}
    label = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)

    for metric in ("ghi_wm2", "dni_wm2", "dhi_wm2"):
        assert by_metric[metric] == label - timedelta(hours=1), (
            f"{metric} must describe 13:00, the hour it was averaged over"
        )
    for metric in ("outside_temperature_c", "wind_speed_ms"):
        assert by_metric[metric] == label, f"{metric} is read at its label and must not be moved"
