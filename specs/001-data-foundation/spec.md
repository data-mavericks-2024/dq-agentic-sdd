# Feature Specification: Deterministic Data-Quality Foundation

**Feature Branch**: `001-data-foundation`

**Created**: 2026-08-24

**Status**: Draft

**Input**: User description: "Deterministic data-quality foundation for pharmaceutical commercial data. A data foundation plus a deterministic rule engine that detects data-quality failures across commercial domains: sales transactions, HCP master, HCO master, product master, and territory alignment."

## Clarifications

### Session 2026-08-24

- Q: When you say "re-run the rules for batch X", what exactly is the X — one delivered file, everything a source sent for a business period, or a group of files you explicitly seal as complete? → A: One physical arrival — a single file or load event, timestamped and immutable once recorded.
- Q: What makes two HCP records duplicates — must they share an NPI, or should records matching on name and address with no NPI also be caught? → A: Both, as two separate rules — NPI collision at High severity, deterministic composite match at Medium.
- Q: How does the system know a data feed is missing — is a feed expectation catalogue in scope, or does the schedule come from elsewhere? → A: A feed expectation catalogue is in scope for this feature (source, cadence, delivery window, active date range).
- Q: What data volume must the rule engine be designed to handle? → A: Realistic mid-size — ~1M sales transactions per month, ~100K HCP master records, ~5K products.
- Q: How long may a full rule run over one month of data take before a steward would consider it broken? → A: Under 10 minutes.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Steward sees exactly what failed (Priority: P1)

A data steward opens the previous night's data load and sees, record by record, which
data-quality rules failed and what the offending value was. Instead of "417 problems", they see
"HCP record 88213 has NPI `12345` which is not a structurally valid NPI, flagged by rule
HCP-NPI-FORMAT version 3, severity High, owned by Master Data Management".

**Why this priority**: This is the entire baseline value of the feature and the thing every later
feature builds on. Without trustworthy, record-level, attributable findings, there is nothing for
an investigation capability to investigate, and no measurable baseline against which the value of
later automation can be judged.

**Independent Test**: Load the seeded dataset containing deliberately injected defects, execute a
full rule run, and confirm that every injected defect appears as a finding with its offending value
and that no clean record is flagged. Deliverable value: a steward can act on real findings today,
with no agent, no interface, and no automation.

**Acceptance Scenarios**:

1. **Given** a seeded dataset with one deliberately invalid NPI, **When** a full rule run executes,
   **Then** exactly one finding is recorded, naming the failing record, the offending value, the
   rule, and the rule version applied.
2. **Given** a dataset with no injected defects, **When** a full rule run executes, **Then** zero
   findings are recorded.
3. **Given** a record that violates three separate rules, **When** a full rule run executes,
   **Then** three distinct findings are recorded against that record, one per rule.
4. **Given** a rule whose severity is High and whose owning business function is Commercial
   Operations, **When** it produces a finding, **Then** the finding carries that severity and
   ownership without the steward needing to look the rule up separately.

---

### User Story 2 - Engineer governs the rule catalogue (Priority: P2)

A data engineer adds a new rule, changes the threshold on an existing one, and retires a rule that
is no longer meaningful — without any of those actions silently rewriting the meaning of findings
that were already recorded. Each rule declares which domain it applies to, which quality dimension
it tests, how severe a failure is, and which business function owns the outcome.

**Why this priority**: Rules change constantly in commercial data — thresholds drift, definitions
get corrected, new source systems arrive. Without versioning, a threshold change silently rewrites
history and makes every prior finding unexplainable. This is second only to detection itself
because the constitution requires findings to be reconstructable long after the fact.

**Independent Test**: Register a rule, run it, change its predicate, run it again, and confirm the
two runs' findings are each attributable to the correct rule version and that the earlier findings
still describe what was true when they were produced.

**Acceptance Scenarios**:

1. **Given** an active rule at version 2, **When** its predicate is changed, **Then** a new version 3
   is created and version 2 remains readable for interpreting prior findings.
2. **Given** a rule marked inactive, **When** a full rule run executes, **Then** that rule produces
   no new findings and its historical findings are unaffected.
3. **Given** a new rule declaring domain, dimension, severity, and owning business function,
   **When** it is registered, **Then** it is rejected if any of those four declarations is missing.

---

### User Story 3 - Any historical run can be reproduced (Priority: P3)

A steward re-runs the rules for a data batch from three weeks ago and gets exactly the same findings
as the original run produced — not similar findings, the same ones. Running the same rules over the
same batch twice does not create duplicate findings.

**Why this priority**: This is what makes the system defensible under inspection. It is separable
from P1 and P2 because detection and rule governance are valuable even before reproducibility is
guaranteed, but nothing can be published to a regulator's satisfaction without it.

**Independent Test**: Execute a rule run over a historical batch twice and confirm the finding set
is identical and not duplicated.

**Acceptance Scenarios**:

1. **Given** a batch that has already been evaluated, **When** the same rules are re-run against it,
   **Then** the resulting finding set is identical and no duplicate findings are created.
2. **Given** a batch from an arbitrary past period, **When** a rule run is requested for it,
   **Then** it executes against that batch's data as it exists, without requiring the batch to be
   the most recent one.
3. **Given** a rule run that fails partway through, **When** it is re-executed, **Then** the outcome
   is the same as if it had succeeded on the first attempt.

---

### User Story 4 - Findings can be sliced and summarised (Priority: P4)

A steward filters findings by domain, rule, severity, batch, and time window, and reads a per-batch
summary showing how many failures occurred by domain, by rule, and by severity.

**Why this priority**: Necessary for the feature to be usable at real volume, but genuinely
separable — the record-level findings from P1 have value even when queried crudely. This is the
slice that makes the difference between "the data exists" and "a steward can work with it".

**Independent Test**: With the seeded dataset evaluated, filter findings on each dimension
independently and confirm counts reconcile against the per-batch summary.

**Acceptance Scenarios**:

1. **Given** an evaluated batch, **When** a per-batch summary is requested, **Then** it reports
   failure counts grouped by domain, by rule, and by severity.
2. **Given** findings across several batches, **When** filtered to a single severity and time window,
   **Then** only findings matching both criteria are returned.
3. **Given** a summary and its underlying findings, **When** the summary counts are compared to a
   direct count of findings, **Then** the two reconcile exactly.

---

### Edge Cases

- **A rule references data that does not exist yet.** A referential-integrity rule runs before the
  master-data feed for that period has arrived. The rule must report a genuine failure rather than
  an error, and the timeliness rules must independently flag the missing feed, so the steward can
  see that one caused the other.
- **A batch is empty.** A feed arrives containing zero records. This must be distinguishable from
  "the feed did not arrive" — they have different causes and different remediations.
- **A rule predicate itself errors** on malformed data (for example, a date comparison against a
  non-date value). The run must record the rule as errored for that record rather than silently
  passing it, since a silently-passing broken rule is indistinguishable from clean data.
- **Two rules disagree in effect** — one flags a record as invalid, another flags the same record as
  a duplicate. Both findings stand; the system does not attempt to reconcile them.
- **Two genuine namesakes practise at the same location.** The composite-match rule (FR-016b) will
  flag them, correctly by its own definition and incorrectly in fact. This is why that rule carries
  Medium severity and is described as suspected rather than confirmed duplication. The seeded
  dataset MUST include such a pair so the behaviour is exercised deliberately rather than discovered
  in production.
- **Effective-dated alignment with a gap at exactly the transaction date.** Boundary behaviour at
  the first and last day of an alignment period must be explicit, not incidental.
- **A retired rule's historical findings.** Deactivating a rule must not orphan or delete findings
  it previously produced.
- **A correction file supersedes an earlier arrival.** Because batches are immutable, the corrected
  data forms a new batch while the original batch and its findings remain intact. Both are true
  statements about what the source system sent, and both remain queryable; the system does not
  retract the earlier findings.
- **Volume deviation on a new product** with no prior period to compare against. The rule must not
  fire spuriously for records that have no baseline.

## Requirements *(mandatory)*

### Functional Requirements

**Domain representation**

- **FR-001**: System MUST represent sales transactions, HCP master records, HCO master records,
  product master records, and territory definitions as distinct commercial domains.
- **FR-002**: System MUST represent territory-to-HCP alignment as effective-dated assignments, such
  that the alignment applicable to any given transaction date can be determined unambiguously.
- **FR-003**: System MUST record, for every commercial record, the source system it came from and
  the data batch in which it arrived.
- **FR-003a**: A data batch MUST correspond to one physical arrival (a single file or load event),
  identified by source system and arrival timestamp.
- **FR-003b**: A data batch MUST be immutable once recorded. Records MUST NOT be added to, removed
  from, or altered within an existing batch; corrected data arriving later MUST form a new batch.

**Rule registry**

- **FR-004**: System MUST maintain a rule registry in which every rule declares: the domain it
  applies to, the quality dimension it tests (completeness, uniqueness, validity, referential
  integrity, timeliness, consistency, or conformity), a severity, an owning business function, and
  a deterministic pass/fail predicate.
- **FR-005**: System MUST reject registration of a rule that omits any of the declarations in FR-004.
- **FR-006**: System MUST version rules, such that changing a rule's predicate, threshold, or
  severity creates a new version rather than mutating the existing one.
- **FR-007**: System MUST allow a rule to be activated and deactivated without deleting it or its
  historical findings.
- **FR-008**: Rule predicates MUST be deterministic: the same rule version evaluated against the
  same record MUST always produce the same verdict.

**Rule execution**

- **FR-009**: System MUST execute all active rules for a specified data batch in a single rule run.
- **FR-010**: System MUST persist one finding per failing record per rule, capturing the failing
  record's identity, the offending value, the rule, and the rule version that produced the verdict.
- **FR-010a**: Some rules fail against an aggregate rather than a record — a feed that never arrived
  has no record to point at, and a volume deviation is a property of a period, not a row. System
  MUST support findings whose subject is a source-and-period or a batch rather than an individual
  record, carrying the observed and expected values in place of an offending field value.
- **FR-011**: Rule execution MUST be idempotent: re-running the same rules against the same batch
  MUST NOT create duplicate findings.
- **FR-012**: System MUST be able to execute a rule run against any historical batch, not only the
  most recent one.
- **FR-013**: System MUST record, per rule run, which rules were evaluated, at which versions, over
  which batch, and when.
- **FR-014**: When a rule predicate cannot be evaluated for a record, System MUST record that
  outcome distinctly from both "passed" and "failed".

**Rule families the engine must support**

- **FR-015**: System MUST support detecting HCP records with a missing or structurally invalid NPI.
- **FR-016a**: System MUST support detecting HCP records held under distinct surrogate keys that
  share the same NPI, reported at High severity. An NPI collision is treated as near-certain
  duplication.
- **FR-016b**: System MUST support detecting HCP records held under distinct surrogate keys that
  match on a normalised composite key — last name, first initial, postal code, and licence state —
  reported at Medium severity. This is treated as suspected duplication requiring steward judgement,
  not as a confirmed defect.
- **FR-016c**: The normalisation applied for FR-016b (case folding, whitespace and punctuation
  handling, postal-code truncation) MUST be deterministic and MUST be part of the rule's versioned
  configuration, so that a change to normalisation produces a new rule version rather than silently
  altering what past findings meant.
- **FR-016d**: Duplicate detection MUST NOT use fuzzy, probabilistic, or similarity-scored matching.
  Every duplicate verdict MUST be reproducible from the record values alone.
- **FR-017**: System MUST support detecting sales transactions referencing a product or HCP absent
  from master data.
- **FR-018**: System MUST support detecting sales attributed to a territory with no active alignment
  covering the transaction date.
- **FR-019**: System MUST support detecting overlapping or gapped effective-date ranges in territory
  alignment.
- **FR-020**: System MUST support detecting unit-of-measure inconsistency between a sales transaction
  and the corresponding product master record.
- **FR-021a**: System MUST maintain a feed expectation catalogue declaring, per expected feed: the
  source system, the delivery cadence, the delivery window within which an arrival counts as timely,
  and the date range over which the expectation is active.
- **FR-021b**: System MUST support detecting a feed that arrived outside its declared delivery
  window (late) and a feed that did not arrive at all for an expected period (missing), as
  distinguishable outcomes.
- **FR-021c**: A feed expectation MUST be deactivatable with an end date, so that retiring a source
  system does not generate perpetual missing-feed findings.
- **FR-021d**: System MUST be able to report a feed as missing for a period in which no batch was
  ever recorded for that source — including a feed that has never arrived since being declared.
- **FR-022**: System MUST support detecting period-over-period volume deviation beyond a threshold
  configured on the rule.

**Findings access**

- **FR-023**: Users MUST be able to query findings filtered by domain, rule, severity, batch, and
  time window, in any combination.
- **FR-024**: System MUST produce a per-batch summary reporting failure counts by domain, by rule,
  and by severity.
- **FR-025**: Summary counts MUST reconcile exactly with the underlying findings they summarise.

**Boundaries**

- **FR-026**: System MUST NOT infer, adjust, or correct any commercial data. Detection only.
- **FR-027**: Every failure verdict MUST be attributable to a named rule version, with no verdict
  produced by any non-deterministic means.

### Key Entities

- **Sales Transaction**: A recorded commercial transaction, carrying at minimum the product, the
  attributed HCP, the territory, the transaction date, quantity, and unit of measure. Arrives via a
  data batch from a source system.
- **HCP**: A healthcare professional master record, identified internally by a surrogate key and
  externally by a national identifier (NPI). Subject to duplication across source systems.
- **HCO**: A healthcare organisation master record. HCPs may be affiliated with HCOs.
- **Product**: A product master record carrying its canonical unit of measure and its lifecycle
  status, including whether and when it was retired.
- **Territory**: A sales territory definition.
- **Territory Alignment**: An effective-dated assignment linking an HCP to a territory for a date
  range. The source of overlap and gap defects.
- **Data Batch**: A single physical arrival of data from a source system — one file or one load
  event — identified by source system and arrival timestamp. **Immutable once recorded**: no record
  is added to, removed from, or altered within a batch after it exists. A correction sent later
  arrives as its own new batch. This is the unit against which rule runs execute and the anchor for
  reproducibility.
- **Feed Expectation**: A declaration that a given source system is expected to deliver data on a
  stated cadence, within a stated delivery window, over a stated active date range. The reference
  against which late and missing feeds are detected. Deactivatable by end-dating, so a retired
  source stops generating findings.
- **Rule**: A named, owned, versioned quality check declaring its domain, dimension, severity, and
  owning business function.
- **Rule Version**: An immutable snapshot of a rule's predicate and configuration, referenced by
  every finding it produces.
- **Rule Run**: An execution of a set of active rule versions against a data batch, recording what
  was evaluated and when.
- **Finding**: A single rule version's failure verdict against a single record, capturing the
  offending value.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Against the seeded dataset, the finding set produced by a full rule run is exactly
  equal to the expected finding set — every deliberately injected defect across all eight rule
  families is detected, and no finding is produced that is not in the expected set. The namesake
  pair required by the edge cases counts as an expected finding of FR-016b, not as a false positive.
- **SC-002**: Every finding names the failing record, the offending value, the rule, and the rule
  version — verifiable on 100% of findings with no missing attribution.
- **SC-003**: Re-running the rules over an already-evaluated batch produces an identical finding set
  and zero duplicate findings.
- **SC-004**: A steward can retrieve all findings for a given batch, filtered by any combination of
  domain, rule, severity, and time window, and the counts reconcile exactly with the per-batch
  summary.
- **SC-005**: A rule's threshold can be changed and the change takes effect on the next run without
  altering the interpretation of any finding produced before the change.
- **SC-006**: A deactivated rule produces no new findings while all of its historical findings remain
  retrievable and interpretable.
- **SC-007**: A data engineer can register a new rule and see it produce findings on the next run
  without any code change to the rule engine itself.
- **SC-008**: A full rule run over one month of data at target scale — approximately 1 million sales
  transactions against 100,000 HCP records — completes in under 10 minutes.
- **SC-009**: A missing feed is reported for a source and period in which no data ever arrived,
  including a feed that has never arrived since being declared — demonstrating that absence is
  detected from the expectation catalogue rather than inferred from observed history.

## Assumptions

- **Five domains, not four.** The feature description says "four commercial domains" and then lists
  five (sales, HCP master, HCO master, product master, territory alignment). This spec treats all
  five as in scope; the count in the original phrasing is assumed to be a slip.
- **Synthetic data only.** No real commercial data, and no real HCP identifiers, are used in this
  feature. The seeded dataset is synthetic and generated with known, deliberately injected defects.
  Real source-system integration is out of scope.
- **NPI structural validity means format and check-digit validity**, not verification against an
  external registry. No external identifier lookup is in scope.
- **Volume deviation thresholds are per-rule configuration**, not a global setting, so different
  products and territories can carry different tolerances.
- **A record's identity is stable within a batch**, so a finding can reference it unambiguously.
- **Target scale is approximately 1M sales transactions per month, 100K HCP master records, and 5K
  products.** Rules must be evaluated in a way that does not degrade linearly with per-record
  overhead at this volume. The seeded test dataset is deliberately far smaller than this and is
  therefore not, on its own, evidence that the target scale is met — a separate volume check is
  required.
- **The database is reached over a network link, not locally.** Per-record round trips are
  therefore disproportionately expensive compared with evaluating a rule across a whole batch at
  once. This constrains rule evaluation design without dictating the mechanism.
- **Rule predicates are declarative configuration**, authored by data engineers without changing
  engine code — this is what makes SC-007 achievable.
- **No user interface, no HTTP API, no agent, and no automated remediation** are in scope. Findings
  are reached by direct query. This is deliberate: this feature is the deterministic baseline
  against which later automated capability is measured.
- **Effective-dated alignment uses inclusive start and exclusive end** boundaries unless
  clarification says otherwise, this being the convention least likely to produce phantom
  single-day gaps.
- **Rule runs are triggered manually or on a schedule external to this feature.** Orchestration of
  when runs happen is not in scope.

## Dependencies

- None. This is the foundation feature; nothing precedes it.
- The four least-privilege data-access roles required by constitution principle VI are created as
  part of this feature, since every later feature depends on them existing.
