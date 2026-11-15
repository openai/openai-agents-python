"""Runner profiles stay inside the trusted Docker fixture creation boundary."""

import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.native_docker_profiles import configure_runner_profiles

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def profile_manifest(tmp_path, monkeypatch):
    seccomp = {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": []}
    files = {
        "seccomp": json.dumps(seccomp).encode(),
        "apparmor": b"profile agents-native-ci-123-1 { }\n",
    }
    manifest = {
        "format": "native-docker-runner-profile-v1",
        "diagnosticOnly": True,
        "imageId": IMAGE,
    }
    for kind, data in files.items():
        path = tmp_path / kind
        path.write_bytes(data)
        manifest[kind] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
    manifest["apparmor"]["name"] = "agents-native-ci-123-1"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("CODEX_DOCKER_TEST_PROFILE_MANIFEST", str(path))
    return path, manifest


def api_with_calls():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return "created-container"

    return SimpleNamespace(create_container=create), calls


def test_unconfigured_runner_keeps_the_docker_default(monkeypatch):
    monkeypatch.delenv("CODEX_DOCKER_TEST_PROFILE_MANIFEST", raising=False)
    api, calls = api_with_calls()
    create = api.create_container
    assert configure_runner_profiles(api) == ()
    assert api.create_container is create
    assert not calls


def test_profiled_runner_preserves_restricted_arguments(profile_manifest, monkeypatch):
    api, calls = api_with_calls()
    options = configure_runner_profiles(api)
    monkeypatch.delenv("CODEX_DOCKER_TEST_PROFILE_MANIFEST")
    config = {"NetworkMode": "none", "Privileged": False, "Mounts": ["existing-grant"]}
    labels = {"existing": "preserved"}
    assert (
        api.create_container(image=IMAGE, host_config=config, labels=labels) == "created-container"
    )
    assert calls == [
        {
            "image": IMAGE,
            "host_config": {**config, "SecurityOpt": list(options)},
            "labels": {
                "existing": "preserved",
                "openai-agents.native-ci-profile": "agents-native-ci-123-1",
            },
        }
    ]
    assert "SecurityOpt" not in config
    assert labels == {"existing": "preserved"}
    assert options[0] == "apparmor=agents-native-ci-123-1"
    assert json.loads(options[1].removeprefix("seccomp="))["defaultAction"] == "SCMP_ACT_ERRNO"


@pytest.mark.parametrize(
    "change",
    [
        {"NetworkMode": "bridge"},
        {"Privileged": True},
        {"CapAdd": ["SYS_ADMIN"]},
        {"Devices": ["/dev/fuse"]},
        {"SecurityOpt": ["seccomp=unconfined"]},
    ],
)
def test_profiles_reject_other_container_authority(profile_manifest, change):
    api, calls = api_with_calls()
    configure_runner_profiles(api)
    with pytest.raises(ValueError, match="unchanged restricted Docker fixture"):
        api.create_container(image=IMAGE, host_config={"NetworkMode": "none", **change})
    assert not calls


def test_profiles_reject_a_different_image(profile_manifest):
    api, calls = api_with_calls()
    configure_runner_profiles(api)
    with pytest.raises(ValueError, match="unchanged restricted Docker fixture"):
        api.create_container(image="other-image", host_config={"NetworkMode": "none"})
    assert not calls


def test_profile_digest_mismatch_fails_before_create(profile_manifest):
    _, manifest = profile_manifest
    Path(manifest["seccomp"]["path"]).write_text('{"defaultAction":"SCMP_ACT_ALLOW"}')
    api, calls = api_with_calls()
    create = api.create_container
    with pytest.raises(ValueError, match="bytes differ"):
        configure_runner_profiles(api)
    assert api.create_container is create
    assert not calls


def test_unconfined_apparmor_name_fails_before_create(profile_manifest):
    path, manifest = profile_manifest
    manifest["apparmor"]["name"] = "unconfined"
    path.write_text(json.dumps(manifest))
    api, calls = api_with_calls()
    with pytest.raises(ValueError, match="Invalid native Docker"):
        configure_runner_profiles(api)
    assert not calls


def test_existing_profile_ownership_label_fails_before_create(profile_manifest):
    api, calls = api_with_calls()
    configure_runner_profiles(api)
    with pytest.raises(ValueError, match="unchanged restricted Docker fixture"):
        api.create_container(
            image=IMAGE,
            host_config={"NetworkMode": "none"},
            labels={"openai-agents.native-ci-profile": "another-owner"},
        )
    assert not calls


def cleanup_function():
    script = (
        Path(__file__).resolve().parents[3] / ".github/scripts/cleanup-native-docker-containers.py"
    )
    return runpy.run_path(str(script))["cleanup"]


def cleanup_fixture():
    profile = "agents-native-ci-123-1"
    identifier = "b" * 64
    row = {
        "Id": identifier,
        "Image": IMAGE,
        "Config": {"Labels": {"openai-agents.native-ci-profile": profile}},
        "HostConfig": {"NetworkMode": "none", "SecurityOpt": ["apparmor=" + profile]},
    }
    present = [identifier]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[:3] == ["docker", "--host", "unix:///run/owned-test.sock"]
        operation = command[3:5]
        if operation == ["container", "ls"]:
            assert command[-2:] == ["--filter", "label=openai-agents.native-ci-profile=" + profile]
            output = "\n".join(present)
        elif operation == ["container", "inspect"]:
            assert command[-1] == identifier
            output = json.dumps([row])
        elif operation == ["container", "rm"]:
            assert command[5:] == ["--force", identifier]
            present.remove(identifier)
            output = identifier
        else:
            raise AssertionError(command)
        return SimpleNamespace(stdout=output)

    return profile, identifier, row, present, calls, run


def test_cleanup_removes_only_owned_profile_container_on_exact_daemon():
    profile, identifier, _, present, _, run = cleanup_fixture()
    assert cleanup_function()("/run/owned-test.sock", profile, IMAGE, run) == [identifier]
    assert not present


@pytest.mark.parametrize("mismatch", ["image", "label", "profile", "network"])
def test_cleanup_rejects_mismatched_container_before_removal(mismatch):
    profile, _, row, present, calls, run = cleanup_fixture()
    if mismatch == "image":
        row["Image"] = "sha256:" + "c" * 64
    elif mismatch == "label":
        row["Config"]["Labels"]["openai-agents.native-ci-profile"] = "another-owner"
    elif mismatch == "profile":
        row["HostConfig"]["SecurityOpt"] = ["apparmor=another-profile"]
    else:
        row["HostConfig"]["NetworkMode"] = "bridge"
    with pytest.raises(ValueError, match="outside this diagnostic run"):
        cleanup_function()("/run/owned-test.sock", profile, IMAGE, run)
    assert present
    assert not any(command[3:5] == ["container", "rm"] for command in calls)


def test_cleanup_refuses_profile_unload_when_an_owned_container_remains():
    profile, identifier, _, present, _, run = cleanup_fixture()

    def concurrent_create(command, **kwargs):
        result = run(command, **kwargs)
        if command[3:5] == ["container", "rm"]:
            present.append(identifier)
        return result

    with pytest.raises(RuntimeError, match="refusing profile unload"):
        cleanup_function()("/run/owned-test.sock", profile, IMAGE, concurrent_create)


def test_cleanup_propagates_removal_failure_before_profile_unload():
    profile, _, _, present, _, run = cleanup_fixture()

    def removal_failed(command, **kwargs):
        if command[3:5] == ["container", "rm"]:
            raise RuntimeError("removal failed")
        return run(command, **kwargs)

    with pytest.raises(RuntimeError, match="removal failed"):
        cleanup_function()("/run/owned-test.sock", profile, IMAGE, removal_failed)
    assert present
