import { consumeMcpPairing, fetchMcpClients, fetchMcpPairingRequests, forgetMcpClient, revokeMcpClient, rotateMcpClient, setMcpClientCapabilities, startMcpPairing } from "../src/lib/api";
import { formatCountdown, formatDateTime } from "../src/lib/format";
import { isMcpClientOnline } from "../src/lib/ui";
import type { McpClient, McpPairingRequest, McpPairingStart } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";
import { confirmAction } from "./modal";

function showToken(token: string, title: string): void {
  const dialog = element("dialog", { className: "action-dialog token-dialog" });
  const close = button("I saved the token", "primary-action");
  const copy = button("Copy", "quiet-button");
  copy.addEventListener("click", () => void navigator.clipboard.writeText(token));
  close.addEventListener("click", () => { dialog.close(); dialog.remove(); });
  dialog.append(element("p", { className: "eyebrow" }, "Shown once"), element("h2", {}, title), element("p", {}, "Store this client token now. It cannot be displayed again."), element("pre", { className: "token-output" }, token), element("div", { className: "dialog-actions" }, copy, close));
  document.body.append(dialog); dialog.showModal();
}

export function mountMcp(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let clients: McpClient[] = [];
  let requests: McpPairingRequest[] = [];
  let tab: "clients" | "requests" | "pairing" = "clients";
  let query = "";
  let loading = true;
  let busy = "";
  let errorMessage = "";
  let pairing: McpPairingStart | null = null;
  let active = true;
  let pairingPoll: number | null = null;
  const refreshButton = button("Refresh", "quiet-button");
  const tabs = element("div", { className: "filter-bar" });
  const content = element("div", { className: "mcp-content" });

  async function refresh(showLoading = false): Promise<void> {
    if (showLoading) loading = true;
    const results = await Promise.allSettled([fetchMcpClients(), fetchMcpPairingRequests(25)]);
    if (!active) return;
    if (results[0].status === "fulfilled") clients = results[0].value;
    if (results[1].status === "fulfilled") requests = results[1].value;
    errorMessage = results.some((result) => result.status === "rejected") ? "Some MCP registry data could not be loaded." : "";
    loading = false; refreshButton.disabled = false; render();
  }

  async function mutate(name: string, action: () => Promise<unknown>): Promise<void> {
    busy = name; errorMessage = ""; render();
    try { await action(); await refresh(); }
    catch (error) { errorMessage = error instanceof Error ? error.message : "MCP action failed."; }
    finally { if (active) { busy = ""; render(); } }
  }

  function clientCard(client: McpClient): HTMLElement {
    const revoked = Boolean(client.revoked_at);
    const online = !revoked && isMcpClientOnline(client);
    const reason = element("input", { className: "control-input", placeholder: "Revocation reason", "aria-label": `Reason to revoke ${client.client_label}` });
    const rotate = button("Rotate token", "quiet-button"); const revoke = button("Revoke", "quiet-button danger-text"); const forget = button("Forget", "quiet-button danger-text");
    const hasWorker = client.capabilities.includes("task-worker.v1");
    const worker = button(hasWorker ? "Disable worker" : "Enable worker", "quiet-button");
    rotate.disabled = revoke.disabled = forget.disabled = worker.disabled = Boolean(busy);
    rotate.addEventListener("click", () => void mutate(`rotate-${client.id}`, async () => { const result = await rotateMcpClient(client.id); showToken(result.token, `Rotated token for ${client.client_label || client.agent_id}`); }));
    revoke.addEventListener("click", async () => {
      if (!reason.value.trim()) { reason.focus(); return; }
      if (await confirmAction("Revoke MCP client", `Immediately revoke ${client.client_label || client.agent_id}? Active sessions using this token will stop working.`, "Revoke")) void mutate(`revoke-${client.id}`, () => revokeMcpClient(client.id, reason.value.trim()));
    });
    forget.addEventListener("click", async () => { if (await confirmAction("Forget revoked client", "Permanently remove this revoked registration from the console registry?", "Forget")) void mutate(`forget-${client.id}`, () => forgetMcpClient(client.id)); });
    worker.addEventListener("click", async () => {
      const message = hasWorker ? "Remove task-worker.v1 from this client?" : "Convert this dedicated client to an identity-bound remediation worker? This capability is privileged and revocable.";
      if (await confirmAction(hasWorker ? "Disable worker capability" : "Confirm worker conversion", message, hasWorker ? "Disable" : "Convert")) {
        const capabilities = hasWorker ? client.capabilities.filter((capability) => capability !== "task-worker.v1") : [...new Set([...client.capabilities, "task-worker.v1"])];
        void mutate(`worker-${client.id}`, () => setMcpClientCapabilities(client.id, capabilities, !hasWorker));
      }
    });
    return element("article", { className: "client-card" }, element("div", { className: "client-head" }, element("span", { className: `state-dot state-dot--${revoked ? "critical" : online ? "healthy" : "warning"}` }), element("div", { className: "record-copy" }, element("strong", {}, client.client_label || client.agent_id), element("small", {}, client.principal_id || client.agent_id)), element("span", { className: "state-label" }, revoked ? "revoked" : online ? "online" : "idle")), element("dl", { className: "inline-facts" }, element("div", {}, element("dt", {}, "Token"), element("dd", {}, client.token_hint || "—")), element("div", {}, element("dt", {}, "Last seen"), element("dd", {}, formatDateTime(client.last_seen_at))), element("div", {}, element("dt", {}, "Capabilities"), element("dd", {}, client.capabilities.join(", ") || "default"))), revoked ? element("div", { className: "task-quick-actions" }, forget) : element("div", { className: "client-actions" }, reason, rotate, revoke, worker));
  }

  function pairingForm(): HTMLElement {
    if (pairing) {
      const cancel = button("Cancel pairing", "quiet-button");
      cancel.addEventListener("click", () => { pairing = null; if (pairingPoll !== null) window.clearInterval(pairingPoll); pairingPoll = null; render(); });
      return element("section", { className: "pairing-wait" }, element("p", { className: "eyebrow" }, "Telegram approval pending"), element("h2", {}, "Approve the pairing request"), element("p", {}, `Request ${pairing.request_id} expires in ${formatCountdown(pairing.expires_at)}. The secret remains only in this browser until consumed.`), cancel);
    }
    const form = element("form", { className: "pairing-form" });
    const agent = element("select", { className: "control-input", "aria-label": "Agent type" }, ...["opencode", "claude", "codex", "cline", "fixer", "worker"].map((value) => element("option", { value }, value)));
    const label = element("input", { className: "control-input", required: true, placeholder: "Client label", "aria-label": "Client label" });
    const fingerprint = element("input", { className: "control-input", required: true, placeholder: "Host fingerprint", "aria-label": "Host fingerprint" });
    const submit = element("button", { className: "primary-action", type: "submit" }, "Start Telegram pairing");
    form.append(agent, label, fingerprint, submit);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); busy = "pairing"; render();
      try {
        pairing = await startMcpPairing({ agent_id: agent.value as "opencode", client_label: label.value.trim(), host_fingerprint: fingerprint.value.trim() });
        startPairingPoll();
      } catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to start pairing."; }
      finally { busy = ""; if (active) render(); }
    });
    return form;
  }

  function startPairingPoll(): void {
    if (pairingPoll !== null) window.clearInterval(pairingPoll);
    pairingPoll = window.setInterval(async () => {
      if (!pairing || busy === "consume") return;
      busy = "consume";
      try {
        const result = await consumeMcpPairing(pairing.request_id, pairing.pairing_secret);
        if (result.ok) { showToken(result.token, `Token for ${result.client.client_label || result.client.agent_id}`); pairing = null; if (pairingPoll !== null) window.clearInterval(pairingPoll); pairingPoll = null; tab = "clients"; await refresh(); }
        else if (!["pending", "approval_required"].includes(result.error.code)) { throw new Error(result.error.message); }
      } catch (error) { errorMessage = error instanceof Error ? error.message : "Pairing failed."; }
      finally { busy = ""; if (active) render(); }
    }, 2_000);
  }

  function renderTabs(): void {
    replaceChildren(tabs, ...(["clients", "requests", "pairing"] as const).map((name) => { const control = button(name === "pairing" ? "New pairing" : name[0].toUpperCase() + name.slice(1), `filter-button${tab === name ? " filter-button--active" : ""}`); control.addEventListener("click", () => { tab = name; render(); }); return control; }));
  }
  function render(): void {
    renderTabs();
    if (loading) { replaceChildren(content, element("div", { className: "loading-state" }, "Loading MCP registry")); return; }
    const normalized = query.trim().toLowerCase();
    const visible = clients.filter((client) => !normalized || [client.agent_id, client.client_label, client.principal_id, client.id].join(" ").toLowerCase().includes(normalized));
    replaceChildren(content, errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null, tab === "clients" ? element("div", { className: "client-grid" }, ...visible.map(clientCard)) : tab === "requests" ? element("div", { className: "request-list" }, ...requests.map((request) => element("div", { className: "run-row" }, element("span", { className: `state-dot state-dot--${request.status === "approved" || request.status === "consumed" ? "healthy" : request.status === "pending" ? "warning" : "critical"}` }), element("strong", {}, request.client_label || request.agent_id), element("span", {}, request.status), element("span", {}, request.delivery_status), element("time", {}, formatDateTime(request.created_at))))) : pairingForm());
  }
  const handleSearch = () => { query = searchInput.value; render(); };
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void refresh(true));
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "mcp-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Agent access"), element("h1", { id: "mcp-heading" }, "MCP Clients"), element("p", { className: "inbox-intro" }, "Per-client identity, pairing, revocation, and worker capability.")), refreshButton), tabs, content));
  void refresh(true);
  return () => { active = false; if (pairingPoll !== null) window.clearInterval(pairingPoll); searchInput.removeEventListener("input", handleSearch); };
}
