"""test_emporia_tokens.py — the Emporia credential, and where it is not.

The refresh token is account access, so these check the two properties that
keep it that way and no more: it survives a round trip, and the file it lands
in is readable by nobody else. The rest is about failure being absence — a
truncated write, a half-written file, a file that never existed all have to
read as "log in again" rather than as a service that will not start.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from arraysense.modules.emporia import tokens


def test_a_saved_token_comes_back(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    tokens.save(path, tokens.TokenSet("id-1", "refresh-1", "2026-08-15T00:00:00+00:00"))
    got = tokens.load(path)
    assert got is not None
    assert got.refresh_token == "refresh-1"
    assert got.refresh_issued == "2026-08-15T00:00:00+00:00"


def test_the_file_is_not_readable_by_anybody_else(tmp_path: Path) -> None:
    # The whole reason this lives outside the database: a database gets shared
    # while diagnosing a problem, and this must not travel with it.
    path = tmp_path / "tok.json"
    tokens.save(path, tokens.TokenSet("id-1", "refresh-1", "2026-08-15T00:00:00+00:00"))
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, must be 0o600"


def test_no_file_is_not_an_error(tmp_path: Path) -> None:
    assert tokens.load(tmp_path / "absent.json") is None


def test_a_corrupt_file_reads_as_absent_rather_than_raising(tmp_path: Path) -> None:
    # A truncated write must mean "log in again", not a service that will not
    # start. Nothing here is irreplaceable — the owner can always log in.
    path = tmp_path / "tok.json"
    path.write_text("{not json")
    assert tokens.load(path) is None


def test_a_file_missing_the_refresh_token_reads_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    path.write_text(json.dumps({"id_token": "id-1"}))
    assert tokens.load(path) is None


def test_clear_removes_it_and_is_safe_to_repeat(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    tokens.save(path, tokens.TokenSet("id-1", "refresh-1", "2026-08-15T00:00:00+00:00"))
    tokens.clear(path)
    assert not path.exists()
    tokens.clear(path)  # must not raise


def test_a_top_level_array_or_string_reads_as_absent(tmp_path: Path) -> None:
    array = tmp_path / "array.json"
    array.write_text(json.dumps([1, 2, 3]))
    assert tokens.load(array) is None
    text = tmp_path / "string.json"
    text.write_text(json.dumps("not-an-object"))
    assert tokens.load(text) is None


def test_a_refresh_token_that_is_not_a_string_reads_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    path.write_text(json.dumps({"refresh_token": 0}))
    assert tokens.load(path) is None


def test_an_empty_refresh_token_reads_as_absent(tmp_path: Path) -> None:
    # This is what keeps the client from ever being handed a blank credential:
    # an empty string would be sent to Cognito, refused, and reported as a
    # rejected login when the real fault is a half-written file.
    path = tmp_path / "tok.json"
    path.write_text(json.dumps({"id_token": "id-1", "refresh_token": ""}))
    assert tokens.load(path) is None


def test_overwriting_keeps_the_file_private(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    tokens.save(path, tokens.TokenSet("id-1", "refresh-1", "2026-08-15T00:00:00+00:00"))
    tokens.save(path, tokens.TokenSet("id-2", "refresh-2", "2026-08-16T00:00:00+00:00"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    got = tokens.load(path)
    assert got is not None
    assert got.refresh_token == "refresh-2"


def test_a_missing_parent_directory_is_created_and_the_file_stays_private(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "dir" / "tok.json"
    tokens.save(path, tokens.TokenSet("id-1", "refresh-1", "2026-08-15T00:00:00+00:00"))
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_directory_where_the_token_should_be_reads_as_absent(tmp_path: Path) -> None:
    assert tokens.load(tmp_path) is None
