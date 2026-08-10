# Contributor Guide

This guide helps new contributors get started with the OpenAI Agents Python repository. It covers repo structure, how to test your work, available utilities, and guidelines for commits and PRs.

**Location:** `AGENTS.md` at the repository root.

## Table of Contents

1. [Policies & Mandatory Rules](#policies--mandatory-rules)
2. [Project Structure Guide](#project-structure-guide)
3. [Operation Guide](#operation-guide)
4. [Code Review Rules](#code-review-rules)

## Policies & Mandatory Rules

### Mandatory Skill Usage

Repository skills are stored under `.agents/skills/`. A reference such as `$<skill-name>` in this file is a repository instruction reference, not a request for manual user invocation. When a rule requires a skill, read `.agents/skills/<skill-name>/SKILL.md` completely before taking task actions, follow its instructions, and resolve referenced files relative to that skill directory.

#### `$code-change-verification`

Run `$code-change-verification` before marking work complete when changes affect runtime code, tests, or build/test behavior.

Run it when you change:
- `src/agents/` (library code) or shared utilities.
- `tests/` or add or modify snapshot tests.
- `examples/`.
- Build or test configuration such as `pyproject.toml`, `Makefile`, `mkdocs.yml`, `docs/scripts/`, or CI workflows.

You can skip `$code-change-verification` for docs-only or repo-meta changes (for example, `docs/`, `.agents/`, `README.md`, `AGENTS.md`, `.github/`), unless a user explicitly asks to run the full verification stack.

Treat `$code-change-verification` as the post-review final gate, not as an iterative review check. When `$implementation-final-review` applies, satisfy its clean-review condition before starting the repository-wide format, lint, typecheck, and test stack. Immediately before starting that stack, use available read-only task or process evidence to check for another broad test, typecheck, build, examples, or integration command already running on the same host. When concrete contention is visible, keep making progress on review, remediation, evidence preparation, or focused checks and defer the broad stack until capacity is available. Do not add a repository lock, host-wide mutex, sentinel file, or user-triggered `finalize` step. Lack of host telemetry alone is not a blocker.

#### `$openai-knowledge`

When working on OpenAI API or OpenAI platform integrations in this repo (Responses API, tools, streaming, Realtime API, auth, models, rate limits, MCP, Agents SDK or ChatGPT Apps SDK), use `$openai-knowledge` to pull authoritative docs via the OpenAI Developer Docs MCP server (and guide setup if it is not configured).

#### `$implementation-strategy`

Before changing or reviewing runtime code, exported APIs, external configuration, persisted schemas, wire protocols, or other user-facing behavior, use `$implementation-strategy` to decide the compatibility boundary and implementation shape. Before coding, write an implementation scope contract that states the required behavior, compatibility requirements, intentionally unsupported cases and their failure behavior, and an already-supported alternative for those cases or that none exists. Treat this contract as a short, updateable engineering decision record, not as a new public API promise. During review, use the skill before requesting compatibility layers, migrations, new abstractions, or broader refactors.

Repeat the skill before editing each new review-feedback batch; an earlier strategy decision is stale when a comment would widen the supported contract or add another compatibility branch, resolver condition, or test permutation. Judge breaking changes against the latest release tag, not unreleased branch-local churn. Interfaces introduced or changed after the latest release tag may be rewritten without compatibility shims unless they define a released or explicitly supported durable external state boundary, or the user explicitly asks for a migration path. Unreleased persisted formats on `main` may be renumbered or squashed before release when intermediate snapshots are intentionally unsupported.

Independent reviewers dispatched by `$implementation-final-review` inherit the implementer's recorded implementation scope contract and do not rerun `$implementation-strategy` in their fresh review contexts. They report inconsistent or decision-incomplete strategy evidence as uncertainty to the implementer. The implementer remains responsible for rerunning `$implementation-strategy` before any review-feedback batch that widens the supported contract, adds a compatibility branch, changes ownership or protocol behavior, expands test permutations, or triggers a complexity reset.

#### `$implementation-final-review`

After implementing runtime code, tests, examples, build/test behavior, or behavior-impacting docs and completing focused tests, run `$implementation-final-review` before final `$code-change-verification` and `$pr-draft-summary` work and before declaring the task complete. Do not start repository-wide lint, typecheck, tests, builds, examples, or integration suites while the independent review is incomplete or finding-bearing. This repository instruction authorizes automatic invocation without a separate user mention. Do not invoke it for planning, investigation, review, or report-only tasks, repo-meta changes, or docs without behavior impact. The skill's clean-review gate does not replace any other mandatory repository skill or verification gate.

#### `$pr-draft-summary`

Before every final response for a task that changed runtime code, tests, examples, build/test configuration, or docs with behavior impact, invoke `$pr-draft-summary` to generate the required PR summary block, branch suggestion, title, and draft description. Determine whether to invoke it from the changed files, not from a subjective assessment of change size.

Skip `$pr-draft-summary` only for trivial or conversation-only tasks, repo-meta/doc-only tasks without behavior impact, or when the user explicitly says not to include the PR draft block.

Producing the PR draft block is part of the local final handoff. It is required for eligible local-only or uncommitted changes and does not authorize creating a branch, committing, pushing, or opening a pull request.

### Work Status Reporting

- Use `RUNNING` only in commentary while autonomous work remains and no user action is required. Do not end a turn with a final response that says the task is still running or asks the user to send a generic continuation prompt.
- Use `COMPLETE` in the final response only when the requested work and every applicable review, verification, and local handoff step are complete.
- Use `NEEDS_DECISION` in the final response only when progress requires a concrete user choice, expanded authority, or an unresolved external condition. State the exact decision or condition instead of asking the user to say "continue".

### Git Worktree and Branch Safety

Work in the user's current checkout and on the current branch by default. If the Codex task is already running in a selected Git worktree, use that worktree without requesting additional permission. Do not create or switch to another Git worktree, and do not create or switch branches, unless the user explicitly asks for or approves that exact action in the current conversation. A request to implement, investigate, review, test, or verify changes does not by itself authorize changing the active worktree or branch.

If isolation or a different checkout is needed, explain why and ask the user before changing Git state. This requirement also applies when another rule or workflow recommends a linked worktree: stop and request approval instead of choosing or creating one automatically.

### Documentation Release Timing

When a feature or bug fix introduces behavior that is not yet available in the latest published release, do not include `docs/` changes that describe that unreleased behavior in the feature or bug-fix pull request, and do not expect those changes as part of that pull request. Handle them in a separate docs-only pull request so maintainers can coordinate its merge timing with the release that makes the documentation accurate. This exception applies only when the documentation would be incorrect for the latest published release; documentation that is already accurate for released behavior remains part of the normal change scope.

### Scope Discipline and Complexity Reset

- Implement the narrowest explicitly stated set of behaviors that satisfies the request. Do not interpret every shape accepted by a host-language protocol, third-party library, or reflection API unless those shapes are required by the task or supported behavior shipped in the latest release.
- Prefer adapting the required case into an existing pipeline over creating a parallel contract, resolver, execution path, or source of truth. Continue to derive schema, validation, naming, documentation, and invocation from the existing source-of-truth functions, types, or modules.
- Every new abstraction, state field, cached classification, compatibility branch, or dispatch mode must map to a stated requirement, released contract, durable boundary, or verified runtime risk. Remove it if that mapping cannot be stated concretely.
- Treat a second related review finding that would add another condition, protocol hop, compatibility case, or test permutation to the same abstraction as a mandatory complexity-reset checkpoint, not another item to patch. Continue the design only when concrete evidence shows that the additional case belongs to the supported contract.
- When that signal appears, stop extending the current design. Re-read the original requirement, group all findings by root cause, compare the complete diff with the merge base of the intended target branch or with the latest release tag when it is the compatibility baseline, and replace branch-local machinery with a narrower contract. Existing unreleased code and tests are not sunk costs. Perform this reset proactively; do not wait for the user or reviewer to request it.
- A released-version reproducer proves reachability, not a supported contract. Verify the exact shape against documentation, tests, examples, intentional public typing, explicit maintainer intent, or concrete user reliance before adding compatibility machinery.
- Prefer an actionable error during construction or validation, before invocation or other side effects, and an existing supported alternative (for example a wrapper function, explicit override, or typed adapter) over partially emulating a broad protocol. Do not add another alternative when an adequate supported one already exists.
- A growing diff is not itself proof of overengineering, but unexpected cross-module spread, duplicated metadata, combinatorial tests, or repeated special cases requires restarting the design review from the original requirement before more code is added.
- Before handoff, verify that the patch has one source of truth per concern, tests the required behavior and intentionally unsupported cases, and does not accidentally make every constructible combination part of the supported SDK behavior.

### ExecPlans

Call out compatibility risk early in your plan only when the change affects behavior shipped in the latest release tag or a released or explicitly supported durable external state boundary, and confirm the approach before implementing changes that could impact users.

Use an ExecPlan when work is multi-step, spans several files, involves new features or refactors, or is likely to take more than about an hour. Start with the template and rules in `PLANS.md`, keep milestones and living sections (Progress, Surprises & Discoveries, Decision Log, Outcomes & Retrospective) up to date as you execute, and rewrite the plan if scope shifts. Call out compatibility risk only when the plan changes behavior shipped in the latest release tag or a released or explicitly supported durable external state boundary. Do not treat branch-local interface churn or unreleased post-tag changes on `main` as breaking by default; prefer direct replacement over compatibility layers in those cases, and renumber or squash unreleased persisted schemas before release when the intermediate snapshots are intentionally unsupported. If you intentionally skip an ExecPlan for a complex task, note why in your response so reviewers understand the choice.

### Public API Compatibility

Treat the parameter and dataclass field order of exported runtime APIs as a compatibility contract.

- For public constructors (for example `RunConfig`, `FunctionTool`, `AgentHookContext`), preserve existing positional argument meaning. Do not insert new constructor parameters or dataclass fields in the middle of existing public order.
- When adding a new optional public field/parameter, append it to the end whenever possible and keep old fields in the same order.
- If reordering is unavoidable, add an explicit compatibility layer and regression tests that exercise the old positional call pattern.
- Prefer keyword arguments at call sites to reduce accidental breakage, but do not rely on this to justify breaking positional compatibility for public APIs.
- Treat intended import paths and `__all__` membership as compatibility contracts. When adding or moving a public symbol, update the owning module, intended top-level or subpackage re-exports, and an import regression test. Keep top-level imports free of optional-dependency failures and runtime side effects; use lazy exports when needed.

### Platform, Docs, and Security Review

- Treat translation-safe English as a documentation compatibility requirement. In new or materially rewritten translatable prose under `docs/` (excluding generated API reference pages), state the actor, scope, ownership, ordering, modality, and lifecycle boundary explicitly whenever they affect the meaning. Use exact API identifiers in inline code, and replace ambiguous pronouns, overloaded nouns, or shorthand when a small clarification can prevent a materially different translation. Do not change the documented behavior merely to make a sentence easier to translate.
- For new or materially rewritten translatable prose, use a lightweight cross-language review of only the changed English sentences and their immediate context. Have an independent reviewer or review pass inspect the source from Japanese, Korean, and Chinese translation perspectives and report only concrete risks such as an ambiguous actor, scope, ownership, ordering, modality, lifecycle boundary, overloaded SDK term, or identifier corruption. Resolve concrete findings in the English source and review the revised lines once. Do not generate full localized pages for routine documentation changes. Pure link, formatting, typo, and other edits that do not change translatable meaning may skip this review.
- If a concrete concern cannot be resolved confidently from the English source, use a temporary translation of only the disputed sentence or paragraph as a focused probe; do not write or commit generated localized files. Reserve `docs/scripts/translate_docs.py --mode full --file <path>` and broader Japanese, Korean, and Chinese output review for changes to the translation tooling or translation controls, explicit localization work, or an explicitly requested broad translation audit. Add or change a fixed translation mapping only when actual cross-document evidence shows that one stable target term is correct across contexts. Prefer contextual guidance and established target-language developer terminology, including standard English terms, over a large or rigid mapping table.
- Treat runnable docs snippets as API compatibility checks. Before adding OpenAI API, provider, Responses, Realtime, WebSocket, or SDK constructor examples, verify the shown arguments and call shape against the actual implementation.
- When adding or updating code in `examples/` or runnable `docs/` snippets, import Agents SDK decorators from `agents.decorators`. Prefer `tool` over `function_tool`; keep non-decorator SDK imports on their existing public import paths.
- Do not let untrusted sandbox manifests opt themselves out of host filesystem or base-directory boundaries. Escape hatches for local source materialization must be controlled by trusted application code at the call site, not by serialized manifest data.
- When documenting sandbox or security grants, verify the actual implementation path enforces the grant or boundary. Do not claim a grant applies to `LocalDir`, `LocalFile`, archive extraction, or other materialization paths unless those paths actually consult it.
- When redacting OpenAI tool, MCP, model, or provider payloads, consider traceback display, exception chaining, `__context__`, logs, and telemetry. Suppressing display with `raise ... from None` is not enough if the original exception object still carries sensitive input data.
- For OpenAI platform or SDK-specific docs changes, prefer `$openai-knowledge` for authoritative platform behavior and inspect the local code path for SDK behavior. Do not rely on generic API assumptions when documenting Responses, Chat Completions, Realtime, tools, MCP, or provider adapters.
- For Realtime tracing changes, read [Realtime tracing architecture](.agents/references/realtime-tracing.md) before proposing SDK spans. Realtime API server traces and Agents SDK client traces are separate; `group_id` can correlate them but does not create a shared trace hierarchy.

## Project Structure Guide

### Overview

The OpenAI Agents Python repository provides the Python Agents SDK, examples, and documentation built with MkDocs. Use `uv run python ...` for Python commands to ensure a consistent environment.

### Repo Structure & Important Files

- `src/agents/`: Core library implementation.
- `tests/`: Test suite; see `tests/README.md` for snapshot guidance.
- `examples/`: Sample projects showing SDK usage.
- `docs/`: MkDocs documentation source; do not edit translated docs under `docs/ja`, `docs/ko`, or `docs/zh` (they are generated).
- `docs/scripts/`: Documentation utilities, including translation and reference generation.
- `mkdocs.yml`: Documentation site configuration.
- `Makefile`: Common developer commands.
- `pyproject.toml`, `uv.lock`: Python dependencies and tool configuration.
- `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`: Pull request template to use when opening PRs.
- `.agents/references/`: Durable SDK maintainer architecture references. Start with [the reference map](.agents/references/README.md) and open only the files relevant to the affected runtime boundary.
- `site/`: Built documentation output.

### Agents Core Runtime Guidelines

- For `Agent` fields, cloning, dynamic instructions, enabled tools or handoffs, output schemas, run context wrappers, usage aggregation, or public-versus-internal agent identity, read [Agent definition and run context](.agents/references/agent-definition-and-run-context.md).
- `src/agents/run.py` is the runtime entrypoint (`Runner`, `AgentRunner`). Keep it focused on orchestration and public flow control. Put new runtime logic under `src/agents/run_internal/` and import it into `run.py`.
- When `run.py` grows, refactor helpers into `run_internal/` modules (for example `run_loop.py`, `turn_resolution.py`, `tool_execution.py`, `session_persistence.py`) and leave only wiring and composition in `run.py`.
- For turn accounting, guardrail ordering, handoffs, interruptions, cancellation, hooks, or streaming behavior, read [Runner lifecycle](.agents/references/runner-lifecycle.md). Keep streaming and non-streaming paths behaviorally aligned.
- For new model output, tool call, approval, or run item variants, read [Run item lifecycle](.agents/references/run-item-lifecycle.md) and update every applicable processing, event, replay, persistence, tracing, and serialization surface.
- For function-tool parameter schemas, `Annotated` or `Field` metadata, strict JSON schema conversion, or structured output schemas, read [Function and output schema](.agents/references/function-and-output-schema.md).
- For function-tool naming, namespacing, lookup, approvals, tracing, or call-ID changes, read [Tool identity and routing](.agents/references/tool-identity.md) and use the canonical helpers in `src/agents/_tool_identity.py` instead of adding local normalization rules.
- For function-tool planning, approval ordering, tool guardrails, concurrency, cancellation, timeouts, hooks, or failure conversion, read [Tool execution lifecycle](.agents/references/tool-execution-lifecycle.md).
- For local MCP connection ownership, `MCPServerManager`, request serialization, tool caching or filtering, transport retries, cancellation, or cleanup, read [Local MCP server lifecycle](.agents/references/local-mcp-server-lifecycle.md).
- For trace or span context, processors, export, flush, shutdown, sensitive data, or resumed trace state, read [Tracing lifecycle](.agents/references/tracing-lifecycle.md).
- For `RealtimeSession` lifecycle, background-task, handoff, listener, connection, or cleanup changes, read [Realtime session lifecycle](.agents/references/realtime-session-lifecycle.md) and verify both normal and failure-path resource ownership.
- For `VoicePipeline`, streamed audio input, STT session ownership, TTS task ordering, voice lifecycle events, PCM framing, or voice tracing changes, read [Voice pipeline lifecycle](.agents/references/voice-pipeline-lifecycle.md).
- For server-managed conversation (`conversation_id`, `previous_response_id`, `auto_previous_response_id`), read [Conversation state ownership](.agents/references/conversation-state-ownership.md) before changing continuation, filtering, retry, compaction, handoffs, or resume behavior.
- For client-managed session input, per-turn saves, retry rewind, backend atomicity, or compaction replacement, read [Session persistence](.agents/references/session-persistence.md).
- For model resolution, `ModelSettings`, provider adapters, Responses versus Chat Completions capabilities, request conversion, terminal events, transport reuse, or model retries, read [Model and provider boundaries](.agents/references/model-provider-boundaries.md).
- If the serialized `RunState` shape changes, read [RunState schema and resume boundary](.agents/references/runstate-schema.md) and follow its release-boundary, schema-version, backward-read, and regression-test rules.
- For sandbox session ownership, agent preparation, manifests, host-path materialization, snapshots, resume state, or cleanup, read [Sandbox runtime boundary](.agents/references/sandbox-runtime-boundary.md).

## Operation Guide

### Prerequisites

- Python 3.10+.
- `uv` installed for dependency management (`uv sync`) and `uv run` for Python commands.
- `make` available to run repository tasks.

### Development Workflow

1. Stay in the user's current checkout and on the current branch unless the user explicitly asks for or approves a Git state change.
2. If the user explicitly requests a feature/fix branch, create one with a descriptive name:
   ```bash
   git checkout -b feat/<short-description>
   ```
3. If dependencies changed or you are setting up the repo, run `make sync`.
4. Implement changes and add or update tests alongside code updates.
5. Highlight compatibility or API risks in your plan before implementing changes that alter the latest released behavior or a released or explicitly supported durable external state boundary.
6. Build docs when you touch documentation:
   ```bash
   make build-docs
   ```
7. When `$code-change-verification` applies, run it to execute the full verification stack before marking work complete.
8. Commit with concise, imperative messages; keep commits small and focused, then open a pull request.
9. Before reporting eligible code changes as complete, invoke `$pr-draft-summary` as the final handoff step unless the task falls under the documented skip cases. Do not omit it based on perceived change size or because the work remains local or uncommitted.

### Testing & Automated Checks

Before submitting changes, ensure relevant checks pass and extend tests when you touch code.

Before adding or changing async, retry, timeout, subprocess, PTY, warning, or xdist-sensitive tests, read [Performance and determinism](tests/README.md#performance-and-determinism) and preserve the applicable behavioral and lifecycle coverage while optimizing execution.

When `$code-change-verification` applies, run it to execute the required verification stack from the repository root. Rerun the full stack after applying fixes.

#### Unit tests and type checking

- Run the full test suite:
  ```bash
  make tests
  ```
- Run a focused test:
  ```bash
  uv run pytest -s -k <pattern>
  ```
- Type checking:
  ```bash
  make typecheck
  ```

#### Snapshot tests

Some tests rely on inline snapshots; see `tests/README.md` for details. Re-run `make tests` after updating snapshots.

- Fix snapshots:
  ```bash
  make snapshots-fix
  ```
- Create new snapshots:
  ```bash
  make snapshots-create
  ```

#### Coverage

- Generate coverage (fails if coverage drops below threshold):
  ```bash
  make coverage
  ```

#### Formatting, linting, and type checking

- Formatting and linting use `ruff`; run `make format` (applies fixes) and `make lint` (checks only).
- Type hints must pass `make typecheck`.
- Write comments as full sentences ending with a period.
- Imports are managed by Ruff and should stay sorted.
- Do not hard-wrap prose in Markdown or other non-code text files at a fixed column width. Keep each paragraph on one source line unless the file format or Markdown structure requires a line break, such as for lists, tables, blockquotes, or code fences.

#### Mandatory local run order

When `$code-change-verification` applies, run the full sequence in order (or use the skill scripts):

```bash
make format
make lint
make typecheck
make tests
```

### Utilities & Tips

- Install or refresh development dependencies:
  ```bash
  make sync
  ```
- Run tests against the oldest supported version (Python 3.10) in an isolated environment:
  ```bash
  UV_PROJECT_ENVIRONMENT=.venv_310 uv sync --python 3.10 --all-extras --all-packages --group dev
  UV_PROJECT_ENVIRONMENT=.venv_310 uv run --python 3.10 -m pytest
  ```
- Documentation workflows:
  ```bash
  make build-docs      # build docs after editing docs
  make serve-docs      # preview docs locally
  make build-full-docs # run translations and build
  ```
- Snapshot helpers:
  ```bash
  make snapshots-fix
  make snapshots-create
  ```
- Use `examples/` to see common SDK usage patterns.
- Review `Makefile` for common commands and use `uv run` for Python invocations.
- Explore `docs/` and `docs/scripts/` to understand the documentation pipeline.
- Consult `tests/README.md` for test and snapshot workflows.
- Check `mkdocs.yml` to understand how docs are organized.

### Pull Request & Commit Guidelines

- Use the template at `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`; include a summary, test plan, and issue number if applicable.
- In copy-ready GitHub text, use native issue and pull-request references: exactly `#123` for this repository and `owner/repo#123` for another repository. Do not qualify same-repository references as `openai/openai-agents-python#123`. Preserve closing forms such as `Fixes #123` or `Resolves #123`. Never wrap these references in Markdown links such as `[PR #123](https://github.com/owner/repo/pull/123)` or `[#123](...)`; those Codex-friendly links require manual cleanup after pasting into GitHub. Use descriptive Markdown links only for external resources or GitHub targets that cannot be expressed as a native issue or pull-request reference.
- Add tests for new behavior when feasible. Update documentation for user-facing changes, except unreleased-behavior documentation that must follow the separate docs-only pull request policy above.
- Run `make format`, `make lint`, `make typecheck`, and `make tests` before marking work ready.
- Commit messages should be concise and written in the imperative mood. Small, focused commits are preferred.

## Code Review Rules

- Use `$implementation-strategy` to establish the requested outcome and latest released compatibility boundary before judging implementation scope or architecture.
- Treat added complexity as an actionable finding only when specific machinery is not required by the task, a released contract, supported durable state, or a verified runtime or platform risk. Identify the unnecessary machinery and recommend the smallest safe removal or direct replacement.
- Do not request speculative abstractions, general-purpose helpers, configuration knobs, dependencies, compatibility layers, feature flags, parallel code paths, or extensibility for hypothetical future consumers.
- Do not process a sequence of related review comments as independent local fixes when they expose the same missing boundary. Classify them together, decide whether the disputed shapes belong to the supported contract, and prefer one narrowing redesign over accumulating branches.
- Review the complete diff from the merge base of the intended target branch, or from the latest release tag when it is the compatibility baseline, not only the latest incremental fix. Passing tests do not justify branch-local machinery that no longer matches the original requirement.
- Keep findings scoped to the patch. Do not block on unrelated cleanup, pre-existing bugs, or optional refactors; report them separately when useful.
- Require a broader refactor only when concrete evidence shows the focused change would otherwise be incorrect, unsafe, incompatible, or materially harder to maintain.

### Baseline review expectations

- ✅ Checks pass (`make format`, `make lint`, `make typecheck`, `make tests`).
- ✅ Tests cover new behavior and edge cases.
- ✅ Code is readable, maintainable, and consistent with existing style.
- ✅ Examples are updated if behavior changes.
- ✅ History is clean with a clear PR description.
