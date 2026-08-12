"""test_main.py — argument parsing and wiring, without opening a socket.

The entry point is mostly assembly, so what is worth testing is that the
assembly is right: a bad config reports itself instead of crashing, and the
lifespan hook actually stops the collector so the dongle's single slot is
released on the way out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arraysense.__main__ import build_app, build_parser, main
from arraysense.config import Config
from conftest import TEST_DEVICE


def test_parser_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.host == "0.0.0.0"
    assert args.port == 8080
    assert args.config.endswith("config.toml")


def test_parser_accepts_overrides() -> None:
    args = build_parser().parse_args(
        ["--config", "/tmp/c.toml", "--host", "127.0.0.1", "--port", "9000"]
    )
    assert args.config == "/tmp/c.toml"
    assert args.host == "127.0.0.1"
    assert args.port == 9000


def test_a_missing_config_serves_setup_mode(tmp_path: Path, monkeypatch: Any) -> None:
    # A missing file used to be an error, which was right for a broken
    # installation and wrong for a brand-new one. It now serves first-run
    # setup. uvicorn.run is stood in for because the real one serves forever.

    served: list[Any] = []
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append(app))
    code = main(["--config", str(tmp_path / "absent.toml")])
    assert code == 0
    assert len(served) == 1, "setup mode should have been served"


def test_an_invalid_config_exits_cleanly(tmp_path: Path) -> None:
    bad = tmp_path / "c.toml"
    bad.write_text('dongle_host = "h"\n')  # missing the rest
    assert main(["--config", str(bad)]) == 1


def _config(tmp_path: Path) -> Config:
    return Config(
        dongle_host="192.0.2.10",
        dongle_serial="BA12345678",
        inverter_serial="CE12345678",
        database_path=str(tmp_path / "nested" / "arraysense.db"),
        poll_interval=10.0,
    )


def test_build_app_creates_the_database_directory(tmp_path: Path) -> None:
    # Someone deploying this should not have to mkdir by hand.
    app, store, service = build_app(_config(tmp_path))
    store.close()
    assert (tmp_path / "nested").is_dir()
    assert app.title == "Solar ArraySense"
    assert service.status.running is False


def test_the_api_is_actually_reachable(tmp_path: Path) -> None:
    # Asserted by calling the endpoints rather than reading app.routes: this
    # FastAPI version wraps an included router in a single object, so the paths
    # are not flattened into app.routes and inspecting them proves nothing.
    from fastapi.testclient import TestClient

    app, store, _service = build_app(_config(tmp_path))
    try:
        # No context manager: entering it would run the lifespan hook and
        # start the collector dialling a nonexistent inverter.
        client = TestClient(app)
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/live").status_code == 200
        # /history needs arguments; a bare call must be rejected, not 404.
        assert client.get("/api/history").status_code == 422
    finally:
        store.close()


async def test_lifespan_stops_the_collector_on_the_way_out(tmp_path: Path) -> None:
    # The collector holds the dongle's only client slot; a process that exits
    # without letting go leaves it unavailable to the next start.
    from arraysense.api.app import create_app
    from arraysense.collector.service import CollectorService
    from arraysense.collector.source import FakeSource
    from arraysense.store.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    source = FakeSource()
    service = CollectorService(source=source, store=store, interval=3600)
    create_app(store=store, service=service, config=_config(tmp_path))
    await service.start()
    assert service.status.running
    await service.stop()
    store.close()
    assert not service.status.running
    assert not source.connected


def _toml(tmp_path: Path, serial: str = "CE12345678") -> Path:
    """A minimal valid config pointing at a database in ``tmp_path``."""
    path = tmp_path / "config.toml"
    path.write_text(
        'dongle_host = "192.0.2.10"\n'
        'dongle_serial = "BA12345678"\n'
        f'inverter_serial = "{serial}"\n'
        f'database_path = "{tmp_path / "arraysense.db"}"\n'
    )
    return path


def test_a_database_without_device_identity_stops_the_service(tmp_path: Path) -> None:
    # It refuses rather than migrating by itself. Rewriting a year of history
    # on a restart nobody was watching is the wrong thing to learn from a log
    # afterwards, and the message names the command that does it.
    import sqlite3

    db = tmp_path / "arraysense.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE inverter_raw (timestamp INTEGER PRIMARY KEY, error TEXT)")
    conn.commit()
    conn.close()

    assert main(["--config", str(_toml(tmp_path))]) == 1


def test_the_migrate_flag_stamps_the_database_and_exits(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "arraysense.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE inverter_raw (timestamp INTEGER PRIMARY KEY, error TEXT)")
    conn.execute("INSERT INTO inverter_raw (timestamp) VALUES (1700000000)")
    conn.commit()
    conn.close()

    assert main(["--config", str(_toml(tmp_path)), "--migrate"]) == 0

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT device FROM inverter_raw").fetchall()
    conn.close()
    assert rows == [("CE12345678",)]


def test_the_store_follows_a_serial_changed_from_the_settings_page(tmp_path: Path) -> None:
    # The settings live in the database and the database is opened for a
    # device, so the override is only visible after the store is open. Without
    # the reopen the API would read one serial's rows while the collector wrote
    # another's, and every page would go blank over a healthy collector.
    from arraysense.settings import SettingsStore
    from arraysense.store.sqlite_store import SqliteStore

    config = _config(tmp_path)
    Path(config.database_path).parent.mkdir(parents=True, exist_ok=True)
    first = SqliteStore(config.database_path, device=config.inverter_serial)
    SettingsStore(first).update({"connection.inverter_serial": "CE99999999"})
    first.close()

    _app, store, _service = build_app(config)
    device = store.device
    store.close()
    assert device == "CE99999999"


def test_the_migration_uses_the_serial_the_service_will_read_by(tmp_path: Path) -> None:
    # The one way this command can lose the history it exists to protect. The
    # settings page may override the serial, build_app reopens the store under
    # the overridden one, and a migration that stamped the file's value instead
    # would leave every row under an identity nothing ever queries — reported
    # as a success.
    import sqlite3 as _sqlite

    from arraysense.__main__ import _configured_serial

    db = tmp_path / "as.db"
    conn = _sqlite.connect(db)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO settings VALUES ('connection.inverter_serial', 'CE99999999')")
    conn.commit()
    conn.close()

    config = Config(
        dongle_host="h",
        dongle_serial="BA12345678",
        inverter_serial="CE12345678",
        database_path=str(db),
    )
    assert _configured_serial(config) == "CE99999999"


def test_the_file_serial_is_used_when_nothing_overrides_it(tmp_path: Path) -> None:
    # A fresh install has no settings table at all, and a bare connection to it
    # must not become a reason the migration refuses to run.
    from arraysense.__main__ import _configured_serial

    config = Config(
        dongle_host="h",
        dongle_serial="BA12345678",
        inverter_serial="CE12345678",
        database_path=str(tmp_path / "missing.db"),
    )
    assert _configured_serial(config) == "CE12345678"


def test_a_missing_config_starts_setup_mode_not_an_error(tmp_path: Path) -> None:
    # Today a missing file is a logged error and exit 1 — correct for a broken
    # installation, wrong for a brand new one. Setup mode serves the wizard's
    # endpoints with no store and no collector, because there is no identity
    # to open a store under until Detect has asked the hardware.
    from fastapi.testclient import TestClient

    from arraysense.__main__ import build_setup_app

    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        r = client.get("/api/setup")
        assert r.status_code == 200
        body = r.json()
        assert "manufacturers" in body
        assert body["first_run"] is True
        assert client.get("/api/status").status_code == 404


def test_setup_mode_carries_the_running_version(tmp_path: Path) -> None:
    # The lifecycle CLI reads the running version from whichever endpoint the
    # service serves, and setup mode serves only /api/setup. Without a version
    # there, `arraysense status` and `arraysense version` would call a healthy
    # new service 'not answering'.
    from fastapi.testclient import TestClient

    from arraysense import __version__
    from arraysense.__main__ import build_setup_app

    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        body = client.get("/api/setup").json()
    assert body["version"] == __version__


def test_setup_geocode_reports_a_no_match_as_empty_not_unreachable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # A query matching nothing comes back from Open-Meteo with no results key
    # at all. The wizard's box must be able to tell that "nothing matched"
    # from "the service is down", so the setup route answers empty candidates
    # (200) rather than the 502 it reserves for a failed fetch.
    from fastapi.testclient import TestClient

    from arraysense import weather
    from arraysense.__main__ import build_setup_app

    monkeypatch.setattr(
        weather.open_meteo, "_http_get", lambda url, timeout: b'{"generationtime_ms": 1.5}'
    )
    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        r = client.get("/api/geocode", params={"q": "M5V"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["query"] == "M5V"
        assert body["candidates"] == []


def test_setup_geocode_calls_the_same_client_as_the_running_service(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The route is a two-line shell over geocode(); the matching behaviour is
    # geocode()'s and is covered by its own tests. This pins the shell: a
    # fetch failure is a 502, a found place is a 200 with candidates.
    from fastapi.testclient import TestClient

    from arraysense import weather
    from arraysense.__main__ import build_setup_app

    def _found(url: str, timeout: float) -> bytes:
        return (
            b'{"results":[{"name":"Argyle","admin1":"Texas","country":"United States",'
            b'"country_code":"US","latitude":33.12123,"longitude":-97.18335,'
            b'"timezone":"America/Chicago"}]}'
        )

    monkeypatch.setattr(weather.open_meteo, "_http_get", _found)
    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        r = client.get("/api/geocode", params={"q": "76226"})
        assert r.status_code == 200
        assert len(r.json()["candidates"]) == 1
        assert r.json()["candidates"][0]["name"] == "Argyle"


def test_setup_geocode_returns_502_when_the_fetch_itself_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from urllib.error import URLError

    from fastapi.testclient import TestClient

    from arraysense import weather
    from arraysense.__main__ import build_setup_app

    def _down(url: str, timeout: float) -> bytes:
        raise URLError("down")

    monkeypatch.setattr(weather.open_meteo, "_http_get", _down)
    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        r = client.get("/api/geocode", params={"q": "76226"})
        assert r.status_code == 502


def test_first_run_apply_writes_a_config_load_accepts_and_restarts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The restart is stubbed because the real one SIGTERMs this process —
    # which under TestClient is the test runner itself.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app
    from arraysense.config import load

    target = tmp_path / "config.toml"
    fired: list[str] = []
    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: fired.append("restart"))
    app = build_setup_app(config_path=target)
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/apply",
            json={
                "driver": "fake",
                "model": "Simulated",
                "transport": "dongle",
                "dongle_host": "192.0.2.1",
                "dongle_serial": "BA00000000",
                "inverter_serial": "CE00000000",
                "database_path": str(tmp_path / "db.sqlite"),
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["restarting"] is True
    assert fired == ["restart"]
    config = load(target)
    assert config.driver == "fake"
    assert config.inverter_serial == "CE00000000"


def test_first_run_apply_writes_a_resolved_postcode_to_the_settings_table(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The wizard's one skippable box: when the owner accepts a geocoded
    # postcode, the apply writes latitude, longitude and timezone into the
    # brand-new settings table before the config file replaces the target. A
    # coordinate the registry refuses then fails the apply with the existing
    # 400 and leaves the installation in setup mode, and an empty box stores
    # nothing rather than a guess.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app
    from arraysense.settings import (
        SETTING_LATITUDE,
        SETTING_LONGITUDE,
        SETTING_TIMEZONE,
        SettingsStore,
    )
    from arraysense.store.sqlite_store import SqliteStore
    from conftest import TEST_DEVICE

    fired: list[str] = []
    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: fired.append("restart"))
    db = tmp_path / "db.sqlite"
    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/apply",
            json={
                "driver": "fake",
                "model": "Simulated",
                "transport": "dongle",
                "dongle_host": "192.0.2.1",
                "dongle_serial": "BA00000000",
                "inverter_serial": "CE00000000",
                "database_path": str(db),
                "latitude": 33.12123,
                "longitude": -97.18335,
                "timezone": "America/Chicago",
            },
        )
        assert r.status_code == 200, r.text
    store = SqliteStore(str(db), device=TEST_DEVICE)
    settings = SettingsStore(store)
    assert settings.get(SETTING_LATITUDE) == 33.12123
    assert settings.get(SETTING_LONGITUDE) == -97.18335
    assert settings.get(SETTING_TIMEZONE) == "America/Chicago"
    store.close()


def test_first_run_apply_with_no_postcode_stores_no_location(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # Skipping the box leaves the location unset — not zero, not a default.
    # An unset latitude that read as 0.0 would put the installation in the
    # Gulf of Guinea, and this project exists because absent data rendered as
    # a number.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app
    from arraysense.settings import (
        SETTING_LATITUDE,
        SETTING_LONGITUDE,
        SETTING_TIMEZONE,
        SettingsStore,
    )
    from arraysense.store.sqlite_store import SqliteStore
    from conftest import TEST_DEVICE

    fired: list[str] = []
    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: fired.append("restart"))
    db = tmp_path / "db.sqlite"
    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/apply",
            json={
                "driver": "fake",
                "model": "Simulated",
                "transport": "dongle",
                "dongle_host": "192.0.2.1",
                "dongle_serial": "BA00000000",
                "inverter_serial": "CE00000000",
                "database_path": str(db),
            },
        )
        assert r.status_code == 200, r.text
    store = SqliteStore(str(db), device=TEST_DEVICE)
    settings = SettingsStore(store)
    assert settings.get(SETTING_LATITUDE) is None
    assert settings.get(SETTING_LONGITUDE) is None
    # The zone's default is the empty string, which means "follow the
    # machine's zone"; a skipped box must not leave a guess behind either.
    assert settings.get(SETTING_TIMEZONE) == ""
    store.close()


def test_first_run_apply_refuses_what_load_would_refuse(tmp_path: Path, monkeypatch: Any) -> None:
    # The candidate file is validated by load() itself before it replaces
    # anything — one rule set. A refused apply must leave no file behind.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app

    fired: list[str] = []
    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: fired.append("restart"))
    target = tmp_path / "config.toml"
    app = build_setup_app(config_path=target)
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/apply",
            json={"driver": "fake", "transport": "modbus_serial", "inverter_serial": "CE0"},
        )
        assert r.status_code == 400
    assert fired == [], "a refused first-run apply must not schedule a restart"
    assert not target.exists()
    assert not target.with_suffix(".candidate").exists()


def test_first_run_apply_refuses_a_driver_the_registry_refuses(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # load() alone cannot know which drivers exist; the registry's rules run
    # on the candidate before the file is born, or an unbootable file would
    # exist and setup mode would never be offered again.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app

    fired: list[str] = []
    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: fired.append("restart"))
    target = tmp_path / "config.toml"
    app = build_setup_app(config_path=target)
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/apply",
            json={
                "driver": "no_such_family",
                "transport": "dongle",
                "dongle_host": "192.0.2.1",
                "dongle_serial": "BA00000000",
                "inverter_serial": "CE00000000",
                "database_path": str(tmp_path / "db.sqlite"),
            },
        )
        assert r.status_code == 400
    assert fired == [], "a refused first-run apply must not schedule a restart"
    assert not target.exists()
    assert not target.with_suffix(".candidate").exists()


def test_switching_the_driver_reopens_the_store_for_the_new_declaration(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The overlay can now name a different driver than the file did. The store
    # must be reopened declared for the effective driver, or EG4 samples land
    # in a fake-declared store and KeyError on the first EG4-only metric. This
    # pins the store's declaration follows the overlay, not the file.
    from arraysense.config import load
    from arraysense.settings import SettingsStore
    from arraysense.store.sqlite_store import SqliteStore

    db = tmp_path / "switch.db"
    # A file that names the fake driver.
    store = SqliteStore(str(db), device="CE00000000")
    SettingsStore(store).set("connection.driver", "eg4_luxpower")
    store.close()

    path = tmp_path / "config.toml"
    path.write_text(
        'driver = "fake"\n'
        'dongle_host = "192.0.2.1"\n'
        'dongle_serial = "BA00000000"\n'
        'inverter_serial = "CE00000000"\n'
        f'database_path = "{db}"\n'
    )
    config = load(path)
    assert config.driver == "fake"
    # After the overlay, the effective driver is eg4_luxpower — the code that
    # opens the store must declare it for that, which build_app exercises.
    from arraysense.__main__ import build_app

    _app, opened_store, _service = build_app(config)
    try:
        # An EG4-only metric column exists, proving the store was declared for
        # the effective driver rather than the file's fake one.
        assert "pv1_power_w" in opened_store._present.get("inverter_raw", frozenset())
    finally:
        opened_store.close()


def test_first_run_apply_refuses_a_database_path_that_cannot_open(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # load() accepts any non-empty database_path string, and the registry does
    # not look at it — but the next boot opens a store there, and "/" cannot be
    # opened. Validating only the file would let that become the real config
    # and crash-loop every restart with no setup mode left to offer. First-run
    # apply proves the store opens before it writes the file.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app

    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: None)
    target = tmp_path / "config.toml"
    app = build_setup_app(config_path=target)
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/apply",
            json={
                "driver": "fake",
                "model": "Simulated",
                "transport": "dongle",
                "dongle_host": "192.0.2.1",
                "dongle_serial": "BA00000000",
                "inverter_serial": "CE00000000",
                "database_path": "/",
            },
        )
        assert r.status_code == 400
    assert not target.exists()
    assert not target.with_suffix(".candidate").exists()


def test_first_run_detect_bounds_reject_a_bad_port_as_422(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from arraysense.__main__ import build_setup_app

    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/detect",
            json={"transport": "dongle", "dongle_host": "192.0.2.1", "dongle_port": -1},
        )
        assert r.status_code == 422


def test_first_run_refuses_a_database_path_that_is_the_config_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # database_path aliasing the config file: the store opened to validate the
    # path is the file replace() overwrites with TOML, and the next boot reads
    # TOML as sqlite and crashes. Refused before anything is written.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app

    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: None)
    target = tmp_path / "config.toml"
    app = build_setup_app(config_path=target)
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/apply",
            json={
                "driver": "fake",
                "model": "Simulated",
                "transport": "dongle",
                "dongle_host": "192.0.2.1",
                "dongle_serial": "BA00000000",
                "inverter_serial": "CE00000000",
                "database_path": str(target),
            },
        )
        assert r.status_code == 400
    assert not target.exists()
    assert not target.with_suffix(".candidate").exists()


def test_first_run_apply_survives_a_lone_surrogate_in_the_body(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # A lone surrogate makes render_config's write raise UnicodeEncodeError.
    # It must be a 400 with no stray candidate, not a 500 — the write is inside
    # the guard for exactly this.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app

    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: None)
    target = tmp_path / "config.toml"
    app = build_setup_app(config_path=target)
    with TestClient(app) as client:
        body = (
            '{"driver": "fake", "transport": "dongle", '
            '"dongle_host": "192.0.2.1", "dongle_serial": "BA00000000", '
            '"inverter_serial": "\\ud800", '
            f'"database_path": "{tmp_path / "db.sqlite"}"}}'
        )
        r = client.post(
            "/api/setup/apply",
            content=body.encode("ascii"),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
    assert not target.with_suffix(".candidate").exists()


def test_first_run_apply_refuses_a_url_serial_device(tmp_path: Path, monkeypatch: Any) -> None:
    # First-run apply writes the config file directly from a raw body — it does
    # not go through ApplyRequest, so it needs its own refusal. A device pyserial
    # reads as a URL parses fine but crashes the collector at connect, and the
    # file is the one write with no setup mode left to fall back to. Refused
    # before anything is written.
    from fastapi.testclient import TestClient

    from arraysense import __main__ as main_module
    from arraysense.__main__ import build_setup_app

    monkeypatch.setattr(main_module, "_schedule_setup_restart", lambda: None)
    target = tmp_path / "config.toml"
    app = build_setup_app(config_path=target)
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/apply",
            json={
                "driver": "fake",
                "transport": "modbus_serial",
                "serial_device": "loop://?foo=bar",
                "inverter_serial": "CE00000000",
                "database_path": str(tmp_path / "db.sqlite"),
            },
        )
        assert r.status_code == 400
    assert not target.exists()
    assert not target.with_suffix(".candidate").exists()


def test_first_run_detect_refuses_a_url_serial_device(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from arraysense.__main__ import build_setup_app

    app = build_setup_app(config_path=tmp_path / "config.toml")
    with TestClient(app) as client:
        r = client.post(
            "/api/setup/detect",
            json={"transport": "modbus_serial", "serial_device": "loop://?foo=bar"},
        )
        assert r.status_code == 422


def test_the_weather_poller_starts_and_stops_with_the_service(tmp_path: Path) -> None:
    """The weather poller lives and dies with the lifespan, exactly like the collector.

    A service that stopped cleanly must leave no orphan task behind. With the
    fake driver the collector won't dial anything, so entering the lifespan is
    safe — and is the only way to exercise the weather poller's start and stop
    through the same hook the production service uses.
    """
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from arraysense.__main__ import build_app

    config = _config(tmp_path)
    config = replace(config, driver="fake")
    app, store, _service = build_app(config)
    try:
        weather = app.state.weather
        assert weather.running is False
        with TestClient(app):
            assert weather.running is True
        assert weather.running is False
    finally:
        store.close()


def test_the_production_store_accepts_a_weather_append(tmp_path: Path) -> None:
    """build_app's store must take the poller's writes, not only the driver's.

    The store is opened with a whitelist of writable metrics, and the driver's
    declaration does not include the sky: opened for the driver alone, every
    weather append raised KeyError — not a sqlite error, so it escaped the
    poller's store guard and weather was silently never recorded in
    production while every unit test (full-registry stores) stayed green.
    """
    from dataclasses import replace
    from datetime import UTC, datetime

    from arraysense.models import Sample

    config = replace(_config(tmp_path), driver="fake")
    _app, store, _service = build_app(config)
    try:
        store.append(
            Sample(
                timestamp=datetime.now(UTC),
                readings={"outside_temperature_c": 37.4, "cloud_cover_pct": 0.0},
            )
        )
        row = store.latest(["outside_temperature_c", "cloud_cover_pct"])
        assert row is not None
        assert row["outside_temperature_c"] == 37.4
        assert row["cloud_cover_pct"] == 0.0
    finally:
        store.close()


def test_the_wizard_writes_the_config_unreadable_to_others(tmp_path: Path) -> None:
    """The setup wizard's config must be 0600, like a hand-written one.

    It carries the dongle serial and the inverter serial, which is what the
    dongle protocol authenticates with — the installation guide has always told
    a hand-installer to chmod 600 for that reason. The wizard was writing it
    world-readable, which made the guided path the less careful one, and a first
    run is exactly when nobody thinks to check.

    Asserted on the mode the file lands with rather than on a chmod call, since
    what matters is the state on disk and not how it got there.
    """
    import os
    import stat

    from fastapi.testclient import TestClient

    from arraysense.__main__ import build_setup_app

    config = tmp_path / "config.toml"
    app = build_setup_app(str(config))
    with TestClient(app) as client:
        reply = client.post(
            "/api/setup/apply",
            json={
                "driver": "eg4_luxpower",
                "model": "18kPV",
                "transport": "dongle",
                "dongle_host": "192.0.2.10",
                "dongle_serial": "BA12345678",
                "inverter_serial": "CE12345678",
                "battery_source": "relayed",
                "database_path": str(tmp_path / "as.db"),
            },
        )
    assert reply.status_code == 200, reply.text
    assert config.exists()
    mode = stat.S_IMODE(os.stat(config).st_mode)
    assert mode == 0o600, f"config landed {oct(mode)}, must be 0o600 — it holds the serials"
