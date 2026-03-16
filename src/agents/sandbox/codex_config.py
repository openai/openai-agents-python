from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .entries import Codex, resolve_workspace_path
from .errors import InvalidManifestPathError
from .manifest import Manifest
from .session.sandbox_session_state import SandboxSessionState

DEFAULT_CODEX_PATH = "~/.codex/codex"
# TODO: this should eventually be sourced from the pinned version of codex-python-sdk
DEFAULT_CODEX_VERSION = "0.114.0"
SandboxSessionStateT = TypeVar("SandboxSessionStateT", bound=SandboxSessionState)


@dataclass(frozen=True)
class CodexConfig:
    path: str | Path = DEFAULT_CODEX_PATH
    version: str = DEFAULT_CODEX_VERSION


def normalize_codex_config(codex: bool | CodexConfig) -> CodexConfig | None:
    if isinstance(codex, CodexConfig):
        return codex
    if codex:
        return CodexConfig()
    return None


def apply_codex_to_manifest(
    manifest: Manifest | None,
    codex: bool | CodexConfig,
) -> Manifest:
    normalized = normalize_codex_config(codex)
    base_manifest = manifest.model_copy(deep=True) if manifest is not None else Manifest()
    if normalized is None:
        return base_manifest

    codex_path = _manifest_codex_path(manifest=base_manifest, configured_path=normalized.path)
    entries = dict(base_manifest.entries)
    entries.setdefault(codex_path, Codex(version=normalized.version))
    return base_manifest.model_copy(update={"entries": entries})


def manifest_has_codex_entry(
    manifest: Manifest | None,
    codex: bool | CodexConfig,
) -> bool:
    normalized = normalize_codex_config(codex)
    if normalized is None or manifest is None:
        return normalized is None

    codex_path = _manifest_codex_path(manifest=manifest, configured_path=normalized.path)
    return codex_path in {manifest._coerce_rel_path(path) for path in manifest.entries}


def apply_codex_to_session_state(
    state: SandboxSessionStateT,
    codex: bool | CodexConfig,
) -> SandboxSessionStateT:
    return state.model_copy(update={"manifest": apply_codex_to_manifest(state.manifest, codex)})


def _manifest_codex_path(*, manifest: Manifest, configured_path: str | Path) -> Path:
    configured_str = str(configured_path)
    if configured_str == "~":
        return Path(".")
    if configured_str.startswith("~/"):
        home_relative = Path(configured_str.removeprefix("~/"))
        manifest._validate_rel_path(home_relative)
        return home_relative

    raw_path = Path(configured_path)
    if not raw_path.is_absolute():
        manifest._validate_rel_path(raw_path)
        return raw_path

    candidate_roots = [Path(manifest.root)]
    default_root = Path(str(Manifest.model_fields["root"].default))
    if default_root not in candidate_roots:
        candidate_roots.append(default_root)

    for root in candidate_roots:
        try:
            resolved = resolve_workspace_path(
                root,
                raw_path,
                allow_absolute_within_root=True,
            )
        except InvalidManifestPathError:
            continue
        return resolved.relative_to(root)

    raise InvalidManifestPathError(rel=raw_path, reason="absolute")
