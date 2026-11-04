# Run two local coding workers

Use a wheel and native helpers built from this revision. See [the source build guide](../../packages/codex/MAINTAINING.md#build-from-this-source). Run the example from the repository root on a supported Linux or macOS host. Supply `OPENAI_API_KEY` through your usual credential setup. Set `WORKER_MODEL` to select a different model.

```sh
job_dir="$(mktemp -d)"
python -m examples.supervised_worker work "$job_dir/alpha" &
python -m examples.supervised_worker work "$job_dir/bravo" &
python -m examples.supervised_worker status "$job_dir/alpha"
wait
python -m examples.supervised_worker status "$job_dir/alpha"
python -m examples.supervised_worker publish "$job_dir/alpha"
```

Each process owns a separate workspace and native `LocalStorage` history. A status client can exit while the worker runs. When a new client reads status, it sees the same saved session ID and any accepted result. The application keeps its records beside the workspace, outside the roots it grants to model tools.

Each worker calls `Turn.steer_exact()` twice when the brief tool starts. The receipts confirm the native turn accepted the inputs. The worker denies approval requests and stops when an unattended run needs another decision. It uses `Turn.wait_for_attention()` to distinguish completion from interruption, connection loss, and provider limits.

To select a trusted MCP server, pass `--mcp-url` with a server that exposes a `lookup` tool. To load a method file, pass `--instruction-file PATH`. Use the same trusted settings when reopening a worker.

The output schema checks the shape of the model's claim. Before accepting it, the worker verifies the saved command record and reads the artifact. A rejected draft gets a revision request in the same conversation. The accepted record stores the text with a SHA-256 digest. The `publish` action writes one local marker from that record and reports an existing marker on repeat.

The `interrupt` action cancels a turn when the brief tool starts. The worker also bounds startup and each turn with `--startup-timeout` and `--turn-timeout` in seconds. After a turn timeout, the worker joins cancellation before reporting the outcome. Cleanup can extend past the wait deadline.

The native fixture tests cover two simultaneous workers, a closed and reopened status client, denial, revision, interruption, and a crash after a simulated handoff. The `effect` action records that handoff. The recovery worker matches the uncertain native call against the application record, reopens the same session with the confirmed result, and sends a new tool-free input. The test verifies that the original effect and the publication marker each appear once.

Accepted records show how long the session took to open and each revision took to settle. They also show the time from submission to the first observed tool result and the time to settle the accepted turn. These are wall-clock measurements for this workflow. The records include the applied turn settings and token usage. They include the remaining request count when the API-key provider reports it. The tests use synthetic Responses and MCP peers.
