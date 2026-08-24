# Feature roadmap

Seven independently specifiable, independently shippable features, ordered by dependency.

Derived from `.specify/memory/constitution.md` v1.0.0. Each feature names the principles it first
puts into force, so the constitution check at plan time has a starting point rather than a blank
page.

**Sequencing logic.** Feature 1 delivers value with zero agents, establishing a deterministic
baseline that the agentic layer is measured against. Features 2–5 add agent capability behind that
baseline, each writing only to proposal, evidence, and audit tables. Feature 6 is the first that can
change curated data, and is therefore the first that needs a server-enforced gate. Feature 7 is the
full operator surface.

---

## Feature 1 — Data foundation & deterministic rule engine

**Scope.** The commercial data model plus a deterministic rule engine. Represents sales
transactions, HCP master, HCO master, product master, and effective-dated territory alignment. A
versioned rule registry declares, per rule: domain, quality dimension (completeness, uniqueness,
validity, referential integrity, timeliness, consistency, conformity), severity, owning business
function, and a deterministic pass/fail predicate. A rule runner executes all active rules for a
given data batch and persists one finding per failing record, capturing the offending value and the
rule version that produced it. Execution is idempotent and re-runnable for any historical batch.
Ships with seeded synthetic data containing deliberately injected defects of every rule family.

**Out of scope.** Any AI agent. Any model call. Any remediation or correction. Any user interface.
Any HTTP API. Issue clustering (findings remain individual). Impact assessment.

**User-visible outcome.** A data engineer can register a rule and run it against a batch. A data
steward can query findings by domain, rule, severity, batch, and time window, and read a per-batch
summary of failures. Against the seeded dataset, a full run detects exactly the injected defects
with no false positives.

**Depends on.** Nothing.

**Principles first enforced.** I (determinism first — this feature *is* the deterministic layer),
VI (the four database roles and their grants are created here), X (rule-level testability).

---

## Feature 2 — Issue triage & prioritization

**Scope.** Findings are not the unit of work; issues are. This feature clusters related findings
into issues, scores them for severity and priority, and introduces the first orchestrated workflow
with durable state. Clustering must have an explicit, defensible definition of "the same issue" —
this is the feature's principal design risk and the main thing clarification must resolve. Scoring
combines rule severity, record count, business-function ownership, and recency.

**Out of scope.** Root-cause reasoning. Impact assessment. Any correction. Any user interface. Any
model-driven clustering decision that cannot be explained deterministically.

**User-visible outcome.** A steward sees a prioritized queue of issues rather than an undifferentiated
list of thousands of findings, with the rationale for each issue's rank inspectable.

**Depends on.** Feature 1 (findings must exist to cluster).

**Principles first enforced.** VIII (control flow lives in explicit graph nodes), IX (workflow state
is checkpointed and resumable), V (the audit trail begins here, since this is the first workflow).

---

## Feature 3 — Root-cause investigation agent

**Scope.** For a prioritized issue, an autonomous tool-using agent investigates why the failure
occurred and produces one to five ranked root-cause hypotheses. Each hypothesis states its causal
mechanism, a confidence score, the evidence IDs supporting it, and the evidence that would confirm
or refute it. The agent autonomously chooses and sequences read-only investigative actions —
profiling the failing column, comparing against prior loads, inspecting source-system and load-batch
metadata, checking for master-data records under alternate keys, checking alignment effective dating,
retrieving how the same pattern was resolved historically. It distinguishes source-system defects,
ingestion/transformation defects, timing effects, legitimate business change, and rule
misconfiguration. It terminates deterministically under bounded tool calls, wall-clock, and token
cost, returning a partial report on exhaustion rather than failing.

**Out of scope.** Proposing or applying any correction. Impact assessment. Human approval. Any user
interface. Any write outside the evidence and audit tables.

**User-visible outcome.** A steward opens an issue and reads a ranked explanation of why it happened,
every claim traceable to a persisted evidence record. Critically, the agent can conclude "the data is
correct, this alert is not actionable" and "the rule is wrong, not the data."

**Depends on.** Feature 2 (needs prioritized issues), Feature 1 (needs findings and data to query).

**Principles first enforced.** III (evidence-bound reasoning — the defining principle of this
feature), VII (PII/PHI masking, since this is the first feature to put commercial data into a
prompt), II (agents write only to evidence and audit tables), X (the golden scenario set, including
the two negative cases, is built here).

---

## Feature 4 — Business impact assessment agent

**Scope.** Quantifies the downstream blast radius of an issue. Traces lineage from affected records
to the reports, KPIs, territories, and time periods that consume them, and expresses impact in
business terms — which territories' numbers are wrong, by how much, and for which periods. Produces
a structured, evidence-cited impact assessment that feeds prioritization back into Feature 2 and
gives the steward a basis for deciding whether a correction is worth making.

**Out of scope.** Proposing corrections. Approval. Any user interface. Repairing lineage metadata
that does not exist — if lineage is unavailable for a domain, the assessment must say so explicitly
rather than estimate.

**User-visible outcome.** A steward sees "this affects Q3 sales for 14 territories, roughly 2.3% of
reported units" rather than "417 rows failed."

**Depends on.** Feature 3 (impact is assessed on an investigated issue), Feature 1 (lineage anchors
to the commercial model).

**Principles first enforced.** III extended to impact claims — an unciteable impact number is
rejected exactly as an unciteable hypothesis is.

---

## Feature 5 — Remediation recommendation & sandbox validation

**Scope.** Generates candidate corrections for an investigated issue and validates each against an
isolated sandbox schema before any human sees it. The simulation reports rows affected, rules that
flip from fail to pass, rules that regress from pass to fail, and referential-integrity effects.
Safety gates reject proposals that regress any rule, exceed a blast-radius threshold, or touch
records outside the issue's scope. A proposal with no attached validation result cannot be surfaced.

**Out of scope.** Applying anything to curated data. Human approval. Any user interface. Any write
under a role that can reach curated tables.

**User-visible outcome.** Each issue carries one or more concrete, simulated proposals, each showing
exactly what it would fix and what it would break — before anyone is asked to approve it.

**Depends on.** Feature 3 (a correction requires a diagnosed cause), Feature 4 (blast radius informs
the safety gates), Feature 1 (rules must be re-runnable against the sandbox).

**Principles first enforced.** IV (simulate before propose — the defining principle of this feature),
VI extended (the sandbox-write role is exercised for the first time here).

---

## Feature 6 — Human-in-the-loop approval & governed publish

**Scope.** The first feature that can change curated commercial data. A durable workflow interrupt
holds the run at the approval point; a steward's decision resumes it. Approval, rejection, and
publish are server-enforced endpoints that independently re-validate steward authority, proposal
freshness (the underlying data must not have changed since simulation), and constitution compliance.
Publishing runs under the dedicated publish role, invoked only after approval, and writes an
immutable audit record linking the published change to the evidence, the simulation, and the
approving steward.

**Out of scope.** The full steward dashboard. Trace visualization. Observability instrumentation.
Bulk or batch approval. Any approval rule enforced in the client.

**Where the line is drawn on UI.** This feature includes the **thinnest possible approval page** —
a list of pending proposals, the simulation result for each, and approve/reject with a required
justification field. Nothing else. It exists because an approval gate with no way to approve is not
shippable. Every rule it appears to enforce is in fact enforced server-side; the page is a
rendering of state and a capture of intent, nothing more. Everything richer — filtering, search,
trace views, dashboards, metrics — is Feature 7.

**User-visible outcome.** A steward reviews a simulated proposal, approves it with justification, and
the correction is published to curated data under a governed, fully auditable path. A stale or
unauthorized approval is rejected.

**Depends on.** Feature 5 (there must be a validated proposal to approve), Feature 2 (the interrupt
lives in the workflow), Feature 1 (curated tables and the publish role).

**Principles first enforced.** II (the approval gate becomes real), XI (the UI is not a trust
boundary), IX extended (approval freshness validated on resume), V extended (the trail must now
reconstruct a *published* change end to end).

---

## Feature 7 — Dashboard, API surface & observability

**Scope.** The full operator surface. A steward dashboard over the complete backend API: issue
queue with filtering and search, investigation detail with evidence drill-down, impact views,
proposal history, and agent trace views showing every tool call and decision for a given
correlation ID. Instrumentation for cost, latency, and agent quality metrics, with defined SLOs and
regression thresholds.

**Out of scope.** New agent capability. New rules. Any change to the approval semantics established
in Feature 6.

**User-visible outcome.** A steward runs their whole workflow from one place. An engineer can answer
"what did the agent do, what did it cost, and how long did it take" for any issue.

**Depends on.** Features 1–6. This is the only feature that depends on all prior ones.

**Principles first enforced.** V completed (the trail becomes *navigable*, not merely stored), XI
extended across every remaining state-changing operation.

---

## Cross-cutting notes

**The negative scenarios are load-bearing.** Constitution principle X requires golden scenarios
where the correct answer is "no action needed" and "the rule is wrong." These are built in Feature 3
and are what separate this system from a dashboard that merely displays failures. An agent that can
only ever confirm a problem will erode steward trust faster than one that is occasionally wrong.

**Feature 2's clustering definition is the highest-risk unresolved decision.** "The same issue" has
no obvious correct answer, and two competent engineers will build materially different systems from
the same specification. This must be resolved in clarification, not during implementation.

**Feature 4 depends on lineage that may not exist.** The real downstream reports and KPIs have not
yet been identified. If that information is unavailable when Feature 4 is specified, the feature's
scope must shrink to the lineage that genuinely exists rather than inventing a model of it.

**Nothing before Feature 6 can write to curated data.** Features 1–5 either write to metadata,
evidence, and audit tables, or to the sandbox schema. This is enforced by role grants created in
Feature 1, not by convention.
