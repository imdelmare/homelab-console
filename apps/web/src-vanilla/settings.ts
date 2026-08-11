import { fetchMcpClients, fetchSession } from "../src/lib/api";
import { formatDateTime, shortId } from "../src/lib/format";
import type { McpClient, SessionUser } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

export function mountSettings(target: HTMLElement, searchInput: HTMLInputElement, currentUser: SessionUser): () => void {
  let clients: McpClient[] = [];
  let query = "";
  let loading = true;
  let errorMessage = "";
  let active = true;
  const refreshButton = button("Refresh", "quiet-button");
  const content = element("div", { className: "settings-sections" });

  function render(): void {
    if (loading) { replaceChildren(content, element("div", { className: "loading-state" }, "Loading settings")); return; }
    const normalized = query.trim().toLowerCase();
    const visible = clients.filter((client) => !normalized || [client.agent_id, client.client_label, client.principal_id, client.id].join(" ").toLowerCase().includes(normalized));
    replaceChildren(content,
      errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null,
      element("section", { className: "settings-block" }, element("p", { className: "eyebrow" }, "Session"), element("h2", {}, currentUser.username), element("dl", { className: "settings-facts" }, element("div", {}, element("dt", {}, "User ID"), element("dd", {}, currentUser.id)), element("div", {}, element("dt", {}, "Authentication"), element("dd", {}, "Active browser session")))),
      element("section", { className: "settings-block settings-block--wide" }, element("div", { className: "settings-title" }, element("div", {}, element("p", { className: "eyebrow" }, "Agent access"), element("h2", {}, "MCP clients")), element("span", { className: "settings-count" }, `${clients.filter((client) => !client.revoked_at).length} active`)), element("div", { className: "client-list" }, ...(visible.length ? visible.map((client) => element("div", { className: "client-row" }, element("span", { className: `state-dot state-dot--${client.revoked_at ? "critical" : "healthy"}` }), element("span", { className: "record-copy" }, element("strong", {}, client.client_label || client.agent_id), element("small", {}, client.principal_id || client.agent_id)), element("span", { className: "record-meta" }, client.last_seen_at ? formatDateTime(client.last_seen_at) : "Never seen"), element("span", { className: "client-token" }, client.token_hint || shortId(client.id)))) : [element("p", { className: "empty-state" }, "No clients match this search.")]))),
      element("section", { className: "settings-block settings-block--wide" }, element("p", { className: "eyebrow" }, "Safety boundary"), element("h2", {}, "Core enforced"), element("p", { className: "settings-copy" }, "Infrastructure actions remain subject to backend validation, CSRF protection, centralized redaction, audit and per-invocation approval.")),
    );
  }
  async function load(): Promise<void> {
    loading = true; errorMessage = ""; refreshButton.disabled = true; render();
    const results = await Promise.allSettled([fetchSession(), fetchMcpClients()]);
    if (!active) return;
    clients = results[1].status === "fulfilled" ? results[1].value : [];
    errorMessage = results.some((result) => result.status === "rejected") ? "Some settings could not be loaded." : "";
    loading = false; refreshButton.disabled = false; render();
  }
  const handleSearch = () => { query = searchInput.value; render(); };
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "settings-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Console"), element("h1", { id: "settings-heading" }, "Settings"), element("p", { className: "inbox-intro" }, "Identity, agent access, and safety boundaries.")), refreshButton), content));
  void load();
  return () => { active = false; searchInput.removeEventListener("input", handleSearch); };
}
