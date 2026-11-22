# Trace the request Codex sends and the time a tool runs

Codex can change the model input just before sending it. Its normal tool timer can also include time spent waiting for approval. Those details matter when debugging a slow or unexpected turn.

[This patch](01-operation-tracing.patch) lets the SDK observe the final HTTP or WebSocket request, distinguish a failed connection from cancellation, and start timing a command or edit after approval. These callbacks cannot change the request or grant permission to run a tool.

Native spans contain metadata by default. To include text in model and tool spans, pass this configuration to the Runner.

```python
from agents import RunConfig
from agents.models.codex import CodexProvider

config = RunConfig(
    model_provider=CodexProvider(),
    tracing={"operation_detail": "content"},
    trace_include_sensitive_data=True,
)
```

The SDK uses existing Codex events for model responses, usage and tool results. It excludes request headers, encrypted reasoning, media and values over 32 KiB. Captured text can still contain secrets. Setting `trace_include_sensitive_data=False` always disables content capture.

## Check and remove

Build and test the wheel as described in the [patch guide](README.md). Check both metadata and content modes. Failed requests and cancellations should have distinct outcomes. Denied commands should have no execution span. Remove the patch and guide once upstream Codex exposes the same events.
