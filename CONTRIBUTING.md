# Contributing to the OpenAI Agents Python SDK

## Contribution policy

We welcome bug reports, questions, feature requests, minimal reproductions, and root-cause analysis through [GitHub issues](https://github.com/openai/openai-agents-python/issues).

**Pull requests are limited to repository collaborators. We do not accept pull requests from non-collaborators**, including documentation or example changes. If you are not a collaborator, please open an issue instead of preparing a pull request. Include the affected version, expected and actual behavior, and a small, sanitized reproduction when applicable.

Report suspected security vulnerabilities privately as described in [SECURITY.md](SECURITY.md), rather than in issues or pull requests.

The development and pull request instructions below are for maintainers and repository collaborators.

For suspected vulnerabilities, follow [SECURITY.md](SECURITY.md). Keep undisclosed security reports and fixes out of public issues, discussions, and pull requests until disclosure is coordinated.

## Development workflow

Read [AGENTS.md](AGENTS.md) for the repository's scope, compatibility, review, and verification requirements. Install Make and the [native build prerequisites](packages/codex/MAINTAINING.md#build-from-this-source), including a supported CPython version, `uv`, Git, and Rust, before running `make sync`. That command installs the SDK for contributor work and can compile its native extension. Run Python commands through `uv run`.

Install Git with your operating system's package manager and ensure the `git` executable is on `PATH` before running the test suite. The release-provenance and change-detection tests invoke Git to create and inspect temporary repositories. Git is a system dependency; `make sync` and Python package installers do not install it.

Keep changes focused on the agreed outcome. Add regression coverage for changed behavior and follow [tests/README.md](tests/README.md) for test execution. Run focused checks while developing, then the applicable final checks described in [AGENTS.md](AGENTS.md#testing--automated-checks). Use the [pull request template](.github/PULL_REQUEST_TEMPLATE/pull_request_template.md) to explain the problem, change, and validation. Documentation changes follow the repository's verification tiers and release-timing rules.

## Build the SDK from source

Source builds are for SDK development. Users install prebuilt wheels as described in the [installation guide](docs/sdk/installation.md).

Install standard CPython 3.10–3.14, Git, `uv`, Rust 1.95.0, and the [platform-specific native build tools](packages/codex/MAINTAINING.md#build-from-this-source), including a compiler and Protocol Buffers. The build downloads pinned public Codex and RMCP sources. Python package installers do not install these system tools.

Clone the development branch and record the source commit:

```sh
git clone --branch feat/codex-harness https://github.com/openai/openai-agents-python.git
cd openai-agents-python
git rev-parse HEAD
python3 packages/codex/scripts/prepare_codex.py
uv build --wheel
```

On Windows, use `py -3` instead of `python3`. For source preparation with Python 3.10, install `tomli==2.2.1` in that interpreter's environment first. The wheel build installs its declared Python build dependencies in an isolated environment.

Preparation verifies the pinned native sources. The wheel build compiles the native extension and matching execution helpers, then writes a wheel under `dist/`. Keep the wheel and its build records with the source commit. See the [maintenance guide](packages/codex/MAINTAINING.md) for native build details and troubleshooting.

To check the resulting package, create a fresh virtual environment outside the SDK checkout and install the exact wheel from `dist/` with `python -m pip install` followed by its absolute path. Use that environment to run the [installation check](docs/sdk/installation.md#install-a-preview-wheel). A matching package version alone does not identify a development commit.

## Security checklist

### Credentials and sensitive data

- Use synthetic fixtures and obvious placeholder credentials in tests, examples, snapshots, and documentation. Never commit real API keys, tokens, cookies, signing keys, customer data, private prompts or responses, tool payloads, or recordings.
- Provide credentials for explicitly authorized live tests through the approved environment or secret store. Use the minimum necessary access and keep live credentials out of untrusted contributor runs. Do not embed credentials in browser code, commands, URLs, or generated artifacts.
- Inspect diffs and attachments for sensitive data before sharing them. Include logs, exceptions and their chained context, tracebacks, telemetry, session exports, files, and audio in that check. Do not assume a tracing redaction setting sanitizes every channel.
- If a credential is exposed, stop sharing it, report it privately through [SECURITY.md](SECURITY.md), and have its owner revoke or rotate it. Deleting the visible value alone does not invalidate the credential.

### Dependencies and downloaded tools

- Justify new dependencies and review the package source, maintenance history, install or build hooks, transitive dependencies, and lockfile changes. Keep dependency changes scoped and reproducible.
- Assess dependency alerts for the affected runtime, optional integration, development, example, CI, or publishing path. Record reachability and impact instead of dismissing an alert solely because the dependency is not shipped to users.
- For dependency-update automation, apply a documented release-age cooldown to ordinary version updates while allowing security updates immediately. Review security updates promptly; they still require appropriate review and checks. Do not claim a cooldown or update ecosystem is configured without checking the actual configuration.
- Escalate critical or actively exploited findings immediately through the private security process. Record any proposed exception with an owner, mitigation, approving authority, and expiry; an unapproved exception is not an accepted risk.

### CI and publishing

- Treat pull request content, branch names, artifacts, and external downloads as untrusted input. Do not execute contributor-controlled code in a privileged workflow or expose secrets to it, including through `pull_request_target` or a later workflow that consumes contributor artifacts.
- Use explicit, least-privilege workflow and job permissions, review third-party actions, and pin actions to full commit SHAs. Grant write or `id-token` permissions only to jobs that require them. Do not bypass required reviews, secret protections, or security checks to make CI pass.
- Changes to credentials, redaction, requests and redirects, parsing, uploads, tool approvals, MCP, persisted state, sandbox access, dependencies, CI, or releases need focused security review and regression coverage appropriate to the affected boundary.
- Release approval under the shared SDK policy requires CODEOWNERS coverage of release workflows and publishing configuration, required code-owner review of release pull requests, and passing required checks. A separate environment reviewer gate is not required by that policy. Follow any protections currently configured for this repository; this guidance does not authorize removing or bypassing them.
- Preserve the existing PyPI OIDC publishing flow, release-source validation, and artifact handoff in [the publishing workflow](.github/workflows/publish.yml). Do not replace short-lived trusted publishing with long-lived registry tokens or weaken provenance checks for convenience. Follow the [maintainer release procedure](.github/RELEASING.md).
- When assessing publishing readiness, verify the repository-specific registry binding, artifact provenance, publisher access, and recovery arrangements. Workflow configuration alone does not prove those controls are in place.

These requirements describe how to contribute safely. Their presence does not certify repository settings, establish a scan baseline, or close existing security findings. Maintainers must track verified gaps and approved exceptions separately from proposed work.
