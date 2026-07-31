---
name: integration-tests
description: Run the packaged OpenAI Agents Python SDK integration tests from clean wheel and source-distribution environments. Use for release readiness, live OpenAI regression checks, package import compatibility, optional-extra validation, or when asked to run integration tests after examples-auto-run.
---

# Integration Tests

## Overview

Run the release-oriented integration suite against the exact wheel and source distribution produced by `uv build`. The runner installs both artifacts into isolated environments and validates supported imports, optional extras, OpenAI model adapters, hosted tools, Realtime, and voice workflows.

## Execution requirements

- Fresh isolated environments download optional dependencies from PyPI and connect to the configured API providers.
- When the execution environment requires approval for package downloads or configured provider connections, request elevated command execution (`sandbox_permissions=require_escalated`). Retry with the required network permissions before classifying a connectivity failure as an SDK regression.

## Release workflow

Run this command from the repository root:

```bash
env UV_DEFAULT_INDEX=https://pypi.org/simple \
  OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS=1 \
  OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS=0 \
  make integration-tests-release
```

- Use the release profile as the default whenever `$integration-tests` is invoked without a narrower request.
- Use OpenRouter as the standard multi-provider gateway. Add provider-specific direct connections only when the user explicitly requests that additional credential matrix.
- Use existing `OPENAI_API_KEY` and `OPENROUTER_API_KEY` values without printing them. Missing optional service configuration may skip capability-specific tests unless strict mode was explicitly requested.
- The command rebuilds the wheel and source distribution, creates isolated virtual environments, checks public imports and optional dependencies, and runs the release-oriented live suites.
- Do not run watch mode, modify source files, create a branch, commit, push, or open a pull request as part of this skill.

## Paired release validation

When the user requests both pre-release checks, run `$examples-auto-run` first and follow that skill's required per-example behavioral validation. Then run the command above and report the examples and integration outcomes separately. Invoking `$integration-tests` alone does not implicitly start the examples suite.

## Focused commands

Use a focused target only when the user specifically asks to narrow the run:

```bash
env UV_DEFAULT_INDEX=https://pypi.org/simple make integration-tests-packaging
env UV_DEFAULT_INDEX=https://pypi.org/simple make integration-tests-core
env UV_DEFAULT_INDEX=https://pypi.org/simple make integration-tests-providers
env UV_DEFAULT_INDEX=https://pypi.org/simple make integration-tests-hosted
env UV_DEFAULT_INDEX=https://pypi.org/simple make integration-tests-realtime
env UV_DEFAULT_INDEX=https://pypi.org/simple make integration-tests-voice
env UV_DEFAULT_INDEX=https://pypi.org/simple make integration-tests-extras
```

For the minimum supported Python package boundary, use:

```bash
env UV_DEFAULT_INDEX=https://pypi.org/simple \
  OPENAI_AGENTS_INTEGRATION_PYTHON=3.10 \
  make integration-tests-packaging
```

Nightly and manual profiles include additional capability-specific or higher-cost checks. Run them only when explicitly requested; use the configured OpenRouter matrix by default and include direct providers only when explicitly selected.

## Reporting

Report the final pass, fail, skip, and deselection counts for each isolated environment. If a command fails, identify the exact profile, package environment, failing test, and actionable error. Separate product regressions from missing credentials, unsupported hosted features, dependency installation failures, and execution-environment restrictions.
