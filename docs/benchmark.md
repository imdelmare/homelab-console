# MCP Lite Benchmark

The MCP Lite benchmark tests the central product claim behind Homelab Console:
a smaller model can be fast and accurate when the control plane first reduces
the problem to compact observations and a narrow tool surface.

## Method

Each model received the same prompt and the same reduced MCP catalog of 12
read-only tools. Each result below is a single execution per test, not an
average over repeated runs. The three routes were:

- **General** — overall homelab summary;
- **Network** — normalized network summary;
- **Uptime Kuma** — monitor status summary.

Latency includes the complete model turn used to select the route and produce
the response. The benchmark records tool selection, expected call count, and
observable output issues.

::: warning EARLY DIRECTIONAL RESULT
One execution per test is enough to compare this controlled run, but not enough
to establish statistical variance. Treat the ranking as directional and
re-run it after model, prompt, hardware, network, or tool-catalog changes.
:::

## Latency

All times are seconds. Lower is better.

| Model | General | Network | Uptime Kuma | Mean |
|---|---:|---:|---:|---:|
| **Qwen 3.5 4B local** | **16.8** | **10.9** | **8.0** | **11.9** |
| DeepSeek V4 Flash | 21.3 | 12.2 | 8.4 | 14.0 |
| DeepSeek V4 Pro | 28.2 | 12.8 | 11.0 | 17.3 |
| GPT-5.6 Luna Fast | 31.3 | 18.2 | 19.2 | 22.9 |
| GPT-5.6 Sol | 36.3 | 31.9 | 21.2 | 29.8 |

Qwen was fastest in all three routes. Based on the displayed means, it was
about 15% faster than DeepSeek Flash, 31% faster than DeepSeek Pro, 48% faster
than Luna Fast, and 60% faster than Sol.

## Tool accuracy

| Model | Correct tools | Expected calls | Observed issues |
|---|---:|---:|---|
| Qwen 3.5 4B local | 3/3 | 3/3 | No substantive issue |
| DeepSeek V4 Flash | 3/3 | 3/3 | Translated the next action and added healthy details |
| DeepSeek V4 Pro | 3/3 | 3/3 | None |
| GPT-5.6 Luna Fast | 3/3 | 3/3 | None |
| GPT-5.6 Sol | 3/3 | 3/3 | None |

Every model selected the correct tool and made the expected number of calls.
The difference in this run was therefore latency and output discipline, not
basic route accuracy.

## Recommended fit

1. **Qwen 3.5 4B local** — best combination of speed, privacy, and accuracy on
   this workload.
2. **DeepSeek V4 Pro** — best balanced commercial alternative when output
   discipline matters more than minimum latency.
3. **GPT-5.6 Luna Fast** — reliable, but materially slower for simple summary
   routing.
4. **GPT-5.6 Sol** — accurate, but oversized for these deterministic summary
   questions.
5. **DeepSeek V4 Flash** — fast, but less strict about preserving the requested
   response format in this run.

## What this proves — and what it does not

The result supports using a local 4B model for MCP Lite read-only questions when
Homelab Console supplies deterministic summaries and routes among 12 tools. It
does not show that the same model is ready for:

- ambiguous incident diagnosis;
- multi-provider causal correlation;
- long-horizon investigation;
- remediation planning;
- infrastructure writes.

Commercial models are still likely to be stronger on those workloads. They
need a separate benchmark with repeated runs, explicit quality criteria, and
operator-reviewed safety evidence. No model should receive write authority:
all infrastructure writes remain typed, input-bound, audited, and approved by
the operator per invocation.

Continue with [Local Models](./local-models.md) for the selection guide or the
[MCP adapter](./mcp.md) for the governed tool contract.
