import React, { Suspense, lazy, useCallback, useEffect, useMemo, useState } from "react";
import type { ComponentType, SVGProps } from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "styled-components";
import original from "react95/dist/themes/original";
import { Button, Window, WindowContent, WindowHeader } from "react95";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { Bot } from "lucide-react";
import { Computer } from "@react95/icons/Computer";
import { Imgscan10 } from "@react95/icons/Imgscan10";
import { Key } from "@react95/icons/Key";
import { Lock } from "@react95/icons/Lock";
import { LogView } from "@react95/icons/LogView";
import { Network2 } from "@react95/icons/Network2";
import { Settings } from "@react95/icons/Settings";
import { Taskman100 } from "@react95/icons/Taskman100";
import { Tree } from "@react95/icons/Tree";
import { WindowGraph } from "@react95/icons/WindowGraph";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { LoginWindow } from "./components/LoginWindow";
import { LoadingIndicator } from "./components/LoadingIndicator";
import { MobileShell } from "./components/MobileShell";
import { PanelLoadingScreen } from "./components/PanelLoadingScreen";
import { StartMenu } from "./components/StartMenu";
import { Taskbar } from "./components/Taskbar";
import { WindowFrame } from "./components/WindowFrame";
import {
  fetchIncidents,
  fetchMcpClients,
  fetchProviders,
  fetchSession,
  fetchTasks,
  fetchWatcherRuns,
  fetchWatcherStatus,
  logout,
  setCsrfToken,
  setUnauthorizedHandler,
} from "./lib/api";
import { isMcpClientOnline } from "./lib/ui";
import { PanelQueryScope, shouldRetryQuery, usePanelQuery } from "./lib/usePanelQuery";
import type { AppId, AuthCompleteResponse, Provider, SessionUser, WatcherRun, WindowState } from "./lib/types";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "./styles.css";

// React.lazy's public type itself uses `any` here; keeping the concrete Panel
// generic preserves each imported component's actual props at call sites.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function lazyPanel<Panel extends ComponentType<any>>(panelId: string, load: () => Promise<{ default: Panel }>) {
  return lazy(async (): Promise<{ default: Panel }> => {
    try {
      const module = await load();
      window.sessionStorage.removeItem(`guixos.chunk-reload.${panelId}`);
      return module;
    } catch (error) {
      const reloadKey = `guixos.chunk-reload.${panelId}`;
      if (!window.sessionStorage.getItem(reloadKey)) {
        window.sessionStorage.setItem(reloadKey, "1");
        window.location.reload();
        return await new Promise<{ default: Panel }>(() => undefined);
      }
      throw error;
    }
  });
}

const OverviewApp = lazyPanel("overview", () => import("./apps/OverviewApp").then((module) => ({ default: module.OverviewApp })));
const ProvidersApp = lazyPanel("providers", () => import("./apps/ProvidersApp").then((module) => ({ default: module.ProvidersApp })));
const ToolsApp = lazyPanel("tools", () => import("./apps/ToolsApp").then((module) => ({ default: module.ToolsApp })));
const TasksApp = lazyPanel("tasks", () => import("./apps/TasksApp").then((module) => ({ default: module.TasksApp })));
const WatchersApp = lazyPanel("watchers", () => import("./apps/WatchersApp").then((module) => ({ default: module.WatchersApp })));
const LunaMetricsApp = lazyPanel("luna", () => import("./apps/LunaMetricsApp").then((module) => ({ default: module.LunaMetricsApp })));
const AiDeliveryApp = lazyPanel("delivery", () => import("./apps/AiDeliveryApp").then((module) => ({ default: module.AiDeliveryApp })));
const TopologyApp = lazyPanel("topology", () => import("./apps/TopologyApp").then((module) => ({ default: module.TopologyApp })));
const McpClientsApp = lazyPanel("mcp", () => import("./apps/McpClientsApp").then((module) => ({ default: module.McpClientsApp })));
const AuditApp = lazyPanel("audit", () => import("./apps/AuditApp").then((module) => ({ default: module.AuditApp })));
const ApprovalsApp = lazyPanel("approvals", () => import("./apps/ApprovalsApp").then((module) => ({ default: module.ApprovalsApp })));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Console data ages fast; keep every open window in sync in the
      // background without each panel rolling its own polling loop.
      staleTime: 15_000,
      refetchInterval: 30_000,
      refetchOnWindowFocus: true,
      retry: shouldRetryQuery,
    },
  },
});

export type DesktopIcon = ComponentType<SVGProps<SVGSVGElement>>;

const appRegistry: Array<{ id: AppId; title: string; icon: DesktopIcon; tone: string }> = [
  { id: "overview", title: "Overview", icon: Computer, tone: "overview" },
  { id: "providers", title: "Providers", icon: Network2, tone: "network" },
  { id: "tools", title: "Tool routing", icon: Settings, tone: "tools" },
  { id: "tasks", title: "Task", icon: Taskman100, tone: "tasks" },
  { id: "watchers", title: "Watcher", icon: Imgscan10, tone: "watchers" },
  { id: "luna", title: "Metrics", icon: WindowGraph, tone: "luna" },
  { id: "delivery", title: "AI Delivery", icon: Bot, tone: "delivery" },
  { id: "topology", title: "Topology", icon: Tree, tone: "topology" },
  { id: "mcp", title: "MCP Clients", icon: Key, tone: "mcp" },
  { id: "approvals", title: "Approvals", icon: Lock, tone: "approvals" },
  { id: "audit", title: "Audit log", icon: LogView, tone: "audit" },
];

const initialWindows: Record<AppId, WindowState> = {
  overview: {
    id: "overview",
    title: "Console overview",
    x: 132,
    y: 42,
    width: 760,
    height: 500,
    minWidth: 520,
    minHeight: 360,
    zIndex: 6,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  providers: {
    id: "providers",
    title: "Providers",
    x: 170,
    y: 92,
    width: 820,
    height: 540,
    minWidth: 380,
    minHeight: 300,
    zIndex: 2,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  tools: {
    id: "tools",
    title: "Tool routing",
    x: 620,
    y: 42,
    width: 610,
    height: 520,
    minWidth: 440,
    minHeight: 320,
    zIndex: 4,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  tasks: {
    id: "tasks",
    title: "Task",
    x: 180,
    y: 430,
    width: 720,
    height: 430,
    minWidth: 500,
    minHeight: 320,
    zIndex: 3,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  watchers: {
    id: "watchers",
    title: "Watcher",
    x: 920,
    y: 118,
    width: 620,
    height: 430,
    minWidth: 480,
    minHeight: 320,
    zIndex: 1,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  luna: {
    id: "luna",
    title: "Metrics",
    x: 210,
    y: 86,
    width: 900,
    height: 570,
    minWidth: 560,
    minHeight: 380,
    zIndex: 2,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  delivery: {
    id: "delivery",
    title: "AI Delivery",
    x: 238,
    y: 72,
    width: 880,
    height: 590,
    minWidth: 560,
    minHeight: 400,
    zIndex: 3,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  topology: {
    id: "topology",
    title: "Topology",
    x: 96,
    y: 76,
    width: 860,
    height: 520,
    minWidth: 560,
    minHeight: 360,
    zIndex: 2,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  mcp: {
    id: "mcp",
    title: "MCP Clients",
    x: 260,
    y: 104,
    width: 620,
    height: 560,
    minWidth: 460,
    minHeight: 420,
    zIndex: 1,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  audit: {
    id: "audit",
    title: "Audit log",
    x: 280,
    y: 148,
    width: 780,
    height: 420,
    minWidth: 520,
    minHeight: 300,
    zIndex: 0,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
  approvals: {
    id: "approvals",
    title: "Approvals",
    x: 260,
    y: 120,
    width: 680,
    height: 480,
    minWidth: 420,
    minHeight: 300,
    zIndex: 1,
    isOpen: false,
    isMinimized: false,
    isMaximized: false,
  },
};

const WINDOW_LAYOUT_STORAGE_KEY = "guixos.window-layout.v1";

function loadStoredWindows(): Record<AppId, WindowState> {
  try {
    const raw = window.localStorage.getItem(WINDOW_LAYOUT_STORAGE_KEY);
    if (!raw) return initialWindows;
    const parsed = JSON.parse(raw) as Partial<Record<AppId, Partial<WindowState>>>;
    const merged = { ...initialWindows };
    for (const id of Object.keys(initialWindows) as AppId[]) {
      const saved = parsed[id];
      if (!saved || typeof saved !== "object") continue;
      // Only restore layout fields; titles/min sizes always come from code so
      // renames and constraint fixes apply to returning sessions too.
      merged[id] = {
        ...initialWindows[id],
        x: typeof saved.x === "number" ? saved.x : initialWindows[id].x,
        y: typeof saved.y === "number" ? saved.y : initialWindows[id].y,
        width: typeof saved.width === "number" ? saved.width : initialWindows[id].width,
        height: typeof saved.height === "number" ? saved.height : initialWindows[id].height,
        zIndex: typeof saved.zIndex === "number" ? saved.zIndex : initialWindows[id].zIndex,
        isOpen: Boolean(saved.isOpen),
        isMinimized: Boolean(saved.isMinimized),
        isMaximized: Boolean(saved.isMaximized),
        restoreBounds: saved.restoreBounds,
      };
    }
    return merged;
  } catch {
    return initialWindows;
  }
}

type OperatorStatus = {
  openTasks: number | null;
  openIncidents: number | null;
  providerIssues: number | null;
  mcpOnline: number | null;
  mcpActive: number | null;
  watcherEnabled: boolean | null;
  lastWatcherRun: WatcherRun | null;
};

function issueProviders(providers: Provider[]): number {
  return providers.filter((provider) => provider.status !== "healthy").length;
}

// Taskbar/desktop badge data. Query keys deliberately match the panels', so
// an open Overview or Tasks window and the taskbar share one request.
function useOperatorStatus(enabled: boolean): OperatorStatus {
  const options = { enabled, refetchInterval: 60_000 as const };
  const tasks = usePanelQuery(["tasks", "open"], () => fetchTasks({ status: "open", limit: 100 }), options);
  const incidents = usePanelQuery(["incidents", "open"], () => fetchIncidents({ status: "open", limit: 100 }), options);
  const providers = usePanelQuery(["providers"], fetchProviders, options);
  const clients = usePanelQuery(["mcp-clients"], fetchMcpClients, options);
  const watcherStatus = usePanelQuery(["watcher-status"], fetchWatcherStatus, options);
  const watcherRuns = usePanelQuery(["watcher-runs", 1], () => fetchWatcherRuns(1), options);

  const activeClients = (clients.data ?? []).filter((client) => !client.revoked_at);
  return {
    openTasks: tasks.data ? tasks.data.length : null,
    openIncidents: incidents.data ? incidents.data.length : null,
    providerIssues: providers.data ? issueProviders(providers.data) : null,
    mcpOnline: clients.data ? activeClients.filter((client) => isMcpClientOnline(client)).length : null,
    mcpActive: clients.data ? activeClients.length : null,
    watcherEnabled: watcherStatus.data ? watcherStatus.data.enabled : null,
    lastWatcherRun: watcherRuns.data?.[0] ?? null,
  };
}

function maximizedBounds() {
  return {
    x: 8,
    y: 8,
    width: Math.max(280, window.innerWidth - 16),
    height: Math.max(200, window.innerHeight - 48),
  };
}

type AuthState = "checking" | "authenticated" | "expired" | "anonymous";

function App() {
  const activeQueryClient = useQueryClient();
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [sessionUser, setSessionUser] = useState<SessionUser | null>(null);
  const [windows, setWindows] = useState<Record<AppId, WindowState>>(loadStoredWindows);
  const [activeWindowId, setActiveWindowId] = useState<AppId>(() => window.matchMedia("(max-width: 767px)").matches ? "overview" : "tools");
  const [isStartOpen, setIsStartOpen] = useState(false);
  const [requestedTaskId, setRequestedTaskId] = useState<string | null>(null);
  const [showShortcuts, setShowShortcuts] = useState(false);
  const [isCompactViewport, setIsCompactViewport] = useState(() => window.matchMedia("(max-width: 767px)").matches);
  const [isMobileDrawerOpen, setIsMobileDrawerOpen] = useState(false);

  const windowList = useMemo(() => Object.values(windows), [windows]);
  const isAuthenticated = authState === "authenticated";
  const operatorStatus = useOperatorStatus(isAuthenticated);
  const openTasksCount = operatorStatus.openTasks;

  const patchWindow = useCallback((id: AppId, patch: Partial<WindowState>) => {
    setWindows((current) => ({
      ...current,
      [id]: {
        ...current[id],
        ...patch,
      },
    }));
  }, []);

  const focusWindow = useCallback((id: AppId) => {
    setWindows((current) => {
      const target = current[id];
      const restoreBounds = target.restoreBounds ?? {
        x: target.x,
        y: target.y,
        width: target.width,
        height: target.height,
      };
      const nextZIndex = Math.max(10, ...Object.values(current).map((appWindow) => appWindow.zIndex)) + 1;
      return {
        ...current,
        [id]: {
          ...target,
          ...(target.isOpen ? {} : maximizedBounds()),
          zIndex: nextZIndex,
          isMinimized: false,
          isOpen: true,
          isMaximized: target.isOpen ? target.isMaximized : true,
          restoreBounds: target.isOpen ? target.restoreBounds : restoreBounds,
        },
      };
    });
    setActiveWindowId(id);
  }, []);

  const openApp = useCallback((id: AppId) => {
    setIsStartOpen(false);
    focusWindow(id);
  }, [focusWindow]);

  const closeWindow = useCallback((id: AppId) => {
    patchWindow(id, { isOpen: false, isMinimized: false, isMaximized: false });
  }, [patchWindow]);

  useEffect(() => {
    try {
      window.localStorage.setItem(WINDOW_LAYOUT_STORAGE_KEY, JSON.stringify(windows));
    } catch {
      // Storage may be unavailable (private mode, quota); layout persistence
      // is best-effort.
    }
  }, [windows]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 767px)");
    const handleChange = () => setIsCompactViewport(media.matches);
    handleChange();
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, []);

  useEffect(() => {
    function handleViewportResize() {
      const bounds = maximizedBounds();
      setWindows((current) =>
        Object.fromEntries(
          Object.entries(current).map(([id, appWindow]) => [
            id,
            appWindow.isMaximized ? { ...appWindow, ...bounds } : appWindow,
          ]),
        ) as Record<AppId, WindowState>,
      );
    }

    window.addEventListener("resize", handleViewportResize);
    return () => window.removeEventListener("resize", handleViewportResize);
  }, []);

  useEffect(() => {
    const shortcutApps: Partial<Record<string, AppId>> = {
      a: "audit",
      g: "topology",
      m: "mcp",
      l: "luna",
      o: "overview",
      p: "providers",
      r: "tools",
      t: "tasks",
      v: "approvals",
      w: "watchers",
    };

    function handleKeyboardShortcut(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      const isEditing = Boolean(
        target?.isContentEditable || target?.closest("input, textarea, select, [contenteditable='true']"),
      );

      if (event.key === "F1" || (!isEditing && event.key === "?")) {
        event.preventDefault();
        setShowShortcuts(true);
        return;
      }

      if (event.key === "Escape") {
        if (isEditing) {
          target?.blur();
          return;
        }
        if (showShortcuts) {
          setShowShortcuts(false);
          return;
        }
        if (document.querySelector(".modal-overlay")) return;
        if (isStartOpen) {
          setIsStartOpen(false);
          return;
        }
        const active = windows[activeWindowId];
        if (active?.isOpen) closeWindow(activeWindowId);
        return;
      }

      if (isEditing || event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
      const appId = shortcutApps[event.key.toLowerCase()];
      if (!appId) return;
      event.preventDefault();
      const targetWindow = windows[appId];
      if (activeWindowId === appId && targetWindow.isOpen && !targetWindow.isMinimized) {
        closeWindow(appId);
        return;
      }
      openApp(appId);
    }

    window.addEventListener("keydown", handleKeyboardShortcut);
    return () => window.removeEventListener("keydown", handleKeyboardShortcut);
  }, [activeWindowId, closeWindow, isStartOpen, openApp, showShortcuts, windows]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setCsrfToken(null);
      void activeQueryClient.cancelQueries();
      // A session that drops mid-work becomes "expired": the desktop stays
      // mounted (window layout, form drafts, panel state survive) behind a
      // re-login overlay. Only a session that was never established falls
      // back to the plain login screen.
      setAuthState((current) =>
        current === "authenticated" || current === "expired" ? "expired" : "anonymous",
      );
    });

    fetchSession()
      .then((session) => {
        if (session.authenticated) {
          setCsrfToken(session.csrf_token);
          setSessionUser(session.user);
          setAuthState("authenticated");
          return;
        }
        setCsrfToken(null);
        setSessionUser(null);
        setAuthState("anonymous");
      })
      .catch(() => {
        setCsrfToken(null);
        setSessionUser(null);
        setAuthState("anonymous");
      });

    return () => setUnauthorizedHandler(null);
  }, [activeQueryClient]);

  useEffect(() => {
    function handleOpenTask(event: Event) {
      const taskId = (event as CustomEvent<{ taskId?: string }>).detail?.taskId;
      if (!taskId) return;
      setRequestedTaskId(taskId);
      focusWindow("tasks");
    }

    window.addEventListener("homelab:open-task", handleOpenTask);
    return () => window.removeEventListener("homelab:open-task", handleOpenTask);
  }, [focusWindow]);

  useEffect(() => {
    function handleOpenTopology() {
      focusWindow("topology");
    }

    window.addEventListener("homelab:open-topology", handleOpenTopology);
    return () => window.removeEventListener("homelab:open-topology", handleOpenTopology);
  }, [focusWindow]);

  function minimizeWindow(id: AppId) {
    patchWindow(id, { isMinimized: true });
  }

  function toggleMaximizeWindow(id: AppId) {
    const target = windows[id];
    if (target.isMaximized && target.restoreBounds) {
      patchWindow(id, {
        ...target.restoreBounds,
        isMaximized: false,
        restoreBounds: undefined,
      });
      focusWindow(id);
      return;
    }

    patchWindow(id, {
      ...maximizedBounds(),
      isMaximized: true,
      isMinimized: false,
      restoreBounds: {
        x: target.x,
        y: target.y,
        width: target.width,
        height: target.height,
      },
    });
    focusWindow(id);
  }

  function handleTaskbarWindowClick(id: AppId) {
    const target = windows[id];
    if (target.id === activeWindowId && !target.isMinimized) {
      minimizeWindow(id);
      return;
    }
    focusWindow(id);
  }

  function handleAuthenticated(result: AuthCompleteResponse) {
    setCsrfToken(result.csrf_token);
    setSessionUser(result.user);
    setAuthState("authenticated");
    // Everything cached before the session dropped may be stale or partial.
    void activeQueryClient.invalidateQueries();
  }

  async function handleLogout() {
    try {
      await logout();
    } finally {
      setCsrfToken(null);
      setSessionUser(null);
      setAuthState("anonymous");
      setIsStartOpen(false);
      setIsMobileDrawerOpen(false);
      activeQueryClient.clear();
    }
  }

  function renderApp(id: AppId) {
    switch (id) {
      case "overview":
        return <OverviewApp onOpenApp={openApp} />;
      case "providers":
        return <ProvidersApp />;
      case "tools":
        return <ToolsApp />;
      case "tasks":
        return <TasksApp requestedTaskId={requestedTaskId} username={sessionUser?.username ?? "user"} />;
      case "watchers":
        return <WatchersApp />;
      case "luna":
        return <LunaMetricsApp />;
      case "delivery":
        return <AiDeliveryApp />;
      case "topology":
        return <TopologyApp />;
      case "mcp":
        return <McpClientsApp />;
      case "audit":
        return <AuditApp />;
      case "approvals":
        return <ApprovalsApp />;
    }
  }

  if (authState === "checking") {
    return (
      <main className="login-screen">
        <Window className="window login-window react95-window">
          <WindowHeader active className="title-bar"><span className="window-title">Starting</span></WindowHeader>
          <WindowContent className="window-body login-window-body">
            <LoadingIndicator label="Checking session…" />
          </WindowContent>
        </Window>
      </main>
    );
  }

  if (authState === "anonymous") {
    return <LoginWindow onAuthenticated={handleAuthenticated} />;
  }

  if (isCompactViewport) {
    return (
      <PanelQueryScope enabled={isAuthenticated}>
        <MobileShell
          apps={appRegistry}
          activeAppId={activeWindowId}
          username={sessionUser?.username ?? "user"}
          isDrawerOpen={isMobileDrawerOpen}
          badges={{
            tasks: operatorStatus.openTasks,
            watchers: operatorStatus.openIncidents,
            providers: operatorStatus.providerIssues,
            mcp: operatorStatus.mcpOnline,
          }}
          onNavigate={openApp}
          onDrawerChange={setIsMobileDrawerOpen}
          onLogout={handleLogout}
        >
          <ErrorBoundary label={initialWindows[activeWindowId].title}>
            <Suspense fallback={<PanelLoadingScreen label={`Loading ${initialWindows[activeWindowId].title}…`} />}>{renderApp(activeWindowId)}</Suspense>
          </ErrorBoundary>
        </MobileShell>
        {authState === "expired" && (
          <div className="session-expired-overlay">
            <LoginWindow onAuthenticated={handleAuthenticated} />
          </div>
        )}
      </PanelQueryScope>
    );
  }

  return (
    <PanelQueryScope enabled={isAuthenticated}>
      <main className="desktop" onMouseDown={() => setIsStartOpen(false)}>
      {windowList.map((window) => (
        <WindowFrame
          key={window.id}
          window={window}
          compact={isCompactViewport}
          isActive={window.id === activeWindowId}
          onFocus={() => focusWindow(window.id)}
          onClose={() => closeWindow(window.id)}
          onMinimize={() => minimizeWindow(window.id)}
          onMaximize={() => toggleMaximizeWindow(window.id)}
          onChange={(patch) => patchWindow(window.id, { ...patch, isMaximized: false })}
        >
          <ErrorBoundary label={window.title}>
            <Suspense fallback={<PanelLoadingScreen label={`Loading ${window.title}…`} />}>{renderApp(window.id)}</Suspense>
          </ErrorBoundary>
        </WindowFrame>
      ))}

      <div className="desktop-icons">
        {appRegistry.map(({ id, title, icon: Icon, tone }) => {
          const badge =
            id === "tasks"
              ? operatorStatus.openTasks
              : id === "watchers"
                ? operatorStatus.openIncidents
                : id === "providers"
                  ? operatorStatus.providerIssues
                  : id === "mcp"
                    ? operatorStatus.mcpOnline
                    : null;
          return (
            <button className="desktop-icon" key={id} type="button" onClick={() => openApp(id)}>
              <span className={`icon-glyph icon-glyph-${tone}`}>
                <Icon className="desktop-icon-image" width={32} height={32} aria-hidden="true" />
                {badge !== null && badge > 0 && <mark className="desktop-badge">{badge}</mark>}
              </span>
              <span>{title}</span>
            </button>
          );
        })}
      </div>

      <StartMenu
        isOpen={isStartOpen}
        apps={appRegistry}
        username={sessionUser?.username ?? "user"}
        onOpenApp={openApp}
        onLogout={handleLogout}
      />
      <Taskbar
        windows={windowList}
        apps={appRegistry}
        activeWindowId={activeWindowId}
        isStartOpen={isStartOpen}
        openTasksCount={openTasksCount}
        operatorStatus={operatorStatus}
        onStartClick={() => setIsStartOpen((current) => !current)}
        onWindowClick={handleTaskbarWindowClick}
        onShowShortcuts={() => setShowShortcuts(true)}
      />
      {authState === "expired" && (
        <div className="session-expired-overlay">
          <LoginWindow onAuthenticated={handleAuthenticated} />
        </div>
      )}
      {showShortcuts && (
        <div className="modal-overlay shortcut-overlay" role="presentation" onMouseDown={() => setShowShortcuts(false)}>
          <Window className="window shortcut-dialog react95-window" role="dialog" aria-modal="true" aria-labelledby="shortcut-title" onMouseDown={(event) => event.stopPropagation()}>
            <WindowHeader active className="title-bar">
              <span className="window-title" id="shortcut-title">Keyboard shortcuts</span>
              <div className="title-bar-controls" aria-label="Window controls">
                <Button type="button" aria-label="Close window" title="Close" onClick={() => setShowShortcuts(false)}>×</Button>
              </div>
            </WindowHeader>
            <WindowContent className="window-body shortcut-dialog-body">
              <p>Shortcuts work when you are not typing in a field. Press the active app key again to close it.</p>
              <dl className="shortcut-grid">
                <div><dt>O</dt><dd>Overview</dd></div>
                <div><dt>P</dt><dd>Providers</dd></div>
                <div><dt>R</dt><dd>Tool routing</dd></div>
                <div><dt>T</dt><dd>Tasks</dd></div>
                <div><dt>W</dt><dd>Watchers</dd></div>
                <div><dt>L</dt><dd>Metrics</dd></div>
                <div><dt>G</dt><dd>Topology Graph</dd></div>
                <div><dt>M</dt><dd>MCP Clients</dd></div>
                <div><dt>A</dt><dd>Audit Log</dd></div>
                <div><dt>Esc</dt><dd>Close active window</dd></div>
                <div><dt>F1 / ?</dt><dd>Show this guide</dd></div>
              </dl>
              <div className="dialog-actions">
                <Button type="button" onClick={() => setShowShortcuts(false)}>Close</Button>
              </div>
            </WindowContent>
          </Window>
        </div>
      )}
      </main>
    </PanelQueryScope>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider theme={original}>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ThemeProvider>
  </React.StrictMode>,
);
