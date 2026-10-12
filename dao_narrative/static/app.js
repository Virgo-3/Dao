"use strict";
const $ = (id) => document.getElementById(id);
let workspace, branch = "main", csrf = "", busy = false, ready = false;
let documentDirty = false, noteDirty = false, toastTimer, errorFocus;
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function state() { return workspace?.head?.state || {}; }
function headId() { return workspace?.head?.id || ""; }
function documentText(value) { return typeof value === "string" ? value : value?.content || ""; }
function clearError() { $("global-alert").hidden = true; }
function showError(error) {
  errorFocus = document.activeElement;
  $("alert-message").textContent = error instanceof Error ? error.message : String(error);
  $("global-alert").hidden = false;
}
function notify(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000);
}
function setBusy(value) {
  busy = value;
  document.querySelectorAll("[data-mutation]").forEach((element) => {
    element.disabled = busy || !ready || element.dataset.intrinsicDisabled === "true";
  });
  $("branch-select").disabled = $("export-button").disabled = busy || !ready;
  $("send-label").textContent = busy ? "Working" : "Send";
  $("chat-form").setAttribute("aria-busy", String(busy));
}
async function responseError(response) {
  try { const body = await response.json(); return new Error(body.error || "Request failed (" + response.status + ")."); }
  catch { return new Error("Request failed (" + response.status + "). Refresh and try again."); }
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json", "X-Dao-CSRF": csrf },
    body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin"
  });
  if (!response.ok) throw await responseError(response);
  return response.json();
}
async function mutate(operation) {
  if (busy || !ready) return;
  clearError(); setBusy(true);
  try { await operation(); } catch (error) { showError(error); } finally { setBusy(false); }
}
function request(data) { return { ...data, branch, expected_head: headId() }; }
function canLeaveEditors() {
  return !(documentDirty || noteDirty) || window.confirm("Leave your unsaved document or note edits?");
}
function resetEditors() {
  $("document-name").value = "scene.md";
  $("document-content").value = $("document-status").textContent = "";
  $("memory-key").value = $("memory-value").value = "";
  documentDirty = noteDirty = false;
}
function icon(className = "") {
  const image = node("img", className);
  image.src = "/static/dao.svg"; image.alt = ""; image.setAttribute("aria-hidden", "true");
  return image;
}
function renderMessage(message, streaming = false) {
  const user = message.role === "user";
  const item = node("article", "message " + (user ? "user" : "assistant") + (streaming ? " streaming" : ""));
  const avatar = node("span", "message-avatar", user ? "You" : undefined);
  if (!user) avatar.append(icon());
  const body = node("div", "message-body"), content = node("div", "message-content", message.content || "");
  body.append(node("div", "message-label", user ? "You" : "Dao Narrative"), content);
  if (message.status === "failed") body.append(node("div", "message-status", "Incomplete reply"));
  item.append(avatar, body); $("messages").append(item);
  return { item, content };
}
function scrollMessages() { $("messages").scrollTop = $("messages").scrollHeight; }
function renderWorkspace() {
  const config = workspace.config || {};
  $("provider-badge").textContent = config.provider === "demo" ? "Offline demo" : config.model;
  $("provider-description").textContent = config.provider === "demo"
    ? "Offline demo · example replies. Your conversation and writing are saved."
    : "Develop your story with Dao Narrative. Saved notes and documents accompany each reply.";
  $("branch-select").replaceChildren();
  (workspace.branches || []).forEach((draft) => {
    const option = node("option", "", draft.name);
    option.value = draft.name; option.selected = draft.name === branch; $("branch-select").append(option);
  });
  $("revision-label").textContent = "Saved version " + headId().slice(0, 8);
  $("messages").replaceChildren();
  const messages = state().messages || [];
  $("starter-prompts").hidden = messages.length > 0;
  if (messages.length) messages.forEach((message) => renderMessage(message));
  else { const empty = node("section", "empty-state"); empty.append(icon("empty-emblem"), node("h2", "", "Where next?")); $("messages").append(empty); }
  scrollMessages(); renderHistory(); renderNotes(); renderDocuments(); renderUsage(); setBusy(busy);
}
function renderHistory() {
  const history = workspace.history || [];
  $("history-count").textContent = history.length; $("history-list").replaceChildren();
  history.forEach((version) => {
    const current = version.id === headId(), item = node("li", "history-item" + (current ? " current" : ""));
    const label = version.kind === "bootstrap" ? "Story started" : version.label;
    item.append(node("div", "history-title", label), node("div", "history-meta", new Date(version.created_at).toLocaleString()));
    const restore = node("button", "history-action", current ? "Current" : "Restore");
    restore.type = "button"; restore.dataset.mutation = ""; restore.dataset.intrinsicDisabled = String(current);
    restore.setAttribute("aria-label", "Restore version " + version.id.slice(0, 8));
    restore.addEventListener("click", () => {
      if (!canLeaveEditors()) return;
      mutate(async () => {
        await api("/api/restore", request({ commit_id: version.id }));
        resetEditors(); await refresh(); notify("Restored as a new version. Earlier writing stays saved.");
      });
    });
    item.append(restore); $("history-list").append(item);
  });
}
function renderNotes() {
  $("note-list").replaceChildren();
  const notes = Object.entries(state().memory || {});
  if (!notes.length) $("note-list").append(node("p", "empty-copy", "Keep character details, established facts, or open questions here."));
  notes.forEach(([key, value]) => {
    const item = node("div", "event-item"), edit = node("button", "text-button", key);
    edit.dataset.mutation = "";
    edit.addEventListener("click", () => {
      if (noteDirty && !window.confirm("Replace your unsaved note edits?")) return;
      $("memory-key").value = key; $("memory-value").value = value; noteDirty = false; $("memory-value").focus();
    });
    item.append(edit, node("p", "message-content", value)); $("note-list").append(item);
  });
}
function renderDocuments() {
  $("document-list").replaceChildren();
  const documents = Object.entries(state().artifacts || {});
  if (!documents.length) $("document-list").append(node("p", "empty-copy", "Save a scene or chapter below."));
  documents.forEach(([name, value]) => {
    const open = node("button", "text-button", name); open.dataset.mutation = "";
    open.addEventListener("click", () => {
      if (documentDirty && !window.confirm("Replace your unsaved document edits?")) return;
      $("document-name").value = name; $("document-content").value = documentText(value);
      documentDirty = false; $("document-status").textContent = "Saved in this draft."; $("document-content").focus();
    });
    $("document-list").append(open, node("br"));
  });
}
function renderUsage() {
  const usage = workspace.usage || {}, tokens = usage.total_tokens || 0, budget = workspace.config.token_budget;
  $("usage-summary").textContent = tokens.toLocaleString() + " / " + budget.toLocaleString() + " recorded tokens across all drafts.";
  $("usage-cost").textContent = workspace.config.provider === "demo" ? "Offline demo estimates · no provider cost."
    : workspace.config.pricing_configured ? "Estimated cost: $" + ((usage.cost_microusd || 0) / 1e6).toFixed(5) : "Pricing not configured.";
}
async function refresh(nextBranch = branch) {
  const next = await api("/api/state?branch=" + encodeURIComponent(nextBranch));
  if (nextBranch !== branch) resetEditors();
  workspace = next; branch = nextBranch; csrf = next.csrf || csrf; renderWorkspace();
}
async function submitChat() {
  const message = $("message-input").value.trim();
  if (!message || busy || !ready) return;
  await mutate(async () => {
    if (!(state().messages || []).length) $("messages").replaceChildren();
    $("starter-prompts").hidden = true; renderMessage({ role: "user", content: message });
    const assistant = renderMessage({ role: "assistant", content: "" }, true);
    $("message-input").value = ""; scrollMessages();
    let complete = false, receivedText = "";
    try {
      const response = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Dao-CSRF": csrf },
        body: JSON.stringify(request({ message })), credentials: "same-origin"
      });
      if (!response.ok) throw await responseError(response);
      if (!response.body) throw new Error("This browser cannot read the conversation stream.");
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
      function consume(line) {
        if (!line.trim()) return;
        const event = JSON.parse(line);
        if (event.type === "delta") { receivedText += event.text || ""; assistant.content.textContent = receivedText; scrollMessages(); }
        else if (event.type === "done") complete = true;
        else if (event.type === "error") throw new Error(event.error || "The reply did not complete.");
      }
      try {
        while (true) {
          const { done, value } = await reader.read(); if (done) break;
          buffer += decoder.decode(value, { stream: true }); let newline;
          while ((newline = buffer.indexOf("\n")) !== -1) {
            const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1); consume(line);
          }
        }
        buffer += decoder.decode(); if (buffer.trim()) consume(buffer);
      } finally { reader.releaseLock(); }
      if (!complete) throw new Error("The reply stopped before completion. Reloading saved writing.");
      await refresh();
    } catch (error) {
      assistant.item.classList.remove("streaming"); if (!receivedText) $("message-input").value = message;
      try { await refresh(); } catch { /* Preserve the original diagnostic. */ }
      throw error;
    }
  });
  $("message-input").focus();
}
function download(name, content) {
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" })), link = node("a");
  link.href = url; link.download = name.replace(/[\\/:*?"<>|]/g, "_"); link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$("chat-form").addEventListener("submit", (event) => { event.preventDefault(); submitChat(); });
$("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitChat(); }
});
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => { $("message-input").value = button.dataset.prompt; $("message-input").focus(); });
});
$("branch-select").addEventListener("change", () => {
  if (!canLeaveEditors()) { $("branch-select").value = branch; return; }
  mutate(async () => { try { await refresh($("branch-select").value); } catch (error) { $("branch-select").value = branch; throw error; } });
});
async function createDraft() {
  const name = $("branch-name").value.trim(); if (!name) { $("branch-name").focus(); return; }
  if (!canLeaveEditors()) return;
  await mutate(async () => {
    await api("/api/branches", { name, from_commit: headId() }); await refresh(name);
    $("branch-name").value = ""; notify("Draft created: " + name + ".");
  });
}
$("create-branch").addEventListener("click", createDraft);
$("branch-name").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); createDraft(); } });
["document-name", "document-content"].forEach((id) => $(id).addEventListener("input", () => {
  documentDirty = true; $("document-status").textContent = "Unsaved changes";
}));
["memory-key", "memory-value"].forEach((id) => $(id).addEventListener("input", () => { noteDirty = true; }));
$("document-new").addEventListener("click", () => {
  if (documentDirty && !window.confirm("Replace your unsaved document edits?")) return;
  $("document-name").value = $("document-content").value = ""; $("document-status").textContent = "";
  documentDirty = false; $("document-name").focus();
});
$("document-save").addEventListener("click", () => mutate(async () => {
  const name = $("document-name").value.trim(); if (!name) throw new Error("Give the document a name.");
  await api("/api/artifact", request({ name, content: $("document-content").value }));
  documentDirty = false; $("document-status").textContent = "Saved in a new version.";
  await refresh(); notify("Saved " + name + ".");
}));
$("document-download").addEventListener("click", () => {
  download($("document-name").value.trim() || "scene.md", $("document-content").value);
});
$("memory-save").addEventListener("click", () => mutate(async () => {
  const key = $("memory-key").value.trim(), value = $("memory-value").value.trim();
  if (!key || !value) throw new Error("Add a name and text for this story note.");
  await api("/api/memory", request({ key, value })); noteDirty = false;
  $("memory-key").value = $("memory-value").value = ""; await refresh(); notify("Story note saved.");
}));
$("export-button").addEventListener("click", () => mutate(async () => {
  const exported = await api("/api/export?branch=" + encodeURIComponent(branch));
  download(exported.name, exported.content); notify("Exported the saved draft as Markdown.");
}));
$("alert-close").addEventListener("click", () => {
  clearError(); if (errorFocus?.isConnected && errorFocus.getClientRects().length) errorFocus.focus();
});
const tabs = [...document.querySelectorAll("[role=tab]")];
function selectTab(tab) {
  tabs.forEach((item) => {
    const selected = item === tab; item.setAttribute("aria-selected", String(selected)); item.tabIndex = selected ? 0 : -1;
    $(item.dataset.panel).hidden = !selected;
  });
}
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    const positions = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 };
    if (Object.hasOwn(positions, event.key)) { event.preventDefault(); const next = tabs[positions[event.key]]; selectTab(next); next.focus(); }
  });
});
const wideLayout = window.matchMedia("(min-width: 1200px)");
function setToolsOpen(open) {
  const focused = $("workspace-tools").contains(document.activeElement);
  $("app-shell").classList.toggle("tools-open", open); $("workspace-tools").hidden = !open;
  $("tools-toggle").setAttribute("aria-expanded", String(open));
  $("tools-toggle").textContent = open ? (wideLayout.matches ? "Hide panel" : "Back to writing") : "Notes & documents";
  if (!open && focused) $("tools-toggle").focus();
}
$("tools-toggle").addEventListener("click", () => setToolsOpen($("workspace-tools").hidden));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !wideLayout.matches && !$("workspace-tools").hidden) setToolsOpen(false);
});
window.addEventListener("beforeunload", (event) => {
  if (documentDirty || noteDirty) { event.preventDefault(); event.returnValue = ""; }
});
wideLayout.addEventListener("change", () => setToolsOpen(wideLayout.matches));
setToolsOpen(wideLayout.matches);
if (!wideLayout.matches) $("history-details").open = false;
async function initialize() {
  setBusy(false);
  try {
    workspace = await api("/api/bootstrap"); csrf = workspace.csrf || ""; ready = true; renderWorkspace();
  } catch (error) {
    $("provider-badge").textContent = "Connection unavailable";
    $("provider-description").textContent = "Check that Dao Narrative is running, then reload."; showError(error);
  } finally { setBusy(false); }
}
initialize();
