from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agents import RunConfig
from agents.run_config import SandboxRunConfig
from agents.sandbox import SandboxAgent
from agents.sandbox.entries import Dir, File, LocalDir, LocalFile
from agents.sandbox.manifest import Manifest


@pytest.fixture(params=["sandbox_config", "run_config", "agent"])
def configure_manifest(request: pytest.FixtureRequest) -> Callable[[Any], Manifest]:
    def configure(value: Any) -> Manifest:
        if request.param == "sandbox_config":
            manifest = SandboxRunConfig(manifest=value).manifest
        elif request.param == "run_config":
            config = RunConfig(sandbox={"manifest": value})
            assert config.sandbox is not None
            manifest = config.sandbox.manifest
        else:
            manifest = SandboxAgent(name="test", default_manifest=value).default_manifest
        assert isinstance(manifest, Manifest)
        return manifest

    return configure


@pytest.mark.parametrize("entry_type", ["local_file", "local_dir"])
@pytest.mark.parametrize("nested", [False, True])
def test_dictionary_manifest_rejects_host_sources(
    configure_manifest: Callable[[Any], Manifest], entry_type: str, nested: bool
) -> None:
    entry: dict[str, Any] = {"type": entry_type, "src": ".env"}
    if nested:
        entry = {"type": "dir", "children": {"copied": entry}}

    with pytest.raises(TypeError, match="must be configured on a trusted Manifest"):
        configure_manifest({"entries": {"output": entry}})


def test_dictionary_manifest_rejects_typed_host_source_entries(
    configure_manifest: Callable[[Any], Manifest],
) -> None:
    with pytest.raises(TypeError, match="must be configured on a trusted Manifest"):
        configure_manifest({"entries": {"output": Dir(children={"copied": LocalFile(src=".env")})}})


def test_configuration_preserves_trusted_manifest_host_sources(
    configure_manifest: Callable[[Any], Manifest],
) -> None:
    manifest = Manifest(
        entries={
            "file": LocalFile(src="input.txt"),
            "nested": Dir(children={"directory": LocalDir(src="data")}),
        }
    )

    assert configure_manifest(manifest) is manifest
    file = manifest.entries["file"]
    assert isinstance(file, LocalFile)
    assert file.src == Path("input.txt")


def test_dictionary_manifest_preserves_entries_without_host_sources(
    configure_manifest: Callable[[Any], Manifest],
) -> None:
    manifest = configure_manifest(
        {
            "entries": {
                "nested": {
                    "type": "dir",
                    "children": {"hello.txt": {"type": "file", "content": "hello"}},
                },
                "empty": {"type": "local_dir"},
            }
        }
    )

    directory = manifest.entries["nested"]
    assert isinstance(directory, Dir)
    file = directory.children["hello.txt"]
    assert isinstance(file, File)
    assert file.content == b"hello"
    empty = manifest.entries["empty"]
    assert isinstance(empty, LocalDir)
    assert empty.src is None
