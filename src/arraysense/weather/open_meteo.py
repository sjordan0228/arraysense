"""open_meteo.py — one HTTPS GET for the sky over the installation.

Open-Meteo is the chosen source because it satisfies every constraint in #5:
no API key, so there is nothing to store or leak; one GET returns both values
this project records; and the same endpoint serves the hourly forecast, so the
forecast sub-feature needs no second source. The client is stdlib-only —
urllib and json — because a weather client is an HTTP GET, not a dependency.

Every failure returns None: no internet, DNS, a timeout, a non-200, malformed
JSON, a missing block. The service must work with no route to the internet,
and a fetch that fails leaves every existing reading intact and records
nothing — absent, not zero. Values the registry's bounds call implausible are
dropped field by field for the same reason: nothing at runtime validates at
the store, so the door is here.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime

from arraysense.metrics import INVERTER_METRICS
from arraysense.models import Sample

logger = logging.getLogger(__name__)

_BASE = "https://api.open-meteo.com/v1/forecast"

# What Open-Meteo calls each value -> the registry metric it lands in.
_FIELDS: dict[str, str] = {
    "temperature_2m": "outside_temperature_c",
    "cloud_cover": "cloud_cover_pct",
    "shortwave_radiation": "ghi_wm2",
    "direct_normal_irradiance": "dni_wm2",
    "diffuse_radiation": "dhi_wm2",
    "wind_speed_10m": "wind_speed_ms",
}

# Values the reply gives in one unit and the registry stores in another.
# Open-Meteo reports wind in km/h; the cell-temperature model wants m/s, and a
# unit that converts at every reader is a unit that eventually does not.
_CONVERSIONS: dict[str, float] = {"wind_speed_10m": 1.0 / 3.6}

# The metric names this source writes, for whoever opens the store. A store is
# opened with a whitelist of writable metrics, and the driver's declaration
# does not include the sky — a store opened for the driver alone refuses every
# weather append with a KeyError. Exported here, beside the mapping it is
# derived from, so the set cannot drift from what fetch_current produces.
METRICS: frozenset[str] = frozenset(_FIELDS.values())

_SPECS = {spec.name: spec for spec in INVERTER_METRICS}

# The exact ways a stdlib HTTP GET plus JSON parse can fail, and nothing wider:
# URLError covers HTTPError and DNS, OSError covers socket teardown mid-read,
# TimeoutError the socket timeout, ValueError the JSON that is not JSON.
_FETCH_ERRORS = (urllib.error.URLError, OSError, TimeoutError, ValueError)

_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def _http_get(url: str, timeout: float) -> bytes:
    """One GET, kept separate so tests inject replies without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return bytes(response.read())


def fetch_radiation_forecast(
    latitude: float,
    longitude: float,
    timeout: float = 10.0,
) -> list[tuple[datetime, float]] | None:
    """Fetch the day's hourly shortwave radiation forecast, or nothing.

    The same free endpoint that serves current conditions also serves the
    day's cloud-adjusted radiation forecast. That forecast is what the
    prediction engine scales into expected production — a watt-hour figure per
    hour, from the cloud cover the model already knows and the sun angle at
    this latitude. A prediction is fetched, never measured, and the caller
    stores it in the forecast table, never a metric column.

    Returns a list of (aware UTC datetime, W/m²) pairs for every hour whose
    value is a real non-negative number. None means nothing to record: the
    fetch failed, the reply had no hourly block, or no pair survived filtering.
    """
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "shortwave_radiation",
            # Two days, not one: forecast_days counts UTC calendar days, and one
            # UTC day ends at 6 PM in Chicago — which cut the owner's chart off
            # mid-evening and left mornings without the day's tail. Two days
            # always cover the whole local day whatever the zone; hours beyond
            # today land in the table and simply become tomorrow's early
            # "latest" curve until tomorrow's own dawn plan is made.
            "forecast_days": "2",
            "timezone": "UTC",
        }
    )
    try:
        raw = _http_get(f"{_BASE}?{query}", timeout)
        hourly = json.loads(raw)["hourly"]
    except _FETCH_ERRORS as exc:
        logger.debug("radiation forecast fetch failed, recording nothing: %s", exc)
        return None
    except (KeyError, TypeError):
        logger.debug("radiation forecast reply carried no hourly block; recording nothing")
        return None

    times = hourly.get("time") if isinstance(hourly, dict) else None
    values = hourly.get("shortwave_radiation") if isinstance(hourly, dict) else None
    if not isinstance(times, list) or not isinstance(values, list):
        return None

    pairs: list[tuple[datetime, float]] = []
    # strict=False by intent: a reply whose two lists disagree in length is
    # malformed at the tail, and pairing what aligns while dropping the rest
    # is the same degrade-to-absent rule as every other failure here.
    for time_str, value in zip(times, values, strict=False):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if float(value) < 0:
            continue
        try:
            ts = datetime.fromisoformat(time_str).replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        pairs.append((ts, float(value)))
    return pairs or None


def fetch_archive_hours(
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    timeout: float = 30.0,
) -> list[Sample] | None:
    """Read past hourly conditions, one Sample per hour, or nothing.

    The same free service keeps an ERA5 archive, so a system that has been
    collecting for months can be scored against the conditions those months
    actually had rather than starting its trend from today. An hour whose
    values are all missing contributes no Sample at all — absent, not zero —
    and any failure returns None so a partial archive is never mistaken for a
    complete one.
    """
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": ",".join(_FIELDS),
            "timezone": "UTC",
        }
    )
    try:
        raw = _http_get(f"{_ARCHIVE}?{query}", timeout)
        hourly = json.loads(raw)["hourly"]
        times = hourly["time"]
    except _FETCH_ERRORS as exc:
        logger.debug("archive fetch failed, recording nothing: %s", exc)
        return None
    except (KeyError, TypeError):
        logger.debug("archive reply carried no hourly block; recording nothing")
        return None

    samples: list[Sample] = []
    for index, when in enumerate(times):
        readings: dict[str, float] = {}
        for field, metric in _FIELDS.items():
            column = hourly.get(field)
            if not isinstance(column, list) or index >= len(column):
                continue
            value = column[index]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            converted = float(value) * _CONVERSIONS.get(field, 1.0)
            spec = _SPECS[metric]
            if not spec.lower <= converted <= spec.upper:
                continue
            readings[metric] = converted
        if not readings:
            continue
        try:
            stamp = datetime.fromisoformat(when).replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        samples.append(Sample(timestamp=stamp, readings=readings))
    return samples or None


def fetch_current(latitude: float, longitude: float, timeout: float = 10.0) -> Sample | None:
    """Read the current outside temperature and cloud cover, or nothing.

    Returns a Sample carrying whichever of the two values arrived plausible,
    stamped with the fetch time. None means nothing worth recording: the fetch
    failed, the reply had no current block, or no value survived the registry's
    bounds. The caller records a None by doing nothing at all.
    """
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": ",".join(_FIELDS),
        }
    )
    try:
        raw = _http_get(f"{_BASE}?{query}", timeout)
        current = json.loads(raw)["current"]
    except _FETCH_ERRORS as exc:
        logger.debug("weather fetch failed, recording nothing: %s", exc)
        return None
    except (KeyError, TypeError):
        logger.debug("weather reply carried no current block; recording nothing")
        return None

    readings: dict[str, float] = {}
    for field, metric in _FIELDS.items():
        value = current.get(field) if isinstance(current, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        converted = float(value) * _CONVERSIONS.get(field, 1.0)
        spec = _SPECS[metric]
        if not spec.lower <= converted <= spec.upper:
            logger.debug(
                "weather %s=%s is outside %s..%s; dropped",
                metric,
                converted,
                spec.lower,
                spec.upper,
            )
            continue
        readings[metric] = converted
    if not readings:
        return None
    return Sample(timestamp=datetime.now(UTC), readings=readings)
