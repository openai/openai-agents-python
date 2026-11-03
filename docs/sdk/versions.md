# Choose the docs for your SDK version

These guides describe the public `v1` source branch. This snapshot has no public v1 wheels or release tags. The published `openai-agents` package remains v0, with a different runtime and API.

| SDK | Installation | Guide and examples |
| --- | --- | --- |
| Public v0.24.0 | `python -m pip install openai-agents==0.24.0` in a separate v0 environment | [V0 documentation](https://openai.github.io/openai-agents-python/) and [v0.24.0 source](https://github.com/openai/openai-agents-python/tree/v0.24.0) |
| V1 source preview | [Build instructions](https://github.com/openai/openai-agents-python/blob/v1/docs/installation.md) | [Quickstart](index.md), [task recipes](recipes.md) and [reference](reference.md) from the same source commit |

For a source build, record `git rev-parse HEAD` and use the documentation and examples from that commit. The package version alone does not identify the source. Keep v0 and v1 in separate environments because both distributions import as `agents`. Read the [migration guide](migration.md) before porting a v0 application.

This source snapshot retains package metadata `0.24.0-alpha.5` (normalized as `0.24.0a5`) from the private-preview lineage. That version does not identify a public v1 release or the exact source commit. The historical private preview v0.24.0-alpha.5 is a separate build, not a public v1 release. Its wheel and documentation do not describe this branch. Existing private-preview users can consult the [API difference table](index.md#migrate-from-alpha5).

## Machine-readable topic index

[topics.json](topics.json) lists version information and task aliases for coding agents. Paths resolve relative to this SDK guide. `status` is `unreleased` and `release` is `null` until a matching v1 version is published. `published_release` and `published_guide` are also `null`: the published v0 SDK uses a different interface.

`source_ref` identifies the v1 branch, which can change. For reproducible work, fetch the index and every linked source file from one pinned Git commit.

Examples that use additional libraries list their required versions and setup commands in their own READMEs.
