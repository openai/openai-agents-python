# A local Python service and browser

This starter runs real native SDK tools behind an HTTP service. Its browser uses `ai@6.0.315`. A Python tool reads 10,000 synthetic rows and returns a bounded summary to the model. The model asks for approval and returns a validated `Final` object. The application publishes validated XML only after the original native request accepts the approval decision. No provider key is required.

Install the [SDK preview wheel](../../docs/sdk/installation.md) and use the example files from the same release. The example supports a private local POSIX host. It does not supply production authentication or a distributed worker service.

## Run

Use Linux or macOS, Python 3.10 or later, Node 22 or later, and npm.
The SDK wheel must support the host platform and Python version. This example requires POSIX file locks and directory permissions, so it does not support Windows. With the SDK installed and your Python environment activated, run these commands from the root of the release's source archive:

```sh
python -m pip install -r examples/web_starter/requirements.txt
npm --prefix examples/web_starter ci
npm --prefix examples/web_starter run build
python -m examples.web_starter --directory /tmp/agents-web-state --port 8080
```

For an optional live model, set `OPENAI_API_KEY` in the server environment and add `--live` to the same command. The server rejects live mode before opening state when the key is missing. Both modes use the same Session, tools, approval and publication code. The UI labels the selected mode. Live calls can incur provider charges; the scripted tests do not measure live model quality.

Open `http://127.0.0.1:8080`, choose `alice`, and send the request. The service creates the state directory with mode `0700`. Its parent must already exist and be trusted. Choose a private persistent path for state that must survive a service restart. A second process cannot open the same state directory.

Approve publication to get a download link. Open a second browser profile as `bob` to check isolation. The login selector deliberately allows either synthetic user. Anyone with local access can choose either identity. Before a shared deployment, replace the login selector with your application's authenticated identity. Keep this demo private and bound to loopback.

## Ownership and recovery

A worker owns each fresh `Agent.session()` from open through joined cleanup. The HTTP handlers own no Sessions. One worker executes work by default, and a finite queue holds two more requests. Admission returns `503` when that queue is full. These limits apply to this process, not an entire deployment. A single deadline starts at admission, so queue wait, model startup, tools and approvals consume the same budget. Cancellation and cleanup can outlast that deadline. The worker keeps its slot until cleanup joins. The deadline does not impose a hard wall-clock or model-token ceiling.

The browser serializes login and request submission. It clears the display when identity changes and rejects late responses from an older identity or receipt. The browser saves the request key and payload before sending them. If the HTTP response is lost, Send retries that envelope. The server scopes keys to the authenticated user. The same key and payload return the original receipt; a different payload gets `400`. A browser that knows the receipt can recover it with `GET /api/receipts/{id}`. Restart changes unfinished receipts to `unknown`, retains their published objects, and never repeats the work. A new key means a new operation; do not choose one to retry an unknown effect without reconciliation.

Disconnect closes only the browser reader. Reconnect or reload reads an authorized application snapshot. Stop asks the owner to cancel and join the original Turn. The UI shows `stopping` until cleanup completes. A caller timeout also releases only its waiter; it does not prove cancellation. A committed publication remains after later cancellation or failure.

The UI stream contains application snapshots and the settled typed `Final`. It does not stream partial typed objects or reconstruct text from SDK history. The application keeps its submitted user prompt. Public `Session.items()` is a projection, not a complete transcript; bounded observations are not a durable event log. On `EventGap`, the owner marks the application snapshot for resynchronization. The browser can replace its display from that snapshot without replaying observations. An application that needs intermediate text replay must store its own authorized messages.

## Identity, approval and credentials

`Identity` and `RunContext` are frozen application dataclasses. `ToolContext` carries that chosen context into `read_report`; the SDK does not make arbitrary context contents immutable. Model arguments never choose an owner, credential, checkpoint path or artifact path.

The live approval registry binds an opaque token to the original `ActionRequested`, receipt and identity. Foreign, duplicate and stale replies cannot publish. The handler validates XML, consumes the token, and awaits `request.respond(ApprovalDecision(...))` before any artifact write. A cancelled native grant fails at that boundary. After native acceptance, the handler checks local revocation, Stop and deadline again, then writes synchronously. It wakes the worker only after that step finishes. If native reply delivery or publication fails, the worker records an error and exposes no successful final result or checkpoint. `approved` means the native request accepted the decision. An artifact in the authorized application snapshot establishes publication.

The service also owns a tiny synthetic HTTP MCP peer on a private loopback port. `mcp_for` builds a current `HttpMCP` declaration for each user's server-side token and selects only `read_record`. The browser and model never receive those tokens. The scripted demo calls the Python `read_report` tool. In live mode, the model can select that tool or the MCP `read_record` tool, which returns the current user's synthetic record.

A Session captures its MCP declaration and credential at open. The approval callback checks local revocation before selected tool dispatch; it does not prevent initial discovery or revoke an effect already running remotely. This synthetic peer checks revocation on each HTTP request too. A real MCP server must enforce its own credential scope and revocation policy. Refreshing a token requires a new Session with a current declaration.

After a Turn settles, the application saves a checkpoint privately and returns only its opaque ID. `GET /api/objects/checkpoint/{id}` checks owner access and returns metadata, never checkpoint bytes. An owner can send `checkpoint` with a new `/api/receipts` request to open a fresh Session with that settled state, current context and current MCP credentials. The server never restores browser approval tokens or pending manual requests. Native checkpoints are sensitive trusted application data, not portable browser authority.

## Rich output and download

This extends the validation flow in [validated_artifact.py](../validated_artifact.py).
The service stores the application-selected output as a SQLite blob instead of exposing a local file path. The download serves exactly those validated bytes.

`read_report` stores the full report in the application database and returns fewer than 200 bytes of summary and opaque handle to the model. The browser renders only the reviewed title, row count and three-row preview. The authorized report endpoint can return the full data. Model output cannot select a filesystem path.

The publish handler validates XML syntax and requires a `presentation` root with a `slide`. It stores the validated bytes under an opaque artifact ID. Download checks owner and expiry, sets `application/xml`, and chooses the fixed filename `report.xml`. Failed validation publishes nothing. The UI shows the typed final result only after the Turn settles, and shows pending, error, cancelled, expired or unknown status when appropriate. A scripted final answer proves no model judgment about publication; use the artifact record for that fact.

| Store | Lifetime and owner |
| --- | --- |
| Live Session, approval registry, login cookies and MCP credentials | Process-owned; lost on restart. Never restore live grants from a snapshot. |
| Receipt, user prompt and reviewed UI snapshot | SQLite application data. Preserved across restart until the application deletes the state directory. |
| Full reports, validated artifacts and settled checkpoints | Owner-scoped SQLite objects. Access expires after five minutes; expired bytes remain until application deletion. |
| SDK history and observations | Owned by the live Session; neither is this application's durable transcript. |

Expiry prevents access; this demo does not run a retention job. Protect the whole directory, including SQLite files and lock files. The service trusts the directory's parent and other processes running as the same OS user. Configure retention, backups, encryption and log handling in the owning application before storing real data. This example uses no real secrets or customer data and disables HTTP access logs.

## Local service and external worker deployment

For a supervised local service, install the matching SDK wheel and optional dependencies in a dedicated virtual environment on a supported host. Check that `python -c 'import agents._native'` succeeds on that platform, build the pinned browser client, and start the command above with a persistent private state directory. Bind only to loopback. Send the process `SIGTERM` for shutdown: aiohttp stops accepting requests, and application cleanup signals admitted work and joins the workers before closing MCP and SQLite. Do not kill workers merely to meet the request deadline. A crash or forced kill leaves unfinished receipts `unknown` at the next start.

For jobs that need work outside this process, use the existing [external-job example](../external_job/README.md). It supplies the worker and journal rather than treating this in-memory queue as durable execution. From the repository root, with a local Responses endpoint on port 8081 and `THUMBNAIL_STATE_DIR` set to a private absolute POSIX directory:

```sh
python -m examples.external_job run sample-01 --model-port 8081 --directory "$THUMBNAIL_STATE_DIR"
python -m examples.external_job worker sample-01 --directory "$THUMBNAIL_STATE_DIR"
python -m examples.external_job run sample-01 --model-port 8081 --directory "$THUMBNAIL_STATE_DIR"
```

The worker needs no model credential. The external-job example closes its Sessions before closing `NativeProvider`. Reuse its journal's reconciliation rules for that worker's effects. These are separate local examples; this starter does not silently launch the external worker, transfer typed pending approvals to it, or claim production durability.

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

The browser test uses real HTTP and checks Send, approval, typed final rendering, disconnect, reload, download, foreign-user denial and Stop. Without installed optional browser files, this test skips unless `AGENTS_REQUIRE_WEB_BROWSER=1`. Scripted tests prove application and SDK behavior, not live model quality, production authentication, hard resource limits or remote executor artifact export.
