"""Remove only this diagnostic run's labeled, profile-bound fixture containers."""

import json
import os
import re
import subprocess

OWNER_LABEL = "openai-agents.native-ci-profile"


def cleanup(socket, profile, image, run=subprocess.run):
    if (
        not socket.startswith("/")
        or re.fullmatch(r"agents-native-ci-[0-9]+-[0-9]+", profile) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None
    ):
        raise ValueError("Invalid diagnostic cleanup authority")
    docker = ["docker", "--host", "unix://" + socket]

    def invoke(*arguments):
        return run([*docker, *arguments], check=True, capture_output=True, text=True).stdout

    def owned_ids():
        return invoke(
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label={OWNER_LABEL}={profile}",
        ).split()

    removed = []
    for identifier in owned_ids():
        if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
            raise ValueError("Docker returned an invalid owned container ID")
        (row,) = json.loads(invoke("container", "inspect", identifier))
        host = row["HostConfig"]
        if (
            row["Id"] != identifier
            or row["Image"] != image
            or row["Config"]["Labels"].get(OWNER_LABEL) != profile
            or "apparmor=" + profile not in (host.get("SecurityOpt") or [])
            or host.get("NetworkMode") != "none"
            or host.get("Privileged")
            or host.get("CapAdd")
            or host.get("Devices")
        ):
            raise ValueError("Refusing cleanup of a container outside this diagnostic run")
        invoke("container", "rm", "--force", identifier)
        removed.append(identifier)
    if owned_ids():
        raise RuntimeError("Owned diagnostic containers remain; refusing profile unload")
    return removed


if __name__ == "__main__":
    removed = cleanup(
        os.environ["CODEX_DOCKER_TEST_SOCKET"],
        f"agents-native-ci-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
        os.environ["CODEX_DOCKER_TEST_IMAGE"],
    )
    print(json.dumps({"removedOwnedContainers": removed, "ownedContainersRemain": False}))
