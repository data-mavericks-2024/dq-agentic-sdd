"""CLI request-mode contract for explicit historical replay (T081)."""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dq import cli
from dq.engine.runner import RuleRunResult


def _successful_replay() -> RuleRunResult:
    return RuleRunResult(
        rule_run_id=52,
        correlation_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        scope_key="b:7",
        as_of_date=date(2026, 8, 31),
        reference_watermark=9,
        status="COMPLETED",
    )


def test_cli_replay_requires_no_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, object, list[str] | None, int | None]] = []
    settings = SimpleNamespace()

    monkeypatch.setattr(cli, "_settings", lambda: settings)

    def fake_run_rules(
        actual_settings: object,
        scope: object = None,
        *,
        rule_keys: list[str] | None = None,
        replay_of: int | None = None,
    ) -> RuleRunResult:
        calls.append((actual_settings, scope, rule_keys, replay_of))
        return _successful_replay()

    monkeypatch.setattr(cli, "run_rules", fake_run_rules)

    result = CliRunner().invoke(cli.main, ["run-rules", "--replay-of", "41"])

    assert result.exit_code == 0, result.output
    assert calls == [(settings, None, None, 41)]
    assert "replay of        : 41" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--replay-of", "41", "--batch-id", "7"],
        ["--replay-of", "41", "--source", "VEEVA", "--period", "2026-08"],
        ["--replay-of", "41", "--rule", "HCP-NPI-FORMAT"],
    ],
)
def test_cli_replay_rejects_scope_and_rule_filters(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    called = False

    def fake_run_rules(*args: object, **kwargs: object) -> RuleRunResult:
        nonlocal called
        called = True
        return _successful_replay()

    monkeypatch.setattr(cli, "_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "run_rules", fake_run_rules)

    result = CliRunner().invoke(cli.main, ["run-rules", *arguments])

    assert result.exit_code != 0
    assert "--replay-of cannot be combined" in result.output
    assert not called
