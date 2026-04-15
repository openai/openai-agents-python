# TEMPORARY: Patch helpers for unreleased Temporal OpenAI Agents plugin sandbox support.
# Remove this file (and _vendored_plugin/) once temporalio ships with sandbox support baked in
# (i.e. `temporalio.contrib.openai_agents.sandbox` exists in the released package).

# Vendored plugin files checked into this repo
_plugin_src := justfile_directory() / "_vendored_plugin"

# Patch the installed temporalio package with local plugin changes
[private]
patch:
    #!/usr/bin/env bash
    set -euo pipefail
    plugin_dst="$(uv run python -c "import temporalio, os; print(os.path.join(os.path.dirname(temporalio.__file__), 'contrib', 'openai_agents'))")"
    patch_marker="$plugin_dst/.patched"
    if [ ! -f "$patch_marker" ]; then
        echo "Patching installed temporalio plugin from vendored source..."
        cp "{{_plugin_src}}"/*.py "$plugin_dst/"
        cp -r "{{_plugin_src}}/sandbox" "$plugin_dst/"
        touch "$patch_marker"
        echo "Done. Plugin patched with sandbox support."
    fi

# Force re-patch (e.g. after updating vendored files)
[private]
repatch: unpatch patch

# Restore the installed temporalio plugin to its original state
[private]
unpatch:
    @echo "Restoring original temporalio plugin..."
    @uv pip install --reinstall --no-deps temporalio
    @echo "Done. Plugin restored."
