# Build the plugin preview

Build `openai-agents==0.25.0+plugins.preview.2` locally from the plugin branch. For installation and examples, see the [plugin guide](../README.md).

Start at the repository root in a clean checkout. Use Python 3.12 and `uv` in one Bash session. Build both variants from the same Python source without editing the SDK version.

## Python

```bash
preview=examples/plugins/preview
pywheel=dist/python/openai_agents-0.25.0+plugins.preview.2-py3-none-any.whl
uv run --no-project --with hatchling==1.33.0 python "$preview/build.py" --out-dir dist/python
bash examples/plugins/check_installed.sh "$pywheel"
```

Expect a temporary installation of the SDK and all eight examples. Look for `"route": "python"` in the results from the local model fixture. No model API key needed.

## Optional Codex runtime

Use CPython on Linux x86-64 with glibc 2.28 or newer. Start with the Python build above. Download the matching runtime from [the SDK build for PR #6123](https://github.com/openai/openai-agents-python/actions/runs/45484940731) with an authenticated GitHub CLI.

```bash
gh run download 45484940731 --repo openai/openai-agents-python \
  --name codex-wheel-manylinux_2_28_x86_64 --dir dist/runtime
original=dist/runtime/openai_agents-1.0.0a2-cp310-abi3-manylinux_2_28_x86_64.whl
native=dist/codex/openai_agents-0.25.0+plugins.preview.2-cp310-abi3-manylinux_2_28_x86_64.whl
uv run --no-project --with hatchling==1.33.0 python "$preview/build.py" \
  --out-dir dist/codex --codex-wheel "$original"
python "$preview/verify_wheels.py" "$pywheel" "$native" "$original"
bash examples/plugins/check_installed.sh "$native" --codex
```

Use `--codex-wheel` to bundle the native library, executables and licenses without compiling or patching Codex. Expect the build to reject a mismatched SHA-256 or Python provider. Use `verify_wheels.py` to compare the shared SDK files, platform tags, native files and permissions. Look for results from the default Python provider first, then `"route": "codex"` from `CodexProvider`.

**Keep the original wheel.** After the GitHub Actions retention period, reuse a saved copy with SHA-256 `2912ef478fe35a2357616cfd0e0e76eed581230921f0ada1310327b86f0a0a6c`. Avoid the identically named public alpha.2 wheel, built without the branch's native child-agent controls and MCP result transforms. Without the matching artifact, use the Python wheel.

Keep the two previews in separate directories. Install by file path to prevent pip from selecting the platform wheel automatically.
