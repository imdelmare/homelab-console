---
layout: home

hero:
  name: "Homelab Console"
  text: "Make a small local model useful"
  tagline: Reduce the problem with deterministic summaries, typed tools, bounded context, and human authority. Keep the reasoning local when the workload allows it.
  actions:
    - theme: brand
      text: Start locally
      link: /getting-started
    - theme: alt
      text: See how it works
      link: /product-tour

features:
  - title: Local 4B, proven on MCP Lite
    details: Qwen 3.5 4B led all three read-only routes in one controlled benchmark and selected the expected tool in 3/3 tests.
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

## Evidence, not model hype

In the first MCP Lite run, local Qwen 3.5 4B completed the three read-only routes
with an 11.9-second mean. DeepSeek V4 Pro averaged 17.3 seconds, Luna Fast 22.9,
and Sol 29.8. All tested models selected the correct tool in 3/3 tests.

The [full benchmark](./benchmark.md) publishes the prompt conditions, individual
route times, observed format issue, ranking rationale, and limits of a
single-execution test. It does not claim that a 4B model is ready for complex
diagnosis or remediation.

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
