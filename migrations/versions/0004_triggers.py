"""Immutability and predicate-determinism triggers (T019, T024, T025).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24

Three enforcement mechanisms, all in the database rather than in application code, because the
roles that write these tables can reach them directly. A check that lives only in a Python
registration path is a convention, and constitution principle I does not accept conventions.

* **T019** — ``data_batch`` and every batch member table reject UPDATE, DELETE, and TRUNCATE
  (FR-003b). Revision 1 protected only the batch header, which left the contents mutable.
* **T024** — ``rule_version`` and ``finding`` reject the same. They are the audit-bearing tables:
  a finding says what was true at a moment, and a rule version says what "failed" meant at that
  moment.
* **T025** — a ``BEFORE INSERT`` trigger on ``rule_version`` enforcing the eight validation rules
  in contracts/rule-definition.md, plus FR-016d's rejection of similarity matching.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from dq.db.migration_support import assume_migrate_role, execute_raw, phys, reset_role
from dq.db.schemas import Schema
from dq.domain.commercial import BATCH_MEMBER_TABLES
from dq.domain.dq import IMMUTABLE_TABLES

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


REJECT_MUTATION_FN = """
CREATE OR REPLACE FUNCTION @DQ@.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION
        'Table %.% is append-only; % is rejected. A correction is a new row, not an edit.',
        TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$fn$;
"""


# ---------------------------------------------------------------------------
# T025 — predicate validation
# ---------------------------------------------------------------------------
#
# Rules 2, 3, and 5 are checked *exactly*, not lexically. The function builds a probe statement by
# substituting NULL for each bound parameter, creates it as a temporary view, and then reads
# `pg_depend` to learn precisely which functions and which relations PostgreSQL actually resolved.
# That is strictly stronger than pattern-matching the text: it sees through aliases, operators
# (`%` and `<->` resolve to their backing functions), and overload selection, so
# `date_trunc(text, timestamp)` is admitted as IMMUTABLE while `date_trunc(text, timestamptz)` is
# rejected as STABLE — a distinction no name-based check can make.
#
# Creating a view does not execute the query, so no data is read and none can leak through this
# path. The function is SECURITY DEFINER because `dq_author` deliberately has no SELECT on
# `commercial` (T030) and could not otherwise plan a predicate over it.
#
# Rules 1, 4, 6, 7, and 8 remain lexical. Rule 6 in particular is an approximation — see the
# comment at its implementation.

VALIDATE_PREDICATE_FN = """
CREATE OR REPLACE FUNCTION @DQ@.assert_predicate_valid(
    predicate_sql text,
    parameters    jsonb,
    subject_type  text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = @COMMERCIAL@, @DQ@, pg_temp
AS $fn$
DECLARE
    -- Tables a predicate may read. Anything else -- pg_catalog, information_schema, the finding
    -- table itself -- is rejected.
    allowed_tables  text[] := ARRAY[
        'hcp','hco','product','territory','territory_alignment',
        'sales_transaction','data_batch','source_system','feed_expectation'
    ];
    master_tables   text[] := ARRAY['hcp','hco','product','territory'];
    engine_params   text[] := ARRAY['batch_id','as_of_date','reference_watermark'];
    -- Columns unique enough to terminate an ORDER BY. `valid_from` is unique per natural key,
    -- which is what makes the as-of idiom's tie-break total.
    unique_cols     text[] := ARRAY[
        'valid_from','hcp_id','hco_id','product_id','territory_id','alignment_id',
        'txn_id','batch_id','finding_id','rule_version_id','source_key','source_txn_key'
    ];
    banned_similarity text[] := ARRAY[
        'levenshtein','levenshtein_less_equal','similarity','soundex','metaphone',
        'dmetaphone','difference','word_similarity','strict_word_similarity',
        'similarity_op','word_similarity_op','strict_word_similarity_op'
    ];

    stripped     text;
    param_scan   text;
    probe        text;
    view_name    text;
    view_oid     oid;
    node_tree    text;
    cols         text[];
    bad          text;
    params_found text[];
    p            text;
    order_tail   text;
    last_term    text;
    tbl          text;
BEGIN
    IF predicate_sql IS NULL OR btrim(predicate_sql) = '' THEN
        RAISE EXCEPTION 'predicate_sql is empty.' USING ERRCODE = 'check_violation';
    END IF;

    ----------------------------------------------------------------------
    -- Normalise: remove comments and string literals so a keyword inside a
    -- literal cannot trip the lexical checks below.
    ----------------------------------------------------------------------
    stripped := regexp_replace(predicate_sql, '--[^\\n]*', ' ', 'g');
    stripped := regexp_replace(stripped, '/\\*.*?\\*/', ' ', 'gs');
    stripped := regexp_replace(stripped, '''(''''|[^''])*''', ' $LIT$ ', 'g');
    stripped := lower(regexp_replace(stripped, '\\s+', ' ', 'g'));

    ----------------------------------------------------------------------
    -- Rule 1: a single SELECT. No semicolons, no DML, no DDL.
    ----------------------------------------------------------------------
    IF btrim(regexp_replace(stripped, ';\\s*$', '')) LIKE '%;%' THEN
        RAISE EXCEPTION
            'Rule 1 (single statement): predicate_sql contains a semicolon.'
            USING ERRCODE = 'check_violation';
    END IF;

    IF stripped !~ '^\\s*(with|select)\\y' THEN
        RAISE EXCEPTION
            'Rule 1 (single statement): predicate_sql must begin with SELECT or WITH.'
            USING ERRCODE = 'check_violation';
    END IF;

    IF stripped ~ '\\y(insert|update|delete|merge|truncate|create|drop|alter|grant|revoke|copy|call|vacuum|refresh)\\y'
    THEN
        RAISE EXCEPTION
            'Rule 1 (single statement): predicate_sql contains a data- or schema-modifying keyword.'
            USING ERRCODE = 'check_violation';
    END IF;

    ----------------------------------------------------------------------
    -- Rule 3, first half: the keyword-form value functions.
    --
    -- CURRENT_DATE and its relatives parse to a SQLValueFunction node, not a
    -- FuncExpr, so they carry no function OID and the parse-tree scan below
    -- cannot see them. They also take no parentheses, so they do not look
    -- like calls. They are caught here, lexically, or not at all -- and
    -- CURRENT_DATE is the first thing a timeliness-rule author writes.
    ----------------------------------------------------------------------
    IF stripped ~ '\\y(current_date|current_time|current_timestamp|localtime|localtimestamp|current_user|session_user|current_catalog|current_schema)\\y'
    THEN
        RAISE EXCEPTION
            'Rule 3: keyword value functions (CURRENT_DATE, CURRENT_TIMESTAMP, LOCALTIME, '
            'CURRENT_USER and relatives) are not permitted -- they are STABLE, so the same '
            'predicate returns different rows on different days. Bind :as_of_date instead.'
            USING ERRCODE = 'check_violation';
    END IF;

    ----------------------------------------------------------------------
    -- FR-016d: no probabilistic matching. A deterministic
    -- `levenshtein(a,b) < 3` is IMMUTABLE and passes every other check here,
    -- so it needs rejecting by name as well as by volatility.
    ----------------------------------------------------------------------
    FOREACH bad IN ARRAY banned_similarity LOOP
        IF stripped ~ ('\\y' || bad || '\\s*\\(') THEN
            RAISE EXCEPTION
                'FR-016d: similarity function %() is not permitted. Duplicate detection is exact '
                'match on declared keys, never fuzzy.', bad
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;

    IF stripped ~ '<->' OR stripped ~ '\\s%\\s' THEN
        RAISE EXCEPTION
            'FR-016d: the pg_trgm similarity operators (%% and <->) are not permitted.'
            USING ERRCODE = 'check_violation';
    END IF;

    ----------------------------------------------------------------------
    -- Rule 4: no function calls in FROM or JOIN. This is what keeps rule 3
    -- total: `generate_series` is IMMUTABLE, so volatility alone would admit
    -- a synthetic row source.
    ----------------------------------------------------------------------
    IF regexp_replace(stripped, '\\ylateral\\y', ' ', 'g')
       ~ '\\y(from|join)\\s+[a-z_][a-z0-9_]*\\s*\\('
    THEN
        RAISE EXCEPTION
            'Rule 4: a function call appears in FROM or JOIN. Only tables may be row sources.'
            USING ERRCODE = 'check_violation';
    END IF;

    ----------------------------------------------------------------------
    -- Rule 6: total ordering wherever order decides the result.
    --
    -- Approximate by design: this checks that the final ORDER BY term names a
    -- column from `unique_cols`, not that the ordering is provably total. A
    -- full proof needs the parse tree and the constraint set. The
    -- approximation is deliberately strict -- it rejects orderings that might
    -- be fine -- because in the duplicate families ties are the whole subject
    -- matter, and a silently non-total ordering there decides which record
    -- survives.
    ----------------------------------------------------------------------
    IF stripped ~ '\\ylimit\\y'
       OR stripped ~ '\\ydistinct\\s+on\\y'
       OR stripped ~ '\\y(row_number|rank|dense_rank|first_value|last_value|nth_value|lead|lag|ntile)\\s*\\('
    THEN
        IF stripped !~ '\\yorder\\s+by\\y' THEN
            RAISE EXCEPTION
                'Rule 6: LIMIT, DISTINCT ON, or a ranking window function is present without an '
                'ORDER BY. Which row survives would depend on the query plan.'
                USING ERRCODE = 'check_violation';
        END IF;

        order_tail := regexp_replace(stripped, '^.*\\yorder\\s+by\\y', '');
        order_tail := regexp_replace(order_tail, '\\y(limit|offset|fetch)\\y.*$', '');
        order_tail := regexp_replace(order_tail, '\\)\\s*[a-z_][a-z0-9_]*\\s+on\\y.*$', '');
        last_term  := btrim(regexp_replace(order_tail, '^.*,', ''));
        last_term  := btrim(regexp_replace(
                          last_term, '\\y(asc|desc|nulls|first|last)\\y', ' ', 'g'));

        IF NOT EXISTS (
            SELECT 1 FROM unnest(unique_cols) u
            WHERE last_term ~ ('\\y' || u || '\\y')
        ) THEN
            RAISE EXCEPTION
                'Rule 6: the final ORDER BY term (%) does not terminate in a unique key. '
                'Permitted tie-break columns: %.', last_term, array_to_string(unique_cols, ', ')
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    ----------------------------------------------------------------------
    -- Rule 8: every :name is declared or engine-bound.
    --
    -- The cast operator is removed first. Without that, `h.hcp_id::text`
    -- reads as a parameter named `:text` and every well-formed predicate is
    -- rejected -- the cast operator and the parameter marker share a
    -- character.
    ----------------------------------------------------------------------
    param_scan := replace(stripped, '::', ' ');

    SELECT array_agg(DISTINCT m[1]) INTO params_found
    FROM regexp_matches(param_scan, ':([a-z_][a-z0-9_]*)', 'g') AS m;

    IF params_found IS NOT NULL THEN
        FOREACH p IN ARRAY params_found LOOP
            IF NOT (p = ANY(engine_params)) AND NOT (parameters ? p) THEN
                RAISE EXCEPTION
                    'Rule 8: parameter :% is neither declared in `parameters` nor engine-bound '
                    '(engine-bound are: %).', p, array_to_string(engine_params, ', ')
                    USING ERRCODE = 'check_violation';
            END IF;
        END LOOP;
    END IF;

    ----------------------------------------------------------------------
    -- Rule 7: a master-data join binds BOTH as-of parameters.
    --
    -- Omitting :reference_watermark is the most dangerous mistake available in
    -- this contract. The predicate still returns plausible results and still
    -- passes the determinism test, which runs twice within the same minute,
    -- and silently breaks reproducibility for every historical re-run.
    ----------------------------------------------------------------------
    FOREACH tbl IN ARRAY master_tables LOOP
        IF stripped ~ ('\\y' || tbl || '\\y') THEN
            -- No leading `\\y`: a word boundary cannot occur between a space and a colon, both
            -- being non-word characters, so anchoring the front would never match.
            IF param_scan !~ ':as_of_date\\y' OR param_scan !~ ':reference_watermark\\y' THEN
                RAISE EXCEPTION
                    'Rule 7: predicate references master table % but does not bind both '
                    ':as_of_date and :reference_watermark. See the required as-of join idiom in '
                    'contracts/rule-definition.md.', tbl
                    USING ERRCODE = 'check_violation';
            END IF;
            EXIT;
        END IF;
    END LOOP;

    ----------------------------------------------------------------------
    -- Rules 2, 3, 5: exact, via a probe view and pg_depend.
    ----------------------------------------------------------------------
    -- Casts are protected behind a sentinel before parameters are substituted, for the same
    -- reason rule 8 strips them: `h.hcp_id::text` would otherwise have its `:text` replaced,
    -- yielding `h.hcp_id:NULL` and a syntax error on every well-formed predicate.
    --
    -- A `:word` inside a string literal is substituted too. That is a known and accepted
    -- imprecision: it can only turn a valid predicate into an unplannable one, which surfaces as
    -- the clear error below rather than as a wrong verdict.
    probe := replace(predicate_sql, '::', chr(1));
    probe := regexp_replace(probe, ':[a-zA-Z_][a-zA-Z0-9_]*', 'NULL', 'g');
    probe := replace(probe, chr(1), '::');
    probe := regexp_replace(probe, ';\\s*$', '');
    view_name := 'dq_probe_' || replace(gen_random_uuid()::text, '-', '');

    BEGIN
        EXECUTE format('CREATE TEMP VIEW %I AS %s', view_name, probe);
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION
            'predicate_sql could not be planned: % (%). Parameters are substituted with NULL for '
            'this check, so an unresolvable type here usually means an explicit cast is needed.',
            SQLERRM, SQLSTATE
            USING ERRCODE = 'check_violation';
    END;

    SELECT c.oid INTO view_oid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = view_name AND n.nspname LIKE 'pg_temp%';

    -- Rule 2: exactly the four contract columns.
    SELECT array_agg(a.attname ORDER BY a.attname) INTO cols
    FROM pg_attribute a
    WHERE a.attrelid = view_oid AND a.attnum > 0 AND NOT a.attisdropped;

    IF cols IS DISTINCT FROM
       ARRAY['expected_value','observed_value','offending_value','subject_key']
    THEN
        EXECUTE format('DROP VIEW %I', view_name);
        RAISE EXCEPTION
            'Rule 2: predicate must project exactly subject_key, offending_value, '
            'observed_value, expected_value. Got: %.', array_to_string(cols, ', ')
            USING ERRCODE = 'check_violation';
    END IF;

    -- The view's rewrite rule, as a parse tree.
    --
    -- Read from `pg_rewrite.ev_action` rather than from `pg_depend`, because **PostgreSQL records
    -- no dependency rows for pinned system objects** -- every built-in function and every system
    -- catalog. A pg_depend-based check therefore sees `levenshtein` (an extension function) and is
    -- blind to `now()` and `random()`, which are precisely the ones that matter. The parse tree
    -- carries the OIDs the planner actually resolved, built-ins included.
    SELECT r.ev_action::text INTO node_tree FROM pg_rewrite r WHERE r.ev_class = view_oid;

    -- Rule 3, second half: every resolved function is IMMUTABLE.
    -- `opfuncid` as well as `funcid`, so an operator is judged by the function behind it.
    SELECT string_agg(DISTINCT p.proname || '() is ' ||
               CASE p.provolatile WHEN 's' THEN 'STABLE' ELSE 'VOLATILE' END, ', ')
      INTO bad
    FROM (
        SELECT (m[1])::oid AS fn
        FROM regexp_matches(node_tree, ':(?:funcid|opfuncid) ([0-9]+)', 'g') AS m
    ) refs
    JOIN pg_proc p ON p.oid = refs.fn
    WHERE p.provolatile <> 'i';

    IF bad IS NOT NULL THEN
        EXECUTE format('DROP VIEW %I', view_name);
        RAISE EXCEPTION
            'Rule 3: every function in a predicate must be IMMUTABLE. Found: %. '
            'Note that now() and current_date are STABLE, not VOLATILE -- a rule rejecting only '
            'VOLATILE functions would have admitted them.', bad
            USING ERRCODE = 'check_violation';
    END IF;

    -- Rule 5: only allowlisted tables, in the two rule schemas.
    -- Also from the parse tree, and for the same reason: `pg_catalog` relations are pinned, so a
    -- pg_depend-based check would not see `FROM pg_catalog.pg_class` at all.
    SELECT string_agg(DISTINCT n.nspname || '.' || c.relname, ', ') INTO bad
    FROM (
        SELECT (m[1])::oid AS rel
        FROM regexp_matches(node_tree, ':relid ([0-9]+)', 'g') AS m
    ) refs
    JOIN pg_class c ON c.oid = refs.rel
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.oid <> view_oid
      AND c.relkind IN ('r','v','m','p','f')
      AND (n.nspname NOT IN ('@COMMERCIAL@','@DQ@') OR NOT (c.relname = ANY(allowed_tables)));

    IF bad IS NOT NULL THEN
        EXECUTE format('DROP VIEW %I', view_name);
        RAISE EXCEPTION
            'Rule 5: predicate references relations outside the allowlist: %. Permitted: %.',
            bad, array_to_string(allowed_tables, ', ')
            USING ERRCODE = 'check_violation';
    END IF;

    EXECUTE format('DROP VIEW %I', view_name);
END;
$fn$;
"""


VALIDATE_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION @DQ@.rule_version_validate() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    PERFORM @DQ@.assert_predicate_valid(NEW.predicate_sql, NEW.parameters, NEW.subject_type);
    RETURN NEW;
END;
$fn$;
"""


def _immutable_triggers(schema: str, table: str, dq_schema: str) -> list[str]:
    return [
        f'DROP TRIGGER IF EXISTS trg_{table}_immutable ON "{schema}"."{table}"',
        f'CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON "{schema}"."{table}" '
        f'FOR EACH STATEMENT EXECUTE FUNCTION "{dq_schema}".reject_mutation()',
        f'DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON "{schema}"."{table}"',
        f'CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON "{schema}"."{table}" '
        f'FOR EACH STATEMENT EXECUTE FUNCTION "{dq_schema}".reject_mutation()',
    ]


def upgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)

    commercial = phys(Schema.COMMERCIAL)
    dq = phys(Schema.DQ)

    def define(sql: str) -> None:
        """Create a function with its ``%`` placeholders intact — see :func:`execute_raw`."""
        execute_raw(conn, sql.replace("@DQ@", dq).replace("@COMMERCIAL@", commercial))

    define(REJECT_MUTATION_FN)

    # T019 — the batch header and every batch member table.
    for table in ("data_batch", *BATCH_MEMBER_TABLES):
        for stmt in _immutable_triggers(commercial, table, dq):
            conn.execute(text(stmt))

    # T024 — the audit-bearing tables.
    for table in IMMUTABLE_TABLES:
        for stmt in _immutable_triggers(dq, table, dq):
            conn.execute(text(stmt))

    # T025 — predicate determinism.
    define(VALIDATE_PREDICATE_FN)
    define(VALIDATE_TRIGGER_FN)
    conn.execute(text(f'DROP TRIGGER IF EXISTS trg_rule_version_validate ON "{dq}".rule_version'))
    conn.execute(
        text(
            f'CREATE TRIGGER trg_rule_version_validate BEFORE INSERT ON "{dq}".rule_version '
            f'FOR EACH ROW EXECUTE FUNCTION "{dq}".rule_version_validate()'
        )
    )

    reset_role(conn)


def downgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)

    commercial = phys(Schema.COMMERCIAL)
    dq = phys(Schema.DQ)

    conn.execute(text(f'DROP TRIGGER IF EXISTS trg_rule_version_validate ON "{dq}".rule_version'))
    conn.execute(text(f'DROP FUNCTION IF EXISTS "{dq}".rule_version_validate()'))
    conn.execute(text(f'DROP FUNCTION IF EXISTS "{dq}".assert_predicate_valid(text, jsonb, text)'))

    for schema, tables in (
        (commercial, ("data_batch", *BATCH_MEMBER_TABLES)),
        (dq, IMMUTABLE_TABLES),
    ):
        for table in tables:
            conn.execute(
                text(f'DROP TRIGGER IF EXISTS trg_{table}_immutable ON "{schema}"."{table}"')
            )
            conn.execute(
                text(f'DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON "{schema}"."{table}"')
            )

    conn.execute(text(f'DROP FUNCTION IF EXISTS "{dq}".reject_mutation()'))
    reset_role(conn)
