from __future__ import annotations

import asyncio
import builtins
import importlib
import io
import os
import sys
import tarfile
import types
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any, NoReturn, cast

import pytest
from pydantic import Field, PrivateAttr

from agents.sandbox import Manifest
from agents.sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from agents.sandbox.entries import (
    File,
    GCSMount,
    InContainerMountStrategy,
    Mount,
    MountpointMountPattern,
    R2Mount,
    S3Mount,
)
from agents.sandbox.entries.mounts.base import InContainerMountAdapter
from agents.sandbox.errors import (
    InvalidManifestPathError,
    MountConfigError,
    WorkspaceArchiveReadError,
)
from agents.sandbox.files import EntryKind
from agents.sandbox.manifest import Environment
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.runtime_helpers import (
    RESOLVE_WORKSPACE_PATH_HELPER,
    WORKSPACE_FINGERPRINT_HELPER,
)
from agents.sandbox.snapshot import LocalSnapshot
from agents.sandbox.types import ExecResult


def _with_aio(fn: Callable[..., object]) -> Callable[..., object]:
    def _sync(*args: object, **kwargs: object) -> object:
        return fn(*args, **kwargs)

    async def _aio(*args: object, **kwargs: object) -> object:
        return fn(*args, **kwargs)

    _sync.aio = _aio  # type: ignore[attr-defined]
    return _sync


def _set_aio_attr(obj: object, name: str, fn: Callable[..., object]) -> None:
    setattr(obj, name, _with_aio(fn))


class _RecordingMount(Mount):
    type: str = "modal_recording_mount"
    mount_strategy: InContainerMountStrategy = Field(
        default_factory=lambda: InContainerMountStrategy(pattern=MountpointMountPattern())
    )
    _events: list[tuple[str, str]] = PrivateAttr(default_factory=list)
    _teardown_error: str | None = PrivateAttr(default=None)

    def bind_events(self, events: list[tuple[str, str]]) -> _RecordingMount:
        self._events = events
        return self

    def bind_teardown_error(self, message: str) -> _RecordingMount:
        self._teardown_error = message
        return self

    def supported_in_container_patterns(
        self,
    ) -> tuple[builtins.type[MountpointMountPattern], ...]:
        return (MountpointMountPattern,)

    def build_docker_volume_driver_config(
        self,
        strategy: object,
    ) -> tuple[str, dict[str, str], bool]:
        _ = strategy
        raise MountConfigError(
            message="docker-volume mounts are not supported for this mount type",
            context={"mount_type": self.type},
        )

    def in_container_adapter(self) -> InContainerMountAdapter:
        mount = self

        class _Adapter(InContainerMountAdapter):
            def validate(self, strategy: InContainerMountStrategy) -> None:
                _ = strategy

            async def activate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> list[MaterializedFile]:
                _ = (strategy, session, dest, base_dir)
                return []

            async def deactivate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> None:
                _ = (strategy, session, dest, base_dir)

            async def teardown_for_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = (strategy, session)
                if mount._teardown_error is not None:
                    raise RuntimeError(mount._teardown_error)
                mount._events.append(("unmount", path.as_posix()))

            async def restore_after_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = (strategy, session)
                mount._events.append(("mount", path.as_posix()))

        return _Adapter(self)


def _load_modal_module(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[dict[str, object]], list[str]]:
    create_calls: list[dict[str, object]] = []
    registry_tags: list[str] = []

    class _FakeImage:
        object_id = "im-123"
        from_id_calls: list[str] = []

        def __init__(self, object_id: str | None = None) -> None:
            if object_id is not None:
                self.object_id = object_id
            self.cmd_calls: list[list[str]] = []

        @staticmethod
        def from_registry(_tag: str) -> _FakeImage:
            registry_tags.append(_tag)
            return _FakeImage()

        @staticmethod
        def from_id(_image_id: str) -> _FakeImage:
            _FakeImage.from_id_calls.append(_image_id)
            return _FakeImage(object_id=_image_id)

        def cmd(self, command: list[str]) -> _FakeImage:
            self.cmd_calls.append(command)
            return self

    class _FakeSandboxInstance:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.terminate_kwargs: list[dict[str, object]] = []
            self.mount_image_calls: list[tuple[str, str | None]] = []
            self.terminate = _with_aio(self._terminate)
            self.poll = _with_aio(self._poll)
            self.tunnels = _with_aio(self._tunnels)
            self.exec = _with_aio(self._exec)
            self.snapshot_directory = _with_aio(self._snapshot_directory)
            self.mount_image = _with_aio(self._mount_image)

        def _terminate(self, **kwargs: object) -> None:
            self.terminate_calls += 1
            self.terminate_kwargs.append(kwargs)

        def _poll(self) -> None:
            return None

        def _tunnels(self, timeout: int = 50) -> dict[int, object]:
            _ = timeout
            return {
                8765: types.SimpleNamespace(
                    host="sandbox.example.test",
                    port=443,
                    unencrypted_host="",
                    unencrypted_port=0,
                )
            }

        def _snapshot_directory(self, _path: str) -> _FakeImage:
            return _FakeImage()

        def _mount_image(self, path: str, image: object) -> None:
            self.mount_image_calls.append((path, getattr(image, "object_id", None)))

        def _exec(self, *command: object, **kwargs: object) -> object:
            _ = (command, kwargs)
            resolve_helper_path = str(RESOLVE_WORKSPACE_PATH_HELPER.install_path)
            fingerprint_helper_path = str(WORKSPACE_FINGERPRINT_HELPER.install_path)

            class _FakeStream:
                def __init__(self, payload: bytes = b"") -> None:
                    self.read = _with_aio(lambda: payload)

            stdout = b""
            if (
                command[:2] == ("sh", "-c")
                and isinstance(command[2], str)
                and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in command[2]
            ):
                return types.SimpleNamespace(
                    stdout=_FakeStream(),
                    stderr=_FakeStream(),
                    wait=_with_aio(lambda: 0),
                )
            if command and command[0] == resolve_helper_path:
                stdout = str(command[2]).encode("utf-8")
            if command and command[0] == fingerprint_helper_path:
                stdout = (
                    b'{"fingerprint":"fake-workspace-fingerprint",'
                    b'"version":"workspace_tar_sha256_v1"}\n'
                )
            if command == ("test", "-d", "/workspace"):
                return types.SimpleNamespace(
                    stdout=_FakeStream(),
                    stderr=_FakeStream(),
                    wait=_with_aio(lambda: 1),
                )

            return types.SimpleNamespace(
                stdout=_FakeStream(stdout),
                stderr=_FakeStream(),
                wait=_with_aio(lambda: 0),
            )

    class _FakeSandbox:
        from_id_calls: list[str] = []
        create: Any
        from_id: Any

        @staticmethod
        def _create(**kwargs: object) -> _FakeSandboxInstance:
            create_calls.append(
                dict(
                    kwargs,
                    modal_image_builder_version_env=os.environ.get("MODAL_IMAGE_BUILDER_VERSION"),
                )
            )
            return _FakeSandboxInstance()

        @staticmethod
        def _from_id(_sandbox_id: str) -> _FakeSandboxInstance:
            _FakeSandbox.from_id_calls.append(_sandbox_id)
            return _FakeSandboxInstance()

    class _FakeApp:
        lookup: Any

        @staticmethod
        def _lookup(_name: str, *, create_if_missing: bool = False) -> object:
            _ = create_if_missing
            return object()

    class _FakeSecret:
        def __init__(
            self,
            value: dict[str, str] | None = None,
            *,
            name: str | None = None,
            environment_name: str | None = None,
        ) -> None:
            self.value = value
            self.name = name
            self.environment_name = environment_name

        @staticmethod
        def from_dict(value: dict[str, str]) -> _FakeSecret:
            return _FakeSecret(value)

        @staticmethod
        def from_name(name: str, *, environment_name: str | None = None) -> _FakeSecret:
            return _FakeSecret(name=name, environment_name=environment_name)

    class _FakeCloudBucketMount:
        def __init__(
            self,
            *,
            bucket_name: str,
            bucket_endpoint_url: str | None = None,
            key_prefix: str | None = None,
            secret: _FakeSecret | None = None,
            read_only: bool = True,
        ) -> None:
            self.bucket_name = bucket_name
            self.bucket_endpoint_url = bucket_endpoint_url
            self.key_prefix = key_prefix
            self.secret = secret
            self.read_only = read_only

    class _FakeConfig:
        override_calls: list[tuple[str, str]] = []

        @staticmethod
        def override_locally(key: str, value: str) -> None:
            _FakeConfig.override_calls.append((key, value))
            os.environ["MODAL_" + key.upper()] = value

    _FakeSandbox.create = staticmethod(_with_aio(_FakeSandbox._create))
    _FakeSandbox.from_id = staticmethod(_with_aio(_FakeSandbox._from_id))
    _FakeApp.lookup = staticmethod(_with_aio(_FakeApp._lookup))

    fake_modal: Any = types.ModuleType("modal")
    fake_modal.Image = _FakeImage
    fake_modal.App = _FakeApp
    fake_modal.Sandbox = _FakeSandbox
    fake_modal.Secret = _FakeSecret
    fake_modal.CloudBucketMount = _FakeCloudBucketMount

    fake_modal_config: Any = types.ModuleType("modal.config")
    fake_modal_config.config = _FakeConfig

    fake_container_process: Any = types.ModuleType("modal.container_process")
    fake_container_process.ContainerProcess = object

    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    monkeypatch.setitem(sys.modules, "modal.config", fake_modal_config)
    monkeypatch.setitem(sys.modules, "modal.container_process", fake_container_process)
    sys.modules.pop("agents.extensions.sandbox.modal.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.modal.mounts", None)
    sys.modules.pop("agents.extensions.sandbox.modal", None)

    module: Any = importlib.import_module("agents.extensions.sandbox.modal.sandbox")
    return module, create_calls, registry_tags


def test_modal_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.modal")

    assert package_module.ModalSandboxClient is modal_module.ModalSandboxClient
    assert (
        package_module.ModalCloudBucketMountStrategy is modal_module.ModalCloudBucketMountStrategy
    )


@pytest.mark.asyncio
async def test_modal_sandbox_create_passes_manifest_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    await client.create(
        manifest=Manifest(environment=Environment(value={"SANDBOX_FLAG": "enabled"})),
        options=modal_module.ModalSandboxClientOptions(app_name="sandbox-tests"),
    )

    assert create_calls
    assert create_calls[0]["env"] == {"SANDBOX_FLAG": "enabled"}
    assert create_calls[0]["modal_image_builder_version_env"] == "2025.06"
    assert registry_tags == [DEFAULT_PYTHON_SANDBOX_IMAGE]
    image = cast(Any, create_calls[0]["image"])
    assert image.cmd_calls == [["sleep", "infinity"]]
    assert os.environ.get("MODAL_IMAGE_BUILDER_VERSION") is None


@pytest.mark.asyncio
async def test_modal_sandbox_create_passes_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    session = await client.create(
        options=modal_module.ModalSandboxClientOptions(
            app_name="sandbox-tests",
            idle_timeout=60,
        ),
    )

    assert create_calls
    assert create_calls[0]["idle_timeout"] == 60
    assert session.state.idle_timeout == 60


@pytest.mark.asyncio
async def test_modal_sandbox_create_sets_default_cmd_for_custom_registry_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient(
        image=modal_module.ModalImageSelector.from_tag("debian:bookworm-slim")
    )
    await client.create(
        options=modal_module.ModalSandboxClientOptions(app_name="sandbox-tests"),
    )

    assert create_calls
    assert registry_tags == ["debian:bookworm-slim"]
    image = cast(Any, create_calls[0]["image"])
    assert image.cmd_calls == [["sleep", "infinity"]]


@pytest.mark.asyncio
async def test_modal_sandbox_create_can_opt_out_of_default_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    await client.create(
        options=modal_module.ModalSandboxClientOptions(
            app_name="sandbox-tests",
            use_sleep_cmd=False,
        ),
    )

    assert create_calls
    assert registry_tags == [DEFAULT_PYTHON_SANDBOX_IMAGE]
    image = cast(Any, create_calls[0]["image"])
    assert image.cmd_calls == []


@pytest.mark.asyncio
async def test_modal_sandbox_create_uses_custom_image_builder_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    session = await client.create(
        options=modal_module.ModalSandboxClientOptions(
            app_name="sandbox-tests",
            image_builder_version="PREVIEW",
        ),
    )

    assert create_calls
    assert create_calls[0]["modal_image_builder_version_env"] == "PREVIEW"
    assert session.state.image_builder_version == "PREVIEW"
    assert os.environ.get("MODAL_IMAGE_BUILDER_VERSION") is None


@pytest.mark.asyncio
async def test_modal_sandbox_create_uses_existing_config_when_image_builder_version_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)
    monkeypatch.setenv("MODAL_IMAGE_BUILDER_VERSION", "USER-CONFIGURED")

    client = modal_module.ModalSandboxClient()
    session = await client.create(
        options=modal_module.ModalSandboxClientOptions(
            app_name="sandbox-tests",
            image_builder_version=None,
        ),
    )

    assert create_calls
    assert create_calls[0]["modal_image_builder_version_env"] == "USER-CONFIGURED"
    assert session.state.image_builder_version is None
    assert os.environ.get("MODAL_IMAGE_BUILDER_VERSION") == "USER-CONFIGURED"


def test_modal_deserialize_session_state_defaults_missing_image_builder_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        image_builder_version="PREVIEW",
    )
    payload = state.model_dump(mode="json")
    payload.pop("image_builder_version")

    restored = modal_module.ModalSandboxClient().deserialize_session_state(
        cast(dict[str, object], payload)
    )

    assert restored.image_builder_version == "2025.06"


def test_modal_deserialize_session_state_defaults_missing_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        idle_timeout=60,
    )
    payload = state.model_dump(mode="json")
    payload.pop("idle_timeout")

    restored = modal_module.ModalSandboxClient().deserialize_session_state(
        cast(dict[str, object], payload)
    )

    assert restored.idle_timeout is None


@pytest.mark.asyncio
async def test_modal_sandbox_create_passes_modal_cloud_bucket_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    await client.create(
        manifest=Manifest(
            entries={
                "remote": S3Mount(
                    bucket="bucket",
                    access_key_id="access-key",
                    secret_access_key="secret-key",
                    prefix="nested/prefix/",
                    mount_strategy=modal_module.ModalCloudBucketMountStrategy(),
                    read_only=False,
                )
            }
        ),
        options=modal_module.ModalSandboxClientOptions(app_name="sandbox-tests"),
    )

    assert create_calls
    volumes = create_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    assert volumes.keys() == {"/workspace/remote"}
    mount = volumes["/workspace/remote"]
    assert mount.bucket_name == "bucket"
    assert mount.bucket_endpoint_url is None
    assert mount.key_prefix == "nested/prefix/"
    assert mount.secret.value == {
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-key",
    }
    assert mount.read_only is False


@pytest.mark.asyncio
async def test_modal_sandbox_create_passes_named_modal_secret_for_cloud_bucket_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    await client.create(
        manifest=Manifest(
            entries={
                "remote": S3Mount(
                    bucket="bucket",
                    prefix="nested/prefix/",
                    mount_strategy=modal_module.ModalCloudBucketMountStrategy(
                        secret_name="named-modal-secret"
                    ),
                    read_only=False,
                )
            }
        ),
        options=modal_module.ModalSandboxClientOptions(app_name="sandbox-tests"),
    )

    assert create_calls
    volumes = create_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    assert volumes.keys() == {"/workspace/remote"}
    mount = volumes["/workspace/remote"]
    assert mount.bucket_name == "bucket"
    assert mount.bucket_endpoint_url is None
    assert mount.key_prefix == "nested/prefix/"
    assert mount.secret.name == "named-modal-secret"
    assert mount.secret.value is None
    assert mount.read_only is False


@pytest.mark.asyncio
async def test_modal_sandbox_create_passes_named_modal_secret_environment_for_cloud_bucket_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    await client.create(
        manifest=Manifest(
            entries={
                "remote": S3Mount(
                    bucket="bucket",
                    prefix="nested/prefix/",
                    mount_strategy=modal_module.ModalCloudBucketMountStrategy(
                        secret_name="named-modal-secret",
                        secret_environment_name="staging",
                    ),
                    read_only=False,
                )
            }
        ),
        options=modal_module.ModalSandboxClientOptions(app_name="sandbox-tests"),
    )

    assert create_calls
    volumes = create_calls[0]["volumes"]
    assert isinstance(volumes, dict)
    mount = volumes["/workspace/remote"]
    assert mount.secret.name == "named-modal-secret"
    assert mount.secret.environment_name == "staging"
    assert mount.secret.value is None


def test_modal_cloud_bucket_mount_strategy_round_trips_through_manifest_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    manifest = Manifest.model_validate(
        {
            "entries": {
                "remote": {
                    "type": "s3_mount",
                    "bucket": "bucket",
                    "mount_strategy": {"type": "modal_cloud_bucket"},
                }
            }
        }
    )

    mount = manifest.entries["remote"]

    assert isinstance(mount, S3Mount)
    assert isinstance(mount.mount_strategy, modal_module.ModalCloudBucketMountStrategy)


def test_modal_cloud_bucket_mount_strategy_round_trips_secret_name_through_manifest_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    manifest = Manifest.model_validate(
        {
            "entries": {
                "remote": {
                    "type": "s3_mount",
                    "bucket": "bucket",
                    "mount_strategy": {
                        "type": "modal_cloud_bucket",
                        "secret_name": "named-modal-secret",
                    },
                }
            }
        }
    )

    mount = manifest.entries["remote"]

    assert isinstance(mount, S3Mount)
    assert isinstance(mount.mount_strategy, modal_module.ModalCloudBucketMountStrategy)
    assert mount.mount_strategy.secret_name == "named-modal-secret"


def test_modal_cloud_bucket_mount_strategy_round_trips_secret_env_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    manifest = Manifest.model_validate(
        {
            "entries": {
                "remote": {
                    "type": "s3_mount",
                    "bucket": "bucket",
                    "mount_strategy": {
                        "type": "modal_cloud_bucket",
                        "secret_name": "named-modal-secret",
                        "secret_environment_name": "staging",
                    },
                }
            }
        }
    )

    mount = manifest.entries["remote"]

    assert isinstance(mount, S3Mount)
    assert isinstance(mount.mount_strategy, modal_module.ModalCloudBucketMountStrategy)
    assert mount.mount_strategy.secret_name == "named-modal-secret"
    assert mount.mount_strategy.secret_environment_name == "staging"


def test_modal_cloud_bucket_mount_strategy_builds_s3_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    strategy = modal_module.ModalCloudBucketMountStrategy()
    mount = S3Mount(
        bucket="bucket",
        access_key_id="access-key",
        secret_access_key="secret-key",
        session_token="session-token",
        prefix="nested/prefix/",
        endpoint_url="https://s3.example.test",
        mount_strategy=strategy,
        read_only=False,
    )

    config = strategy._build_modal_cloud_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.bucket_endpoint_url == "https://s3.example.test"
    assert config.key_prefix == "nested/prefix/"
    assert config.credentials == {
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-key",
        "AWS_SESSION_TOKEN": "session-token",
    }
    assert config.read_only is False


def test_modal_cloud_bucket_mount_strategy_builds_s3_config_with_named_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    strategy = modal_module.ModalCloudBucketMountStrategy(secret_name="named-modal-secret")
    mount = S3Mount(
        bucket="bucket",
        prefix="nested/prefix/",
        mount_strategy=strategy,
        read_only=False,
    )

    config = strategy._build_modal_cloud_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.bucket_endpoint_url is None
    assert config.key_prefix == "nested/prefix/"
    assert config.credentials is None
    assert config.secret_name == "named-modal-secret"
    assert config.secret_environment_name is None
    assert config.read_only is False


def test_modal_cloud_bucket_mount_strategy_builds_s3_config_with_named_secret_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    strategy = modal_module.ModalCloudBucketMountStrategy(
        secret_name="named-modal-secret",
        secret_environment_name="staging",
    )
    mount = S3Mount(
        bucket="bucket",
        prefix="nested/prefix/",
        mount_strategy=strategy,
        read_only=False,
    )

    config = strategy._build_modal_cloud_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.credentials is None
    assert config.secret_name == "named-modal-secret"
    assert config.secret_environment_name == "staging"
    assert config.read_only is False


def test_modal_cloud_bucket_mount_strategy_builds_r2_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    strategy = modal_module.ModalCloudBucketMountStrategy()
    mount = R2Mount(
        bucket="bucket",
        account_id="abc123accountid",
        access_key_id="access-key",
        secret_access_key="secret-key",
        mount_strategy=strategy,
    )

    config = strategy._build_modal_cloud_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.bucket_endpoint_url == "https://abc123accountid.r2.cloudflarestorage.com"
    assert config.key_prefix is None
    assert config.credentials == {
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-key",
    }
    assert config.read_only is True


def test_modal_cloud_bucket_mount_strategy_builds_gcs_hmac_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    strategy = modal_module.ModalCloudBucketMountStrategy()
    mount = GCSMount(
        bucket="bucket",
        access_id="access-id",
        secret_access_key="secret-key",
        prefix="nested/prefix/",
        mount_strategy=strategy,
        read_only=False,
    )

    config = strategy._build_modal_cloud_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.bucket_endpoint_url == "https://storage.googleapis.com"
    assert config.key_prefix == "nested/prefix/"
    assert config.credentials == {
        "GOOGLE_ACCESS_KEY_ID": "access-id",
        "GOOGLE_ACCESS_KEY_SECRET": "secret-key",
    }
    assert config.read_only is False


def test_modal_cloud_bucket_mount_strategy_builds_gcs_hmac_config_with_named_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    strategy = modal_module.ModalCloudBucketMountStrategy(secret_name="named-modal-secret")
    mount = GCSMount(
        bucket="bucket",
        prefix="nested/prefix/",
        mount_strategy=strategy,
        read_only=False,
    )

    config = strategy._build_modal_cloud_bucket_mount_config(mount)  # noqa: SLF001

    assert config.bucket_name == "bucket"
    assert config.bucket_endpoint_url == "https://storage.googleapis.com"
    assert config.key_prefix == "nested/prefix/"
    assert config.credentials is None
    assert config.secret_name == "named-modal-secret"
    assert config.secret_environment_name is None
    assert config.read_only is False


def test_modal_cloud_bucket_mount_strategy_rejects_secret_environment_name_without_secret_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    strategy = modal_module.ModalCloudBucketMountStrategy(secret_environment_name="staging")

    with pytest.raises(
        MountConfigError,
        match="secret_environment_name requires secret_name to also be set",
    ):
        strategy._build_modal_cloud_bucket_mount_config(  # noqa: SLF001
            S3Mount(bucket="bucket", mount_strategy=strategy)
        )


def test_modal_cloud_bucket_mount_strategy_rejects_mixed_inline_credentials_and_secret_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    strategy = modal_module.ModalCloudBucketMountStrategy(secret_name="named-modal-secret")

    with pytest.raises(
        MountConfigError,
        match="do not support both inline credentials and secret_name",
    ):
        strategy._build_modal_cloud_bucket_mount_config(  # noqa: SLF001
            S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key="secret-key",
                mount_strategy=strategy,
            )
        )


def test_modal_cloud_bucket_mount_strategy_rejects_gcs_native_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    with pytest.raises(
        MountConfigError,
        match="gcs modal cloud bucket mounts require access_id and secret_access_key",
    ):
        GCSMount(
            bucket="bucket",
            service_account_file="/data/config/gcs.json",
            mount_strategy=modal_module.ModalCloudBucketMountStrategy(),
        )


def _load_modal_runner_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    _load_modal_module(monkeypatch)
    monkeypatch.delitem(sys.modules, "agents.extensions.sandbox", raising=False)
    monkeypatch.delitem(sys.modules, "examples.sandbox.extensions.modal_runner", raising=False)
    return importlib.import_module("examples.sandbox.extensions.modal_runner")


def test_modal_runner_builds_s3_native_bucket_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_modal_runner_module(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-key")

    manifest = runner._build_manifest(native_cloud_bucket_name="bucket")  # noqa: SLF001

    mount = manifest.entries["cloud-bucket"]
    assert isinstance(mount, S3Mount)
    assert mount.bucket == "bucket"
    assert mount.access_key_id == "access-key"
    assert mount.secret_access_key == "secret-key"


def test_modal_runner_builds_s3_native_bucket_with_named_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_modal_runner_module(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-key")

    manifest = runner._build_manifest(  # noqa: SLF001
        native_cloud_bucket_name="bucket",
        native_cloud_bucket_secret_name="named-modal-secret",
    )

    mount = manifest.entries["cloud-bucket"]
    assert isinstance(mount, S3Mount)
    assert mount.bucket == "bucket"
    assert mount.access_key_id is None
    assert mount.secret_access_key is None
    assert mount.session_token is None
    strategy = mount.mount_strategy
    assert isinstance(strategy, runner.ModalCloudBucketMountStrategy)
    assert strategy.secret_name == "named-modal-secret"
    assert strategy.secret_environment_name is None


def test_modal_runner_builds_gcs_hmac_native_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_modal_runner_module(monkeypatch)
    monkeypatch.setenv("GCS_HMAC_ACCESS_KEY_ID", "access-id")
    monkeypatch.setenv("GCS_HMAC_SECRET_ACCESS_KEY", "secret-key")

    manifest = runner._build_manifest(  # noqa: SLF001
        native_cloud_bucket_name="bucket",
        native_cloud_bucket_provider="gcs-hmac",
        native_cloud_bucket_mount_path="mounted",
        native_cloud_bucket_key_prefix="nested/prefix/",
    )

    mount = manifest.entries["cloud-bucket"]
    assert isinstance(mount, GCSMount)
    assert mount.bucket == "bucket"
    assert mount.access_id == "access-id"
    assert mount.secret_access_key == "secret-key"
    assert mount.mount_path == Path("mounted")
    assert mount.prefix == "nested/prefix/"
    assert runner._native_cloud_bucket_mount_path(manifest) == Path("/workspace/mounted")


def test_modal_runner_builds_gcs_hmac_native_bucket_with_named_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_modal_runner_module(monkeypatch)
    monkeypatch.setenv("GCS_HMAC_ACCESS_KEY_ID", "access-id")
    monkeypatch.setenv("GCS_HMAC_SECRET_ACCESS_KEY", "secret-key")

    manifest = runner._build_manifest(  # noqa: SLF001
        native_cloud_bucket_name="bucket",
        native_cloud_bucket_provider="gcs-hmac",
        native_cloud_bucket_secret_name="named-modal-secret",
    )

    mount = manifest.entries["cloud-bucket"]
    assert isinstance(mount, GCSMount)
    assert mount.bucket == "bucket"
    assert mount.access_id is None
    assert mount.secret_access_key is None
    strategy = mount.mount_strategy
    assert isinstance(strategy, runner.ModalCloudBucketMountStrategy)
    assert strategy.secret_name == "named-modal-secret"
    assert strategy.secret_environment_name is None


@pytest.mark.asyncio
async def test_modal_start_ensures_sandbox_before_running_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    session = await client.create(
        options=modal_module.ModalSandboxClientOptions(app_name="sandbox-tests"),
    )

    assert session._inner._sandbox is not None  # noqa: SLF001
    assert len(create_calls) == 1

    await session.start()

    assert session._inner._sandbox is not None  # noqa: SLF001
    assert len(create_calls) == 1


@pytest.mark.asyncio
async def test_modal_sandbox_create_exposes_declared_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    await client.create(
        options=modal_module.ModalSandboxClientOptions(
            app_name="sandbox-tests",
            exposed_ports=(8765,),
        ),
    )

    assert create_calls
    assert create_calls[0]["encrypted_ports"] == (8765,)


@pytest.mark.asyncio
async def test_modal_resume_eagerly_reconnects_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id="sb-existing",
    )

    client = modal_module.ModalSandboxClient()
    session = await client.resume(state)

    assert session._inner._sandbox is not None  # noqa: SLF001
    assert create_calls == []
    assert sys.modules["modal"].Sandbox.from_id_calls == ["sb-existing"]


@pytest.mark.asyncio
async def test_modal_resume_marks_reconnected_sandbox_preserved_before_snapshot_reuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)
    snapshot = LocalSnapshot(id="modal-snapshot", base_path=tmp_path)
    await snapshot.persist(
        io.BytesIO(modal_module._encode_snapshot_filesystem_ref(snapshot_id="snap-123"))
    )
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=snapshot,
        app_name="sandbox-tests",
        sandbox_id="sb-existing",
        workspace_persistence="snapshot_filesystem",
        snapshot_fingerprint="fake-workspace-fingerprint",
        snapshot_fingerprint_version="workspace_tar_sha256_v1",
        workspace_root_ready=True,
    )

    client = modal_module.ModalSandboxClient()
    session = await client.resume(state)

    assert session._inner._running is True  # noqa: SLF001
    assert session._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
    assert session._inner._system_state_preserved_on_start() is True  # noqa: SLF001

    await session.start()

    assert create_calls == []
    assert sys.modules["modal"].Sandbox.from_id_calls == ["sb-existing"]
    assert sys.modules["modal"].Image.from_id_calls == []


@pytest.mark.asyncio
async def test_modal_resume_restores_snapshot_when_workspace_readiness_unproven(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)
    snapshot = LocalSnapshot(id="modal-snapshot", base_path=tmp_path)
    await snapshot.persist(
        io.BytesIO(modal_module._encode_snapshot_filesystem_ref(snapshot_id="snap-123"))
    )
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=snapshot,
        app_name="sandbox-tests",
        sandbox_id="sb-existing",
        workspace_persistence="snapshot_filesystem",
        snapshot_fingerprint="fake-workspace-fingerprint",
        snapshot_fingerprint_version="workspace_tar_sha256_v1",
    )

    client = modal_module.ModalSandboxClient()
    session = await client.resume(state)

    assert session._inner._running is True  # noqa: SLF001
    assert session._inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
    assert session._inner._can_reuse_preserved_workspace_on_resume() is False  # noqa: SLF001

    await session.start()

    assert len(create_calls) == 1
    assert create_calls[0]["workdir"] == "/workspace"
    assert sys.modules["modal"].Sandbox.from_id_calls == ["sb-existing"]
    assert sys.modules["modal"].Image.from_id_calls == ["snap-123"]


@pytest.mark.asyncio
async def test_modal_resume_restores_directory_snapshot_when_workspace_readiness_unproven(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)
    snapshot = LocalSnapshot(id="modal-snapshot", base_path=tmp_path)
    await snapshot.persist(
        io.BytesIO(modal_module._encode_snapshot_directory_ref(snapshot_id="snap-dir-123"))
    )
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=snapshot,
        app_name="sandbox-tests",
        sandbox_id="sb-existing",
        workspace_persistence="snapshot_directory",
        snapshot_fingerprint="fake-workspace-fingerprint",
        snapshot_fingerprint_version="workspace_tar_sha256_v1",
    )

    client = modal_module.ModalSandboxClient()
    session = await client.resume(state)
    inner = session._inner  # noqa: SLF001

    assert inner._running is True  # noqa: SLF001
    assert inner._workspace_state_preserved_on_start() is True  # noqa: SLF001
    assert inner._can_reuse_preserved_workspace_on_resume() is False  # noqa: SLF001

    await session.start()

    assert create_calls == []
    assert sys.modules["modal"].Sandbox.from_id_calls == ["sb-existing"]
    assert sys.modules["modal"].Image.from_id_calls == ["snap-dir-123"]
    assert inner._sandbox is not None  # noqa: SLF001
    assert inner._sandbox.mount_image_calls == [("/workspace", "snap-dir-123")]  # noqa: SLF001


@pytest.mark.asyncio
async def test_modal_resume_resets_workspace_readiness_when_sandbox_is_recreated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _StoppedSandboxInstance:
        object_id = "sb-stopped"

        def __init__(self) -> None:
            self.poll = _with_aio(lambda: 1)

    def _from_stopped_id(_sandbox_id: str) -> object:
        sys.modules["modal"].Sandbox.from_id_calls.append(_sandbox_id)
        return _StoppedSandboxInstance()

    sys.modules["modal"].Sandbox.from_id = staticmethod(_with_aio(_from_stopped_id))
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id="sb-stopped",
        workspace_root_ready=True,
        image_builder_version="PREVIEW",
    )

    client = modal_module.ModalSandboxClient()
    session = await client.resume(state)

    assert session._inner._workspace_state_preserved_on_start() is False  # noqa: SLF001
    assert state.workspace_root_ready is False
    assert create_calls
    assert create_calls[0]["modal_image_builder_version_env"] == "PREVIEW"
    assert state.sandbox_id == "sb-123"
    assert os.environ.get("MODAL_IMAGE_BUILDER_VERSION") is None


@pytest.mark.asyncio
async def test_modal_resume_bounds_reconnect_and_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_create_timeout_s=12.5,
        sandbox_id="sb-existing",
    )

    session = modal_module.ModalSandboxSession.from_state(state)
    call_timeouts: list[float | None] = []

    real_call_modal = session._call_modal  # noqa: SLF001

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        call_timeouts.append(call_timeout)
        return await real_call_modal(fn, *args, call_timeout=call_timeout, **kwargs)

    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    await session._ensure_sandbox()  # noqa: SLF001

    assert session._sandbox is not None  # noqa: SLF001
    assert create_calls == []
    assert call_timeouts == [12.5, modal_module._DEFAULT_TIMEOUT_S]  # noqa: SLF001


@pytest.mark.asyncio
async def test_modal_ensure_sandbox_bounds_app_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
    )

    session = modal_module.ModalSandboxSession.from_state(state)
    call_timeouts: list[float | None] = []

    real_call_modal = session._call_modal  # noqa: SLF001

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        call_timeouts.append(call_timeout)
        return await real_call_modal(fn, *args, call_timeout=call_timeout, **kwargs)

    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    await session._ensure_sandbox()  # noqa: SLF001

    assert session._sandbox is not None  # noqa: SLF001
    assert len(create_calls) == 1
    assert call_timeouts == [10.0, modal_module._DEFAULT_TIMEOUT_S]  # noqa: SLF001


@pytest.mark.asyncio
async def test_modal_ensure_sandbox_bounds_image_id_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        image_id="im-existing",
    )

    session = modal_module.ModalSandboxSession.from_state(state)
    call_names: list[str] = []
    call_timeouts: list[float | None] = []

    real_call_modal = session._call_modal  # noqa: SLF001

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        call_names.append(getattr(fn, "__name__", ""))
        call_timeouts.append(call_timeout)
        return await real_call_modal(fn, *args, call_timeout=call_timeout, **kwargs)

    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    await session._ensure_sandbox()  # noqa: SLF001

    assert session._sandbox is not None  # noqa: SLF001
    assert len(create_calls) == 1
    assert sys.modules["modal"].Image.from_id_calls == ["im-existing"]
    assert call_names == ["_sync"]
    assert call_timeouts == [10.0]


@pytest.mark.asyncio
async def test_modal_resolve_exposed_port_reads_tunnel_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    sandbox = sys.modules["modal"].Sandbox.create()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        exposed_ports=(8765,),
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)

    endpoint = await session.resolve_exposed_port(8765)

    assert endpoint.host == "sandbox.example.test"
    assert endpoint.port == 443
    assert endpoint.tls is True


@pytest.mark.asyncio
async def test_modal_stop_is_persistence_only_and_shutdown_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    sandbox = sys.modules["modal"].Sandbox.create()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    session._running = True
    call_timeouts: list[float | None] = []

    real_call_modal = session._call_modal  # noqa: SLF001

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        call_timeouts.append(call_timeout)
        return await real_call_modal(fn, *args, call_timeout=call_timeout, **kwargs)

    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    await session.stop()

    assert sandbox.terminate_calls == 0
    assert session.state.sandbox_id == "sb-123"
    assert await session.running() is True

    await session.shutdown()

    assert sandbox.terminate_calls == 1
    assert sandbox.terminate_kwargs == [{}]
    assert session.state.sandbox_id is None
    assert await session.running() is False
    assert call_timeouts == [modal_module._DEFAULT_TIMEOUT_S]  # noqa: SLF001


@pytest.mark.asyncio
async def test_modal_shutdown_rehydrates_sandbox_and_terminates_without_wait_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    sandbox = sys.modules["modal"].Sandbox.create()

    def _from_id(_sandbox_id: str) -> object:
        sys.modules["modal"].Sandbox.from_id_calls.append(_sandbox_id)
        return sandbox

    sys.modules["modal"].Sandbox.from_id = staticmethod(_with_aio(_from_id))
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id="sb-existing",
    )
    session = modal_module.ModalSandboxSession.from_state(state)
    call_timeouts: list[float | None] = []

    real_call_modal = session._call_modal  # noqa: SLF001

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        call_timeouts.append(call_timeout)
        return await real_call_modal(fn, *args, call_timeout=call_timeout, **kwargs)

    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    await session.shutdown()

    assert sys.modules["modal"].Sandbox.from_id_calls == ["sb-existing"]
    assert sandbox.terminate_kwargs == [{}]
    assert session.state.sandbox_id is None
    assert await session.running() is False
    assert call_timeouts == [
        modal_module._DEFAULT_TIMEOUT_S,
        modal_module._DEFAULT_TIMEOUT_S,
    ]  # noqa: SLF001


@pytest.mark.asyncio
async def test_modal_tar_persist_respects_runtime_skip_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id="sb-123",
    )
    session = modal_module.ModalSandboxSession.from_state(state)
    session.register_persist_workspace_skip_path(Path("logs/events.jsonl"))

    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        return ExecResult(stdout=b"fake-tar-bytes", stderr=b"", exit_code=0)

    monkeypatch.setattr(session, "exec", _fake_exec)

    archive = await session.persist_workspace()

    assert archive.read() == b"fake-tar-bytes"
    assert commands == [
        [
            "tar",
            "cf",
            "-",
            "--exclude",
            "./logs/events.jsonl",
            "-C",
            "/workspace",
            ".",
        ]
    ]


@pytest.mark.asyncio
async def test_modal_snapshot_failure_restores_ephemeral_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeRestoreProcess:
        def __init__(self, owner: Any) -> None:
            self._owner = owner
            self.stderr = types.SimpleNamespace(read=_with_aio(lambda: b""))
            self.stdin = self._FakeStdin(owner)
            _set_aio_attr(self.stdin, "drain", self.stdin.drain)
            self.wait = _with_aio(self._wait)

        class _FakeStdin:
            def __init__(self, owner: Any) -> None:
                self._owner = owner
                self._buffer = bytearray()

            def write(self, data: bytes) -> None:
                self._buffer.extend(data)

            def write_eof(self) -> None:
                return

            def drain(self) -> None:
                return

        def _wait(self) -> int:
            self._owner.restore_payloads.append(bytes(self.stdin._buffer))
            return 0

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.restore_payloads: list[bytes] = []
            self.snapshot_filesystem = _with_aio(self._snapshot_filesystem)
            self.exec = _with_aio(self._exec)

        def _snapshot_filesystem(self) -> str:
            raise RuntimeError("snapshot failed")

        def _exec(self, *command: object, **kwargs: object) -> _FakeRestoreProcess:
            _ = kwargs
            assert command[:3] == ("tar", "xf", "-")
            return _FakeRestoreProcess(self)

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={"tmp.txt": File(content=b"ephemeral", ephemeral=True)},
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        if rendered[:2] == ["sh", "-lc"]:
            return ExecResult(stdout=b"ephemeral-backup", stderr=b"", exit_code=0)
        if rendered[:3] == ["rm", "-rf", "--"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = call_timeout
        return fn(*args, **kwargs)

    monkeypatch.setattr(session, "exec", _fake_exec)
    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert exc_info.value.context["reason"] == "snapshot_filesystem_failed"
    assert sandbox.restore_payloads == [b"ephemeral-backup"]


@pytest.mark.asyncio
async def test_modal_snapshot_cleanup_failure_raises_before_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeRestoreProcess:
        def __init__(self, owner: Any) -> None:
            self._owner = owner
            self.stderr = types.SimpleNamespace(read=_with_aio(lambda: b""))
            self.stdin = self._FakeStdin(owner)
            _set_aio_attr(self.stdin, "drain", self.stdin.drain)
            self.wait = _with_aio(self._wait)

        class _FakeStdin:
            def __init__(self, owner: Any) -> None:
                self._owner = owner
                self._buffer = bytearray()

            def write(self, data: bytes) -> None:
                self._buffer.extend(data)

            def write_eof(self) -> None:
                return

            def drain(self) -> None:
                return

        def _wait(self) -> int:
            self._owner.restore_payloads.append(bytes(self.stdin._buffer))
            return 0

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.restore_payloads: list[bytes] = []
            self.snapshot_calls = 0
            self.snapshot_filesystem = _with_aio(self._snapshot_filesystem)
            self.exec = _with_aio(self._exec)

        def _snapshot_filesystem(self) -> str:
            self.snapshot_calls += 1
            return "snap-123"

        def _exec(self, *command: object, **kwargs: object) -> _FakeRestoreProcess:
            _ = kwargs
            assert command[:3] == ("tar", "xf", "-")
            return _FakeRestoreProcess(self)

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={"tmp.txt": File(content=b"ephemeral", ephemeral=True)},
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        if rendered[:2] == ["sh", "-lc"]:
            return ExecResult(stdout=b"ephemeral-backup", stderr=b"", exit_code=0)
        if rendered[:3] == ["rm", "-rf", "--"]:
            return ExecResult(stdout=b"", stderr=b"rm failed", exit_code=1)
        raise AssertionError(f"unexpected command: {rendered!r}")

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = call_timeout
        return fn(*args, **kwargs)

    monkeypatch.setattr(session, "exec", _fake_exec)
    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert exc_info.value.context["reason"] == "snapshot_filesystem_ephemeral_remove_failed"
    assert exc_info.value.context["exit_code"] == 1
    assert exc_info.value.context["stderr"] == "rm failed"
    assert sandbox.snapshot_calls == 0
    assert sandbox.restore_payloads == [b"ephemeral-backup"]


@pytest.mark.asyncio
async def test_modal_normalize_path_preserves_safe_leaf_symlink_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
    )
    session = modal_module.ModalSandboxSession.from_state(state)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        if (
            rendered[:2] == ["sh", "-c"]
            and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in rendered[2]
        ):
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered and rendered[0] == str(RESOLVE_WORKSPACE_PATH_HELPER.install_path):
            return ExecResult(stdout=b"/workspace/target.txt", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    normalized = await session._validate_path_access("link.txt")  # noqa: SLF001

    assert normalized.as_posix() == "/workspace/link.txt"


@pytest.mark.asyncio
async def test_modal_normalize_path_uses_posix_commands_for_windows_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
    )
    session = modal_module.ModalSandboxSession.from_state(state)
    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if (
            rendered[:2] == ["sh", "-c"]
            and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in rendered[2]
        ):
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered and rendered[0] == str(RESOLVE_WORKSPACE_PATH_HELPER.install_path):
            return ExecResult(stdout=b"/workspace/link.txt", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    normalized = await session._validate_path_access(PureWindowsPath("/workspace/link.txt"))  # noqa: SLF001

    helper_path = str(RESOLVE_WORKSPACE_PATH_HELPER.install_path)
    assert normalized.as_posix() == "/workspace/link.txt"
    assert commands[-1] == [helper_path, "/workspace", "/workspace/link.txt", "0"]
    assert all("\\" not in arg for arg in commands[-1])


@pytest.mark.asyncio
async def test_modal_normalize_path_rejects_windows_drive_absolute_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
    )
    session = modal_module.ModalSandboxSession.from_state(state)

    async def _fake_exec(*args: object, **kwargs: object) -> ExecResult:
        _ = (args, kwargs)
        raise AssertionError("path validation should reject before remote helper execution")

    monkeypatch.setattr(session, "exec", _fake_exec)

    with pytest.raises(InvalidManifestPathError) as exc_info:
        await session._validate_path_access(PureWindowsPath("C:/tmp/link.txt"))  # noqa: SLF001

    assert str(exc_info.value) == "manifest path must be relative: C:/tmp/link.txt"
    assert exc_info.value.context == {"rel": "C:/tmp/link.txt", "reason": "absolute"}


@pytest.mark.asyncio
async def test_modal_normalize_path_rejects_symlink_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
    )
    session = modal_module.ModalSandboxSession.from_state(state)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        if (
            rendered[:2] == ["sh", "-c"]
            and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in rendered[2]
        ):
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered and rendered[0] == str(RESOLVE_WORKSPACE_PATH_HELPER.install_path):
            return ExecResult(stdout=b"", stderr=b"workspace escape", exit_code=111)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session._validate_path_access("link/secret.txt")  # noqa: SLF001


@pytest.mark.asyncio
async def test_modal_normalize_path_reinstalls_helper_after_runtime_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id="sb-old",
    )
    session = modal_module.ModalSandboxSession.from_state(state)
    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if (
            rendered[:2] == ["sh", "-c"]
            and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in rendered[2]
        ):
            if state.sandbox_id is None:
                state.sandbox_id = "sb-new"
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered == ["test", "-x", str(RESOLVE_WORKSPACE_PATH_HELPER.install_path)]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered and rendered[0] == str(RESOLVE_WORKSPACE_PATH_HELPER.install_path):
            return ExecResult(stdout=b"/workspace/target.txt", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    assert (await session._validate_path_access("link.txt")).as_posix() == "/workspace/link.txt"
    first_run_commands = list(commands)
    commands.clear()

    state.sandbox_id = None
    assert (await session._validate_path_access("link.txt")).as_posix() == "/workspace/link.txt"
    second_run_commands = list(commands)
    commands.clear()

    assert (await session._validate_path_access("link.txt")).as_posix() == "/workspace/link.txt"

    helper_path = str(RESOLVE_WORKSPACE_PATH_HELPER.install_path)
    assert any(
        cmd[:2] == ["sh", "-c"] and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in cmd[2]
        for cmd in first_run_commands
    )
    assert any(
        cmd[:2] == ["sh", "-c"] and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in cmd[2]
        for cmd in second_run_commands
    )
    assert any(cmd and cmd[0] == helper_path for cmd in second_run_commands)
    assert commands == [
        ["test", "-x", helper_path],
        [helper_path, "/workspace", "/workspace/link.txt", "0"],
    ]


@pytest.mark.asyncio
async def test_modal_snapshot_filesystem_uses_resolved_mount_paths_for_backup_and_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeRestoreProcess:
        def __init__(self) -> None:
            self.stderr = types.SimpleNamespace(read=_with_aio(lambda: b""))
            self.stdin = self._FakeStdin()
            _set_aio_attr(self.stdin, "drain", self.stdin.drain)
            self.wait = _with_aio(self._wait)

        class _FakeStdin:
            def write(self, data: bytes) -> None:
                _ = data

            def write_eof(self) -> None:
                return

            def drain(self) -> None:
                return

        def _wait(self) -> int:
            return 0

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.snapshot_filesystem = _with_aio(self._snapshot_filesystem)
            self.exec = _with_aio(self._exec)

        def _snapshot_filesystem(self) -> str:
            return "snap-123"

        def _exec(self, *command: object, **kwargs: object) -> _FakeRestoreProcess:
            _ = kwargs
            assert command[:3] == ("tar", "xf", "-")
            return _FakeRestoreProcess()

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "logical": _RecordingMount(
                    mount_path=Path("actual"),
                    ephemeral=False,
                ),
                "logs/events.jsonl": File(content=b"skip", ephemeral=True),
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    def _snapshot_filesystem() -> str:
        return "snap-123"

    sandbox.snapshot_filesystem = _with_aio(_snapshot_filesystem)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered[:2] == ["sh", "-lc"]:
            return ExecResult(stdout=b"ephemeral-backup", stderr=b"", exit_code=0)
        if rendered[:3] == ["rm", "-rf", "--"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    archive = await session.persist_workspace()

    assert archive.read() == modal_module._encode_snapshot_filesystem_ref(snapshot_id="snap-123")
    assert commands[0][0:2] == ["sh", "-lc"]
    assert "logs/events.jsonl" in commands[0][2]
    assert "actual" not in commands[0][2]
    assert "logical" not in commands[0][2]
    assert commands[1] == ["rm", "-rf", "--", "/workspace/logs/events.jsonl"]


@pytest.mark.asyncio
async def test_modal_snapshot_directory_uses_resolved_mount_paths_for_backup_and_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeRestoreProcess:
        def __init__(self) -> None:
            self.stderr = types.SimpleNamespace(read=_with_aio(lambda: b""))
            self.stdin = self._FakeStdin()
            _set_aio_attr(self.stdin, "drain", self.stdin.drain)
            self.wait = _with_aio(self._wait)

        class _FakeStdin:
            def write(self, data: bytes) -> None:
                _ = data

            def write_eof(self) -> None:
                return

            def drain(self) -> None:
                return

        def _wait(self) -> int:
            return 0

    class _FakeSnapshotSandbox:
        object_id = "sb-123"
        snapshot_directory: Any

        def __init__(self) -> None:
            self.exec = _with_aio(self._exec)

        def _exec(self, *command: object, **kwargs: object) -> _FakeRestoreProcess:
            _ = kwargs
            assert command[:3] == ("tar", "xf", "-")
            return _FakeRestoreProcess()

    sandbox = _FakeSnapshotSandbox()
    mount = _RecordingMount()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "logical": mount,
                "logs/events.jsonl": File(content=b"skip", ephemeral=True),
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_directory",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    def _snapshot_directory(path: str) -> str:
        assert path == "/workspace"
        return "snap-dir-123"

    sandbox.snapshot_directory = _with_aio(_snapshot_directory)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered[:2] == ["sh", "-lc"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered[:3] == ["rm", "-rf", "--"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    archive = await session.persist_workspace()

    assert archive.read() == modal_module._encode_snapshot_directory_ref(snapshot_id="snap-dir-123")
    assert commands[0][0:2] == ["sh", "-lc"]
    assert "logs/events.jsonl" in commands[0][2]
    assert "logical" not in commands[0][2]
    assert "/tmp/openai-agents/session-state/" in commands[0][2]
    assert "modal-snapshot-directory-ephemeral.tar" in commands[0][2]
    assert "for rel in logs/events.jsonl;" in commands[0][2]
    assert "tar cf" in commands[0][2]
    assert "-T -" in commands[0][2]
    assert commands[1] == ["rm", "-rf", "--", "/workspace/logs/events.jsonl"]
    assert commands[2][0:2] == ["sh", "-lc"]
    assert "modal-snapshot-directory-ephemeral.tar" in commands[2][2]
    assert "tar xf" in commands[2][2]


@pytest.mark.asyncio
async def test_modal_snapshot_directory_backup_failure_aborts_before_removing_ephemeral_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeSnapshotSandbox:
        object_id = "sb-123"
        snapshot_directory: Any

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "tmp.txt": File(content=b"skip", ephemeral=True),
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_directory",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    def _snapshot_directory(_path: str) -> str:
        raise AssertionError("snapshot_directory should not run after backup failure")

    sandbox.snapshot_directory = _with_aio(_snapshot_directory)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered[:2] == ["sh", "-lc"]:
            return ExecResult(stdout=b"", stderr=b"mkdir failed", exit_code=1)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert exc_info.value.context["reason"] == "snapshot_directory_ephemeral_backup_failed"
    assert exc_info.value.context["exit_code"] == 1
    assert exc_info.value.context["stderr"] == "mkdir failed"
    assert commands == [
        [
            "sh",
            "-lc",
            "mkdir -p -- /tmp/openai-agents/session-state/"
            f"{session.state.session_id.hex} && "
            "cd -- /workspace && "
            '{ for rel in tmp.txt; do if [ -e "$rel" ]; '
            "then printf '%s\\n' \"$rel\"; fi; done; } | tar cf "
            f"/tmp/openai-agents/session-state/{session.state.session_id.hex}/"
            "modal-snapshot-directory-ephemeral.tar -T - 2>/dev/null && test -f "
            f"/tmp/openai-agents/session-state/{session.state.session_id.hex}/"
            "modal-snapshot-directory-ephemeral.tar",
        ]
    ]


@pytest.mark.asyncio
async def test_modal_snapshot_directory_teardown_failure_restores_partial_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)
    events: list[tuple[str, str]] = []

    class _FakeSnapshotSandbox:
        object_id = "sb-123"
        snapshot_directory: Any

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "tmp.txt": File(content=b"skip", ephemeral=True),
                "first": _RecordingMount(
                    mount_path=Path("actual-1"),
                    ephemeral=False,
                ).bind_events(events),
                "second": _RecordingMount(
                    mount_path=Path("actual-2"),
                    ephemeral=False,
                )
                .bind_events(events)
                .bind_teardown_error("teardown failed"),
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_directory",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    def _snapshot_directory(_path: str) -> str:
        raise AssertionError("snapshot_directory should not run after teardown failure")

    sandbox.snapshot_directory = _with_aio(_snapshot_directory)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered[:2] == ["sh", "-lc"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered[:3] == ["rm", "-rf", "--"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert isinstance(exc_info.value.cause, RuntimeError)
    assert str(exc_info.value.cause) == "teardown failed"
    assert events == [("unmount", "/workspace/actual-1"), ("mount", "/workspace/actual-1")]
    assert commands[0][0:2] == ["sh", "-lc"]
    assert "for rel in tmp.txt;" in commands[0][2]
    assert commands[1] == ["rm", "-rf", "--", "/workspace/tmp.txt"]
    assert commands[2][0:2] == ["sh", "-lc"]
    assert "modal-snapshot-directory-ephemeral.tar" in commands[2][2]
    assert "tar xf" in commands[2][2]


@pytest.mark.asyncio
async def test_modal_snapshot_directory_tolerates_missing_ephemeral_paths_in_backup_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeSnapshotSandbox:
        object_id = "sb-123"
        snapshot_directory: Any

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "tmp.txt": File(content=b"skip", ephemeral=True),
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_directory",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    def _snapshot_directory(path: str) -> str:
        assert path == "/workspace"
        return "snap-dir-123"

    sandbox.snapshot_directory = _with_aio(_snapshot_directory)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered[:2] == ["sh", "-lc"]:
            if "for rel in tmp.txt;" in rendered[2]:
                assert "-T -" in rendered[2]
            else:
                assert "tar xf" in rendered[2]
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered[:3] == ["rm", "-rf", "--"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    monkeypatch.setattr(session, "exec", _fake_exec)

    archive = await session.persist_workspace()

    assert archive.read() == modal_module._encode_snapshot_directory_ref(snapshot_id="snap-dir-123")
    assert commands[1] == ["rm", "-rf", "--", "/workspace/tmp.txt"]


@pytest.mark.asyncio
async def test_modal_snapshot_unexpected_return_restores_live_session_before_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeRestoreProcess:
        def __init__(self, owner: Any) -> None:
            self._owner = owner
            self.stderr = types.SimpleNamespace(read=_with_aio(lambda: b""))
            self.stdin = self._FakeStdin(owner)
            _set_aio_attr(self.stdin, "drain", self.stdin.drain)
            self.wait = _with_aio(self._wait)

        class _FakeStdin:
            def __init__(self, owner: Any) -> None:
                self._owner = owner
                self._buffer = bytearray()

            def write(self, data: bytes) -> None:
                self._buffer.extend(data)

            def write_eof(self) -> None:
                return

            def drain(self) -> None:
                return

        def _wait(self) -> int:
            self._owner.restore_payloads.append(bytes(self.stdin._buffer))
            return 0

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.restore_payloads: list[bytes] = []
            self.snapshot_filesystem = _with_aio(self._snapshot_filesystem)
            self.exec = _with_aio(self._exec)

        def _snapshot_filesystem(self) -> object:
            return object()

        def _exec(self, *command: object, **kwargs: object) -> _FakeRestoreProcess:
            _ = kwargs
            assert command == ("tar", "xf", "-", "-C", "/workspace")
            return _FakeRestoreProcess(self)

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "logical": _RecordingMount(
                    mount_path=Path("actual"),
                    ephemeral=False,
                ),
                "tmp.txt": File(content=b"ephemeral", ephemeral=True),
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []
    events: list[tuple[str, str]] = []

    def _snapshot_filesystem() -> object:
        events.append(("snapshot", ""))
        return object()

    sandbox.snapshot_filesystem = _with_aio(_snapshot_filesystem)

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered == [
            "sh",
            "-lc",
            "cd -- /workspace && (tar cf - -- tmp.txt 2>/dev/null || true)",
        ]:
            return ExecResult(stdout=b"ephemeral-backup", stderr=b"", exit_code=0)
        if rendered == ["rm", "-rf", "--", "/workspace/tmp.txt"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = call_timeout
        if getattr(fn, "__name__", "") == "snapshot_filesystem":
            events.append(("snapshot", ""))
        return fn(*args, **kwargs)

    monkeypatch.setattr(session, "exec", _fake_exec)
    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert exc_info.value.context == {
        "path": "/workspace",
        "reason": "snapshot_filesystem_unexpected_return",
        "type": "object",
    }
    assert sandbox.restore_payloads == [b"ephemeral-backup"]
    assert commands == [
        ["sh", "-lc", "cd -- /workspace && (tar cf - -- tmp.txt 2>/dev/null || true)"],
        ["rm", "-rf", "--", "/workspace/tmp.txt"],
    ]
    assert events == [("snapshot", "")]


@pytest.mark.asyncio
async def test_modal_snapshot_unexpected_return_skips_restore_for_empty_ephemeral_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.snapshot_filesystem = _with_aio(self._snapshot_filesystem)
            self.exec = _with_aio(self._exec)

        def _snapshot_filesystem(self) -> object:
            return object()

        def _exec(self, *command: object, **kwargs: object) -> NoReturn:
            _ = kwargs
            raise AssertionError(f"restore should be skipped for empty backup: {command!r}")

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={"tmp.txt": File(content=b"ephemeral", ephemeral=True)},
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        if rendered == [
            "sh",
            "-lc",
            "cd -- /workspace && (tar cf - -- tmp.txt 2>/dev/null || true)",
        ]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if rendered == ["rm", "-rf", "--", "/workspace/tmp.txt"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"unexpected command: {rendered!r}")

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = call_timeout
        return fn(*args, **kwargs)

    monkeypatch.setattr(session, "exec", _fake_exec)
    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    with pytest.raises(WorkspaceArchiveReadError) as exc_info:
        await session.persist_workspace()

    assert exc_info.value.context == {
        "path": "/workspace",
        "reason": "snapshot_filesystem_unexpected_return",
        "type": "object",
    }
    assert commands == [
        ["sh", "-lc", "cd -- /workspace && (tar cf - -- tmp.txt 2>/dev/null || true)"],
        ["rm", "-rf", "--", "/workspace/tmp.txt"],
    ]


@pytest.mark.asyncio
async def test_modal_tar_persist_uses_resolved_mount_paths_for_excludes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "logical": GCSMount(
                    bucket="bucket",
                    mount_path=Path("actual"),
                    ephemeral=False,
                    mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                )
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=None)
    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        rendered = [str(part) for part in command]
        commands.append(rendered)
        return ExecResult(stdout=b"tar-bytes", stderr=b"", exit_code=0)

    monkeypatch.setattr(session, "exec", _fake_exec)

    archive = await session.persist_workspace()

    assert archive.read() == b"tar-bytes"
    assert commands == [
        [
            "tar",
            "cf",
            "-",
            "--exclude",
            "./actual",
            "-C",
            "/workspace",
            ".",
        ]
    ]


@pytest.mark.asyncio
async def test_modal_snapshot_filesystem_rejects_escaping_mount_paths_before_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.snapshot_calls = 0

        def snapshot_filesystem(self) -> str:
            self.snapshot_calls += 1
            return "snap-123"

    sandbox = _FakeSnapshotSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "logical": GCSMount(
                    bucket="bucket",
                    mount_path=Path("/workspace/../../tmp"),
                    mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                )
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
        workspace_persistence="snapshot_filesystem",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    commands: list[list[str]] = []

    async def _fake_exec(
        *command: object,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: object | None = None,
    ) -> ExecResult:
        _ = (timeout, shell, user)
        commands.append([str(part) for part in command])
        raise AssertionError("exec() should not run for escaping mount paths")

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = (fn, args, call_timeout, kwargs)
        raise AssertionError("snapshot_filesystem() should not run for escaping mount paths")

    monkeypatch.setattr(session, "exec", _fake_exec)
    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.persist_workspace()

    assert commands == []
    assert sandbox.snapshot_calls == 0


@pytest.mark.asyncio
async def test_modal_write_chunks_large_payload_before_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeWaitResult:
        def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"") -> None:
            self.stdout = types.SimpleNamespace(read=_with_aio(lambda: stdout))
            self.stderr = types.SimpleNamespace(read=_with_aio(lambda: stderr))
            self.wait = _with_aio(self._wait)

        def _wait(self) -> int:
            return 0

    class _FakeStdin:
        def __init__(self, *, limit: int) -> None:
            self._limit = limit
            self._buffer = bytearray()
            self.chunks: list[bytes] = []
            self.write_eof_calls = 0
            self.drain_calls = 0

        def write(self, data: bytes | bytearray | memoryview) -> None:
            rendered = bytes(data)
            if len(self._buffer) + len(rendered) > self._limit:
                raise BufferError("Buffer size exceed limit. Call drain to flush the buffer.")
            self._buffer.extend(rendered)

        def write_eof(self) -> None:
            self.write_eof_calls += 1

        def drain(self) -> None:
            self.chunks.append(bytes(self._buffer))
            self._buffer.clear()
            self.drain_calls += 1

    class _FakeProcess:
        def __init__(self, *, limit: int) -> None:
            self.stdin = _FakeStdin(limit=limit)
            _set_aio_attr(self.stdin, "drain", self.stdin.drain)
            self.stderr = types.SimpleNamespace(read=_with_aio(lambda: b""))
            self.wait = _with_aio(self._wait)

        def _wait(self) -> int:
            return 0

    class _FakeSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.processes: list[_FakeProcess] = []
            self.commands: list[tuple[object, ...]] = []
            self.exec = _with_aio(self._exec)

        def _exec(self, *command: object, **kwargs: object) -> object:
            _ = kwargs
            self.commands.append(command)
            helper_path = str(RESOLVE_WORKSPACE_PATH_HELPER.install_path)
            if command[:3] == ("mkdir", "-p", "--"):
                return _FakeWaitResult()
            if (
                command[:2] == ("sh", "-c")
                and isinstance(command[2], str)
                and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in command[2]
            ):
                return _FakeWaitResult()
            if command == ("test", "-x", helper_path):
                return _FakeWaitResult()
            if command and command[0] == helper_path:
                return _FakeWaitResult(stdout=b"/workspace/nested/file.bin")
            process = _FakeProcess(limit=5)
            self.processes.append(process)
            return process

    sandbox = _FakeSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    monkeypatch.setattr(modal_module, "_MODAL_STDIN_CHUNK_SIZE", 5)

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = call_timeout
        return fn(*args, **kwargs)

    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    payload = b"abcdefghijklm"
    await session.write(Path("nested/file.bin"), io.BytesIO(payload))

    assert sandbox.commands[-2:] == [
        ("mkdir", "-p", "--", "/workspace/nested"),
        ("sh", "-lc", "cat > /workspace/nested/file.bin"),
    ]
    assert len(sandbox.processes) == 1
    assert sandbox.processes[0].stdin.chunks == [b"abcde", b"fghij", b"klm", b""]
    assert sandbox.processes[0].stdin.write_eof_calls == 1
    assert sandbox.processes[0].stdin.drain_calls == 4


@pytest.mark.asyncio
async def test_modal_hydrate_tar_chunks_large_payload_before_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeWaitResult:
        def __init__(self) -> None:
            self.wait = _with_aio(self._wait)

        def _wait(self) -> int:
            return 0

    class _FakeStdin:
        def __init__(self, *, limit: int) -> None:
            self._limit = limit
            self._buffer = bytearray()
            self.chunks: list[bytes] = []
            self.write_eof_calls = 0
            self.drain_calls = 0

        def write(self, data: bytes | bytearray | memoryview) -> None:
            rendered = bytes(data)
            if len(self._buffer) + len(rendered) > self._limit:
                raise BufferError("Buffer size exceed limit. Call drain to flush the buffer.")
            self._buffer.extend(rendered)

        def write_eof(self) -> None:
            self.write_eof_calls += 1

        def drain(self) -> None:
            self.chunks.append(bytes(self._buffer))
            self._buffer.clear()
            self.drain_calls += 1

    class _FakeProcess:
        def __init__(self, *, limit: int) -> None:
            self.stdin = _FakeStdin(limit=limit)
            _set_aio_attr(self.stdin, "drain", self.stdin.drain)
            self.stderr = types.SimpleNamespace(read=_with_aio(lambda: b""))
            self.wait = _with_aio(self._wait)

        def _wait(self) -> int:
            return 0

    class _FakeSandbox:
        object_id = "sb-123"

        def __init__(self) -> None:
            self.processes: list[_FakeProcess] = []
            self.commands: list[tuple[object, ...]] = []
            self.exec = _with_aio(self._exec)

        def _exec(self, *command: object, **kwargs: object) -> object:
            _ = kwargs
            self.commands.append(command)
            if command[:3] == ("mkdir", "-p", "--"):
                return _FakeWaitResult()
            process = _FakeProcess(limit=7)
            self.processes.append(process)
            return process

    sandbox = _FakeSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)
    monkeypatch.setattr(modal_module, "_MODAL_STDIN_CHUNK_SIZE", 7)

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        _ = call_timeout
        return fn(*args, **kwargs)

    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    tar_payload = io.BytesIO()
    with tarfile.open(fileobj=tar_payload, mode="w") as tar:
        info = tarfile.TarInfo(name="large.txt")
        contents = b"abcdefghijklmno"
        info.size = len(contents)
        tar.addfile(info, io.BytesIO(contents))
    tar_payload.seek(0)

    await session.hydrate_workspace(tar_payload)

    assert sandbox.commands == [
        ("mkdir", "-p", "--", "/workspace"),
        ("tar", "xf", "-", "-C", "/workspace"),
    ]
    assert len(sandbox.processes) == 1
    assert b"".join(sandbox.processes[0].stdin.chunks[:-1]) == tar_payload.getvalue()
    assert sandbox.processes[0].stdin.write_eof_calls == 1
    assert sandbox.processes[0].stdin.drain_calls >= 2


@pytest.mark.asyncio
async def test_modal_snapshot_filesystem_restore_preserves_exposed_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        workspace_persistence="snapshot_filesystem",
        exposed_ports=(8765,),
        idle_timeout=60,
    )
    session = modal_module.ModalSandboxSession.from_state(state)
    call_names: list[str] = []
    call_timeouts: list[float | None] = []

    real_call_modal = session._call_modal  # noqa: SLF001

    async def _fake_call_modal(
        fn: Callable[..., object],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> object:
        call_names.append(getattr(fn, "__name__", ""))
        call_timeouts.append(call_timeout)
        return await real_call_modal(fn, *args, call_timeout=call_timeout, **kwargs)

    monkeypatch.setattr(session, "_call_modal", _fake_call_modal)

    await session.hydrate_workspace(
        io.BytesIO(modal_module._encode_snapshot_filesystem_ref(snapshot_id="snap-123"))
    )

    assert create_calls
    assert create_calls[0]["encrypted_ports"] == (8765,)
    assert create_calls[0]["idle_timeout"] == 60
    assert sys.modules["modal"].Image.from_id_calls == ["snap-123"]
    assert call_names == []
    assert call_timeouts == []


@pytest.mark.asyncio
async def test_modal_snapshot_directory_restore_preserves_exposed_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        workspace_persistence="snapshot_directory",
        exposed_ports=(8765,),
    )
    session = modal_module.ModalSandboxSession.from_state(state)

    await session.hydrate_workspace(
        io.BytesIO(modal_module._encode_snapshot_directory_ref(snapshot_id="snap-dir-123"))
    )

    assert create_calls
    assert create_calls[0]["encrypted_ports"] == (8765,)
    assert session._sandbox is not None  # noqa: SLF001
    assert session._sandbox.mount_image_calls == [("/workspace", "snap-dir-123")]  # noqa: SLF001
    assert sys.modules["modal"].Image.from_id_calls == ["snap-dir-123"]


@pytest.mark.asyncio
async def test_modal_snapshot_directory_restore_reactivates_durable_workspace_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)
    events: list[tuple[str, str]] = []

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "remote": _RecordingMount(
                    mount_path=Path("actual"),
                    ephemeral=False,
                ).bind_events(events)
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        workspace_persistence="snapshot_directory",
        exposed_ports=(8765,),
    )
    session = modal_module.ModalSandboxSession.from_state(state)

    await session.hydrate_workspace(
        io.BytesIO(modal_module._encode_snapshot_directory_ref(snapshot_id="snap-dir-123"))
    )

    assert create_calls
    assert session._sandbox is not None  # noqa: SLF001
    assert session._sandbox.mount_image_calls == [("/workspace", "snap-dir-123")]  # noqa: SLF001
    assert events == [("mount", "/workspace/actual")]


@pytest.mark.asyncio
async def test_modal_snapshot_directory_persist_only_detaches_durable_workspace_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)
    events: list[tuple[str, str]] = []

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            root="/workspace",
            entries={
                "inside": _RecordingMount(
                    mount_path=Path("actual"),
                    ephemeral=False,
                ).bind_events(events),
                "outside": _RecordingMount(
                    mount_path=Path("/mnt/remote"),
                    ephemeral=False,
                ).bind_events(events),
            },
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        workspace_persistence="snapshot_directory",
        exposed_ports=(8765,),
    )
    session = modal_module.ModalSandboxSession.from_state(state)

    archive = await session.persist_workspace()

    assert create_calls
    assert session._sandbox is not None  # noqa: SLF001
    assert archive.read() == modal_module._encode_snapshot_directory_ref(snapshot_id="im-123")
    assert events == [("unmount", "/workspace/actual"), ("mount", "/workspace/actual")]


@pytest.mark.asyncio
async def test_modal_create_allows_snapshot_filesystem_with_modal_cloud_bucket_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    await client.create(
        manifest=Manifest(
            entries={
                "remote": S3Mount(
                    bucket="bucket",
                    mount_strategy=modal_module.ModalCloudBucketMountStrategy(),
                )
            }
        ),
        options=modal_module.ModalSandboxClientOptions(
            app_name="sandbox-tests",
            workspace_persistence="snapshot_filesystem",
        ),
    )

    assert create_calls
    volumes = cast(dict[str, object], create_calls[0]["volumes"])
    assert volumes.keys() == {"/workspace/remote"}


@pytest.mark.asyncio
async def test_modal_snapshot_filesystem_falls_back_to_tar_for_non_detachable_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeSnapshotSandbox:
        object_id = "sb-123"

        def snapshot_filesystem(self) -> str:
            raise AssertionError("snapshot_filesystem() should not run for non-detachable mounts")

    session = modal_module.ModalSandboxSession.from_state(
        modal_module.ModalSandboxSessionState(
            manifest=Manifest(
                root="/workspace",
                entries={
                    "remote": S3Mount(
                        bucket="bucket",
                        mount_strategy=modal_module.ModalCloudBucketMountStrategy(),
                    )
                },
            ),
            snapshot=modal_module.resolve_snapshot(None, "snapshot"),
            app_name="sandbox-tests",
            sandbox_id="sb-123",
            workspace_persistence="snapshot_filesystem",
        ),
        sandbox=_FakeSnapshotSandbox(),
    )

    async def _fake_tar_persist() -> io.BytesIO:
        return io.BytesIO(b"tar-fallback")

    monkeypatch.setattr(session, "_persist_workspace_via_tar", _fake_tar_persist)

    archive = await session.persist_workspace()

    assert archive.read() == b"tar-fallback"


@pytest.mark.asyncio
async def test_modal_create_rejects_snapshot_directory_with_cloud_bucket_mount_under_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    with pytest.raises(
        MountConfigError,
        match=(
            "snapshot_directory is not supported when a Modal cloud bucket mount "
            "lives at or under the workspace root"
        ),
    ):
        await client.create(
            manifest=Manifest(
                entries={
                    "remote": S3Mount(
                        bucket="bucket",
                        mount_strategy=modal_module.ModalCloudBucketMountStrategy(),
                    )
                }
            ),
            options=modal_module.ModalSandboxClientOptions(
                app_name="sandbox-tests",
                workspace_persistence="snapshot_directory",
            ),
        )

    assert create_calls == []


@pytest.mark.asyncio
async def test_modal_create_allows_snapshot_directory_with_cloud_bucket_mount_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, create_calls, _registry_tags = _load_modal_module(monkeypatch)

    client = modal_module.ModalSandboxClient()
    await client.create(
        manifest=Manifest(
            entries={
                "remote": S3Mount(
                    bucket="bucket",
                    mount_path=Path("/mnt/remote"),
                    mount_strategy=modal_module.ModalCloudBucketMountStrategy(),
                )
            }
        ),
        options=modal_module.ModalSandboxClientOptions(
            app_name="sandbox-tests",
            workspace_persistence="snapshot_directory",
        ),
    )

    assert create_calls
    volumes = cast(dict[str, object], create_calls[0]["volumes"])
    assert volumes.keys() == {"/mnt/remote"}


@pytest.mark.asyncio
async def test_modal_clear_workspace_root_on_resume_preserves_nested_cloud_bucket_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(
            entries={
                "a/b": S3Mount(
                    bucket="bucket",
                    mount_strategy=modal_module.ModalCloudBucketMountStrategy(),
                ),
            }
        ),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
    )
    session = modal_module.ModalSandboxSession.from_state(state)
    ls_calls: list[Path] = []
    rm_calls: list[tuple[Path, bool]] = []

    async def _fake_ls(path: Path | str) -> list[object]:
        rendered = Path(path)
        ls_calls.append(rendered)
        if rendered == Path("/workspace"):
            return [
                types.SimpleNamespace(path="/workspace/a", kind=EntryKind.DIRECTORY),
                types.SimpleNamespace(path="/workspace/root.txt", kind=EntryKind.FILE),
            ]
        if rendered == Path("/workspace/a"):
            return [
                types.SimpleNamespace(path="/workspace/a/b", kind=EntryKind.DIRECTORY),
                types.SimpleNamespace(path="/workspace/a/local.txt", kind=EntryKind.FILE),
            ]
        raise AssertionError(f"unexpected ls path: {rendered}")

    async def _fake_rm(path: Path | str, *, recursive: bool = False) -> None:
        rm_calls.append((Path(path), recursive))

    monkeypatch.setattr(session, "ls", _fake_ls)
    monkeypatch.setattr(session, "rm", _fake_rm)

    await session._clear_workspace_root_on_resume()  # noqa: SLF001

    assert ls_calls == [Path("/workspace"), Path("/workspace/a")]
    assert rm_calls == [
        (Path("/workspace/a/local.txt"), True),
        (Path("/workspace/root.txt"), True),
    ]


@pytest.mark.asyncio
async def test_modal_pty_start_and_write_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self._chunk_event = asyncio.Event()
            if self._chunks:
                self._chunk_event.set()
            self.read = _with_aio(self._read)

        def __aiter__(self) -> _FakeStream:
            return self

        async def __anext__(self) -> bytes:
            while not self._chunks:
                self._chunk_event.clear()
                await self._chunk_event.wait()
            chunk = self._chunks.pop(0)
            if not self._chunks:
                self._chunk_event.clear()
            return chunk

        def append(self, chunk: bytes) -> None:
            self._chunks.append(chunk)
            self._chunk_event.set()

        def _read(self, size: int | None = None) -> bytes:
            if size is None:
                raise AssertionError("PTY polling should not call read() with no size")
            if self._chunks:
                return self._chunks.pop(0)
            return b""

    class _FakeStdin:
        def __init__(self, stdout: _FakeStream) -> None:
            self.writes: list[bytes] = []
            self._stdout = stdout
            self.write = _with_aio(self._write)
            self.drain = _with_aio(lambda: None)

        def _write(self, payload: bytes) -> None:
            self.writes.append(payload)
            if payload == b"5 + 5\n":
                self._stdout.append(b"10\n")

    class _FakeProcess:
        def __init__(self) -> None:
            self.stdout = _FakeStream([b">>> "])
            self.stderr = _FakeStream([])
            self.stdin = _FakeStdin(self.stdout)
            self.poll = _with_aio(lambda: None)
            self.terminate = _with_aio(lambda: None)

    class _FakeSandbox:
        object_id = "sb-pty"

        def __init__(self) -> None:
            self.process = _FakeProcess()
            self.exec_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
            self.exec = _with_aio(self._exec)

        def _exec(self, *command: object, **kwargs: object) -> object:
            self.exec_calls.append((command, kwargs))
            return self.process

    sandbox = _FakeSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)

    started = await session.pty_exec_start("python3", shell=False, tty=True, yield_time_s=0.05)

    assert started.process_id is not None
    assert b">>>" in started.output
    assert sandbox.exec_calls == [
        (("python3",), {"text": False, "timeout": None, "pty": True}),
    ]

    updated = await session.pty_write_stdin(
        session_id=started.process_id,
        chars="5 + 5\n",
        yield_time_s=0.05,
    )

    assert updated.process_id == started.process_id
    assert b"10" in updated.output
    assert sandbox.process.stdin.writes == [b"5 + 5\n"]

    await session.pty_terminate_all()


@pytest.mark.asyncio
async def test_modal_pty_start_drains_all_buffered_output_after_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self.read = _with_aio(self._read)

        def __aiter__(self) -> _FakeStream:
            return self

        async def __anext__(self) -> bytes:
            if self._chunks:
                return self._chunks.pop(0)
            raise StopAsyncIteration

        def _read(self, _size: int | None = None) -> bytes:
            raise AssertionError("PTY output collection should use stream iteration")

    class _FakeProcess:
        def __init__(self) -> None:
            self.stdout = _FakeStream([b"out-1", b"out-2", b"out-3"])
            self.stderr = _FakeStream([b"err-1", b"err-2"])
            self.poll = _with_aio(lambda: 0)
            self.terminate = _with_aio(lambda: None)

    class _FakeSandbox:
        object_id = "sb-exited"

        def __init__(self) -> None:
            self.process = _FakeProcess()
            self.exec = _with_aio(self._exec)

        def _exec(self, *command: object, **kwargs: object) -> object:
            _ = (command, kwargs)
            return self.process

    sandbox = _FakeSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)

    started = await session.pty_exec_start("python3", shell=False, tty=True, yield_time_s=0.05)

    assert started.process_id is None
    assert started.exit_code == 0
    assert started.output == b"out-1err-1out-2out-3err-2"


@pytest.mark.asyncio
async def test_modal_pty_start_wraps_startup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FailingSandbox:
        object_id = "sb-fail"

        def __init__(self) -> None:
            self.exec = _with_aio(self._exec)

        def _exec(self, *command: object, **kwargs: object) -> object:
            _ = (command, kwargs)
            raise FileNotFoundError("missing-shell")

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id="sb-fail",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=_FailingSandbox())

    with pytest.raises(modal_module.ExecTransportError):
        await session.pty_exec_start("python3", shell=False, tty=True)


@pytest.mark.asyncio
async def test_modal_pty_start_maps_timeout_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _TimeoutSandbox:
        object_id = "sb-timeout"

        def __init__(self) -> None:
            self.exec = _with_aio(self._exec)

        def _exec(self, *command: object, **kwargs: object) -> object:
            _ = (command, kwargs)
            raise asyncio.TimeoutError()

    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id="sb-timeout",
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=_TimeoutSandbox())

    with pytest.raises(modal_module.ExecTimeoutError):
        await session.pty_exec_start("python3", shell=False, tty=True, timeout=2.0)


@pytest.mark.asyncio
async def test_modal_pty_start_cleans_up_unregistered_process_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal_module, _create_calls, _registry_tags = _load_modal_module(monkeypatch)

    class _FakeStream:
        def __init__(self) -> None:
            self.read = _with_aio(lambda: b"")

    class _FakeProcess:
        def __init__(self) -> None:
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.poll = _with_aio(lambda: None)
            self.terminate_calls = 0
            self.terminate = _with_aio(self._terminate)

        def _terminate(self) -> None:
            self.terminate_calls += 1

    class _FakeSandbox:
        object_id = "sb-cancel"

        def __init__(self) -> None:
            self.process = _FakeProcess()
            self.exec = _with_aio(lambda *args, **kwargs: self.process)

    sandbox = _FakeSandbox()
    state = modal_module.ModalSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=modal_module.resolve_snapshot(None, "snapshot"),
        app_name="sandbox-tests",
        sandbox_id=sandbox.object_id,
    )
    session = modal_module.ModalSandboxSession.from_state(state, sandbox=sandbox)

    async def _raise_cancelled() -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(session, "_prune_pty_processes_if_needed", _raise_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await session.pty_exec_start("python3", shell=False, tty=True)

    assert sandbox.process.terminate_calls == 1
    assert session._pty_processes == {}  # noqa: SLF001
