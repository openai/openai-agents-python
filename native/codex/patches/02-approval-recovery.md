# Resume a built-in command or edit after a worker stops

Codex normally keeps a pending approval in memory. When the process stops, restoring conversation history cannot finish that action. Asking the model to propose it again could run a different command or repeat a write.

[This patch](02-approval-recovery.patch) lets the SDK save one local `exec_command` or `apply_patch` before it runs. A replacement worker can submit that call directly to Codex without asking the model again.

1. Create a session with `session = await ResumableSession.create(store)`. Pass it to `Runner.run(..., session=session)` and keep `session.session_id`. The SDK saves the pending call before returning the approval request.
2. On the replacement worker, load the same session with `session = await ResumableSession.resume(store, session_id)`. Then use `state = await session.load_state(agent)`.
3. Match the user's decision to the call in `state.get_interruptions()`. Use `state.approve(call)` or `state.reject(call)`, then `await Runner.run(agent, state, session=session, run_config=config)`.

Only one worker can claim that session revision. Denial stops the call.

## Which checks need a Codex patch?

| Where | Responsibility |
| --- | --- |
| Existing Codex APIs | Start a turn on an idle thread, pass data to that turn, and run the built-in tool through its usual permission checks. |
| This patch | Identify the first live approval and check which hooks can run. On restart, dispatch the saved call and let the SDK stop each attempt before execution. |
| [SDK approval checks](../src/native_approval.rs) | Compare the new request and permissions with the saved ones. Answer once and bind approval to this turn and call. A cached decision cannot authorize execution. |
| [SDK file checks](../src/approval_files.rs) | Verify file contents and empty destinations. Replace prepared update diffs with hashes and remove the contents of deleted files. |

## Use a scrubber with approvals

Set `prepare_tool_input` and `prepare_tool_output` on `ToolExecutionConfig` and pass it as `RunConfig(tool_execution=...)`. You can use either callback alone. Supply the same callbacks and lifecycle hooks on both workers. The SDK checks which hooks you provide. Your application must keep their code and policy consistent.

- Before approval, the input hook can replace `call.arguments["command"]`. This field holds the shell command or patch text. The SDK checks that Codex accepted that text, then saves it for review.
- On approval, the new worker uses the reviewed text without running the input hook again. After execution, the output hook can replace the result before the SDK saves it or sends it to the model.
- If a required hook is missing, the approval stays pending. Rejection skips execution and the output hook. If output preparation fails after execution, check for side effects before retrying.

The SDK saves the required hook names, never the callbacks. Earlier SDK versions cannot resume these approvals. Approvals without hooks keep their original format.

Codex used to reject every hook at this step. The patch now checks the full hook list against the SDK's trusted handlers for the current worker. Any other hook or hook warning stops recovery. The SDK handles the callbacks and checks the input. Codex still checks permissions before execution.

## Safety limits

Recovery requires direct tools in one local environment and human review. Protect the session store. Tool hooks do not scrub other data such as prompts, paths or approval reasons. When recovery and either preparation callback are on, native SDK traces omit model and tool content.

The patch adds a limit to Codex's permission-checked file reader because the existing stream can follow symbolic links. The SDK checks files before saving, on restart and just before execution. Each check covers at most 32 paths, 8 MiB per file, 16 MiB in total and 10 seconds. A changed, unreadable or oversized source file or an occupied destination stops recovery.

Codex chooses the sandbox for the first attempt as usual. A restored command runs at most once. Codex checks the call ID and exit code and refuses a live process handle.

After a sandbox denial, Codex may retry an edit outside the sandbox if its policy allows that retry without another approval. The SDK checks the files and permissions again. Any committed edit stops the retry, even a write of identical contents. Earlier writes remain in place.

The application must restore the same workspace and prevent concurrent edits. A partial edit or a crash before the result is saved can leave the session active. Inspect the workspace and reconcile any effects before retrying.

## Check and remove

Follow the [patch guide](README.md) and run the approval tests. On another worker, approve and deny a saved command and edit. With a scrubber, check that the action matches the reviewed input and history contains the prepared result. Check that a stale file, changed permissions or a duplicate approval causes no extra write. Remove the patch when upstream Codex can resume the same action with these checks.
