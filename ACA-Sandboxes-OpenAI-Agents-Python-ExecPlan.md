# Implement the ACA Sandboxes hosted provider

This ExecPlan is a living document. The sections Progress, Surprises & Discoveries,
Decision Log, and Outcomes & Retrospective must stay up to date as work proceeds.

This document follows `PLANS.md` and implements
`ACA-Sandboxes-OpenAI-Agents-Python-First-Release-Plan.md`.

## Purpose / Big Picture

Add Azure Container Apps (ACA) Sandboxes as an optional hosted sandbox provider. A
consumer can create or resume an ACA sandbox through `SandboxRunConfig`, materialize a
manifest, execute non-interactive commands from the manifest root, read and write files,
resolve configured HTTP ports, serialize provider state, and delete provider-owned
resources. The first release must fail clearly for PTY sessions, manifest mounts, and the
OpenAI snapshot persistence lifecycle.

## Progress

- [x] (2026-07-23 19:15Z) Read the first-release plan, repository contributor rules, and sandbox runtime boundary.
- [x] (2026-07-23 19:18Z) Created a dependency-ordered TODO list for implementation and validation.
- [x] (2026-07-23 19:25Z) Established the compatibility boundary at release tag `v0.18.3`.
- [x] (2026-07-23 19:32Z) Verified the `azure-containerapps-sandbox==0.1.0b4` async API from the public wheel.
- [x] (2026-07-23 20:05Z) Implemented ACA options, session state, and centralized provider error mapping.
- [x] (2026-07-23 20:05Z) Implemented ACA client/session lifecycle, exec, file operations, exposed ports, and unsupported-feature behavior.
- [x] (2026-07-23 20:12Z) Added guarded exports, optional dependencies, package freshness exception, and lockfile updates.
- [x] (2026-07-23 20:26Z) Added provider tests and compatibility guards.
- [x] (2026-07-23 20:34Z) Added the public-import-only runner example and English documentation.
- [x] (2026-07-23 20:36Z) Focused ACA, compatibility, import, and example tests passed.
- [x] (2026-07-23 21:06Z) Full lint, focused tests, targeted ACA mypy, and docs build passed.
- [ ] Mandatory full typecheck and test suite are blocked by unrelated Windows baseline failures recorded below.
- [x] (2026-07-23 21:28Z) Fresh-context review found no implementation defects.
- [ ] Run the real ACA end-to-end checklist when credentials and a sandbox group are available.

## Surprises & Discoveries

- Observation: The configured package index could not resolve the pinned ACA preview
  release even though the public PyPI release exists.
  Evidence: `uv` reported no matching `azure-containerapps-sandbox==0.1.0b4`; the public
  PyPI JSON and wheel identify version `0.1.0b4`, uploaded 2026-07-17.
- Observation: The plan pseudocode is directionally correct but cannot be copied
  literally.
  Evidence: The async SDK requires `poller = await begin_create_sandbox(...)` followed
  by `await poller.result()`. `SandboxClient.add_port` accepts `port` plus
  `anonymous=False`; create-time authenticated ports use `AddPortRequest(auth=None)`.
- Observation: The OpenAI Developer Docs MCP is not configured in this environment.
  Evidence: The `codex` executable is unavailable. SDK behavior is therefore verified
  from the checked-out runtime contracts, while ACA behavior is verified from the pinned
  public wheel.
- Observation: The repository-wide seven-day package freshness delay initially excluded
  the six-day-old ACA release.
  Evidence: `pyproject.toml` sets `exclude-newer = "7 days"`. A package-specific
  exception was required so normal sync and lock resolution can install the exact
  release mandated by the plan without disabling the global policy.
- Observation: The repository's mandatory Unix-oriented verification stack does not pass
  in this Windows checkout before considering ACA behavior.
  Evidence: full tests report 4,540 passed but fail on missing `tee` and unsupported
  Windows symlink behavior; full mypy reports five unrelated existing errors, and
  pyright reports the existing `tar_utils.py` temporary-file return mismatch. ACA
  targeted mypy, lint, focused tests, and docs build pass.

## Decision Log

- Decision: Treat ACA as an additive public provider with no compatibility shim.
  Rationale: The latest release tag is `v0.18.3`, and no ACA provider or
  `aca_sandboxes` serialized discriminator exists in that release.
  Date/Author: 2026-07-23 / Copilot.
- Decision: Keep deployment targeting and credentials on `ACASandboxesClient`; keep
  create-time settings on `ACASandboxesClientOptions`.
  Rationale: This is the boundary prescribed by the release plan and avoids serializing
  credentials or endpoint URLs.
  Date/Author: 2026-07-23 / Copilot.
- Decision: Resume only the recorded ACA sandbox ID and never create a replacement.
  Rationale: The first-release plan requires deterministic errors for deleted, disabled,
  timed-out, terminal, and unexpected states.
  Date/Author: 2026-07-23 / Copilot.
- Decision: Use the SDK's native async clients and own only clients/credentials created
  by the adapter.
  Rationale: Injected clients and credentials remain caller-owned; adapter-created
  resources must be closed during client/session cleanup.
  Date/Author: 2026-07-23 / Copilot.
- Decision: Resolve manifest environment values during create, but serialize only the
  explicit provider option environment.
  Rationale: ACA environment is create-time configuration, while resolved manifest
  values may contain secrets that must not enter durable session state.
  Date/Author: 2026-07-23 / Copilot.

## Outcomes & Retrospective

The ACA provider, exports, dependency extra, lockfile, unit tests, compatibility guards,
customer example, English docs, and generated ACA API reference pages are implemented.
Focused validation passes with 76 tests and 12 platform skips, targeted ACA mypy passes,
full Ruff lint passes, and MkDocs builds successfully.

The implementation is not yet release-ready because two external gates remain:

1. The repository-wide typecheck and test commands are blocked by unrelated Windows
   baseline failures described in Surprises & Discoveries.
2. The real ACA end-to-end checklist requires Azure credentials, an ACA sandbox group,
   the required RBAC assignment, and an OpenAI API key; those resources are not available
   in this checkout.

The fresh-context verification review graded all implementation, compatibility, docs,
and local validation criteria as passing. Its only unverified criterion was the live ACA
end-to-end run, so the code is complete but the release gate remains blocked on external
Azure/OpenAI credentials and infrastructure.

## Context and Orientation

Sandbox provider contracts live in `src/agents/sandbox/session/`. Hosted adapters live
under `src/agents/extensions/sandbox/`, and guarded re-exports are owned by
`src/agents/extensions/sandbox/__init__.py`. `BaseSandboxSession.start()` prepares the
backend, materializes the manifest, and marks workspace readiness. A provider client
creates or resumes the inner session and wraps it with `SandboxSession` for
instrumentation and dependency cleanup.

ACA provider code will live in `src/agents/extensions/sandbox/aca/`. The serialized
backend discriminator is `aca_sandboxes`. The implementation depends on
`azure-containerapps-sandbox>=0.1.0b4,<0.2` and `azure-identity>=1.15,<2`.

## Plan of Work

First add immutable Pydantic option and state models with stable field ordering and
registry-compatible discriminators. Add provider-specific error helpers that preserve
Azure diagnostics, retryability, sandbox ID, lifecycle state, and operation context.

Then implement the async ACA client and session. Creation forwards disk, resource,
auto-suspend, labels, environment, and port settings. Session startup creates the
manifest root before materialization. Exec sends one shell command with
`working_directory=manifest.root`. File operations validate workspace paths before using
ACA read/write APIs. Resume obtains the existing sandbox client and calls
`ensure_running`; it never creates a replacement. Deletion stops the local handle and
deletes the ACA resource. PTY, mounts, and OpenAI snapshot lifecycle methods fail with
the documented provider messages.

After runtime behavior is covered, wire optional imports and dependencies, then add
mocked tests using fake ACA modules so tests remain deterministic without Azure access.
Add compatibility guards for exports, constructor order, state order, and registry
round-trips. Finally add the customer example and English docs, run focused validation,
the mandatory full stack, docs build, and a fresh-context verification review.

## Concrete Steps

Run from `C:\Users\ajsharm\source\repos\openai-agents-python`.

1. Implement `src/agents/extensions/sandbox/aca/`.
2. Update extension exports, `pyproject.toml`, and `uv.lock`.
3. Add `tests/extensions/sandbox/test_aca_sandboxes.py` and compatibility assertions.
4. Add `examples/sandbox/extensions/aca_sandboxes_runner.py`; update the extension
   README and `docs/sandbox/clients.md`.
5. Run focused ACA and compatibility tests.
6. Invoke the repository `code-change-verification` skill and run `make build-docs`.
7. Review `git diff --check`, the complete diff, and acceptance evidence.

## Validation and Acceptance

Acceptance requires mocked tests proving create/delete/resume behavior, manifest-root
exec, file I/O, lifecycle error distinctions, secure-by-default ports, anonymous opt-in,
disk and auto-suspend forwarding, and explicit unsupported-feature failures. Registry
and import guards must prove the new public types are stable and optional imports do not
break a base installation.

Repository validation must pass in this order:

    make format
    make lint
    make typecheck
    make tests
    make build-docs

The real ACA release gate from the source plan remains required before release. If Azure
credentials or a sandbox group are unavailable locally, the final handoff must state
that the real-provider gate is unverified rather than claiming release readiness.

## Idempotence and Recovery

All code and test changes are additive. Re-running formatting, lockfile generation, and
tests is safe. Unit tests use fakes and must not allocate Azure resources. Real ACA
validation must label test sandboxes and delete them in `finally` blocks; cleanup queries
from the source plan must show that no labeled resources remain.

## Artifacts and Notes

Pinned ACA async signatures verified from the public `0.1.0b4` wheel:

    await SandboxGroupClient.begin_create_sandbox(...)
    SandboxGroupClient.get_sandbox_client(sandbox_id)
    await SandboxClient.ensure_running(timeout=...)
    await SandboxClient.exec(command, working_directory=...)
    await SandboxClient.read_file(path)
    await SandboxClient.write_file(path, content)
    await SandboxClient.add_port(port, anonymous=False)
    await SandboxClient.begin_delete()

## Interfaces and Dependencies

The provider exports `ACASandboxesClient`, `ACASandboxesClientOptions`,
`ACASandboxesSession`, and `ACASandboxesSessionState` from both
`agents.extensions.sandbox.aca` and the guarded `agents.extensions.sandbox` package.
`ACASandboxesClient.backend_id`, the options discriminator, and the session-state
discriminator are all `aca_sandboxes`.
