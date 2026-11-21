"""Regression coverage for the native wheel's local Core patch boundary."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def test_core_patch_applies_inside_an_ancestor_git_checkout(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/build_codex_wheel.py"
    spec = importlib.util.spec_from_file_location("build_codex_wheel", script)
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    upstream = tmp_path / "upstream"
    crate = upstream / "codex-rs/core"
    crate.mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "codex-core"\n')
    (crate / "value.txt").write_bytes(b"stock\n")
    source = tmp_path / "target/staged"
    patches = source / "native/codex/patches"
    patches.mkdir(parents=True)
    (patches / "0001.patch").write_text(
        "diff --git a/codex-rs/core/value.txt b/codex-rs/core/value.txt\n"
        "--- a/codex-rs/core/value.txt\n"
        "+++ b/codex-rs/core/value.txt\n"
        "@@ -1 +1 @@\n-stock\n+patched\n",
        newline="\n",
    )
    metadata = {
        "packages": [
            {
                "name": "codex-core",
                "source": "git+https://github.com/openai/codex?rev=pinned#pinned",
                "manifest_path": str(crate / "Cargo.toml"),
            },
            {"name": "sdk", "source": None},
        ]
    }

    guide = patches / "0001.md"
    for text in (None, "  \n"):
        if text is not None:
            guide.write_text(text)
        with pytest.raises(ValueError, match=r"Document 0001\.patch in 0001\.md"):
            build.patch_codex_source(source, upstream, metadata)

    guide.write_text("Replace the stock value for the build test.")
    build.patch_codex_source(source, upstream, metadata)
    patched = source / "native/codex-upstream"

    assert (patched / "codex-rs/core/value.txt").read_text() == "patched\n"
    assert (crate / "value.txt").read_text() == "stock\n"
    assert json.dumps(str(patched / "codex-rs/core")) in (source / ".cargo/config.toml").read_text()
