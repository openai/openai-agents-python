# OpenAI Runtime Patterns

Use this reference for recurring OpenAI investigations so you do not have to rediscover the probe strategy each time. In this repository, use [$openai-knowledge](../../openai-knowledge/SKILL.md) up front for contract-sensitive details, then use this reference to design the runtime validation. If the docs MCP is unavailable, fall back to the official OpenAI docs and say so in the report.

## General Rules

- Prefer small live probes over large harnesses.
- Keep one script focused on one uncertainty.
- For comparative or benchmark-like questions, start with a pilot and expand only when the answer is still unclear.
- Capture both the request shape and the returned item types.
- Preserve raw error payloads and status codes.
- Record whether behavior differs between the first call and a repeated call.
- When the question is about regression or contract drift, add a known-good control run before attributing the result to the change under investigation.
- Keep comparison parity explicit. Record what was held constant, what variable changed, and whether output-shape or usage differences could bias the conclusion.
- When the question depends on tool invocation, force the target path with the matching `tool_choice`.
- Treat `container_auto` and `container_reference` as distinct setup modes, not interchangeable details.
- Clear unsupported model or tool options before diagnosing runtime behavior.

## Standard Environment Variables

Do not read these variables automatically. Before a live probe uses any of them, tell the user the exact variable names you plan to read and why each one is needed, then wait for explicit approval. Never print their values:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_ORG_ID`
- `OPENAI_PROJECT_ID`

If the task targets another standard integration, use that integration's expected default variable names under the same rule.

## Responses API Probe Patterns

For Responses API work, start from the uncertainty instead of from the full feature surface.

### Benchmark or model-switch comparisons

Use when you need to compare models, settings, transports, or providers with enough rigor to support a product or release decision.

Probe suggestions:

- Start with a pilot that includes one control and two or three highest-signal scenarios.
- Keep prompt shape, tool choice, state setup, and non-tested settings aligned across candidates.
- If the question is about speed, capture medians and, when relevant, first-token latency plus any usage note that could explain the difference.
- If the question is about "same intelligence" or "same quality," add at least one harder or more open-ended case. Otherwise report the result as pattern parity only.
- Expand to a larger matrix only when the pilot survives, the candidates are close, or a major runtime surface is still uncovered.

### Plain response behavior

Use when you need to confirm:

- The shape of returned output items.
- Whether text appears in one item or multiple items.
- How metadata appears in the final object.

Probe suggestions:

- Baseline call with a minimal input.
- Same call with a slightly different instruction shape.
- Repeat the same call to check output stability where that matters.

### Structured output behavior

Use when you need to observe:

- Schema rejection versus best-effort completion.
- Handling of missing required fields.
- Differences between model-compliant output and transport-level errors.

Probe suggestions:

- Valid schema and valid prompt.
- Prompt likely to produce omitted fields.
- Clearly incompatible schema or unsupported option when relevant.

### Tool invocation behavior

Use when you need to learn:

- When tool calls are emitted.
- How arguments are shaped at runtime.
- What happens when the tool fails or returns malformed output.

Probe suggestions:

- Baseline tool-call success.
- Tool failure with a realistic exception.
- Tool result that is syntactically valid but semantically incomplete.

### Hosted shell and code interpreter failure shields

When probing hosted tools through the Responses API, eliminate common setup ambiguity first:

- Force the tool path you want to test with the matching `tool_choice`. A text-only completion without forced tool choice is not a reliable negative result.
- Treat `container_auto` and `container_reference` differently. Use `container_auto` when the probe needs fresh container provisioning or skill attachment, and use `container_reference` only to reuse existing container state.
- Do not assume every environment field is accepted on every container mode. If the probe is about skills, validate that the chosen container mode actually supports skill attachment before treating an API error as a runtime defect.
- Check model-specific option support before chasing unrelated failures. Unsupported reasoning or model settings can invalidate the probe before the tool path is exercised.
- For hosted package installation, treat network-dependent setup as best-effort and separate install failures from the underlying tool behavior you are trying to observe.
- For prompt cache investigations, keep model, instructions, tool configuration, and cache key effectively identical across repeated runs before interpreting `cached_tokens`.

### Streaming behavior

Use when the uncertainty involves:

- Event ordering.
- Partial text delivery.
- Termination after interruption.
- Tool-call events in streams.

Probe suggestions:

- Normal streamed completion.
- Early local cancellation.
- Network interruption if it can be reproduced safely.

## What to Capture

For OpenAI probes, try to record:

- Request options that materially affect behavior.
- Response item types and their order.
- Whether fields are absent, null, empty, or transformed.
- Server status and error payload details for failures.
- Retry and backoff hints when present.
- Stable identifiers that help compare repeated runs, such as request IDs, response IDs, tool call IDs, or container IDs when available.
- Which environment-variable names were approved for the probe when live credentials were required.

Do not spend time rediscovering static documentation unless the runtime result seems to contradict what you expected. The value of this skill is in the observed behavior.
