import { Activity, KeyRound, ListChecks, Radar } from "lucide-react";
import { Logo } from "@react95/icons/Logo";
import type { ComponentType, SVGProps } from "react";
import { AppBar, Button } from "react95";
import type { AppId, WatcherRun, WindowState } from "../lib/types";

type OperatorStatus = {
  openIncidents: number | null;
  providerIssues: number | null;
  mcpOnline: number | null;
  mcpActive: number | null;
  watcherEnabled: boolean | null;
  lastWatcherRun: WatcherRun | null;
};

type TaskbarProps = {
  windows: WindowState[];
  apps: Array<{ id: AppId; icon: ComponentType<SVGProps<SVGSVGElement>>; tone: string }>;
  activeWindowId: string;
  isStartOpen: boolean;
  openTasksCount: number | null;
  operatorStatus: OperatorStatus;
  onStartClick: () => void;
  onWindowClick: (id: WindowState["id"]) => void;
  onShowShortcuts: () => void;
};

export function Taskbar({
  windows,
  apps,
  activeWindowId,
  isStartOpen,
  openTasksCount,
  operatorStatus,
  onStartClick,
  onWindowClick,
  onShowShortcuts,
}: TaskbarProps) {
  const visibleWindows = windows.filter((window) => window.isOpen);
  const time = new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Rome",
  }).format(new Date());

  return (
    <AppBar position="fixed" className="taskbar react95-taskbar" onMouseDown={(event) => event.stopPropagation()}>
      <Button className={`start-button ${isStartOpen ? "pressed" : ""}`} aria-expanded={isStartOpen} aria-controls="start-menu" onClick={onStartClick}>
        <Logo className="start-button-icon" variant="16x16_4" width={16} height={16} aria-hidden="true" />
        <span>Start</span>
      </Button>
      <div className="taskbar-windows">
        {visibleWindows.map((window) => {
          const app = apps.find((candidate) => candidate.id === window.id);
          const Icon = app?.icon;
          const tone = app?.tone ?? window.id;
          return (
            <Button
              className={[
                "taskbar-item",
                window.id === activeWindowId && !window.isMinimized ? "pressed" : "",
                window.isMinimized ? "taskbar-item-minimized" : "",
              ].join(" ")}
              key={window.id}
              onClick={() => onWindowClick(window.id)}
            >
              {Icon && <span className={`taskbar-item-icon icon-glyph-${tone}`}><Icon width={16} height={16} aria-hidden="true" /></span>}
              <span className="taskbar-item-label">{window.title}</span>
            </Button>
          );
        })}
      </div>
      <div className="tray tray-status tasks-tray" title="Open tasks not yet assigned">
        <ListChecks size={14} strokeWidth={2.4} />
        <span>Open tasks: {openTasksCount ?? "…"}</span>
      </div>
      <div className="tray tray-status" title="Provider issues">
        <Activity size={14} strokeWidth={2.4} />
        <span>Providers: {operatorStatus.providerIssues ?? "…"}</span>
      </div>
      <div className="tray tray-status" title="Watcher automation and open incidents">
        <Radar size={14} strokeWidth={2.4} />
        <span>
          Watchers: {operatorStatus.watcherEnabled === null ? "…" : operatorStatus.watcherEnabled ? "on" : "off"}
          {operatorStatus.openIncidents !== null ? ` / ${operatorStatus.openIncidents}` : ""}
        </span>
      </div>
      <div className="tray tray-status" title="MCP clients online / active">
        <KeyRound size={14} strokeWidth={2.4} />
        <span>
          MCP: {operatorStatus.mcpOnline ?? "…"}/{operatorStatus.mcpActive ?? "…"}
        </span>
      </div>
      <Button className="tray shortcut-button" type="button" title="Keyboard shortcuts (F1 or ?)" onClick={onShowShortcuts}>
        ?
      </Button>
      <div className="tray tray-clock">{time}</div>
    </AppBar>
  );
}
