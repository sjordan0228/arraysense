"""test_web_page_wiring.py -- a function a page declares is one something calls.

The settings page carried an orphaned comment for eleven releases:

    // Build the tab bar and select the initial tab. Done after the form is
    }
    drawTabs();             <- gone
    selectTab(wantedTab()); <- gone

A commit that meant to drop a paragraph of prose deleted those two calls with it,
and nothing noticed: no exception, no console error, every gate green. The tab bar
simply stopped being drawn and every group stayed visible, so the page quietly
became one long form. The tests that cover that page extract the marked
``tab-defs`` slice and check the tab table; none of them runs the page.

This is the cheap check for that class, and it is the one that would have caught
it: a declared function that nothing references is either dead code to delete or
a call that went missing, and both are worth failing a build over. It reads the
web assets as text — no browser and no node — so it covers every page rather than
only the page that happens to have a harness.

Two rules:

* Every declared function is referenced by name somewhere else in the web tree.
  A handler wired with ``addEventListener('click', name)`` counts, as does a
  function another page calls; only a declaration that nothing ever mentions
  fails.
* Every id a page looks up is defined on that page, in its markup or by a JS
  assignment the page makes itself. A page looking for an element that does not
  exist draws nothing, silently, which is the same failure wearing a different
  hat.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web"

# `function name(`, and the two assignment shapes that declare a function.
DECLARED = (
    re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_][\w$]*)\s*\("),
    re.compile(r"^\s*(?:const|let|var)\s+([A-Za-z_][\w$]*)\s*=\s*(?:async\s*)?(?:function\s*)?\("),
)
# `$('id')`, `document.getElementById('id')`.
LOOKUP = re.compile(r"""\$\(\s*['"]([\w-]+)['"]\s*\)|getElementById\(\s*['"]([\w-]+)['"]\s*\)""")
# The two ways a page can define an id: in markup, or by assigning it in JS.
DEFINED = (
    re.compile(r"""id=["']([\w-]+)["']"""),
    re.compile(r"""\.id\s*=\s*['"]([\w-]+)['"]"""),
)


def _sources() -> dict[str, str]:
    files = sorted([*WEB.glob("*.html"), *WEB.glob("*.js")])
    assert files, f"no web assets under {WEB}"
    return {path.name: path.read_text() for path in files}


SHARED = "common.js"


def _uncalled(files: dict[str, str]) -> list[str]:
    """Declared functions with no call in the place that has to make it.

    A page's own function has to be called by that page: counting mentions across
    the whole tree would let another page mask a missing call, which is not
    hypothetical — graphs.html has a drawTabs() of its own, and a tree-wide count
    passed the settings page while its tab bar was gone. Functions in the shared
    script are the other way round: the pages are the ones that call those, so
    they are counted across everything.
    """
    everything = "\n".join(files.values())
    orphans = []
    for name, text in files.items():
        haystack = everything if name == SHARED else text
        for number, line in enumerate(text.splitlines(), start=1):
            match = next((pattern.match(line) for pattern in DECLARED if pattern.match(line)), None)
            if match is None:
                continue
            called = len(re.findall(rf"\b{re.escape(match.group(1))}\b", haystack))
            if called < 2:
                orphans.append(f"{name}:{number}: {match.group(1)}()")
    return orphans


def _undefined_ids(files: dict[str, str]) -> list[str]:
    """Ids a page looks up that the page never defines."""
    missing = []
    for name, text in files.items():
        if not name.endswith(".html"):
            continue
        defined: set[str] = set()
        for pattern in DEFINED:
            defined.update(pattern.findall(text))
        for number, line in enumerate(text.splitlines(), start=1):
            for first, second in LOOKUP.findall(line):
                wanted = first or second
                if wanted and wanted not in defined:
                    missing.append(f"{name}:{number}: #{wanted}")
    return missing


def test_every_declared_function_is_called_somewhere() -> None:
    """A declaration nothing references is a call that went missing, or code to
    delete. The settings page's tab bar was the first kind."""
    orphans = _uncalled(_sources())
    assert not orphans, (
        "these functions are declared in src/arraysense/web and referenced nowhere "
        "else — either the call went missing, which is how the settings tabs "
        "disappeared, or the function is dead and should go:\n  " + "\n  ".join(orphans)
    )


def test_every_id_a_page_looks_up_is_defined_on_that_page() -> None:
    """An element a page looks for and never finds draws nothing, quietly. The id
    may be in the markup or assigned by the page's own script; what it cannot be
    is absent."""
    missing = _undefined_ids(_sources())
    assert not missing, (
        "these ids are looked up on a page that never defines them, so the code "
        "that needs them does nothing at all:\n  " + "\n  ".join(missing)
    )


def test_the_rule_reports_the_regression_that_prompted_it() -> None:
    """The scanner on the exact edit, so this file cannot rot into a guard that
    only ever says yes.

    Two pages and a shared script, mirroring the real tree: settings declares the
    tab bar and calls it on boot, graphs has a drawTabs of its own — which is the
    function that once masked the missing call — and common.js is where the pages
    legitimately call from. Every name is referenced twice, so the rule is silent;
    then the one call that draws the settings tabs is removed, which is precisely
    what the page lost, and the rule has to report that file and line.
    """
    worked = {
        "settings.html": """<script>
document.addEventListener('DOMContentLoaded', render);
function render() {
  // Build the tab bar and select the initial tab.
  drawTabs();
}
function drawTabs() {
  return 1;
}
</script>
""",
        "graphs.html": """<script>
document.addEventListener('DOMContentLoaded', render);
function render() {
  fade(drawTabs);
}
function drawTabs() {
  return 2;
}
</script>
""",
        "common.js": """function fade(fn) {
  fn();
}
""",
    }
    assert _uncalled(worked) == []

    broken = dict(worked)
    broken["settings.html"] = worked["settings.html"].replace("  drawTabs();\n", "")
    assert _uncalled(broken) == ["settings.html:6: drawTabs()"]
    # And the shared script is judged across the tree, not by its own file: fade
    # is declared there and called from a page, which is not an orphan.
    shared_only = dict(broken)
    shared_only["settings.html"] = worked["settings.html"].replace(
        "function drawTabs() {\n  return 1;\n}\n", ""
    )
    assert _uncalled(shared_only) == []


def test_the_rule_reports_an_id_no_page_defines() -> None:
    """The other half: code looking for an element that is gone."""
    stray = {"page.html": '<div id="here"></div>\n<script>\n$("gone");\n$("here");\n</script>\n'}
    reported = _undefined_ids(stray)
    assert reported == ["page.html:3: #gone"]


def test_the_scan_finds_the_tree_it_claims_to_scan() -> None:
    """A scan that reads no files reports the same clean result as a clean tree.

    A moved directory or a renamed extension would otherwise turn both rules
    above into passes that mean nothing, so the tree is checked for the pages
    this file exists for, and the settings page for the two calls themselves.
    """
    files = _sources()
    assert len(files) >= 9, f"expected the nine pages and the shared script, found {len(files)}"
    assert "settings.html" in files and "common.js" in files
    assert "drawTabs();" in files["settings.html"], "the settings page stopped drawing tabs"
    assert "selectTab(wantedTab());" in files["settings.html"], "no tab is selected on load"
