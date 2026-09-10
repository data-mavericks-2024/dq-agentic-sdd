"""Public request-mode contract for explicit historical replay (T081)."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest

from dq.config.settings import Settings
from dq.db import engine as db
from dq.engine import runner


def _error_type(name: str) -> type[BaseException]:
    value = getattr(runner, name, None)
    assert isinstance(value, type), f"dq.engine.runner must export {name}"
    assert issubclass(value, BaseException)
    return value


def test_run_rules_accepts_replay_without_a_scope() -> None:
    signature = inspect.signature(runner.run_rules)

    assert signature.parameters["scope"].default is None


def test_replay_request_errors_are_public_and_distinct() -> None:
    invalid_request = _error_type("InvalidRunRequestError")
    invalid_source = _error_type("ReplaySourceError")

    assert issubclass(invalid_request, Exception)
    assert issubclass(invalid_source, Exception)
    assert invalid_request is not invalid_source


@pytest.mark.parametrize(
    ("scope", "rule_keys", "replay_of"),
    [
        (None, None, None),
        (runner.BatchScope(7), None, 41),
        (None, ["HCP-NPI-FORMAT"], 41),
    ],
)
def test_invalid_request_modes_are_rejected_before_opening_the_database(
    monkeypatch: pytest.MonkeyPatch,
    scope: runner.RunScope | None,
    rule_keys: list[str] | None,
    replay_of: int | None,
) -> None:
    invalid_request = _error_type("InvalidRunRequestError")

    def database_must_not_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("invalid requests must be rejected before database access")

    monkeypatch.setattr(db, "connection", database_must_not_open)
    settings = cast(Settings, SimpleNamespace(schema_prefix=""))
    run_rules = cast(Callable[..., runner.RuleRunResult], runner.run_rules)

    with pytest.raises(invalid_request):
        run_rules(settings, scope, rule_keys=rule_keys, replay_of=replay_of)
