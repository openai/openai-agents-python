# Decisions API recipes

These examples compose the native `AsyncOpenAI.decisions.create()` client with existing Agents SDK APIs. They do not add a Decisions model adapter or a generic risk policy.

From the repository root, set `OPENAI_API_KEY` in your environment and run:

```bash
uv run --with 'openai>=3.26.0,<4' python -m examples.decisions.classify
uv run --with 'openai>=3.26.0,<4' python -m examples.decisions.model_router
uv run --with 'openai>=3.26.0,<4' python -m examples.decisions.tool_risk_gate
uv run --with 'openai>=3.26.0,<4' python -m examples.decisions.tool_risk_gate --require-approval
```

The dependency override supplies the native Decisions resource without modifying the repository lockfile. These commands make paid API calls. The publication tool is a simulation; it only returns text.

The repository example runner (`make examples-run`) applies the same dependency override when it launches these recipes.

- `classify.py` asks predicate, choice, and score questions, indexes answers by name, and uses the predicate in a blocking input guardrail. A refusal or missing scope answer blocks the run. Other questions may refuse independently.
- `model_router.py` classifies a single text request before the run and passes the selected model to `agent.clone()`. A refusal or missing route uses the application's `powerful` fallback. API errors propagate.
- `tool_risk_gate.py` applies an illustrative editorial policy to announcement arguments. The default mode rejects content that needs review before the tool runs. `--require-approval` instead pauses for a human decision; it does not automatically approve interruptions. Refusals require review, and API errors stop the attempted operation.

Choose thresholds using labeled data from your application. A classifier is not an authorization mechanism. Enforce actual permissions in trusted application code.

The tool recipe evaluates only the announcement arguments, not conversation history, attachments, or user authorization. Conditional approval callbacks are skipped when decorated tool arguments require conversion or defaults; the SDK requires manual approval instead. Resuming an explicitly approved call does not re-run the conditional approval callback. Tool input guardrails can run again before invocation, including after an approval interruption.

These recipes accept text rather than implementing general conversation or media conversion. Pre-run routing does not re-route handoffs or resumed runs and cannot inspect server-held conversation history. See the [Decisions guide](https://developers.openai.com/api/docs/guides/decisions) for the native API's input formats and [SDK model documentation](https://openai.github.io/openai-agents-python/models/) for integration guidance.
