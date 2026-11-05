# Run two local coding workers

Run independent coding jobs, check their saved status, and record publication after the application verifies an artifact.

## Set up the workers

Use a wheel and native helpers built from this revision. See [the source build guide](../../packages/codex/MAINTAINING.md#build-from-this-source). Run the example from the repository root on a supported Linux or macOS host.

Supply `OPENAI_API_KEY` through your usual credential setup. Model requests can incur charges. Set `WORKER_MODEL` to select a different model.

Keep the wheel and native helpers outside both job directories. Use trusted prompts and workspace files for this demo. For untrusted input, run each job in its own OS sandbox or VM with access only to that job's data.

## Run and inspect two jobs

```sh
job_dir="$(mktemp -d)"
python -m examples.supervised_worker work "$job_dir/alpha" &
alpha_pid=$!
python -m examples.supervised_worker work "$job_dir/bravo" &
while [ ! -f "$job_dir/alpha/identity.json" ] && kill -0 "$alpha_pid" 2>/dev/null; do
    sleep 0.1
done
if [ -f "$job_dir/alpha/identity.json" ]; then
    python -m examples.supervised_worker status "$job_dir/alpha"
fi
wait
python -m examples.supervised_worker status "$job_dir/alpha"
python -m examples.supervised_worker publish "$job_dir/alpha"
```

Each worker prints a JSON outcome. When a worker succeeds, its workspace contains `summary.txt` and its saved status includes an `accepted` record. The final `publish` command prints `{"status": "published"}`. Repeating publication prints `{"status": "already_published"}`.

## Reconnect to a running worker

Each process owns a separate workspace and native `LocalStorage` history. A status client can exit while the worker runs. When a new client reads status, it sees the same saved session ID and any accepted result. The application keeps its records beside the workspace, outside the roots it grants to model tools.

## Steer work and handle attention

Each worker calls `Turn.steer_exact()` twice when the brief tool starts. The receipts confirm the native turn accepted the inputs. The worker denies approval requests and stops when an unattended run needs another decision. It uses `Turn.wait_for_attention()` to distinguish completion from interruption, connection loss, and provider limits.

## Add trusted instructions and tools

To select a trusted MCP server, pass `--mcp-url` with a server that exposes a `lookup` tool. To load a method file, pass `--instruction-file PATH`. Use the same trusted settings when reopening a worker.

## Verify and publish an artifact

The output schema checks the shape of the model's claim. Before accepting it, the worker verifies the saved command record and reads the artifact. A rejected draft gets a revision request in the same conversation. The accepted record stores the text with a SHA-256 digest. The `publish` action writes one local marker from that record and reports an existing marker on repeat.

## Interrupt work and set deadlines

The `interrupt` action cancels a turn when the brief tool starts. The worker also bounds startup and each turn with `--startup-timeout` and `--turn-timeout` in seconds. After a turn timeout, the worker joins cancellation before reporting the outcome. Cleanup can extend past the wait deadline.

## Recover an uncertain handoff

The native fixture tests cover two simultaneous workers, a closed and reopened status client, denial, revision, interruption, and a crash after a simulated handoff. The `effect` action reports completion only after verifying the handoff in the application journal. The `recover` action matches the uncertain native call against the application record. It reopens the same session with the confirmed result and sends a new tool-free input. The test verifies that the original effect and the publication marker each appear once.

## Read timing and usage records

Accepted records show how long the session took to open and each revision took to settle. They also show the time from submission to the first observed tool result and the time to settle the accepted turn. These are wall-clock measurements for this workflow. The records include the applied turn settings and token usage. They include the remaining request count when the API-key provider reports it.

The fixture tests use synthetic Responses and MCP peers to check these workflows without measuring live model quality.
