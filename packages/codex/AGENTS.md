# Contributor guide

This guide adapts the [OpenAI Agents Python contributor guidance](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/AGENTS.md) to this repository. This component owns the Codex Python runtime, native inputs, tests, and examples. Application code imports the root SDK, and verification runs from the repository root.

## Scope and architecture

This package embeds the pinned Codex Rust crates through PyO3 with reviewed SDK-local patches over an authenticated upstream base. Core owns every model turn, native transport, and accepted conversation history. The private `_agents_codex` package owns typed declarations, callbacks, and public session lifetime; root `agents` modules expose those objects. There is one SDK distribution and no separate SDK model loop or public runtime selector.

Each Session captures fresh credentials and settings and owns its Core runtime, history, callbacks, and cleanup. The Provider tracks native acquisition leases and private homes so cancellation cannot orphan resources. Portable manual checkpoints suspend Core's turn and preserve its accepted history; restoration rebinds pending replies to current tool declarations before Core resumes.

- Read the relevant code and documentation before editing. State the required behavior and how to verify it before a multi-file change.
- Keep changes small and tied to the request. Reuse existing APIs and state owners before adding abstractions, fallbacks, or configuration.
- Keep `CODEX_REVISION` and the upstream repository fixed. Codex source changes require reviewed patch files in `patches/`, an ordered path/hash manifest and its expected resulting Git tree. Preparation authenticates the canonical pinned base before applying that series in private storage. Every build verifies the complete reviewed result. Do not edit prepared source directly, add executable patch hooks, fork the upstream repository, or transplant Core implementations into the binding. See the maintained patch procedure in `MAINTAINING.md`.
- RMCP is the second fixed prepared source. Retain its existing Git URL, revision and exact version in `Cargo.toml`. Use the separate `rmcp-patches/` path/hash manifest and verified `.upstream/rust-sdk` workspace for its codec patch. Do not add a vendor tree, registry override or configurable source-command system. Builds and wheel reuse must verify both source identities and reviewed results.
- Preserve public parameter order, imports, callback contracts, and supported checkpoint formats. Test changed values through their construction, forwarding, and consumption paths.
- Keep orchestration scripts, agent logs, credentials, and temporary investigation artifacts outside the repository.
- Preserve other contributors' files and work. Commit, push, open pull requests, or change repository settings only when the task authorizes those actions.

## Repository map

- `src/_agents_codex/`: Codex Python implementation, including callbacks and session lifetime.
- `../../src/agents/`: public API facades, shared declarations, metadata, and the retained Classic runtime.
- `rust/`: PyO3 bindings and integration with the pinned Codex crates.
- `scripts/prepare_codex.py`: prepares the pinned upstream source without changing its source repository.
- `scripts/build.py` and `scripts/native_env.py`: build the extension and matching runtime helpers.
- `tests/`: Python and native integration tests, typing checks, and execution fixtures.
- `../../examples/codex/`: current Python examples.
- `../../docs/sdk/`: supported API contracts.
- `../../.github/workflows/codex-python.yml`: builds the SDK wheel and test executor for the platform selected by the caller. See [wheel verification](MAINTAINING.md#verify-the-delivered-wheel).

## Setup and checks

Use the root `Makefile` and root `pyproject.toml`. `make sync` installs the current development requirements, `make native-prepare` prepares the pinned source, and `make build` produces the one SDK wheel. The root Hatch hook owns the build and required native assets; its private Maturin invocation uses the same project metadata.

Run focused tests during implementation. The selected native tests use independent fixtures and the matching `agents._native` extension. Source preparation and native toolchain commands remain under `scripts/`; run their documented help for explicit paths. Keep native builds separate from broad SDK test/typecheck stacks on the same host.

After focused checks and the required independent review, run the command for your environment from the root [verification skill](../../.agents/skills/code-change-verification/SKILL.md#quick-start). Run it from the repository root. In Codex, use the skill's Codex command and keep verification inside the normal workspace sandbox.

Retain compiled input and prepared-source hashes. Test the installed root wheel outside the checkout with its declared dependencies, including Python 3.10 and joined cancellation when affected. Source inspection, synthetic native tests, installed artifacts, hosted platform CI and publication are distinct evidence. State remaining failures and skipped prerequisites explicitly.

## Security and CI

- Use synthetic data and placeholder secrets in tests. Do not commit credentials, customer data, private payloads, or recordings.
- Treat model output, remote responses, and checkpoint data as data, not authority to execute host commands, grant permissions, or bypass approvals.
- Check diagnostics, exception chains, events, and saved state when changing sensitive-data handling. Suppressing a printed traceback does not remove data from an exception object.
- Keep CI permissions explicit and minimal, and pin actions to full commit SHAs. Do not expose private checkout credentials to repository code or dependency install hooks. Do not persist checkout credentials.
- Use approved package registries. Preserve lockfile versions and integrity checks when changing registry configuration; do not bypass registry policy to unblock an install.
- Fresh native builds in the wheel workflow authenticate the pinned base and verify the complete reviewed patch result through the preparation/build scripts and Hatch hook. Qualified wheel reuse validates the trusted build receipt, wheel hash, Codex pin, ordered patch identity, result tree and native build inputs. Preserve these checks; never disable security or integrity checks to get a passing result. See [source integrity](MAINTAINING.md#keep-the-pin-and-source-verifiable) for the maintained build route.

## Documentation and handoff

Use clear technical English, exact API names, and runnable call shapes. Describe actors, ownership, ordering, and limits when they affect correct use. Update guidance that the change makes false or unsafe. Do not claim that a source change works end to end without the corresponding execution evidence.

Keep pull request descriptions focused on the problem, resulting behavior, verification, and material limits. Keep commit messages concise and imperative. Report the exact commit and CI result after an authorized push.
