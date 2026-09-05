# Realtime session test groups

Run a behavior group directly from the repository root:

```bash
uv run pytest tests/realtime/test_session_approvals.py
uv run pytest tests/realtime/test_session_tool_outputs.py
uv run pytest tests/realtime/test_session_guardrails.py
uv run pytest tests/realtime/test_session_history.py
uv run pytest tests/realtime
```

File selection avoids importing or collecting the other session groups. It is a navigation and test-selection boundary; splitting files does not by itself imply a faster full suite. Existing `-k` filters and class/function selection still work within each new file.

| Module | Responsibility |
| --- | --- |
| [test_session.py](test_session.py) | Session entry/exit, event forwarding, tool dispatch and timeouts, handoffs, model settings, and update-agent behavior. |
| [test_session_approvals.py](test_session_approvals.py) | Function-tool approval requests, sticky decisions, rejection formatting, and pre/post-approval input guardrails. |
| [test_session_tool_outputs.py](test_session_tool_outputs.py) | Function-tool output serialization, send failures, and retries without repeating execution. |
| [test_session_guardrails.py](test_session_guardrails.py) | Response-scoped output guardrails, feedback ordering, and audio interruption. |
| [test_session_history.py](test_session_history.py) | Item insertion/update/deletion, transcript merging, and transcript preservation. |

## Fixture and helper ownership

`session_test_support.py` owns the existing `_DummyModel`, `RecordingRealtimeModel`, and shared function-tool helpers. Each model instance remains constructed per test. The session modules explicitly bind only the fixtures they use from that support module: `mock_agent`, `mock_model`, and, for tool-related modules, `mock_function_tool`. These fixtures retain pytest's default function scope and are not autouse. There is no Realtime-wide `conftest.py` introducing them to unrelated test modules.

`TestGuardrailFunctionality` keeps its function-scoped `triggered_guardrail` and `safe_guardrail` fixtures and `_wait_for_guardrail_tasks` helper. Specialized blocking/failing model subclasses stay inside their original test functions. `_FakeAudio` belongs to history tests; `TestToolCallExecution.ToolResult` belongs to output serialization tests. The original file retains its connection/enablement/traceback helpers and `mock_handoff` fixture. Repository-wide tracing, fake API credentials, and cleanup remain owned by `tests/conftest.py`.

## Relocation map

The source for every relocation below is `test_session.py` at `3e0e89374f629c974929054e56e823a43c91a013`. Class names, function names, and every bracketed parameter ID are unchanged. Replace only the file prefix of an old pytest node ID. Any node not listed below remains in `test_session.py`, including lifecycle tests that exercise history suppression after close or background guardrail cleanup.

For example:

```text
tests/realtime/test_session.py::TestToolCallExecution::test_serialize_tool_output_edge_cases[dataclass]
    -> tests/realtime/test_session_tool_outputs.py::TestToolCallExecution::test_serialize_tool_output_edge_cases[dataclass]
```

Whole classes move as follows:

| Original class | Destination |
| --- | --- |
| `TestHistoryManagement` | `test_session_history.py` |
| `TestTranscriptPreservation` | `test_session_history.py` |
| `TestGuardrailFunctionality` | `test_session_guardrails.py` |

The remaining relocations below retain their original containing class, where shown. All other methods of the partial classes remain in `test_session.py`.

### test_session_approvals.py

- `TestToolCallExecution::test_approval_resume_uses_pending_initial_settings_dispatch_snapshot`
- `TestToolCallExecution::test_function_tool_needs_approval_emits_event`
- `TestToolCallExecution::test_callable_function_approval_fails_closed_for_invalid_arguments`
- `TestToolCallExecution::test_callable_function_approval_receives_valid_object_arguments`
- `TestToolCallExecution::test_tool_input_guardrail_rejects_before_realtime_function_execution`
- `TestToolCallExecution::test_realtime_pending_approval_skips_tool_input_guardrails_by_default`
- `TestToolCallExecution::test_realtime_pre_approval_tool_input_guardrail_rejects_pending_approval`
- `TestToolCallExecution::test_realtime_pre_approval_tool_input_guardrails_rerun_after_approval`
- `TestToolCallExecution::test_duplicate_pending_approval_call_id_is_ignored_and_approval_runs_once`
- `TestToolCallExecution::test_approve_pending_tool_call_runs_tool`
- `TestToolCallExecution::test_async_approve_pending_tool_call_reserves_call_id_before_task_runs`
- `TestToolCallExecution::test_always_approve_namespaced_tool_call_does_not_approve_bare_tool`
- `TestToolCallExecution::test_reject_pending_tool_call_sends_rejection_output`
- `TestToolCallExecution::test_reject_pending_tool_call_reserves_call_id_before_sending`
- `TestToolCallExecution::test_reject_pending_tool_call_uses_run_level_formatter`
- `TestToolCallExecution::test_rejection_formatter_error_is_redacted`
- `TestToolCallExecution::test_cancelled_rejection_formatter_leaves_invocation_executed`
- `TestToolCallExecution::test_reject_pending_tool_call_prefers_explicit_message`
- `TestToolCallExecution::test_always_reject_namespaced_tool_call_reuses_explicit_message`
- `TestToolCallExecution::test_sticky_rejection_does_not_bind_duplicate_call_id_payload`
- `TestToolCallExecution::test_sticky_rejection_skips_dynamic_approval_checker`
- `TestToolCallExecution::test_sticky_rejection_wins_while_dynamic_approval_checker_is_pending`
- `TestToolCallExecution::test_sticky_decision_wins_while_rejecting_pre_approval_guardrail_is_pending`

### test_session_tool_outputs.py

- `TestToolCallExecution::test_approved_function_tool_failure_replay_does_not_rerun`
- `TestToolCallExecution::test_function_tool_send_failure_retries_cached_output_without_rerun`
- `TestToolCallExecution::test_tool_end_cancellation_after_output_send_does_not_resend`
- `TestToolCallExecution::test_async_function_tool_send_failure_retries_cached_output_without_rerun`
- `TestToolCallExecution::test_pending_function_output_rejects_handoff_role_reuse`
- `TestToolCallExecution::test_async_exact_function_retry_after_serialization_failure_does_not_repeat_callback`
- `TestToolCallExecution::test_tool_result_conversion_to_string`
- `TestToolCallExecution::test_tool_result_conversion_serializes_pydantic_models`
- `TestToolCallExecution::test_serialize_tool_output_ignores_non_pydantic_model_dump_objects`
- `TestToolCallExecution::test_serialize_tool_output_falls_back_when_pydantic_json_dump_fails`
- `TestToolCallExecution::test_serialize_tool_output_returns_string_when_pydantic_dump_fails`
- `TestToolCallExecution::test_serialize_tool_output_returns_string_when_dataclass_asdict_fails`
- `TestToolCallExecution::test_serialize_tool_output_edge_cases`

### test_session_history.py

- `test_transcription_completed_adds_new_user_item`
- `test_item_updated_merge_exception_path_logs_error`
- `TestEventHandling::test_transcription_completed_event_updates_history`
- `TestEventHandling::test_item_updated_event_adds_new_item`
- `TestEventHandling::test_item_updated_event_updates_existing_item`
- `TestEventHandling::test_item_updated_event_completes_tool_call`
- `TestEventHandling::test_item_deleted_event_removes_item`

The relocation preserves all 198 collected session cases: 95 in `test_session.py`, 30 in `test_session_approvals.py`, 22 in `test_session_tool_outputs.py`, 29 in `test_session_guardrails.py`, 22 in `test_session_history.py`. These counts describe the relocation baseline, not a requirement for future test additions.
