"""The restored Classic runner discovers runnable modules from the SDK root."""

from pathlib import Path

from examples.classic.run_examples import discover_examples


def test_classic_runner_discovers_modules_from_repository_root():
    examples = discover_examples([])
    modules = {example.module for example in examples}
    assert "examples.classic.basic.hello_world" in modules
    assert "examples.classic.run_examples" not in modules
    assert all(module.startswith("examples.classic.") for module in modules)
    root = Path(__file__).resolve().parents[3]
    assert all(root / example.relpath == example.path for example in examples)


def test_classic_capabilities_manifest_contains_readable_image(tmp_path):
    from agents.classic.sandbox import LocalFile
    from examples.classic.sandbox.sandbox_agent_capabilities import _build_manifest

    manifest = _build_manifest(tmp_path)
    image = manifest.entries["examples/classic/image.png"]
    assert isinstance(image, LocalFile)
    assert image.src.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
