"use strict";

const $ = (id) => document.getElementById(id);
let workspace = null;
let branch = "main";
let csrf = "";
let busy = false;
let ready = false;
let auditVerdict = null;
let toastTimer;
let errorFocus = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function idOf(commit) {
  return typeof commit === "string" ? commit : commit?.id || "";
}

function versionedState() {
  return workspace?.head?.state || {};
}

function messageRecords() {
  return workspace?.messages ?? versionedState().messages ?? [];
}

function historicalView() {
  const currentBranch = (workspace?.branches || []).find((item) => item.name === branch);
  return Boolean(currentBranch && idOf(currentBranch.head) && idOf(currentBranch.head) !== idOf(workspace?.head));
}

function formatNumber(value, precision = 2) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString(undefined, { maximumFractionDigits: precision })
    : "—";
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function titleCase(value) {
  return String(value).replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}

function scalar(value) {
  return value === null || value === undefined ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
}

function clearError() {
  $("global-alert").hidden = true;
  $("alert-message").textContent = "";
}

function showError(error) {
  errorFocus = document.activeElement;
  $("alert-message").textContent = error instanceof Error ? error.message : String(error);
  $("global-alert").hidden = false;
}

function notify(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5500);
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll("[data-mutation]").forEach((element) => {
    element.disabled = busy || !ready || element.dataset.intrinsicDisabled === "true";
  });
  $("branch-select").disabled = busy || !ready;
  $("export-button").disabled = busy || !ready;
  $("send-label").textContent = busy ? "Working" : "Send";
  $("chat-form").setAttribute("aria-busy", String(busy));
}

async function responseError(response) {
  try {
    const body = await response.json();
    const message = body.error || body.detail || body.message || `Request failed (${response.status}).`;
    return new Error(typeof message === "string" ? message : JSON.stringify(message));
  } catch {
    return new Error(`Request failed (${response.status}). Please refresh and try again.`);
  }
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json", "X-Dao-CSRF": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin"
  });
  if (!response.ok) throw await responseError(response);
  return response.json();
}

async function mutate(operation) {
  if (busy || !ready) return;
  clearError();
  setBusy(true);
  try { await operation(); } catch (error) { showError(error); } finally { setBusy(false); }
}

function addJsonDetails(container, value, label = "Inspect the full result") {
  const details = node("details", "json-details");
  details.append(node("summary", "", label), node("pre", "json-preview", JSON.stringify(value, null, 2)));
  container.append(details);
}

function resetVerdict() {
  auditVerdict = null;
  $("artifact-save").dataset.intrinsicDisabled = "true";
  $("artifact-save").disabled = true;
  $("artifact-status").textContent = "";
}

function daoIcon(className = "") {
  const icon = node("img", className);
  icon.src = "/static/dao.svg";
  icon.alt = "";
  icon.setAttribute("aria-hidden", "true");
  return icon;
}

function renderMessage(message, streaming = false) {
  const role = message.role === "user" ? "user" : message.role === "assistant" ? "assistant" : "system";
  const item = node("article", `message ${role}${streaming ? " streaming" : ""}`);
  if (message.id) {
    item.dataset.messageId = message.id;
    item.tabIndex = -1;
  }
  const avatar = node("span", `message-avatar ${role}`, role === "user" ? "You" : role === "system" ? "✧" : undefined);
  if (role === "assistant") avatar.append(daoIcon());
  item.append(avatar);
  const body = node("div", "message-body");
  body.append(node("div", "message-label", role === "assistant" ? "Dao" : titleCase(role)));
  const content = node("div", "message-content", message.content || "");
  body.append(content);
  if (message.id) {
    const source = node("details", "message-source");
    source.append(node("summary", "", `Source details · ${message.id}`));
    const citation = messageCitation(message);
    if (!citation) {
      source.append(node("p", "field-help", "This message has no citable passage because its turn failed or its content is empty or contains only unsupported controls."));
      body.append(source);
    } else {
      source.append(node("p", "field-help", "Copy this citation into a node, relation, or assessment’s citations array. Narrow start, end, and quote to cite a passage."));
      source.append(node("pre", "json-preview", JSON.stringify(citation, null, 2)));
      const copy = node("button", "text-button", "Copy citation");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable. Select and copy the citation shown here.");
          await navigator.clipboard.writeText(JSON.stringify(citation, null, 2));
          notify("Source citation copied.");
        } catch (error) { showError(error); }
      });
      source.append(copy);
      body.append(source);
    }
  }
  if (message.status && !["complete", "completed", "done"].includes(message.status)) {
    body.append(node("div", "message-status", titleCase(message.status)));
  }
  item.append(body);
  $("messages").append(item);
  return { item, content };
}

function scrollMessages() {
  $("messages").scrollTop = $("messages").scrollHeight;
}

function renderConversation() {
  const messages = messageRecords();
  $("messages").replaceChildren();
  $("starter-prompts").hidden = messages.length > 0;
  if (!messages.length) {
    const empty = node("section", "empty-state");
    const emblem = daoIcon("empty-emblem");
    const heading = node("h2");
    heading.textContent = "Room to think.";
    const copy = node("p", "", "Ask a question or explore a decision.");
    empty.append(emblem, heading, copy);
    $("messages").append(empty);
  } else {
    messages.forEach((message) => renderMessage(message));
    scrollMessages();
  }
}

function renderBranches() {
  $("branch-select").replaceChildren();
  const branches = workspace?.branches || [{ name: branch }];
  branches.forEach((item) => {
    const option = node("option", "", item.name);
    option.value = item.name;
    option.selected = item.name === branch;
    $("branch-select").append(option);
  });
  $("revision-label").textContent = idOf(workspace?.head) ? `Revision ${idOf(workspace.head).slice(0, 8)}` : "Revision history is saved";
}

function renderHistory() {
  const history = workspace?.history || [];
  $("history-count").textContent = history.length;
  $("history-list").replaceChildren();
  if (!history.length) {
    $("history-list").append(node("li", "empty-copy", "No saved revisions to display."));
    return;
  }
  history.forEach((commit) => {
    const current = idOf(commit) === idOf(workspace.head);
    const item = node("li", `history-item${current ? " current" : ""}`);
    const label = commit.kind === "bootstrap" ? "Workspace started" : commit.label || titleCase(commit.kind || "Revision");
    item.append(node("div", "history-title", label));
    item.append(node("div", "history-meta", `${idOf(commit).slice(0, 7)} · ${formatTime(commit.created_at)}`));
    const restore = node("button", "history-action", current ? "Current revision" : "↶ Restore revision");
    if (!current) restore.title = "Restoring creates a new revision and preserves earlier revisions.";
    restore.type = "button";
    restore.dataset.mutation = "";
    restore.dataset.intrinsicDisabled = String(current);
    restore.disabled = current || busy;
    restore.addEventListener("click", () => mutate(async () => {
      await api("/api/restore", { branch, commit_id: idOf(commit), expected_head: idOf(workspace.head) });
      resetVerdict();
      $("decision-result").replaceChildren();
      $("audit-result").replaceChildren();
      await refresh();
      notify("Restored as a new revision. Earlier revisions and usage stay saved.");
    }));
    item.append(restore);
    $("history-list").append(item);
  });
}

function metricCard(value, label) {
  const card = node("div", "metric-card");
  card.append(node("div", "metric-value", value), node("div", "metric-label", label));
  return card;
}

function renderUsage() {
  const usage = workspace?.usage || {};
  const entries = usage.entries || [];
  const totals = usage.totals || {};
  const totalTokens = totals.total_tokens ?? ((totals.input_tokens ?? 0) + (totals.output_tokens ?? 0));
  const cost = typeof totals.cost_microusd === "number" ? totals.cost_microusd / 1e6 : totals.cost_usd ?? totals.estimated_cost_usd ?? totals.total_cost_usd;
  const pricingConfigured = workspace?.config?.pricing_configured === true;
  $("usage-totals").replaceChildren(metricCard(formatNumber(totalTokens, 0), "Recorded tokens"), metricCard(pricingConfigured && typeof cost === "number" ? `$${cost.toFixed(5)}` : "—", pricingConfigured ? "Estimated usage cost" : "Pricing not configured"));
  $("budget-display").replaceChildren();
  const budget = workspace?.config?.token_budget;
  if (typeof budget === "number" && budget > 0) {
    $("budget-display").append(node("div", "", `${formatNumber(totalTokens, 0)} / ${formatNumber(budget, 0)} lifetime token budget · all branches`));
    const progress = node("progress");
    progress.max = budget;
    progress.value = Math.min(totalTokens, budget);
    progress.setAttribute("aria-label", "Token budget used");
    $("budget-display").append(progress);
  }
  if (workspace?.config?.provider === "demo") $("budget-display").append(node("div", "field-help", "Estimated tokens · no provider cost."));
  else if (!pricingConfigured) $("budget-display").append(node("div", "field-help", "Configure pricing to estimate costs."));
  $("usage-count").textContent = entries.length;
  $("usage-entries").replaceChildren();
  if (!entries.length) $("usage-entries").append(node("p", "empty-copy", "No usage yet."));
  [...entries].reverse().forEach((entry) => {
    const row = node("div", "ledger-row");
    const heading = node("div", "ledger-row-title");
    heading.append(node("span", "", entry.model || titleCase(entry.kind || entry.provider || "Usage")), node("span", "", formatNumber(entry.total_tokens ?? ((entry.input_tokens ?? 0) + (entry.output_tokens ?? 0)), 0) + " tokens"));
    row.append(heading);
    const meta = [`${formatNumber(entry.input_tokens, 0)} in · ${formatNumber(entry.output_tokens, 0)} out`, entry.status ? titleCase(entry.status) : "", formatTime(entry.created_at || entry.timestamp)].filter(Boolean).join(" · ");
    row.append(node("div", "ledger-row-meta", meta));
    $("usage-entries").append(row);
  });
  $("memory-preview").textContent = JSON.stringify(versionedState().memory || {}, null, 2);
  $("event-list").replaceChildren();
  const events = workspace?.events || [];
  if (!events.length) $("event-list").append(node("p", "empty-copy", "Reviews and saved changes appear here."));
  [...events].reverse().forEach((event) => {
    const item = node("div", "event-item");
    item.append(node("strong", "", titleCase(event.kind || event.type || event.action || "Event")));
    item.append(node("p", "", JSON.stringify(event, null, 2)));
    $("event-list").append(item);
  });
  $("artifact-list").replaceChildren();
  const artifacts = versionedState().artifacts || {};
  const artifactEntries = Object.entries(artifacts);
  if (!artifactEntries.length) $("artifact-list").append(node("p", "empty-copy", "No artifacts yet."));
  artifactEntries.forEach(([name, content]) => {
    const details = node("details", "event-item");
    details.append(node("summary", "", name), node("pre", "json-preview", typeof content === "string" ? content : JSON.stringify(content, null, 2)));
    $("artifact-list").append(details);
  });
}

function renderWorkspace() {
  renderBranches();
  renderHistory();
  renderConversation();
  renderUsage();
  renderRelationships();
  renderStructure();
  const saved = versionedState();
  if (saved.decisions?.length) renderDecision(saved.decisions[saved.decisions.length - 1].result);
  else $("decision-result").replaceChildren();
  if (saved.audits?.length) renderAudit(saved.audits[saved.audits.length - 1]);
  else { $("audit-result").replaceChildren(); resetVerdict(); }
  const config = workspace?.config || {};
  $("provider-badge").replaceChildren(node("span", "status-dot"), document.createTextNode(config.provider === "demo" ? "Offline demo" : config.provider === "openai" ? `Live · ${config.model || "OpenAI"}` : "Connected"));
  $("provider-description").textContent = config.provider === "demo" ? "Demo replies · no model calls" : "Your conversation is sent to the configured model.";
  $("provider-badge").title = config.provider === "demo" ? "Offline demo with deterministic replies. No AI model or external API is used." : "Connected model provider";
  setBusy(busy);
}

function renderRelationships() {
  const summary = workspace?.relationships || {};
  const coherence = summary.coherence;
  const coverage = summary.coverage;
  const conflicts = Array.isArray(summary.unresolved_conflicts) ? summary.unresolved_conflicts : [];
  const severe = conflicts.filter((conflict) => conflict.severe === true);
  const percent = (value) => typeof value === "number" && Number.isFinite(value) ? `${formatNumber(value * 100, 1)}%` : "Not defined";
  $("relationship-summary").replaceChildren(
    metricCard(coherence === null || coherence === undefined ? "Not defined" : percent(coherence), "Conditional coherence"),
    metricCard(percent(coverage), "Assessed coverage"),
    metricCard(formatNumber(summary.unknown_weight ?? 0, 2), "Unknown weight"),
    metricCard(String(severe.length), "Unresolved severe conflicts")
  );
  $("conflict-count").textContent = String(conflicts.length);
  $("relationship-conflicts").replaceChildren();
  conflicts.forEach((conflict) => {
    const item = node("details", `event-item${conflict.severe ? " conflict-severe" : ""}`);
    const relation = versionedState().relationships?.relations?.[conflict.relation_id];
    const actions = Array.isArray(conflict.actions) ? conflict.actions : relation?.actions || [];
    item.append(node("summary", "", `${conflict.severe ? "Severe · " : ""}${conflict.relation_id || conflict.id || "Relationship conflict"}`));
    item.append(node("p", "", actions.length ? `Affected actions: ${actions.join(", ")}` : "Affects every action."));
    item.append(node("pre", "json-preview", JSON.stringify(conflict, null, 2)));
    $("relationship-conflicts").append(item);
  });
  $("relationship-details").replaceChildren();
  addJsonDetails($("relationship-details"), summary, "Weighted summary and transition estimates");
  addJsonDetails($("relationship-details"), versionedState().relationships || {}, "Saved relationships, assessments, and observations");
}

function citationsIn(value) {
  const citations = [];
  const pending = [value];
  const seen = new Set();
  while (pending.length) {
    const item = pending.pop();
    if (!item || typeof item !== "object") continue;
    if (Array.isArray(item)) { pending.push(...item); continue; }
    if (Array.isArray(item.citations)) {
      item.citations.forEach((citation) => {
        const key = JSON.stringify(citation);
        if (citation && !seen.has(key)) { citations.push(citation); seen.add(key); }
      });
    }
    Object.entries(item).forEach(([key, child]) => { if (key !== "citations") pending.push(child); });
  }
  return citations;
}

function showPassage(citation) {
  const message = messageRecords().find((item) => item.id === citation.message_id);
  const article = [...$("messages").querySelectorAll("[data-message-id]")].find((item) => item.dataset.messageId === citation.message_id);
  if (!message || !article) { showError(new Error("This source message is unavailable in the viewed revision.")); return; }
  const characters = Array.from(message.content || "");
  const { start, end } = citation;
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > characters.length || characters.slice(start, end).join("") !== citation.quote) {
    showError(new Error("This citation does not match the original message passage.")); return;
  }
  $("messages").querySelectorAll(".message-source-highlight").forEach((item) => item.classList.remove("message-source-highlight"));
  $("messages").querySelectorAll(".message-content mark").forEach((mark) => mark.replaceWith(document.createTextNode(mark.textContent)));
  const content = article.querySelector(".message-content");
  const mark = node("mark", "", citation.quote);
  content.replaceChildren(document.createTextNode(characters.slice(0, start).join("")), mark, document.createTextNode(characters.slice(end).join("")));
  article.classList.add("message-source-highlight");
  if (!wideLayout.matches) setToolsOpen(false);
  article.focus({ preventScroll: true });
  mark.scrollIntoView({ behavior: "smooth", block: "center" });
}

function appendCitations(container, value) {
  const citations = citationsIn(value);
  if (!citations.length) return;
  const list = node("ul", "source-citations");
  citations.forEach((citation) => {
    const item = node("li");
    const link = node("button", "text-button source-link", `Source: ${citation.message_id} [${citation.start}:${citation.end}]`);
    link.type = "button";
    link.addEventListener("click", () => showPassage(citation));
    item.append(link, node("blockquote", "source-quote", citation.quote || ""));
    list.append(item);
  });
  container.append(list);
}

function renderStructure() {
  const proposals = versionedState().proposals || [];
  const historical = historicalView();
  $("proposal-count").textContent = `${proposals.filter((proposal) => proposal.status === "pending").length} pending`;
  $("structure-stage").dataset.intrinsicDisabled = String(historical);
  const latest = latestCitableMessage();
  $("structure-from-message").dataset.intrinsicDisabled = String(historical || !latest);
  $("structure-from-message").disabled = historical || !latest || busy || !ready;
  $("structure-proposals").replaceChildren();
  if (!proposals.length) $("structure-proposals").append(node("p", "empty-copy", "No structure proposals at this revision. Stage one below or ask Dao to propose knowledge from your conversation."));
  [...proposals].reverse().forEach((proposal) => {
    const card = node("article", `proposal-card proposal-${proposal.status}`);
    const heading = node("div", "proposal-heading");
    heading.append(node("strong", "", titleCase(proposal.status || "pending")), node("span", "proposal-origin", `${titleCase(proposal.source || "operator")} · ${formatTime(proposal.created_at)}`));
    card.append(heading, node("p", "proposal-rationale", proposal.rationale || ""), node("code", "proposal-id", proposal.id));
    const operations = Array.isArray(proposal.operations) ? proposal.operations : [];
    const list = node("ol", "proposal-operations");
    operations.forEach((operation) => {
      const record = operation.node || operation.relation || {};
      const label = record.label || record.id || operation.relation_id || "";
      const item = node("li", "", `${titleCase(operation.operation || "Operation")}${label ? `: ${label}` : ""}`);
      if (record.source && record.target) item.append(node("div", "field-help", `${record.source} → ${record.target} · ${titleCase(record.kind || "relationship")}`));
      appendCitations(item, operation);
      list.append(item);
    });
    card.append(list);
    addJsonDetails(card, proposal, "Inspect complete proposal");
    if (proposal.status === "pending") {
      const actions = node("div", "proposal-actions");
      ["accept", "reject"].forEach((decision) => {
        const button = node("button", decision === "accept" ? "primary-button" : "secondary-button", titleCase(decision));
        button.type = "button";
        button.dataset.mutation = "";
        button.dataset.intrinsicDisabled = String(historical);
        button.disabled = busy || !ready || historical;
        button.setAttribute("aria-label", `${titleCase(decision)} proposal ${proposal.id}`);
        button.addEventListener("click", () => mutate(async () => {
          try {
            await api("/api/structure/review", { branch, expected_head: idOf(workspace.head), proposal_id: proposal.id, decision });
          } catch (error) {
            try { await refresh(); } catch { /* Preserve the review failure. */ }
            if (decision === "accept" && /stale|graph.*chang|relationship.*chang/i.test(error.message)) {
              throw new Error("This proposal is based on earlier relationships. Stage a new proposal against the current graph before accepting it.");
            }
            throw error;
          }
          resetVerdict();
          await refresh();
          $("structure-status").textContent = decision === "accept" ? "Proposal accepted. Its operations are now saved knowledge." : "Proposal rejected. Its operations were not applied.";
          notify(decision === "accept" ? "Proposal accepted. Its operations are now saved knowledge." : "Proposal rejected. Its operations were not applied.");
        }));
        actions.append(button);
      });
      card.append(actions);
      if (historical) card.append(node("p", "field-help", "Proposal review is available at the current branch revision."));
    }
    $("structure-proposals").append(card);
  });
  const graph = versionedState().relationships || {};
  const nodes = graph.nodes || {};
  const relations = graph.relations || {};
  const accepted = $("accepted-structure");
  accepted.replaceChildren();
  if (!Object.keys(nodes).length && !Object.keys(relations).length) accepted.append(node("p", "empty-copy", "No accepted structure at this revision."));
  Object.entries(nodes).forEach(([id, record]) => {
    const card = node("article", "accepted-record");
    card.append(node("strong", "", record.label || id), node("p", "field-help", `${titleCase(record.kind || "node")} · ${id}`));
    if (record.content) card.append(node("p", "proposal-rationale", record.content));
    appendCitations(card, record);
    const dependsOn = Object.values(relations).filter((relation) => relation.kind === "depends_on" && relation.source === id).map((relation) => relation.target);
    const dependents = workspace?.dependencies?.[id] || [];
    [["Depends on", dependsOn], ["Used by", dependents]].forEach(([label, ids]) => {
      if (!ids.length) return;
      const list = node("ul", "dependency-list");
      ids.forEach((relatedId) => {
        const related = nodes[relatedId] || {};
        const item = node("li", "", related.label || relatedId);
        appendCitations(item, related);
        list.append(item);
      });
      card.append(node("div", "section-label", label), list);
    });
    accepted.append(card);
  });
  Object.entries(relations).forEach(([id, record]) => {
    const card = node("article", "accepted-record");
    card.append(node("strong", "", `${nodes[record.source]?.label || record.source} → ${nodes[record.target]?.label || record.target}`), node("p", "field-help", `${titleCase(record.kind || "relationship")} · ${id}`));
    appendCitations(card, record);
    accepted.append(card);
  });
  (graph.observations || []).forEach((observation) => {
    const card = node("article", "accepted-record");
    card.append(node("strong", "", `Observation: ${observation.relation_id}`), node("p", "field-help", `${observation.action} · ${observation.context}`), node("p", "proposal-rationale", observation.content));
    appendCitations(card, observation);
    accepted.append(card);
  });
}

async function refresh(nextBranch = branch) {
  const response = await api(`/api/state?branch=${encodeURIComponent(nextBranch)}`);
  if (response.csrf) csrf = response.csrf;
  workspace = { ...response, config: response.config || workspace?.config };
  if (nextBranch !== branch) {
    $("relationship-status").textContent = "";
    $("structure-status").textContent = "";
  }
  branch = nextBranch;
  renderWorkspace();
}

function addEvidence(initial = {}) {
  const card = node("div", "evidence-card");
  const header = node("div", "evidence-card-header");
  const remove = node("button", "icon-button", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "Remove evidence source");
  remove.dataset.mutation = "";
  remove.addEventListener("click", () => card.remove());
  header.append(remove);
  const source = node("input");
  source.className = "evidence-source";
  source.placeholder = "Source name or reference";
  source.value = initial.source || "";
  source.maxLength = 4000;
  source.setAttribute("aria-label", "Evidence source");
  source.dataset.mutation = "";
  const content = node("textarea");
  content.className = "evidence-content";
  content.rows = 3;
  content.placeholder = "What does this source show?";
  content.value = initial.content || "";
  content.maxLength = 24000;
  content.setAttribute("aria-label", "Evidence content");
  content.dataset.mutation = "";
  const options = node("div", "evidence-options");
  const stanceLabel = node("label", "", "Stance");
  const stance = node("select", "evidence-stance");
  stance.setAttribute("aria-label", "Evidence stance");
  ["support", "contradict", "neutral"].forEach((value) => {
    const option = node("option", "", titleCase(value));
    option.value = value;
    option.selected = value === (initial.stance || "support");
    stance.append(option);
  });
  stance.dataset.mutation = "";
  stanceLabel.append(stance);
  const reliabilityLabel = node("label", "", "Reliability (0–1)");
  const reliability = node("input", "evidence-reliability");
  reliability.setAttribute("aria-label", "Evidence reliability");
  reliability.type = "number";
  reliability.min = "0";
  reliability.max = "1";
  reliability.step = "0.05";
  reliability.value = initial.reliability ?? 0.8;
  reliability.dataset.mutation = "";
  reliabilityLabel.append(reliability);
  options.append(stanceLabel, reliabilityLabel);
  card.append(header, source, content, options);
  $("evidence-list").append(card);
  setBusy(busy);
}

function renderDecision(result) {
  const container = $("decision-result");
  container.replaceChildren(node("div", "result-heading", "Saved decision"));
  const summary = node("div", "recommendation-card");
  const recommendation = result.recommendation === "act" ? `Act: ${result.selected_action || "selected action"}` : result.recommendation === "wait" ? "Wait for information" : result.recommendation === "abstain" ? "Abstain" : scalar(result.recommendation || result.selected_action || "Result recorded");
  summary.append(node("h3", "", recommendation));
  if (result.reason) summary.append(node("p", "", scalar(result.reason)));
  if (result.recommendation === "wait" && result.waiting_plan?.reconsider_when) {
    summary.append(node("p", "", `Reconsider: ${scalar(result.waiting_plan.reconsider_when)}`));
  }
  container.append(summary);
  if (Array.isArray(result.blocked_actions) && result.blocked_actions.length) {
    container.append(node("p", "relationship-warning", `Withheld by unresolved severe conflicts: ${result.blocked_actions.map((action) => typeof action === "string" ? action : action.name || action.action || scalar(action)).join(", ")}`));
  }
  const metrics = [
    ["Value of new information", result.expected_value_of_information],
    ["Utility of waiting", result.wait_utility]
  ].filter(([, value]) => typeof value === "number");
  if (metrics.length) {
    const grid = node("div", "usage-totals");
    metrics.forEach(([label, value]) => grid.append(metricCard(formatNumber(value, 3), label)));
    container.append(grid);
  }
  const scores = Array.isArray(result.scores) ? result.scores : [];
  const utilities = scores.map((score) => score.adjusted_utility ?? score.utility ?? score.expected_utility ?? score.score ?? 0);
  const minimum = Math.min(0, ...utilities);
  const maximum = Math.max(1, ...utilities);
  scores.forEach((score, index) => {
    const card = node("div", "score-card");
    const label = node("div", "score-label");
    label.append(node("span", "", score.name || score.action || `Action ${index + 1}`), node("span", "score-value", formatNumber(utilities[index], 3)));
    card.append(label);
    const track = node("div", "score-track");
    const fill = node("span");
    fill.style.width = `${Math.max(0, Math.min(100, 100 * (utilities[index] - minimum) / (maximum - minimum)))}%`;
    track.append(fill);
    track.setAttribute("aria-hidden", "true");
    card.append(track);
    container.append(card);
  });
  addJsonDetails(container, result);
}

function renderAudit(result) {
  const container = $("audit-result");
  container.replaceChildren(node("div", "result-heading", "AUDIT VERDICT"));
  const card = node("div", "recommendation-card");
  const graphDigest = workspace?.relationship_digest || workspace?.relationships?.digest;
  const stale = Boolean(graphDigest && result.relationship_digest !== graphDigest);
  if (stale || !result.allowed) card.append(node("div", "result-kicker", stale ? "Previous verdict" : "Approval withheld"));
  card.append(node("h3", "", stale ? "Review changed relationships" : titleCase(result.verdict || (result.allowed ? "Allowed" : "Review required"))));
  if (stale) card.append(node("p", "", "Relationships have changed since this audit. Review the claim again before saving an artifact."));
  const reasons = Array.isArray(result.reasons) ? result.reasons : result.reasons ? [result.reasons] : [];
  reasons.forEach((reason) => card.append(node("p", "", scalar(reason))));
  container.append(card);
  addJsonDetails(container, result, "Inspect evidence and verdict");
  auditVerdict = result;
  updateArtifactGate();
}

async function artifactClaim(name, content) {
  if (!window.crypto?.subtle) throw new Error("Artifact verification requires Web Crypto. Open Dao on localhost or HTTPS.");
  const digest = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(content.trim()));
  const hex = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `artifact:${name.trim()}:${hex}`;
}

async function updateArtifactGate() {
  const name = $("artifact-name").value.trim();
  const content = $("artifact-content").value.trim();
  let claim = "";
  try { claim = await artifactClaim(name, content); } catch { /* The prepare action explains an unavailable Web Crypto API. */ }
  if (name !== $("artifact-name").value.trim() || content !== $("artifact-content").value.trim()) return;
  const graphDigest = workspace?.relationship_digest || workspace?.relationships?.digest;
  const currentGraph = !graphDigest || auditVerdict?.relationship_digest === graphDigest;
  const allowed = Boolean(name && content && auditVerdict?.allowed && auditVerdict.verdict_id && auditVerdict.claim === claim && !auditVerdict.action && currentGraph);
  $("artifact-save").dataset.intrinsicDisabled = String(!allowed);
  $("artifact-save").disabled = busy || !allowed || !ready;
  $("artifact-status").textContent = allowed ? "Ready to save." : auditVerdict ? auditVerdict.claim === claim && !auditVerdict.action && currentGraph ? "Review does not allow saving." : "Prepare a matching review to save." : "";
}

async function submitChat() {
  const message = $("message-input").value.trim();
  if (!message || busy || !ready) return;
  await mutate(async () => {
    const expectedHead = idOf(workspace.head);
    if (!messageRecords().length) $("messages").replaceChildren();
    $("starter-prompts").hidden = true;
    renderMessage({ role: "user", content: message });
    const assistant = renderMessage({ role: "assistant", content: "" }, true);
    $("message-input").value = "";
    scrollMessages();
    let complete = false;
    let proposalCount = 0;
    let receivedText = "";
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dao-CSRF": csrf },
        body: JSON.stringify({ branch, message, expected_head: expectedHead }),
        credentials: "same-origin"
      });
      if (!response.ok) throw await responseError(response);
      if (!response.body) throw new Error("This browser cannot read the conversation stream.");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      function consume(line) {
        if (!line.trim()) return;
        const event = JSON.parse(line);
        if (event.type === "delta") {
          receivedText += event.text || "";
          assistant.content.textContent = receivedText;
          scrollMessages();
        } else if (event.type === "done") {
          complete = true;
        } else if (event.type === "proposal") {
          proposalCount += 1;
          $("structure-status").textContent = "Dao proposed structured knowledge. Review the pending proposal in Relationships.";
        } else if (event.type === "error") {
          throw new Error(event.error || "The conversation stopped before completion.");
        }
      }
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let newline;
          while ((newline = buffer.indexOf("\n")) !== -1) {
            const line = buffer.slice(0, newline);
            buffer = buffer.slice(newline + 1);
            consume(line);
          }
        }
        buffer += decoder.decode();
        if (buffer.trim()) consume(buffer);
      } finally {
        reader.releaseLock();
      }
      if (!complete) throw new Error("The stream ended before completion. Your saved state will be reloaded.");
      assistant.item.classList.remove("streaming");
      await refresh();
      if (proposalCount) notify(`${proposalCount === 1 ? "A structure proposal is" : `${proposalCount} structure proposals are`} pending review in Relationships.`);
    } catch (error) {
      assistant.item.classList.remove("streaming");
      if (!receivedText) $("message-input").value = message;
      try { await refresh(); } catch { /* Keep the original failure visible. */ }
      $("structure-status").textContent = "The conversation failed. New proposals from this turn were discarded; saved proposals are shown above.";
      throw error;
    }
  });
  $("message-input").focus();
}

$("chat-form").addEventListener("submit", (event) => { event.preventDefault(); submitChat(); });
$("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitChat(); }
});
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => { $("message-input").value = button.dataset.prompt; $("message-input").focus(); });
});

const tabs = [...document.querySelectorAll("[role=tab]")];
const wideLayout = window.matchMedia("(min-width: 1200px)");

function setToolsOpen(open) {
  const focusedInTools = $("workspace-tools").contains(document.activeElement);
  $("app-shell").classList.toggle("tools-open", open);
  $("workspace-tools").hidden = !open;
  $("tools-toggle").setAttribute("aria-expanded", String(open));
  const label = open ? wideLayout.matches ? "Hide tools" : "Back to conversation" : "Tools";
  $("tools-toggle").setAttribute("aria-label", label);
  if (open && !wideLayout.matches) {
    const shortLabel = node("span", "utility-short", "← Chat");
    shortLabel.setAttribute("aria-hidden", "true");
    $("tools-toggle").replaceChildren(node("span", "utility-label", label), shortLabel);
  } else $("tools-toggle").textContent = label;
  if (!open && focusedInTools) $("tools-toggle").focus();
}

$("tools-toggle").addEventListener("click", () => setToolsOpen($("workspace-tools").hidden));
$("alert-close").addEventListener("click", () => {
  clearError();
  if (errorFocus?.isConnected && errorFocus.getClientRects().length) errorFocus.focus();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !wideLayout.matches && !$("workspace-tools").hidden) {
    setToolsOpen(false);
    $("tools-toggle").focus();
  }
});
wideLayout.addEventListener("change", () => setToolsOpen(wideLayout.matches));
setToolsOpen(wideLayout.matches);
if (!wideLayout.matches) $("history-details").open = false;

function selectTab(tab) {
  tabs.forEach((item) => {
    const selected = item === tab;
    item.setAttribute("aria-selected", String(selected));
    item.tabIndex = selected ? 0 : -1;
    $(item.dataset.panel).hidden = !selected;
  });
}
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    let next;
    if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
    if (event.key === "ArrowLeft") next = tabs[(index + tabs.length - 1) % tabs.length];
    if (event.key === "Home") next = tabs[0];
    if (event.key === "End") next = tabs[tabs.length - 1];
    if (next) { event.preventDefault(); selectTab(next); next.focus(); }
  });
});

$("branch-select").addEventListener("change", () => mutate(async () => {
  const next = $("branch-select").value;
  try {
    await refresh(next);
  } catch (error) {
    $("branch-select").value = branch;
    throw error;
  }
}));

async function createBranch() {
  const name = $("branch-name").value.trim();
  if (!name) { $("branch-name").focus(); return; }
  await mutate(async () => {
    await api("/api/branches", { name, from_commit: idOf(workspace.head) });
    await refresh(name);
    $("branch-name").value = "";
    notify(`Branch created: ${name}.`);
  });
}
$("create-branch").addEventListener("click", createBranch);
$("branch-name").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); createBranch(); } });

$("decision-example").addEventListener("click", () => mutate(async () => {
  const example = await api("/api/decision-example");
  $("decision-input").value = JSON.stringify(example.problem || example, null, 2);
  renderDecisionPreview();
  $("decision-result").replaceChildren();
}));

function renderDecisionPreview() {
  const container = $("decision-preview");
  container.replaceChildren();
  let problem;
  try { problem = JSON.parse($("decision-input").value); } catch {
    container.append(node("p", "field-help", "The decision model needs valid JSON before it can be evaluated."));
    return;
  }
  const actions = Array.isArray(problem?.actions) ? problem.actions : [];
  const scenarios = Array.isArray(problem?.scenarios) ? problem.scenarios : [];
  container.append(node("div", "section-label", `${actions.length} actions · ${scenarios.length} scenarios`));
  const list = node("ul", "model-actions");
  actions.forEach((action) => list.append(node("li", "", action?.name || "Unnamed action")));
  container.append(list);
}
$("decision-input").addEventListener("input", () => {
  renderDecisionPreview();
  $("decision-result").replaceChildren();
});
$("decision-run").addEventListener("click", () => mutate(async () => {
  let problem;
  try { problem = JSON.parse($("decision-input").value); } catch { throw new Error("The decision model needs valid JSON. Check its commas and quotation marks."); }
  const response = await api("/api/decision", { branch, expected_head: idOf(workspace.head), problem });
  renderDecision(response.result);
  await refresh();
}));

const relationshipExamples = {
  action: { operation: "node", node: { id: "launch", label: "Full launch", kind: "action", importance: 1 } },
  goal: { operation: "node", node: { id: "customer-trust", label: "Protect customer trust", kind: "goal", importance: 20 } },
  relation: { operation: "relation", relation: { id: "launch-trust", source: "launch", target: "customer-trust", kind: "effect", weight: 20, severe: true, actions: ["Full launch"] } },
  assess: { operation: "assess", relation_id: "launch-trust", belief: { positive: 0.1, neutral: 0.1, negative: 0.8 }, source: "Example review", content: "A launch before the reliability issue is fixed may undermine customer trust. Replace this with your evidence." },
  unknown: { operation: "assess", relation_id: "launch-trust", belief: null, source: "Example uncertainty review", content: "The prior assessment is now uncertain. This does not resolve the conflict." },
  observe: { operation: "observe", relation_id: "launch-trust", action: "Reversible pilot", context: "Example controlled pilot", before: "negative", after: "positive", source: "Example pilot report", content: "Record a reported transition with its action and context. This example does not establish causation." },
  reassess: { operation: "assess", relation_id: "launch-trust", belief: { positive: 0.9, neutral: 0.1, negative: 0 }, source: "Example follow-up review", content: "Replace with evidence supporting zero negative probability. Reassessment alone leaves the conflict open." },
  resolve: { operation: "resolve", relation_id: "launch-trust", evidence: [
    { source: "Example operator review", content: "Replace with evidence that the original concern is resolved.", stance: "support", reliability: 0.9 },
    { source: "Example outcome report", content: "Replace with a distinct reviewed source supporting resolution.", stance: "support", reliability: 0.9 }
  ] }
};

function messageCitation(message) {
  if (!message.id || ["failed", "interrupted"].includes(message.status) || typeof message.content !== "string") return null;
  const characters = Array.from(message.content);
  const unsupported = (character) => /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/u.test(character);
  for (let start = 0; start < characters.length;) {
    while (start < characters.length && unsupported(characters[start])) start += 1;
    let end = start;
    while (end < characters.length && end - start < 8192 && !unsupported(characters[end])) end += 1;
    const quote = characters.slice(start, end).join("");
    if (quote.trim()) return { message_id: message.id, start, end, quote };
    start = end < characters.length && unsupported(characters[end]) ? end + 1 : end;
  }
  return null;
}

function latestCitableMessage() {
  return [...messageRecords()].reverse().find((message) => message.role === "user" && messageCitation(message));
}

$("structure-from-message").addEventListener("click", () => {
  if (busy || !ready || historicalView()) return;
  const message = latestCitableMessage();
  if (!message) { showError(new Error("Send a message before starting a cited proposal.")); return; }
  const citation = messageCitation(message);
  const identity = window.crypto?.randomUUID ? window.crypto.randomUUID().replaceAll("-", "") : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
  const proposal = {
    rationale: "Capture a passage from the latest user message as a claim for operator review.",
    operations: [{ operation: "node", node: {
      id: `claim-${identity}`,
      label: Array.from(citation.quote.replace(/\s+/gu, " ").trim()).slice(0, 256).join(""),
      kind: "claim",
      importance: 1,
      content: Array.from(citation.quote.trim()).slice(0, 4000).join(""),
      citations: [citation]
    } }]
  };
  $("structure-input").value = JSON.stringify(proposal, null, 2);
  $("structure-status").textContent = "Copied the latest message into a cited claim starter. Edit its meaning and label, then stage it for review.";
  $("structure-input").focus();
});
$("structure-stage").addEventListener("click", () => mutate(async () => {
  let proposal;
  try { proposal = JSON.parse($("structure-input").value); } catch { throw new Error("The structure proposal needs valid JSON."); }
  if (!proposal || Array.isArray(proposal) || typeof proposal !== "object") throw new Error("A structure proposal must be a JSON object with rationale and operations.");
  if (Object.hasOwn(proposal, "branch") || Object.hasOwn(proposal, "expected_head")) throw new Error("A proposal cannot select a branch or override its current revision.");
  await api("/api/structure/propose", { ...proposal, branch, expected_head: idOf(workspace.head) });
  await refresh();
  $("structure-status").textContent = "Proposal staged. Review its operations and citations above, then accept or reject it.";
  notify("Structure proposal staged for review.");
}));
document.querySelectorAll("[data-relationship-example]").forEach((button) => {
  button.addEventListener("click", () => {
    $("relationship-input").value = JSON.stringify(relationshipExamples[button.dataset.relationshipExample], null, 2);
    $("relationship-status").textContent = "Example loaded.";
  });
});
$("relationship-refresh").addEventListener("click", () => mutate(async () => {
  await refresh();
  $("relationship-status").textContent = "Relationships refreshed for the current branch.";
}));
$("relationship-save").addEventListener("click", () => mutate(async () => {
  let operation;
  try { operation = JSON.parse($("relationship-input").value); } catch { throw new Error("The relationship operation needs valid JSON."); }
  if (!operation || Array.isArray(operation) || typeof operation !== "object") throw new Error("A relationship operation must be a JSON object.");
  if (Object.hasOwn(operation, "branch") || Object.hasOwn(operation, "expected_head")) throw new Error("The operation cannot select a branch or override its current revision.");
  await api("/api/relationships", { ...operation, branch, expected_head: idOf(workspace.head) });
  resetVerdict();
  await refresh();
  $("relationship-status").textContent = "";
  notify("Relationships saved in a new revision.");
}));

$("add-evidence").addEventListener("click", () => addEvidence());
$("audit-run").addEventListener("click", () => mutate(async () => {
  const claim = $("audit-claim").value.trim();
  if (!claim) throw new Error("Enter a claim to review.");
  const evidence = [...$("evidence-list").querySelectorAll(".evidence-card")].map((card) => {
    const reliability = Number(card.querySelector(".evidence-reliability").value);
    const source = card.querySelector(".evidence-source").value.trim();
    const content = card.querySelector(".evidence-content").value.trim();
    if (!source || !content) throw new Error("Each evidence card needs a source and content.");
    if (!Number.isFinite(reliability) || reliability < 0 || reliability > 1) throw new Error("Evidence reliability must be between 0 and 1.");
    return { source, content, stance: card.querySelector(".evidence-stance").value, reliability };
  });
  const response = await api("/api/audit", { branch, expected_head: idOf(workspace.head), claim, evidence });
  renderAudit(response.result);
  await refresh();
}));
$("artifact-name").addEventListener("input", updateArtifactGate);
$("artifact-content").addEventListener("input", updateArtifactGate);
$("artifact-prepare").addEventListener("click", () => mutate(async () => {
  const name = $("artifact-name").value.trim();
  const content = $("artifact-content").value.trim();
  if (!name || !content) throw new Error("Give the artifact a name and content before preparing its review.");
  $("audit-claim").value = await artifactClaim(name, content);
  resetVerdict();
  $("audit-result").replaceChildren();
  $("audit-claim").scrollIntoView({ behavior: "smooth", block: "center" });
  notify("Artifact claim prepared. Review its supporting sources before saving.");
}));
$("artifact-save").addEventListener("click", () => mutate(async () => {
  if (!auditVerdict?.allowed) throw new Error("A current allowed review is required.");
  const name = $("artifact-name").value.trim();
  const content = $("artifact-content").value;
  if (!name || !content.trim()) throw new Error("Give the artifact a name and content.");
  if (auditVerdict.claim !== await artifactClaim(name, content)) throw new Error("The artifact changed after the review. Prepare and review a claim for its updated content.");
  await api("/api/artifact", { branch, expected_head: idOf(workspace.head), name, content, verdict_id: auditVerdict.verdict_id });
  await refresh();
  notify(`Saved ${name} with its review result.`);
}));
$("memory-save").addEventListener("click", () => mutate(async () => {
  const key = $("memory-key").value.trim();
  const value = $("memory-value").value.trim();
  if (!key || !value) throw new Error("Add a key and value for this memory item.");
  await api("/api/memory", { branch, expected_head: idOf(workspace.head), key, value });
  await refresh();
  $("memory-key").value = "";
  $("memory-value").value = "";
  notify("Memory saved in a new revision.");
}));
$("verify-button").addEventListener("click", async () => {
  $("verify-button").disabled = true;
  try {
    const result = await api("/api/verify");
    const failed = result.ok === false || result.valid === false || result.integrity === false || (Array.isArray(result.errors) && result.errors.length > 0);
    if (failed) { showError(new Error(`Integrity verification found an issue: ${JSON.stringify(result)}`)); }
    else { notify(`Integrity verified${result.commits !== undefined ? ` · ${scalar(result.commits)} revisions` : ""}.`); }
  } catch (error) { showError(error); } finally { $("verify-button").disabled = false; }
});
$("export-button").addEventListener("click", async () => {
  if (busy || !ready) return;
  $("export-button").disabled = true;
  try {
    const response = await fetch(`/api/export?branch=${encodeURIComponent(branch)}`, { credentials: "same-origin" });
    if (!response.ok) throw await responseError(response);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = node("a");
    link.href = url;
    link.download = `dao-${branch.replace(/[^a-zA-Z0-9_-]/g, "_")}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify("Workspace exported with its saved revisions.");
  } catch (error) { showError(error); } finally { $("export-button").disabled = busy || !ready; }
});

async function initialize() {
  setBusy(false);
  resetVerdict();
  addEvidence({ source: "Example project brief", content: "The plan is a small reversible experiment with a defined rollback step.", stance: "support", reliability: 0.85 });
  addEvidence({ source: "Example review note", content: "The proposed artifact is a draft plan for internal review; no external action is authorized by this evidence.", stance: "support", reliability: 0.8 });
  try {
    workspace = await api("/api/bootstrap");
    csrf = workspace.csrf || "";
    branch = workspace.branch || (workspace.branches || []).find((item) => idOf(item.head) === idOf(workspace.head))?.name || "main";
    ready = true;
    renderWorkspace();
    try {
      const example = await api("/api/decision-example");
      $("decision-input").value = JSON.stringify(example.problem || example, null, 2);
      renderDecisionPreview();
    } catch (error) {
      $("decision-preview").replaceChildren(node("p", "field-help", "Example unavailable. Enter your decision model below."));
      $("decision-editor").open = true;
      $("decision-help").textContent = `Example could not be loaded: ${error.message}. You can still enter a decision model.`;
    }
  } catch (error) {
    $("provider-badge").textContent = "Connection unavailable";
    $("provider-description").textContent = "Workspace unavailable. Check that Dao is running, then reload.";
    $("branch-select").replaceChildren(node("option", "", "Unavailable"));
    $("history-list").replaceChildren(node("li", "empty-copy", "Refresh this page after the server is available."));
    showError(error);
  }
  setBusy(false);
}

initialize();
