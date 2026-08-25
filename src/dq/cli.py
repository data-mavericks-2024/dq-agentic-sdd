"""Operational entry point (T051).

**Development and operations only. This CLI never reaches a steward machine.** It holds
``dq_migrate``, ``dq_author``, and ``dq_ingest`` credentials, so once Feature 6 puts the approval
gate behind an API, this CLI would be a path around every server-side re-validation constitution
principle XI requires. Stated in writing because the constraint is a decision, not an accident of
packaging.

For Feature 1 it makes no trust decision and gates no approval, so principle XI does not apply to it.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import click

from dq.config.settings import Role, Settings, load_settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, RunScope, ScopeNotFoundError, SourcePeriodScope, run_rules
from dq.rules.definition import LIBRARY_DIR, load_definition, load_library
from dq.rules.predicates import RuleDefinitionError
from dq.rules.registry import register, set_active


def _settings() -> Settings:
    return load_settings()


def _parse_period(value: str) -> tuple[date, date]:
    """``2026-09`` becomes ``[2026-09-01, 2026-10-01)``."""
    try:
        start = datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise click.BadParameter(f"expected YYYY-MM, got {value!r}") from exc
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, end


@click.group()
def main() -> None:
    """Deterministic data-quality engine."""


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


@main.command("seed")
@click.option("--with-defects/--clean", default=True, help="Inject the deliberate defect set.")
@click.option("--show-expected", is_flag=True, help="Print the expected finding set and exit.")
def seed_cmd(with_defects: bool, show_expected: bool) -> None:
    """Generate synthetic commercial data across three consecutive periods."""
    from dq.seed.generator import seed

    settings = _settings()
    result = seed(settings, with_defects=with_defects)

    click.echo(f"sources          : {', '.join(sorted(result.source_ids))}")
    click.echo(f"batches          : {len(result.batches)}")
    for b in result.batches:
        click.echo(f"  batch {b.batch_id:<5} {b.source_code:<10} {b.period_label}")
    click.echo(f"expected findings: {len(result.expected)}")

    if show_expected:
        for rule_key, subject in sorted(result.expected):
            note = result.notes.get((rule_key, subject), "")
            click.echo(f"  {rule_key:<20} {subject:<28} {note}")


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


@main.group("rules")
def rules_group() -> None:
    """Manage the rule registry. Connects as dq_author."""


@rules_group.command("register")
@click.argument("path", type=click.Path(exists=True, dir_okay=False), required=False)
@click.option("--all", "register_all", is_flag=True, help="Register the whole shipped library.")
def rules_register(path: str | None, register_all: bool) -> None:
    """Register one rule definition, or the entire library."""
    if not path and not register_all:
        raise click.UsageError("give a path, or --all")

    settings = _settings()
    definitions = load_library(LIBRARY_DIR) if register_all else [load_definition(Path(str(path)))]

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        for definition in definitions:
            try:
                outcome = register(conn, definition)
            except RuleDefinitionError as exc:
                raise click.ClickException(f"{definition.rule_key}: {exc}") from exc
            click.echo(f"  {outcome.rule_key:<20} v{outcome.version_no}  {outcome.outcome}")


@rules_group.command("deactivate")
@click.argument("rule_key")
def rules_deactivate(rule_key: str) -> None:
    """Stop a rule producing new findings. Historical findings are untouched."""
    settings = _settings()
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        if not set_active(conn, rule_key, False):
            raise click.ClickException(f"no rule with key {rule_key!r}")
    click.echo(f"{rule_key} deactivated. Findings already recorded keep their meaning.")


@rules_group.command("activate")
@click.argument("rule_key")
def rules_activate(rule_key: str) -> None:
    """Resume a deactivated rule."""
    settings = _settings()
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        if not set_active(conn, rule_key, True):
            raise click.ClickException(f"no rule with key {rule_key!r}")
    click.echo(f"{rule_key} activated.")


# ---------------------------------------------------------------------------
# run-rules
# ---------------------------------------------------------------------------


@main.command("run-rules")
@click.option("--batch-id", type=int, help="Evaluate record and batch rules over one arrival.")
@click.option("--source", help="Source system code, with --period.")
@click.option("--period", help="Business period as YYYY-MM, with --source.")
@click.option("--rule", "rule_keys", multiple=True, help="Restrict to these rule keys.")
def run_rules_cmd(
    batch_id: int | None, source: str | None, period: str | None, rule_keys: tuple[str, ...]
) -> None:
    """Evaluate active rules against a scope.

    A scope is either a batch or a (source, period) pair. The second form is what lets the engine
    answer "did the September feed arrive?" — a question with no batch to ask it against.
    """
    if batch_id is not None and (source or period):
        raise click.UsageError("--batch-id and --source/--period are alternatives")
    if batch_id is None and not (source and period):
        raise click.UsageError("give --batch-id, or both --source and --period")

    scope: RunScope
    if batch_id is not None:
        scope = BatchScope(batch_id)
    elif source and period:
        start, end = _parse_period(period)
        scope = SourcePeriodScope(source, start, end)
    else:  # pragma: no cover — the usage checks above make this unreachable
        raise click.UsageError("give --batch-id, or both --source and --period")

    settings = _settings()
    try:
        result = run_rules(settings, scope, rule_keys=list(rule_keys) or None)
    except ScopeNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"run              : {result.rule_run_id}  ({result.correlation_id})")
    click.echo(f"scope            : {result.scope_key}")
    click.echo(f"as-of / watermark: {result.as_of_date}  /  batch {result.reference_watermark}")
    click.echo(f"status           : {result.status}")
    click.echo(f"findings         : {result.finding_count}")
    for outcome in result.outcomes:
        marker = " " if outcome.outcome == "EVALUATED" else "!"
        click.echo(
            f" {marker} {outcome.rule_key:<20} v{outcome.version_no}  "
            f"{outcome.outcome:<10} {outcome.finding_count}"
        )
        if outcome.error_detail:
            click.echo(f"     {outcome.error_detail}")

    # A partially-evaluated scope must not look clean to a script either.
    if result.errored:
        sys.exit(2)


if __name__ == "__main__":
    main()
