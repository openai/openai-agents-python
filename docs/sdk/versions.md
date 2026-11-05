# Releases and documentation

Match the guides and example files to your installed SDK version. These guides follow the `feat/codex-harness` development branch, which includes APIs absent from the published preview.

| Installed SDK | Documentation and examples |
| --- | --- |
| [0.24.0-alpha.6](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0-alpha.6) wheel | Use [the guides from that release](https://github.com/openai/openai-agents-python/blob/v0.24.0-alpha.6/docs/sdk/index.md) and its source archive for examples. |
| A development build from `feat/codex-harness` | Use guides and example files from the same source commit as the build. |

See [installation](installation.md) for platform requirements, wheel commands, and examples that need a newer build.

The release page contains the versioned downloads, checksums, installation commands, and release notes. This preview is distributed through GitHub Releases; it has not been published to PyPI.

## Machine-readable topic index

[topics.json](topics.json) lists package metadata and task aliases for coding agents. File paths are relative to this SDK guide's directory. `release` identifies the published tag; `published_release` links to its downloads and release notes. `published_guide` links to the guides at that release tag.

`source_ref` identifies the development branch. Fetch the index and linked files from one pinned commit to keep them consistent. Contributor build instructions are in [CONTRIBUTING.md](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md).
