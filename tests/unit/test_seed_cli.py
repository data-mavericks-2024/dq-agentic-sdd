"""CLI request-mode contract for `dq seed --periods` / `--amend-master` (T082).

Covers quickstart Scenario 1 (`dq seed --periods 3 --with-defects`) and Scenario 3
(`dq seed --periods 1 --amend-master`) at the option-parsing level, without a database — the same
split `test_cli_replay.py` makes for `run-rules --replay-of`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from dq import cli
from dq.seed import generator as gen
from dq.seed.generator import Amendment, AmendmentTargetMissingError, SeedResult


def _empty_seed_result() -> SeedResult:
    return SeedResult()


def _amendment() -> Amendment:
    return Amendment(
        batch_id=99,
        watermark_before=12,
        orphan_key="HCP-GHOST-0001",
        product_key="PRD-1-000",
        new_uom="ML",
    )


def test_periods_3_with_defects_calls_the_fresh_seed_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quickstart Scenario 1: `dq seed --periods 3 --with-defects`."""
    calls: list[dict[str, Any]] = []

    def fake_seed(settings: object, *, with_defects: bool = True) -> SeedResult:
        calls.append({"settings": settings, "with_defects": with_defects})
        return _empty_seed_result()

    settings = SimpleNamespace()
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(gen, "seed", fake_seed)

    result = CliRunner().invoke(cli.main, ["seed", "--periods", "3", "--with-defects"])

    assert result.exit_code == 0, result.output
    assert calls == [{"settings": settings, "with_defects": True}]


def test_periods_1_amend_master_calls_the_amendment_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quickstart Scenario 3: `dq seed --periods 1 --amend-master`."""
    calls: list[object] = []

    def fake_amend_master(settings: object) -> Amendment:
        calls.append(settings)
        return _amendment()

    settings = SimpleNamespace()
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(gen, "amend_master", fake_amend_master)

    result = CliRunner().invoke(cli.main, ["seed", "--periods", "1", "--amend-master"])

    assert result.exit_code == 0, result.output
    assert calls == [settings]
    assert "amendment batch  : 99" in result.output
    assert "product amended  : PRD-1-000 -> uom ML" in result.output


def test_amend_master_without_periods_flag_also_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--periods` documents the invocation; `--amend-master` alone is sufficient."""
    monkeypatch.setattr(cli, "_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(gen, "amend_master", lambda settings: _amendment())

    result = CliRunner().invoke(cli.main, ["seed", "--amend-master"])

    assert result.exit_code == 0, result.output


def test_amend_master_rejects_a_periods_value_other_than_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_amend_master(settings: object) -> Amendment:
        nonlocal called
        called = True
        return _amendment()

    monkeypatch.setattr(cli, "_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(gen, "amend_master", fake_amend_master)

    result = CliRunner().invoke(cli.main, ["seed", "--periods", "3", "--amend-master"])

    assert result.exit_code != 0
    assert "--amend-master delivers exactly one period" in result.output
    assert not called


def test_a_fresh_seed_rejects_a_periods_value_other_than_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_seed(settings: object, *, with_defects: bool = True) -> SeedResult:
        nonlocal called
        called = True
        return _empty_seed_result()

    monkeypatch.setattr(cli, "_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(gen, "seed", fake_seed)

    result = CliRunner().invoke(cli.main, ["seed", "--periods", "2"])

    assert result.exit_code != 0
    assert "this generator seeds exactly 3 periods" in result.output
    assert not called


def test_a_missing_prior_seed_is_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_amend_master(settings: object) -> Amendment:
        raise AmendmentTargetMissingError("no source_system 'VEEVA' — run `dq seed` first")

    monkeypatch.setattr(cli, "_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(gen, "amend_master", fake_amend_master)

    result = CliRunner().invoke(cli.main, ["seed", "--amend-master"])

    assert result.exit_code != 0
    assert result.exc_info is None or not issubclass(
        result.exc_info[0], AmendmentTargetMissingError
    )
    assert "run `dq seed` first" in result.output
