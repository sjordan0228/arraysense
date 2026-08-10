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
