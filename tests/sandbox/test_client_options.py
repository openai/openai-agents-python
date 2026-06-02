from __future__ import annotations

import importlib
from typing import Literal

import pytest

from agents.extensions.sandbox.cloudflare import CloudflareSandboxClientOptions
from agents.extensions.sandbox.daytona import DaytonaSandboxClientOptions
from agents.extensions.sandbox.e2b import E2BSandboxClientOptions
from agents.sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from agents.sandbox.sandboxes import DockerSandboxClientOptions, UnixLocalSandboxClientOptions
from agents.sandbox.session import BaseSandboxClientOptions


def test_sandbox_client_options_parse_uses_registered_builtin_type() -> None:
    parsed = BaseSandboxClientOptions.parse(
        {
            "type": "docker",
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "exposed_ports": [8080],
        }
    )

    assert parsed == DockerSandboxClientOptions(
        image=DEFAULT_PYTHON_SANDBOX_IMAGE, exposed_ports=(8080,)
    )


def test_sandbox_client_options_parse_passthrough_existing_instance() -> None:
    options = UnixLocalSandboxClientOptions(exposed_ports=(8080,))

    parsed = BaseSandboxClientOptions.parse(options)

    assert parsed is options


def test_sandbox_client_options_exclude_unset_preserves_type_discriminator() -> None:
    try:
        modal_module = importlib.import_module("agents.extensions.sandbox.modal")
    except ModuleNotFoundError:
        pytest.skip("modal is not installed")

    payload = modal_module.ModalSandboxClientOptions(app_name="sandbox-tests").model_dump(
        exclude_unset=True
    )

    assert payload == {
        "type": "modal",
        "app_name": "sandbox-tests",
        "sandbox_create_timeout_s": None,
        "workspace_persistence": "tar",
        "snapshot_filesystem_timeout_s": None,
        "snapshot_filesystem_restore_timeout_s": None,
        "exposed_ports": (),
        "gpu": None,
        "timeout": 300,
        "use_sleep_cmd": True,
        "image_builder_version": "2025.06",
        "idle_timeout": None,
    }


@pytest.mark.parametrize(
    "options",
    [
        DockerSandboxClientOptions(image=DEFAULT_PYTHON_SANDBOX_IMAGE, exposed_ports=(8080,)),
        UnixLocalSandboxClientOptions(exposed_ports=(8080,)),
        E2BSandboxClientOptions(sandbox_type="e2b", template="base"),
        DaytonaSandboxClientOptions(image=DEFAULT_PYTHON_SANDBOX_IMAGE),
        CloudflareSandboxClientOptions(worker_url="https://example.com"),
    ],
)
def test_sandbox_client_options_roundtrip_preserves_concrete_type(
    options: BaseSandboxClientOptions,
) -> None:
    payload = options.model_dump(mode="json")

    restored = BaseSandboxClientOptions.parse(payload)

    assert restored == options
    assert type(restored) is type(options)


def test_sandbox_client_options_parse_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown sandbox client options type `unknown`"):
        BaseSandboxClientOptions.parse({"type": "unknown"})


def test_sandbox_client_options_parse_rejects_invalid_payload() -> None:
    with pytest.raises(
        TypeError,
        match="sandbox client options payload must be a BaseSandboxClientOptions or object payload",
    ):
        BaseSandboxClientOptions.parse("docker")


def test_duplicate_sandbox_client_options_type_registration_raises() -> None:
    with pytest.raises(TypeError, match="already registered"):

        class DuplicateDockerSandboxClientOptions(BaseSandboxClientOptions):
            type: Literal["docker"] = "docker"


def test_sandbox_client_options_subclasses_require_type_discriminator_default() -> None:
    with pytest.raises(TypeError, match="must define a non-empty string default for `type`"):

        class MissingTypeSandboxClientOptions(BaseSandboxClientOptions):
            pass
