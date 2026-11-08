# Run commands and edit files

Pass an `Environment` to a Session to give the model selected process and file tools in an application-owned workspace. Choose the executor before opening the Session. The Environment controls executor access; Python callbacks and MCP servers keep their own permissions.

| Your task | Configure |
| --- | --- |
| On a supported Linux or macOS application host | [`Environment.local()`](#local-execution) |
| On an application-owned exec-server service | [A WSS `Environment`](#remote-execution) |
| With additional write access | [Write roots](#replace-write-roots-between-turns) |
| With explicit network or local-socket access | [Network policy](#network-and-local-socket-access) |
| After an application decision | [Executor policies](#executor-policies) |

Use an SDK wheel and native helpers that match the documented API, then [configure the model provider](configuration.md#provider-connection). Use a [preview wheel](installation.md) for `0.24.0-alpha.7` or later. Examples with `await` or `async with` belong inside an async application. The remote examples also require an already deployed compatible exec-server and current credentials.

## Local execution

`Environment.local()` requires `0.24.0-alpha.7` or later. Install a [matching preview wheel](installation.md).

Use `Environment.local()` to run selected tools with the SDK's native sandbox on a supported Linux or macOS host. The matching wheel supplies the local executor helper. Give the executor an existing absolute working directory and select the tools the model may call. Use a workspace your application owns.

On macOS, local execution needs a usable per-user `launchd` domain and OS support for signaling a specific process instance. If either capability is unavailable, the SDK fails before running local commands. You can import the wheel and use model-only or WSS sessions on supported hosts that lack either capability.

Replace `/jobs/worker/work` below with an existing absolute workspace path owned by your application. Install the wheel and native helpers outside the workspace and any additional write roots. Local startup and write-root changes reject paths that overlap the helper installation. If you keep `.venv` in a project checkout, give the job a separate workspace.

```python
from pathlib import Path

from agents import Agent, Environment

workspace = Path("/jobs/worker/work")
environment = Environment.local(
    cwd=workspace,
    tools=("exec_command", "apply_patch"),
)
agent = Agent(instructions="Work in the assigned workspace.")
async with agent.session(environment=environment) as session:
    turn = await session.submit("List the files in the workspace.")
    print((await turn.get_result()).final_output)
```

The example asks the model to list files in that workspace. The final answer describes the result, with wording chosen by the model. The SDK refuses startup when the helper or required sandbox support is unavailable. Its sandbox governs executor tools; your application still owns the permissions of Python callbacks, the model connection, and supplied MCP services.

Local commands can read the workspace, any additional `writable_roots`, and the platform files they need to run. The SDK starts them with a minimal environment and a system `PATH`. Command output can reach the model provider. For untrusted prompts or files, run each job in a job-private OS sandbox or VM. Keep host credentials and other jobs' data outside that boundary.

### Add read-only directories on Linux

SDK version `0.24.0-alpha.7` or later supports `Environment.local(mounts=[...])` on Linux. Import `LocalStorageMount` from `agents` and declare each directory as `LocalStorageMount(source=path)`. Destination defaults to source, strategy defaults to `"local_bind"`, and `read_only` defaults to `True`; writable mounts are unsupported. Source and destination must name the same existing canonical absolute directory without symlink components. The caller owns the directory and its contents; the mount grants native commands read-only access without changing the host directory's permissions.

Mounts are unsupported on macOS and WSS Environments. The SDK rejects overlapping mounts, overlaps with `cwd`, write roots, or SDK storage, and files hard-linked to writable directories. Keep the mount paths stable while the Session runs. Active mounts prohibit per-turn `writable_roots` replacement, including an empty list. Restore requires the same applied executor policy, including the mounted directories. Mounts do not copy files into a checkpoint or limit the application's Python callbacks.

### Replace write roots between turns

The executor limits writes to `cwd` and any additional `writable_roots`, while preserving native metadata protections. Linux commands can also write to a private temporary filesystem whose contents disappear when the sandbox exits. Paths resolve on the executor. If a linked Git worktree must update Git state, explicitly grant its required Git directories after checking which paths those grants expose.

In `0.24.0-alpha.7` or later, new local and remote Environments default to `write_policy="strict"`. Set the initial `writable_roots` to opt into extra directories:

| `writable_roots` value | Strict write policy |
| --- | --- |
| `None`, the default, or `[]` | Writes in the working directory only. |
| A nonempty sequence | Writes in the working directory and the listed roots. |

An empty list keeps the working directory writable; it does not create a read-only workspace. Strict enforcement requires a compatible Linux or macOS executor. An unsupported executor fails admission rather than falling back to broader write access. The strict policy disables native sandbox escalation and permission requests that could widen the grants.

To retain the previous implicit WSS policy, explicitly set `write_policy="legacy"` on a remote Environment and leave `writable_roots=None`. Legacy mode rejects any supplied roots, including `[]`, and is unavailable for local execution. Use strict mode with explicit roots for additional workspace grants.

To replace write grants between turns, use a supported local executor or Linux WSS executor. With macOS WSS, set write roots when opening the Session. Later submissions can resubmit an equivalent grant if the paths pass validation.

Before replacing grants, account for earlier command processes. The SDK cannot confirm that processes from earlier remote commands have exited.

In a Session without `NativeAgents`, wait for the active turn and any descendant or background executor work to settle. Collect the results of any yielded tool cells before changing the roots. Then supply the complete replacement list on the next root turn. If the Session uses `NativeAgents`, set its write roots when opening it.

If a current writable root uses a symlink, that path must still resolve to the same directory when you submit it again. To add a directory, pass its canonical executor path from your application's configuration. Use the same rule when restoring a grant you removed.

This fragment requires `Path` from `pathlib` and an open `session` whose earlier executor work has settled. Replace `/jobs/worker/output` with an existing canonical directory on the executor:

```python
# After earlier work settles.
turn = await session.submit(
    "Write the report in the assigned output directory.",
    writable_roots=[Path("/jobs/worker/output")],
)
await turn.get_result()
```

The output directory must already exist on the executor. The working directory stays writable. On executors that support replacement, an empty list clears the additional grants. Omit `writable_roots` to keep the last applied policy. Start a new conversation to change the [network policy](#network-and-local-socket-access). See [restore executor policy](state.md#restore-executor-policy) for when the current Environment must match the last applied policy.

## Remote execution

Remote execution requires an application-owned service that implements the Codex exec-server protocol. Use a version compatible with the SDK's pinned Codex runtime. This guide assumes that service is already deployed and has an authenticated WSS endpoint.

Pass `Environment` to a Session or one-shot run to connect to that service. The Environment configures the Session and does not belong in the Agent's `tools` list. Set `EXECUTOR_URL` to the service's WSS endpoint and `EXECUTOR_TOKEN` to its current credential:

```python
import os

from agents import Agent, Environment


environment = Environment(
    url=os.environ["EXECUTOR_URL"],
    cwd="/workspace",
    headers={"Authorization": f"Bearer {os.environ['EXECUTOR_TOKEN']}"},
    tools=("exec_command", "write_stdin", "apply_patch"),
)
agent = Agent(instructions="Inspect the assigned workspace.")
async with agent.session(environment=environment) as session:
    turn = await session.submit("List the workspace files.")
    result = await turn.get_result()
```

`agent.run(..., environment=environment)` and `agent.run_sync(..., environment=environment)` accept the same connection data. `Environment.tools` defaults to an empty tuple, so process and file tools must be selected explicitly. If the WSS connection fails, the SDK reports a startup cause, such as a certificate error, HTTP status, or timeout. To run tools on the application host, select [local execution](#local-execution) explicitly.

### Select executor tools

Set `cwd` to an absolute executor path or `file:` URI. The selected native model and runtime must support the tools:

- `exec_command` starts commands.
- `write_stdin` polls commands or writes to a TTY. It requires `exec_command`; ordinary pipe-mode stdin is unsupported.
- `apply_patch` edits files.

There is no separate Python process or file client.

### Supply native skills

To make a trusted skill available to the model, place its files on the selected executor. Each discovery root contains a `skills` directory. For example:

```text
/workspace/capabilities/
└── skills/
    └── report-style/
        └── SKILL.md
```

Save this content in `SKILL.md`. The frontmatter supplies the name and description used for discovery; the body contains the skill's instructions.

```markdown
---
name: report-style
description: Write reports with an evidence section.
---

Include an Evidence section that names the files you inspected.
```

Select the parent of `skills`, rather than the skill directory itself. This async fragment uses `environment` and `agent` from [remote execution](#remote-execution). Provision the directory above before opening the new Session:

```python
from dataclasses import replace

environment = replace(
    environment,
    capability_directories=["/workspace/capabilities"],
)
async with agent.session(environment=environment) as session:
    turn = await session.submit("Use report-style to summarize the workspace.")
    result = await turn.get_result()
```

Use the same `capability_directories` option with `Environment.local()` for files on a local executor. The SDK disables host and bundled skill discovery. Selecting a discovery root does not widen command permissions. Keep any files or scripts that commands need within the executor's existing readable roots.

An empty sequence selects no discovery roots. `None` omits the override and keeps the native defaults. When restoring a checkpoint, supply the current executor's roots again. Restore does not recover discovery roots from the saved Environment or fall back to the Python controller's files.

### Argument validation

Before dispatch, the SDK checks `exec_command` and `write_stdin` arguments against their pinned object schemas. It rejects malformed JSON, missing fields, unknown keys, wrong types, and invalid enum values. Omitting an optional field differs from passing `null`.

A rejection fails the turn without automatic retry. Earlier calls may have effects or be cancelled; later calls are not admitted. The rejected call does not enter accepted history. Its error names the tool and includes a SHA-256 digest of the call ID to match an application-owned provider record.

These checks cover the two pinned schemas, not arbitrary JSON Schema or the freeform `apply_patch` grammar. They do not guarantee rollback.

The schemas currently advertise `number` for numeric fields. Codex applies narrower integer and range requirements, with unsigned values for time and token budgets. A schema-valid number can therefore pass admission and still fail native parsing. The SDK passes accepted arguments unchanged; it does not round values or insert defaults.

### Workspace and connection limits

Selected tools can modify the assigned remote workspace. By default the SDK requests writes in `cwd`, protects native metadata, and requests restricted networking. Supply [additional write roots](#replace-write-roots-between-turns) through application code. A model's `workdir` argument only sets a command's working directory.

The application must isolate workspaces and tenants and choose an executor that enforces the required OS limits. Selecting tools does not approve permission escalation. `Environment` has no `sandbox_mode` option.

Configure the connection with current application-owned credentials:

- Supply credentials in `headers`. The declaration's representation omits header values and the URL.
- Use WSS with the native system trust store. TLS verification cannot be bypassed.
- Set `connect_timeout` to positive finite seconds, or `None` for the native default.
- Omit userinfo, query parameters, and fragments from URLs.

### Replace or restore an Environment

A Session captures one Environment. Tool replacement cannot change the Environment, and it does not belong in `update(tools=...)`. To change the connection or selected executor tools, open a new Session. The same Agent can serve Sessions with different Environments. Restore uses the current Environment you supply. Check the [executor policy matching rules](state.md#restore-executor-policy) for your type of restore. Supply current credentials and start new processes through the current Session.

Python, manual, HTTP MCP and stdio MCP tools can coexist with an Environment. A `StdioMCP` server runs on the host by default, or on the executor with `location="environment"`.

### Cancel remote work

On cancellation, the SDK waits for native work to settle and its callbacks to finish. Closing also asks the native owner to terminate known background processes.

An acknowledgment confirms only that the executor accepted the termination attempt. It does not prove process exit, complete output collection, or rollback. Rejected or timed-out termination attempts raise an error while the Session closes.

A lost process-start acknowledgment can leave earlier effects and pending cleanup. Do not retry a consequential command merely because its result is missing.

### Manage the executor lifetime

Closing the Session closes the SDK's connection. Your application still owns the executor service and workspace.

To manage provisioning and release, pass an async context manager that yields an `Environment`. The SDK enters it when opening the Session and exits after releasing native resources, including after opening fails or is cancelled. The manager can then release the executor.

If opening fails and automatic cleanup cannot finish, follow the [provider recovery procedure](configuration.md#recover-a-failed-managed-environment-opening).

This async fragment uses `agent` from [remote execution](#remote-execution) and application functions `acquire_executor()` and `release_executor(job)`. The acquired job supplies its authenticated WSS `url`, assigned absolute `cwd`, and current `headers`. Your application implements provisioning, release, and rollback if acquisition fails:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agents import Environment


@asynccontextmanager
async def executor_lease() -> AsyncIterator[Environment]:
    job = await acquire_executor()
    try:
        yield Environment(
            url=job.url,
            cwd=job.cwd,
            headers=job.headers,
            tools=("exec_command", "write_stdin", "apply_patch"),
        )
    finally:
        await release_executor(job)


async with agent.session(environment=executor_lease()) as session:
    turn = await session.submit("Inspect the assigned workspace.")
    result = await turn.get_result()
```

For each opening, including restore:

1. Create a fresh manager. To restore, pass `environment=executor_lease()` with `state=saved_state`.
2. Supply current credentials and a policy matching any saved network-policy digest.
3. Retain the workspace separately if later work needs its files. Checkpoints do not recreate files.

The SDK enters and exits the manager in one lease task. `ContextVar` changes stay in that task, so pass application data through `context=`. The manager cannot suppress exceptions from the separate Session body.

Close the Session on its opening event loop and handle release failures from Session exit. The SDK keeps the lease until native resource release finishes. Session exit does not guarantee successful backend shutdown.

## Network and local socket access

Pass a `NetworkPolicy` as `network` when constructing a local or WSS Environment. It controls network destinations and local socket access for native commands. This policy applies to the selected executor. The model connection, Python functions, and MCP clients use their own connections. Omitting `network` preserves the default executor behavior.

### Platform requirements

Explicit policies have platform limits:

- On macOS, `NetworkPolicy()` grants no network destinations or Unix sockets.
- Linux cannot enforce Unix-socket denial. The SDK rejects an explicit policy before commands start unless `allow_all_unix_sockets=True`, even with domain rules.
- Windows rejects every explicit network policy in this revision. Its named-pipe path remains available with `write_policy="legacy"`.

### Domain and local access rules

The policy accepts `allowed_domains` and `denied_domains`. A denial takes precedence over an allowance. Supply hostnames or supported host patterns without a scheme or port; each rule covers all ports on the matching hosts. The managed proxy checks hostnames and cannot inspect the contents of encrypted traffic. The policy never permits an upstream proxy or an unsandboxed fallback.

On a macOS executor, `NetworkPolicy(allowed_domains=("api.example.com",))` permits that host through the managed proxy without granting Unix socket access.

### Grant local access

On Linux and macOS executors, `allow_all_unix_sockets=True` grants access to every reachable Unix socket. Use this grant only on an executor private to the job. Keep every socket the job does not need out of reach. The SDK rejects grants for individual socket paths.

`allow_local_binding=True` allows local listeners where supported and adds loopback and private IPv4 ranges to proxy bypass rules. Domain rules apply only to traffic through the managed proxy.

On macOS, local binding can also allow direct traffic to any reachable IP address on port 53, including non-DNS traffic. To enforce stricter limits, the application needs network isolation outside the executor.

Leave `allow_local_binding=False` when the job only needs Unix sockets. An empty domain list does not cancel explicit local-access or Unix-socket grants.

The SDK captures the policy when creating the Environment and checks the reported executor OS and managed-proxy support before commands run. Unsupported settings fail rather than grant broader access.

Every explicit network policy disables sandbox escalation while retaining native workspace limits and command approval callbacks. Calls with `sandbox_permissions="require_escalated"` fail before starting, even if the Session opened successfully.

### Unix sockets in an offline job

This example grants Unix socket access on a private Linux or macOS executor. Configure the executor so the job can reach only its startup socket. The policy leaves the network destination list empty.

```python
import os

from agents import Environment, NetworkPolicy

environment = Environment(
    url=os.environ["EXECUTOR_URL"],
    cwd="/workspace",
    headers={"Authorization": "Bearer " + os.environ["EXECUTOR_TOKEN"]},
    tools=["exec_command"],
    network=NetworkPolicy(allow_all_unix_sockets=True),
)
```

Pass this environment to `agent.session(environment=environment)` or `agent.run(..., environment=environment)`. The command can bind, listen and connect to **every Unix socket the job can reach**. On Linux this includes abstract sockets, which have no filesystem path. The grant excludes VSOCK sockets. Native workspace write limits still apply. A reachable service can have more access than the command. Even a read-only socket mount allows requests.

The backend must enforce the offline boundary:

- Isolate commands, exec-server, managed proxy, and DNS resolver together.
- Exclude host and other jobs' service sockets from mounts and the socket namespace.
- Close inherited connections before starting the executor.

An empty destination list does not prevent proxy hostname lookups before denial. Linux processes can bind local IP sockets inside their isolated namespace. On macOS, the all-sockets grant lets the proxy forward HTTP to any reachable Unix socket. Use an independently isolated macOS VM or equivalent backend so Codex can apply its command sandbox once. A second outer Seatbelt profile is unsupported.

The backend must verify the offline boundary. The SDK cannot infer that isolation from a URL.

When restoring a checkpoint or reopening with `LocalStorage`, follow the [executor policy matching rules](state.md#restore-executor-policy). These rules also explain when a legacy WSS conversation can reopen for model-only work. Start a new conversation to change the network policy.

Python callbacks can run fixed operations directly in the application's worker, using the worker's permissions.

### Docker creation and restore checks

Use the [Docker gateway helper](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/offline_docker.py) to check Linux Docker networking at creation and attachment:

1. `create_offline_job` starts a private container with `--network none`.
2. On every authenticated gateway attachment, use `open_exec_stdio`, including for restore. It inspects the same full container ID passed to `docker exec -i`.
3. Before starting exec-server, the check rejects a wrong network mode, external attached network, missing inspection data, or a stopped container.

The application owns the Docker daemon, image, container ID and authenticated gateway route. Keep that job record outside conversation checkpoints. The gateway stays outside the offline container and sends the exec-server protocol through standard input and output; the example publishes no container port. Closing a gateway attachment does not end the job. Keep the container for restore and remove it when the job finishes.

The image and host must support the native executor and its Linux sandbox. Docker's network setting alone does not establish that support. The example's network checks do not attest an arbitrary WSS URL or prevent a Docker administrator from changing the job later. Native macOS executors and Windows named pipes use their separate platform paths; a Docker Linux guest uses the Linux path regardless of the controller's OS.

### Windows named pipes in an offline job

For a job-local named pipe on Windows x64:

1. Select `tools=["exec_command"]` and `write_policy="legacy"`, and omit `network` and `writable_roots`. Strict write enforcement is unsupported on Windows. Unix socket grants do not apply.
2. Have the provisioner resolve the workspace on the Windows executor and pass its canonical path as `Environment.cwd`.
3. Give the executor account read and write access to its private workspace. The native command sandbox still applies.

The Python controller cannot resolve remote paths locally. The executor handshake reports its own working directory, which can differ from the job's workspace.

The pipe must allow the restricted job process to connect, read and write. A server that creates additional pipe instances needs permission to create them (`FILE_CREATE_PIPE_INSTANCE`).

For example, a Win32 listener can create a single pipe instance with `PIPE_REJECT_REMOTE_CLIENTS` and grant read and write access only to the current logon SID.

Your backend must keep the command, executor and any proxy and DNS resolver inside the same offline, job-private VM or equivalent environment. Keep host and other jobs' service pipes out of reach. Close inherited handles and connections that could reach outside the job. The SDK does not create or verify this isolation.

## Executor policies

Use `Environment.approval(context, arguments)` to let the application decide whether an executor call can run. Return a bool or `StopTurn`, directly or through an awaitable. The callback receives the original arguments before transformation. It has no transform deadline and may wait for a person.

If approval denies the call, returns `StopTurn`, raises an error, or is cancelled, the SDK does not invoke `transform_arguments`. Approval cannot grant extra host or native permissions. It controls selected executor calls, not MCP server startup.

The effect of a denial depends on the tool and how the model calls it:

| Decision | Effect |
| --- | --- |
| `Environment.approval` returns `False` for a direct call | Denies the call. The owning turn fails with `NativeError`; the model does not continue that turn. |
| `Environment.approval` returns `False` for a call from generated code | Stops and joins the owning turn before subsequent generated-code operations can run. Generated code cannot catch the denial to continue execution. |
| MCP `approval` returns `False` for a direct call | Denies the call. The owning turn fails with `NativeError`. |
| MCP `approval` returns `False` for a call from generated code | Rejects that nested MCP call. Generated code can catch the rejection and continue; this denial does not itself stop the owning turn. |
| `StopTurn(...)` from executor approval or an argument transform, or from MCP `before` or `approval` | Stops and joins the exact owning root or child turn. |

After a direct root-call denial, submit a new turn to continue.

Import `StopTurn` and `TurnStoppedError` from `agents`. Once a stopped root's work and cleanup finish, `await turn.get_result()` raises `TurnStoppedError("Turn stopped by policy")`. A captured child Turn that was stopped can report `NativeError` instead. Stopping one child does not stop the root, other children, or later turns. The stopped call does not run, but earlier effects remain.

### Transform executor arguments and results

`transform_arguments(context, value)` accepts or transforms arguments before direct or programmatic calls. It can return raw arguments, `Allow`, `ReplaceArguments` or `StopTurn`. A raw dictionary with an `action` key stays argument data; only SDK decision objects control the call. `transform_result(context, value)` transforms the executor result before the model or generated code receives it. Use it for direct executor calls in the default mode and for calls from generated code with `Agent(programmatic_tool_calling=True)`.

Both transforms can be synchronous or asynchronous. `ExecutorContext` supplies application context, the owning Python Session and Agent, and root or child call identity:

- `agent_role` and `model` identify the native child role and executing model when Codex supplies them. Root calls have no child role, and child calls do not create Python Agents.
- Direct calls set `call_id`.
- Programmatic calls set `outer_call_id`, `cell_id`, and `runtime_tool_call_id`.

Callbacks cannot replace these identities.

An executor approval or transform callback cannot observe its own Session, submit work to it, update it, interrupt it, checkpoint it, or close it. Start any event observer in an application task outside the callback. Use application context for data needed to make a decision.

Arguments for `exec_command` and `write_stdin` are JSON objects; `apply_patch` receives a patch string. Native validation still applies after transformation. A result transform runs after the executor effect and cannot undo it.

Setting `Environment.transform_result` also changes observations for selected `exec_command`, `write_stdin`, and `apply_patch` calls, including direct calls, generated-code calls, and native child calls. The runtime drops raw command-output deltas for those calls. Command and patch completion records retain their identity and status, but their output fields contain a withheld-output placeholder. This control also covers later output from the same background process, including output collected by a controlled `write_stdin` call.

The accepted whole result still reaches the model or generated code. The callback does not transform individual output chunks. Argument transforms and approvals alone do not enable this result control. Other observations and Python or MCP tool output keep their own rules; this setting does not sanitize all telemetry.

`policy_timeout` bounds transform decisions in seconds and defaults to 30. The SDK rejects invalid, late, or failed decisions. These callbacks cover root and native child calls.

See [programmatic tool calling](configuration.md#programmatic-tool-calling). Saved configuration contains no policy callbacks, credentials, endpoints or network-policy names. The [network-policy support](#network-and-local-socket-access) also compares the current Environment policy with the recorded digest on restore. This does not redact command history or native debug logs.
