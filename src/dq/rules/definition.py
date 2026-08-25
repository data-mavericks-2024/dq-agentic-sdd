"""The rule definition a data engineer authors against (T034).

This is the feature's primary external contract. SC-007 requires that registering a rule produce
findings on the next run **with no change to engine code** — so everything the engine needs to
evaluate a rule has to be expressible here, and nothing here may require a code path that knows
the rule's name.

Shape, per contracts/rule-definition.md::

    rule_key: HCP-NPI-FORMAT
    domain: HCP
    dimension: validity
    severity: HIGH
    owning_function: Master Data Management
    subject_type: record
    parameters: {}
    predicate_sql: |
      SELECT ...

Validation is layered, and the layers are not redundant:

1. Pydantic checks shape and vocabulary here.
2. :mod:`dq.rules.predicates` checks predicate safety here, for a legible error.
3. The ``BEFORE INSERT`` trigger checks it again in the database, which is the enforcement —
   ``dq_author`` can INSERT directly, so anything above this line is advisory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dq.rules.predicates import RuleDefinitionError, validate_predicate

#: Mirrors `dq.domain.dq.DOMAINS`. Spelled as a Literal so mypy checks assignments, with
#: `tests/unit/test_rule_definition.py::test_vocabulary_matches_database` asserting the two cannot
#: drift — a value accepted here but rejected by the CHECK constraint would fail at INSERT, after
#: the registration path has already reported success.
Domain = Literal["SALES", "HCP", "HCO", "PRODUCT", "TERRITORY_ALIGNMENT"]

Dimension = Literal[
    "completeness",
    "uniqueness",
    "validity",
    "referential_integrity",
    "timeliness",
    "consistency",
    "conformity",
]

Severity = Literal["HIGH", "MEDIUM", "LOW"]

#: `record` names a row. `batch` and `source_period` are aggregate subjects, which may have no row
#: at all — a feed that never arrived being the case that forced the distinction.
SubjectType = Literal["record", "batch", "source_period"]

#: Changing any of these appends a new `rule_version`; changing anything else does not, because
#: nothing else alters what a finding *means* (FR-006, contracts/rule-definition.md).
VERSIONED_FIELDS: tuple[str, ...] = ("predicate_sql", "parameters", "severity", "subject_type")


class RuleDefinition(BaseModel):
    """One rule, at one version."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    rule_key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9-]*[A-Z0-9]$")
    domain: Domain
    dimension: Dimension
    severity: Severity
    owning_function: str = Field(min_length=1, max_length=120)
    subject_type: SubjectType
    predicate_sql: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def _parameter_names_are_bindable(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name in value:
            if not name.replace("_", "").isalnum() or not name[0].isalpha():
                raise ValueError(
                    f"parameter name {name!r} is not bindable. Names must be alphanumeric with "
                    f"underscores and start with a letter, because they are matched as `:name` "
                    f"inside predicate_sql."
                )
        return value

    @model_validator(mode="after")
    def _predicate_is_safe(self) -> RuleDefinition:
        """Run the full predicate safety check with this definition's declared parameters."""
        validate_predicate(self.predicate_sql, self.parameters)
        return self

    def versioned_fields(self) -> dict[str, Any]:
        """The subset whose change creates a new version."""
        return {name: getattr(self, name) for name in VERSIONED_FIELDS}

    def differs_semantically_from(
        self, severity: str, subject_type: str, predicate_sql: str, parameters: dict[str, Any]
    ) -> bool:
        """True if this definition would need a new version relative to the given stored values.

        ``predicate_sql`` is compared on normalised whitespace: a reindent is not a semantic change,
        and treating it as one would append a version every time someone ran a formatter over the
        library.
        """
        return (
            self.severity != severity
            or self.subject_type != subject_type
            or _canonical_sql(self.predicate_sql) != _canonical_sql(predicate_sql)
            or self.parameters != parameters
        )


def _canonical_sql(sql: str) -> str:
    return " ".join(sql.split())


def load_definition(path: Path) -> RuleDefinition:
    """Load one rule definition from a YAML file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuleDefinitionError(f"{path.name} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuleDefinitionError(
            f"{path.name} must contain a YAML mapping, got {type(raw).__name__}."
        )

    try:
        return RuleDefinition.model_validate(raw)
    except RuleDefinitionError as exc:
        # Raised by the predicate validator inside a model validator; re-raised with the filename
        # so a library of nine rules says which one is wrong.
        raise RuleDefinitionError(f"{path.name}: {exc}") from exc


def load_library(directory: Path) -> list[RuleDefinition]:
    """Load every ``.yaml`` definition in ``directory``, sorted by rule key."""
    definitions = [load_definition(p) for p in sorted(directory.glob("*.yaml"))]
    keys = [d.rule_key for d in definitions]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise RuleDefinitionError(
            f"duplicate rule_key in {directory}: {', '.join(sorted(duplicates))}. "
            f"A rule key is referenced by findings forever, so it must identify exactly one rule."
        )
    return definitions


#: Where the nine shipped rule families live.
LIBRARY_DIR: Path = Path(__file__).resolve().parent / "library"
