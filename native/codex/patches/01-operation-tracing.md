# Trace the request Codex sends and the time a tool runs

Codex can change the model input just before sending it. Its normal tool timer can also include time spent waiting for approval. Those details matter when debugging a slow or unexpected turn.

The SDK has been using [three optional Codex callbacks](01-operation-tracing.patch) to observe operations without changing requests or approval decisions.

| Callback | Observation |
| --- | --- |
| `on_request` | Borrowed full request, exact submitted input and previous response ID, including WebSocket deltas |
| `on_request_result` | Stream setup errors and total HTTP attempts, including the first send. No HTTP count for WebSockets. |
| `on_tool_execution_start` | A native execution attempt after approval and network admission |

The existing stream interceptor has been reporting completions and later errors. Requests that fail before preparation and WebSocket warmups without a contributor fall outside these callbacks.

Native tool observations have been capturing metadata by default. To include text in model and tool spans, pass this configuration to the Runner.

```python
from agents import RunConfig
from agents.models.codex import CodexProvider

config = RunConfig(
    model_provider=CodexProvider(),
    tracing={"operation_detail": "content"},
    trace_include_sensitive_data=True,
)
```

The SDK has been using existing Codex events for model responses, usage, and tool outcomes. Native tool results have been coming from the output Codex accepts after hooks. Content capture has been excluding request headers, encrypted reasoning, and binary media, with separate 32 KiB input and output limits. Captured text can still contain secrets. Setting `trace_include_sensitive_data=False` always disables content capture.

## Apply and verify

The [wheel build](README.md#test-a-change) has been applying both patches to a temporary copy of the locked Codex source. After installing that wheel in `.venv-wheel`, use the following commands from the SDK root on Linux or macOS.

```sh
uv pip install --python .venv-wheel/bin/python "pytest>=9.0.3" pytest-asyncio "opentelemetry-sdk>=1.45.0,<2"
CODEX_TRACING_NATIVE_TEST=1 .venv-wheel/bin/python -I -m pytest \
  --import-mode=importlib tests/models/test_operation_tracing_parity.py
```

The installed-wheel tests have been checking one model span per logical request, one event per observed retry and one span per tool execution across both providers and all capture settings. Binding tests have been checking interruption, denied execution, continuation input and request identity.

## Upstream and retire

To prepare a Codex PR, apply only the tracing patch in a clean checkout at the SDK pin. Set `CODEX_SOURCE` to the checkout path and run from the SDK root.

```sh
patch="$PWD/native/codex/patches/01-operation-tracing.patch"
git -C "$CODEX_SOURCE" apply --check "$patch"
git -C "$CODEX_SOURCE" apply "$patch"
git -C "$CODEX_SOURCE" diff --check
```

Include regression coverage for HTTP attempts, WebSocket continuations and denied tools in the upstream PR. Keep SDK policy and SDK tests here. To refresh the patch, export the reviewed source changes with `git diff` against the pin, then rerun the binding tests and installed-wheel checks.

After advancing the pin to a release with equivalent callbacks, remove this patch and its guide together. Keep approval recovery independent and repeat the same checks.
