from __future__ import annotations

from dataclasses import dataclass

from ._identity import _preserve_legacy_module

DEFAULT_MAX_MANIFEST_ENTRY_CONCURRENCY = 4

DEFAULT_MAX_LOCAL_DIR_FILE_CONCURRENCY = 4

DEFAULT_MAX_ARCHIVE_INPUT_BYTES = 1024 * 1024 * 1024

DEFAULT_MAX_ARCHIVE_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024

DEFAULT_MAX_ARCHIVE_MEMBERS = 100_000

DEFAULT_MAX_ARCHIVE_TAR_METADATA_BYTES = 16 * 1024 * 1024

DEFAULT_MAX_ARCHIVE_TAR_STREAM_BYTES = (
    DEFAULT_MAX_ARCHIVE_INPUT_BYTES + DEFAULT_MAX_ARCHIVE_EXTRACTED_BYTES
)

DEFAULT_MAX_ARCHIVE_ZIP_DIRECTORY_BYTES = 64 * 1024 * 1024

DEFAULT_MAX_ARCHIVE_LZMA_MEMORY_BYTES = 128 * 1024 * 1024


@dataclass
class SandboxConcurrencyLimits:
    """Concurrency limits for sandbox materialization work."""

    manifest_entries: int | None = DEFAULT_MAX_MANIFEST_ENTRY_CONCURRENCY
    """Maximum number of manifest entries to materialize concurrently per sandbox session.

    Set to `None` to disable this manifest entry limit.
    """

    local_dir_files: int | None = DEFAULT_MAX_LOCAL_DIR_FILE_CONCURRENCY
    """Maximum number of files to copy concurrently for each local_dir manifest entry.

    Set to `None` to disable this per-local-dir file copy limit.
    """

    def validate(self) -> None:
        if self.manifest_entries is not None and self.manifest_entries < 1:
            raise ValueError("concurrency_limits.manifest_entries must be at least 1")
        if self.local_dir_files is not None and self.local_dir_files < 1:
            raise ValueError("concurrency_limits.local_dir_files must be at least 1")


@dataclass
class SandboxArchiveLimits:
    """Resource limits for sandbox archive extraction.

    Classic applies SDK defaults when the run or session has no limits object.
    This differs from v0, where missing configuration disabled SDK archive limits.
    Setting an individual field to ``None`` disables only that field's limit;
    fields left out of this constructor keep their defaults. In particular, setting
    only the three v0 fields to ``None`` leaves the five parser/decoder limits active.

    Trusted applications that need to disable every configurable SDK archive budget
    can use the dataclass fields of the installed SDK version::

        from dataclasses import fields
        from agents.classic.sandbox import SandboxArchiveLimits, SandboxRunConfig

        unlimited = SandboxArchiveLimits(
            **{field.name: None for field in fields(SandboxArchiveLimits)}
        )
        sandbox = SandboxRunConfig(archive_limits=unlimited)

    This disables all eight current configurable budgets. Pass ``unlimited`` to
    ``session.extract(..., archive_limits=unlimited)`` for a per-call override.
    Passing ``None`` to ``extract`` instead inherits the session configuration,
    then SDK defaults when the session has none. Raising only the needed limits
    keeps the remaining bounds active. Path and member safety checks still apply,
    as do codec constraints such as the Zstandard window cap.
    """

    max_input_bytes: int | None = DEFAULT_MAX_ARCHIVE_INPUT_BYTES
    """Maximum archive input bytes accepted by `BaseSandboxSession.extract()`.

    Set to `None` to disable this input-size limit.
    """

    max_extracted_bytes: int | None = DEFAULT_MAX_ARCHIVE_EXTRACTED_BYTES
    """Maximum declared bytes that an archive may extract.

    Set to `None` to disable this extracted-size limit.
    """

    max_members: int | None = DEFAULT_MAX_ARCHIVE_MEMBERS
    """Maximum number of archive members.

    Set to `None` to disable this member-count limit.
    """

    max_tar_metadata_bytes: int | None = DEFAULT_MAX_ARCHIVE_TAR_METADATA_BYTES
    """Maximum decoded TAR metadata bytes, including PAX and GNU headers.

    Set to `None` to disable this metadata limit.
    """

    max_tar_stream_bytes: int | None = DEFAULT_MAX_ARCHIVE_TAR_STREAM_BYTES
    """Maximum decoded TAR bytes read by the parser, including file payloads.

    Set to `None` to disable this decoded-stream limit.
    """

    max_zip_directory_bytes: int | None = DEFAULT_MAX_ARCHIVE_ZIP_DIRECTORY_BYTES
    """Maximum ZIP central-directory bytes parsed on the host.

    Set to `None` to disable this directory-size limit.
    """

    max_tar_lzma_memory_bytes: int | None = DEFAULT_MAX_ARCHIVE_LZMA_MEMORY_BYTES
    """Maximum memory requested by a TAR LZMA decoder.

    Set to `None` to disable this decoder-memory limit.
    """

    max_zip_lzma_dictionary_bytes: int | None = DEFAULT_MAX_ARCHIVE_LZMA_MEMORY_BYTES
    """Maximum dictionary size requested by a ZIP LZMA decoder.

    Set to `None` to disable this dictionary-size limit.
    """

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.max_input_bytes is not None and self.max_input_bytes < 1:
            raise ValueError("archive_limits.max_input_bytes must be at least 1")
        if self.max_extracted_bytes is not None and self.max_extracted_bytes < 1:
            raise ValueError("archive_limits.max_extracted_bytes must be at least 1")
        if self.max_members is not None and self.max_members < 1:
            raise ValueError("archive_limits.max_members must be at least 1")
        if self.max_tar_metadata_bytes is not None and self.max_tar_metadata_bytes < 1:
            raise ValueError("archive_limits.max_tar_metadata_bytes must be at least 1")
        if self.max_tar_stream_bytes is not None and self.max_tar_stream_bytes < 1:
            raise ValueError("archive_limits.max_tar_stream_bytes must be at least 1")
        if self.max_zip_directory_bytes is not None and self.max_zip_directory_bytes < 1:
            raise ValueError("archive_limits.max_zip_directory_bytes must be at least 1")
        if self.max_tar_lzma_memory_bytes is not None and self.max_tar_lzma_memory_bytes < 1:
            raise ValueError("archive_limits.max_tar_lzma_memory_bytes must be at least 1")
        if (
            self.max_zip_lzma_dictionary_bytes is not None
            and self.max_zip_lzma_dictionary_bytes < 1
        ):
            raise ValueError("archive_limits.max_zip_lzma_dictionary_bytes must be at least 1")


_preserve_legacy_module(globals(), "agents.classic.run_config")
