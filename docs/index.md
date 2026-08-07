---
layout: home

hero:
  name: "Homelab Console"
  text: "The operator's field manual"
  tagline: Build a governed control plane where humans, automation, and AI agents share one narrow, auditable tool surface.
  actions:
    - theme: brand
      text: Start here
      link: /getting-started
    - theme: alt
      text: Understand the architecture
      link: /architecture

features:
  - title: One execution core
    details: REST, Telegram, watchers, and MCP clients all pass through the same validation, policy, redaction, and audit pipeline.
  - title: Bring your own agent
    details: Connect Claude, Codex, OpenCode, or Cline with a per-client identity and a revocable token.
  - title: Humans approve writes
    details: Every infrastructure write is narrow, input-bound, single-use, and explicitly approved by the operator.
  - title: Your stack, normalized
    details: Proxmox, OPNsense, Home Assistant, Frigate, AdGuard, Cloudflare, and more become typed provider observations.
---

## A control plane, not a remote shell

Homelab Console is designed around one constraint: an AI agent should be useful
without becoming an unbounded administrator. The tool catalog contains named,
typed capabilities instead of arbitrary commands, URLs, or provider payloads.

Use this manual to understand the trust boundaries, deploy your own instance,
connect an MCP client, and extend the provider catalog without weakening the
execution contract.

::: tip SOURCE OF TRUTH
These pages are generated from the Markdown documentation maintained beside the
application. Public builds replace private deployment identifiers with examples
and intentionally exclude operator-only runbooks and live milestone records.
:::
