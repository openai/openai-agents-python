# Build a sandbox provider

You can distribute a sandbox client in your own Python package. Applications import your client and pass an instance to `SandboxRunConfig(client=...)`. The SDK does not need to import your package, add it to `agents.extensions.sandbox`, or discover it through Python entry points.

This guide describes sandbox execution providers, not model providers. The outer `Runner` still owns model turns, approvals, handoffs, and run history. Your client and session implement the execution environment.

## Start with a working external package

The [external provider example](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/external_provider) is a separately installable distribution with its own `pyproject.toml`, import package, and runnable demo. It implements `ExampleDockerClient` by composing the SDK's `DockerSandboxClient`. The example demonstrates the packaging and client boundary using real Docker execution; it is not a new container engine or a published third-party service.

After installing the example package, construct the client in application code:

```python
import docker
from example_docker_provider import ExampleDockerClient, ExampleDockerOptions

from agents import RunConfig
from agents.sandbox import SandboxRunConfig
from agents.sandbox.sandboxes.docker import DockerSandboxClient

connection = docker.from_env()
client = ExampleDockerClient(DockerSandboxClient(connection))
run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=client,
        options=ExampleDockerOptions(image="python:3.14-slim"),
    ),
)
# Pass run_config to Runner.run(...).
# Close connection after all runs using this client have finished.
```

The [demo](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/external_provider/src/example_docker_provider/demo.py) shows the complete `try`/`finally` connection lifecycle. It uses a real container with `ScriptedModel`, so the demo needs no model API key or model request. The runner creates and cleans up the sandbox session. The application closes the Docker connection that the application created.

The example requires a typed `ExampleDockerOptions` instance. Dictionary options passed to `SandboxRunConfig` are parsed according to the client’s backend ID, which remains `docker` here; the adapter rejects the resulting Docker options before creating a session. It also rejects unknown fields on its own options model. An adapter must preserve accepted settings or reject unsupported inputs before delegation, especially settings that control isolation. Use `DockerSandboxClient` directly for Docker networking and other advanced options.

Use composition when an existing backend already implements your runtime. Return the delegate's `SandboxSession` unchanged, and delegate serialization and resume to the same backend. The example retains `backend_id = "docker"` and Docker's state type because the underlying session remains a Docker session. It does not introduce an independent persisted state format or claim additional isolation.

## Implement a new execution runtime

A backend for a different runtime, such as a micro-VM service, needs its own client and session implementation. The relevant interfaces are exported from `agents.sandbox.session`:

| Interface | Responsibility |
| --- | --- |
| `BaseSandboxClient` | Create, resume, and delete sessions; serialize and deserialize backend state. |
| `BaseSandboxClientOptions` | Validate provider-specific creation options and support typed JSON parsing. |
| `BaseSandboxSession` | Execute commands, access files, manage the runtime lifecycle, and persist or hydrate workspace contents. |
| `SandboxSessionState` | Represent the provider identity and workspace state needed for resume. |
| `SandboxSession` | Wrap a backend session with the SDK's instrumentation and dependency lifecycle. |

Implement `create`, `resume`, `delete`, and `deserialize_session_state` on your client. Give a genuinely new runtime a distinct, stable `backend_id`; the runner checks this identity when restoring its saved sandbox state. Keep credentials, SDK connections, and trusted executable selection on the live client rather than in model-visible options or serialized state.

`create` receives `manifest`, `snapshot`, and your `options` object. Preserve their meaning instead of silently ignoring inputs. Validate unsupported options before allocating resources where possible, and clean up resources if creation fails. Return an instrumented `SandboxSession`, not a bare backend session. Existing clients use the base client's `_wrap_session` hook to preserve dependency cloning and instrumentation; an adapter that delegates to an existing client should reuse the returned wrapper instead of wrapping it again.

A new `BaseSandboxSession` must implement the abstract command, file, status, and archive methods: `_exec_internal`, `read`, `write`, `running`, `persist_workspace`, and `hydrate_workspace`. Abstract-method completeness alone does not make a working backend. The session must also implement the appropriate startup and shutdown hooks for its runtime, satisfy the commands used by enabled capabilities and inherited helpers, and enforce its filesystem and process boundaries. Consult the [session implementation](https://github.com/openai/openai-agents-python/blob/main/src/agents/sandbox/session/base_sandbox_session.py) and a comparable backend before choosing which operations to inherit.

Underscored base-class hooks are implementation coupling: pin and test the SDK versions your package supports. Do not copy private helpers from another backend or assume that changing only its command executor gives your runtime the same filesystem, persistence, or isolation semantics. In particular, host filesystem access inherited from a local backend needs a separate trust-boundary assessment for a VM backend.

State the capabilities your backend supports. Reject unsupported PTY sessions, users, exposed ports, mounts, or manifest entries explicitly instead of executing them on the host as a fallback. Translate provider failures into the SDK's sandbox error types while preserving useful error codes and retryability without exposing credentials or raw sensitive responses.

## Registration happens when types are imported

Client selection and serialized-type registration are separate operations:

- Applications select the client by passing an instance to `SandboxRunConfig`. There is no global client factory or entry-point discovery step.
- `BaseSandboxClientOptions` subclasses register automatically from a non-empty `type` default when Python defines the class.
- `SandboxSessionState` subclasses register automatically when their `type` field has a `Literal` annotation and a non-empty string default.

For a new runtime, define stable type identifiers owned by your package. The following declarations illustrate the type-registration mechanism; they do not implement a runnable backend:

```python
from typing import Literal

from agents.sandbox.session import BaseSandboxClientOptions, SandboxSessionState


class AcmeOptions(BaseSandboxClientOptions):
    type: Literal["acme_microvm"] = "acme_microvm"
    image: str


class AcmeState(SandboxSessionState):
    type: Literal["acme_microvm"] = "acme_microvm"
    provider_session_id: str
```

Import these classes in every process that parses their serialized values, including resume workers. For example, importing the external example package makes `BaseSandboxClientOptions.parse({"type": "example_docker", "image": "python:3.14-slim"})` return `ExampleDockerOptions`. Installing a distribution alone does not execute those imports. Do not modify the registries directly. Choose identifiers that do not collide with SDK or other package types; do not rely on collision handling as validation of your package's identity.

The external Docker example needs only a new options type. Its session state remains `DockerSandboxSessionState`, which the delegate imports and understands. A package's distribution name, import name, options type, and execution backend ID serve different purposes and need not match.

## Preserve lifecycle and resume semantics

When an application supplies `client=`, the runner owns the session it creates or resumes. The runner starts the session, uses its capabilities, persists snapshot-backed workspace contents during cleanup, and invokes the backend cleanup paths. Your client must release resources it owns without deleting caller-owned resources. Cleanup must tolerate partial startup and repeated calls.

When an application supplies a live `session=`, the application owns that session. The runner can use the session but does not take over its full lifetime. Document how callers close sessions and how callers close live provider connections. See [session lifecycle](guide.md#sandbox-lifecycle) for both usage patterns.

Provider session state and workspace snapshots are different. State identifies or describes an execution environment that can be resumed. A snapshot contains workspace data that can seed a new environment. Resume should reconnect to the existing environment when possible, or reconstruct it from the saved snapshot if your backend supports that behavior. Do not overwrite an injected or resumed workspace with fresh creation inputs.

Use the client's serialization/deserialization path when restoring a backend session. Generic `SandboxSessionState.parse` selects a registered type; it does not authenticate a remote session or authorize host paths, credentials, mount access, or network policy. Preserve the SDK's handling of persisted grants and mount authority, and require trusted application configuration to rebind authority when necessary. Document which serialized fields survive across package upgrades, and test that supported saved states remain usable.

For per-command VMs, specify which files survive the next command, whether guest-installed packages persist, and whether background processes can survive. A warm VM image and an SDK workspace snapshot are not interchangeable.

## Package and test the integration

Keep provider code in your own import namespace and declare `openai-agents` plus any runtime client library as package dependencies. The example declares `openai-agents[docker]>=0.24.0,<0.25.0`. Treat that as the example's compatibility range, not a guarantee that every provider works across that range without testing. Do not publish into the `agents` namespace or require an SDK fork just to expose your client.

Before distributing your provider:

1. Build and install the distribution into a clean environment. Import it outside your source directory and run an application using `SandboxRunConfig(client=...)`.
2. Test options parsing and state serialization in a fresh interpreter after importing the package. Test unsupported and malformed inputs as well as a valid round trip.
3. Exercise the public `Runner` path with `ScriptedModel`: fresh client-owned sessions, application-owned live sessions, and persisted Runner resume. Verify resource cleanup after success, tool errors, timeout, cancellation, and partial startup.
4. Test workspace snapshot restore and supported file operations. Use framework mocks or the SDK's scripted test helpers for deterministic orchestration checks, while retaining real runtime tests for provider behavior.
5. Run real isolation, filesystem, network, and process tests on each supported runtime/platform combination. Mock tests cannot establish those properties. For a delegating adapter, reuse the underlying runtime contract and test that the adapter preserves it.

The example README contains install and demo commands. The example intentionally leaves a new VM implementation, security policy, and runtime-specific compatibility promises to the provider author.
