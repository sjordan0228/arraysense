"""test_main.py — argument parsing and wiring, without opening a socket.

The entry point is mostly assembly, so what is worth testing is that the
assembly is right: a bad config reports itself instead of crashing, and the
lifespan hook actually stops the collector so the dongle's single slot is
released on the way out.
"""

from __future__ import annotations

from pathlib import Path

from arraysense.__main__ import build_app, build_parser, main
from arraysense.config import Config


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


def test_a_missing_config_reports_itself_and_exits(tmp_path: Path, caplog: object) -> None:
    # A misconfigured service should say what is wrong, not bury it in a
    # traceback.
    code = main(["--config", str(tmp_path / "absent.toml")])
    assert code == 1


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

    store = SqliteStore(str(tmp_path / "s.db"))
    source = FakeSource()
    service = CollectorService(source=source, store=store, interval=3600)
    create_app(store=store, service=service, config=_config(tmp_path))
    await service.start()
    assert service.status.running
    await service.stop()
    store.close()
    assert not service.status.running
    assert not source.connected
