const state = {
  data: null,
  breakdown: "severity",
  filters: { search: "", severity: "all", domain: "all", owner: "all", type: "all" },
};

const el = (id) => document.getElementById(id);

const PAGE_TITLES = {
  overview: ["Command centre", "Nightly commercial load"],
  findings: ["Findings", "Attributable, immutable, rule-versioned"],
  rules: ["Rule library", "Deterministic SQL, executed in PostgreSQL"],
  evidence: ["Run evidence", "Reproducible execution context"],
};

// ---------------------------------------------------------------- formatting

function esc(value) {
  const node = document.createElement("span");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function num(value) {
  return new Intl.NumberFormat("en-US", {
    notation: value >= 100000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value ?? 0);
}

function duration(seconds) {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// Domain acronyms would otherwise title-case to "Hcp"/"Npi", which reads as a typo to this audience.
const ACRONYMS = new Set(["HCP", "HCO", "NPI", "UOM", "DQ"]);

function words(value) {
  return String(value ?? "")
    .toLowerCase()
    .replaceAll("_", " ")
    .split(" ")
    .map((word) =>
      ACRONYMS.has(word.toUpperCase())
        ? word.toUpperCase()
        : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}

function clock(value) {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function stamp(value) {
  return new Date(value).toLocaleString([], {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function icon(name, cls = "i") {
  return `<svg class="${cls}"><use href="#i-${name}"/></svg>`;
}

function sevPill(severity) {
  return `<span class="sev sev-pill-${String(severity).toLowerCase()}">${esc(severity)}</span>`;
}

// ---------------------------------------------------------------- transport

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function setBusy(busy, label) {
  el("btn-run").disabled = busy;
  el("btn-refresh").disabled = busy;
  el("period-select").disabled = busy;
  el("btn-run").innerHTML = busy && label
    ? `${icon("refresh", "i spin")}<span>${label}</span>`
    : `${icon("play")}<span>Run rules</span>`;
}

function notice(message, kind = "is-error") {
  const box = el("notice");
  box.hidden = !message;
  box.textContent = message;
  box.className = `notice ${kind}`;
}

let toastTimer;
function toast(message) {
  const box = el("toast");
  box.textContent = message;
  box.classList.add("is-open");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove("is-open"), 3200);
}

async function load(period) {
  setBusy(true);
  try {
    const query = period ? `?period=${encodeURIComponent(period)}` : "";
    render(await api(`/api/nightly-load${query}`));
    notice("");
  } catch (error) {
    notice(error.message);
  } finally {
    setBusy(false);
  }
}

async function run() {
  if (!state.data) return;
  const period = state.data.period;
  setBusy(true, "Evaluating…");
  notice(
    `Evaluating every batch and source-period scope for ${period}. ` +
    `Re-running is idempotent — existing findings are not duplicated.`,
    "is-busy",
  );
  try {
    const next = await api("/api/nightly-load/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-DQ-Demo-Action": "run-nightly-load" },
      body: JSON.stringify({ period }),
    });
    render(next);
    notice("");
    toast(
      next.rules_errored
        ? `Run finished with ${next.rules_errored} errored rule(s) — this period is not clean.`
        : `Run complete. ${num(next.new_findings)} new finding(s) persisted.`,
    );
  } catch (error) {
    notice(error.message);
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------------- rendering

function render(data) {
  state.data = data;

  el("period-select").innerHTML = data.available_periods
    .map((p) => `<option ${p === data.period ? "selected" : ""}>${esc(p)}</option>`)
    .join("");

  el("nav-findings").textContent = num(data.findings_count);
  el("nav-rules").textContent = data.rules.length;

  renderStatus(data);
  renderKpis(data);
  renderAlerts(data);
  renderBreakdown();
  renderRecentRuns(data.runs);
  renderBatches(data.batches);
  populateFilters(data.findings);
  renderFindings();
  renderRules(data.rules);
  renderRunsTable(data.runs);
}

function renderStatus(data) {
  const band = el("status-band");
  const truncated = data.findings.length < data.findings_count;
  let tone = "";
  let mark = "clock";
  let title = "No evaluation on record";
  let detail = `Nothing has been evaluated for ${data.period}. Run the deterministic rules to create evidence.`;

  if (data.run_status === "COMPLETED_WITH_ERRORS") {
    tone = "is-bad";
    mark = "alert";
    title = "Partially evaluated — do not read this as clean";
    detail = `${data.rules_errored} rule(s) errored. A rule that cannot be evaluated contributes no findings at all, including records that would legitimately have failed it.`;
  } else if (data.run_status === "INCOMPLETE") {
    tone = "is-warn";
    mark = "alert";
    title = "Some scopes have not been evaluated";
    detail = "At least one batch or source-period scope in this period has no completed run.";
  } else if (data.run_status === "COMPLETED") {
    tone = "is-ok";
    mark = "check";
    title = "All scopes evaluated";
    detail = `${num(data.findings_count)} finding(s) persisted across ${data.runs.length} run(s). ${data.evidence_status}.`;
  }

  band.className = `status-band ${tone}`;
  el("status-icon-use").setAttribute("href", `#i-${mark}`);
  el("status-title").textContent = title;
  el("status-detail").textContent = detail;
  el("status-watermark").textContent =
    data.reference_watermark ? `batch_id ${data.reference_watermark}` : "—";

  if (truncated) {
    notice(
      `Showing the ${data.findings.length} highest-severity findings of ${num(data.findings_count)} persisted. ` +
      `Breakdowns by severity and dimension cover all of them; the table and owner view do not.`,
      "is-busy",
    );
  }
}

function renderKpis(data) {
  el("kpi-records").textContent = num(data.records_evaluated);
  el("kpi-records-note").textContent =
    `${data.batches.length} immutable batch${data.batches.length === 1 ? "" : "es"} in scope`;

  el("kpi-findings").textContent = num(data.findings_count);
  el("kpi-findings-note").textContent = data.new_findings
    ? `${num(data.new_findings)} newly persisted in the latest runs`
    : "No new findings in the latest runs";

  el("kpi-rules").textContent = `${data.rules_evaluated} / ${data.rules.length}`;
  el("kpi-rules-note").textContent = data.rules_errored
    ? `${data.rules_errored} errored — findings incomplete`
    : "Every active rule evaluated";
  el("kpi-rules").parentElement.classList.toggle("is-bad", data.rules_errored > 0);

  el("kpi-duration").textContent = duration(data.run_duration_seconds);
  el("kpi-duration-note").textContent = `Across ${data.runs.length} scoped run(s)`;
}

function renderAlerts(data) {
  const box = el("alerts");
  const rows = [];

  if (data.rules_errored) {
    rows.push(`
      <div class="alert sev-high">
        <span class="alert-icon">${icon("alert")}</span>
        <div>
          <strong>${data.rules_errored} rule(s) could not be evaluated</strong>
          <p>The run closed as COMPLETED_WITH_ERRORS. Treat this period as unverified until the rules are fixed and re-run.</p>
        </div>
        <span class="tag is-bad">Blocking</span>
      </div>`);
  }

  for (const finding of data.findings.filter((f) => f.subject_type === "source_period")) {
    rows.push(`
      <div class="alert sev-${String(finding.severity).toLowerCase()}">
        <span class="alert-icon">${icon(aggregateIcon(finding))}</span>
        <div>
          <strong>${esc(finding.subject_key)}</strong>
          <p>
            <span class="mono">${esc(finding.rule_key)}</span> ·
            observed <b>${esc(finding.observed_value ?? "—")}</b>,
            expected <b>${esc(finding.expected_value ?? "—")}</b>${aggregateNote(finding)}
          </p>
        </div>
        ${sevPill(finding.severity)}
      </div>`);
  }

  box.innerHTML = rows.join("");
  box.hidden = rows.length === 0;
}

// A period-level finding has no row to point at. The two shipped families that produce one mean
// very different things, so the framing is taken from the rule's own dimension rather than assumed.
function aggregateIcon(finding) {
  if (finding.dimension === "timeliness") return "ghost";
  if (finding.dimension === "consistency") return "signal";
  return "alert";
}

function aggregateNote(finding) {
  if (finding.dimension === "timeliness") {
    return ". Nothing else looks wrong when a feed goes quiet — the numbers simply stop.";
  }
  if (finding.dimension === "consistency") {
    return ". Movement beyond the configured threshold; the data may still be correct.";
  }
  return "";
}

function ownerCounts(findings) {
  const tally = new Map();
  for (const finding of findings) {
    tally.set(finding.owning_function, (tally.get(finding.owning_function) || 0) + 1);
  }
  return [...tally]
    .map(([label, count]) => ({ label, count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

function renderBreakdown() {
  const data = state.data;
  const mode = state.breakdown;
  const items = mode === "severity" ? data.severities
    : mode === "dimension" ? data.dimensions
    : ownerCounts(data.findings);

  if (!items.length) {
    el("breakdown").innerHTML = `<p class="empty">No findings persisted for this period.</p>`;
    return;
  }

  const max = Math.max(...items.map((i) => i.count), 1);
  el("breakdown").innerHTML = items
    .map((item) => {
      const tone = mode === "severity" ? ` sev-${String(item.label).toLowerCase()}` : "";
      return `
        <div class="bar-row">
          <span title="${esc(words(item.label))}">${esc(words(item.label))}</span>
          <div class="bar${tone}"><span style="width:${Math.round((item.count / max) * 100)}%"></span></div>
          <b>${num(item.count)}</b>
        </div>`;
    })
    .join("");
}

function renderRecentRuns(runs) {
  if (!runs.length) {
    el("recent-runs").innerHTML = `<p class="empty">No runs recorded. Use “Run rules” to create evidence.</p>`;
    return;
  }
  el("recent-runs").innerHTML = runs
    .slice(0, 5)
    .map((run) => {
      const bad = run.rules_errored > 0;
      return `
        <div class="feed-row">
          <span class="feed-icon${bad ? " is-bad" : ""}">${icon(bad ? "alert" : "check")}</span>
          <div>
            <strong>${esc(run.scope_key)}</strong>
            <p>${run.rules_evaluated} rule version(s) · ${num(run.new_findings)} new${bad ? ` · ${run.rules_errored} errored` : ""}${run.replay_of_rule_run_id ? ` · replay of run ${run.replay_of_rule_run_id}` : ""}</p>
          </div>
          <time>${clock(run.started_at)}</time>
        </div>`;
    })
    .join("");
}

function renderBatches(batches) {
  el("batches-tbody").innerHTML = batches.length
    ? batches
        .map((batch) => `
          <tr>
            <td class="mono">${batch.batch_id}</td>
            <td><span class="cell-strong">${esc(batch.source_code)}</span><span class="cell-sub">${esc(batch.source_name)}</span></td>
            <td class="cell-nowrap">${num(batch.record_count)}</td>
            <td class="cell-nowrap">${stamp(batch.arrival_ts)}</td>
          </tr>`)
        .join("")
    : `<tr><td colspan="4" class="empty">No batches arrived for this period.</td></tr>`;
}

function populateFilters(findings) {
  const fill = (id, values, label) => {
    const select = el(id);
    const current = select.value;
    select.innerHTML = `<option value="all">${label}</option>` +
      values.map((value) => `<option>${esc(value)}</option>`).join("");
    if (values.includes(current)) select.value = current;
  };
  const unique = (key) => [...new Set(findings.map((f) => f[key]))].sort();

  fill("f-severity", ["HIGH", "MEDIUM", "LOW"].filter((s) => unique("severity").includes(s)), "All severities");
  fill("f-domain", unique("domain"), "All domains");
  fill("f-owner", unique("owning_function"), "All owners");
  fill("f-type", unique("subject_type"), "All subjects");
}

function visibleFindings() {
  const { search, severity, domain, owner, type } = state.filters;
  return state.data.findings.filter((f) =>
    (severity === "all" || f.severity === severity) &&
    (domain === "all" || f.domain === domain) &&
    (owner === "all" || f.owning_function === owner) &&
    (type === "all" || f.subject_type === type) &&
    (!search || `${f.subject_key} ${f.rule_key} ${f.owning_function}`.toLowerCase().includes(search))
  );
}

function renderFindings() {
  const rows = visibleFindings();
  el("findings-count").textContent = num(rows.length);
  el("findings-tbody").innerHTML = rows.length
    ? rows
        .map((f) => `
          <tr data-id="${f.finding_id}">
            <td><span class="cell-strong">${esc(f.subject_key)}</span><span class="cell-sub">${esc(words(f.subject_type))} · ${esc(words(f.domain))}</span></td>
            <td><span class="mono">${esc(f.rule_key)}</span><span class="cell-sub">v${f.version_no} · ${esc(words(f.dimension))}</span></td>
            <td>${sevPill(f.severity)}</td>
            <td class="cell-nowrap">${esc(f.owning_function)}</td>
            <td><div class="cell-value" title="${esc(f.offending_value || f.observed_value || "")}">${esc(f.offending_value || f.observed_value || "—")}</div></td>
            <td class="cell-nowrap">${stamp(f.detected_at)}</td>
          </tr>`)
        .join("")
    : `<tr><td colspan="6" class="empty">No findings match these filters.</td></tr>`;

  for (const row of document.querySelectorAll("#findings-tbody tr[data-id]")) {
    row.addEventListener("click", () => openFinding(Number(row.dataset.id)));
  }
}

function renderRules(rules) {
  el("rules-grid").innerHTML = rules
    .map((rule) => `
      <article class="rule-card">
        <div class="rule-card-top">
          <span class="tag is-ok">Active</span>
          ${sevPill(rule.severity)}
        </div>
        <h3>${esc(rule.rule_key)}</h3>
        <p>${esc(words(rule.dimension))} check over ${esc(words(rule.domain))}, judged per ${esc(words(rule.subject_type))}.</p>
        <div class="rule-meta">
          <span class="tag">v${rule.version_no}</span>
          <span class="tag">${esc(rule.owning_function)}</span>
        </div>
      </article>`)
    .join("");
}

function renderRunsTable(runs) {
  el("runs-tbody").innerHTML = runs.length
    ? runs
        .map((run) => {
          const done = run.finished_at
            ? duration((new Date(run.finished_at) - new Date(run.started_at)) / 1000)
            : "running";
          const tone = run.status === "COMPLETED" ? "is-ok" : run.status === "COMPLETED_WITH_ERRORS" ? "is-bad" : "";
          return `
            <tr>
              <td><span class="cell-strong mono">${run.rule_run_id}</span><span class="cell-sub mono">${esc(run.correlation_id.slice(0, 8))}…</span></td>
              <td><span class="mono">${esc(run.scope_key)}</span>${run.replay_of_rule_run_id ? `<span class="cell-sub">${icon("link", "i")} replay of ${run.replay_of_rule_run_id}</span>` : ""}</td>
              <td class="cell-nowrap">${run.rules_evaluated}${run.rules_errored ? ` <span class="tag is-bad">${run.rules_errored} errored</span>` : ""}</td>
              <td class="cell-nowrap">${num(run.new_findings)}</td>
              <td class="mono cell-nowrap">${run.reference_watermark}</td>
              <td><span class="tag ${tone}">${esc(words(run.status))}</span></td>
              <td class="cell-nowrap">${done}</td>
            </tr>`;
        })
        .join("")
    : `<tr><td colspan="7" class="empty">No rule-run evidence exists for this period.</td></tr>`;
}

function openFinding(id) {
  const f = state.data.findings.find((item) => item.finding_id === id);
  if (!f) return;

  el("d-sev").textContent = f.severity;
  el("d-sev").className = `sev sev-pill-${String(f.severity).toLowerCase()}`;
  el("d-rule").textContent = f.rule_key;
  el("d-id").textContent = `Finding ${f.finding_id} · rule run ${f.rule_run_id}`;

  el("d-kv").innerHTML = [
    ["Subject", esc(f.subject_key)],
    ["Subject type", esc(words(f.subject_type))],
    ["Rule version", `v${f.version_no}`],
    ["Domain", esc(words(f.domain))],
    ["Dimension", esc(words(f.dimension))],
    ["Owner", esc(f.owning_function)],
    ["Detected", stamp(f.detected_at)],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  el("d-offending").textContent = f.offending_value ?? "—";
  el("d-observed").textContent = f.observed_value ?? "—";
  el("d-expected").textContent = f.expected_value ?? "—";

  el("overlay").hidden = false;
}

function closeFinding() {
  el("overlay").hidden = true;
}

function setView(name, { push = true } = {}) {
  if (!PAGE_TITLES[name]) name = "overview";
  for (const view of document.querySelectorAll(".view")) view.hidden = view.id !== `view-${name}`;
  for (const button of document.querySelectorAll(".nav-item")) {
    button.classList.toggle("is-active", button.dataset.view === name);
  }
  const [title, sub] = PAGE_TITLES[name];
  el("page-title").textContent = title;
  el("page-sub").textContent = sub;
  if (push && location.hash.slice(1) !== name) location.hash = name;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ---------------------------------------------------------------- events

for (const button of document.querySelectorAll(".nav-item")) {
  button.addEventListener("click", () => setView(button.dataset.view));
}
for (const button of document.querySelectorAll("[data-jump]")) {
  button.addEventListener("click", () => setView(button.dataset.jump));
}
for (const button of document.querySelectorAll(".seg-btn")) {
  button.addEventListener("click", () => {
    state.breakdown = button.dataset.break;
    for (const other of document.querySelectorAll(".seg-btn")) {
      other.classList.toggle("is-active", other === button);
    }
    renderBreakdown();
  });
}

el("f-search").addEventListener("input", (e) => {
  state.filters.search = e.target.value.trim().toLowerCase();
  renderFindings();
});
for (const [id, key] of [["f-severity", "severity"], ["f-domain", "domain"], ["f-owner", "owner"], ["f-type", "type"]]) {
  el(id).addEventListener("change", (e) => {
    state.filters[key] = e.target.value;
    renderFindings();
  });
}

el("period-select").addEventListener("change", (e) => load(e.target.value));
el("btn-refresh").addEventListener("click", () => load(state.data?.period));
el("btn-run").addEventListener("click", run);
el("d-close").addEventListener("click", closeFinding);
el("overlay").addEventListener("click", (e) => { if (e.target === el("overlay")) closeFinding(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeFinding(); });

// ---------------------------------------------------------------- boot

window.addEventListener("hashchange", () => setView(location.hash.slice(1), { push: false }));
setView(location.hash.slice(1) || "overview", { push: false });

api("/api/health")
  .then((data) => {
    el("health").classList.add("is-ok");
    el("health-text").textContent = `${data.database} connected`;
  })
  .catch(() => {
    el("health").classList.add("is-bad");
    el("health-text").textContent = "Database unavailable";
  });

load();
