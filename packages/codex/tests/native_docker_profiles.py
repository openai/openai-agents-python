"""Attach runner-owned enforced profiles only to the opt-in Docker fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


def configure_runner_profiles(api: Any) -> tuple[str, ...]:
    manifest_path = os.environ.get("CODEX_DOCKER_TEST_PROFILE_MANIFEST")
    if manifest_path is None:
        return ()
    manifest = json.loads(Path(manifest_path).read_text())
    if (
        manifest.get("format") != "native-docker-runner-profile-v1"
        or type(manifest.get("diagnosticOnly")) is not bool
        or not re.fullmatch(r"agents-native-ci-[0-9]+-[0-9]+", manifest["apparmor"]["name"])
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["imageId"])
    ):
        raise ValueError("Invalid native Docker runner profile manifest")
    payloads = {}
    for kind in ("seccomp", "apparmor"):
        entry = manifest[kind]
        path = Path(entry["path"])
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Runner profile paths must be absolute regular files")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError("Runner profile bytes differ from their manifest")
        payloads[kind] = payload
    seccomp = json.loads(payloads["seccomp"])
    if seccomp.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise ValueError("The runner seccomp profile must deny unlisted syscalls")
    options = (
        "apparmor=" + manifest["apparmor"]["name"],
        "seccomp=" + json.dumps(seccomp, separators=(",", ":")),
    )
    create = api.create_container

    def create_with_profiles(*args: Any, **kwargs: Any) -> Any:
        config = kwargs.get("host_config")
        labels = kwargs.get("labels") or {}
        owner_label = "openai-agents.native-ci-profile"
        if (
            kwargs.get("image") != manifest["imageId"]
            or not isinstance(config, dict)
            or config.get("NetworkMode") != "none"
            or config.get("Privileged")
            or config.get("CapAdd")
            or config.get("Devices")
            or config.get("SecurityOpt")
            or not isinstance(labels, dict)
            or owner_label in labels
        ):
            raise ValueError("Runner profiles require the unchanged restricted Docker fixture")
        kwargs["host_config"] = {**config, "SecurityOpt": list(options)}
        kwargs["labels"] = {**labels, owner_label: manifest["apparmor"]["name"]}
        return create(*args, **kwargs)

    api.create_container = create_with_profiles
    return options
