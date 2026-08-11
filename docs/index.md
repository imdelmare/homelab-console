---
layout: home

hero:
  name: "Homelab Console"
  text: "Run your homelab with a small local AI"
  tagline: Private, local reasoning over typed tools, bounded context, operator approvals, and one auditable execution core. No cloud AI required.
  actions:
    - theme: brand
      text: Start locally
      link: /getting-started
    - theme: alt
      text: See how it works
      link: /product-tour

features:
  - title: Local-first reasoning
    details: Use Ollama or an inventory-bound OpenAI-compatible model on your private LAN. Cloud models are optional.
  - title: Built for smaller models
    details: Compact observations, strict schemas, persistent tasks, and authored runbooks reduce the reasoning burden.
  - title: No direct authority
    details: Models never receive provider credentials. Every infrastructure call passes through validation, policy, redaction, and audit.
  - title: Humans approve writes
    details: Every infrastructure write is narrow, input-bound, single-use, and explicitly approved by the operator.
---

## Better tools, not more privileges

A lightweight model should not need SSH access or an enormous context window to
answer an operational question. Homelab Console turns different providers into
predictable typed observations, preserves investigation state outside the chat,
and validates every decision before execution.

The model remains replaceable. Infrastructure knowledge, credentials, task
state, approvals, and authority remain in the control plane.

## From a local question to an audited action

Follow an incident from the Quiet Operations Inbox through bounded evidence
collection, an input-bound approval, and the final Activity record in the
[product tour](./product-tour.md).

When you need another interface, MCP remains available for authenticated Claude,
Codex, OpenCode, Cline, and other compatible clients. They use the same tools and
policy as local reasoning, Telegram, REST, and watchers.

::: tip SOURCE OF TRUTH
These pages are generated from the Markdown documentation maintained beside the
application. Public builds replace private deployment identifiers with examples
and intentionally exclude operator-only runbooks and live milestone records.
:::
