import { useState } from "react";
import { Button, ProgressBar } from "react95";
import { fetchConversationStatus, fetchLunaMetrics } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { usePanelQuery } from "../lib/usePanelQuery";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";

export function deliveryMilliseconds(value: number | null): string {
  if (value === null) return "No samples";
  if (value >= 1000) return `${(value / 1000).toFixed(1)} s`;
  return `${Math.round(value)} ms`;
}

export function deliveryPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function deliveryProviderLabel(provider: string): string {
  if (provider === "opencode") return "OpenCode";
  if (provider === "ai_manager") return "AI Manager";
  if (provider === "openai") return "Luna";
  return provider || "Unknown";
}

export function deliveryModelChain(model: string | undefined): string[] {
  return model ? model.split(" -> ").filter(Boolean) : [];
}

export function deliveryRouteLabel(route: string): string {
  if (route === "chat") return "Free chat";
  if (route === "operations_shortcut") return "Operations shortcut";
  if (route === "operations_question") return "Operations question";
  if (route === "legacy") return "Legacy / unclassified";
  return "Conversation";
}

export function AiDeliveryApp() {
  const [days, setDays] = useState(1);
  const statusQuery = usePanelQuery(["conversation-status"], fetchConversationStatus);
  const metricsQuery = usePanelQuery(["ai-delivery", days], () => fetchLunaMetrics(days));
  const status = statusQuery.data;
  const delivery = metricsQuery.data?.ai_delivery;
  const loading = statusQuery.loadState === "loading" || metricsQuery.loadState === "loading";

  if (loading) return <PanelLoadingScreen label="Tracing AI delivery…" />;

  const chain = deliveryModelChain(status?.model);
  const reliability = delivery?.calls ? delivery.successful_calls / delivery.calls : 0;
  const latest = delivery?.recent[0];

  return (
    <div className="panel-app ai-delivery-app">
      <div className="panel-toolbar ai-delivery-toolbar">
        <Button type="button" onClick={() => { statusQuery.refresh(); metricsQuery.refresh(); }} disabled={statusQuery.isFetching || metricsQuery.isFetching}>Refresh trace</Button>
        <div className="ai-delivery-period" role="group" aria-label="Delivery period">
          {[1, 7, 30].map((period) => <Button key={period} active={days === period} onClick={() => setDays(period)}>{period === 1 ? "24h" : `${period}d`}</Button>)}
        </div>
        <span className={`ai-delivery-live ${status?.configured ? "is-live" : "is-offline"}`}><i />{status?.configured ? "ROUTE ARMED" : "ROUTE INCOMPLETE"}</span>
      </div>

      {(statusQuery.errorMessage || metricsQuery.errorMessage) && <p className="login-error">{statusQuery.errorMessage ?? metricsQuery.errorMessage}</p>}

      <section className="ai-delivery-route" aria-label="Conversation delivery route">
        <div className="ai-delivery-route-head">
          <span>TELEGRAM CONVERSATION ROUTE</span>
          <small>{latest ? `Last delivery ${formatDateTime(latest.created_at)}` : "Waiting for first measured delivery"}</small>
        </div>
        <div className="ai-delivery-pipeline">
          <article className="ai-delivery-node source"><span>01</span><strong>Telegram</strong><small>Operator channel</small></article>
          {chain.map((model, index) => (
            <article className={`ai-delivery-node ${index === 0 ? "primary" : "fallback"}`} key={`${model}-${index}`}>
              <span>{String(index + 2).padStart(2, "0")}</span>
              <strong>{model}</strong>
              <small>{index === 0 ? "Primary decision" : `Fallback tier ${index}`}</small>
            </article>
          ))}
          <article className="ai-delivery-node output"><span>{String(chain.length + 2).padStart(2, "0")}</span><strong>Validated reply</strong><small>Schema + governed tools</small></article>
        </div>
      </section>

      <section className="ai-delivery-kpis">
        <article><span>Conversation turns</span><strong>{delivery?.calls ?? 0}</strong><small>{days === 1 ? "last 24 hours" : `last ${days} days`}</small></article>
        <article><span>Reliability</span><strong>{delivery?.calls ? deliveryPercent(reliability) : "N/D"}</strong><ProgressBar aria-label="Conversation delivery reliability" value={Math.round(reliability * 100)} /><small>{delivery?.successful_calls ?? 0} successful · {delivery?.failed_calls ?? 0} failed</small></article>
        <article><span>Model path latency</span><strong>{deliveryMilliseconds(delivery?.latency.average_ms ?? null)}</strong><small>{delivery?.latency.samples ?? 0} measured turns · sums measured model decisions</small></article>
        <article><span>p95 path latency</span><strong>{deliveryMilliseconds(delivery?.latency.p95_ms ?? null)}</strong><small>timeout budget {status?.timeout_seconds ?? 0}s</small></article>
        <article className={(delivery?.fallback_calls ?? 0) > 0 ? "has-fallback" : ""}><span>Fallback rate</span><strong>{deliveryPercent(delivery?.fallback_rate ?? 0)}</strong><small>{delivery?.fallback_calls ?? 0} turns routed away from primary</small></article>
      </section>

      <div className="ai-delivery-grid">
        <section className="ai-delivery-board">
          <header><h3>Delivery ledger</h3><span>latest 20</span></header>
          <div className="ai-delivery-ledger">
            {delivery?.recent.map((row) => (
              <article key={row.id} className={`delivery-row is-${row.status}`}>
                <time>{formatDateTime(row.created_at)}</time>
                <span className={`delivery-provider provider-${row.provider}`}>{deliveryProviderLabel(row.provider)}</span>
                <div><strong>{row.model}</strong><small>{deliveryRouteLabel(row.route_mode)} · {row.fallback_used ? `Fallback · ${row.fallback_reason || "primary unavailable"}` : "Primary route"}</small></div>
                <b>{deliveryMilliseconds(row.inference_latency_ms)}</b>
                <i title={row.error_kind || row.status}>{row.status === "success" ? "OK" : "ERR"}</i>
              </article>
            ))}
            {!delivery?.recent.length && <div className="ai-delivery-empty"><strong>No measured conversations yet.</strong><span>Send a Telegram message after deployment to populate this ledger.</span></div>}
          </div>
        </section>

        <aside className="ai-delivery-board ai-delivery-mix">
          <header><h3>Traffic mix</h3><span>{days === 1 ? "24h" : `${days}d`}</span></header>
          <h4>Providers</h4>
          {Object.entries(delivery?.providers ?? {}).map(([provider, count]) => <div className="delivery-mix-row" key={provider}><span>{deliveryProviderLabel(provider)}</span><strong>{count}</strong></div>)}
          {Object.keys(delivery?.providers ?? {}).length === 0 && <p>No provider samples.</p>}
          <h4>Effective models</h4>
          {Object.entries(delivery?.models ?? {}).map(([model, count]) => <div className="delivery-mix-row" key={model}><span>{model}</span><strong>{count}</strong></div>)}
          <h4>Routes</h4>
          {Object.entries(delivery?.routes ?? {}).map(([route, count]) => <div className="delivery-mix-row" key={route}><span>{deliveryRouteLabel(route)}</span><strong>{count}</strong></div>)}
          <div className="ai-delivery-limits">
            <span>Context</span><b>{status?.max_turns ?? 0} turns</b>
            <span>Tool budget</span><b>{status?.max_tool_calls ?? 0} calls</b>
            <span>Output ceiling</span><b>{status?.max_output_tokens ?? 0} tokens</b>
          </div>
        </aside>
      </div>
    </div>
  );
}
