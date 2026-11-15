# Releases and documentation

Use the guides and examples for the SDK version in your Python environment. To check your version, run `python -m pip show openai-agents`.

| Preview release | Guides | Examples |
| --- | --- | --- |
| [0.24.0-alpha.7](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0-alpha.7) | [Install](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/installation.md), [Quickstart](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/index.md), [Test without an API key](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/testing.md) | [Browse examples](https://github.com/openai/openai-agents-python/tree/v0.24.0-alpha.7/examples/codex) |

The installation guide for each release explains how to download and run its examples.

Each preview release includes its wheel downloads, checksums, and release notes.

If you [build the SDK from source](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md#build-the-sdk-from-source), use the guides and examples from the same commit as your build.

## Machine-readable topic index

[topics.json](topics.json) lists package metadata and task aliases for coding agents. File paths are relative to this SDK guide's directory. `package_version` and `release` identify the version targeted by these source files; check `status` before treating that version as published. `published_release` links to the available release's downloads and notes, and `published_guide` links to its corrected setup and quickstart. The original release tag remains unchanged.

`source_ref` identifies the development branch. Fetch the index and linked files from one pinned commit to keep them consistent. Contributor build instructions are in [CONTRIBUTING.md](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md).
