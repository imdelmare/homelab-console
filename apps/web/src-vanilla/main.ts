import { fetchApprovals, fetchIncidents, fetchProviders, fetchSession, fetchTasks, logout, setCsrfToken, setUnauthorizedHandler } from "../src/lib/api";
import type { AuthCompleteResponse, SessionUser } from "../src/lib/types";
import { mountActivity } from "./audit";
import { mountApprovals } from "./approvals";
import { mountIncidents } from "./incidents";
import { mountInbox } from "./inbox";
import { button, element, replaceChildren } from "./dom";
import { mountLogin } from "./login";
import { mountMcp } from "./mcp";
import { mountDelivery, mountMetrics } from "./metrics";
import { mountSettings } from "./settings";
import { mountSystems } from "./systems";
import { mountTasks } from "./tasks";
import { mountTools } from "./tools";
import { mountTopology } from "./topology";
import { mountWatchers } from "./watchers";
import "./styles.css";

type PageId = "inbox" | "systems" | "incidents" | "tools" | "tasks" | "watchers" | "metrics" | "delivery" | "topology" | "mcp" | "approvals" | "activity" | "settings";

const rootElement = document.querySelector<HTMLElement>("#app-root");
if (!rootElement) throw new Error("Missing #app-root mount point");
const root: HTMLElement = rootElement;

let cleanupView: (() => void) | null = null;

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(message)), timeoutMs);
    promise.then(
      (value) => { window.clearTimeout(timer); resolve(value); },
      (error) => { window.clearTimeout(timer); reject(error); },
    );
  });
}

function switchView(render: () => (() => void) | void): void {
  cleanupView?.();
  cleanupView = render() ?? null;
}

function showLogin(expired = false): void {
  setCsrfToken(null);
  switchView(() => {
    const cleanup = mountLogin(root, showConsole);
    if (expired) {
      const alert = element("p", { className: "session-alert", role: "alert" }, "Your session expired. Please sign in again.");
      root.prepend(alert);
    }
    return cleanup;
  });
}

function showConsole(result: AuthCompleteResponse | { user: SessionUser; csrf_token: string }): void {
  setCsrfToken(result.csrf_token);
  switchView(() => {
    let cleanupPage: (() => void) | null = null;
    const panelTarget = element("main", { className: "workspace-panel" });
    const logoutButton = button("Sign out", "quiet-button signout-button");
    const searchInput = element("input", {
      className: "global-search",
      type: "search",
      placeholder: "Search this inbox",
      "aria-label": "Search operational inbox",
    });
    const navItems: Array<[string, PageId, string]> = [
      ["Inbox", "inbox", "Observe"],
      ["Systems", "systems", "Observe"],
      ["Topology", "topology", "Observe"],
      ["Metrics", "metrics", "Observe"],
      ["AI Delivery", "delivery", "Observe"],
      ["Incidents", "incidents", "Operate"],
      ["Tools", "tools", "Operate"],
      ["Tasks", "tasks", "Operate"],
      ["Watchers", "watchers", "Operate"],
      ["Approvals", "approvals", "Govern"],
      ["MCP Clients", "mcp", "Govern"],
      ["Activity", "activity", "Govern"],
      ["Settings", "settings", "Govern"],
    ];
    const nav = element("nav", { className: "primary-nav", "aria-label": "Primary navigation" });
    const navButtons = new Map<PageId, HTMLButtonElement>();
    const navBadges = new Map<PageId, HTMLElement>();
    let badgeLoadInFlight = false;
    let currentGroup = "";
    navItems.forEach(([label, pageId, group]) => {
      if (group !== currentGroup) {
        currentGroup = group;
        nav.append(element("p", { className: "nav-group" }, group));
      }
      const item = button(label, "nav-item");
      item.setAttribute("aria-label", label);
      if (["inbox", "systems", "incidents", "tasks", "approvals"].includes(pageId)) {
        const badge = element("span", { className: "nav-badge", "aria-label": `${label} attention count`, hidden: true });
        item.append(badge);
        navBadges.set(pageId, badge);
      }
      item.addEventListener("click", () => {
        if (window.location.hash === `#${pageId}`) activatePage(pageId);
        else window.location.hash = pageId;
      });
      navButtons.set(pageId, item);
      nav.append(item);
    });

    const pages: Record<PageId, { placeholder: string; mount: () => () => void }> = {
      inbox: { placeholder: "Search this inbox", mount: () => mountInbox(panelTarget, searchInput) },
      systems: { placeholder: "Search systems", mount: () => mountSystems(panelTarget, searchInput) },
      incidents: { placeholder: "Search incidents", mount: () => mountIncidents(panelTarget, searchInput) },
      tools: { placeholder: "Search tools", mount: () => mountTools(panelTarget, searchInput) },
      tasks: { placeholder: "Search tasks", mount: () => mountTasks(panelTarget, searchInput, result.user.username) },
      watchers: { placeholder: "Search watchers and incidents", mount: () => mountWatchers(panelTarget, searchInput) },
      metrics: { placeholder: "Search router reviews", mount: () => mountMetrics(panelTarget, searchInput) },
      delivery: { placeholder: "AI delivery trace", mount: () => mountDelivery(panelTarget) },
      topology: { placeholder: "Search topology", mount: () => mountTopology(panelTarget, searchInput) },
      mcp: { placeholder: "Search MCP clients", mount: () => mountMcp(panelTarget, searchInput) },
      approvals: { placeholder: "Search approvals", mount: () => mountApprovals(panelTarget, searchInput) },
      activity: { placeholder: "Search activity", mount: () => mountActivity(panelTarget, searchInput) },
      settings: { placeholder: "Search clients", mount: () => mountSettings(panelTarget, searchInput, result.user) },
    };

    async function updateNavBadges(): Promise<void> {
      if (badgeLoadInFlight || document.hidden) return;
      badgeLoadInFlight = true;
      const results = await Promise.allSettled([
        fetchApprovals({ status: "pending", limit: 100 }),
        fetchIncidents({ status: "open", limit: 100 }),
        fetchTasks({ status: "open", limit: 100 }),
        fetchProviders(),
      ]);
      badgeLoadInFlight = false;
      const approvalCount = results[0].status === "fulfilled" ? results[0].value.length : 0;
      const incidentCount = results[1].status === "fulfilled" ? results[1].value.length : 0;
      const taskCount = results[2].status === "fulfilled" ? results[2].value.filter((task) => task.status === "blocked" || task.status === "waiting_operator" || !task.assigned_agent).length : 0;
      const systemCount = results[3].status === "fulfilled" ? results[3].value.filter((provider) => provider.status !== "healthy").length : 0;
      const counts: Partial<Record<PageId, number>> = { inbox: approvalCount + incidentCount + taskCount, systems: systemCount, incidents: incidentCount, tasks: taskCount, approvals: approvalCount };
      navBadges.forEach((badge, pageId) => {
        const count = counts[pageId] ?? 0;
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.hidden = count === 0;
      });
    }

    function activatePage(pageId: PageId): void {
      cleanupPage?.();
      searchInput.value = "";
      searchInput.placeholder = pages[pageId].placeholder;
      searchInput.setAttribute("aria-label", pages[pageId].placeholder);
      navButtons.forEach((control, id) => control.classList.toggle("nav-item--active", id === pageId));
      cleanupPage = pages[pageId].mount();
      document.title = `${navItems.find(([, id]) => id === pageId)?.[0] ?? "Homelab"} — Homelab Console`;
    }

    function pageFromHash(): PageId {
      const candidate = window.location.hash.slice(1).split("/", 1)[0];
      return candidate in pages ? candidate as PageId : "inbox";
    }

    const handleHashChange = () => activatePage(pageFromHash());
    logoutButton.addEventListener("click", async () => {
      logoutButton.disabled = true;
      try {
        await logout();
      } finally {
        showLogin();
      }
    });

    replaceChildren(
      root,
      element(
        "div",
        { className: "console-shell" },
        element("aside", { className: "sidebar" },
          element("a", { className: "wordmark", href: "#inbox", "aria-label": "Homelab Console inbox" }, element("span", {}, "Homelab", element("small", {}, "private operations"))),
          nav,
          element("div", { className: "sidebar-footer" },
            element("span", { className: "user-label" }, "Signed in as", element("strong", {}, result.user.username)),
            logoutButton,
          ),
        ),
        element("div", { className: "console-content" },
          element("header", { className: "utility-bar" },
            element("div", { className: "live-status" }, element("span", { className: "status-light" }), "Control plane online"),
            searchInput,
          ),
          panelTarget,
        ),
      ),
    );
    window.addEventListener("hashchange", handleHashChange);
    activatePage(pageFromHash());
    void updateNavBadges();
    const badgeTimer = window.setInterval(() => void updateNavBadges(), 20_000);
    const handleVisibility = () => { if (!document.hidden) void updateNavBadges(); };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      cleanupPage?.();
      window.clearInterval(badgeTimer);
      document.removeEventListener("visibilitychange", handleVisibility);
      window.removeEventListener("hashchange", handleHashChange);
    };
  });
}

setUnauthorizedHandler(() => showLogin(true));

replaceChildren(root, element("div", { className: "boot-screen" }, element("p", {}, "Opening private operations…")));

withTimeout(fetchSession(), 8_000, "Session check timed out")
  .then((session) => {
    if (session.authenticated) showConsole(session);
    else showLogin();
  })
  .catch(() => showLogin());
