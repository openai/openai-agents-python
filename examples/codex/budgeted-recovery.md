# Budgets while waiting and recovering

A direct native run's `RunBudget` has been following its live root through manual replies, managed function approvals, steering, and output repair. Portable checkpoints have not been carrying this budget across a process restart. A settled conversation has been accepting a new input with a fresh budget.

This guide has been describing the matching source checkout. The [native examples setup](README.md) has been providing installation and build prerequisites. [State and recovery](../../docs/sdk/state.md) has been providing the full capture and storage restrictions.

## Choose the continuation

The approval rows have been referring to managed function approvals. Native approval prompts and Realtime interactions have been retaining their own capture restrictions.

| Operation | Without `RunBudget` | With `RunBudget` | Preserved state and next allowance |
| --- | --- | --- | --- |
| Waiting for a manual result in the original Session | The pending request has been accepting `request.respond(value)`. | The same request has been accepting a reply. | The original Turn and its consumed budget have been remaining live. |
| Waiting for managed function approval | The request has been accepting `approve()` or `reject(reason)`. | The same request has been accepting a decision. | Approval has been running the current callback once; subsequent sampling has been using the original budget. |
| Capturing an active automatic turn | `save_state()` has been requiring idle state; `suspend()` has been requiring a settled pending manual request. | Active capture has been rejecting with the budget error below. | Running callbacks and arbitrary execution have not been becoming portable state. |
| Suspending a supported manual or managed approval request | `await session.suspend()` has been sealing the pending turn. | `suspend()` has been rejecting with the budget error below. | Successful suspension has been preserving accepted history and pending replies while retiring the original Session. |
| Restoring a supported pending portable checkpoint | `agent.run(None, state=state, on_interaction=handler)` has been resuming its existing turn. | Adding `budget=` has been rejecting with the restore error below. | Current matching declarations have been supplying fresh reply authority; no `RunBudget` has been coming from the checkpoint. |
| Saving and reopening settled portable state | `await session.save_state()` has been exporting history for `agent.session(state=state)`. | Idle capture has been remaining available after completion. | A new non-`None` input has been starting a fresh root; its caller has been supplying any new budget. |
| Reopening settled `LocalStorage` or `CustomStorage` history | `agent.session(storage=storage, resume=saved_id)` has been reopening the same conversation. | A new `submit(input, budget=...)` has been starting fresh. | The backend has been preserving committed history and Session identity, without restoring a live Turn's budget. |
| Exporting portable state from a storage-backed Session | `save_state()` and `suspend()` have been rejecting persistent storage. | The same storage restriction has been applying, alongside active-budget restrictions. | The backend and Session ID have been supplying the reopen route. |

`checkpoint_status(kind="manual_wait")` has been reporting `not_ready` for a budgeted active turn. This probe has not been reserving a checkpoint or consuming the live reply.

The active-budget `CapabilityError` has been carrying `Cannot checkpoint an active run with a budget. Reply in the live Session.` A restored active checkpoint with `budget=` has been raising `CapabilityError` with `Cannot set a budget when restoring an active checkpoint`.

The persistent-storage `CapabilityError` has been carrying `Persistent storage cannot export portable SessionState; retain Session.id and reopen with the same storage backend`. An active budget can trigger its refusal first.

A one-shot budgeted run with reachable manual tools and no `on_interaction` handler has been rejecting before inference with `CapabilityError`. An explicit Session or an interaction handler has been providing the live reply route. JavaScript has been discovering this one-shot failure at the requested interaction instead; its separate `maxModelSteps` has been retaining remaining steps at supported manual checkpoints. Python has not been offering that option.

## Distinguish waiting from stopping

A pending manual or approval request has been keeping its original Turn live. `turn.cancel()` and `session.cancel_all()` have been stopping work and joining owned cleanup, without preserving an arbitrary resumable instruction pointer. A later submission has been starting a new root. Reopening stored history has not been continuing an interrupted Turn automatically.

A `RunBudget(max_requests=1)` has been permitting the model's first admission and a subsequent approved callback. The next sampling admission has been failing with `NativeError.code == "run_budget_exceeded"`. The approved effect has been remaining accepted. Token and cost limits have likewise been checking before the next admission; an admitted completion has been remaining able to overshoot and finish. Application effect receipts have been supplying evidence for uncertain external work before any retry.

## Verify the combinations

The focused native suite has been exercising live manual replies, approval budgets of one and two admissions, rejected capture, portable idle restore, and settled local/custom-store reopen. From the repository root, with the matching native build and test dependencies active, this command has been selecting those synthetic loopback cases:

```sh
python -m pytest packages/codex/tests/test_native_run_budget.py -q
```

[The budget suite](../../packages/codex/tests/test_native_run_budget.py) has been asserting request counts and accepted callback effects. [The approval suite](../../packages/codex/tests/test_native_function_approvals.py) has been covering unbudgeted fresh-process approval restore and accepted siblings. [The local storage suite](../../packages/codex/tests/test_native_local_storage.py) has been covering process restart and uncertain effects. These fixtures have not been establishing live-provider billing or same-turn budget recovery. The automatic-turn and interruption rows have been reflecting source contracts and neighboring lifecycle tests, rather than a full budget-by-storage interruption matrix.
