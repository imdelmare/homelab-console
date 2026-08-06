import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Avatar, Button, TextInput as Input, TextInput as TextArea } from "react95";
import { SelectControl } from "../components/SelectControl";
import { useQueryClient } from "@tanstack/react-query";
import {
  consumeMcpPairing,
  fetchMcpClients,
  fetchMcpPairingRequests,
  forgetMcpClient,
  revokeMcpClient,
  rotateMcpClient,
  startMcpPairing,
} from "../lib/api";
import { formatDateTime, parseApiDate } from "../lib/format";
import { describeError, isMcpClientOnline, mcpPairingDisplayStatus, shortId } from "../lib/ui";
import { combineLoadStates, usePanelQuery } from "../lib/usePanelQuery";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PanelLoadingScreen } from "../components/PanelLoadingScreen";
import type { McpClient, McpPairingStart } from "../lib/types";

type McpAgentId = "codex" | "claude" | "fixer" | "cline" | "opencode";

const MCP_ENDPOINT = import.meta.env.VITE_MCP_ENDPOINT ?? `http://${window.location.hostname}:8765/mcp/`;

function onboardingPrompt(agentId: McpAgentId, endpoint: string) {
  const agentNotes: Record<McpAgentId, string> = {
    codex: "Add it as a streamable HTTP MCP server in Codex.",
    claude: "Add it as a remote HTTP MCP server in Claude.",
    fixer: "Store the dedicated token only in the Fixer OpenCode profile. Follow docs/OPENCODE_WORKER.md and never claim open work.",
    cline: "Add the URL and Authorization header to Cline's MCP HTTP configuration.",
    opencode: "Configure type=remote, OAuth=false, and read the bearer token from HOMELAB_MCP_TOKEN.",
  };
  return `Connect ${agentId} to Homelab Console MCP.

The operator performs Telegram pairing in the MCP Clients window and will provide the per-client hmc_... token exactly once.
Do not call the internal /api/mcp/pairing endpoints yourself.

MCP URL: ${endpoint}
Transport: streamable HTTP
Header: Authorization: Bearer <token>

${agentNotes[agentId]}
Store the token locally and never use a shared static token.`;
}

const MCP_DEFAULT_LABELS: Record<McpAgentId, string> = {
  codex: "Codex workstation",
  claude: "Claude workstation",
  fixer: "Fixer",
  cline: "Cline workstation",
  opencode: "OpenCode workstation",
};

export function McpClientsApp() {
  const queryClient = useQueryClient();
  const clientsQuery = usePanelQuery(["mcp-clients"], fetchMcpClients);
  const pairingQuery = usePanelQuery(["mcp-pairing-requests"], () => fetchMcpPairingRequests());
  const clients = clientsQuery.data ?? [];
  const pairingRequests = pairingQuery.data ?? [];
  const loadState = combineLoadStates(clientsQuery.loadState, pairingQuery.loadState);
  const [actionError, setActionError] = useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<McpClient | null>(null);
  const [revokeReason, setRevokeReason] = useState("operator_revoked");
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [forgetTarget, setForgetTarget] = useState<McpClient | null>(null);
  const [forgettingId, setForgettingId] = useState<string | null>(null);
  const [onboardingAgent, setOnboardingAgent] = useState<McpAgentId>("codex");
  const [pairingLabel, setPairingLabel] = useState(MCP_DEFAULT_LABELS.codex);
  const [pairingHost, setPairingHost] = useState("");
  const [pairingRequest, setPairingRequest] = useState<McpPairingStart | null>(null);
  const [pairingBusy, setPairingBusy] = useState(false);
  const [pairingStatus, setPairingStatus] = useState<string | null>(null);
  const [pairingToken, setPairingToken] = useState<{ client: McpClient; token: string; endpoint: string } | null>(null);
  const [rotatingId, setRotatingId] = useState<string | null>(null);
  const [rotatedToken, setRotatedToken] = useState<{ client: McpClient; token: string } | null>(null);
  const [activeRegistryTab, setActiveRegistryTab] = useState<"clients" | "requests" | "pairing">("clients");

  function refresh() {
    clientsQuery.refresh();
    pairingQuery.refresh();
  }

  function invalidateRegistry() {
    void queryClient.invalidateQueries({ queryKey: ["mcp-clients"] });
    void queryClient.invalidateQueries({ queryKey: ["mcp-pairing-requests"] });
    void queryClient.invalidateQueries({ queryKey: ["audit"] });
  }

  useEffect(() => {
    if (!pairingRequest) return;
    let cancelled = false;
    let inFlight = false;
    const requestId = pairingRequest.request_id;
    const pairingSecret = pairingRequest.pairing_secret;
    const expiresAt = parseApiDate(pairingRequest.expires_at).getTime();

    async function pollPairing() {
      if (inFlight) return;
      if (Date.now() >= expiresAt) {
        setPairingStatus("Pairing expired. Start a new request.");
        setPairingRequest(null);
        return;
      }
      inFlight = true;
      try {
        const result = await consumeMcpPairing(requestId, pairingSecret);
        if (cancelled) return;
        if (!result.ok) {
          setPairingStatus(`${result.error.message || result.error.code} Automatically verifying every 2 seconds.`);
          return;
        }
        setPairingToken({ client: result.client, token: result.token, endpoint: MCP_ENDPOINT });
        setPairingRequest(null);
        setPairingStatus("Approved. Token generated.");
        invalidateRegistry();
      } catch (error) {
        if (!cancelled) setPairingStatus(describeError(error));
      } finally {
        inFlight = false;
      }
    }

    void pollPairing();
    const interval = window.setInterval(() => void pollPairing(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairingRequest?.request_id]);

  function openRevokeDialog(client: McpClient) {
    setRevokeTarget(client);
    setRevokeReason("operator_revoked");
  }

  function changeOnboardingAgent(agentId: McpAgentId) {
    setOnboardingAgent(agentId);
    setPairingLabel((current) => {
      const knownDefault = Object.values(MCP_DEFAULT_LABELS).includes(current);
      return knownDefault ? MCP_DEFAULT_LABELS[agentId] : current;
    });
  }

  async function handleStartPairing(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPairingBusy(true);
    setPairingStatus(null);
    setActionError(null);
    try {
      const request = await startMcpPairing({
        agent_id: onboardingAgent,
        client_label: pairingLabel.trim(),
        host_fingerprint: pairingHost.trim(),
      });
      setPairingRequest(request);
      setPairingStatus(`Telegram delivery: ${request.delivery_status}. Automatically verifying every 2 seconds.`);
      invalidateRegistry();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setPairingBusy(false);
    }
  }

  async function handleConsumePairing() {
    if (!pairingRequest) return;
    setPairingBusy(true);
    setActionError(null);
    try {
      const result = await consumeMcpPairing(pairingRequest.request_id, pairingRequest.pairing_secret);
      if (!result.ok) {
        setPairingStatus(result.error.message || result.error.code);
        return;
      }
      setPairingToken({ client: result.client, token: result.token, endpoint: MCP_ENDPOINT });
      setPairingRequest(null);
      setPairingStatus("Approved. Token generated.");
      invalidateRegistry();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setPairingBusy(false);
    }
  }

  async function confirmRevoke() {
    if (!revokeTarget) return;
    setRevokingId(revokeTarget.id);
    try {
      await revokeMcpClient(revokeTarget.id, revokeReason.trim() || "operator_revoked");
      setRevokeTarget(null);
      invalidateRegistry();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setRevokingId(null);
    }
  }

  async function confirmForget() {
    if (!forgetTarget) return;
    setForgettingId(forgetTarget.id);
    setActionError(null);
    try {
      await forgetMcpClient(forgetTarget.id);
      setForgetTarget(null);
      invalidateRegistry();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setForgettingId(null);
    }
  }

  async function handleRotate(client: McpClient) {
    setRotatingId(client.id);
    setActionError(null);
    try {
      const result = await rotateMcpClient(client.id);
      setRotatedToken({ client: result.client, token: result.token });
      invalidateRegistry();
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      setRotatingId(null);
    }
  }

  const activeClients = clients.filter((client) => !client.revoked_at);
  const onlineClients = activeClients.filter((client) => isMcpClientOnline(client));
  const shownError = clientsQuery.errorMessage ?? pairingQuery.errorMessage ?? actionError;
  const missingPairingFields = [
    !pairingLabel.trim() ? "client name" : "",
    !pairingHost.trim() ? "fingerprint host" : "",
  ].filter(Boolean);

  if (loadState === "loading") {
    return <PanelLoadingScreen label="Loading MCP clients…" />;
  }

  return (
    <div className="panel-app mcp-app">
      <div className="panel-toolbar">
        <Button onClick={refresh} disabled={clientsQuery.isFetching || pairingQuery.isFetching}>
          Refresh
        </Button>
        <span className="toolbar-count">
          online {onlineClients.length} / active {activeClients.length} / total {clients.length}
        </span>
      </div>
      <Button className="mobile-list-cta" type="button" onClick={() => { setActiveRegistryTab("clients"); document.getElementById("mcp-client-registry")?.scrollIntoView({ behavior: "smooth", block: "start" }); }}>
        View {clients.length} registered clients ↓
      </Button>
      {shownError && <p className="login-error">{shownError}</p>}
      <div className="mcp-workspace">
      <div className="mcp-tabs" role="tablist" aria-label="MCP client management">
        <Button type="button" role="tab" aria-selected={activeRegistryTab === "clients"} className={activeRegistryTab === "clients" ? "pressed" : ""} onClick={() => setActiveRegistryTab("clients")}>Clients</Button>
        <Button type="button" role="tab" aria-selected={activeRegistryTab === "requests"} className={activeRegistryTab === "requests" ? "pressed" : ""} onClick={() => setActiveRegistryTab("requests")}>Requests</Button>
        <Button type="button" role="tab" aria-selected={activeRegistryTab === "pairing"} className={activeRegistryTab === "pairing" ? "pressed" : ""} onClick={() => setActiveRegistryTab("pairing")}>New pairing</Button>
      </div>
      {activeRegistryTab === "pairing" && (
      <section className="mcp-onboarding-panel">
        <div className="mcp-onboarding-head">
          <div>
            <h3>Guided MCP pairing</h3>
            <span>Telegram pairing for an individual client. The token is shown only once.</span>
          </div>
          <SelectControl
            value={onboardingAgent}
            onChange={(event) => changeOnboardingAgent(event.target.value as McpAgentId)}
          >
            <option value="codex">Codex</option>
            <option value="claude">Claude</option>
            <option value="fixer">Fixer</option>
            <option value="cline">Cline</option>
            <option value="opencode">OpenCode</option>
          </SelectControl>
        </div>
        <form className="mcp-pairing-form mcp-guided-pairing" onSubmit={handleStartPairing}>
          <p className="mcp-step-progress">Step {pairingHost.trim() ? 4 : pairingLabel.trim() ? 3 : 2} of 4</p>
          <ol className="mcp-step-checklist"><li className="complete">Client type selected</li><li className={pairingLabel.trim() && pairingHost.trim() ? "complete" : "current"}>Name and fingerprint</li><li className={pairingHost.trim() ? "complete" : ""}>Verifiable endpoint</li><li>Telegram pairing</li></ol>
          <div className="mcp-pairing-step mcp-pairing-step-agent"><strong>1</strong><span>Select the client type from the menu above.</span></div>
          <label className="field-row-stacked mcp-pairing-step">
            <strong>2</strong><span>Client name</span>
            <Input value={pairingLabel} onChange={(event) => setPairingLabel(event.target.value)} />
          </label>
          <label className="field-row-stacked mcp-pairing-step">
            <strong>2</strong><span>Fingerprint host</span>
            <Input
              value={pairingHost}
              placeholder="Client hostname"
              onChange={(event) => setPairingHost(event.target.value)}
            />
          </label>
          <label className="field-row-stacked mcp-pairing-step mcp-pairing-endpoint">
            <strong>3</strong><span>Verify MCP endpoint · HTTPS via Cloudflare Tunnel</span>
            <Input readOnly value={MCP_ENDPOINT} />
          </label>
          <div className="mcp-pairing-submit mcp-pairing-step">
            <strong>4</strong>
            <Button type="submit" disabled={pairingBusy || missingPairingFields.length > 0}>
              {pairingBusy ? "Starting…" : "Start Telegram pairing"}
            </Button>
            {missingPairingFields.length > 0 && <small>Complete: {missingPairingFields.join(" and ")}.</small>}
          </div>
        </form>
        {pairingRequest && (
          <div className="mcp-pairing-status">
            <span>request {shortId(pairingRequest.request_id)}</span>
            <span>{pairingRequest.status} · expires {formatDateTime(pairingRequest.expires_at)}</span>
            <Button type="button" disabled={pairingBusy} onClick={handleConsumePairing}>
              {pairingBusy ? "Verifying…" : "Verify approval"}
            </Button>
          </div>
        )}
        {pairingStatus && <p className="mcp-pairing-note">{pairingStatus}</p>}
        <details className="mcp-onboarding-instructions">
          <summary>Client setup instructions</summary>
          <TextArea multiline readOnly value={onboardingPrompt(onboardingAgent, MCP_ENDPOINT)} rows={10} />
        </details>
      </section>
      )}
      {activeRegistryTab === "requests" && (
      <section className="mcp-pairing-history">
        <div className="mcp-section-head">
          <h3>Pairing requests</h3>
          <span>{pairingRequests.length} recent</span>
        </div>
        <div className="mcp-pairing-list">
          {pairingRequests.slice(0, 4).map((request) => {
            const displayStatus = mcpPairingDisplayStatus(request);
            return (
              <article className="mcp-pairing-row" key={request.id}>
                <div>
                  <strong>
                    {request.agent_id} · {request.client_label || "MCP client"}
                  </strong>
                  <span>{request.host_fingerprint || "unknown host"}</span>
                  <small>
                    request {shortId(request.id)} · delivery {request.delivery_status} · created{" "}
                    {formatDateTime(request.created_at)}
                  </small>
                </div>
                <div className="mcp-pairing-row-meta">
                  <mark className={`mcp-pairing-badge mcp-pairing-${displayStatus}`}>
                    {displayStatus}
                  </mark>
                  <small>
                    {request.consumed_at
                      ? `used ${formatDateTime(request.consumed_at)}`
                      : request.approved_at
                        ? `approved ${formatDateTime(request.approved_at)}`
                        : request.denied_at
                          ? `denied ${formatDateTime(request.denied_at)}`
                          : displayStatus === "expired"
                            ? `expired ${formatDateTime(request.expires_at)}`
                            : `expires ${formatDateTime(request.expires_at)}`}
                  </small>
                  {request.decided_by && <small>{request.decided_by}</small>}
                </div>
              </article>
            );
          })}
          {loadState === "ready" && pairingRequests.length === 0 && <p>No pairing requests.</p>}
        </div>
      </section>
      )}
      {activeRegistryTab === "clients" && (
      <section className="mcp-client-registry" id="mcp-client-registry">
        <div className="mcp-section-head">
          <h3>Registered clients</h3>
          <span>{activeClients.length} active · {clients.length - activeClients.length} revoked</span>
        </div>
      <div className="item-list mcp-client-list">
        {clients.map((client) => {
          const online = isMcpClientOnline(client);
          const revoked = Boolean(client.revoked_at);
          return (
            <article className={`item-row item-row-stacked mcp-client-row ${revoked ? "mcp-client-revoked" : ""}`} key={client.id}>
              <div className="mcp-client-identity">
              <Avatar className="agent-avatar mcp-client-avatar" size="34px" title={client.agent_id} aria-hidden="true">
                {client.agent_id.slice(0, 2).toUpperCase()}
              </Avatar>
              <div className="task-row-main">
                <strong>
                  {client.agent_id} · {client.client_label || "MCP client"}
                </strong>
                <span>{client.host_fingerprint || "unknown host"}</span>
                <small>
                  token *{client.token_hint || "--------"} · created {formatDateTime(client.created_at)} · last activity{" "}
                  {formatDateTime(client.last_seen_at)}
                </small>
                {client.revoked_reason && <small>revoked: {client.revoked_reason}</small>}
              </div>
              </div>
              <div className="watcher-incident-meta">
                <mark className={`task-status task-status-${revoked ? "cancelled" : online ? "completed" : "waiting_operator"}`}>
                  {revoked ? "revoked" : online ? "online" : "inactive"}
                </mark>
                {revoked ? (
                  <Button
                    type="button"
                    disabled={forgettingId === client.id}
                    onClick={() => setForgetTarget(client)}
                  >
                    {forgettingId === client.id ? "Removing…" : "Remove"}
                  </Button>
                ) : (
                  <>
                    <Button
                      type="button"
                      disabled={revokingId === client.id}
                      onClick={() => openRevokeDialog(client)}
                    >
                      {revokingId === client.id ? "Revoking…" : "Revoke token"}
                    </Button>
                    <Button
                      type="button"
                      disabled={rotatingId === client.id}
                      onClick={() => handleRotate(client)}
                    >
                      {rotatingId === client.id ? "Rotating…" : "Rotate token"}
                    </Button>
                  </>
                )}
              </div>
            </article>
          );
        })}
        {loadState === "ready" && clients.length === 0 && <p>No MCP clients registered.</p>}
      </div>
      </section>
      )}
      </div>
      {revokeTarget && (
        <ConfirmDialog
          title="Revoke MCP token"
          message={`Revoke token for ${revokeTarget.agent_id} (${revokeTarget.client_label || revokeTarget.id.slice(0, 8)})?`}
          confirmLabel="Revoke"
          onConfirm={confirmRevoke}
          onCancel={() => setRevokeTarget(null)}
        >
          <label className="field-row-stacked">
            <span>Reason</span>
            <Input value={revokeReason} onChange={(event) => setRevokeReason(event.target.value)} />
          </label>
        </ConfirmDialog>
      )}
      {forgetTarget && (
        <ConfirmDialog
          title="Remove revoked MCP client"
          message={`Permanently forget ${forgetTarget.agent_id} (${forgetTarget.client_label || forgetTarget.id.slice(0, 8)})? Pairing history and audit events will remain.`}
          confirmLabel={forgettingId ? "Removing…" : "Remove"}
          busy={Boolean(forgettingId)}
          onConfirm={confirmForget}
          onCancel={() => setForgetTarget(null)}
        />
      )}
      {rotatedToken && (
        <ConfirmDialog
          title="MCP token rotated"
          message={`New token for ${rotatedToken.client.agent_id} (${rotatedToken.client.client_label || rotatedToken.client.id.slice(0, 8)}). It is shown once.`}
          confirmLabel="Close"
          onConfirm={() => setRotatedToken(null)}
          onCancel={() => setRotatedToken(null)}
        >
          <label className="field-row-stacked">
            <span>Bearer token</span>
            <TextArea multiline readOnly value={rotatedToken.token} rows={3} />
          </label>
        </ConfirmDialog>
      )}
      {pairingToken && (
        <ConfirmDialog
          title="MCP token generated"
          message={`Token for ${pairingToken.client.agent_id} (${pairingToken.client.client_label || pairingToken.client.id.slice(0, 8)}). It is shown once.`}
          confirmLabel="Close"
          onConfirm={() => setPairingToken(null)}
          onCancel={() => setPairingToken(null)}
        >
          <label className="field-row-stacked">
            <span>Bearer token</span>
            <TextArea multiline readOnly value={pairingToken.token} rows={3} />
          </label>
          <label className="field-row-stacked">
            <span>MCP endpoint</span>
            <Input readOnly value={pairingToken.endpoint} />
          </label>
          <label className="field-row-stacked">
            <span>Authorization header</span>
            <TextArea multiline readOnly value={`Authorization: Bearer ${pairingToken.token}`} rows={3} />
          </label>
        </ConfirmDialog>
      )}
    </div>
  );
}
