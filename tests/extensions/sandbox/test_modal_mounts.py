from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest


def _stub_modal_if_missing() -> None:
    """Make the mount strategy importable without the optional ``modal`` extra.

    ``agents.extensions.sandbox.modal.__init__`` imports the backend, which does
    ``import modal`` at module scope. ``mounts.py`` itself needs nothing from the
    SDK, so in an environment without the extra a stub keeps these tests
    collectable instead of failing the whole file at import time.
    """
    try:
        importlib.import_module("modal")
        return
    except ImportError:
        pass

    modal_module = types.ModuleType("modal")
    config_module = types.ModuleType("modal.config")
    config_module.config = types.SimpleNamespace(get=lambda *args, **kwargs: None)  # type: ignore[attr-defined]
    container_process_module = types.ModuleType("modal.container_process")
    container_process_module.ContainerProcess = type("ContainerProcess", (), {})  # type: ignore[attr-defined]
    modal_module.config = config_module  # type: ignore[attr-defined]
    modal_module.container_process = container_process_module  # type: ignore[attr-defined]

    sys.modules.setdefault("modal", modal_module)
    sys.modules.setdefault("modal.config", config_module)
    sys.modules.setdefault("modal.container_process", container_process_module)


_stub_modal_if_missing()

from agents.extensions.sandbox.modal.mounts import (  # noqa: E402
    ModalCloudBucketMountStrategy,
)
from agents.sandbox.entries import Dir, GCSMount, R2Mount, S3Mount  # noqa: E402
from agents.sandbox.errors import MountConfigError  # noqa: E402


class _FakeModalSession:
    """Modal-shaped session. The strategy only inspects its class name."""


_FakeModalSession.__name__ = "ModalSandboxSession"


def _s3(strategy: ModalCloudBucketMountStrategy, **kwargs: Any) -> S3Mount:
    defaults: dict[str, Any] = {"bucket": "bucket", "mount_strategy": strategy}
    defaults.update(kwargs)
    return S3Mount(**defaults)


# -- config validation -----------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"secret_name": ""}, "secret_name must be a non-empty string"),
        (
            {"secret_environment_name": "", "secret_name": "s"},
            "secret_environment_name must be a non-empty string",
        ),
        (
            {"secret_environment_name": "env"},
            "secret_environment_name requires secret_name",
        ),
    ],
    ids=["empty_secret_name", "empty_secret_env", "env_without_secret"],
)
def test_secret_options_are_validated(kwargs: dict[str, Any], message: str) -> None:
    strategy = ModalCloudBucketMountStrategy(**kwargs)

    with pytest.raises(MountConfigError, match=message):
        _s3(strategy)


@pytest.mark.parametrize(
    "mount_factory",
    [
        lambda s: _s3(s, access_key_id="a", secret_access_key="b"),
        lambda s: R2Mount(
            bucket="bucket",
            account_id="acct",
            access_key_id="a",
            secret_access_key="b",
            mount_strategy=s,
        ),
        lambda s: GCSMount(bucket="bucket", access_id="a", secret_access_key="b", mount_strategy=s),
    ],
    ids=["s3", "r2", "gcs"],
)
def test_inline_credentials_cannot_be_combined_with_a_named_secret(
    mount_factory: Any,
) -> None:
    """Two credential sources would be ambiguous, so the mount must be rejected."""
    strategy = ModalCloudBucketMountStrategy(secret_name="named-secret")

    with pytest.raises(MountConfigError, match="do not support both inline credentials"):
        mount_factory(strategy)


def test_gcs_requires_s3_compatible_credentials_without_a_secret() -> None:
    strategy = ModalCloudBucketMountStrategy()

    with pytest.raises(MountConfigError, match="require access_id and secret_access_key"):
        GCSMount(bucket="bucket", mount_strategy=strategy)


def test_gcs_accepts_a_named_secret_without_inline_credentials() -> None:
    strategy = ModalCloudBucketMountStrategy(secret_name="named-secret")

    mount = GCSMount(bucket="bucket", mount_strategy=strategy)
    config = strategy._build_modal_cloud_bucket_mount_config(mount)

    assert config.credentials is None
    assert config.secret_name == "named-secret"
    assert config.bucket_endpoint_url == "https://storage.googleapis.com"


def test_s3_config_carries_session_token_credentials() -> None:
    strategy = ModalCloudBucketMountStrategy()
    mount = _s3(
        strategy,
        access_key_id="a",
        secret_access_key="b",
        session_token="t",
        prefix="p/",
        read_only=False,
    )

    config = strategy._build_modal_cloud_bucket_mount_config(mount)

    assert config.credentials == {
        "AWS_ACCESS_KEY_ID": "a",
        "AWS_SECRET_ACCESS_KEY": "b",
        "AWS_SESSION_TOKEN": "t",
    }
    assert config.key_prefix == "p/"
    assert config.read_only is False


def test_r2_config_defaults_to_the_account_endpoint() -> None:
    strategy = ModalCloudBucketMountStrategy()
    mount = R2Mount(bucket="bucket", account_id="acct", mount_strategy=strategy)

    config = strategy._build_modal_cloud_bucket_mount_config(mount)

    assert config.bucket_endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert config.credentials is None


def test_unsupported_mount_types_are_rejected() -> None:
    strategy = ModalCloudBucketMountStrategy()

    with pytest.raises(MountConfigError, match="not supported for this mount type"):
        strategy._build_modal_cloud_bucket_mount_config(cast(Any, Dir()))


# -- strategy lifecycle ----------------------------------------------------


def test_native_snapshot_detach_is_declined_so_the_tar_fallback_is_used() -> None:
    """Modal attaches buckets natively, so a native snapshot must not be trusted."""
    strategy = ModalCloudBucketMountStrategy()

    assert strategy.supports_native_snapshot_detach(_s3(strategy)) is False


async def test_activate_is_a_no_op_because_modal_attaches_at_create_time() -> None:
    """Modal attaches the bucket when the sandbox is created, so there is nothing to do."""
    strategy = ModalCloudBucketMountStrategy()
    session = cast(Any, _FakeModalSession())

    assert await strategy.activate(_s3(strategy), session, Path("/w/d"), Path("/tmp")) == []
    # Must accept the session rather than raise; there is no return value to check.
    await strategy.deactivate(_s3(strategy), session, Path("/w/d"), Path("/tmp"))


async def test_snapshot_hooks_are_no_ops() -> None:
    """Detaching is declined above, so the snapshot hooks have nothing to unwind."""
    strategy = ModalCloudBucketMountStrategy()
    session = cast(Any, _FakeModalSession())
    mount = _s3(strategy)

    await strategy.teardown_for_snapshot(mount, session, Path("/w/d"))
    await strategy.restore_after_snapshot(mount, session, Path("/w/d"))


@pytest.mark.parametrize("method", ["activate", "deactivate"])
async def test_activate_and_deactivate_reject_a_foreign_session(method: str) -> None:
    class _WrongSession:
        pass

    _WrongSession.__name__ = "NotAModalSession"
    session = cast(Any, _WrongSession())
    strategy = ModalCloudBucketMountStrategy()
    mount = _s3(strategy)

    with pytest.raises(MountConfigError, match="not supported by this sandbox backend") as info:
        if method == "activate":
            await strategy.activate(mount, session, Path("/w/d"), Path("/tmp"))
        else:
            await strategy.deactivate(mount, session, Path("/w/d"), Path("/tmp"))

    assert info.value.context["session_type"] == "NotAModalSession"


def test_strategy_has_no_docker_volume_driver_config() -> None:
    strategy = ModalCloudBucketMountStrategy()

    assert strategy.build_docker_volume_driver_config(_s3(strategy)) is None
