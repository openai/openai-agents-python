from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import cast

import pytest

from agents.sandbox import Manifest
from agents.sandbox.capabilities import LocalDirLazySkillSource, Skill, Skills
from agents.sandbox.entries import Dir, File, LocalDir
from agents.sandbox.errors import SkillsConfigError
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, Permissions, User
from agents.sandbox.workspace_paths import coerce_posix_path
from agents.tool import FunctionTool
from agents.tool_context import ToolContext
from tests.utils.factories import TestSessionState


def _children_keys(entry: Dir) -> set[str]:
    return {coerce_posix_path(key).as_posix() for key in entry.children}


def _user_name(user: object) -> str | None:
    if user is None:
        return None
    if isinstance(user, User):
        return user.name
    if isinstance(user, str):
        return user
    return str(user)


class _SkillsSession(BaseSandboxSession):
    def __init__(self, manifest: Manifest) -> None:
        self.state = TestSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self.read_users: list[str | None] = []
        self.write_users: list[str | None] = []
        self.mkdir_users: list[str | None] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def running(self) -> bool:
        return True

    async def read(self, path: Path, *, user: object = None) -> io.BytesIO:
        self.read_users.append(_user_name(user))
        normalized = self.normalize_path(path)
        return io.BytesIO(normalized.read_bytes())

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        self.write_users.append(_user_name(user))
        normalized = self.normalize_path(path)
        normalized.parent.mkdir(parents=True, exist_ok=True)
        payload = data.read()
        if isinstance(payload, str):
            normalized.write_text(payload, encoding="utf-8")
        else:
            normalized.write_bytes(bytes(payload))

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = (command, timeout)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: object = None,
    ) -> None:
        self.mkdir_users.append(_user_name(user))
        normalized = self.normalize_path(path)
        normalized.mkdir(parents=parents, exist_ok=True)

    async def ls(
        self,
        path: Path | str,
        *,
        user: object = None,
    ) -> list[FileEntry]:
        _ = user
        normalized = self.normalize_path(path)
        if not normalized.exists():
            raise FileNotFoundError(normalized)
        entries: list[FileEntry] = []
        for child in sorted(normalized.iterdir(), key=lambda entry: entry.name):
            stat_result = child.stat()
            entries.append(
                FileEntry(
                    path=str(child),
                    permissions=Permissions.from_mode(stat_result.st_mode),
                    owner="owner",
                    group="group",
                    size=stat_result.st_size,
                    kind=EntryKind.DIRECTORY if child.is_dir() else EntryKind.FILE,
                )
            )
        return entries


class TestSkillValidation:
    def test_rejects_directory_content_artifact(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skill(name="my-skill", description="desc", content=Dir())

    def test_rejects_duplicate_script_paths_after_normalization(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skill(
                name="my-skill",
                description="desc",
                content="literal",
                scripts={
                    "run.sh": File(content=b"echo one"),
                    Path("run.sh"): File(content=b"echo two"),
                },
            )


class TestSkillsValidation:
    def test_requires_at_least_one_source(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skills()

    def test_rejects_non_directory_from_artifact(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skills(from_=File(content=b"not-a-dir"))

    def test_rejects_duplicate_skill_names(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skills(
                skills=[
                    Skill(name="dup", description="first", content="a"),
                    Skill(name="dup", description="second", content="b"),
                ]
            )

    def test_rejects_combining_literal_and_from_sources(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skills(
                from_=Dir(
                    children={"my-skill": Dir(children={"SKILL.md": File(content=b"imported")})}
                ),
                skills=[Skill(name="my-skill", description="desc", content="literal")],
            )

    def test_rejects_combining_literal_and_lazy_sources(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skills(
                skills=[Skill(name="my-skill", description="desc", content="literal")],
                lazy_from=LocalDirLazySkillSource(source=LocalDir(src=Path("skills"))),
            )

    def test_rejects_absolute_skills_path(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skills(
                skills=[Skill(name="my-skill", description="desc", content="literal")],
                skills_path="/skills",
            )

    def test_rejects_windows_drive_absolute_skills_path(self) -> None:
        with pytest.raises(SkillsConfigError) as exc_info:
            Skills(
                skills=[Skill(name="my-skill", description="desc", content="literal")],
                skills_path="C:\\skills",
            )

        assert exc_info.value.context == {
            "field": "skills_path",
            "path": "C:/skills",
            "reason": "absolute",
        }

    def test_rejects_escape_root_skills_path(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skills(
                skills=[Skill(name="my-skill", description="desc", content="literal")],
                skills_path="../skills",
            )


class TestSkillsManifest:
    def test_literals_materialize_full_skill_structure(self) -> None:
        capability = Skills(
            skills=[
                Skill(
                    name="my-skill",
                    description="desc",
                    content="Use this skill.",
                    scripts={"run.sh": File(content=b"echo run")},
                    references={"docs/readme.md": File(content=b"ref")},
                    assets={"images/icon.txt": File(content=b"asset")},
                )
            ]
        )

        processed = capability.process_manifest(Manifest(root="/workspace"))
        skill_entry = processed.entries[Path(".agents/my-skill")]
        assert isinstance(skill_entry, Dir)
        assert _children_keys(skill_entry) == {"SKILL.md", "assets", "references", "scripts"}

        scripts = skill_entry.children["scripts"]
        assert isinstance(scripts, Dir)
        assert _children_keys(scripts) == {"run.sh"}

        references = skill_entry.children["references"]
        assert isinstance(references, Dir)
        assert _children_keys(references) == {"docs/readme.md"}

        assets = skill_entry.children["assets"]
        assert isinstance(assets, Dir)
        assert _children_keys(assets) == {"images/icon.txt"}

    def test_from_source_is_mapped_to_skills_root(self) -> None:
        source = Dir(children={"imported": Dir(children={"SKILL.md": File(content=b"imported")})})
        capability = Skills(from_=source)

        processed = capability.process_manifest(Manifest(root="/workspace"))
        assert processed.entries[Path(".agents")] is source

    def test_local_dir_from_source_stays_eager_by_default(self, tmp_path: Path) -> None:
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

        capability = Skills(from_=LocalDir(src=src_root))

        processed = capability.process_manifest(Manifest(root="/workspace"))
        assert processed.entries[Path(".agents")].type == "local_dir"

    def test_lazy_local_dir_source_skips_manifest_materialization(self, tmp_path: Path) -> None:
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

        capability = Skills(
            lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)),
        )

        processed = capability.process_manifest(Manifest(root="/workspace"))
        assert processed.entries == {}

    def test_lazy_local_dir_rejects_overlapping_manifest_entries(self, tmp_path: Path) -> None:
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

        capability = Skills(
            lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)),
        )
        manifest = Manifest(
            root="/workspace",
            entries={Path(".agents"): Dir()},
        )

        with pytest.raises(SkillsConfigError) as exc_info:
            capability.process_manifest(manifest)

        assert exc_info.value.message == "skills lazy_from path overlaps existing manifest entries"
        assert exc_info.value.context == {
            "path": ".agents",
            "source": "lazy_from",
            "overlaps": [".agents"],
        }

    def test_literal_skills_allow_existing_manifest_entry_when_content_matches(self) -> None:
        capability = Skills(
            skills=[
                Skill(
                    name="my-skill",
                    description="desc",
                    content="Use this skill.",
                    scripts={"run.sh": File(content=b"echo run")},
                )
            ]
        )
        rendered_skill = capability.skills[0].as_dir_entry()
        manifest = Manifest(
            root="/workspace",
            entries={".agents/my-skill": rendered_skill},
        )

        processed = capability.process_manifest(manifest)

        assert processed is manifest
        assert processed.entries[".agents/my-skill"] == rendered_skill

    def test_process_manifest_rejects_exact_path_collision(self) -> None:
        capability = Skills(skills=[Skill(name="my-skill", description="desc", content="literal")])
        manifest = Manifest(root="/workspace", entries={Path(".agents/my-skill"): Dir()})

        with pytest.raises(SkillsConfigError):
            capability.process_manifest(manifest)

    def test_custom_skills_path_is_used_for_manifest_entries(self) -> None:
        capability = Skills(
            skills=[Skill(name="my-skill", description="desc", content="literal")],
            skills_path=".sandbox/skills",
        )

        processed = capability.process_manifest(Manifest(root="/workspace"))

        assert processed.entries[Path(".sandbox/skills/my-skill")] == (
            capability.skills[0].as_dir_entry()
        )


class TestSkillsInstructions:
    @pytest.mark.asyncio
    async def test_instructions_include_root_and_literal_index(self) -> None:
        capability = Skills(
            skills=[
                Skill(name="z-skill", description="z description", content="z"),
                Skill(name="a-skill", description="a description", content="a"),
            ]
        )

        instructions = await capability.instructions(Manifest(root="/workspace"))
        assert instructions is not None
        assert instructions.startswith("## Skills\n")
        assert "### Available skills" in instructions
        assert "### How to use skills" in instructions
        assert "- a-skill: a description (file: .agents/a-skill)" in instructions
        assert "- z-skill: z description (file: .agents/z-skill)" in instructions
        assert instructions.index(
            "- a-skill: a description (file: .agents/a-skill)"
        ) < instructions.index("- z-skill: z description (file: .agents/z-skill)")

    @pytest.mark.asyncio
    async def test_instructions_use_custom_skills_path(self) -> None:
        capability = Skills(
            skills=[Skill(name="my-skill", description="desc", content="literal")],
            skills_path=".sandbox/skills",
        )

        instructions = await capability.instructions(Manifest(root="/workspace"))

        assert instructions is not None
        assert "- my-skill: desc (file: .sandbox/skills/my-skill)" in instructions

    @pytest.mark.asyncio
    async def test_instructions_return_none_when_metadata_is_empty(self) -> None:
        capability = Skills(from_=Dir())

        instructions = await capability.instructions(Manifest(root="/workspace"))
        assert instructions is None

    @pytest.mark.asyncio
    async def test_instructions_resolve_from_runtime_frontmatter(self, tmp_path: Path) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        capability = Skills(
            from_=Dir(
                children={
                    "dynamic-skill": Dir(
                        children={
                            "SKILL.md": File(
                                content=(
                                    b"---\n"
                                    b"name: discovered-skill\n"
                                    b"description: loaded from runtime frontmatter\n"
                                    b"---\n\n"
                                    b"# Skill\n"
                                )
                            )
                        }
                    )
                }
            )
        )
        manifest = capability.process_manifest(Manifest(root=str(workspace_root)))
        session = _SkillsSession(manifest)
        await session.apply_manifest()
        capability.bind(session)

        instructions = await capability.instructions(session.state.manifest)

        assert instructions is not None
        assert (
            "- discovered-skill: loaded from runtime frontmatter (file: .agents/dynamic-skill)"
        ) in instructions

    @pytest.mark.asyncio
    async def test_instructions_resolve_opt_in_lazy_local_dir_metadata(
        self, tmp_path: Path
    ) -> None:
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: discovered-skill\ndescription: local dir metadata\n---\n# Skill\n",
            encoding="utf-8",
        )

        capability = Skills(
            lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)),
        )

        instructions = await capability.instructions(Manifest(root="/workspace"))

        assert instructions is not None
        assert (
            "- discovered-skill: local dir metadata (file: .agents/dynamic-skill)" in instructions
        )
        assert "Call `load_skill` with a single skill name from the list" in instructions
        assert "loaded on demand instead of being present up front" in instructions

    @pytest.mark.asyncio
    async def test_lazy_local_dir_load_skill_tool_materializes_single_skill(
        self, tmp_path: Path
    ) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# dynamic skill\n", encoding="utf-8")

        capability = Skills(
            lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)),
        )
        manifest = capability.process_manifest(Manifest(root=str(workspace_root)))
        assert manifest.entries == {}

        session = _SkillsSession(manifest)
        capability.bind(session)
        tool = cast(FunctionTool, capability.tools()[0])

        with pytest.raises(FileNotFoundError):
            await session.read(Path(".agents/dynamic-skill/SKILL.md"))

        output = await tool.on_invoke_tool(
            cast(ToolContext[object], None),
            '{"skill_name":"dynamic-skill"}',
        )

        assert output == {
            "status": "loaded",
            "skill_name": "dynamic-skill",
            "path": ".agents/dynamic-skill",
        }
        loaded_skill = workspace_root / ".agents" / "dynamic-skill" / "SKILL.md"
        assert loaded_skill.read_text(encoding="utf-8") == "# dynamic skill\n"


class TestSkillsLazyLoading:
    def test_tools_returns_empty_without_lazy_source(self) -> None:
        capability = Skills(skills=[Skill(name="my-skill", description="desc", content="literal")])

        assert capability.tools() == []

    def test_lazy_tools_require_bound_session(self, tmp_path: Path) -> None:
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
        capability = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)))

        with pytest.raises(ValueError, match="Skills is not bound to a SandboxSession"):
            capability.tools()

    def test_lazy_tools_expose_load_skill_after_bind(self, tmp_path: Path) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
        capability = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)))
        capability.bind(_SkillsSession(Manifest(root=str(workspace_root))))

        tools = capability.tools()

        assert len(tools) == 1
        assert isinstance(tools[0], FunctionTool)
        assert tools[0].name == "load_skill"

    @pytest.mark.asyncio
    async def test_load_skill_rejects_non_lazy_capability(self) -> None:
        capability = Skills(skills=[Skill(name="my-skill", description="desc", content="literal")])

        with pytest.raises(SkillsConfigError):
            await capability.load_skill("my-skill")

    @pytest.mark.asyncio
    async def test_load_skill_returns_already_loaded_for_existing_materialized_skill(
        self, tmp_path: Path
    ) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# dynamic skill\n", encoding="utf-8")
        capability = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)))
        session = _SkillsSession(Manifest(root=str(workspace_root)))
        capability.bind(session)
        await session.write(
            Path(".agents/dynamic-skill/SKILL.md"),
            io.BytesIO(b"# already loaded\n"),
        )

        output = await capability.load_skill("dynamic-skill")

        assert output == {
            "status": "already_loaded",
            "skill_name": "dynamic-skill",
            "path": ".agents/dynamic-skill",
        }

    @pytest.mark.asyncio
    async def test_load_skill_materializes_with_bound_run_as_user(self, tmp_path: Path) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# dynamic skill\n", encoding="utf-8")

        capability = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)))
        session = _SkillsSession(Manifest(root=str(workspace_root)))
        capability.bind(session)
        capability.bind_run_as(User(name="sandbox-user"))

        output = await capability.load_skill("dynamic-skill")

        assert output == {
            "status": "loaded",
            "skill_name": "dynamic-skill",
            "path": ".agents/dynamic-skill",
        }
        assert session.read_users == ["sandbox-user"]
        assert session.write_users == ["sandbox-user"]
        assert session.mkdir_users
        assert set(session.mkdir_users) == {"sandbox-user"}

    @pytest.mark.asyncio
    async def test_load_skill_rejects_missing_lazy_source_directory(self, tmp_path: Path) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        capability = Skills(
            lazy_from=LocalDirLazySkillSource(source=LocalDir(src=tmp_path / "missing-skills"))
        )
        capability.bind(_SkillsSession(Manifest(root=str(workspace_root))))

        with pytest.raises(SkillsConfigError):
            await capability.load_skill("missing-skill")

    @pytest.mark.asyncio
    async def test_load_skill_rejects_ambiguous_skill_name(self, tmp_path: Path) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        src_root = tmp_path / "skills"
        first_dir = src_root / "skill-one"
        second_dir = src_root / "skill-two"
        first_dir.mkdir(parents=True)
        second_dir.mkdir(parents=True)
        (first_dir / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: first\n---\n# Skill\n",
            encoding="utf-8",
        )
        (second_dir / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: second\n---\n# Skill\n",
            encoding="utf-8",
        )
        capability = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)))
        capability.bind(_SkillsSession(Manifest(root=str(workspace_root))))

        with pytest.raises(SkillsConfigError):
            await capability.load_skill("shared-skill")

    @pytest.mark.asyncio
    async def test_lazy_metadata_cache_is_reset_on_bind(self, tmp_path: Path) -> None:
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        src_root = tmp_path / "skills"
        skill_dir = src_root / "dynamic-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            "---\nname: cached-skill\ndescription: old description\n---\n# Skill\n",
            encoding="utf-8",
        )
        capability = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=src_root)))

        first_instructions = await capability.instructions(Manifest(root=str(workspace_root)))
        skill_md.write_text(
            "---\nname: cached-skill\ndescription: new description\n---\n# Skill\n",
            encoding="utf-8",
        )
        second_instructions = await capability.instructions(Manifest(root=str(workspace_root)))
        capability.bind(_SkillsSession(Manifest(root=str(workspace_root))))
        third_instructions = await capability.instructions(Manifest(root=str(workspace_root)))

        assert first_instructions is not None
        assert second_instructions is not None
        assert third_instructions is not None
        assert "- cached-skill: old description (file: .agents/dynamic-skill)" in first_instructions
        assert (
            "- cached-skill: old description (file: .agents/dynamic-skill)" in second_instructions
        )
        assert "- cached-skill: new description (file: .agents/dynamic-skill)" in third_instructions
