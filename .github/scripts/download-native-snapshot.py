"""Download one exact native publication and reject changed archive bytes."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile


def download_archive(artifact_id, digest, source_run, expected_name, archive):
    artifact_id = str(artifact_id)
    if not artifact_id.isdecimal() or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid native snapshot identity")
    endpoint = f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/artifacts/{artifact_id}"
    metadata = json.loads(subprocess.check_output(["gh", "api", endpoint], text=True, timeout=30))
    if (
        str(metadata["workflow_run"]["id"]) != source_run
        or metadata["name"] != expected_name
        or metadata["expired"]
        or metadata["digest"] != digest
    ):
        raise ValueError("Native snapshot provenance changed after selection")
    subprocess.run(["gh", "api", endpoint + "/zip"], stdout=archive, check=True, timeout=300)
    archive.seek(0)
    checksum = hashlib.sha256()
    for chunk in iter(lambda: archive.read(1024 * 1024), b""):
        checksum.update(chunk)
    if "sha256:" + checksum.hexdigest() != digest:
        raise ValueError("Native snapshot archive digest mismatch")
    archive.seek(0)


def main():
    identity = os.environ["RUNNER_OS"] + "-" + os.environ["RUNNER_ARCH"]
    selected = json.loads(os.environ["NATIVE_ARTIFACTS"])[identity]
    artifact_id, digest = selected["id"], selected["digest"]
    destination = Path(os.environ["SNAPSHOT_DIRECTORY"])
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryFile() as archive:
        download_archive(
            artifact_id,
            digest,
            os.environ["NATIVE_SOURCE_RUN"],
            selected["name"],
            archive,
        )
        with ZipFile(archive) as bundle:
            names = []
            for member in bundle.infolist():
                name = member.filename
                if (
                    not name
                    or name in {".", ".."}
                    or "/" in name
                    or "\\" in name
                    or ":" in name
                    or member.is_dir()
                    or member.external_attr >> 28 == 0xA
                    or name in names
                ):
                    raise ValueError("Unexpected native snapshot archive member")
                names.append(name)
            wheels = [
                name
                for name in names
                if name.startswith("openai_agents-") and name.endswith(".whl")
            ]
            if len(wheels) != 1:
                raise ValueError("Native snapshot must contain one SDK wheel")
            fixture = "exec-server-fixture" + (".exe" if os.name == "nt" else "")
            if set(names) != {
                wheels[0],
                wheels[0] + ".pristine.json",
                fixture,
                "native-provenance.json",
            }:
                raise ValueError("Native snapshot product inventory differs")
            if json.loads(bundle.read("native-provenance.json")) != selected["provenance"]:
                raise ValueError("Native snapshot captured identity differs")
            bundle.extractall(destination)
    print(f"Verified native snapshot {artifact_id} ({digest})")


if __name__ == "__main__":
    main()
