# Budgets while waiting and recovering

A request-only `RunBudget(max_requests=N)` has been retaining its prior admissions across a supported portable manual or managed function approval checkpoint. Restore has been requiring the application to supply that same request ceiling from current trusted configuration. Token and estimated-cost limits have been remaining live-only for active turns.

This guide has been describing the matching source checkout. The [native examples setup](README.md) has been providing prerequisites. [State and recovery](../../docs/sdk/state.md) has been providing the other capture restrictions. The approval rows below have been referring to managed function approvals; native prompts and Realtime interactions have been retaining their own restrictions.

| Operation | Supported call and result | Budget ownership |
| --- | --- | --- |
| Live manual reply | `await request.respond(value)` has been answering the original request. | Any live `RunBudget` has been retaining its consumed allowance. |
| Live managed approval | `await request.approve()` or `await request.reject(reason)` has been deciding the original request. | Approval has been running the callback before the next sampling admission checks the original allowance. |
| Active automatic work | `save_state()` has been requiring idle state; `suspend()` has been requiring a settled pending manual request. | Request-only limits have not been adding arbitrary running-work capture. Token/cost limits have been rejecting active portable capture. |
| Portable manual or managed approval suspension | `await session.suspend()` has been retaining the pending turn and accepted sibling results while retiring the original Session. | A request-only budget has been saving its original ceiling and prior admission count. Token/cost limits have been rejecting capture. |
| Request-budget manual restore | `agent.session(state=state, resume_budget=RunBudget(max_requests=N))` has been reopening the existing turn with fresh reply authority. | Current `N` has been needing to match the saved ceiling exactly; prior admissions have been remaining consumed. |
| Old unbudgeted manual restore | `agent.session(state=state)` or `agent.run(None, state=state, on_interaction=handler)` has been continuing the old turn. | Adding a budget has been remaining unsupported. |
| Settled portable reopen | `save_state()` and `agent.session(state=state)` have been retaining conversation history. | A new input has been starting a new root with its caller's fresh budget. |
| Settled local/custom-store reopen | `agent.session(storage=storage, resume=saved_id)` has been reopening the same stored conversation. | A new `submit(input, budget=...)` has been starting fresh. Persistent storage has not been exporting portable state or restoring the active budget this way. |

For one-shot request-only continuation, `agent.run(None, state=state, budget=RunBudget(max_requests=N), on_interaction=handler)` has been supplying the same current ceiling. A request-only one-shot run without a handler has been raising `InteractionRequired` with its supported manual checkpoint. New budgeted checkpoints have been using outer formats 8 or 9, so earlier SDK readers have been rejecting them instead of silently dropping enforcement. Old unbudgeted checkpoints have been retaining their earlier behavior.

Missing or changed current ceilings have been raising `CapabilityError` with `Restoring a request budget requires the original request-only ceiling`. Active token/cost capture has been raising `CapabilityError` with `Only request-only run budgets support portable checkpoints`. Readiness has been reporting `not_ready` for that unsupported capture without consuming a live reply. A cold unbudgeted checkpoint with `budget=` has been retaining `Cannot set a budget when restoring an active checkpoint`.

## Preserve the boundary and the receipt

The request limit has been counting root sampling admissions, including admitted preparations that send no physical request. Transport retries and child requests have not been charging that counter. A saved ceiling of two with one prior admission has been allowing one further admission. A saved exhausted ceiling has been remaining restorable; an approved callback or terminal tool has been remaining able to finish without another sample. The next sampling admission has been failing with `NativeError.code == "run_budget_exceeded"`.

The application has been verifying checkpoint integrity and supplying the current ceiling independently of the saved JSON. Shape validation has not been authenticating saved history or counters. Reopening the same portable checkpoint twice has not been claiming exclusive ownership or a shared global allowance. The native continuation has been retaining accepted sibling receipts within each restored run; application idempotency keys and effect records have been governing uncertain or repeated external effects.

`turn.cancel()` and `session.cancel_all()` have been stopping work and joining cleanup. They have not been saving an arbitrary instruction pointer. Stored history has not been restarting an interrupted Turn automatically. Python has not been offering JavaScript's separate `maxModelSteps` option; that option has been retaining its own supported manual-checkpoint contract.

## Verify the combinations

From the repository root with the matching native build and test dependencies active, this command has been selecting the synthetic budget and process-restart cases:

```sh
python -m pytest packages/codex/tests/test_native_run_budget.py packages/codex/tests/test_native_function_approvals.py -k 'run_budget or fresh_process_restore' -q
```

[The budget suite](../../packages/codex/tests/test_native_run_budget.py) has been checking live replies, exhausted and remaining allowances after manual/approval restore, trusted-ceiling rejection, one-shot continuation, and fresh settled store budgets. [The approval suite](../../packages/codex/tests/test_native_function_approvals.py) has been restoring budgeted approvals in a fresh process while retaining accepted siblings. These synthetic fixtures have not been establishing provider billing or crash recovery from arbitrary automatic work.
