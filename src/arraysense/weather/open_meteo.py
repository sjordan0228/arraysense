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
from datetime import UTC, datetime

from arraysense.metrics import INVERTER_METRICS
from arraysense.models import Sample

logger = logging.getLogger(__name__)

_BASE = "https://api.open-meteo.com/v1/forecast"

# What Open-Meteo calls each value -> the registry metric it lands in.
_FIELDS: dict[str, str] = {
    "temperature_2m": "outside_temperature_c",
    "cloud_cover": "cloud_cover_pct",
}

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


def _http_get(url: str, timeout: float) -> bytes:
    """One GET, kept separate so tests inject replies without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return bytes(response.read())


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
        spec = _SPECS[metric]
        if not spec.lower <= float(value) <= spec.upper:
            logger.debug(
                "weather %s=%s is outside %s..%s; dropped", metric, value, spec.lower, spec.upper
            )
            continue
        readings[metric] = float(value)
    if not readings:
        return None
    return Sample(timestamp=datetime.now(UTC), readings=readings)
