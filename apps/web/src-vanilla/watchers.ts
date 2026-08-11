import { fetchWatcherRuns, fetchWatcherStatus, runWatchers, updateWatcherAutomation, updateWatcherConfig } from "../src/lib/api";
import { formatDateTime } from "../src/lib/format";
import type { WatcherRun, WatcherStatus } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

export function mountWatchers(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let runs: WatcherRun[] = [];
  let status: WatcherStatus | null = null;
  let query = "";
  let loading = true;
  let busy = "";
  let errorMessage = "";
  let active = true;
  const refreshButton = button("Refresh", "quiet-button");
  const content = element("div", { className: "watcher-sections" });

  async function mutate(name: string, action: () => Promise<unknown>): Promise<void> {
    busy = name; errorMessage = ""; render();
    try { await action(); await load(false); }
    catch (error) { errorMessage = error instanceof Error ? error.message : "Watcher action failed."; }
    finally { if (active) { busy = ""; render(); } }
  }

  function configRow(watcher: NonNullable<WatcherStatus>["watchers"][number]): HTMLElement {
    const enabled = element("input", { type: "checkbox", checked: watcher.enabled, "aria-label": `Enable ${watcher.label}` });
    const interval = element("input", { className: "control-input compact-input", type: "number", min: 30, value: watcher.interval_seconds, "aria-label": `Interval for ${watcher.label}` });
    const severity = element("select", { className: "control-input", "aria-label": `Severity for ${watcher.label}` }, element("option", { value: "warning" }, "warning"), element("option", { value: "critical" }, "critical"));
    severity.value = watcher.min_severity;
    const mode = element("select", { className: "control-input", "aria-label": `Investigation mode for ${watcher.label}` }, element("option", { value: "manual" }, "manual"), element("option", { value: "auto_investigate" }, "auto investigate"));
    mode.value = watcher.investigation_mode;
    const save = button("Save", "quiet-button");
    const run = button("Run now", "quiet-button");
    save.disabled = run.disabled = Boolean(busy);
    save.addEventListener("click", () => void mutate(`save-${watcher.id}`, () => updateWatcherConfig(watcher.id, { enabled: enabled.checked, interval_seconds: Number(interval.value), min_severity: severity.value as "warning" | "critical", investigation_mode: mode.value as "manual" | "auto_investigate" })));
    run.addEventListener("click", () => void mutate(`run-${watcher.id}`, () => runWatchers([watcher.id])));
    return element("div", { className: "watcher-config-row" }, enabled, element("span", { className: "record-copy" }, element("strong", {}, watcher.label), element("small", {}, `${watcher.id} · next ${formatDateTime(watcher.next_run_at)}`)), interval, severity, mode, save, run);
  }

  function render(): void {
    if (loading) { replaceChildren(content, element("div", { className: "loading-state" }, "Loading watcher operations")); return; }
    if (!status) { replaceChildren(content, element("p", { className: "error-banner", role: "alert" }, errorMessage || "Watcher status unavailable.")); return; }
    const normalized = query.trim().toLowerCase();
    const toggle = button(status.enabled ? "Disable automation" : "Enable automation", "quiet-button");
    const runAll = button("Run scheduled watchers", "primary-action");
    toggle.disabled = runAll.disabled = Boolean(busy);
    toggle.addEventListener("click", () => void mutate("automation", () => updateWatcherAutomation(!status!.enabled)));
    runAll.addEventListener("click", () => void mutate("run-all", () => runWatchers(status!.scheduled_watcher_ids)));
    replaceChildren(content,
      errorMessage ? element("p", { className: "error-banner", role: "alert" }, errorMessage) : null,
      element("section", { className: "watcher-control-strip" }, element("div", {}, element("span", { className: `state-dot state-dot--${status.enabled ? "healthy" : "neutral"}` }), element("strong", {}, status.enabled ? "Automation enabled" : "Automation disabled"), element("small", {}, `${status.scheduled_watcher_ids.length} scheduled · default ${status.interval_seconds}s`)), element("div", {}, toggle, runAll)),
      element("section", { className: "watcher-block" }, element("p", { className: "eyebrow" }, "Watcher configuration"), ...status.watchers.filter((watcher) => !normalized || [watcher.label, watcher.id].join(" ").toLowerCase().includes(normalized)).map(configRow)),
      element("section", { className: "watcher-block" }, element("p", { className: "eyebrow" }, "Recent runs"), element("div", { className: "run-list" }, ...runs.slice(0, 20).map((run) => element("div", { className: "run-row" }, element("span", { className: `state-dot state-dot--${run.status === "completed" || run.status === "ok" ? "healthy" : "critical"}` }), element("strong", {}, run.watcher_id), element("span", {}, run.status), element("span", {}, `${run.created_tasks} tasks · ${run.updated_incidents} updates`), element("time", {}, formatDateTime(run.started_at))))))
    );
  }

  async function load(showLoading = true): Promise<void> {
    if (showLoading) loading = true; refreshButton.disabled = true; render();
    const results = await Promise.allSettled([fetchWatcherRuns(20), fetchWatcherStatus()]);
    if (!active) return;
    if (results[0].status === "fulfilled") runs = results[0].value;
    if (results[1].status === "fulfilled") status = results[1].value;
    errorMessage = results.some((result) => result.status === "rejected") ? "Some watcher data could not be loaded." : "";
    loading = false; refreshButton.disabled = false; render();
  }
  const handleSearch = () => { query = searchInput.value; render(); };
  const timer = window.setInterval(() => { if (!document.hidden && !busy) void load(false); }, 30_000);
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load());
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "watchers-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Automation"), element("h1", { id: "watchers-heading" }, "Watchers"), element("p", { className: "inbox-intro" }, "Detection schedules, thresholds, investigation modes, and recent runs.")), refreshButton), content));
  void load();
  return () => { active = false; window.clearInterval(timer); searchInput.removeEventListener("input", handleSearch); };
}
