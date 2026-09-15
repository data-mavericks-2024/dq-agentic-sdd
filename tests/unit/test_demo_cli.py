from __future__ import annotations

from click.testing import CliRunner
from pytest import MonkeyPatch

from dq import cli


def test_demo_commands_are_discoverable() -> None:
    result = CliRunner().invoke(cli.main, ["demo", "--help"])

    assert result.exit_code == 0
    assert "prepare" in result.output
    assert "serve" in result.output


def test_demo_serve_delegates_without_loading_a_database(monkeypatch: MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_serve(*, host: str, port: int) -> None:
        calls.append((host, port))

    monkeypatch.setattr("dq.demo.server.serve", fake_serve)

    result = CliRunner().invoke(
        cli.main,
        ["demo", "serve", "--host", "127.0.0.1", "--port", "8123"],
    )

    assert result.exit_code == 0
    assert calls == [("127.0.0.1", 8123)]
