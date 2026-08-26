"""Predicate safety validation, mirroring the database trigger (T035).

**The trigger in migration 0004 is the enforcement; this module is the error message.**

``dq_author`` holds INSERT on ``rule_version`` and can reach the table directly, so a check living
only here would be a convention — and constitution principle I does not accept conventions. What
this module buys is a readable failure at registration time, naming the rule and the offending
fragment, instead of a PL/pgSQL exception surfacing through the driver.

**It is deliberately weaker than the trigger, in one specific way.** The trigger checks rules 2, 3,
and 5 by building the predicate as a temporary view and reading its parse tree, so it sees exactly
which functions and relations PostgreSQL resolved — through aliases, through operators, and through
overload selection. Nothing here can do that without a database connection. Rule 3 in particular
degrades from "every resolved function is IMMUTABLE" to "no name on a known-bad list appears",
which is a denylist, and denylists are incomplete by nature.

That gap is the reason ``tests/integration/test_validation_parity.py`` (T035a) exists: one corpus,
asserted rejected by both. Where they disagree, the trigger wins and this module is wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# The vocabulary the trigger works from. Kept identical on both sides on purpose —
# if one list grows, T035a fails until the other does.
# ---------------------------------------------------------------------------

#: Tables a predicate may read. Anything else — `pg_catalog`, `information_schema`, `finding`
#: itself — is rejected. A rule that read `finding` would be circular.
ALLOWED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "hcp",
        "hco",
        "product",
        "territory",
        "territory_alignment",
        "sales_transaction",
        "data_batch",
        "source_system",
        "feed_expectation",
    }
)

#: Append-only version chains. Joining one without pinning the as-of parameters is rule 7.
MASTER_TABLES: Final[frozenset[str]] = frozenset({"hcp", "hco", "product", "territory"})

#: Bound by the engine on every run; never declared in `parameters`.
#:
#: The three ``scope_*`` names exist because a ``source_period`` rule has no batch to anchor to.
#: Without them a feed rule could not know which source and period it was asked about, so it would
#: evaluate every declared expectation on every run — and the same missing period would re-fire
#: under a fresh ``scope_key`` each time (research.md D4, migration 0006).
ENGINE_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "batch_id",
        "as_of_date",
        "reference_watermark",
        "scope_source_system_id",
        "scope_period_start",
        "scope_period_end",
    }
)

#: Columns unique enough to terminate an ORDER BY. `valid_from` is unique per natural key, which
#: is what makes the as-of idiom's `ORDER BY … LIMIT 1` tie-break total.
UNIQUE_TIEBREAK_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "valid_from",
        "hcp_id",
        "hco_id",
        "product_id",
        "territory_id",
        "alignment_id",
        "txn_id",
        "batch_id",
        "finding_id",
        "rule_version_id",
        "source_key",
        "source_txn_key",
    }
)

#: FR-016d. Duplicate detection is exact match on declared keys, never fuzzy. A deterministic
#: `levenshtein(a, b) < 3` is IMMUTABLE and passes every other check, so it needs rejecting by name.
SIMILARITY_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "levenshtein",
        "levenshtein_less_equal",
        "similarity",
        "soundex",
        "metaphone",
        "dmetaphone",
        "difference",
        "word_similarity",
        "strict_word_similarity",
        "similarity_op",
        "word_similarity_op",
        "strict_word_similarity_op",
    }
)

#: Non-IMMUTABLE functions this module can recognise by name. **This is a denylist and is therefore
#: incomplete** — the trigger's `provolatile` check over the parse tree is the real mechanism.
#: Listed here so the common mistakes fail fast and legibly at registration.
#:
#: Note `now` and `current_date` are STABLE, not VOLATILE. A check that rejected only VOLATILE
#: functions would admit both, and `current_date` is the first function a timeliness-rule author
#: reaches for. That specific miss is why the trigger became an allowlist over IMMUTABLE.
NON_IMMUTABLE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "now",
        "random",
        "clock_timestamp",
        "statement_timestamp",
        "transaction_timestamp",
        "timeofday",
        "gen_random_uuid",
        "uuid_generate_v4",
        "nextval",
        "currval",
        "lastval",
        "age",
        "to_char",
        "to_timestamp",
        "pg_backend_pid",
        "inet_client_addr",
        "txid_current",
        "current_setting",
        "set_config",
    }
)

#: Value functions that carry no parentheses, so no `name(` pattern catches them. `CURRENT_DATE`
#: also produces no function OID in the parse tree — it becomes a `SQLValueFunction` node — so even
#: the trigger has to catch it lexically or not at all.
BARE_VALUE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "current_date",
        "current_time",
        "current_timestamp",
        "localtime",
        "localtimestamp",
        "current_user",
        "session_user",
        "user",
        "current_catalog",
        "current_schema",
    }
)

#: Ranking window functions whose result depends on ordering.
RANKING_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "row_number",
        "rank",
        "dense_rank",
        "first_value",
        "last_value",
        "nth_value",
        "lead",
        "lag",
        "ntile",
    }
)

#: The four columns of the predicate output contract.
CONTRACT_COLUMNS: Final[tuple[str, ...]] = (
    "subject_key",
    "offending_value",
    "observed_value",
    "expected_value",
)

#: Schema names a predicate must never qualify with. Unqualified references are what let the same
#: predicate string resolve against `commercial` in development and a prefixed test schema under
#: test; `pg_catalog` and `information_schema` are the catalog-reading escape hatch rule 5 closes.
FORBIDDEN_QUALIFIERS: Final[frozenset[str]] = frozenset(
    {
        "pg_catalog",
        "information_schema",
        "pg_temp",
        "public",
        "commercial",
        "dq",
        "workflow",
        "audit",
        "sandbox",
    }
)

_DML = re.compile(
    r"\b(insert|update|delete|merge|truncate|create|drop|alter|grant|revoke|copy|call|vacuum|refresh)\b"
)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'")
_CAST = re.compile(r"::\s*[a-z_][a-z0-9_]*(\s*\[\s*\])?", re.I)
_PARAM = re.compile(r":([a-z_][a-z0-9_]*)", re.I)
_FN_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(")
_ORDER_BY = re.compile(r"\border\s+by\b")
_QUALIFIED = re.compile(r"\b([a-z_][a-z0-9_]*)\.[a-z_*]")
_IDENT = re.compile(r"[a-z_][a-z0-9_.]*")
_CLAUSE_END = re.compile(
    r"\b(where|group\s+by|order\s+by|having|limit|offset|fetch|window|union|intersect|except|on|using)\b"
)


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` at parenthesis depth zero.

    ``ORDER BY coalesce(a, b), c`` has two terms, not three — a naive split finds three and picks
    the wrong one as the tie-break.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _scan_to_depth_close(text: str, start: int) -> str:
    """Return ``text`` from ``start`` up to the paren that closes the enclosing group.

    This is what stops rule 6 reading past the end of an ``OVER (… ORDER BY x)`` clause and finding
    a unique column further down the predicate that has nothing to do with that ordering.
    """
    depth = 0
    out: list[str] = []
    for ch in text[start:]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        out.append(ch)
    return "".join(out)


def _cte_names(lowered: str) -> set[str]:
    """Names bound by a ``WITH`` clause.

    A CTE is a row source that is not a table, so the allowlist must not be applied to it. The
    trigger has no equivalent problem: it reads relation OIDs out of the parse tree, and a CTE has
    none — which is exactly the kind of divergence T035a exists to catch.
    """
    if not re.match(r"^\s*with\b", lowered):
        return set()
    return {m.group(1) for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\s+as\s*\(", lowered)}


def _table_references(lowered: str) -> list[str]:
    """Every relation named as a row source, including comma-joined ones.

    Reading only the identifier straight after ``FROM`` misses ``FROM hcp h, pg_catalog.pg_class``
    entirely — the second table is a row source with equal reach and no keyword in front of it.
    """
    ctes = _cte_names(lowered)
    refs: list[str] = []
    for match in re.finditer(r"\b(from|join)\b", lowered):
        clause = _scan_to_depth_close(lowered, match.end())
        # A comma-joined list only continues through the FROM clause itself; JOIN takes one item.
        clause = _CLAUSE_END.split(clause)[0]
        items = _split_top_level(clause) if match.group(1) == "from" else [clause]
        for item in items:
            item = re.sub(r"\blateral\b", " ", item).strip()
            if not item or item.startswith("("):
                # A subquery or LATERAL derived table; its own FROM is scanned by this same loop.
                continue
            ident = _IDENT.match(item)
            if ident and ident.group(0) not in ctes:
                refs.append(ident.group(0))
    return refs


class RuleDefinitionError(ValueError):
    """A predicate failed validation. The message names the rule and the fragment."""


@dataclass(frozen=True, slots=True)
class Normalised:
    """A predicate with comments, string literals, and casts removed.

    Stripping literals first is what stops a keyword *inside* a string tripping the lexical checks —
    a rule matching on the literal ``'DROP TABLE'`` is legitimate and must not be read as DDL.

    Casts are stripped separately because ``::`` and ``:name`` share a character: without this,
    ``h.hcp_id::text`` scans as a parameter named ``text`` and rule 8 rejects every well-formed
    predicate in the library.
    """

    text: str
    lowered: str


def normalise(sql: str) -> Normalised:
    """Strip comments, string literals, and casts; collapse whitespace."""
    stripped = _COMMENT_LINE.sub(" ", sql)
    stripped = _COMMENT_BLOCK.sub(" ", stripped)
    stripped = _STRING_LITERAL.sub(" 'LIT' ", stripped)
    stripped = _CAST.sub(" ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return Normalised(text=stripped, lowered=stripped.lower())


def bound_parameter_names(predicate_sql: str) -> set[str]:
    """Every ``:name`` the predicate actually binds.

    The engine binds only these. Handing SQLAlchemy a parameter the statement does not declare is
    an error, and every rule declares a different subset — an aggregate rule over
    ``feed_expectation`` has no ``:batch_id`` to bind.

    Casts are stripped first: ``h.hcp_id::text`` would otherwise scan as a parameter named ``text``.
    """
    return {m.group(1).lower() for m in _PARAM.finditer(normalise(predicate_sql).text)}


def _fail(rule: str, detail: str) -> None:
    raise RuleDefinitionError(f"{rule}: {detail}")


# ---------------------------------------------------------------------------
# The eight rules of contracts/rule-definition.md, plus FR-016d.
# ---------------------------------------------------------------------------


def _rule_1_single_statement(n: Normalised) -> None:
    body = re.sub(r";\s*$", "", n.lowered).strip()
    if ";" in body:
        _fail("Rule 1 (single statement)", "predicate_sql contains a semicolon.")
    if not re.match(r"^\s*(with|select)\b", body):
        _fail("Rule 1 (single statement)", "predicate_sql must begin with SELECT or WITH.")
    hit = _DML.search(body)
    if hit:
        _fail(
            "Rule 1 (single statement)",
            f"predicate_sql contains the data- or schema-modifying keyword {hit.group(1)!r}.",
        )


def _rule_2_projection(n: Normalised) -> None:
    missing = [c for c in CONTRACT_COLUMNS if not re.search(rf"\bas\s+{c}\b", n.lowered)]
    if missing:
        _fail(
            "Rule 2 (exact projection)",
            f"predicate must project exactly {', '.join(CONTRACT_COLUMNS)}; "
            f"missing an `AS` alias for: {', '.join(missing)}.",
        )


def _rule_3_immutable(n: Normalised) -> None:
    called = {m.group(1) for m in _FN_CALL.finditer(n.lowered)}
    bad = sorted(called & NON_IMMUTABLE_FUNCTIONS)
    if bad:
        _fail(
            "Rule 3 (IMMUTABLE only)",
            f"{', '.join(f'{b}()' for b in bad)} is not IMMUTABLE. Note that now() and "
            f"current_date are STABLE, not VOLATILE — rejecting only VOLATILE would admit them.",
        )
    for name in sorted(BARE_VALUE_FUNCTIONS):
        if re.search(rf"\b{name}\b", n.lowered):
            _fail(
                "Rule 3 (IMMUTABLE only)",
                f"{name.upper()} is not IMMUTABLE and takes no parentheses, so it is caught by "
                f"name or not at all. Bind :as_of_date instead.",
            )


def _rule_4_no_function_in_from(n: Normalised) -> None:
    # `LATERAL` is an identifier-shaped word that legitimately precedes `(`, so it is removed
    # before the check rather than special-cased inside it.
    scan = re.sub(r"\blateral\b", " ", n.lowered)
    hit = re.search(r"\b(?:from|join)\s*,?\s*([a-z_][a-z0-9_]*)\s*\(", scan)
    if hit:
        _fail(
            "Rule 4 (no function in FROM)",
            f"{hit.group(1)}() appears as a row source. Only tables may be row sources — this is "
            f"what keeps rule 3 total, since generate_series() is IMMUTABLE.",
        )


def _rule_5_allowlisted_tables(n: Normalised) -> None:
    # Qualified references are checked across the whole predicate, not only in FROM: a schema name
    # can appear in a column reference, a cast, or a function argument just as easily.
    for match in _QUALIFIED.finditer(n.lowered):
        qualifier = match.group(1)
        if qualifier in FORBIDDEN_QUALIFIERS:
            _fail(
                "Rule 5 (unqualified, allowlisted tables)",
                f"{qualifier!r} is a schema qualifier. Predicates use unqualified names so the "
                f"schema prefix resolves through the engine's pinned search_path — qualifying one "
                f"pins the predicate to a single environment.",
            )

    for ref in _table_references(n.lowered):
        if "." in ref:
            _fail(
                "Rule 5 (unqualified, allowlisted tables)",
                f"{ref!r} is schema-qualified. Predicates use unqualified names so the schema "
                f"prefix can resolve through the engine's pinned search_path.",
            )
        if ref not in ALLOWED_TABLES:
            _fail(
                "Rule 5 (unqualified, allowlisted tables)",
                f"{ref!r} is not an allowlisted table. Permitted: "
                f"{', '.join(sorted(ALLOWED_TABLES))}.",
            )


def _rule_6_total_ordering(n: Normalised) -> None:
    needs_order = (
        re.search(r"\blimit\b", n.lowered)
        or re.search(r"\bdistinct\s+on\b", n.lowered)
        or any(re.search(rf"\b{f}\s*\(", n.lowered) for f in RANKING_FUNCTIONS)
    )
    if not needs_order:
        return

    matches = list(_ORDER_BY.finditer(n.lowered))
    if not matches:
        _fail(
            "Rule 6 (total ordering)",
            "LIMIT, DISTINCT ON, or a ranking window function is present without an ORDER BY. "
            "Which row survives would depend on the query plan.",
        )

    # Every ORDER BY is checked, not just the last one. A ranking function's ordering lives inside
    # its OVER clause, and reading past that clause's closing paren finds unique columns elsewhere
    # in the predicate that have nothing to do with the ordering being checked.
    for match in matches:
        clause = _scan_to_depth_close(n.lowered, match.end())
        clause = re.split(r"\b(?:limit|offset|fetch|window|having)\b", clause)[0]
        last_term = _split_top_level(clause)[-1]
        last_term = re.sub(r"\b(asc|desc|nulls|first|last)\b", " ", last_term)

        if not any(re.search(rf"\b{c}\b", last_term) for c in UNIQUE_TIEBREAK_COLUMNS):
            _fail(
                "Rule 6 (total ordering)",
                f"the final ORDER BY term ({last_term.strip()!r}) does not terminate in a unique "
                f"key. Permitted tie-break columns: "
                f"{', '.join(sorted(UNIQUE_TIEBREAK_COLUMNS))}.",
            )


def _rule_7_as_of_binding(n: Normalised) -> None:
    referenced = sorted(t for t in MASTER_TABLES if re.search(rf"\b{t}\b", n.lowered))
    if not referenced:
        return
    for param in (":as_of_date", ":reference_watermark"):
        if param not in n.lowered:
            _fail(
                "Rule 7 (as-of binding)",
                f"predicate references master table {referenced[0]!r} but does not bind {param}. "
                f"Omitting :reference_watermark is the most dangerous mistake in this contract — "
                f"the predicate still returns plausible results and still passes the determinism "
                f"test, while silently breaking every historical re-run.",
            )

    # Both parameters are present. That is not the same as both being *applied*: a predicate can
    # mention `:reference_watermark` in an unrelated clause and still join master data unbounded,
    # which is precisely the R2 risk the earlier check only half-covered. Require each parameter to
    # be compared against the versioning column it exists to constrain.
    for column, param, why in (
        (
            "valid_from",
            ":as_of_date",
            "which master version was in effect on the business date",
        ),
        (
            "batch_id",
            ":reference_watermark",
            "which deliveries had arrived when the run was made",
        ),
    ):
        if not re.search(rf"\b{column}\s*<=\s*{re.escape(param)}\b", n.lowered):
            _fail(
                "Rule 7 (as-of binding)",
                f"predicate references master table {referenced[0]!r} and mentions {param}, but "
                f"never compares it against {column}. Binding a parameter without applying it to "
                f"the column it constrains leaves the join unbounded — the master reference is "
                f"still resolved against every version, and {why} is not pinned at all. Use the "
                f"as-of idiom in contracts/rule-definition.md.",
            )


def _rule_8b_no_postfix_cast_on_a_parameter(predicate_sql: str) -> None:
    """Reject ``:name::type``. Use ``CAST(:name AS type)``.

    Not one of the eight rules in the contract — added because a predicate written this way is
    accepted by every validation rule and then **cannot be executed**. SQLAlchemy declines to bind
    ``:name`` when a colon follows it, which is how it avoids eating cast operators, so the
    parameter marker survives into the statement and PostgreSQL rejects it at run time.

    The engine records that as ``ERRORED`` and carries on, which is safe but silent: the rule
    contributes nothing for the whole scope and a steward sees a quiet gap rather than a failure.
    Catching it at registration turns a recurring runtime surprise into a one-off authoring error.

    Checked against the raw SQL, not the normalised form, because normalisation strips casts.
    """
    hit = re.search(r":([a-z_][a-z0-9_]*)\s*::", predicate_sql, re.I)
    if hit:
        _fail(
            "Rule 8b (no postfix cast on a bound parameter)",
            f":{hit.group(1)}::… cannot be executed — the parameter is never bound, because a "
            f"colon following `:name` is how the driver distinguishes a cast from a marker. "
            f"Write CAST(:{hit.group(1)} AS <type>) instead.",
        )


def _rule_8_declared_parameters(n: Normalised, parameters: dict[str, object]) -> None:
    for match in _PARAM.finditer(n.text):
        name = match.group(1).lower()
        if name in ENGINE_PARAMS or name in parameters:
            continue
        _fail(
            "Rule 8 (declared parameters)",
            f":{name} is neither declared in `parameters` nor engine-bound "
            f"(engine-bound are: {', '.join(sorted(ENGINE_PARAMS))}).",
        )


def _fr_016d_no_similarity(n: Normalised) -> None:
    called = {m.group(1) for m in _FN_CALL.finditer(n.lowered)}
    bad = sorted(called & SIMILARITY_FUNCTIONS)
    if bad:
        _fail(
            "FR-016d (no probabilistic matching)",
            f"similarity function {bad[0]}() is not permitted. Duplicate detection is exact match "
            f"on declared keys, never fuzzy.",
        )
    if "<->" in n.lowered or re.search(r"\s%\s", n.lowered):
        _fail(
            "FR-016d (no probabilistic matching)",
            "the pg_trgm similarity operators (% and <->) are not permitted.",
        )


def validate_predicate(predicate_sql: str, parameters: dict[str, object] | None = None) -> None:
    """Raise :class:`RuleDefinitionError` unless ``predicate_sql`` satisfies every rule.

    Order matters for the message quality, not the verdict: rule 1 runs first so a predicate that
    is not a single SELECT says so, rather than failing on a downstream rule that is only confused
    by the extra statement.
    """
    if not predicate_sql or not predicate_sql.strip():
        raise RuleDefinitionError("predicate_sql is empty.")

    params = parameters or {}
    n = normalise(predicate_sql)

    _rule_1_single_statement(n)
    _fr_016d_no_similarity(n)
    _rule_2_projection(n)
    _rule_3_immutable(n)
    _rule_4_no_function_in_from(n)
    _rule_5_allowlisted_tables(n)
    _rule_6_total_ordering(n)
    _rule_7_as_of_binding(n)
    _rule_8_declared_parameters(n, params)
    _rule_8b_no_postfix_cast_on_a_parameter(predicate_sql)
