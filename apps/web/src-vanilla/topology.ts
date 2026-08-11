import { fetchTopologySnapshot } from "../src/lib/api";
import { formatDateTime } from "../src/lib/format";
import type { TopologyNode, TopologySnapshot } from "../src/lib/types";
import { button, element, replaceChildren } from "./dom";

export function mountTopology(target: HTMLElement, searchInput: HTMLInputElement): () => void {
  let snapshot: TopologySnapshot | null = null;
  let selectedId: string | null = null;
  let impactView = false;
  let query = "";
  let loading = true;
  let errorMessage = "";
  let active = true;
  const refreshButton = button("Force refresh", "quiet-button");
  const controls = element("div", { className: "filter-bar" });
  const content = element("div", { className: "topology-layout" });

  function nodeCard(node: TopologyNode): HTMLElement {
    const incidentCount = snapshot?.incidents.filter((incident) => incident.provider_id === node.provider_id && incident.status === "open").length ?? 0;
    const card = button(node.label, `topology-node${selectedId === node.id ? " topology-node--selected" : ""}`);
    replaceChildren(card, element("span", { className: `state-dot state-dot--${node.status === "healthy" ? "healthy" : node.status === "degraded" || node.status === "unknown" ? "warning" : "critical"}` }), element("span", { className: "record-copy" }, element("strong", {}, node.label), element("small", {}, `${node.kind} · ${node.role || node.id}`)), incidentCount ? element("span", { className: "incident-count" }, String(incidentCount)) : null);
    card.addEventListener("click", () => { selectedId = node.id; render(); }); return card;
  }

  function renderDetail(node: TopologyNode | undefined): HTMLElement {
    if (!node) return element("aside", { className: "topology-detail" }, element("p", { className: "detail-index" }, "GRAPH CONTEXT"), element("h2", {}, "Select a node"), element("p", { className: "detail-empty" }, "Inspect dependencies, live state, observations, and incidents."));
    const upstream = snapshot!.graph.edges.filter((edge) => edge.target === node.id);
    const downstream = snapshot!.graph.edges.filter((edge) => edge.source === node.id);
    const observations = snapshot!.observations.filter((observation) => observation.provider_id === node.provider_id || observation.id === node.observation_id);
    const incidents = snapshot!.incidents.filter((incident) => incident.provider_id === node.provider_id && incident.status === "open");
    const facts = element("dl", { className: "detail-facts" });
    for (const [label, value] of [["Status", node.status], ["Layer", node.layer], ["Provider", node.provider_id || "None"], ["Upstream", upstream.map((edge) => edge.source).join(", ") || "None"], ["Downstream", downstream.map((edge) => edge.target).join(", ") || "None"]]) facts.append(element("div", {}, element("dt", {}, label), element("dd", {}, value)));
    return element("aside", { className: "topology-detail" }, element("p", { className: "item-kind" }, node.kind), element("h2", {}, node.label), element("p", { className: "detail-description" }, node.status_detail || "No additional status detail."), facts, element("div", { className: "detail-notes" }, element("h3", {}, `Observations (${observations.length})`), ...observations.map((observation) => element("p", {}, element("strong", {}, observation.label), `${observation.status} · ${observation.detail}`))), element("div", { className: "detail-notes" }, element("h3", {}, `Open incidents (${incidents.length})`), ...incidents.map((incident) => element("p", {}, element("strong", {}, incident.title), incident.description))));
  }

  function render(): void {
    const physical = button("Physical", `filter-button${!impactView ? " filter-button--active" : ""}`);
    const impact = button("Failure impact", `filter-button${impactView ? " filter-button--active" : ""}`);
    physical.addEventListener("click", () => { impactView = false; render(); }); impact.addEventListener("click", () => { impactView = true; render(); });
    replaceChildren(controls, physical, impact);
    if (loading) { replaceChildren(content, element("div", { className: "loading-state" }, "Loading topology snapshot")); return; }
    if (!snapshot) { replaceChildren(content, element("p", { className: "error-banner", role: "alert" }, errorMessage || "Topology unavailable.")); return; }
    const normalized = query.trim().toLowerCase();
    const visibleNodes = snapshot.graph.nodes.filter((node) => !normalized || [node.label, node.id, node.kind, node.layer, node.provider_id].join(" ").toLowerCase().includes(normalized));
    const graph = element("div", { className: "topology-graph" });
    for (const layer of snapshot.graph.layer_order) {
      const nodes = visibleNodes.filter((node) => node.layer === layer);
      if (nodes.length) graph.append(element("section", { className: "topology-layer" }, element("h3", {}, layer), element("div", { className: "topology-node-grid" }, ...nodes.map(nodeCard))));
    }
    const edges = snapshot.graph.edges.filter((edge) => !impactView || edge.affects_rca);
    graph.append(element("section", { className: "topology-edges" }, element("h3", {}, `${impactView ? "Failure-impact" : "Declared"} relationships`), ...edges.map((edge) => element("p", {}, element("strong", {}, edge.source), ` → ${edge.target}`, element("span", {}, edge.label || edge.kind)))));
    const selected = snapshot.graph.nodes.find((node) => node.id === selectedId);
    replaceChildren(content, element("div", {}, snapshot.graph.warnings.length ? element("p", { className: "error-banner" }, snapshot.graph.warnings.join(" · ")) : null, element("p", { className: "result-summary" }, `${snapshot.graph.nodes.length} nodes · ${edges.length} relationships · generated ${formatDateTime(snapshot.generated_at)}`), graph), renderDetail(selected));
  }
  async function load(force = false): Promise<void> { loading = true; refreshButton.disabled = true; render(); try { snapshot = await fetchTopologySnapshot(force); errorMessage = ""; } catch (error) { errorMessage = error instanceof Error ? error.message : "Unable to load topology."; } finally { if (active) { loading = false; refreshButton.disabled = false; render(); } } }
  const handleSearch = () => { query = searchInput.value; render(); };
  const timer = window.setInterval(() => { if (!document.hidden) void load(false); }, 15_000);
  searchInput.addEventListener("input", handleSearch); refreshButton.addEventListener("click", () => void load(true));
  replaceChildren(target, element("section", { className: "inbox-page", "aria-labelledby": "topology-heading" }, element("header", { className: "inbox-heading compact-heading" }, element("div", {}, element("p", { className: "eyebrow" }, "Dependencies"), element("h1", { id: "topology-heading" }, "Topology"), element("p", { className: "inbox-intro" }, "Physical relationships and failure impact without decorative graph noise.")), refreshButton), controls, content));
  void load();
  return () => { active = false; window.clearInterval(timer); searchInput.removeEventListener("input", handleSearch); };
}
