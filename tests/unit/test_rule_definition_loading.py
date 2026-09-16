"""`load_definition` normalizes every failure to `RuleDefinitionError`, filename included (T088).

No database: the failure this closes is that Pydantic's own `ValidationError` — for a missing
field, an out-of-vocabulary value, or anything else Pydantic itself rejects — reached
`RuleDefinitionError`'s exception boundary, whether through `load_definition`'s CLI caller or
through code that only expects to catch one exception type here. A predicate-safety rejection
(`_predicate_is_safe`, tested exhaustively in `test_rule_definition.py`) already raises
`RuleDefinitionError` directly — but Pydantic wraps that in `ValidationError` too, before it ever
reaches `load_definition`, so the same fix covers both origins in one place.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from dq import cli
from dq.rules.definition import load_definition
from dq.rules.predicates import RuleDefinitionError

VALID_YAML = """
rule_key: TEST-LOADING
domain: HCP
dimension: validity
severity: HIGH
owning_function: Master Data Management
subject_type: record
parameters: {}
predicate_sql: |
  SELECT h.hcp_id::text AS subject_key,
         h.npi          AS offending_value,
         NULL::text     AS observed_value,
         NULL::text     AS expected_value
  FROM   hcp h
  WHERE  h.batch_id    =  :batch_id
    AND  h.valid_from <=  :as_of_date
    AND  h.batch_id   <=  :reference_watermark
    AND  h.npi IS NULL
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_a_valid_file_loads_without_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "valid.yaml", VALID_YAML)

    definition = load_definition(path)

    assert definition.rule_key == "TEST-LOADING"


def test_malformed_yaml_is_a_ruledefinitionerror_naming_the_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "broken-syntax.yaml", "rule_key: [unterminated")

    with pytest.raises(RuleDefinitionError, match=re.escape("broken-syntax.yaml")) as excinfo:
        load_definition(path)

    assert not isinstance(excinfo.value, ValidationError)


def test_a_missing_required_field_is_a_ruledefinitionerror_naming_the_file(tmp_path: Path) -> None:
    """The gap T088 closes: a plain Pydantic shape failure previously reached callers as a raw
    `ValidationError`, with no file name attached — indistinguishable, from a library of nine
    files, as to which one was wrong."""
    missing_domain = VALID_YAML.replace("domain: HCP\n", "")
    path = _write(tmp_path, "missing-field.yaml", missing_domain)

    with pytest.raises(RuleDefinitionError, match=re.escape("missing-field.yaml")) as excinfo:
        load_definition(path)

    assert not isinstance(excinfo.value, ValidationError)
    assert "domain" in str(excinfo.value)


def test_a_value_outside_vocabulary_is_a_ruledefinitionerror_naming_the_file(
    tmp_path: Path,
) -> None:
    bad_domain = VALID_YAML.replace("domain: HCP", "domain: NOT_A_REAL_DOMAIN")
    path = _write(tmp_path, "bad-vocabulary.yaml", bad_domain)

    with pytest.raises(RuleDefinitionError, match=re.escape("bad-vocabulary.yaml")) as excinfo:
        load_definition(path)

    assert not isinstance(excinfo.value, ValidationError)


def test_an_unsafe_predicate_is_also_a_ruledefinitionerror_naming_the_file(tmp_path: Path) -> None:
    """The predicate validator's own rejection, reaching here through the exact same Pydantic
    wrapping as a plain shape failure — proving one fix covers both origins."""
    unsafe = VALID_YAML.replace("h.npi IS NULL", "now() > h.valid_from")
    path = _write(tmp_path, "unsafe-predicate.yaml", unsafe)

    with pytest.raises(RuleDefinitionError, match=re.escape("unsafe-predicate.yaml")) as excinfo:
        load_definition(path)

    assert not isinstance(excinfo.value, ValidationError)


def test_cli_renders_a_loading_failure_without_a_traceback(tmp_path: Path) -> None:
    """The other half of the gap: `rules_register` loaded definitions *before* its own
    try/except, so this exact failure used to reach Click as an uncaught exception."""
    path = _write(tmp_path, "missing-field.yaml", VALID_YAML.replace("domain: HCP\n", ""))

    result = CliRunner().invoke(cli.main, ["rules", "register", str(path)])

    assert result.exit_code != 0
    assert result.exc_info is None or not issubclass(result.exc_info[0], ValidationError)
    assert "missing-field.yaml" in result.output
