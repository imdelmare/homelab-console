# Local Models

Homelab Console is designed to make a small local model useful before asking it
to be powerful. The control plane reduces the problem into compact summaries,
strict tool choices, and validated outputs. Credentials, policy, task state, and
write authority stay outside the model.

## Start with the smallest model that works

For read-only status questions, a local 4B model can be the best default: fast,
private, inexpensive, and close to the infrastructure. The
[MCP Lite benchmark](./benchmark.md) measured Qwen 3.5 4B as the fastest model in
all three tested summary routes while still selecting the expected tool in 3/3
tests.

That result is not a claim that a 4B model is universally better. It shows that
smaller models can perform well when the control plane gives them a deliberately
small problem.

| Workload | Suggested starting point | Why |
|---|---|---|
| Deterministic summaries and status checks | Local 4B model | Lowest latency, private execution, narrow decision space |
| Richer explanations or mixed operational questions | Balanced commercial model | More headroom for synthesis without using the largest model |
| Complex diagnosis and cross-system correlation | Larger commercial model | Better fit for ambiguous evidence and longer reasoning chains |
| Infrastructure writes | Governed tool client plus operator approval | Model size never replaces validation, approval, or read-back |

## What makes local inference practical

Homelab Console does not expose a raw provider API or a large tool catalog and
hope the model chooses well. It supplies:

- deterministic `lab.*.summary` observations;
- a reduced MCP surface for the current task;
- strict schemas for model decisions and tool inputs;
- bounded conversation and task context;
- centralized validation, redaction, and audit;
- a separate human approval boundary for every infrastructure write.

The model remains replaceable. The useful system knowledge lives in the control
plane.

## Current local paths

- **Ollama** provides a fixed local chat endpoint for private inference.
- **LAN AI manager** uses one inventory-declared OpenAI-compatible host on the
  trusted network and can fall back to a configured commercial model.
- **MCP clients** can use the same governed tools from an external compatible
  agent without receiving provider credentials.

You can run the supported Community stack with every AI runtime disabled, then
connect a model after the control plane and providers are healthy.

## Know when to step up

Use a stronger model when the question requires uncertain causal reasoning,
cross-provider correlation, or a remediation plan that must account for several
failure modes. Keep write tools behind the same approval path regardless of the
model selected.

The current benchmark covers read-only summary routing only. A separate,
repeatable benchmark is required before treating a local 4B model as validated
for complex diagnosis or write-oriented workflows.

For schemas, channel behavior, fallbacks, limits, and migration details, see the
[Conversation Service reference](./conversation.md).
