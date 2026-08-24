# Proposed MCP server — read-only Postgres exploration

**Status: proposal. Not installed.** Reviewed and installed deliberately, or not at all.

## Why

During spec and plan work it is useful to explore the live schema — list tables, inspect columns and
constraints, sample rows — without hand-writing `psql` invocations for every question. An MCP
Postgres server exposes that as tools.

## The role it must connect as

`dq_readonly`, and nothing else.

This matters more here than in a typical project. Constitution principle 6 requires least-privilege
data access, and principle 2 forbids autonomous writes to curated data. An MCP server holding a
privileged connection would hand every session an unaudited write path into commercial data,
bypassing the FastAPI approval gate entirely. The DB role is the real enforcement — the MCP server
config is only as safe as the credential you give it.

Point it at the **dev** project. Never the production project, and never the `dq_publish` or
`dq_migrate` role.

## Proposed config

Add to `.mcp.json` at the repo root **only after** `dq_readonly` exists and has been verified to
reject writes:

```json
{
  "mcpServers": {
    "supabase-dq-readonly": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres", "$DQ_READONLY_URL"],
      "env": {
        "DQ_READONLY_URL": "${DQ_READONLY_URL}"
      }
    }
  }
}
```

Notes on this shape:

- The connection string comes from the environment, never inline. `.mcp.json` is committed;
  a pasted credential in it is a leaked credential.
- Requires Node.js, which is not currently installed on this machine — check before adopting.
- Use the **pooled** Supabase endpoint here. This server only issues short read-only queries, so
  pooling is appropriate and reduces connection pressure on the dev project.

## Verify before trusting it

Once `dq_readonly` exists, confirm the boundary holds rather than assuming it:

```sql
-- as dq_readonly, every one of these must fail
INSERT INTO commercial.sales (id) VALUES (1);
CREATE TABLE commercial.scratch (id int);
DROP TABLE commercial.sales;
```

If any succeeds, the role is misconfigured and the MCP server must not be installed.

## Alternative worth considering first

Supabase publishes its own MCP server covering project management alongside SQL. It is broader in
scope, which cuts both ways: more capability, more surface. For this project the narrow
read-only-Postgres server is the better fit — the extra management capability has no role in the
SDD workflow and would need its own privilege review.

## Decision

- [ ] Install `@modelcontextprotocol/server-postgres` against `dq_readonly` (dev, pooled)
- [ ] Skip MCP; use allowlisted `psql` against `$DQ_READONLY_URL` instead

Both are defensible. The second needs no Node.js and no new credential surface; it is the safer
default until Feature 1 has real schema worth exploring interactively.
