import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agents import Agent
from agents.exceptions import UserError
from agents.plugin_discovery import InstalledPlugin, installed_plugins

PIN = InstalledPlugin(
    distribution="sdk-fixture", version="0.1", name="sdk-test", value="sdk_fixture:plugin"
)


def install(path: Path, name: str, version: str, entries: str) -> Path:
    info = path / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    (info / "entry_points.txt").write_text(f"[openai_agents.plugins]\n{entries}\n")
    (info / "RECORD").write_text("sdk_fixture.py,,\n")
    return info


@pytest.fixture
def info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    (tmp_path / "sdk_fixture.py").write_text(
        "from agents.plugins import Plugin\nplugin = Plugin(name='sdk-test')\n"
    )
    (tmp_path / "sdk_unselected.py").write_text("raise AssertionError('unselected')\n")
    install(tmp_path, "sdk-unselected", "0.1", "unused = sdk_unselected:plugin")
    selected = install(tmp_path, PIN.distribution, PIN.version, f"{PIN.name} = {PIN.value}")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield selected
    sys.modules.pop("sdk_fixture", None)


def test_listing_and_loading_are_separate_from_agent_activation(info: Path) -> None:
    install(info.parent, "sdk-broken", "0.1", "invalid entry without an equals sign")
    entries = installed_plugins()
    assert PIN in entries
    assert any(entry.name == "unused" for entry in entries)
    assert "sdk_fixture" not in sys.modules
    plugin = PIN.load()
    assert Agent(name="enabled", plugins=[plugin]).plugins == (plugin,)
    assert Agent(name="disabled").plugins == ()
    assert "sdk_unselected" not in sys.modules


def test_changed_and_forged_selections_never_import(info: Path) -> None:
    for pin in (replace(PIN, version="0.2"), replace(PIN, distribution="sdk-unselected")):
        with pytest.raises(UserError, match="changed"):
            pin.load()
    (info / "entry_points.txt").write_text("[openai_agents.plugins]\nsdk-test = sdk_fixture:other")
    with pytest.raises(UserError, match="changed"):
        PIN.load()
    assert "sdk_fixture" not in sys.modules


@pytest.mark.parametrize(
    ("name", "entry"),
    [("sdk-other", "sdk-test = sdk_unselected:plugin"), ("sdk_fixture", "")],
)
def test_ambiguous_name_or_install_never_imports(info: Path, name: str, entry: str) -> None:
    install(info.parent, name, "0.2", entry)
    with pytest.raises(UserError, match="[Aa]mbiguous"):
        PIN.load()
    assert "sdk_fixture" not in sys.modules


def test_shadow_module_never_executes(info: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shadow = info.parent / "shadow"
    shadow.mkdir()
    (shadow / "sdk_fixture.py").write_text("raise AssertionError('wrong package')\n")
    monkeypatch.syspath_prepend(str(shadow))
    with pytest.raises(UserError, match="selected distribution"):
        PIN.load()


def test_nested_target_never_imports_a_parent_package(info: Path) -> None:
    nested = replace(PIN, value="sdk_unselected.child:plugin")
    (info / "entry_points.txt").write_text(f"[openai_agents.plugins]\nsdk-test = {nested.value}")
    with pytest.raises(UserError, match="top-level"):
        nested.load()


@pytest.mark.parametrize("export", ["object()", "Plugin(name='other')"])
def test_wrong_export_fails_without_activation(info: Path, export: str) -> None:
    (info.parent / "sdk_fixture.py").write_text(
        f"from agents.plugins import Plugin\nplugin = {export}\n"
    )
    with pytest.raises(UserError, match="Expected a Plugin"):
        PIN.load()
