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
from datetime import UTC, date, datetime, timedelta
from zoneinfo import available_timezones

from arraysense.forecast import SkyHour
from arraysense.metrics import INVERTER_METRICS
from arraysense.models import Sample

logger = logging.getLogger(__name__)

_BASE = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_BASE = "https://geocoding-api.open-meteo.com/v1/search"

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

# Open-Meteo labels a radiation hour by the hour it ENDS: the figure against
# 14:00 is the mean over 13:00 to 14:00. This project's tiers label a bucket by
# the hour it BEGINS, so radiation arrives attributed to the hour after the one
# it describes and has to be moved back by one.
#
# Measured rather than taken from the documentation: across four clear days at
# the reference site, true solar noon falls at 13:35, the inverter's power peaks
# in the 13:00 bucket, and the irradiance peaked in the 14:00 one. The model was
# therefore reading each hour's sun against the next hour's sky, which made the
# array look almost twice as good as modelled at 08:00 and a fifth as good at
# 19:00 while the daily totals quietly cancelled out.
#
# Temperature, cloud and wind are instantaneous readings at their label and must
# NOT move; shifting those would trade one misalignment for another.
_PRECEDING_HOUR_MEAN: frozenset[str] = frozenset(
    {"shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation"}
)

_SPECS = {spec.name: spec for spec in INVERTER_METRICS}

# What the forecast asks for: everything the model chain consumes, and nothing
# it does not. Cloud cover is deliberately absent — it is a reading worth
# recording about the present and tells the model nothing the three irradiance
# components have not already said about the future.
_FORECAST_FIELDS: dict[str, str] = {
    field: metric for field, metric in _FIELDS.items() if metric != "cloud_cover_pct"
}

# An hour is only usable when every one of these arrived. Named as a set so the
# completeness check cannot drift from the fields the request asks for.
#
# "Arrived" means survived, not merely was sent: a value the registry's bounds
# call implausible is dropped field by field above, so a single absurd
# temperature takes its whole hour out of the forecast. That is the intended
# behaviour and it is worth knowing when an hour goes missing, because the
# obvious suspect is the network and the real culprit may be one bad number.
_MODEL_INPUTS: frozenset[str] = frozenset(_FORECAST_FIELDS.values())

# The exact ways a stdlib HTTP GET plus JSON parse can fail, and nothing wider:
# URLError covers HTTPError and DNS, OSError covers socket teardown mid-read,
# TimeoutError the socket timeout, ValueError the JSON that is not JSON.
_FETCH_ERRORS = (urllib.error.URLError, OSError, TimeoutError, ValueError)

_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def _http_get(url: str, timeout: float) -> bytes:
    """One GET, kept separate so tests inject replies without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return bytes(response.read())


def fetch_conditions_forecast(
    latitude: float,
    longitude: float,
    timeout: float = 10.0,
) -> list[SkyHour] | None:
    """Fetch the coming hours of predicted sky, or nothing.

    Everything the model chain needs in one keyless GET: the three irradiance
    components, the air temperature and the wind. The same endpoint already
    serves current conditions, so this costs no new dependency and no new key.

    This replaced a fetch of shortwave radiation alone, because the forecast is
    no longer a radiation curve multiplied by a constant — it is the same
    physics the efficiency engine runs, and that physics needs to know how hot
    the modules will be and how hard the wind will be carrying that heat away.

    Two corrections happen here and only here, for the reason the archive reader
    makes them in the same place: a caller that inherited either would be wrong
    in a way no test written from the same misunderstanding would catch.

    Wind arrives in km/h and the cell-temperature model wants m/s.

    Irradiance is labelled by the hour it ENDS and this project's buckets are
    labelled by the hour they BEGIN, so each irradiance figure moves back one
    hour. Temperature and wind are readings taken at their label and must not
    move. Measured rather than taken from the documentation: across 341 hours of
    a fortnight at the reference site, the forecast correlates 0.978 with that
    site's own recorded irradiance when moved back an hour against 0.941 left
    alone, and 0.976 against the inverter's own output against 0.940. The
    forecast that shipped without this put the array's predicted peak at 14:00
    on a day it actually peaked at 13:00, and started the morning an hour late.

    An hour missing any one of the five is dropped rather than modelled from a
    default, because a wind speed nobody predicted is not a still afternoon —
    Faiman puts the cells 13 C hotter at zero wind than at two metres per second.

    Returns None when there is nothing to record: the fetch failed, the reply
    carried no hourly block, or no hour arrived complete.
    """
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(_FORECAST_FIELDS),
            # Two days, not one: forecast_days counts UTC calendar days, and one
            # UTC day ends at 6 PM in Chicago — which cut the owner's chart off
            # mid-evening and left mornings without the day's tail. Two days
            # always cover the whole local day whatever the zone; hours beyond
            # today land in the table and stand as tomorrow's early prediction
            # until tomorrow's own fetches revise them.
            "forecast_days": "2",
            "timezone": "UTC",
        }
    )
    try:
        raw = _http_get(f"{_BASE}?{query}", timeout)
        hourly = json.loads(raw)["hourly"]
        times = hourly["time"]
    except _FETCH_ERRORS as exc:
        logger.debug("conditions forecast fetch failed, recording nothing: %s", exc)
        return None
    except (KeyError, TypeError):
        logger.debug("conditions forecast reply carried no hourly block; recording nothing")
        return None
    if not isinstance(times, list):
        return None

    # Gathered per stamp first, because the irradiance fields move back an hour
    # and so belong beside a different hour's temperature and wind than the one
    # they arrived under.
    gathered: dict[datetime, dict[str, float]] = {}
    for index, when in enumerate(times):
        try:
            stamp = datetime.fromisoformat(when).replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        for field, metric in _FORECAST_FIELDS.items():
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
            at = stamp - timedelta(hours=1) if field in _PRECEDING_HOUR_MEAN else stamp
            gathered.setdefault(at, {})[metric] = converted

    hours = [
        SkyHour(
            when=at,
            ghi=values["ghi_wm2"],
            dni=values["dni_wm2"],
            dhi=values["dhi_wm2"],
            air_c=values["outside_temperature_c"],
            wind_ms=values["wind_speed_ms"],
        )
        for at, values in sorted(gathered.items())
        if values.keys() >= _MODEL_INPUTS
    ]
    return hours or None


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
        # Split by what the label means: a mean over the hour just gone, or a
        # reading taken at that moment. They belong to different hours.
        at_label: dict[str, float] = {}
        over_previous: dict[str, float] = {}
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
            if field in _PRECEDING_HOUR_MEAN:
                over_previous[metric] = converted
            else:
                at_label[metric] = converted
        if not at_label and not over_previous:
            continue
        try:
            stamp = datetime.fromisoformat(when).replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        if over_previous:
            samples.append(Sample(timestamp=stamp - timedelta(hours=1), readings=over_previous))
        if at_label:
            samples.append(Sample(timestamp=stamp, readings=at_label))
    # An empty list and None mean different things and the caller acts on the
    # difference: None is "the fetch failed", which must stop a backfill where
    # it stands, and [] is "the archive answered and had nothing for that day",
    # which a backfill should step over. Collapsing the two into None halted a
    # whole range on the first day the archive happened to be thin.
    return samples


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


def geocode(query: str, country: str | None = None) -> list[dict[str, object]] | None:
    """Resolve a postcode or place name to coordinates, or None on any failure.

    Calls the same keyless Open-Meteo geocoding API as the weather client
    already uses, so this costs no new dependency and no new key. Returns a
    list of candidate results each carrying name, admin1, country, country
    code, latitude, longitude and the timezone — but only when that timezone
    is in ``zoneinfo.available_timezones()``, because ``site.timezone`` is a
    ``choice`` setting validated against the tz database and a zone the
    registry would refuse must be dropped here rather than travelling to the
    page and being refused at save time on a field the owner did not
    knowingly fill.

    Returns None when the fetch fails or the reply carries no ``results``
    key — the same shape ``fetch_current`` already handles for a missing
    block. An empty list means the service answered, found nothing, and said
    so, which is a successful lookup with no matches rather than a failure.
    """
    params: dict[str, str] = {"name": query, "count": "5", "language": "en", "format": "json"}
    if country:
        params["countryCode"] = country
    url = f"{_GEOCODE_BASE}?{urllib.parse.urlencode(params)}"
    try:
        raw = _http_get(url, timeout=10.0)
        body = json.loads(raw)
        candidates = body.get("results")
    except _FETCH_ERRORS as exc:
        logger.debug("geocode fetch failed: %s", exc)
        return None
    except (KeyError, TypeError) as exc:
        logger.debug("geocode reply unreadable: %s", exc)
        return None
    if not isinstance(candidates, list):
        return None

    known_zones = available_timezones()
    results: list[dict[str, object]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        tz = entry.get("timezone")
        if not isinstance(tz, str) or tz not in known_zones:
            # A timezone the registry would refuse is dropped here, where
            # the geocoder gave it, rather than travelling to the page and
            # being refused at save time on a field the owner did not fill.
            continue
        lat = entry.get("latitude")
        lon = entry.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        name = entry.get("name", "")
        admin1 = entry.get("admin1", "")
        country_name = entry.get("country", "")
        country_code = entry.get("country_code", "")
        results.append(
            {
                "name": str(name) if isinstance(name, str) else "",
                "admin1": str(admin1) if isinstance(admin1, str) else "",
                "country": str(country_name) if isinstance(country_name, str) else "",
                "country_code": str(country_code) if isinstance(country_code, str) else "",
                "latitude": float(lat),
                "longitude": float(lon),
                "timezone": tz,
            }
        )
    return results
