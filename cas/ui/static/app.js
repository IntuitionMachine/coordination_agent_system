"use strict";
// CAS goal workspace (spec §11). One goal at a time: goal strip, contract list, the queue of
// things that need a human, and a change inspector. All text is inserted with textContent.

const state = { credential: sessionStorage.getItem("cas.credential") || "", me: null, goals: [], goalId: null,
                overview: null, exceptions: [], principals: [], selectedContract: null, selectedEvent: null,
                events: [], eventDetail: null, timer: null };

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

// ---------------------------------------------------------------- API

class ApiError extends Error {
  constructor(status, body) {
    super((body && body.error && body.error.message) || `Request failed (${status})`);
    this.status = status;
    this.code = (body && body.error && body.error.code) || "ERROR";
    this.details = (body && body.error && body.error.details) || {};
  }
}

async function api(method, path, body, version) {
  const headers = { Authorization: `Bearer ${state.credential}` };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["Idempotency-Key"] = crypto.randomUUID();
    if (version !== undefined && version !== null) headers["If-Match"] = `"${version}"`;
  }
  const response = await fetch(path, { method, headers, body: method === "GET" ? undefined : JSON.stringify(body || {}) });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, data);
  return method === "GET" ? data : data.resource;
}

function notify(kind, title, message) {
  const box = $("notice");
  box.className = `notice ${kind}`;
  box.replaceChildren(h("strong", {}, title), h("span", {}, message),
                      h("button", { class: "quiet small", type: "button", onclick: () => { box.hidden = true; } }, "Dismiss"));
  box.hidden = false;
}

function report(err) {
  if (err instanceof ApiError && err.status === 401) return signOut("Your credential was not accepted.");
  if (err instanceof ApiError && err.status === 403) {
    // Authorization failures stay visible and say what was refused (spec §11.5).
    const missing = err.details.missing_in ? ` Missing in: ${err.details.missing_in.join(", ")}.` : "";
    return notify("denied", "Not authorized", `${err.message}.${missing} This attempt was recorded in the audit log.`);
  }
  if (err instanceof ApiError) return notify("error", err.code.replaceAll("_", " ").toLowerCase(), err.message);
  notify("error", "Error", err.message || String(err));
}

async function act(fn, done) {
  try {
    await fn();
    if (done) notify("done", "Done", done);
    await refresh();
  } catch (err) { report(err); await refresh().catch(() => {}); }
}

// ---------------------------------------------------------------- formatting

const TONE = { accepted: "ok", active: "ok", ready: "info", leased: "info", running: "info", submitted: "info",
               validating: "info", proposed: "", draft: "", paused: "warn", blocked: "warn", rejected: "bad",
               failed: "bad", cancelled: "", completed: "ok", superseded: "", pass: "ok", fail: "bad" };
const chip = (text, tone) => h("span", { class: `chip ${tone === undefined ? (TONE[text] || "") : tone}` }, text);
const nameOf = (id) => { const p = state.principals.find((x) => x.id === id); return p ? p.display_name : (id || "unassigned"); };
const ago = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
};
const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);
const commas = (text) => text.split(",").map((s) => s.trim()).filter(Boolean);
const fact = (label, ...value) => h("span", {}, `${label} `, h("b", {}, ...value));

// ---------------------------------------------------------------- dialogs

function openDialog(title, fields, submitLabel, onSubmit) {
  const form = $("dialog-form");
  const error = h("p", { class: "form-error", role: "alert" });
  form.replaceChildren(h("h2", {}, title), ...fields, error,
    h("div", { class: "buttons" },
      h("button", { class: "quiet", type: "button", onclick: () => $("dialog").close() }, "Cancel"),
      h("button", { type: "submit" }, submitLabel)));
  form.onsubmit = async (event) => {
    event.preventDefault();
    error.textContent = "";
    try {
      await onSubmit(new FormData(form));
      $("dialog").close();
      await refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status !== 403 && err.status !== 401) error.textContent = err.message;
      else { $("dialog").close(); report(err); }
    }
  };
  $("dialog").showModal();
}

const field = (label, control, help) => h("label", {}, label, control, help ? h("span", { class: "help" }, help) : null);
const input = (name, value, attrs) => h("input", { type: "text", name, value: value || "", ...(attrs || {}) });
const area = (name, value, attrs) => { const t = h("textarea", { name, ...(attrs || {}) }); t.value = value || ""; return t; };

function goalDialog(goal) {
  const owner = goal && goal.owner_principal_id === state.me.id;
  openDialog(goal ? (owner ? "Revise goal" : "Propose a goal revision") : "New goal", [
    goal ? null : field("Title", input("title", "", { required: true })),
    field("Objective", area("objective", goal ? goal.objective : "", { required: true })),
    field("Success criteria", area("success_criteria", goal ? goal.success_criteria.join("\n") : "", { required: true }), "One per line. Each must be something that can be checked."),
    field("Constraints", area("constraints", goal ? goal.constraints.join("\n") : ""), "One per line."),
    field("Preferences", area("preferences", goal ? goal.preferences.join("\n") : ""), "Soft priorities, one per line. Preferences never grant permission."),
    goal ? field("Why", input("rationale", "", { required: true })) : null,
    goal && !owner ? h("p", { class: "help" }, "You are not this goal's owner, so this becomes a proposal. Nothing changes until the owner approves it.") : null,
  ], goal ? (owner ? "Revise goal" : "Send proposal") : "Create goal", async (data) => {
    const doc = { objective: data.get("objective").trim(), success_criteria: lines(data.get("success_criteria")),
                  constraints: lines(data.get("constraints")), preferences: lines(data.get("preferences")) };
    if (!goal) {
      const created = await api("POST", "/v1/goals", { title: data.get("title").trim(), ...doc, owner_principal_id: state.me.id });
      state.goalId = created.id;
      return;
    }
    const changes = {};
    for (const key of Object.keys(doc)) if (JSON.stringify(doc[key]) !== JSON.stringify(goal[key])) changes[key] = doc[key];
    const out = await api("POST", `/v1/goals/${goal.id}/revisions`, { changes, rationale: data.get("rationale") }, owner ? goal.version : undefined);
    notify("done", owner ? "Goal revised" : "Proposal sent",
           owner ? `Now at version ${out.version}. ${out.impact.reassessment_contract_ids.length} contract(s) flagged for reassessment.`
                 : "The goal is unchanged until its owner approves the proposal.");
  });
}

function contractDialog() {
  const o = state.overview;
  const agents = state.principals.filter((p) => p.kind === "agent");
  openDialog("Propose contract", [
    field("Title", input("title", "", { required: true })),
    field("Objective", area("objective", "", { required: true })),
    field("Deliverable type", input("deliverable", "", { required: true }), "For example: spec, prototype, report."),
    field("Acceptance checks", input("checks", "", { required: true }), "Comma separated. Prefix with review: for a human sign-off; nonempty:, hash:verify and json:<type> are checked automatically."),
    field("Depends on", h("select", { name: "deps", multiple: true }, o.contracts.map((c) => h("option", { value: c.id }, `${c.title} (${c.status})`))), "Stays unready until every one of these is accepted."),
    field("Input artifacts", input("inputs", ""), "Comma separated artifact ids. Add @v2 to pin an exact version; otherwise the latest accepted version is pinned when the contract becomes ready."),
    field("Permission scope", input("scope", ""), "Comma separated, e.g. staging:read. The contract cannot start if you cannot delegate these."),
    field("Owner", h("select", { name: "owner" }, h("option", { value: "" }, "Any authorized agent"), agents.map((a) => h("option", { value: a.id }, a.display_name)))),
    field("If an input changes", h("select", { name: "on_stale" }, h("option", { value: "hold" }, "Hold until someone revalidates"), h("option", { value: "revalidate" }, "Re-pin and continue automatically"))),
    field("Attempts allowed", input("max_attempts", "3", { type: "number", min: "1", max: "10", required: true })),
  ], "Propose", async (data) => {
    await api("POST", "/v1/contracts", {
      goal_id: state.goalId, title: data.get("title").trim(), objective: data.get("objective").trim(),
      requester_id: state.me.id, ...(data.get("owner") ? { owner_id: data.get("owner") } : {}),
      input_refs: commas(data.get("inputs")).map((ref) => {
        const [artifact_id, version] = ref.split("@");
        return version ? { artifact_id, required_version: version, mode: "exact" } : { artifact_id, required_version: "latest", mode: "latest_accepted" };
      }),
      dependency_ids: data.getAll("deps"), deliverable_spec: [{ type: data.get("deliverable").trim() }],
      acceptance_checks: commas(data.get("checks")), permission_scope: commas(data.get("scope")),
      escalation_policy: { on_stale: data.get("on_stale"), on_conflict: "reconcile_then_escalate", decision_owner_id: o.goal.owner_principal_id },
      max_attempts: Number(data.get("max_attempts")),
    });
  });
}

function acceptDialog(c) {
  openDialog(`Accept "${c.title}"`, [
    h("p", { class: "help" }, "Accepting publishes this attempt's artifacts. Tick each check you are vouching for; checks already passed stay passed."),
    ...Object.entries(c.checks).map(([check, status]) => h("label", { class: "inline" },
      h("input", { type: "checkbox", name: "pass", value: check, checked: status === "pass", disabled: status === "pass" }),
      check, status ? chip(status) : null)),
  ], "Accept", (data) => api("POST", `/v1/contracts/${c.id}/accept`,
    { check_results: data.getAll("pass").map((check) => ({ check, status: "pass" })) }, c.version));
}

function rejectDialog(c) {
  const pending = Object.entries(c.checks).filter(([, s]) => s !== "pass").map(([k]) => k);
  openDialog(`Reject "${c.title}"`, [
    field("Failed check", h("select", { name: "check" }, h("option", { value: "" }, "None in particular"), pending.map((k) => h("option", { value: k }, k)))),
    field("What is wrong", area("reason", "", { required: true }), "The rejected artifact version is kept. A retry starts a new attempt."),
  ], "Reject", (data) => api("POST", `/v1/contracts/${c.id}/reject`,
    { reason: data.get("reason"), check_results: data.get("check") ? [{ check: data.get("check"), status: "fail" }] : [] }, c.version));
}

function reasonDialog(title, label, submit, run) {
  openDialog(title, [field(label, area("reason", "", { required: true }))], submit, (data) => run(data.get("reason")));
}

function decisionDialog(item) {
  openDialog("Resolve decision", [
    h("p", {}, item.title),
    item.consequences ? h("p", { class: "help" }, item.consequences) : null,
    item.baseline_current ? null : h("p", { class: "form-error" }, "The evidence changed after this decision was opened. Resolving will be refused until the decision is refreshed against the current versions."),
    ...item.alternatives.map((a, i) => h("label", { class: "inline" },
      h("input", { type: "radio", name: "choice", value: a.id, required: true, checked: i === 0 }), a.label,
      a.consequences ? h("span", { class: "help" }, a.consequences) : null)),
    field("Rationale", area("rationale", "", { required: true }), "Recorded with the decision. A decision lifts holds; it never grants a permission."),
  ], "Resolve", (data) => api("POST", `/v1/decisions/${item.decision_id}/resolve`,
    { chosen_alternative: data.get("choice"), rationale: data.get("rationale") }, item.version));
}

function recoverDialog(item) {
  openDialog("Record manual recovery", [
    h("p", {}, item.title),
    h("p", { class: "help" }, "This step is not safe to repeat blindly. Find out whether it took effect, then record it so the contract can be retried."),
    field("What actually happened", h("select", { name: "outcome" }, h("option", { value: "not_applied" }, "It did not take effect; run it again on retry"), h("option", { value: "applied" }, "It took effect; do not repeat it"))),
    field("How you checked", area("note", "", { required: true })),
  ], "Record", (data) => api("POST", `/v1/effects/${item.effect_id}/recover`, { outcome: data.get("outcome"), note: data.get("note") }));
}

// ---------------------------------------------------------------- rendering

function renderGoalStrip() {
  const g = state.overview.goal;
  const mine = g.owner_principal_id === state.me.id;
  const list = (title, items) => h("div", {}, h("h3", {}, title), items.length ? h("ul", {}, items.map((i) => h("li", {}, i))) : h("span", { class: "help" }, "None"));
  const status = (label, verb, done) => h("button", { class: "quiet", type: "button", onclick: () => act(() => api("POST", `/v1/goals/${g.id}/${verb}`, {}, g.version), done) }, label);
  $("goal-strip").replaceChildren(
    h("div", { class: "goal-head" }, h("h1", {}, g.title), chip(g.status), chip(`version ${g.version}`, ""),
      h("span", { class: "help" }, `Owner: ${g.owner_name || g.owner_principal_id}`),
      h("div", { class: "goal-actions" },
        mine && (g.status === "draft" || g.status === "paused") ? status(g.status === "draft" ? "Activate" : "Resume", "activate", "Goal is active.") : null,
        mine && g.status === "active" ? status("Pause", "pause", "Goal paused. No contract becomes ready while paused.") : null,
        g.status !== "completed" && g.status !== "cancelled" ? h("button", { type: "button", onclick: () => goalDialog(g) }, mine ? "Revise goal" : "Propose revision") : null)),
    h("p", { class: "goal-objective" }, g.objective),
    h("div", { class: "goal-facts" }, list("Success criteria", g.success_criteria), list("Constraints", g.constraints), list("Preferences", g.preferences)));
}

function contractActions(c) {
  const post = (verb, body, done) => act(() => api("POST", `/v1/contracts/${c.id}/${verb}`, body, c.version), done);
  const button = (label, onclick, cls) => h("button", { class: `small ${cls || "quiet"}`, type: "button", onclick: (e) => { e.stopPropagation(); onclick(); } }, label);
  const out = [];
  if (c.status === "submitted" || c.status === "validating") out.push(button("Accept", () => acceptDialog(c), ""), button("Reject", () => rejectDialog(c)));
  if (c.status === "rejected" || c.status === "failed") out.push(button("Retry", () => post("retry", {}, "A new attempt is ready to be claimed.")));
  if (c.status === "blocked") out.push(button("Revalidate", () => post("revalidate", {}, "Revalidated against current inputs.")));
  if (["ready", "leased", "running"].includes(c.status)) out.push(button("Hold", () => reasonDialog(`Hold "${c.title}"`, "Why", "Hold", (reason) => api("POST", `/v1/contracts/${c.id}/block`, { reason }, c.version))));
  if (c.status === "accepted" && c.requires_reassessment) out.push(button("Create successor", () => post("revise", { rationale: "reassessment" }, "A successor contract was proposed; the accepted result is unchanged.")));
  if (!["accepted", "cancelled"].includes(c.status)) out.push(button("Cancel", () => reasonDialog(`Cancel "${c.title}"`, "Why", "Cancel contract", (reason) => api("POST", `/v1/contracts/${c.id}/cancel`, { reason }, c.version)), "danger"));
  return out;
}

function renderContracts() {
  const o = state.overview;
  const titles = Object.fromEntries(o.contracts.map((c) => [c.id, c]));
  $("contract-counts").replaceChildren(...Object.entries(o.contract_counts).map(([s, n]) => chip(`${n} ${s}`, TONE[s] || "")));
  if (!o.contracts.length) {
    $("contract-list").replaceChildren(h("p", { class: "empty" }, "No contracts yet. Propose the first piece of work for this goal."));
    return;
  }
  $("contract-list").replaceChildren(...o.contracts.map((c) => {
    const lease = c.lease ? `${c.lease.expired ? "expired" : "held"} by ${nameOf(c.lease.holder_id)}, fencing ${c.lease.fencing_token}` : "none";
    const deps = c.dependency_ids.length ? c.dependency_ids.map((id) => `${titles[id] ? titles[id].title : id} (${titles[id] ? titles[id].status : "?"})`).join(", ") : "none";
    const checks = Object.entries(c.checks);
    const select = () => { state.selectedContract = state.selectedContract === c.id ? null : c.id; state.selectedEvent = null; refresh(); };
    return h("div", { class: `row ${state.selectedContract === c.id ? "selected" : ""}`, tabindex: "0", role: "button",
                      "aria-pressed": String(state.selectedContract === c.id), onclick: select,
                      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(); } } },
      h("div", { class: "row-head" }, h("span", { class: "row-title" }, c.title), chip(c.status),
        c.status === "blocked" || c.status === "failed" || c.status === "rejected" ? h("span", { class: "flag" }, (c.blocked_reason || c.status_reason || "").replaceAll("_", " ")) : null,
        c.requires_reassessment ? h("span", { class: "flag" }, "needs reassessment") : null,
        c.stale_inputs ? h("span", { class: "flag" }, "inputs changed") : null,
        h("div", { class: "row-actions" }, contractActions(c))),
      h("div", { class: "facts" },
        fact("Owner", c.owner_name || "unassigned"), fact("Attempt", `${c.attempt} of ${c.max_attempts}`), fact("Lease", lease),
        fact("Depends on", deps),
        fact("Inputs", c.pinned_inputs.length ? c.pinned_inputs.map((p) => `${p.artifact_id}@${p.version_id}`).join(", ") : (c.input_refs.length ? c.input_refs.map((r) => `${r.artifact_id} (unpinned)`).join(", ") : "none")),
        fact("Artifacts", c.artifacts.length ? c.artifacts.map((a) => h("span", {}, `${a.artifact_id}@${a.version_id} `, chip(a.status), " ")) : "none"),
        fact("Validation", checks.map(([k, s]) => h("span", {}, `${k} `, chip(s || "pending", s ? TONE[s] : ""), " ")))));
  }));
}

const KIND_LABEL = { blocked: "Blocked", invalidated: "Invalidated input", permission_denied: "Permission denied",
                     awaiting_decision: "Waiting on a decision", failed: "Failed", rejected: "Rejected",
                     requires_reassessment: "Needs reassessment", decision: "Decision", unresolved_conflict: "Unresolved conflict",
                     manual_recovery: "Manual recovery", goal_revision_proposed: "Goal revision proposed" };

function renderExceptions() {
  const items = state.exceptions;
  $("exception-count").replaceChildren(chip(String(items.length), items.length ? "warn" : "ok"));
  if (!items.length) { $("exception-list").replaceChildren(h("p", { class: "empty" }, "Nothing needs a decision right now.")); return; }
  const g = state.overview.goal;
  $("exception-list").replaceChildren(...items.map((item) => {
    const actions = [];
    const button = (label, onclick, cls) => actions.push(h("button", { class: `small ${cls || ""}`, type: "button", onclick }, label));
    if (item.decision_id) {
      if (item.decision_owner_id === state.me.id) button("Resolve", () => decisionDialog(item));
      if (!item.baseline_current) button("Refresh evidence", () => act(() => api("POST", `/v1/decisions/${item.decision_id}/refresh`, {}, item.version), "Re-baselined against the current versions. Review before resolving."), "quiet");
    }
    if (item.kind === "manual_recovery") button("Record recovery", () => recoverDialog(item));
    if (item.kind === "goal_revision_proposed") {
      button("Approve", () => act(() => api("POST", `/v1/goals/${g.id}/revisions/${item.revision_id}/approve`, {}, g.version), "Revision applied."));
      button("Decline", () => act(() => api("POST", `/v1/goals/${g.id}/revisions/${item.revision_id}/reject`, {}, g.version), "Revision declined."), "quiet");
    }
    if (item.kind === "unresolved_conflict") button("Escalate to a decision", () => act(async () => { const cf = await api("GET", `/v1/conflicts/${item.conflict_id}`); await api("POST", `/v1/conflicts/${item.conflict_id}/escalate`, {}, cf.version); }, "A decision was opened and dependent work is held."));
    if (item.contract_id) button("Show contract", () => { state.selectedContract = item.contract_id; state.selectedEvent = null; refresh(); }, "quiet");
    const who = item.decision_owner_id ? `Decision owner: ${nameOf(item.decision_owner_id)}. ` : (item.proposed_by ? `Proposed by ${nameOf(item.proposed_by)}. ` : "");
    return h("div", { class: "exception" },
      h("div", { class: "row-head" }, chip(KIND_LABEL[item.kind] || item.kind, ["failed", "rejected", "permission_denied", "manual_recovery"].includes(item.kind) ? "bad" : "warn"), h("span", { class: "meta" }, ago(item.since))),
      h("p", {}, item.title),
      item.changes ? h("pre", {}, JSON.stringify(item.changes, null, 2)) : null,
      h("p", { class: "meta" }, who, item.reason ? `Reason: ${String(item.reason).replaceAll("_", " ")}. ` : "",
        item.held_contract_ids && item.held_contract_ids.length ? `Holding ${item.held_contract_ids.length} contract(s). ` : "",
        item.status ? `Status: ${String(item.status).replaceAll("_", " ")}.` : ""),
      actions.length ? h("div", { class: "actions" }, actions) : null);
  }));
}

function eventDetailNode(d) {
  const data = { ...d.data };
  const before = data.before !== undefined ? data.before : (data.from_status !== undefined ? { status: data.from_status } : null);
  const after = data.after !== undefined ? data.after : (data.to_status !== undefined ? { status: data.to_status } : null);
  for (const k of ["tenant_id", "aggregate_type", "schema_version", "before", "after", "from_status", "to_status", "goal_id"]) delete data[k];
  const link = (e) => h("button", { class: "quiet small", type: "button", onclick: () => { state.selectedEvent = e.id; refresh(); } }, `${e.type} · ${e.subject}`);
  return h("div", { class: "event-detail" },
    before || after ? h("div", {}, h("h3", {}, "Before and after"), h("div", { class: "diff" },
      h("div", {}, h("pre", {}, before ? JSON.stringify(before, null, 2) : "(new)")),
      h("div", {}, h("pre", {}, after ? JSON.stringify(after, null, 2) : "(none)")))) : null,
    h("div", {}, h("h3", {}, "Provenance"),
      h("p", {}, `By ${nameOf(d.data.actor_id)} at ${new Date(d.time).toLocaleString()}. Version ${d.data.aggregate_version} of ${d.subject}.`),
      d.caused_by ? h("p", {}, "Caused by ", link(d.caused_by)) : h("p", { class: "meta" }, "Started by a direct request.")),
    d.caused.length ? h("div", {}, h("h3", {}, "Affected work"), ...d.caused.map((e) => h("p", {}, link(e)))) : null,
    h("div", {}, h("h3", {}, "Details"), h("pre", {}, JSON.stringify(data, null, 2))));
}

function renderInspector() {
  const scope = state.selectedContract ? state.overview.contracts.find((c) => c.id === state.selectedContract) : null;
  $("inspector-scope").replaceChildren(scope
    ? h("span", {}, `${scope.title} `, h("button", { class: "quiet small", type: "button", onclick: () => { state.selectedContract = null; state.selectedEvent = null; refresh(); } }, "Show whole goal"))
    : h("span", {}, "Whole goal, newest first. Select a contract to narrow."));
  if (!state.events.length) { $("inspector").replaceChildren(h("p", { class: "empty" }, "No changes recorded yet.")); return; }
  const nodes = [];
  for (const e of state.events) {
    const selected = state.selectedEvent === e.id;
    nodes.push(h("div", { class: `event ${selected ? "selected" : ""}`, tabindex: "0", role: "button", "aria-expanded": String(selected),
                          onclick: () => { state.selectedEvent = selected ? null : e.id; refresh(); },
                          onkeydown: (ev) => { if (ev.key === "Enter") { state.selectedEvent = selected ? null : e.id; refresh(); } } },
      h("span", { class: "type" }, e.type.replace(/^cas\./, "").replace(/\.v\d+$/, "")),
      h("span", { class: "meta" }, `${e.subject} · ${nameOf(e.data.actor_id)} · ${ago(e.time)}${e.data.reason ? ` · ${String(e.data.reason).replaceAll("_", " ")}` : ""}`)));
    if (selected && state.eventDetail && state.eventDetail.id === e.id) nodes.push(eventDetailNode(state.eventDetail));
  }
  $("inspector").replaceChildren(h("div", { class: "inspector-list" }, nodes));
}

// ---------------------------------------------------------------- data flow

async function loadEvents() {
  let cursor = "0", all = [];
  for (let page = 0; page < 20; page += 1) {
    const subject = state.selectedContract ? `&subject=${encodeURIComponent(`contracts/${state.selectedContract}`)}` : "";
    const out = await api("GET", `/v1/goals/${state.goalId}/events?limit=200&cursor=${cursor}${subject}`);
    all = all.concat(out.items);
    cursor = out.next_cursor;
    if (!out.has_more) break;
  }
  state.events = all.reverse().slice(0, 150);
}

async function refresh() {
  if (!state.credential) return;
  try {
    state.goals = (await api("GET", "/v1/goals")).items;
    const select = $("goal-select");
    if (!state.goals.some((g) => g.id === state.goalId)) state.goalId = state.goals.length ? state.goals[0].id : null;
    select.replaceChildren(...state.goals.map((g) => h("option", { value: g.id, selected: g.id === state.goalId }, `${g.title} (${g.status})`)));
    if (!state.goalId) {
      $("goal-strip").replaceChildren(h("p", { class: "empty" }, "No goals yet. Create one to start coordinating work."));
      $("contract-list").replaceChildren(); $("exception-list").replaceChildren(); $("inspector").replaceChildren();
      return;
    }
    [state.overview, state.exceptions, state.principals] = await Promise.all([
      api("GET", `/v1/goals/${state.goalId}/overview`),
      api("GET", `/v1/goals/${state.goalId}/exceptions`).then((r) => r.items),
      api("GET", "/v1/principals").then((r) => r.items)]);
    await loadEvents();
    state.eventDetail = state.selectedEvent ? await api("GET", `/v1/events/${state.selectedEvent}`).catch(() => null) : null;
    renderGoalStrip(); renderContracts(); renderExceptions(); renderInspector();
  } catch (err) { report(err); }
}

function signOut(message) {
  sessionStorage.removeItem("cas.credential");
  state.credential = ""; state.me = null;
  clearInterval(state.timer);
  $("workspace").hidden = true; $("signin").hidden = false; $("whoami").textContent = "";
  $("sign-out").hidden = true; $("new-goal").hidden = true; $("goal-select").parentElement.hidden = true;
  if (message) notify("error", "Signed out", message); else $("notice").hidden = true;
}

async function signIn() {
  try {
    state.me = await api("GET", "/v1/me");
  } catch (err) { return signOut(err instanceof ApiError && err.status === 401 ? "That credential was not accepted." : err.message); }
  sessionStorage.setItem("cas.credential", state.credential);
  const roles = [...new Set(state.me.grants.map((g) => g.role))].join(", ") || "no roles";
  $("whoami").textContent = `${state.me.display_name} · ${state.me.kind} · ${roles}`;
  $("signin").hidden = true; $("workspace").hidden = false; $("notice").hidden = true;
  $("sign-out").hidden = false; $("new-goal").hidden = false; $("goal-select").parentElement.hidden = false;
  await refresh();
  clearInterval(state.timer);
  // Polling is enough for the pilot (spec §5); skip a tick while a dialog is open.
  state.timer = setInterval(() => { if (!$("dialog").open && !document.hidden) refresh(); }, 4000);
}

$("signin-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.credential = $("signin-credential").value.trim();
  $("signin-credential").value = "";
  signIn();
});
$("sign-out").addEventListener("click", () => signOut());
$("new-goal").addEventListener("click", () => goalDialog(null));
$("propose-contract").addEventListener("click", contractDialog);
$("goal-select").addEventListener("change", (event) => {
  state.goalId = event.target.value; state.selectedContract = null; state.selectedEvent = null; refresh();
});

if (state.credential) signIn(); else signOut();
