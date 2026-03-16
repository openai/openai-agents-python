from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from ..errors import UnsupportedCodexTargetError
from ..materialization import MaterializedFile
from ..util.iterator_io import IteratorIO
from .base import BaseEntry

if TYPE_CHECKING:
    from ..session.base_sandbox_session import BaseSandboxSession

_SUPPORTED_CODEX_OPERATING_SYSTEMS = ("linux", "darwin", "windows")
_SUPPORTED_CODEX_ARCHITECTURES = ("x86_64", "aarch64")
_SUPPORTED_CODEX_LINUX_LIBC_VARIANTS = ("gnu", "musl")
_CODEX_ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


class Codex(BaseEntry):
    type: Literal["codex"] = "codex"
    version: str = "latest"

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _ = base_dir
        asset_name = await session.resolve_codex_github_asset_name()
        if asset_name.endswith(".exe.tar.gz"):
            raise RuntimeError("Windows Codex artifacts are not supported in sandbox manifests.")
        archive_url = self._release_asset_url(asset_name)
        staging_dir = dest.parent / f".codex-install-{uuid.uuid4().hex}"
        archive_path = staging_dir / asset_name

        await session.mkdir(dest.parent, parents=True)
        await session.mkdir(staging_dir, parents=True)
        try:
            with _stream_release_asset(archive_url) as response:
                response.raise_for_status()
                await session.write(
                    archive_path,
                    _IteratorStreamWithLength(
                        response.iter_bytes(),
                        content_length=_parse_content_length(response),
                    ),
                )

            extract_result = await session.exec(
                "tar",
                "-xzf",
                archive_path,
                "-C",
                staging_dir,
                shell=False,
            )
            if not extract_result.ok():
                raise RuntimeError(extract_result.stderr.decode("utf-8", errors="replace"))

            extracted_binary = await self._resolve_extracted_binary_path(
                session=session,
                staging_dir=staging_dir,
            )
            await self._copy_extracted_binary_to_destination(
                session=session,
                extracted_binary=extracted_binary,
                dest=dest,
            )
        finally:
            await session.rm(staging_dir, recursive=True)

        await self._apply_metadata(session, dest)
        return []

    def _release_asset_url(self, asset_name: str) -> str:
        if self.version == "latest":
            return f"https://github.com/openai/codex/releases/latest/download/{asset_name}"
        return (
            f"https://github.com/openai/codex/releases/download/rust-v{self.version}/{asset_name}"
        )

    async def _resolve_extracted_binary_path(
        self,
        *,
        session: BaseSandboxSession,
        staging_dir: Path,
    ) -> str:
        result = await session.exec(
            f"find {staging_dir} -type f \\( -name codex -o -name 'codex-*' \\) | head -n 1"
        )
        if not result.ok():
            raise RuntimeError("Codex binary not found in extracted archive.")
        path = result.stdout.decode("utf-8", errors="replace").strip()
        if not path:
            raise RuntimeError("Codex binary not found in extracted archive.")
        return path

    async def _copy_extracted_binary_to_destination(
        self,
        *,
        session: BaseSandboxSession,
        extracted_binary: str,
        dest: Path,
    ) -> None:
        result = await session.exec("cp", extracted_binary, dest, shell=False)
        if not result.ok():
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))


async def resolve_codex_github_asset_name(*, session: BaseSandboxSession) -> str:
    """Resolve the Codex GitHub release asset filename for the session target."""

    target_triple = await resolve_codex_target_triple(session=session)
    suffix = ".exe.tar.gz" if target_triple.endswith("windows-msvc") else ".tar.gz"
    return f"codex-{target_triple}{suffix}"


async def resolve_codex_target_triple(*, session: BaseSandboxSession) -> str:
    """Resolve the Codex release target triple for the session target platform."""

    target_os = await _detect_target_os(session=session)
    target_arch = await _detect_target_arch(session=session, target_os=target_os)

    if target_os == "linux":
        libc = await _detect_linux_libc_variant(session=session)
        return resolve_codex_target_triple_for_target(
            target_os=target_os,
            target_arch=target_arch,
            linux_libc=libc,
        )

    return resolve_codex_target_triple_for_target(
        target_os=target_os,
        target_arch=target_arch,
    )


def resolve_codex_target_triple_for_target(
    *,
    target_os: str,
    target_arch: str,
    linux_libc: str | None = None,
) -> str:
    normalized_os = target_os.strip().lower()
    normalized_arch = target_arch.strip().lower()
    canonical_arch = _CODEX_ARCH_ALIASES.get(normalized_arch)

    if normalized_os == "linux":
        if canonical_arch is not None:
            libc = linux_libc or "gnu"
            if libc not in _SUPPORTED_CODEX_LINUX_LIBC_VARIANTS:
                raise UnsupportedCodexTargetError(
                    reason="linux_libc",
                    target_os=target_os,
                    target_arch=target_arch,
                    linux_libc=linux_libc,
                    supported_operating_systems=_SUPPORTED_CODEX_OPERATING_SYSTEMS,
                    supported_architectures=_SUPPORTED_CODEX_ARCHITECTURES,
                    supported_linux_libc_variants=_SUPPORTED_CODEX_LINUX_LIBC_VARIANTS,
                )
            return f"{canonical_arch}-unknown-linux-{libc}"
    elif normalized_os == "darwin":
        if canonical_arch is not None:
            return f"{canonical_arch}-apple-darwin"
    elif normalized_os == "windows":
        if canonical_arch is not None:
            return f"{canonical_arch}-pc-windows-msvc"
    else:
        raise UnsupportedCodexTargetError(
            reason="operating_system",
            target_os=target_os,
            target_arch=target_arch,
            supported_operating_systems=_SUPPORTED_CODEX_OPERATING_SYSTEMS,
            supported_architectures=_SUPPORTED_CODEX_ARCHITECTURES,
        )

    raise UnsupportedCodexTargetError(
        reason="architecture",
        target_os=normalized_os,
        target_arch=target_arch,
        supported_operating_systems=_SUPPORTED_CODEX_OPERATING_SYSTEMS,
        supported_architectures=_SUPPORTED_CODEX_ARCHITECTURES,
    )


async def _detect_target_os(
    *,
    session: BaseSandboxSession,
) -> Literal["linux", "darwin", "windows"]:
    unix_result = await session.exec("uname", "-s", shell=False)
    if unix_result.ok():
        system = unix_result.stdout.decode("utf-8", errors="replace").strip().lower()
        if system == "linux":
            return "linux"
        if system == "darwin":
            return "darwin"

    windows_result = await session.exec("cmd", "/c", "echo", "%OS%", shell=False)
    if windows_result.ok():
        system = windows_result.stdout.decode("utf-8", errors="replace").strip().lower()
        if system == "windows_nt":
            return "windows"

    raise RuntimeError("Unable to detect sandbox target operating system.")


async def _detect_target_arch(*, session: BaseSandboxSession, target_os: str) -> str:
    if target_os == "windows":
        result = await session.exec(
            "cmd",
            "/c",
            "echo",
            "%PROCESSOR_ARCHITECTURE%",
            shell=False,
        )
    else:
        result = await session.exec("uname", "-m", shell=False)

    if result.ok():
        return result.stdout.decode("utf-8", errors="replace").strip().lower()

    raise RuntimeError(f"Unable to detect sandbox target architecture for {target_os}.")


async def _detect_linux_libc_variant(*, session: BaseSandboxSession) -> Literal["gnu", "musl"]:
    result = await session.exec("getconf", "GNU_LIBC_VERSION", shell=False)
    if result.ok():
        return "gnu"

    result = await session.exec("ldd", "--version", shell=False)
    combined = (result.stdout + result.stderr).decode("utf-8", errors="replace").lower()
    if "musl" in combined:
        return "musl"
    if result.ok() and combined:
        return "gnu"

    raise RuntimeError("Unable to detect Linux libc variant for Codex release asset.")


class _IteratorStreamWithLength(IteratorIO):
    def __init__(self, it, *, content_length: int | None) -> None:
        super().__init__(it=it)
        self.content_length = content_length


def _stream_release_asset(url: str):
    return httpx.stream("GET", url, follow_redirects=True)


def _parse_content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
