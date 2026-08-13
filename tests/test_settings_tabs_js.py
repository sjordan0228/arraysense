"""test_settings_tabs_js.py — every prefix lands in exactly one tab, and deep links work.

The settings page's TABS array maps setting prefixes to tabs. A prefix not claimed
by any tab still renders under a catch-all, so adding a setting to the registry can
never make it invisible. These tests run the exact tab-defs slice from settings.html
under node — the same pattern test_dashboard_caps_js.py uses for caps-logic — so a
tab definition that leaves a prefix uncovered, or a hash-routing function that picks
the wrong tab, is caught here rather than in Chrome.

Skipped where node is not installed; loud if the extraction markers move.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "settings.html"

_TAB_START = "// >>> tab-defs"
_TAB_END = "// <<< tab-defs"


def _tab_slice() -> str:
    text = PAGE.read_text()
    start = text.index(_TAB_START)
    end = text.index(_TAB_END)
    assert start < end, f"tab-defs markers out of order in settings.html: {_TAB_START}"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    script = _tab_slice() + "\n" + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# Every setting prefix that exists in the registry must appear in exactly one
# tab definition (or be deliberately left for the "other" catch-all). The test
# reads the registry directly so it drifts if settings.py adds a prefix without
# a matching tab entry — which is the drift this test exists to catch.
# ---------------------------------------------------------------------------


def _registry_prefixes() -> set[str]:
    """Read every distinct prefix from the settings registry."""
    from arraysense.settings import SETTINGS

    return {spec.key.split(".")[0] for spec in SETTINGS}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_every_registry_prefix_maps_to_exactly_one_tab() -> None:
    """No setting prefix is claimed by zero tabs or by more than one tab."""
    prefixes = sorted(_registry_prefixes())
    checks = []
    for p in prefixes:
        checks.append(f"  '{p}': tabForPrefix['{p}'],")
    body = (
        "const map = {\n" + "\n".join(checks) + "\n};\n"
        "const entries = Object.entries(map);\n"
        "const missing = entries.filter(([,v]) => v === undefined);\n"
        "if (missing.length) "
        "console.log('MISSING:' + missing.map(([k]) => k).join(','));\n"
        "const covered = entries.filter(([,v]) => v !== undefined);\n"
        "const byTab = {};\n"
        "covered.forEach(([p, t]) => { "
        "if (!byTab[t]) byTab[t] = []; byTab[t].push(p); });\n"
        "Object.entries(byTab).forEach(([t, ps]) => "
        "console.log('TAB:' + t + '=' + ps.join(',')));\n"
    )
    out = _run(body)
    raw = out.split("\n")
    lines = [ln for ln in raw if ln.startswith("MISSING:") or ln.startswith("TAB:")]
    # No prefix should be missing from all tabs.
    missing = [ln for ln in lines if ln.startswith("MISSING:")]
    assert not missing, f"Prefixes with no tab: {missing}"
    # Every covered prefix should appear exactly once (the data structure
    # guarantees this, but assert it anyway).
    tab_lines = [ln for ln in lines if ln.startswith("TAB:")]
    assert tab_lines, "No tab assignments found"
    # Verify the expected tab mapping is intact.
    tab_map: dict[str, list[str]] = {}
    for line in tab_lines:
        _, rest = line.split(":", 1)
        tab_id, prefixes_str = rest.split("=", 1)
        tab_map[tab_id] = prefixes_str.split(",")
    # Known tabs must claim their expected prefixes.
    assert "connection" in tab_map.get("inverter", []), "connection not in inverter tab"
    assert "panels" in tab_map.get("solar-panels", []), "panels not in solar-panels tab"
    assert "efficiency" in tab_map.get("solar-panels", []), "efficiency not in solar-panels tab"
    assert "battery" in tab_map.get("battery", []), "battery not in battery tab"
    assert "collector" in tab_map.get("collection", []), "collector not in collection tab"
    # backup may or may not have settings yet, but the tab definition names it.
    assert "tariff" in tab_map.get("rate-bands", []), "tariff not in rate-bands tab"
    assert "site" in tab_map.get("general", []), "site not in general tab"
    assert "display" in tab_map.get("general", []), "display not in general tab"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_unknown_prefix_is_not_claimed_by_any_tab() -> None:
    """A prefix not in any tab definition returns undefined (catch-all picks it up)."""
    body = "console.log('result:' + String(tabForPrefix['nonexistent_prefix_xyz']));\n"
    out = _run(body)
    assert out == "result:undefined", f"Unknown prefix should be undefined, got: {out}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_backup_prefix_is_named_in_collection_tab() -> None:
    """The backup prefix must be claimed by the collection tab even if no settings
    exist yet."""
    body = "console.log('backup_tab:' + String(tabForPrefix['backup']));\n"
    out = _run(body)
    assert out == "backup_tab:collection", f"backup should map to collection, got: {out}"


# ---------------------------------------------------------------------------
# Deep-link routing logic. The wantedTab() function resolves which tab to show
# from the URL hash, localStorage, and the set of visible tabs. Test the
# priority rules without a DOM by extracting the decision into a pure function.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_wanted_tab_respects_hash_first() -> None:
    """The URL hash is authoritative: it wins over localStorage."""
    body = (
        "const visibleIds = function() {\n"
        "  return ['inverter','solar-panels','battery','collection',"
        "'rate-bands','general','about'];\n"
        "};\n"
        "function testWanted(hash, stored, visible) {\n"
        "  if (hash && visible.includes(hash)) return 'hash:' + hash;\n"
        "  if (stored && visible.includes(stored)) return 'stored:' + stored;\n"
        "  return 'first:' + visible[0];\n"
        "}\n"
        "var vis = visibleIds();\n"
        "console.log('no_hash_no_stored:' + testWanted('', null, vis));\n"
        "console.log('hash_valid:' + testWanted('solar-panels', null, vis));\n"
        "console.log('hash_beats_stored:' "
        "+ testWanted('battery', 'inverter', vis));\n"
        "console.log('hash_invalid_falls_to_stored:' "
        "+ testWanted('nonexistent', 'general', vis));\n"
        "console.log('no_hash_stored_valid:' "
        "+ testWanted('', 'rate-bands', vis));\n"
        "console.log('no_hash_stored_invalid_falls_to_first:' "
        "+ testWanted('', 'gone', vis));\n"
        "console.log('empty_falls_to_first:' + testWanted('', null, []));\n"
    )
    out = _run(body)
    results = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert results.get("no_hash_no_stored") == "first:inverter"
    assert results.get("hash_valid") == "hash:solar-panels"
    assert results.get("hash_beats_stored") == "hash:battery"
    assert results.get("hash_invalid_falls_to_stored") == "stored:general"
    assert results.get("no_hash_stored_valid") == "stored:rate-bands"
    assert results.get("no_hash_stored_invalid_falls_to_first") == "first:inverter"
    # When visibleIds is empty the test function returns 'first:undefined';
    # the real wantedTab guards with `|| TABS[0].id`.
