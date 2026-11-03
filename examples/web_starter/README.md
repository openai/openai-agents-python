# A local Python service and browser

This starter runs real native SDK tools behind an HTTP service. Its browser uses `ai@6.0.315`. A Python tool reads 10,000 synthetic rows and returns a bounded summary to the model. The model asks for approval and returns a validated `Final` object. The application publishes validated XML only after the original native request accepts the approval decision. No provider key is required.

Install the [SDK preview wheel](../../docs/sdk/installation.md) and use the example files from the same release. The example supports a private local POSIX host. It does not supply production authentication or a distributed worker service.

## Run

Requirements:

- Linux or macOS. POSIX file locks and directory permissions are required, so Windows is unsupported.
- Python 3.10 or later and an SDK wheel for that Python version and platform.
- Node 22 or later and npm.

With the SDK installed and your Python environment activated, run these commands from the root of the release's source archive:

```sh
python -m pip install -r examples/web_starter/requirements.txt
npm --prefix examples/web_starter ci
npm --prefix examples/web_starter run build
python -m examples.web_starter --directory /tmp/agents-web-state --port 8080
```

### Choose a model mode

For an optional live model, set `OPENAI_API_KEY` in the server environment and add `--live` to the same command. The server rejects live mode before opening state when the key is missing. Both modes use the same Session, tools, approval, and publication code. The UI labels the mode. Live calls can incur provider charges.

### Try the browser

Open `http://127.0.0.1:8080`, choose `alice`, and send the request. The service creates the state directory with mode `0700`. Its parent must already exist and be trusted. Choose a private persistent path for state that must survive a service restart. A second process cannot open the same state directory.

Approve publication to get a download link. Open a second browser profile as `bob` to check isolation. Anyone with local access can select either synthetic user. Before a shared deployment, replace the login selector with your application's authenticated identity. Keep this demo private and bound to loopback.

## Ownership and recovery

### Workers and deadlines

A worker opens each fresh `Agent.session()` and waits for its cleanup to finish. HTTP handlers do not own Sessions. By default, one worker runs requests and the queue holds two more. The service returns `503` when the queue is full. These limits apply to this process, not an entire deployment.

A single deadline starts when the service admits a request. Time in the queue, model startup, tools, and approvals all count toward that deadline. Cancellation and cleanup can outlast that deadline. The worker keeps its slot until cleanup finishes. The deadline does not impose a hard limit on elapsed time or model tokens.

### Retry and restart

The browser serializes login and request submission. It clears the display when identity changes and rejects late responses from an older identity or receipt. The browser saves the request key and payload before sending them. If the HTTP response is lost, Send retries the saved key and payload. The server scopes keys to the authenticated user:

- The same key and payload return the original receipt. A different payload gets `400`.
- A browser that knows the receipt can recover it with `GET /api/receipts/{id}`.
- On restart, the service marks unfinished receipts as `unknown` and keeps their published objects. It never repeats the work.

A new key starts a new operation. Before retrying an unknown effect with a new key, reconcile the original operation.

### Disconnect and Stop

Disconnect closes only the browser reader. Reconnect or reload reads an authorized application snapshot. Stop asks the owner to cancel and join the original Turn. The UI shows `stopping` until cleanup completes. A caller timeout also stops only that caller's wait. It does not prove cancellation. A committed publication remains after later cancellation or failure.

### Recover the display

The UI stream contains application snapshots and the settled typed `Final`. It does not stream partial typed objects or reconstruct text from SDK history. The application keeps its submitted user prompt.

Public `Session.items()` returns a filtered history rather than a complete transcript. Observations are bounded and do not form a durable event log. On `EventGap`, the owner marks the application snapshot for resynchronization. The browser can replace its display from that snapshot without replaying observations. An application that needs intermediate text replay must store its own authorized messages.

## Identity, approval and credentials

`Identity` and `RunContext` are frozen application dataclasses. `ToolContext` carries that chosen context into `read_report`; the SDK does not make arbitrary context contents immutable. Model arguments never choose an owner, credential, checkpoint path or artifact path.

### Publish after approval

The live approval registry binds an opaque token to the original `ActionRequested`, receipt, and identity. Replies from another identity, duplicate replies, and stale replies cannot publish. The handler:

1. Validates XML and consumes the token.
2. Awaits `request.respond(ApprovalDecision(...))` before any artifact write. A cancelled native grant fails here.
3. Checks local revocation, Stop, and the deadline again after native acceptance, then writes synchronously.
4. Wakes the worker after the write finishes.

If native reply delivery or publication fails, the worker records an error and exposes no successful final result or checkpoint. `approved` means the native request accepted the decision. Check the artifact in the authorized application snapshot to establish publication.

### Scope MCP credentials

The service also owns a tiny synthetic HTTP MCP peer on a private loopback port. `mcp_for` builds an `HttpMCP` declaration with each user's current server-side token and selects only `read_record`. The browser and model never receive those tokens. The scripted demo calls the Python `read_report` tool. In live mode, the model can select that tool or the MCP `read_record` tool, which returns the current user's synthetic record.

A Session captures its MCP declaration and credential at open. The approval callback checks local revocation before dispatching a selected tool. It does not prevent initial discovery or revoke an effect already running remotely. This synthetic peer checks revocation on each HTTP request too. A real MCP server must enforce its own credential scope and revocation policy. Refreshing a token requires a new Session with a current declaration.

### Restore settled state

After a Turn settles, the application saves a checkpoint privately and returns only its opaque ID. `GET /api/objects/checkpoint/{id}` checks owner access and returns metadata, never checkpoint bytes.

An owner can send `checkpoint` with a new `/api/receipts` request to open a fresh Session with that settled state, current context, and current MCP credentials. The server never restores browser approval tokens or pending manual requests.

Load native checkpoints only from trusted application storage. They contain sensitive data and do not grant the browser permission to control a Session.

## Rich output and download

This extends the validation flow in [validated_artifact.py](../validated_artifact.py). The service stores the application-selected output as a SQLite blob instead of exposing a local file path. The download serves exactly those validated bytes.

`read_report` stores the full report in the application database and returns fewer than 200 bytes of summary and opaque handle to the model. The browser renders only the reviewed title, row count and three-row preview. The authorized report endpoint can return the full data. Model output cannot select a filesystem path.

The publish handler validates XML syntax and requires a `presentation` root with a `slide`. It serializes the parsed document as UTF-8 and stores those bytes under an opaque artifact ID. This can change formatting and removes the original encoding declaration.

The download handler checks the owner and expiry, sets `application/xml`, and uses the fixed filename `report.xml`. Failed validation publishes nothing.

The UI shows the typed final result only after the Turn settles, and shows pending, error, cancelled, expired or unknown status when appropriate. Scripted final answers do not establish model judgment about publication.

### Storage and expiry

| Store | Lifetime and owner |
| --- | --- |
| Live Session, approval registry, login cookies and MCP credentials | Process-owned; lost on restart. Never restore live grants from a snapshot. |
| Receipt, user prompt and reviewed UI snapshot | SQLite application data. Preserved across restart until the application deletes the state directory. |
| Full reports, validated artifacts and settled checkpoints | Owner-scoped SQLite objects. Access expires after five minutes; expired bytes remain until application deletion. |
| SDK history and observations | Owned by the live Session; neither is this application's durable transcript. |

Expiry prevents access. This demo does not run a retention job. Protect the whole directory, including SQLite files and lock files. The service trusts the directory's parent and other processes running as the same OS user. Configure retention, backups, encryption and log handling in the owning application before storing real data. This example uses no real secrets or customer data and disables HTTP access logs.

## Local service and external worker deployment

### Supervise the local service

1. Install the matching SDK wheel and optional dependencies in a dedicated virtual environment on a supported host.
2. Check that `python -c 'import agents._native'` succeeds on that platform.
3. Build the pinned browser client and start the [service command](#run) with a persistent private state directory. Bind only to loopback.

To shut down, send the process `SIGTERM`. aiohttp stops accepting requests. Application cleanup signals admitted work and waits for the workers before closing MCP and SQLite.

Do not kill workers merely to meet the request deadline. A crash or forced kill leaves unfinished receipts `unknown` at the next start.

### Use an external worker

For jobs that need work outside this process, use the existing [external-job example](../external_job/README.md). It supplies a separate worker and journal. This starter's in-memory queue does not provide durable execution. From the repository root, with a local Responses endpoint on port 8081 and `THUMBNAIL_STATE_DIR` set to a private absolute POSIX directory:

```sh
python -m examples.external_job run sample-01 --model-port 8081 --directory "$THUMBNAIL_STATE_DIR"
python -m examples.external_job worker sample-01 --directory "$THUMBNAIL_STATE_DIR"
python -m examples.external_job run sample-01 --model-port 8081 --directory "$THUMBNAIL_STATE_DIR"
```

The worker needs no model credential. The external-job example closes its Sessions before closing `NativeProvider`. Reuse its journal's reconciliation rules for that worker's effects.

This starter does not launch the external worker or transfer typed pending approvals to it. Neither local example establishes production durability.

## Verify

Run focused no-key HTTP and native tests from the repository root:

```sh
python -m pytest -q tests/test_web_starter.py
```

To require the real Chromium and pinned-client test, install the browser once, then run:

```sh
npm --prefix examples/web_starter exec -- playwright install chromium
AGENTS_REQUIRE_WEB_BROWSER=1 python -m pytest -q tests/test_web_starter_browser.py
```

The browser test uses real HTTP and checks Send, approval, typed final rendering, disconnect, reload, download, foreign-user denial and Stop. Without installed optional browser files, this test skips unless `AGENTS_REQUIRE_WEB_BROWSER=1`. Scripted tests check application and SDK behavior. They do not measure live model quality or verify production authentication, hard resource limits, or artifact export from remote executors.
