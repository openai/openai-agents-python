"""Native bindings over shared sandbox skill resources."""

from __future__ import annotations

import posixpath
from copy import deepcopy
from pathlib import PurePosixPath
from types import TracebackType
from typing import Any, Protocol, TypeVar
from urllib.parse import unquote, urlsplit

from _agents_shared.sandbox_skills import (
    LazySkillSource as LazySkillSource,
    LocalDirLazySkillSource as LocalDirLazySkillSource,
    Manifest,
    SandboxSession,
    SandboxWorkspaceScope,
    Skill as Skill,
    SkillMetadata as SkillMetadata,
    SkillSet,
    _MetadataBudget,
    coerce_posix_path,
    posix_path_as_path,
)
from agents.classic.sandbox.entries import Dir, LocalDir
from agents.classic.sandbox.entries.artifacts import _LocalDirBudget

from ._function_tools import Allow, FunctionTool, ToolContext, function_tool
from .executor import Environment, _path_uri

_ContextT_contra = TypeVar("_ContextT_contra", contravariant=True)


class Skills(SkillSet):
    """Current declarations have been supplying native sandbox skill staging."""

    _bounded_metadata = True

    def _capture(self) -> Skills:
        source = self.lazy_from
        local_loader = (
            isinstance(source, LocalDirLazySkillSource)
            and type(source).load_skill is LocalDirLazySkillSource.load_skill
        )
        if self.run_as is not None and (self.from_ is not None or local_loader):
            raise ValueError("Configure the Skills reader on the sandbox session")
        memo = {} if isinstance(source, LocalDirLazySkillSource) else {id(source): source}
        return deepcopy(self.model_copy(update={"session": None}), memo)


class SandboxEnvironmentSource(Protocol[_ContextT_contra]):
    """A resource source has been exposing its explicitly selectable skill tools."""

    @property
    def skill_tools(self) -> tuple[FunctionTool[Any, Any, Any], ...]: ...

    async def __aenter__(self) -> Environment[_ContextT_contra]: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


def _remote_path(path: str) -> tuple[str, PurePosixPath]:
    uri = urlsplit(_path_uri(path))
    authority = uri.netloc.lower()
    if uri.scheme == "file" and authority == "localhost":
        authority = ""
    return authority, PurePosixPath(posixpath.normpath(unquote(uri.path)))


def _grant_stamp(manifest: Manifest) -> tuple[Any, ...]:
    return (
        str(manifest.root),
        tuple(
            (grant.path, grant.read_only, grant.host_path) for grant in manifest.extra_path_grants
        ),
    )


def _append_skill_instructions(workspace: str, skills: str | None) -> str:
    if not skills:
        return workspace
    maximum = 40_000
    separator = "\n\n"
    available = maximum - len(workspace.encode("utf-8")) - len(separator)
    if len(skills.encode("utf-8")) <= available:
        return workspace + separator + skills
    notice = "\nThe skill index has been exceeding the instruction limit."
    remaining = available - len(notice.encode("utf-8"))
    if remaining < 0:
        return workspace
    return (
        workspace
        + separator
        + skills.encode("utf-8")[:remaining].decode("utf-8", "ignore")
        + notice
    )


class _SkillLease:
    def __init__(self, skills: Skills) -> None:
        self.skills = skills
        self.environment: Environment[Any] | None = None
        self.session: SandboxSession | None = None
        self.grants: tuple[Any, ...] | None = None
        self.index: tuple[SkillMetadata, ...] = ()
        self.active = False
        self.source: object | None = None

        def admit(context: ToolContext[Any]) -> Allow:
            self._admit(context)
            return Allow()

        @function_tool(name="load_skill", before=admit, error_function=None)
        async def load_skill(context: ToolContext[Any], skill_name: str) -> dict[str, str]:
            """One selected skill has been loading into the current sandbox."""
            self._admit(context)
            matches = [item for item in self.index if skill_name in (item.name, item.path.name)]
            if len(matches) != 1:
                raise ValueError("Skill name must identify one current indexed skill")
            session = self.session
            if session is None:
                raise RuntimeError("Skill loading requires a current sandbox")
            expected = str(
                self.skills.workspace_scope.model_resource_path(
                    workspace_root=session.state.manifest.root,
                    workspace_relative_path=matches[0].path,
                )
            )
            result = await self.skills._load_skill(skill_name, indexed=matches[0])
            self._admit(context)
            if result.get("path") != expected:
                raise ValueError("Skill source returned a path outside its captured index")
            return result

        self.tools = (load_skill,) if skills.lazy_from is not None else ()

    def _admit(self, context: ToolContext[Any]) -> None:
        if (
            not self.active
            or context._environment is not self.environment
            or context._sandbox_source is not self.source
            or self.session is None
            or _grant_stamp(self.session.state.manifest) != self.grants
        ):
            raise RuntimeError("Skill loading requires its current sandbox environment and grants")

    async def materialize_eager(self, session: SandboxSession) -> None:
        if not self.skills.skills and self.skills.from_ is None:
            return
        grants = _grant_stamp(session.state.manifest)
        manifest = self.skills.process_manifest(Manifest(root=session.state.manifest.root))
        root = posix_path_as_path(coerce_posix_path(session.state.manifest.root))
        owner = session._inner
        base_dir = owner._manifest_base_dir()
        self.skills.bind(session)
        previous = await self.skills._resolve_runtime_metadata(
            session.state.manifest, budget=_MetadataBudget(), strict=True
        )
        paths = {item.path for item in previous}
        skills_root = posix_path_as_path(coerce_posix_path(self.skills.skills_path))
        paths.update(skills_root / skill.name for skill in self.skills.skills)
        if isinstance(self.skills.from_, Dir):
            for path, _entry in manifest.iter_entries():
                relative = path.relative_to(skills_root)
                if relative.parts:
                    paths.add(skills_root / relative.parts[0])
        elif isinstance(self.skills.from_, LocalDir) and self.skills.from_.src is not None:
            source = self.skills.from_
            source_grants = session.state.manifest.extra_path_grants
            src_root = source._resolve_local_dir_src_root(base_dir, source_grants=source_grants)
            files = source._list_local_dir_files(
                base_dir=base_dir,
                src_root=src_root,
                source_grants=source_grants,
                budget=_LocalDirBudget(),
            )
            paths.update(skills_root / path.parts[0] for path in files)
        for path in sorted(paths, key=str):
            if _grant_stamp(session.state.manifest) != grants:
                raise RuntimeError(
                    "Skill staging requires its current sandbox environment and grants"
                )
            await owner._remove_workspace_entry_on_resume(root / path)
        await owner._apply_entry_batch(
            [(root / path, entry) for path, entry in manifest.entries.items()],
            base_dir=base_dir,
        )
        if _grant_stamp(session.state.manifest) != grants:
            raise RuntimeError("Skill staging requires its current sandbox environment and grants")

    async def activate(
        self,
        session: SandboxSession,
        environment: Environment[Any],
        *,
        borrowed_manifest: Manifest | None = None,
    ) -> str | None:
        if borrowed_manifest is not None and (
            _grant_stamp(session.state.manifest) != _grant_stamp(borrowed_manifest)
        ):
            raise RuntimeError("Skill loading requires its current sandbox environment and grants")
        self.session = session
        self.environment = environment
        self.grants = _grant_stamp(session.state.manifest)
        self.skills.bind(session)
        self.skills.workspace_scope = SandboxWorkspaceScope.from_cwd(".")
        roots = tuple(_remote_path(root) for root in environment.capability_directories or ())
        namespace = self.skills.workspace_scope.model_resource_path(
            workspace_root=session.state.manifest.root,
            workspace_relative_path=self.skills.skills_path,
        )
        namespace_host, namespace_path = _remote_path(str(namespace))
        if not any(
            namespace_host == host
            and (
                namespace_path == root
                or root in namespace_path.parents
                or namespace_path in root.parents
            )
            for host, root in roots
        ):
            raise ValueError("Skills require a selected current Environment root")
        metadata = await self.skills._skill_metadata(session.state.manifest)
        if _grant_stamp(session.state.manifest) != self.grants:
            raise RuntimeError("Skill index requires its current sandbox environment and grants")
        for item in metadata:
            path = self.skills.workspace_scope.model_resource_path(
                workspace_root=session.state.manifest.root,
                workspace_relative_path=item.path,
            )
            authority, candidate = _remote_path(str(path))
            if not any(
                authority == host and (candidate == root or root in candidate.parents)
                for host, root in roots
            ):
                raise ValueError("Skill metadata requires a selected current Environment root")
        self.index = tuple(metadata)
        instructions = await self.skills.instructions(session.state.manifest)
        if _grant_stamp(session.state.manifest) != self.grants:
            raise RuntimeError("Skill index requires its current sandbox environment and grants")
        self.active = True
        return instructions

    def retire(self) -> None:
        self.active = False
        self.source = None
        self.index = ()
        self.session = None
        self.environment = None
        self.skills.session = None
