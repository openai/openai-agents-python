from __future__ import annotations

from pathlib import Path

import pytest

from agents.sandbox import Manifest
from agents.sandbox.capabilities import Skill, Skills
from agents.sandbox.entries import Dir, File
from agents.sandbox.errors import SkillsConfigError


def _children_keys(entry: Dir) -> set[str]:
    return {str(key if isinstance(key, Path) else Path(key)) for key in entry.children}


class TestSkillValidation:
    def test_rejects_directory_content_artifact(self) -> None:
        with pytest.raises(SkillsConfigError):
            Skill(name="my-skill", description="desc", content=Dir())


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
        skill_entry = processed.entries[Path(".agents/skills/my-skill")]
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
        assert processed.entries[Path(".agents/skills")] is source

    def test_literal_skills_are_idempotent_when_manifest_already_contains_same_skill(self) -> None:
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
            entries={".agents/skills/my-skill": rendered_skill},
        )

        processed = capability.process_manifest(manifest)
        assert processed.entries[".agents/skills/my-skill"] == rendered_skill

    def test_process_manifest_rejects_exact_path_collision(self) -> None:
        capability = Skills(skills=[Skill(name="my-skill", description="desc", content="literal")])
        manifest = Manifest(root="/workspace", entries={Path(".agents/skills/my-skill"): Dir()})

        with pytest.raises(SkillsConfigError):
            capability.process_manifest(manifest)


class TestSkillsInstructions:
    @pytest.mark.asyncio
    async def test_instructions_return_none(self) -> None:
        capability = Skills(
            skills=[
                Skill(name="z-skill", description="z description", content="z"),
                Skill(name="a-skill", description="a description", content="a"),
            ]
        )

        instructions = await capability.instructions(Manifest(root="/workspace"))
        assert instructions is None
