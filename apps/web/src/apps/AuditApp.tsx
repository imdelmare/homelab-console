import { useMemo, useState } from "react";
import { Button, TextInput as Input } from "react95";
import { fetchAudit } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { usePanelQuery } from "../lib/usePanelQuery";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import { SelectControl } from "../components/SelectControl";
import { StatusBadge } from "../components/StatusBadge";

function outcomeTone(outcome: string): "success" | "warning" | "danger" | "neutral" {
  const value = outcome.toLowerCase();
  if (["ok", "success", "approved", "completed"].some((item) => value.includes(item))) return "success";
  if (["error", "failed", "denied", "rejected"].some((item) => value.includes(item))) return "danger";
  if (["pending", "waiting", "required"].some((item) => value.includes(item))) return "warning";
  return "neutral";
}

function outcomeLabel(outcome: string): string {
  const tone = outcomeTone(outcome);
  if (tone === "success") return "Success";
  if (tone === "warning") return "Warning";
  if (tone === "danger") return "Error";
  return "Information";
}

export function AuditApp() {
  const { data, loadState, isFetching, errorMessage, refresh } = usePanelQuery(["audit", 100], () => fetchAudit(100));
  const entries = useMemo(() => data ?? [], [data]);
  const [query, setQuery] = useState("");
  const [actor, setActor] = useState("");
  const [source, setSource] = useState("");
  const [outcome, setOutcome] = useState("");
  const [tool, setTool] = useState("");
  const [hours, setHours] = useState(0);
  const [page, setPage] = useState(1);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [referenceTime, setReferenceTime] = useState(() => Date.now());
  const pageSize = 20;

  const actors = [...new Set(entries.map((entry) => entry.actor))].sort();
  const sources = [...new Set(entries.map((entry) => entry.source))].sort();
  const outcomes = [...new Set(entries.map((entry) => entry.outcome))].sort();
  const tools = [...new Set(entries.map((entry) => entry.tool_id).filter((value): value is string => Boolean(value)))].sort();
  const normalizedQuery = query.trim().toLowerCase();
  const cutoff = hours ? referenceTime - hours * 60 * 60 * 1000 : 0;
  const filteredEntries = entries.filter((entry) => {
    if (actor && entry.actor !== actor) return false;
    if (source && entry.source !== source) return false;
    if (outcome && entry.outcome !== outcome) return false;
    if (tool && entry.tool_id !== tool) return false;
    if (cutoff && new Date(entry.created_at).getTime() < cutoff) return false;
    if (!normalizedQuery) return true;
    return [entry.action, entry.actor, entry.source, entry.outcome, entry.tool_id, entry.task_id]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(normalizedQuery);
  });
  const pageCount = Math.max(1, Math.ceil(filteredEntries.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const visibleEntries = filteredEntries.slice((safePage - 1) * pageSize, safePage * pageSize);
  const hasFilters = Boolean(query || actor || source || outcome || tool || hours);

  function clearFilters() {
    setQuery(""); setActor(""); setSource(""); setOutcome(""); setTool(""); setHours(0); setPage(1);
  }

  if (loadState === "loading") {
    return <PanelLoadingScreen label="Loading audit log…" />;
  }

  return (
    <div className="panel-app audit-app">
      <div className="panel-toolbar audit-toolbar">
        <Button onClick={() => { setReferenceTime(Date.now()); refresh(); }} disabled={isFetching}>Refresh</Button>
        <Button className="mobile-filter-toggle" type="button" aria-expanded={filtersOpen} onClick={() => setFiltersOpen((open) => !open)}>
          {filtersOpen ? "Close filters" : `Filter${hasFilters ? " •" : ""}`}
        </Button>
        <div className={`audit-filter-controls mobile-filter-controls ${filtersOpen ? "mobile-filter-controls-open" : ""}`}>
        <Input aria-label="Search loaded events" placeholder="Search action, actor, tool, or task" value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} />
        <SelectControl aria-label="Time range" value={hours} onChange={(event) => setHours(Number(event.target.value))}>
          <option value={0}>All time (loaded events)</option><option value={1}>Last hour</option><option value={24}>Last 24 hours</option><option value={168}>Last 7 days</option>
        </SelectControl>
        <SelectControl aria-label="Filter by actor" value={actor} onChange={(event) => setActor(event.target.value)}><option value="">All actors</option>{actors.map((value) => <option key={value}>{value}</option>)}</SelectControl>
        <SelectControl aria-label="Filter by source" value={source} onChange={(event) => setSource(event.target.value)}><option value="">All sources</option>{sources.map((value) => <option key={value}>{value}</option>)}</SelectControl>
        <SelectControl aria-label="Filter by outcome" value={outcome} onChange={(event) => setOutcome(event.target.value)}><option value="">All outcomes</option>{outcomes.map((value) => <option key={value}>{value}</option>)}</SelectControl>
        <SelectControl aria-label="Filter by tool" value={tool} onChange={(event) => { setTool(event.target.value); setPage(1); }}><option value="">All tools</option>{tools.map((value) => <option key={value}>{value}</option>)}</SelectControl>
        {hasFilters && <Button onClick={clearFilters}>Clear filters</Button>}
        </div>
      </div>
      <div className="audit-pagination"><p className="audit-scope-note">{filteredEntries.length} results from {entries.length} loaded events · latest 100 records received.</p><span>Page {safePage} of {pageCount}</span></div>
      {loadState === "error" && <p className="login-error">{errorMessage}</p>}
      <div className="audit-event-list">
        {visibleEntries.map((entry) => (
          <details className="audit-event" key={entry.id}>
            <summary>
              <StatusBadge label={outcomeLabel(entry.outcome)} tone={outcomeTone(entry.outcome)} />
              <strong>{entry.action}</strong>
              <span>{entry.actor}</span>
              <time>{formatDateTime(entry.created_at)}</time>
              <span className="audit-open-label">Open details</span>
            </summary>
            <dl>
              <div><dt>Source</dt><dd>{entry.source}</dd></div>
              <div><dt>Tool</dt><dd>{entry.tool_id ?? "—"}</dd></div>
              <div><dt>Task</dt><dd>{entry.task_id ?? "—"}</dd></div>
              <div><dt>Event ID</dt><dd>{entry.id}</dd></div>
            </dl>
            {entry.metadata && <pre className="tool-output">{JSON.stringify(entry.metadata, null, 2)}</pre>}
          </details>
        ))}
        {loadState === "ready" && filteredEntries.length === 0 && <p className="section-state">No events match the applied filters.</p>}
      </div>
      {filteredEntries.length > pageSize && <div className="audit-pagination"><Button disabled={safePage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>Previous page</Button><span>Page {safePage} of {pageCount}</span><Button disabled={safePage >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>Next page</Button></div>}
    </div>
  );
}
