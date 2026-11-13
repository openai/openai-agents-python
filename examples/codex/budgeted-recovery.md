# Budgets while waiting and recovering

A `RunBudget` with request or token limits retains its consumed allowance across a supported portable manual or managed function approval checkpoint. Restore requires the application to supply the same limits from current trusted configuration. Token budgets also require the original effective `missing_usage` policy. Estimated-cost limits remain live-only for active turns.

This guide describes the matching source checkout. The [native examples setup](README.md) provides prerequisites. [State and recovery](../../docs/sdk/state.md) provides the other capture restrictions. The approval rows below refer to managed function approvals; native prompts and Realtime interactions retain their own restrictions.

| Operation | Supported call and result | Budget ownership |
| --- | --- | --- |
| Live manual reply | `await request.respond(value)` answers the original request. | Any live `RunBudget` retains its consumed allowance. |
| Live managed approval | `await request.approve()` or `await request.reject(reason)` decides the original request. | Approval runs the callback before the next sampling admission checks the original allowance. |
| Active automatic work | `save_state()` requires idle state; `suspend()` requires a settled pending manual request. | Request and token limits do not add arbitrary running-work capture. Estimated-cost limits reject active portable capture. |
| Portable manual or managed approval suspension | `await session.suspend()` retains the pending turn and accepted sibling results while retiring the original Session. | Request and token budgets save their original limits and consumed allowance. Token budgets also retain missing usage and its policy. Estimated-cost limits reject capture. |
| Budgeted manual restore | `agent.session(state=state, resume_budget=limits)` reopens the existing turn with fresh reply authority. | Current `limits` must match every saved request/token limit. Token budgets also require the same effective missing-usage policy. Prior admissions and token use remain consumed. |
| Old unbudgeted manual restore | `agent.session(state=state)` or `agent.run(None, state=state, on_interaction=handler)` continues the old turn. | Adding a budget remains unsupported. |
| Settled portable reopen | `save_state()` and `agent.session(state=state)` retain conversation history. | A new input starts a new root with its caller's fresh budget. |
| Settled local/custom-store reopen | `agent.session(storage=storage, resume=saved_id)` reopens the same stored conversation. | A new `submit(input, budget=...)` starts fresh. Persistent storage does not export portable state or restore the active budget this way. |

For example, `limits = RunBudget(max_tokens=1000)` selects a token-only allowance, while `limits = RunBudget(max_requests=2, max_tokens=1000)` also limits requests. For one-shot continuation, `agent.run(None, state=state, budget=limits, on_interaction=handler)` supplies the same current limits and policy. A one-shot run without a handler raises `InteractionRequired` with its supported manual checkpoint.

Budgeted checkpoints use outer formats 8 or 9. Request-only runs retain their version 1 budget receipt. Token-limited runs use a nested version 2 receipt, so older readers reject that unknown receipt instead of silently dropping enforcement. Old unbudgeted checkpoints retain their earlier behavior.

Missing or changed current request-only ceilings raise `CapabilityError` with `Restoring a request budget requires the original request-only ceiling`. Token receipt mismatches raise `Restoring a run budget requires the original limits and policy`. Active estimated-cost capture raises `CapabilityError` with `Estimated-cost run budgets do not support portable checkpoints`. Readiness reports `not_ready` for that unsupported capture without consuming a live reply. Restoring an unbudgeted active checkpoint with `budget=` raises `CapabilityError` with `Cannot set a budget when restoring an active checkpoint`.

## Preserve the boundary and the receipt

The request limit counts root sampling admissions, including admitted preparations that send no physical request. Transport retries and child requests do not charge that counter. A saved ceiling of two with one prior admission allows one further admission. An exhausted budget can still be restored. An approved callback or terminal tool can finish without another sample. The next sampling admission fails with `NativeError.code == "run_budget_exceeded"`.

Token budgets retain observed consumption up to the original ceiling; a response can overshoot that ceiling before the next admission check. Usage totals retain the full observed count separately. Missing usage remains sticky across restore. With `missing_usage="stop"`, the next admission raises `NativeError.code == "run_budget_usage_unavailable"` when no limit has already been exhausted. With `missing_usage="continue"`, the run continues against known usage.

The application verifies checkpoint integrity and supplies the current limits and policy independently of the saved JSON. Shape validation does not authenticate saved history or counters. Reopening the same portable checkpoint twice does not claim exclusive ownership or a shared global allowance. The native continuation retains accepted sibling receipts within each restored run. Application idempotency keys and effect records govern uncertain or repeated external effects.

`turn.cancel()` and `session.cancel_all()` stop work and join cleanup. They do not save an arbitrary instruction pointer. Stored history does not restart an interrupted Turn automatically. Python does not offer JavaScript's separate `maxModelSteps` option; that option retains its own supported manual-checkpoint contract.

## Verify the combinations

From the repository root with the matching native build and test dependencies active, this command selects the synthetic budget and process-restart cases:

```sh
python -m pytest packages/codex/tests/test_native_run_budget.py packages/codex/tests/test_native_function_approvals.py packages/codex/tests/test_deferred_root_output.py -k 'budget or fresh_process_restore or manual_capture_drains' -q
```

[The budget suite](../../packages/codex/tests/test_native_run_budget.py) checks live replies, exhausted and remaining allowances after manual/approval restore, trusted-ceiling rejection, one-shot continuation, missing-usage policy, delayed-reader settlement, and fresh settled store budgets. [Deferred continuation](../../packages/codex/tests/test_deferred_root_output.py) checks SDK-owned restarts with the same limits. [The approval suite](../../packages/codex/tests/test_native_function_approvals.py) restores budgeted approvals in a fresh process while retaining accepted siblings. These synthetic fixtures do not establish provider billing or crash recovery from arbitrary automatic work.
