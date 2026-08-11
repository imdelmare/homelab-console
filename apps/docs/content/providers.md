# Providers

A **provider** is the adapter layer between a tool and a real homelab
system. Tools never talk to Proxmox, OPNsense, etc. directly — they call a
provider, which owns connection details, auth, and error handling for that
one system.

## Provider abstraction

Providers live under `apps/api/app/providers/`:

- `base.py` — the provider interface every adapter implements (connect,
  health/status check, and the operations backing that provider's tools).
- `registry.py` — maps `provider_id` to a provider instance/class, used by
  the execution core to resolve which provider backs a given tool.
- `httpclient.py` — the shared HTTP client base most providers build on:
  applies the [error taxonomy](#error-taxonomy) and the TLS policy in one
  place, so individual adapters don't reimplement either.

Providers are read-only by default. Exact write capabilities exist only when
they are separately governed and approval-bound. There are 17 built-in
provider instances across 16 integration families (the two FritzBox entries
are separate instances); allowlisted API-ready instances can be added by
config:

| Directory | `provider_id` | Talks to |
|---|---|---|
| `proxmox/` | `proxmox` | Proxmox VE API (API token) |
| `pbs/` | `pbs` | Proxmox Backup Server API (API token) |
| `opnsense/` | `opnsense` | OPNsense API (key/secret) |
| `mikrotik/` | `mikrotik` | RouterOS REST API |
| `homeassistant/` | `homeassistant` | Home Assistant REST API (long-lived token) |
| `frigate/` | `frigate` | Frigate HTTP API |
| `adguard/` | `adguard` | AdGuard Home control API |
| `nextcloud/` | `nextcloud` | `status.php` + OCS APIs (app password) |
| `nutups/` | `nutups` | Network UPS Tools `upsd` text protocol |
| `asterisk/` | `asterisk` | AMI, whitelisted read actions only |
| `uptimekuma/` | `uptimekuma` | `/metrics` (API key) + status pages |
| `emqx/` | `emqx` | EMQX v5 management API |
| `cloudflaretunnel/` | `cloudflaretunnel` | Official Cloudflare Tunnel and connector APIs |
| `api_ready/` | declared instance ID | Allowlisted API drivers, including the official Cloudflare Tunnel API |
| `vps/` | `vps` | VPS reachability, Glances, WireGuard and deploy status endpoints |
| `zerotier/` | `zerotier` | ZeroTier Central Legacy v1 API (personal API token) |
| `fritzbox/` | `fritzbox_primary`, `fritzbox_secondary` | TR-064/UPnP (one instance per box) |

## Shared transport contracts and special providers

The standard HTTP/JSON providers are Proxmox, PBS, OPNsense, MikroTik,
Home Assistant, Frigate, AdGuard, Nextcloud, Uptime Kuma, EMQX, Cloudflare
Tunnel, ZeroTier and the VPS Glances integration. They share `BaseJsonClient`,
including TLS enforcement, timeouts, error mapping and explicit per-call
response modes (`json`, `text` or `auto`). A successful text/HTML response
cannot silently pass through a JSON operation.

### OPNsense Wake-on-LAN

`opnsense.wol.wake` is a disabled-by-default write capability defined by ADR
0007. It requires the optional OPNsense `os-wol` plugin and accepts only a
configured `target_id`. Homelab Console resolves that id to an OPNsense
saved-host UUID and calls only `POST /api/wol/wol/set`. MAC addresses,
interfaces, broadcast addresses and arbitrary API paths are never caller
inputs, and the plugin's `wakeall` action is not exposed.

A successful result confirms only that OPNsense reported sending the magic
packet; WoL has no reliable power-state acknowledgement. ADR 0008 records the
completed `operator-workstation` drill and activates the exact tool id through
governance. The confirmed plugin surface uses `searchHost`/`setHost` naming
for host administration and `set` for sending to one saved host; Homelab
Console exposes only the latter.

### OPNsense gateway transitions and egress

`opnsense.gateway.failover` and `opnsense.gateway.restore` accept no caller
input. They resolve only the primary and backup UUID/name pairs declared under
`providers.opnsense.gateway_failover`, apply the transition in a safe order
and require gateway-status plus kernel default-route read-back. ADR 0010
records the successful failover and separately approved restore drill.

The same scoped network-action identity supplies only the exact WoL, gateway
configuration/status and routing-table privileges needed by these tools. They
never fall back to the general OPNsense reader credentials.

`opnsense.egress.switch` remains disabled. ADR 0011 records that changing
gateway upstream flags moved the firewall's kernel default route but an
existing LAN firewall policy-routing rule kept client traffic on primary ISP.
Any successor must pin that declared firewall rule by UUID, keep all gateway
and WireGuard endpoint boundaries closed, and verify the public country using
the fixed `network.egress.status` observation before activation.

`GET /api/provider-definitions` exposes their non-secret catalog metadata:
driver, configuration key names, tool capabilities and typed observation IDs.
`GET /api/observations` executes narrow read tools through the shared execution
core and projects normalized results into capability health.

The standard TCP text transport is implemented by `BaseTcpTextClient`. It owns
bounded line/response reads, connection lifecycle, operation deadlines and the
same normalized reachability/error vocabulary. It does **not** expose commands.
Two explicit drivers currently use it:

- `asterisk_ami_v1` — AMI banner/login, key-value blocks and event streams;
- `nut_upsd_v1` — NUT commands, `BEGIN/END` lists and quoted values.

AMI actions and NUT operations remain separately allowlisted. They share no
wire commands, parser or authentication state machine, and no REST/MCP caller
can provide a TCP payload.

These integrations still remain outside the shared transport catalogs:

- FritzBox (SOAP/TR-064)
- the future source-routed primary ISP probe

Their future observation model can change independently. Asterisk and NUT also
retain protocol-specific observations/normalizers even though their connection
transport is now shared.

## Cloudflare Tunnel API provider

The canonical `cloudflaretunnel` provider uses the fixed official Cloudflare
API origin with mandatory TLS verification. It replaces the legacy public URL
probes and accepts no URL, account or tunnel input from REST/MCP callers:

```yaml
providers:
  cloudflaretunnel:
    account_id: 0123456789abcdef0123456789abcdef
    tunnel_ids:
      - 11111111-2222-4333-8444-555555555555
    timeout_seconds: 8
```

The required credential is an account-scoped API token with only
`Cloudflare Tunnel Read`. It is not the `eyJ...` runtime token used to start
cloudflared:

```yaml
cloudflaretunnel:
  bearer_token: EXAMPLE-READ-ONLY-TOKEN
```

The read-only tools are `cloudflare.tunnels.status`,
`cloudflare.connectors.list` and `cloudflare.summary`. Connector UUIDs,
connection UUIDs, origin IPs, colo names, metadata and raw Cloudflare payloads
are discarded. The provider owns tunnel-to-edge health and feeds the
`cloudflaretunnel.tunnel` topology observation plus the dedicated
`cloudflare.tunnel` watcher. Uptime Kuma
remains the canonical source for individual public application availability.

## Configuration-driven API-ready instances

Small systems implementing the narrow `json_health_v1` contract can be added
without copying a client:

```yaml
api_provider_instances:
  - id: example_service
    name: Example Service
    driver: json_health_v1
    base_url: https://example-service.internal
    verify_tls: true
    timeout_seconds: 5
```

After restart this adds the provider and one read-only tool,
`example_service.health.status`. The driver always requests `GET /health`,
accepts only a JSON object with a known `status` value, and returns only
`instance_id`, normalized `status` and `reported_status`. REST/MCP callers
cannot supply a URL or path, and arbitrary response fields are discarded.
An optional bearer token lives under `example_service.bearer_token` in the
gitignored secrets file.

The shared `cloudflare_tunnel_v1` transport can also back an additional,
independent compatibility instance when a separate account/tunnel must remain
isolated from the canonical provider:

```yaml
api_provider_instances:
  - id: cloudflare_home
    name: Cloudflare Home Tunnel
    driver: cloudflare_tunnel_v1
    account_id: 0123456789abcdef0123456789abcdef
    tunnel_id: 11111111-2222-4333-8444-555555555555
    timeout_seconds: 8
```

Its `cloudflare_home.tunnel.status` tool always calls the fixed official
Cloudflare API origin and the declared account/tunnel path. TLS verification
cannot be disabled. The required `cloudflare_home.bearer_token` secret must be
an account-scoped token with only `Cloudflare Tunnel Read`; raw Cloudflare
errors, metadata, connector IPs and connection payloads are discarded. The
normalized output contains only tunnel identity, state, configuration source
and active/inactive timestamps.

The API driver reports whether the connector is healthy at Cloudflare's edge.
It deliberately does not replace Uptime Kuma, which verifies whether each
published application actually responds.

These are compatibility profiles, not generic HTTP providers. A system needing
richer or vendor-specific capabilities still gets dedicated narrow tools.

## ZeroTier Central Legacy provider

The built-in `zerotier` provider uses the fixed official Legacy v1 origin,
`https://api.zerotier.com/api/v1`, with TLS verification always enabled. Its
stable internal driver identity is `zerotier_central_legacy_v1`; the public
provider and tool IDs do not need to change if the lab later migrates to the
new Central v2 API.

Declare only the networks the console may observe:

```yaml
providers:
  zerotier:
    network_ids:
      - 0123456789abcdef
    timeout_seconds: 8
    offline_after_seconds: 600
    # Empty means this is an on-demand VPN and may legitimately be idle.
    required_online_member_ids: []
```

Keep the personal Legacy Central token in the gitignored secrets file:

```yaml
zerotier:
  api_token: EXAMPLE-READ-ONLY-TOKEN
```

The four read tools are `zerotier.status`, `zerotier.networks.list`,
`zerotier.members.list` and `zerotier.summary`. They cannot enumerate networks
outside `network_ids`. Output includes normalized network metadata, member
authorization, freshness and assigned ZeroTier IPs; physical addresses,
account details, authorization headers and raw vendor payloads are discarded.
The `zerotier.members` capability observation drives the dedicated topology
node. Authorized laptops and phones may normally be offline, so their stale
state remains an informational metric. Only IDs explicitly listed in
`required_online_member_ids` affect observation, summary and watcher health;
use this only for genuine always-on anchors. With an empty list, a successful
Central read is healthy even when the network has no online members. A missing
or offline required member degrades the summary, makes the topology capability
unavailable and creates a watcher incident.

## Response normalization

Raw vendor payloads never leave a provider. Each provider follows the
same layout (see `proxmox/` as the reference):

- `models.py` — Pydantic models with `extra="ignore"`; these are the only
  shapes exposed to the frontend and to model providers.
- `normalizers.py` — pure functions converting raw vendor payloads into
  those models, dropping unknown fields and converting annotated strings
  ("12.3 ms", "0.0 %") into plain numbers.
- `tools.py` — the provider class plus tool implementations: call the
  client, normalize, return the normalized shape.

This keeps tool output stable when a vendor adds fields, and prevents
leaking internal detail (paths, coordinates, credential fragments) that
vendor status endpoints like to include.

## Status vocabulary

Every provider reports one of these statuses, plus the timestamp of its
last successful observation (persisted on `ProviderConfiguration`):

| Status | Meaning |
|---|---|
| `healthy` | Reachable and responding normally |
| `degraded` | Reachable but with reduced confidence (partial data, slow, non-fatal errors) |
| `unreachable` | Configured, but the network/endpoint could not be reached |
| `unavailable` | Not usable right now for a reason other than reachability (e.g. not configured in this lab, disabled) |
| `misconfigured` | Configuration present but invalid (bad URL, missing required field) |
| `unknown` | No observation yet, or status could not be determined |

The **last successful observation timestamp** is persisted even while a
provider is currently degraded/unreachable, so the UI and Telegram `/status`
can show "last seen X ago" rather than just a boolean.

## Error taxonomy

Provider calls normalize failures into one of these error kinds before they
reach the execution core's output/redaction stage:

| Error | Meaning |
|---|---|
| `configuration_missing` | Provider has no endpoint/configuration at all (e.g. no entry in `HOMELAB_CONFIG_PATH`) |
| `credentials_missing` | Configuration present but required credential fields are empty |
| `auth_failed` | Credentials present but rejected by the target system |
| `timeout` | Call exceeded the tool's `timeout_seconds` |
| `tls_error` | TLS handshake/certificate validation failed |
| `unreachable` | Network-level failure to reach the target |
| `permission_denied` | Authenticated, but the credential lacks permission for the operation |
| `invalid_response` | Target responded, but the response didn't match the expected shape |
| `degraded` | Call succeeded but with reduced confidence/partial data |

Tools surface these as normalized, redacted error results — never a raw
stack trace or raw provider error body, which could otherwise leak internal
detail or credential fragments.

## Adding a provider — step by step

First decide whether the system implements one of the exact API-ready contracts
above. If it does, add one server-side instance entry and restart. If it needs
another endpoint, authentication flow or output shape, use the dedicated-driver
process below; never loosen an existing driver into an arbitrary HTTP client.
For a new line-oriented TCP protocol, build a dedicated driver on
`BaseTcpTextClient`; keep its command allowlist and parser inside that driver.

1. Create `apps/api/app/providers/<name>/` implementing the interface in
   `base.py` (endpoint setup from `HOMELAB_CONFIG_PATH`, credentials from
   `SECRETS_PATH`, a status/health method, and one method per operation the
   provider will back). Build the client on `httpclient.py` unless the
   target isn't HTTP.
2. Map every failure mode to the [error taxonomy](#error-taxonomy) above —
   don't let a raw exception escape the adapter.
3. Add `models.py` + `normalizers.py` following the
   [normalization layout](#response-normalization) — no tool returns a raw
   vendor payload.
4. Register the provider in `registry.py` under a stable `provider_id`.
5. Add the provider's non-secret endpoint shape to
   `config/homelab.example.yml` and its credential shape to
   `config/secrets.local.example.yml` (placeholder values only — see
   [`security.md`](security.md) on secret handling).
6. Implement a status/health check that reports one of the six
   [status values](#status-vocabulary) and persists the last successful
   observation timestamp.
7. Add tools for it (see next section) — a provider with zero tools is
   inert.

## Adding a tool — step by step

1. Decide the dotted `tool_id`, e.g. `opnsense.firewall.rules.list`.
2. Define Pydantic input and output schemas — reject extra fields on input.
3. Set `mode` (`read`/`write`), `risk` (`low`/`medium`/`high`/`critical`),
   `timeout_seconds`, and `requires_confirmation`.
   - Remember policy this milestone: all **write** tools must ship
     `enabled=false`; `high`/`critical` risk tools also ship
     `enabled=false` until approval flows are explicitly reviewed for them.
     See [`security.md`](security.md#risk-levels-and-tool-policy).
4. Implement the tool's call into the provider method it wraps — a tool is
   a thin, schema-validated wrapper around one provider operation, not a
   place to add ad hoc logic that bypasses the provider abstraction.
5. Register the tool in the tool registry so it's discoverable via both
   REST and MCP (see [`mcp.md`](mcp.md#tool-discovery-and-execution)).
6. Confirm the full pipeline in
   [`architecture.md`](architecture.md#shared-execution-core) applies
   automatically (it does, by construction) — no extra wiring is needed for
   auth, redaction, or audit.

## Proxmox setup

Proxmox is read-only by default. The two LXC power tools designed under
operator-reviewed design record remain disabled
unless their exact ids are operator-activated in `APPROVED_WRITE_TOOLS` after
the required live drill.

### 1. Create a dedicated API token in the Proxmox UI

Do not reuse the `root@pam` password, and do not reuse a token from another
tool.

1. In the Proxmox VE web UI: **Datacenter → Permissions → API Tokens → Add**.
2. Choose (or create) a dedicated user for this integration, e.g.
   `homelab-console@pve` — avoid `root@pam`.
3. Give the token a clear Token ID, e.g. `homelab-console@pve!api`.
4. **Uncheck "Privilege Separation"** only if you understand the tradeoff;
   leaving it checked (default) means the token's permissions are governed
   by the role you grant it below, which is the recommended, least-privilege
   path.
5. Copy the generated token secret immediately — Proxmox shows it once.

### 2. Grant least-privilege access (PVEAuditor)

1. **Datacenter → Permissions → Add → User/Group/API Token Permission**.
2. Assign the built-in **`PVEAuditor`** role (read-only cluster/node/guest
   visibility) to the token, scoped at the level the tools need (typically
   `/` for full read visibility across nodes/guests, or narrower if you want
   to limit which nodes are visible).
3. Do **not** grant `PVEAdmin` or `PVEVMAdmin`. The read tool set
   (`proxmox.version`, `proxmox.cluster.status`,
   `proxmox.topology`, `proxmox.nodes.list`, `proxmox.resources.list`, `proxmox.guests.list`,
   `proxmox.vms.list`, `proxmox.lxc.list`, `proxmox.storage.list`,
   `proxmox.tasks.failed`) needs audit visibility only.
4. If and only if the LXC write drill is being prepared, create a separate
   power token with privilege separation enabled. Add a narrowly scoped custom
   role containing only `VM.Audit` and `VM.PowerMgmt` to both its backing user
   and token on the intended `/vms/<vmid>` paths. Do not grant power management
   at `/` or `/vms` when per-container ACLs are workable. The backing user must
   remain enabled because Proxmox rejects its tokens when it is disabled; omit
   a password to prevent interactive password login.

### 3. Configure the provider

In `config/homelab.local.yml` (gitignored — copy from
`config/homelab.example.yml`), keep the endpoint and connection policy:

```yaml
proxmox:
  base_url: https://EXAMPLE-PVE-HOST:8006
  verify_tls: true
  critical_lxc_vmids: []
```

List DNS, networking, storage and Homelab Console containers under
`critical_lxc_vmids`. A matching `proxmox.lxc.shutdown` request displays a
prominent warning but still requires the normal single-use approval.

In `config/secrets.local.yml` (gitignored — copy from
`config/secrets.local.example.yml`), keep only credentials:

```yaml
proxmox:
  api_token_id: console-reader@pve!homelab-console
  api_token_secret: EXAMPLE-TOKEN-SECRET
  power_api_token_id: console-lxc-power@pve!homelab-console
  power_api_token_secret: EXAMPLE-POWER-TOKEN-SECRET
```

The reader token resolves inventory and performs post-action read-back. The
power token is used only for the narrow LXC start/shutdown request and its task
status. Never replace the reader credentials with the scoped power token.

### 4. `verify_tls` guidance

- **Preferred**: `verify_tls: true`. Use a certificate Proxmox's HTTPS
  endpoint presents that your app trusts (a real CA-issued cert, or your
  internal CA's cert installed in the app's trust store).
- **Trusted LAN/VPN exception**: set `ALLOW_INSECURE_LOCAL_TLS=true` in the
  runtime and `verify_tls: false` only for providers addressed by private IP.
  The app logs a loud warning and still rejects public IPs and public
  hostnames. Prefer installing your internal CA and removing the exception
  when practical.

## Optional providers

Providers without an endpoint in `HOMELAB_CONFIG_PATH` report as
`unavailable`. Providers with an endpoint but missing credentials report as
`misconfigured`. This distinction lets the UI separate "not installed in
this lab" from "configured target but missing secret".

## Uptime Kuma as availability layer

Provider health answers whether the console can use a system API; service
availability is a separate signal. A topology node can bind an exact Kuma
monitor name with `availability_monitor`. The observations API then emits
`uptimekuma.monitor.<node-id>`, and Kuma incidents are attributed to that
declared service node.

Choose one Telegram owner for those monitors:

- console-owned: keep `uptimekuma.monitors` enabled and disable direct Kuma
  notifications for the same monitors;
- Kuma-owned: keep direct Kuma notifications and disable the console
  `uptimekuma.monitors` watcher. The read-only availability projection still
  works.

Running both notification paths for the same monitor intentionally produces
duplicates and is unsupported.
