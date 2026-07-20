from pathlib import Path

import pytest

from agents.sandbox.capabilities.skills import LocalDirLazySkillSource
from agents.sandbox.entries import LocalDir
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.workspace_paths import SandboxPathGrant


@pytest.mark.asyncio
async def test_lazy_skill_probe_unwrap_and_materialize(monkeypatch, tmp_path):
    # Prepare a simple host skill layout: skills_root/example-skill/SKILL.md
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "example-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: example-skill\n---\n\n# Example", encoding="utf-8"
    )

    # Build the lazy source pointing at the host directory
    source = LocalDir(src=skills_root)
    lazy = LocalDirLazySkillSource(source=source)

    # Minimal inner session stub (subclass BaseSandboxSession) whose read() raises FileNotFoundError
    class MinimalInner(BaseSandboxSession):
        async def read(self, path, *, user=None):
            raise FileNotFoundError("simulated not found")

        async def write(self, path, data, *, user=None):
            return None

        async def running(self):
            return True

        async def persist_workspace(self):
            raise NotImplementedError

        async def hydrate_workspace(self, data):
            return None

        async def _exec_internal(self, *command, timeout=None):
            raise NotImplementedError

    inner = MinimalInner()
    # Create a wrapper-like object whose class name/module matches the detection logic,
    # and expose the _inner attribute so _unwrap_session_wrapper() will return inner.
    WrapperClass = type(
        "SandboxSession",
        (),
        {"__module__": "agents.sandbox.session.sandbox_session"},
    )
    wrapper = WrapperClass()
    wrapper._inner = inner

    # Monkeypatch LocalDir.apply to a no-op so load_skill can complete without a real session.
    async def _noop_apply(self, session, dest, base_dir=None, user=None):
        return None

    monkeypatch.setattr(LocalDir, "apply", _noop_apply)

    # Provide .state.manifest.root and .extra_path_grants used by load_skill (minimal stand-in).
    # Grant the host skills directory so the absolute source path resolves outside cwd.
    class DummyManifest:
        def __init__(self, root, extra_path_grants):
            self.root = root
            self.extra_path_grants = extra_path_grants

    class DummyState:
        def __init__(self, manifest):
            self.manifest = manifest

    dummy_root = Path("/workspace")
    dummy_manifest = DummyManifest(
        root=str(dummy_root),
        extra_path_grants=(SandboxPathGrant(path=skills_root),),
    )
    wrapper.state = DummyState(manifest=dummy_manifest)
    inner.state = dummy_manifest  # harmless placeholder

    result = await lazy.load_skill(
        skill_name="example-skill", session=wrapper, skills_path=".agents"
    )
    assert result["status"] in {"loaded", "already_loaded"}
