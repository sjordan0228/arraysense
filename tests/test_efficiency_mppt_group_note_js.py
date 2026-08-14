"""The Efficiency page explains a shared-MPPT row rather than hiding strings.

An owner configured two real strings and needs to know why one row represents
them. The API provides the group members, but dropping the sentence here would
turn that correct arithmetic back into unexplained missing rows.
"""

from __future__ import annotations

from pathlib import Path

EFFICIENCY = (
    Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "efficiency.html"
)
_START = "// >>> eff-mppt-group-note"
_END = "// <<< eff-mppt-group-note"


def test_a_shared_mppt_row_says_the_inverter_reports_one_figure() -> None:
    """The note gives the missing-row change its hardware explanation."""
    text = EFFICIENCY.read_text()
    start = text.index(_START)
    end = text.index(_END)
    note = text[start:end]

    assert "s.members" in note
    assert "share MPPT" in note
    assert "inverter reports one figure" in note
