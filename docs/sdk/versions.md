# Releases and documentation

Match the guides and example files to your installed SDK version. These guides follow the `feat/codex-harness` development branch, which can change between releases.

| Installed SDK | Documentation and examples |
| --- | --- |
| [0.24.0-alpha.7](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0-alpha.7) wheel | Use [the alpha.7 setup and quickstart](https://github.com/openai/openai-agents-python/blob/247c22dfbadb7e9c07345f9831a06729faa403aa/docs/sdk/index.md) and the alpha.7 source archive for examples. The original tag contains older installation guidance. |
| A development build from `feat/codex-harness` | Use guides and example files from the same source commit as the build. |

See [installation](installation.md) for platform requirements, wheel commands, and matching example files.

The release page contains the versioned downloads, checksums, and release notes. This preview is distributed through GitHub Releases; it has not been published to PyPI.

## Machine-readable topic index

[topics.json](topics.json) lists package metadata and task aliases for coding agents. File paths are relative to this SDK guide's directory. `release` identifies the published tag; `published_release` links to its downloads and release notes. `published_guide` links to the corrected setup and quickstart for that release. The original release tag remains unchanged.

`source_ref` identifies the development branch. Fetch the index and linked files from one pinned commit to keep them consistent. Contributor build instructions are in [CONTRIBUTING.md](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md).
