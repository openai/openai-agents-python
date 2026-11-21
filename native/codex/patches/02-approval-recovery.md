# Resume a built-in command or edit after a worker stops

Codex normally keeps a pending approval in memory. When the process stops, restoring conversation history cannot finish that action. Asking the model to propose it again could run a different command or repeat a write.

[This patch](02-approval-recovery.patch) lets the SDK save one local `exec_command` or `apply_patch` before it runs. A replacement worker can submit that call directly to Codex without asking the model again.

1. Create a session with `session = await ResumableSession.create(store)`. Pass it to `Runner.run(..., session=session)` and keep `session.session_id`. The SDK saves the pending call before returning the approval request.
2. On the replacement worker, load the same session with `session = await ResumableSession.resume(store, session_id)`. Then use `state = await session.load_state(agent)`.
3. Match the user's decision to the call in `state.get_interruptions()`. Use `state.approve(call)` or `state.reject(call)`, then `await Runner.run(agent, state, session=session, run_config=config)`.

Only one worker can claim that session revision. Denial stops the call. On approval, Codex checks the workspace, permissions and newly prepared action. A mismatch stops execution.

## Safety limits

Recovery requires direct tools in one local environment, human review and no tool hooks. The SDK saves the model's submitted command or patch, so protect the session store. Codex replaces prepared update diffs with hashes and removes the original contents of deleted files.

The patch includes a bounded file read because the existing stream can follow symbolic links. Before saving and again before execution, Codex checks file hashes and confirms that new destinations are absent. Each check covers at most 32 paths, 8 MiB per file, 16 MiB in total and 10 seconds. An unreadable or oversized file stops recovery.

A restored call gets one attempt. When Codex policy already allows an approved edit to run outside the sandbox on retry, recovery uses that permission on the first attempt.

The application must restore the same workspace and prevent concurrent edits. If the worker stops after a write and before saving the result, the session stays active. Check the external system before retrying.

## Check and remove

Follow the [patch guide](README.md) and run the approval tests. On another worker, approve and deny a saved command and edit. Check that a stale file, changed permissions or a duplicate approval causes no extra write. Remove the patch when upstream Codex can resume the same action with these checks.
