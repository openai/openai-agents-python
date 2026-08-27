from __future__ import annotations

import copy
import dataclasses
import importlib
import re
import sys
import types
from collections.abc import Callable, Collection, Coroutine, Iterable, Mapping
from functools import wraps
from pathlib import PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any, NoReturn, ParamSpec, TypeVar, cast, get_args
from urllib.parse import urlsplit

from ..exceptions import (
    _base_exception_instance_dict,
    _discard_exception_graph,
    _exact_string_state_entry,
    _exact_string_state_value,
    _is_error_data_redacted,
    _mark_error_data_redacted,
    _object_instance_dict,
    _raise_data_redacted_error,
    _replace_data_redacted_process_control_error,
    _static_type_metadata,
)
from . import errors as _sandbox_errors
from .entries import (
    AzureBlobMount,
    BaseEntry,
    BoxMount,
    Dir,
    File,
    GCSMount,
    LocalDir,
    Mount,
    R2Mount,
    S3FilesMount,
    S3Mount,
)
from .entries.mounts.base import MountStrategyBase
from .entries.mounts.patterns import (
    FuseMountPattern,
    MountPatternBase,
    MountpointMountPattern,
    RcloneMountPattern,
    S3FilesMountPattern,
)
from .errors import ErrorCode, MountConfigError, OpName, SandboxError

if TYPE_CHECKING:
    from .manifest import Manifest

REDACTED_MOUNT_AUTHORITY_KEY = "__openai_agents_redacted_mount_authority"
CREDENTIALLESS_MOUNT_AUTHORITY_KEY = "__openai_agents_credentialless_mount_authority"

# These fields are authority, not merely secrets. Identifiers such as an Azure managed-identity
# client ID or a provider secret name can grant a mount access even though they are not secret
# values by themselves.
_AUTHORITY_FIELDS_BY_MOUNT_TYPE: dict[str, tuple[str, ...]] = {
    "azure_blob_mount": ("identity_client_id", "account_key"),
    "box_mount": (
        "client_secret",
        "access_token",
        "token",
        "box_config_file",
        "config_credentials",
    ),
    "gcs_mount": (
        "access_id",
        "secret_access_key",
        "service_account_file",
        "service_account_credentials",
        "access_token",
    ),
    "r2_mount": ("access_key_id", "secret_access_key"),
    "s3_mount": ("access_key_id", "secret_access_key", "session_token"),
}
# Each entry identifies the fields that activate an inline credential set and the non-empty
# fields required whenever that set is active. Keep this table aligned with provider selection
# and credential emission in the built-in mount implementations.
_IN_CONTAINER_CREDENTIAL_SET_REQUIREMENTS_BY_MOUNT_TYPE: dict[
    str, tuple[tuple[str, ...], tuple[str, ...]]
] = {
    "gcs_mount": (
        ("access_id", "secret_access_key"),
        ("access_id", "secret_access_key"),
    ),
    "r2_mount": (
        ("access_key_id", "secret_access_key"),
        ("access_key_id", "secret_access_key"),
    ),
    "s3_mount": (
        ("access_key_id", "secret_access_key", "session_token"),
        ("access_key_id", "secret_access_key"),
    ),
}
_AUTHORITY_FILE_FIELDS_BY_MOUNT_TYPE: dict[str, tuple[str, ...]] = {
    "box_mount": ("box_config_file",),
    "gcs_mount": ("service_account_file",),
}
_URL_FIELDS_BY_MOUNT_TYPE: dict[str, tuple[str, ...]] = {
    "azure_blob_mount": ("endpoint",),
    "gcs_mount": ("endpoint_url",),
    "r2_mount": ("custom_domain",),
    "s3_mount": ("endpoint_url",),
}
_BLAXEL_S3FS_OPTION_FIELDS_BY_MOUNT_TYPE: dict[str, tuple[str, ...]] = {
    "r2_mount": ("custom_domain", "account_id"),
    "s3_mount": ("endpoint_url", "region"),
}
# Every free-form value interpolated into an rclone configuration line must remain a single line.
# Keep this table aligned with the built-in providers' ``_rclone_required_lines`` methods.
_RCLONE_CONFIG_VALUE_FIELDS_BY_MOUNT_TYPE: dict[str, tuple[str, ...]] = {
    "azure_blob_mount": ("account", "endpoint", "identity_client_id", "account_key"),
    "box_mount": (
        "client_id",
        "client_secret",
        "access_token",
        "token",
        "box_config_file",
        "config_credentials",
        "root_folder_id",
        "impersonate",
        "owned_by",
    ),
    "gcs_mount": (
        "access_id",
        "secret_access_key",
        "region",
        "endpoint_url",
        "service_account_file",
        "service_account_credentials",
        "access_token",
    ),
    "r2_mount": ("account_id", "access_key_id", "secret_access_key", "custom_domain"),
    "s3_mount": (
        "s3_provider",
        "endpoint_url",
        "region",
        "access_key_id",
        "secret_access_key",
        "session_token",
    ),
}
_CANONICAL_MOUNT_TYPES: tuple[tuple[type[Mount], str], ...] = (
    (AzureBlobMount, "azure_blob_mount"),
    (BoxMount, "box_mount"),
    (GCSMount, "gcs_mount"),
    (R2Mount, "r2_mount"),
    (S3FilesMount, "s3_files_mount"),
    (S3Mount, "s3_mount"),
)

# Opaque third-party configuration cannot be classified safely by option name. The complete field
# is therefore live authority: it is allowed only at a trusted external executor and is removed
# from durable state as a unit.
_OPAQUE_STRATEGY_AUTHORITY_FIELDS: dict[str, tuple[str, ...]] = {
    "docker_volume": ("driver_options",),
    "modal_cloud_bucket": ("secret_name", "secret_environment_name"),
}

# SDK-owned extension entry types are not necessarily imported when raw RunState is restored.
# Keep this list closed so documented extension mounts remain import-order independent without
# treating arbitrary unregistered entries as trusted mounts.
_SDK_EXTENSION_MOUNT_CLASSIFICATION_BY_TYPE: dict[str, tuple[str, str]] = {
    "blaxel_drive_mount": (
        "agents.extensions.sandbox.blaxel.mounts",
        "BlaxelDriveMount",
    ),
}
_SDK_EXTENSION_MOUNT_SERIALIZED_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    "blaxel_drive_mount": frozenset(
        {
            "type",
            "description",
            "ephemeral",
            "group",
            "is_dir",
            "permissions",
            "mount_path",
            "read_only",
            "mount_strategy",
            "drive_name",
            "drive_mount_path",
            "drive_path",
            "drive_read_only",
        }
    ),
}
_SDK_EXTENSION_MOUNT_ENTRY_TYPES = frozenset(_SDK_EXTENSION_MOUNT_CLASSIFICATION_BY_TYPE)

# This closed table is the source of truth for SDK-owned execution boundaries. Class provenance
# keeps module reloads stable while ordinary custom subclasses cannot promote themselves into a
# trusted boundary by overriding strategy attributes.
_STRATEGY_CLASSIFICATION_BY_TYPE: dict[str, tuple[str, str | None, str, str]] = {
    "in_container": (
        "in_container",
        None,
        "agents.sandbox.entries.mounts.base",
        "InContainerMountStrategy",
    ),
    "docker_volume": (
        "external",
        "docker",
        "agents.sandbox.entries.mounts.base",
        "DockerVolumeMountStrategy",
    ),
    "blaxel_cloud_bucket": (
        "in_container",
        "blaxel",
        "agents.extensions.sandbox.blaxel.mounts",
        "BlaxelCloudBucketMountStrategy",
    ),
    "blaxel_drive": (
        "external",
        "blaxel",
        "agents.extensions.sandbox.blaxel.mounts",
        "BlaxelDriveMountStrategy",
    ),
    "cloudflare_bucket_mount": (
        "external",
        "cloudflare",
        "agents.extensions.sandbox.cloudflare.mounts",
        "CloudflareBucketMountStrategy",
    ),
    "daytona_cloud_bucket": (
        "in_container",
        "daytona",
        "agents.extensions.sandbox.daytona.mounts",
        "DaytonaCloudBucketMountStrategy",
    ),
    "e2b_cloud_bucket": (
        "in_container",
        "e2b",
        "agents.extensions.sandbox.e2b.mounts",
        "E2BCloudBucketMountStrategy",
    ),
    "modal_cloud_bucket": (
        "external",
        "modal",
        "agents.extensions.sandbox.modal.mounts",
        "ModalCloudBucketMountStrategy",
    ),
    "runloop_cloud_bucket": (
        "in_container",
        "runloop",
        "agents.extensions.sandbox.runloop.mounts",
        "RunloopCloudBucketMountStrategy",
    ),
    "vercel_cloud_bucket": (
        "in_container",
        "vercel",
        "agents.extensions.sandbox.vercel.mounts",
        "VercelCloudBucketMountStrategy",
    ),
}
_SERIALIZED_FIELDS_BY_STRATEGY_TYPE: dict[str, frozenset[str]] = {
    "in_container": frozenset({"type", "pattern"}),
    "docker_volume": frozenset({"type", "driver", "driver_options"}),
    "blaxel_cloud_bucket": frozenset({"type"}),
    "blaxel_drive": frozenset({"type"}),
    "cloudflare_bucket_mount": frozenset({"type"}),
    "daytona_cloud_bucket": frozenset({"type", "pattern"}),
    "e2b_cloud_bucket": frozenset({"type", "pattern"}),
    "modal_cloud_bucket": frozenset({"type", "secret_name", "secret_environment_name"}),
    "runloop_cloud_bucket": frozenset({"type", "pattern"}),
    "vercel_cloud_bucket": frozenset({"type"}),
}
_SERIALIZED_PATTERN_CLASS_BY_TYPE: dict[str, type[MountPatternBase]] = {
    "fuse": FuseMountPattern,
    "mountpoint": MountpointMountPattern,
    "rclone": RcloneMountPattern,
    "s3files": S3FilesMountPattern,
}
_SERIALIZED_OPTIONS_CLASS_BY_PATTERN_TYPE = {
    "mountpoint": MountpointMountPattern.MountpointOptions,
    "s3files": S3FilesMountPattern.S3FilesOptions,
}


@dataclasses.dataclass(frozen=True)
class _InContainerMountCredentialCapability:
    strategy_types: frozenset[str]
    mount_type: str
    pattern_type: str | None
    mount_scoped_fields: frozenset[str]
    broad_fields: frozenset[str]
    enables_broad_credential_discovery: bool = False
    required_any_fields: frozenset[str] = frozenset()


_RCLONE_STRATEGY_TYPES = frozenset(
    {
        "in_container",
        "daytona_cloud_bucket",
        "e2b_cloud_bucket",
        "runloop_cloud_bucket",
    }
)
_IN_CONTAINER_MOUNT_CREDENTIAL_CAPABILITIES: tuple[_InContainerMountCredentialCapability, ...] = (
    _InContainerMountCredentialCapability(
        strategy_types=_RCLONE_STRATEGY_TYPES,
        mount_type="s3_mount",
        pattern_type="rclone",
        mount_scoped_fields=frozenset({"access_key_id", "secret_access_key", "session_token"}),
        broad_fields=frozenset({"mount_strategy.pattern.config_file_path"}),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=_RCLONE_STRATEGY_TYPES,
        mount_type="r2_mount",
        pattern_type="rclone",
        mount_scoped_fields=frozenset({"access_key_id", "secret_access_key"}),
        broad_fields=frozenset({"mount_strategy.pattern.config_file_path"}),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=_RCLONE_STRATEGY_TYPES,
        mount_type="gcs_mount",
        pattern_type="rclone",
        mount_scoped_fields=frozenset(
            {
                "access_id",
                "secret_access_key",
                "service_account_credentials",
                "access_token",
            }
        ),
        broad_fields=frozenset({"service_account_file", "mount_strategy.pattern.config_file_path"}),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=_RCLONE_STRATEGY_TYPES,
        mount_type="azure_blob_mount",
        pattern_type="rclone",
        mount_scoped_fields=frozenset({"account_key"}),
        broad_fields=frozenset({"identity_client_id", "mount_strategy.pattern.config_file_path"}),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=_RCLONE_STRATEGY_TYPES,
        mount_type="box_mount",
        pattern_type="rclone",
        mount_scoped_fields=frozenset(
            {"client_secret", "access_token", "token", "config_credentials"}
        ),
        broad_fields=frozenset({"box_config_file", "mount_strategy.pattern.config_file_path"}),
        required_any_fields=frozenset(
            {"access_token", "token", "config_credentials", "box_config_file"}
        ),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=frozenset({"in_container"}),
        mount_type="s3_mount",
        pattern_type="mountpoint",
        mount_scoped_fields=frozenset({"access_key_id", "secret_access_key", "session_token"}),
        broad_fields=frozenset(),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=frozenset({"in_container"}),
        mount_type="gcs_mount",
        pattern_type="mountpoint",
        mount_scoped_fields=frozenset({"access_id", "secret_access_key"}),
        broad_fields=frozenset(),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=frozenset({"in_container"}),
        mount_type="azure_blob_mount",
        pattern_type="fuse",
        mount_scoped_fields=frozenset({"account_key"}),
        broad_fields=frozenset({"identity_client_id"}),
        enables_broad_credential_discovery=True,
    ),
    _InContainerMountCredentialCapability(
        strategy_types=frozenset({"in_container"}),
        mount_type="s3_files_mount",
        pattern_type="s3files",
        mount_scoped_fields=frozenset(),
        broad_fields=frozenset({"extra_options", "mount_strategy.pattern.options.extra_options"}),
        enables_broad_credential_discovery=True,
    ),
    _InContainerMountCredentialCapability(
        strategy_types=frozenset({"vercel_cloud_bucket"}),
        mount_type="s3_mount",
        pattern_type=None,
        mount_scoped_fields=frozenset({"access_key_id", "secret_access_key", "session_token"}),
        broad_fields=frozenset(),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=frozenset({"blaxel_cloud_bucket"}),
        mount_type="s3_mount",
        pattern_type=None,
        mount_scoped_fields=frozenset({"access_key_id", "secret_access_key", "session_token"}),
        broad_fields=frozenset(),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=frozenset({"blaxel_cloud_bucket"}),
        mount_type="r2_mount",
        pattern_type=None,
        mount_scoped_fields=frozenset({"access_key_id", "secret_access_key"}),
        broad_fields=frozenset(),
    ),
    _InContainerMountCredentialCapability(
        strategy_types=frozenset({"blaxel_cloud_bucket"}),
        mount_type="gcs_mount",
        pattern_type=None,
        mount_scoped_fields=frozenset(
            {"access_id", "secret_access_key", "service_account_credentials"}
        ),
        broad_fields=frozenset(),
    ),
)
_RCLONE_SAFE_FLAG_ARGS = frozenset({"allow-other"})
_RCLONE_SAFE_VALUE_ARGS = frozenset({"buffer-size", "gid", "uid"})
_SAFE_MOUNT_VALIDATION_MESSAGE_ATTR = "_agents_safe_mount_validation_message"
_SAFE_MOUNT_VALIDATION_MESSAGE_MARKER = object()
_SANDBOX_ERROR_OPS = frozenset(get_args(OpName))
_STRUCTURED_SANDBOX_ERROR_SAFE_SUBTYPE_STATE: tuple[
    tuple[type[SandboxError], tuple[tuple[str, object], ...]], ...
] = (
    (_sandbox_errors.SandboxError, ()),
    (_sandbox_errors.ConfigurationError, ()),
    (_sandbox_errors.SandboxRuntimeError, ()),
    (_sandbox_errors.ArtifactError, ()),
    (_sandbox_errors.SnapshotError, ()),
    (_sandbox_errors.ApplyPatchError, ()),
    (_sandbox_errors.InvalidManifestPathError, ()),
    (_sandbox_errors.InvalidCompressionSchemeError, ()),
    (_sandbox_errors.ExposedPortUnavailableError, ()),
    (_sandbox_errors.ExecFailureError, (("command", ()),)),
    (
        _sandbox_errors.ExecNonZeroError,
        (("command", ()), ("exit_code", 1), ("stdout", b""), ("stderr", b"")),
    ),
    (_sandbox_errors.ExecTimeoutError, (("command", ()), ("timeout_s", None))),
    (_sandbox_errors.ExecTransportError, (("command", ()),)),
    (_sandbox_errors.PtySessionNotFoundError, (("session_id", -1),)),
    (_sandbox_errors.WorkspaceIOError, ()),
    (_sandbox_errors.ApplyPatchPathError, ()),
    (_sandbox_errors.ApplyPatchDiffError, ()),
    (_sandbox_errors.ApplyPatchFileNotFoundError, ()),
    (_sandbox_errors.ApplyPatchDecodeError, ()),
    (_sandbox_errors.WorkspaceReadNotFoundError, ()),
    (_sandbox_errors.WorkspaceArchiveReadError, ()),
    (_sandbox_errors.WorkspaceArchiveWriteError, ()),
    (_sandbox_errors.WorkspaceWriteTypeError, ()),
    (_sandbox_errors.WorkspaceStopError, ()),
    (_sandbox_errors.WorkspaceStartError, ()),
    (_sandbox_errors.WorkspaceRootNotFoundError, ()),
    (_sandbox_errors.LocalArtifactError, ()),
    (_sandbox_errors.LocalFileReadError, ()),
    (_sandbox_errors.LocalDirReadError, ()),
    (_sandbox_errors.LocalChecksumError, ()),
    (_sandbox_errors.GitArtifactError, ()),
    (_sandbox_errors.GitMissingInImageError, ()),
    (_sandbox_errors.GitCloneError, ()),
    (_sandbox_errors.GitSubpathError, ()),
    (_sandbox_errors.GitCopyError, ()),
    (_sandbox_errors.MountArtifactError, ()),
    (_sandbox_errors.MountToolMissingError, ()),
    (_sandbox_errors.MountCommandError, ()),
    (_sandbox_errors.SkillsConfigError, ()),
    (_sandbox_errors.SnapshotPersistError, ()),
    (_sandbox_errors.SnapshotRestoreError, ()),
    (_sandbox_errors.SnapshotNotRestorableError, ()),
)
_CALL_AUTHORITY_REQUIRED_STATE_KEYS = (
    "state",
    "manifest",
    "default_manifest",
    "_sandbox_config",
    "session_state",
    "_trusted_manifest",
)
_CALL_AUTHORITY_LINK_STATE_KEYS = (
    "session",
    "_session",
    "_client",
    "_inner",
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


class _InvalidRawMountManifestError(ValueError):
    pass


def redact_mount_error_data(
    function: Callable[_P, Coroutine[Any, Any, _T]],
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Replace failures after clearing async frames that handled mount authority."""

    @wraps(function)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        call_has_authority = _call_has_configured_mount_authority(
            args,
            kwargs,
            function=function,
        )
        safe_error: BaseException | None = None
        try:
            return await function(*args, **kwargs)
        except BaseException as error:
            error_is_redacted = _is_error_data_redacted(error)
            if call_has_authority or error_is_redacted:
                safe_error = _replace_protected_mount_error(error)
            else:
                raise

        del args, kwargs, call_has_authority, error_is_redacted
        assert safe_error is not None
        _raise_data_redacted_error(safe_error)

    return wrapper


def _redact_mount_error_data_sync(
    function: Callable[_P, _T],
    *,
    preserve_value_error_type: bool,
) -> Callable[_P, _T]:
    """Replace failures after clearing sync frames that handled mount authority."""

    @wraps(function)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        call_has_authority = _call_has_configured_mount_authority(
            args,
            kwargs,
            function=function,
        )
        safe_error: BaseException | None = None
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            error_is_redacted = _is_error_data_redacted(error)
            if preserve_value_error_type and call_has_authority and isinstance(error, ValueError):
                discard_mount_source_exception(error)
                safe_error = ValueError("sandbox mount validation failed")
                _mark_error_data_redacted(safe_error)
            elif call_has_authority or error_is_redacted:
                safe_error = _replace_protected_mount_error(error)
            else:
                raise

        del args, kwargs, call_has_authority, error_is_redacted
        assert safe_error is not None
        _raise_data_redacted_error(safe_error)

    return wrapper


def redact_mount_error_data_sync(function: Callable[_P, _T]) -> Callable[_P, _T]:
    """Replace failures after clearing sync frames that handled mount authority."""

    return _redact_mount_error_data_sync(function, preserve_value_error_type=False)


def redact_mount_validation_error_data_sync(
    function: Callable[_P, _T],
) -> Callable[_P, _T]:
    """Redact sync mount validation failures while preserving `ValueError` compatibility."""

    return _redact_mount_error_data_sync(function, preserve_value_error_type=True)


def _replace_mount_error(
    error: MountConfigError,
    *,
    state: dict[object, object],
    error_code: ErrorCode,
    op: OpName,
    retryable: bool | None,
) -> MountConfigError:
    message = "sandbox mount configuration is invalid"
    if (
        state is not None
        and _exact_string_state_value(state, _SAFE_MOUNT_VALIDATION_MESSAGE_ATTR)
        is _SAFE_MOUNT_VALIDATION_MESSAGE_MARKER
        and type(_exact_string_state_value(state, "message")) is str
    ):
        message = cast(str, _exact_string_state_value(state, "message"))
    discard_mount_source_exception(error)
    safe_error = MountConfigError(message=message)
    safe_error.error_code = error_code
    safe_error.op = op
    safe_error.retryable = retryable
    _mark_error_data_redacted(safe_error)
    return safe_error


def _replace_protected_mount_error(error: BaseException) -> BaseException:
    process_control_error = _replace_data_redacted_process_control_error(error)
    if process_control_error is not None:
        return process_control_error
    structured_error = _replace_structured_sandbox_error(error)
    if structured_error is not None:
        return structured_error
    return _replace_mount_operation_error(error)


def _replace_structured_sandbox_error(error: BaseException) -> SandboxError | None:
    error_type = type(error)
    state = _base_exception_instance_dict(error)
    if state is None:
        return None
    error_code = _exact_string_state_value(state, "error_code")
    op = _exact_string_state_value(state, "op")
    retryable_found, retryable = _exact_string_state_entry(state, "retryable")
    if (
        type(error_code) is not ErrorCode
        or type(op) is not str
        or op not in _SANDBOX_ERROR_OPS
        or not retryable_found
        or (retryable is not None and type(retryable) is not bool)
    ):
        return None

    if error_type is MountConfigError:
        return _replace_mount_error(
            cast(MountConfigError, error),
            state=state,
            error_code=error_code,
            op=cast(OpName, op),
            retryable=retryable,
        )

    safe_subtype_state = next(
        (
            fields
            for candidate, fields in _STRUCTURED_SANDBOX_ERROR_SAFE_SUBTYPE_STATE
            if error_type is candidate
        ),
        None,
    )
    if safe_subtype_state is None:
        return None

    discard_mount_source_exception(error)
    safe_error = cast(SandboxError, BaseException.__new__(error_type))
    message = "sandbox operation failed while using a protected mount configuration"
    object.__setattr__(safe_error, "message", message)
    object.__setattr__(safe_error, "error_code", error_code)
    object.__setattr__(safe_error, "op", cast(OpName, op))
    object.__setattr__(safe_error, "context", {})
    object.__setattr__(safe_error, "cause", None)
    object.__setattr__(safe_error, "retryable", retryable)
    for field_name, field_value in safe_subtype_state:
        object.__setattr__(safe_error, field_name, field_value)
    BaseException.__init__(safe_error, message)
    _mark_error_data_redacted(safe_error)
    return safe_error


def _replace_mount_operation_error(error: BaseException) -> RuntimeError:
    discard_mount_source_exception(error)
    safe_error = RuntimeError(
        "sandbox operation failed while using a protected mount configuration"
    )
    _mark_error_data_redacted(safe_error)
    return safe_error


def discard_mount_source_exception(error: BaseException) -> None:
    """Clear source frames without consulting provider-defined exception attributes."""
    _discard_exception_graph(error)


def _url_contains_inline_authority(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        return True
    if "@" in value:
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None or bool(parsed.query)


def _rclone_extra_args_analysis(
    extra_args: Iterable[object],
) -> tuple[bool, tuple[str, ...], bool]:
    """Classify the supported subset and return exact caller-provided config paths."""

    args = tuple(extra_args)
    config_paths: list[str] = []
    safe = True
    invalid_config_path = False
    index = 0
    while index < len(args):
        arg = args[index]
        if not isinstance(arg, str) or not arg.startswith("-"):
            safe = False
            index += 1
            continue
        option, separator, value = arg.lstrip("-").partition("=")
        normalized = option.lower().replace("_", "-")
        if normalized == "config":
            safe = False
            if separator and value:
                config_paths.append(value)
                index += 1
            elif not separator and index + 1 < len(args):
                next_arg = args[index + 1]
                if isinstance(next_arg, str) and not next_arg.startswith("-"):
                    config_paths.append(next_arg)
                else:
                    invalid_config_path = True
                index += 2
            else:
                invalid_config_path = True
                index += 1
            continue
        if normalized in _RCLONE_SAFE_FLAG_ARGS and not separator:
            index += 1
            continue
        if normalized in _RCLONE_SAFE_VALUE_ARGS:
            if separator and value:
                index += 1
                continue
            if not separator and index + 1 < len(args):
                next_arg = args[index + 1]
                if isinstance(next_arg, str) and not next_arg.startswith("-"):
                    index += 2
                    continue
        safe = False
        index += 1
    return safe, tuple(config_paths), invalid_config_path


def _rclone_extra_args_are_safe(extra_args: Iterable[object]) -> bool:
    return _rclone_extra_args_analysis(extra_args)[0]


def _rclone_remote_name_is_safe(remote_name: object) -> bool:
    if remote_name is None or remote_name == "":
        return True
    return (
        isinstance(remote_name, str)
        and remote_name == remote_name.strip()
        and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]*", remote_name) is not None
    )


def _canonical_mount_type(entry_class: type[BaseEntry]) -> str | None:
    for canonical_class, canonical_type in _CANONICAL_MOUNT_TYPES:
        if issubclass(entry_class, canonical_class):
            return canonical_type
    return None


def _canonical_mount_class(entry_class: type[BaseEntry]) -> type[Mount] | None:
    for canonical_class, _canonical_type in _CANONICAL_MOUNT_TYPES:
        if issubclass(entry_class, canonical_class):
            return canonical_class
    return None


def _mount_entry_class_is_trusted(entry_class: type[BaseEntry]) -> bool:
    canonical_class = _canonical_mount_class(entry_class)
    if canonical_class is not None:
        return entry_class is canonical_class
    return any(
        entry_class is _trusted_extension_mount_class(mount_type)
        for mount_type in _SDK_EXTENSION_MOUNT_ENTRY_TYPES
    )


def _configured_pydantic_extra_fields(value: object) -> tuple[str, ...]:
    extra = getattr(value, "model_extra", None)
    if not isinstance(extra, Mapping):
        return ()
    return tuple(
        name
        for name, configured in extra.items()
        if isinstance(name, str) and configured is not None
    )


def _trusted_strategy_class(strategy_type: str) -> type[MountStrategyBase] | None:
    classification = _STRATEGY_CLASSIFICATION_BY_TYPE.get(strategy_type)
    if classification is None:
        return None
    _boundary, _backend_id, module_name, class_name = classification
    try:
        strategy_class = getattr(importlib.import_module(module_name), class_name)
    except (AttributeError, ImportError, TypeError):
        return None
    if not isinstance(strategy_class, type) or not issubclass(strategy_class, MountStrategyBase):
        return None
    return strategy_class


def _trusted_extension_mount_class(mount_type: str) -> type[Mount] | None:
    classification = _SDK_EXTENSION_MOUNT_CLASSIFICATION_BY_TYPE.get(mount_type)
    if classification is None:
        return None
    module_name, class_name = classification
    try:
        mount_class = getattr(importlib.import_module(module_name), class_name)
    except (AttributeError, ImportError, TypeError, ValueError):
        return None
    if not isinstance(mount_class, type) or not issubclass(mount_class, Mount):
        return None
    return mount_class


def _mount_class_is_trusted(mount: Mount) -> bool:
    return _mount_entry_class_is_trusted(type(mount))


def _pattern_class_is_trusted(pattern: object) -> bool:
    return any(
        type(pattern) is pattern_class and getattr(pattern, "type", None) == pattern_type
        for pattern_type, pattern_class in _SERIALIZED_PATTERN_CLASS_BY_TYPE.items()
    )


def _configured_custom_mount_fields(mount: Mount) -> tuple[str, ...]:
    canonical_class = _canonical_mount_class(type(mount))
    if canonical_class is not None and type(mount) is canonical_class:
        return ()
    if canonical_class is None:
        if type(mount) is _trusted_extension_mount_class(mount.type):
            return ()
        safe_fields = Mount.model_fields
    else:
        safe_fields = canonical_class.model_fields
    configured_fields = [
        name
        for name in type(mount).model_fields
        if name not in safe_fields and getattr(mount, name, None) is not None
    ]
    configured_fields.extend(_configured_pydantic_extra_fields(mount))
    return tuple(dict.fromkeys(configured_fields))


def _configured_custom_pattern_fields(pattern: object) -> tuple[str, ...]:
    canonical_class = next(
        (
            pattern_class
            for pattern_class in _SERIALIZED_PATTERN_CLASS_BY_TYPE.values()
            if isinstance(pattern, pattern_class)
        ),
        None,
    )
    if canonical_class is not None and type(pattern) is canonical_class:
        return ()
    safe_fields: Collection[str] = (
        canonical_class.model_fields if canonical_class is not None else {"type"}
    )
    pattern_fields = getattr(type(pattern), "model_fields", {})
    configured_fields = [
        name
        for name in pattern_fields
        if name not in safe_fields and getattr(pattern, name, None) is not None
    ]
    configured_fields.extend(_configured_pydantic_extra_fields(pattern))
    return tuple(dict.fromkeys(configured_fields))


def _value_contains_config_line_break(value: object) -> bool:
    return isinstance(value, str) and ("\r" in value or "\n" in value)


def _value_contains_s3fs_option_delimiter(value: object) -> bool:
    return isinstance(value, str) and "," in value


def _configured_rclone_line_fields(mount: Mount, mount_type: str) -> tuple[str, ...]:
    return tuple(
        name
        for name in _RCLONE_CONFIG_VALUE_FIELDS_BY_MOUNT_TYPE.get(mount_type, ())
        if _value_contains_config_line_break(getattr(mount, name, None))
    )


def _configured_blaxel_s3fs_option_fields(
    mount: Mount,
    mount_type: str,
) -> tuple[str, ...]:
    if mount.mount_strategy.type != "blaxel_cloud_bucket":
        return ()
    return tuple(
        name
        for name in _BLAXEL_S3FS_OPTION_FIELDS_BY_MOUNT_TYPE.get(mount_type, ())
        if _value_contains_s3fs_option_delimiter(getattr(mount, name, None))
    )


def _configured_unknown_strategy_fields(strategy: MountStrategyBase) -> tuple[str, ...]:
    if _strategy_classification(strategy)[0] != "unknown":
        return ()
    configured_fields = [
        name
        for name in type(strategy).model_fields
        if name != "type" and getattr(strategy, name, None) is not None
    ]
    configured_fields.extend(_configured_pydantic_extra_fields(strategy))
    return tuple(dict.fromkeys(configured_fields))


def _configured_mount_authority_fields(mount: Mount) -> tuple[str, ...]:
    mount_type = _canonical_mount_type(type(mount)) or mount.type
    fields = [
        name
        for name in _AUTHORITY_FIELDS_BY_MOUNT_TYPE.get(mount_type, ())
        if getattr(mount, name, None) is not None
    ]
    fields.extend(
        name
        for name in _URL_FIELDS_BY_MOUNT_TYPE.get(mount_type, ())
        if _url_contains_inline_authority(getattr(mount, name, None))
    )
    fields.extend(_configured_rclone_line_fields(mount, mount_type))
    fields.extend(_configured_custom_mount_fields(mount))

    strategy = mount.mount_strategy
    strategy_boundary, _strategy_backend_id = _strategy_classification(strategy)
    fields.extend(_configured_blaxel_s3fs_option_fields(mount, mount_type))
    if strategy_boundary == "unknown":
        fields.extend(
            f"mount_strategy.{name}" for name in _configured_unknown_strategy_fields(strategy)
        )
    else:
        fields.extend(
            f"mount_strategy.{name}"
            for name in _OPAQUE_STRATEGY_AUTHORITY_FIELDS.get(strategy.type, ())
            if getattr(strategy, name, None)
        )
    pattern = getattr(strategy, "pattern", None)
    fields.extend(
        f"mount_strategy.pattern.{name}" for name in _configured_custom_pattern_fields(pattern)
    )
    if isinstance(pattern, RcloneMountPattern):
        if not _rclone_remote_name_is_safe(pattern.remote_name):
            fields.append("mount_strategy.pattern.remote_name")
        if pattern.config_file_path is not None:
            fields.append("mount_strategy.pattern.config_file_path")
        if not _rclone_extra_args_are_safe(pattern.extra_args):
            fields.append("mount_strategy.pattern.extra_args")
    elif isinstance(pattern, MountpointMountPattern) and _url_contains_inline_authority(
        pattern.options.endpoint_url
    ):
        fields.append("mount_strategy.pattern.options.endpoint_url")
    elif isinstance(pattern, S3FilesMountPattern) and pattern.options.extra_options:
        fields.append("mount_strategy.pattern.options.extra_options")
    if mount_type == "s3_files_mount" and getattr(mount, "extra_options", None):
        fields.append("extra_options")
    return tuple(dict.fromkeys(fields))


def _mount_has_usable_required_authority(
    mount: Mount,
    required_fields: Collection[str],
) -> bool:
    for field_name in required_fields:
        value = getattr(mount, field_name, None)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _invalid_in_container_authority_value_fields(
    mount: Mount,
    mount_type: str,
) -> tuple[str, ...]:
    invalid: list[str] = []
    for field_name in _AUTHORITY_FIELDS_BY_MOUNT_TYPE.get(mount_type, ()):
        value = getattr(mount, field_name, None)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            invalid.append(field_name)
    return tuple(sorted(invalid))


def _invalid_in_container_credential_set_fields(
    mount: Mount,
    mount_type: str,
) -> tuple[str, ...]:
    requirement = _IN_CONTAINER_CREDENTIAL_SET_REQUIREMENTS_BY_MOUNT_TYPE.get(mount_type)
    if requirement is None:
        return ()

    configured_fields, required_fields = requirement
    values = {field_name: getattr(mount, field_name, None) for field_name in configured_fields}
    if all(value is None for value in values.values()):
        return ()

    invalid = {
        field_name
        for field_name, value in values.items()
        if value is not None and (not isinstance(value, str) or not value.strip())
    }
    for field_name in required_fields:
        value = values[field_name]
        if not isinstance(value, str) or not value.strip():
            invalid.add(field_name)
    return tuple(sorted(invalid))


def _manifest_has_configured_mount_authority(manifest: Manifest) -> bool:
    pending = list(manifest.entries.values())
    while pending:
        entry = pending.pop()
        entry_metadata = _static_type_metadata(type(entry))
        entry_mro = () if entry_metadata is None else entry_metadata[1]
        if any(base is Mount for base in entry_mro) and _mount_has_or_may_hide_configured_authority(
            cast(Mount, entry)
        ):
            return True
        if type(entry) is Dir:
            pending.extend(entry.children.values())
        elif any(base is Dir for base in entry_mro):
            return True
    return False


def _mount_has_or_may_hide_configured_authority(mount: Mount) -> bool:
    """Classify untrusted mount implementations without reading their configuration."""

    if not _mount_class_is_trusted(mount):
        return True
    strategy = mount.mount_strategy
    if _strategy_classification(strategy)[0] == "unknown":
        return True
    pattern = getattr(strategy, "pattern", None)
    if pattern is not None and not _pattern_class_is_trusted(pattern):
        return True
    mount_type = _canonical_mount_type(type(mount)) or mount.type
    capability = _in_container_mount_credential_capability(mount_type, strategy)
    if capability is not None and capability.enables_broad_credential_discovery:
        return True
    return bool(_configured_mount_authority_fields(mount))


def _decorated_owner_type(function: Callable[..., object]) -> type | None:
    """Resolve the SDK class that owns a decorated function without importing modules."""

    if type(function) is not types.FunctionType:
        return None
    module_name = function.__module__
    qualname = function.__qualname__
    if (
        type(module_name) is not str
        or not module_name.startswith("agents.")
        or type(qualname) is not str
        or qualname.count(".") != 1
    ):
        return None
    owner_name, _ = qualname.split(".", 1)
    module = sys.modules.get(module_name)
    if type(module) is not types.ModuleType:
        return None
    module_dict_descriptor = cast(Any, types.ModuleType).__dict__["__dict__"]
    module_state = module_dict_descriptor.__get__(module, types.ModuleType)
    if type(module_state) is not dict:
        return None
    candidate = _exact_string_state_value(module_state, owner_name)
    if not isinstance(candidate, type) or _static_type_metadata(candidate) is None:
        return None
    return candidate


def _trusted_authority_owner_base(
    value: object,
    *,
    decorated_owner_type: type | None,
) -> type | None:
    """Return a canonical SDK base whose instance-state descriptor is trusted."""

    from ..run_config import SandboxRunConfig
    from .sandbox_agent import SandboxAgent
    from .session.base_sandbox_session import BaseSandboxSession
    from .session.sandbox_client import BaseSandboxClient
    from .session.sandbox_session_state import SandboxSessionState

    metadata = _static_type_metadata(type(value))
    if metadata is None:
        return None
    _, value_mro = metadata
    candidates = (
        decorated_owner_type,
        BaseSandboxSession,
        BaseSandboxClient,
        SandboxSessionState,
        SandboxRunConfig,
        SandboxAgent,
        MountStrategyBase,
    )
    for candidate in candidates:
        if candidate is not None and any(base is candidate for base in value_mro):
            return candidate
    return None


def _exact_slot_state_entry(
    value: object,
    *,
    trusted_base: type,
    name: str,
) -> tuple[bool, bool, object | None]:
    """Read one exact Python slot without invoking provider attribute hooks.

    The first boolean reports whether the named state is declared by the
    inspected hierarchy. The second reports whether that declaration is an
    exact Python slot. A non-slot declaration is returned as the third value
    for identity comparison only. A declared but unreadable slot is unresolved
    and returns ``(True, True, None)`` so callers can fail closed.
    """

    value_metadata = _static_type_metadata(type(value))
    if value_metadata is None:
        return False, False, None
    _, value_mro = value_metadata
    if not any(base is trusted_base for base in value_mro):
        return False, False, None

    for base in value_mro:
        base_metadata = _static_type_metadata(base)
        if base_metadata is None:
            return False, False, None
        namespace, _ = base_metadata
        for candidate, descriptor in namespace.items():
            if type(candidate) is not str or str.__eq__(candidate, name) is not True:
                continue
            if type(descriptor) is not types.MemberDescriptorType:
                return True, False, descriptor
            try:
                slot_value = descriptor.__get__(value, type(value))
            except BaseException:
                return True, True, None
            return True, True, slot_value
        if base is trusted_base:
            break
    return False, False, None


def _call_has_configured_mount_authority(
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
    *,
    function: Callable[..., object],
) -> bool:
    """Inspect SDK-owned call-boundary state for protected authority."""

    from .manifest import Manifest
    from .session.base_sandbox_session import BaseSandboxSession
    from .session.sandbox_session import SandboxSession

    try:
        sandbox_session_metadata = _static_type_metadata(SandboxSession)
        if sandbox_session_metadata is None:
            return True
        sandbox_session_state_descriptor = sandbox_session_metadata[0]["state"]
        values = (*args, *kwargs.values())
        for value in values:
            if type(value) is Manifest and _manifest_has_configured_mount_authority(value):
                return True

        decorated_owner_type = _decorated_owner_type(function)
        pending = [(value, False) for value in values]
        seen_optional: set[int] = set()
        seen_required: set[int] = set()
        while pending:
            value, must_resolve_owner = pending.pop()
            value_id = id(value)
            if value_id in seen_required or (not must_resolve_owner and value_id in seen_optional):
                continue
            if must_resolve_owner:
                seen_required.add(value_id)
            else:
                seen_optional.add(value_id)

            value_metadata = _static_type_metadata(type(value))
            value_mro = () if value_metadata is None else value_metadata[1]
            if type(value) is Manifest:
                if _manifest_has_configured_mount_authority(value):
                    return True
                continue
            if any(base is Manifest for base in value_mro):
                return True
            if any(base is Mount for base in value_mro):
                if _mount_has_or_may_hide_configured_authority(cast(Mount, value)):
                    return True
                continue
            if type(value) is dict:
                pending.extend((item, must_resolve_owner) for item in dict.values(value))
                continue
            if type(value) is list or type(value) is tuple:
                pending.extend((item, must_resolve_owner) for item in value)
                continue
            if value is None or type(value) in (str, bytes, int, float, bool, complex):
                continue
            if any(base is PurePath for base in value_mro):
                continue

            trusted_base = _trusted_authority_owner_base(
                value,
                decorated_owner_type=decorated_owner_type,
            )
            if trusted_base is None:
                if must_resolve_owner:
                    return True
                continue
            state = _object_instance_dict(value, trusted_base=trusted_base)
            if state is None:
                return True
            is_session_owner = any(base is BaseSandboxSession for base in value_mro)
            for name in _CALL_AUTHORITY_REQUIRED_STATE_KEYS:
                found, candidate = _exact_string_state_entry(state, name)
                if name == "state" and is_session_owner:
                    if not found:
                        state_declared, is_exact_slot, candidate = _exact_slot_state_entry(
                            value,
                            trusted_base=trusted_base,
                            name=name,
                        )
                        if state_declared and is_exact_slot:
                            if candidate is None:
                                return True
                            found = True
                        elif state_declared and candidate is sandbox_session_state_descriptor:
                            inner_found, inner = _exact_string_state_entry(state, "_inner")
                            if (
                                not inner_found
                                or inner is None
                                or _trusted_authority_owner_base(
                                    inner,
                                    decorated_owner_type=decorated_owner_type,
                                )
                                is None
                            ):
                                return True
                        elif state_declared:
                            return True
                if found and candidate is not None:
                    pending.append((candidate, True))
            for name in _CALL_AUTHORITY_LINK_STATE_KEYS:
                found, candidate = _exact_string_state_entry(state, name)
                if found and candidate is not None:
                    pending.append((candidate, False))

            credentials_found, credentials = _exact_string_state_entry(
                state,
                "_trusted_s3_mount_credentials",
            )
            if type(credentials) is dict:
                for configured in dict.values(credentials):
                    if type(configured) is tuple and any(item is not None for item in configured):
                        return True
            elif credentials_found and credentials is not None:
                return True
    except BaseException:
        return True
    return False


def _strategy_classification(strategy: MountStrategyBase) -> tuple[str, str | None]:
    for strategy_type, classification in _STRATEGY_CLASSIFICATION_BY_TYPE.items():
        if type(strategy) is _trusted_strategy_class(strategy_type):
            if strategy.type != strategy_type:
                return "unknown", None
            boundary, backend_id, _module_name, _class_name = classification
            return boundary, backend_id
    return "unknown", None


def _redact_mount_serialization_error(error: BaseException) -> MountConfigError:
    discard_mount_source_exception(error)
    safe_error = MountConfigError(
        message="sandbox session state containing mount authority could not be serialized"
    )
    _mark_error_data_redacted(safe_error)
    return safe_error


def _redact_mount_state_validation_error(error: BaseException, *, message: str) -> ValueError:
    discard_mount_source_exception(error)
    safe_error = ValueError(message)
    _mark_error_data_redacted(safe_error)
    return safe_error


def _mark_mount_error_for_manifest(error: MountConfigError, manifest: Manifest) -> None:
    if _manifest_has_configured_mount_authority(manifest):
        _mark_error_data_redacted(error)


def _mark_mount_error_data_safe(error: MountConfigError) -> None:
    """Mark an SDK-created mount error whose message contains no credential-derived data."""
    _mark_error_data_redacted(error)
    setattr(
        error,
        _SAFE_MOUNT_VALIDATION_MESSAGE_ATTR,
        _SAFE_MOUNT_VALIDATION_MESSAGE_MARKER,
    )


def _mark_mount_validation_error(error: MountConfigError) -> None:
    _mark_mount_error_data_safe(error)


def _absolute_manifest_path(root: str, value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute():
        path = PurePosixPath(root) / path
    normalized: list[str] = []
    for part in path.parts:
        if part in {"", ".", "/"}:
            continue
        if part == "..":
            if normalized:
                normalized.pop()
            continue
        normalized.append(part)
    return "/" + "/".join(normalized)


def _manifest_materializes_path(manifest: Manifest, target: str) -> bool:
    """Return whether an entry can place content at a workspace credential path."""

    target_path = PurePosixPath(_absolute_manifest_path(manifest.root, target))
    for path, entry in manifest.iter_entries():
        entry_path = PurePosixPath(_absolute_manifest_path(manifest.root, path.as_posix()))
        overlaps_target = entry_path == target_path or entry_path in target_path.parents
        if not overlaps_target:
            continue
        proven_structural_directory = type(entry) is Dir or (
            type(entry) is LocalDir and entry.src is None
        )
        if not proven_structural_directory:
            return True
    return False


def _manifest_mount_provenance_error(manifest: Manifest) -> MountConfigError | None:
    """Reject unsupported mount classes before invoking their behavior or serializers."""

    for _path, entry in manifest.iter_entries():
        if not isinstance(entry, Mount):
            continue
        if error := _mount_provenance_error(entry):
            return error
    return None


def _mount_provenance_error(
    mount: Mount,
    strategy: MountStrategyBase | None = None,
) -> MountConfigError | None:
    """Validate exact SDK class provenance without copying or inspecting configuration values."""

    if not _mount_class_is_trusted(mount):
        return MountConfigError(
            message=(
                "custom mount implementations are not supported at the sandbox credential boundary"
            )
        )
    resolved_strategy = strategy if strategy is not None else mount.mount_strategy
    if _strategy_classification(resolved_strategy)[0] == "unknown":
        return MountConfigError(
            message="custom mount strategies are not supported at the sandbox credential boundary"
        )
    pattern = getattr(resolved_strategy, "pattern", None)
    if pattern is not None and not _pattern_class_is_trusted(pattern):
        return MountConfigError(
            message="custom mount patterns are not supported at the sandbox credential boundary"
        )
    return None


def _validate_mount_provenance(
    mount: Mount,
    strategy: MountStrategyBase | None = None,
) -> None:
    error = _mount_provenance_error(mount, strategy)
    if error is None:
        return
    _mark_mount_validation_error(error)
    mount = cast(Any, None)
    strategy = cast(Any, None)
    _raise_data_redacted_error(error)


def _validate_manifest_mount_provenance(manifest: Manifest) -> None:
    error = _manifest_mount_provenance_error(manifest)
    if error is None:
        return
    _mark_mount_validation_error(error)
    manifest = cast(Any, None)
    _raise_data_redacted_error(error)


def _resolved_in_container_pattern_type(strategy: MountStrategyBase) -> str | None:
    pattern = getattr(strategy, "pattern", None)
    pattern_type = getattr(pattern, "type", None)
    return pattern_type if isinstance(pattern_type, str) else None


def _in_container_mount_credential_capability(
    mount_type: str,
    strategy: MountStrategyBase,
) -> _InContainerMountCredentialCapability | None:
    return _in_container_mount_credential_capability_for_types(
        mount_type,
        strategy.type,
        _resolved_in_container_pattern_type(strategy),
    )


def _in_container_mount_credential_capability_for_types(
    mount_type: str,
    strategy_type: str,
    pattern_type: str | None,
) -> _InContainerMountCredentialCapability | None:
    return next(
        (
            capability
            for capability in _IN_CONTAINER_MOUNT_CREDENTIAL_CAPABILITIES
            if strategy_type in capability.strategy_types
            and mount_type == capability.mount_type
            and pattern_type == capability.pattern_type
        ),
        None,
    )


def _mount_boundary_error(
    manifest: Manifest,
    mount: Mount,
    mount_path: str | PurePath,
    *,
    provider_backend_id: str | None,
) -> MountConfigError | None:
    mount_type = _canonical_mount_type(type(mount)) or mount.type
    strategy = mount.mount_strategy
    strategy_boundary, strategy_backend_id = _strategy_classification(strategy)
    pattern = getattr(strategy, "pattern", None)
    executes_in_container = strategy_boundary == "in_container"
    if (
        provider_backend_id is not None
        and strategy_backend_id is not None
        and strategy_backend_id != provider_backend_id
    ):
        return MountConfigError(
            message=(
                "docker-volume mounts are not supported by this sandbox backend"
                if strategy.type == "docker_volume"
                else "mount strategy is not supported by this sandbox backend"
            ),
            context={
                "mount_type": mount.type,
                "strategy_type": strategy.type,
                "sandbox_backend": provider_backend_id,
            },
        )

    for field_name in _AUTHORITY_FILE_FIELDS_BY_MOUNT_TYPE.get(mount_type, ()):
        value = getattr(mount, field_name, None)
        if isinstance(value, str) and value and _manifest_materializes_path(manifest, value):
            return MountConfigError(
                message=(
                    "credential files stored in the manifest are not supported for cloud "
                    "mounts; configure credentials outside the sandbox manifest"
                ),
                context={"mount_type": mount.type, "credential_field": field_name},
            )
    if (
        isinstance(pattern, RcloneMountPattern)
        and pattern.config_file_path is not None
        and _manifest_materializes_path(manifest, pattern.config_file_path.as_posix())
    ):
        return MountConfigError(
            message=(
                "credential files stored in the manifest are not supported for cloud mounts; "
                "configure credentials outside the sandbox manifest"
            ),
            context={
                "mount_type": mount.type,
                "credential_field": "mount_strategy.pattern.config_file_path",
            },
        )

    invalid_rclone_fields = _configured_rclone_line_fields(mount, mount_type)
    if executes_in_container and isinstance(pattern, RcloneMountPattern) and invalid_rclone_fields:
        return MountConfigError(
            message="cloud mount configuration values must not contain line breaks",
            context={
                "mount_type": mount.type,
                "configuration_fields": invalid_rclone_fields,
            },
        )
    invalid_s3fs_fields = _configured_blaxel_s3fs_option_fields(mount, mount_type)
    if invalid_s3fs_fields:
        return MountConfigError(
            message="cloud mount configuration values must not contain s3fs option delimiters",
            context={
                "mount_type": mount.type,
                "configuration_fields": invalid_s3fs_fields,
            },
        )

    invalid_credential_set_fields = (
        _invalid_in_container_credential_set_fields(mount, mount_type)
        if executes_in_container
        else ()
    )
    if invalid_credential_set_fields:
        return MountConfigError(
            message="in-container access credentials require a complete non-empty credential set",
            context={
                "mount_type": mount.type,
                "credential_fields": invalid_credential_set_fields,
            },
        )

    invalid_authority_value_fields = (
        _invalid_in_container_authority_value_fields(mount, mount_type)
        if executes_in_container
        else ()
    )
    if invalid_authority_value_fields:
        return MountConfigError(
            message="in-container mount authentication values must not be empty or whitespace-only",
            context={
                "mount_type": mount.type,
                "credential_fields": invalid_authority_value_fields,
            },
        )

    authority_fields = frozenset(_configured_mount_authority_fields(mount))
    if strategy_boundary == "external":
        return None

    mount_scoped_acknowledged = manifest._acknowledges_in_container_mount_credential_exposure(
        mount_path,
        "mount_scoped",
    )
    broad_acknowledged = manifest._acknowledges_in_container_mount_credential_exposure(
        mount_path,
        "broad",
    )
    capability = _in_container_mount_credential_capability(mount_type, strategy)
    requires_implicit_broad = bool(
        capability is not None and capability.enables_broad_credential_discovery
    )
    if (
        capability is not None
        and capability.required_any_fields
        and not _mount_has_usable_required_authority(mount, capability.required_any_fields)
    ):
        return MountConfigError(
            message=(
                "in-container Box mounts require a non-interactive authentication source; "
                "configure a token, access token, or JWT credentials before activation"
            ),
            context={"mount_type": mount.type},
        )
    if (
        not authority_fields
        and not mount_scoped_acknowledged
        and not broad_acknowledged
        and not requires_implicit_broad
    ):
        return None
    if capability is None:
        return MountConfigError(
            message=(
                "credential-bearing in-container mounts require an SDK-supported strategy, "
                "mount type, and pattern combination before exposure can be acknowledged; "
                "use a credentialless helper or an external/provider-native mount strategy"
            ),
            context={
                "mount_type": mount.type,
                "strategy_type": strategy.type,
                "pattern_type": _resolved_in_container_pattern_type(strategy),
                "credential_fields": tuple(sorted(authority_fields)),
            },
        )

    supported_fields = capability.mount_scoped_fields | capability.broad_fields
    unsupported_fields = authority_fields - supported_fields
    if unsupported_fields:
        return MountConfigError(
            message=(
                "the selected in-container mount capability does not support exposing the "
                "configured credential fields; use supported credentials, a credentialless "
                "helper, or an external/provider-native mount strategy"
            ),
            context={
                "mount_type": mount.type,
                "strategy_type": strategy.type,
                "credential_fields": tuple(sorted(unsupported_fields)),
            },
        )

    supports_mount_scoped = bool(capability.mount_scoped_fields)
    supports_broad = bool(capability.broad_fields or capability.enables_broad_credential_discovery)
    if mount_scoped_acknowledged and not supports_mount_scoped:
        return MountConfigError(
            message=(
                "the selected in-container mount capability does not support mount-scoped "
                "credentials"
            ),
            context={"mount_type": mount.type, "strategy_type": strategy.type},
        )
    if broad_acknowledged and not supports_broad:
        return MountConfigError(
            message=(
                "the selected in-container mount capability does not support broad credential "
                "authority"
            ),
            context={"mount_type": mount.type, "strategy_type": strategy.type},
        )

    requires_mount_scoped = bool(authority_fields & capability.mount_scoped_fields)
    requires_broad = bool(authority_fields & capability.broad_fields) or requires_implicit_broad
    if requires_mount_scoped and not mount_scoped_acknowledged:
        return MountConfigError(
            message=(
                "mount-scoped credentials cannot be exposed to a helper inside a "
                "model-controlled sandbox by default; use a credentialless or "
                "external/provider-native strategy, or explicitly acknowledge exposure for "
                "this exact path with "
                "Manifest.with_in_container_mount_credential_exposure_acknowledged()"
            ),
            context={
                "mount_type": mount.type,
                "credential_fields": tuple(
                    sorted(authority_fields & capability.mount_scoped_fields)
                ),
            },
        )
    if requires_broad and not broad_acknowledged:
        return MountConfigError(
            message=(
                "broad credential authority cannot be exposed to a helper inside a "
                "model-controlled sandbox by default; use a credentialless or "
                "external/provider-native strategy, or explicitly acknowledge broad exposure "
                "for this exact path with "
                "Manifest.with_in_container_mount_broad_credential_exposure_acknowledged()"
            ),
            context={
                "mount_type": mount.type,
                "credential_fields": tuple(sorted(authority_fields & capability.broad_fields)),
            },
        )
    return None


def _manifest_boundary_error(
    manifest: Manifest,
    *,
    provider_backend_id: str | None,
) -> MountConfigError | None:
    provenance_error = _manifest_mount_provenance_error(manifest)
    if provenance_error is not None:
        return provenance_error
    for mount, mount_path in manifest.mount_targets():
        if error := _mount_boundary_error(
            manifest,
            mount,
            PurePosixPath(mount_path.as_posix()),
            provider_backend_id=provider_backend_id,
        ):
            return error
    return None


def validate_manifest_mount_credential_boundaries(
    manifest: Manifest,
    *,
    provider_backend_id: str | None = None,
) -> None:
    """Validate all mount authority before a sandbox or helper has side effects."""

    error = _manifest_boundary_error(
        manifest,
        provider_backend_id=provider_backend_id,
    )
    if error is None:
        return
    _mark_mount_validation_error(error)
    del manifest
    provider_backend_id = None
    _raise_data_redacted_error(error)


def validate_mount_activation_credential_boundary(
    mount: Mount,
    strategy: MountStrategyBase,
    *,
    manifest: Manifest | None = None,
    mount_path: str | PurePath | Callable[[], str | PurePath] | None = None,
    provider_backend_id: str | None = None,
) -> None:
    """Revalidate the strategy that is about to execute inside a sandbox."""

    from .manifest import Manifest

    _validate_mount_provenance(mount, strategy)
    activation_mount = mount.model_copy(deep=True, update={"mount_strategy": strategy})
    activation_manifest = manifest or Manifest(entries={"mount": activation_mount})
    resolved_mount_path = mount_path() if callable(mount_path) else mount_path
    activation_path = resolved_mount_path or PurePosixPath(activation_manifest.root) / "mount"
    error = _mount_boundary_error(
        activation_manifest,
        activation_mount,
        activation_path,
        provider_backend_id=provider_backend_id,
    )
    if error is None:
        return
    _mark_mount_validation_error(error)
    del mount, strategy, activation_mount, activation_manifest
    mount_path = None
    resolved_mount_path = None
    provider_backend_id = None
    _raise_data_redacted_error(error)


def sanitize_manifest_mount_authority(manifest: Manifest) -> tuple[Manifest, bool]:
    """Return a typed manifest whose durable form contains no mount authority."""

    from .manifest import Manifest

    provenance_error = _manifest_mount_provenance_error(manifest)
    if provenance_error is not None:
        _mark_mount_validation_error(provenance_error)
        manifest = cast(Any, None)
        _raise_data_redacted_error(provenance_error)

    safe_error: MountConfigError | None = None
    try:
        raw_manifest = manifest.model_dump(mode="json")
    except Exception as error:
        if not _manifest_has_configured_mount_authority(manifest):
            raise
        safe_error = _redact_mount_serialization_error(error)

    if safe_error is not None:
        manifest = cast(Any, None)
        _raise_data_redacted_error(safe_error)

    sanitized, redacted = sanitize_raw_manifest_mount_authority(raw_manifest)
    assert isinstance(sanitized, dict)
    return Manifest.model_validate(sanitized), redacted


def rebind_manifest_mount_authority(
    persisted_manifest: Manifest,
    trusted_manifest: Manifest,
    *,
    provider_backend_id: str,
) -> Manifest:
    """Restore live authority after exact credential-free topology matching."""

    error = _manifest_boundary_error(
        trusted_manifest,
        provider_backend_id=provider_backend_id,
    )
    sanitized_persisted, _ = sanitize_manifest_mount_authority(persisted_manifest)
    sanitized_trusted, _ = sanitize_manifest_mount_authority(trusted_manifest)
    persisted_mounts = {
        path.as_posix(): entry
        for path, entry in sanitized_persisted.iter_entries()
        if isinstance(entry, Mount)
    }
    trusted_mounts = {
        path.as_posix(): entry
        for path, entry in sanitized_trusted.iter_entries()
        if isinstance(entry, Mount)
    }
    topology_matches = (
        sanitized_persisted.root == sanitized_trusted.root
        and persisted_mounts.keys() == trusted_mounts.keys()
        and all(
            persisted_mounts[path].model_dump(mode="json")
            == trusted_mounts[path].model_dump(mode="json")
            for path in persisted_mounts
        )
    )
    if error is None and not topology_matches:
        error = MountConfigError(
            message=(
                "sandbox mount configuration can be rebound only from a current trusted "
                "mount configuration with exactly matching credential-free topology"
            ),
            context={"sandbox_backend": provider_backend_id},
        )
    if error is not None:
        _mark_mount_validation_error(error)
        persisted_manifest = cast(Any, None)
        trusted_manifest = cast(Any, None)
        sanitized_persisted = cast(Any, None)
        sanitized_trusted = cast(Any, None)
        persisted_mounts = {}
        trusted_mounts = {}
        provider_backend_id = ""
        _raise_data_redacted_error(error)

    rebound = persisted_manifest.model_copy(deep=True)
    trusted_entries = {
        path.as_posix(): entry
        for path, entry in trusted_manifest.iter_entries()
        if isinstance(entry, Mount)
    }
    for path, entry in rebound.iter_entries():
        if isinstance(entry, Mount):
            entry.__dict__.update(trusted_entries[path.as_posix()].model_copy(deep=True).__dict__)
    rebound._copy_mount_credential_exposure_policy_from(trusted_manifest)
    return rebound


def _iter_raw_entries(
    entries: object,
    registered_entry_types: Mapping[str, type[BaseEntry]],
    parent: PurePosixPath | None = None,
) -> Iterable[tuple[PurePosixPath, dict[str, Any]]]:
    parent = parent or PurePosixPath()
    if not isinstance(entries, Mapping):
        return
    for name, value in entries.items():
        if not isinstance(value, dict):
            continue
        path = parent / PurePosixPath(str(name))
        yield path, value
        entry_type = value.get("type")
        entry_class = (
            registered_entry_types.get(entry_type) if isinstance(entry_type, str) else None
        )
        if entry_type in _SDK_EXTENSION_MOUNT_ENTRY_TYPES or (
            entry_class is not None and not issubclass(entry_class, Dir)
        ):
            continue
        children = value.get("children")
        if isinstance(children, Mapping):
            yield from _iter_raw_entries(children, registered_entry_types, path)


def _raw_entry_tree_is_valid(
    entries: object,
    registered_entry_types: Mapping[str, type[BaseEntry]],
) -> bool:
    if not isinstance(entries, Mapping):
        return False
    for entry in entries.values():
        if not isinstance(entry, Mapping):
            return False
        entry_type = entry.get("type")
        if not isinstance(entry_type, str):
            return False
        entry_class = registered_entry_types.get(entry_type)
        if entry_type in _SDK_EXTENSION_MOUNT_ENTRY_TYPES or (
            entry_class is not None and not issubclass(entry_class, Dir)
        ):
            continue
        if "children" in entry and not _raw_entry_tree_is_valid(
            entry["children"], registered_entry_types
        ):
            return False
    return True


def _raw_entry_is_structural_directory(
    entry: Mapping[str, Any], entry_class: type[BaseEntry] | None
) -> bool:
    if entry_class is Dir:
        return True
    return entry_class is LocalDir and entry.get("src") is None


def _sanitize_raw_credential_file_sources(
    *,
    root: str,
    raw_entries: Iterable[tuple[PurePosixPath, dict[str, Any]]],
    authority_file_paths: Iterable[str],
    registered_entry_types: Mapping[str, type[BaseEntry]],
) -> bool:
    """Remove inline credential content and reject non-inline materializers."""

    entries = tuple(raw_entries)
    redacted = False
    for authority_file_path in authority_file_paths:
        target = PurePosixPath(_absolute_manifest_path(root, authority_file_path))
        for path, entry in entries:
            entry_path = PurePosixPath(_absolute_manifest_path(root, path.as_posix()))
            entry_type = entry.get("type")
            entry_class = (
                registered_entry_types.get(entry_type) if isinstance(entry_type, str) else None
            )
            if entry_path == target and entry_class is File:
                entry["content"] = ""
                redacted = True
                continue
            if entry_path == target or entry_path in target.parents:
                if _raw_entry_is_structural_directory(entry, entry_class):
                    continue
                error = _InvalidRawMountManifestError(
                    "sandbox manifest credential-file source cannot be restored safely"
                )
                _mark_error_data_redacted(error)
                _raise_data_redacted_error(error)
    return redacted


def _strip_raw_configuration(
    configuration: dict[str, Any],
    safe_fields: Collection[str],
) -> bool:
    opaque_fields = tuple(name for name in configuration if name not in safe_fields)
    for name in opaque_fields:
        configuration.pop(name, None)
    return bool(opaque_fields)


def _strip_raw_strategy_configuration(strategy: dict[str, Any]) -> bool:
    return _strip_raw_configuration(strategy, {"type"})


def _sanitize_raw_mount(
    entry: dict[str, Any],
    entry_class: type[BaseEntry] | None,
) -> tuple[bool, tuple[str, ...]]:
    raw_mount_type = entry.get("type")
    canonical_mount_type = _canonical_mount_type(entry_class) if entry_class is not None else None
    mount_type = canonical_mount_type or (raw_mount_type if isinstance(raw_mount_type, str) else "")
    redacted = False
    authority_file_paths: list[str] = []
    canonical_mount_class = _canonical_mount_class(entry_class) if entry_class is not None else None
    safe_mount_fields: Collection[str] | None = None
    if canonical_mount_class is not None:
        safe_mount_fields = canonical_mount_class.model_fields
    elif mount_type in _SDK_EXTENSION_MOUNT_SERIALIZED_FIELDS_BY_TYPE:
        safe_mount_fields = _SDK_EXTENSION_MOUNT_SERIALIZED_FIELDS_BY_TYPE[mount_type]
    if safe_mount_fields is not None:
        opaque_mount_fields = tuple(name for name in entry if name not in safe_mount_fields)
        for name in opaque_mount_fields:
            entry.pop(name, None)
        redacted = redacted or bool(opaque_mount_fields)
    authority_file_fields = _AUTHORITY_FILE_FIELDS_BY_MOUNT_TYPE.get(mount_type, ())
    for name in _AUTHORITY_FIELDS_BY_MOUNT_TYPE.get(mount_type, ()):
        value = entry.get(name)
        if value is None:
            continue
        entry[name] = None
        redacted = True
        if name in authority_file_fields:
            if not isinstance(value, str):
                error = _InvalidRawMountManifestError(
                    "sandbox manifest credential-file path has an invalid shape"
                )
                _mark_error_data_redacted(error)
                _raise_data_redacted_error(error)
            authority_file_paths.append(value)
    for name in _URL_FIELDS_BY_MOUNT_TYPE.get(mount_type, ()):
        if _url_contains_inline_authority(entry.get(name)):
            entry[name] = None
            redacted = True
    for name in _RCLONE_CONFIG_VALUE_FIELDS_BY_MOUNT_TYPE.get(mount_type, ()):
        if _value_contains_config_line_break(entry.get(name)):
            entry[name] = ""
            redacted = True
    if mount_type == "s3_files_mount" and entry.get("extra_options"):
        entry["extra_options"] = {}
        redacted = True

    strategy = entry.get("mount_strategy")
    if not isinstance(strategy, dict):
        if strategy is not None:
            error = _InvalidRawMountManifestError(
                "sandbox manifest mount strategy has an invalid shape"
            )
            _mark_error_data_redacted(error)
            _raise_data_redacted_error(error)
        return redacted, tuple(authority_file_paths)
    raw_strategy_type = strategy.get("type")
    if isinstance(raw_strategy_type, str):
        strategy_type = raw_strategy_type
    else:
        strategy["type"] = None
        strategy_type = ""
        redacted = True
    invalid_s3fs_fields = tuple(
        name
        for name in _BLAXEL_S3FS_OPTION_FIELDS_BY_MOUNT_TYPE.get(mount_type, ())
        if _value_contains_s3fs_option_delimiter(entry.get(name))
    )
    if strategy_type == "blaxel_cloud_bucket" and invalid_s3fs_fields:
        error = _InvalidRawMountManifestError(
            "sandbox manifest cloud mount configuration contains an s3fs option delimiter"
        )
        _mark_error_data_redacted(error)
        _raise_data_redacted_error(error)
    serialized_strategy_fields = _SERIALIZED_FIELDS_BY_STRATEGY_TYPE.get(strategy_type)
    if isinstance(raw_strategy_type, str) and serialized_strategy_fields is None:
        error = _InvalidRawMountManifestError("sandbox manifest mount strategy has an unknown type")
        _mark_error_data_redacted(error)
        _raise_data_redacted_error(error)
    trusted_strategy_class = _trusted_strategy_class(strategy_type)
    if serialized_strategy_fields is not None and (
        MountStrategyBase._subclass_registry.get(strategy_type) is not trusted_strategy_class
    ):
        error = _InvalidRawMountManifestError("custom mount strategies cannot be restored safely")
        _mark_error_data_redacted(error)
        _raise_data_redacted_error(error)
    strip_unknown_strategy_fields = serialized_strategy_fields is None
    if serialized_strategy_fields is not None:
        redacted = _strip_raw_configuration(strategy, serialized_strategy_fields) or redacted
    opaque_fields = _OPAQUE_STRATEGY_AUTHORITY_FIELDS.get(
        strategy_type,
        (),
    )
    for name in opaque_fields:
        value = strategy.get(name)
        if value:
            strategy[name] = {} if isinstance(value, Mapping) else None
            redacted = True
    pattern = strategy.get("pattern")
    if not isinstance(pattern, dict):
        if pattern is not None:
            error = _InvalidRawMountManifestError(
                "sandbox manifest mount pattern has an invalid shape"
            )
            _mark_error_data_redacted(error)
            _raise_data_redacted_error(error)
        if strip_unknown_strategy_fields:
            redacted = _strip_raw_strategy_configuration(strategy) or redacted
        return redacted, tuple(authority_file_paths)
    pattern_type = pattern.get("type")
    if not isinstance(pattern_type, str):
        pattern["type"] = None
        redacted = True
    capability = _in_container_mount_credential_capability_for_types(
        mount_type,
        strategy_type,
        pattern_type if isinstance(pattern_type, str) else None,
    )
    if capability is not None and capability.enables_broad_credential_discovery:
        # Runtime-only broad acknowledgement must be rebound after serialization even when the
        # helper discovers ambient authority without an explicit credential field to redact.
        redacted = True
    serialized_pattern_class = (
        _SERIALIZED_PATTERN_CLASS_BY_TYPE.get(pattern_type)
        if isinstance(pattern_type, str)
        else None
    )
    if isinstance(pattern_type, str) and serialized_pattern_class is None:
        error = _InvalidRawMountManifestError("sandbox manifest mount pattern has an unknown type")
        _mark_error_data_redacted(error)
        _raise_data_redacted_error(error)
    serialized_pattern_fields: Collection[str] = (
        serialized_pattern_class.model_fields if serialized_pattern_class is not None else {"type"}
    )
    if not _rclone_remote_name_is_safe(pattern.get("remote_name")):
        pattern["remote_name"] = None
        redacted = True
    config_path = pattern.get("config_file_path")
    if config_path is not None:
        pattern["config_file_path"] = None
        redacted = True
        if not isinstance(config_path, str):
            error = _InvalidRawMountManifestError(
                "sandbox manifest rclone config-file path has an invalid shape"
            )
            _mark_error_data_redacted(error)
            _raise_data_redacted_error(error)
        authority_file_paths.append(config_path)
    extra_args = pattern.get("extra_args", [])
    safe_extra_args = False
    extra_config_paths: tuple[str, ...] = ()
    invalid_config_path = False
    if isinstance(extra_args, list | tuple):
        safe_extra_args, extra_config_paths, invalid_config_path = _rclone_extra_args_analysis(
            extra_args
        )
    if invalid_config_path:
        error = _InvalidRawMountManifestError(
            "sandbox manifest rclone config argument has an invalid shape"
        )
        _mark_error_data_redacted(error)
        _raise_data_redacted_error(error)
    if not safe_extra_args:
        if extra_args:
            pattern["extra_args"] = []
            redacted = True
        authority_file_paths.extend(extra_config_paths)
    options = pattern.get("options")
    if isinstance(options, dict):
        if _url_contains_inline_authority(options.get("endpoint_url")):
            options["endpoint_url"] = None
            redacted = True
        if options.get("extra_options") and (
            mount_type == "s3_files_mount"
            or pattern_type == "s3files"
            or not isinstance(pattern_type, str)
        ):
            options["extra_options"] = {}
            redacted = True
        serialized_options_class = (
            _SERIALIZED_OPTIONS_CLASS_BY_PATTERN_TYPE.get(pattern_type)
            if isinstance(pattern_type, str)
            else None
        )
        serialized_options_fields = (
            tuple(field.name for field in dataclasses.fields(serialized_options_class))
            if serialized_options_class is not None
            else ()
        )
        redacted = _strip_raw_configuration(options, serialized_options_fields) or redacted
    elif options is not None:
        pattern["options"] = {}
        redacted = True
    redacted = _strip_raw_configuration(pattern, serialized_pattern_fields) or redacted
    if strip_unknown_strategy_fields:
        redacted = _strip_raw_strategy_configuration(strategy) or redacted
    return redacted, tuple(authority_file_paths)


def sanitize_raw_manifest_mount_authority(payload: object) -> tuple[object, bool]:
    """Sanitize the documented raw manifest shape without importing provider state classes."""

    if not isinstance(payload, Mapping):
        return payload, False
    manifest = copy.deepcopy(dict(payload))
    registered_entry_types = BaseEntry.registered_types()
    if "entries" in manifest and not _raw_entry_tree_is_valid(
        manifest["entries"], registered_entry_types
    ):
        if isinstance(payload, dict):
            payload.clear()
        error = _InvalidRawMountManifestError("sandbox manifest entries have an invalid shape")
        _mark_error_data_redacted(error)
        _raise_data_redacted_error(error)
    root_value = manifest.get("root")
    root = root_value if isinstance(root_value, str) else "/workspace"
    raw_entries = list(_iter_raw_entries(manifest.get("entries"), registered_entry_types))
    redacted = False
    authority_file_paths: set[str] = set()
    for _path, entry in raw_entries:
        entry_type = entry.get("type")
        entry_class = (
            registered_entry_types.get(entry_type) if isinstance(entry_type, str) else None
        )
        if entry_class is None:
            if entry_type not in _SDK_EXTENSION_MOUNT_ENTRY_TYPES:
                if "mount_strategy" not in entry:
                    continue
                error = _InvalidRawMountManifestError(
                    "sandbox manifest contains an unknown mount-like entry"
                )
                _mark_error_data_redacted(error)
                _raise_data_redacted_error(error)
        elif not issubclass(entry_class, Mount):
            continue
        elif not _mount_entry_class_is_trusted(entry_class):
            error = _InvalidRawMountManifestError(
                "custom mount implementations cannot be restored safely"
            )
            _mark_error_data_redacted(error)
            _raise_data_redacted_error(error)
        entry_redacted, file_paths = _sanitize_raw_mount(entry, entry_class)
        redacted = redacted or entry_redacted
        authority_file_paths.update(_absolute_manifest_path(root, value) for value in file_paths)
    redacted = (
        _sanitize_raw_credential_file_sources(
            root=root,
            raw_entries=raw_entries,
            authority_file_paths=authority_file_paths,
            registered_entry_types=registered_entry_types,
        )
        or redacted
    )

    return manifest, redacted


def sanitize_raw_session_state_mount_authority(payload: object) -> tuple[object, bool]:
    if not isinstance(payload, Mapping):
        return payload, False
    state = copy.deepcopy(dict(payload))
    if "manifest" in state and not isinstance(state["manifest"], Mapping):
        if isinstance(payload, dict):
            payload.clear()
        error = _InvalidRawMountManifestError("sandbox manifest has an invalid shape")
        _mark_error_data_redacted(error)
        _raise_data_redacted_error(error)
    state.pop(CREDENTIALLESS_MOUNT_AUTHORITY_KEY, None)
    manifest, redacted = sanitize_raw_manifest_mount_authority(state.get("manifest"))
    if "manifest" in state:
        state["manifest"] = manifest
    if redacted or state.get(REDACTED_MOUNT_AUTHORITY_KEY) is True:
        state[REDACTED_MOUNT_AUTHORITY_KEY] = True
        redacted = True
    return state, redacted


def _run_state_sandbox_envelope_is_valid(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if "session_state" in payload and not isinstance(payload["session_state"], Mapping):
        return False
    sessions_by_agent = payload.get("sessions_by_agent")
    if sessions_by_agent is None:
        return True
    if not isinstance(sessions_by_agent, Mapping):
        return False
    return all(
        isinstance(entry, Mapping)
        and ("session_state" not in entry or isinstance(entry["session_state"], Mapping))
        for entry in sessions_by_agent.values()
    )


def _sanitize_run_state_sandbox_mount_authority(
    payload: object,
    *,
    validation_error_factory: Callable[[str], ValueError] | None = None,
) -> tuple[object, bool]:
    """Sanitize only the documented sandbox resume-state envelope."""

    if not _run_state_sandbox_envelope_is_valid(payload):
        if isinstance(payload, dict | list):
            payload.clear()
        _raise_invalid_run_state_sandbox_envelope(validation_error_factory=validation_error_factory)
    assert isinstance(payload, Mapping)
    sandbox = copy.deepcopy(dict(payload))
    redacted = False

    if "session_state" in sandbox:
        session_state, state_redacted = sanitize_raw_session_state_mount_authority(
            sandbox["session_state"]
        )
        sandbox["session_state"] = session_state
        redacted = redacted or state_redacted

    sessions_by_agent = sandbox.get("sessions_by_agent")
    if isinstance(sessions_by_agent, Mapping):
        sanitized_sessions = copy.deepcopy(dict(sessions_by_agent))
        for key, entry in sanitized_sessions.items():
            assert isinstance(entry, Mapping)
            entry_copy = copy.deepcopy(dict(entry))
            raw_state = entry_copy.get("session_state", entry_copy)
            sanitized_state, entry_redacted = sanitize_raw_session_state_mount_authority(raw_state)
            if "session_state" in entry_copy:
                entry_copy["session_state"] = sanitized_state
                sanitized_sessions[key] = entry_copy
            else:
                sanitized_sessions[key] = sanitized_state
            redacted = redacted or entry_redacted
        sandbox["sessions_by_agent"] = sanitized_sessions
    return sandbox, redacted


def sanitize_run_state_sandbox_mount_authority(
    payload: object,
    *,
    validation_error_factory: Callable[[str], ValueError] | None = None,
) -> tuple[object, bool]:
    safe_error: ValueError | None = None
    try:
        return _sanitize_run_state_sandbox_mount_authority(
            payload,
            validation_error_factory=validation_error_factory,
        )
    except _InvalidRawMountManifestError as error:
        message = "RunState sandbox resume state contains an invalid manifest"
        if validation_error_factory is None:
            safe_error = _redact_mount_state_validation_error(error, message=message)
        else:
            discard_mount_source_exception(error)
            safe_error = validation_error_factory(message)

    if isinstance(payload, dict | list):
        payload.clear()
    payload = None
    assert safe_error is not None
    _raise_data_redacted_error(safe_error)


def _raise_invalid_run_state_sandbox_envelope(
    *,
    validation_error_factory: Callable[[str], ValueError] | None = None,
) -> NoReturn:
    message = "RunState sandbox resume state has an invalid envelope"
    if validation_error_factory is None:
        error = ValueError(message)
        _mark_error_data_redacted(error)
    else:
        error = validation_error_factory(message)
    _raise_data_redacted_error(error)
