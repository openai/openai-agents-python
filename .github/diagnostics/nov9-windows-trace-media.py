"""Pin and verify the official wheel consumed by the original Windows job."""

import hashlib
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

SOURCE = "e340601882cc8b6dad78bb75fad41310a6f706d5"
TREE = "7e02895195e477887d377a0601d9442e8211b3f1"
FIXTURE = "packages/codex/tests/test_native_trace_exporter.py"
FIXTURE_SHA = "3fa5064bed53d1d7a276d96c1078b0156cf997d1a7e8dce0441e6a35077e25cf"
FINGERPRINT = "887acaeba4a82265182c0d9efefcf325b3a7c3e093788fd0a2e4087271af1b68"
WHEEL = "openai_agents-0.24.0a7-cp310-abi3-win_amd64.whl"
WHEEL_SHA = "d8cfdf6d8f6d914d270eeb627ebe219cf9512b70cc4b86246e29daf48d6657ff"
ADDON = "agents/_native.pyd"


def sha(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    project = Path.cwd()
    identity = subprocess.check_output(
        ["git", "rev-parse", "HEAD", "HEAD^{tree}"], text=True
    ).splitlines()
    assert identity == [SOURCE, TREE]
    with (project / FIXTURE).open("rb") as fixture:
        assert sha(fixture) == FIXTURE_SHA
    integrity = runpy.run_path("packages/codex/scripts/source_integrity.py")
    assert integrity["native_bundle_identity"](project) == FINGERPRINT
    print(f"Source {SOURCE} tree {TREE}; fixture SHA-256 {FIXTURE_SHA}", flush=True)
    download = runpy.run_path(".github/scripts/download-native-snapshot.py")["download_archive"]
    folder = Path(os.environ["RUNNER_TEMP"]) / "nov9-trace-media"
    folder.mkdir()
    with (folder / "official.zip").open("w+b") as archive:
        download(
            "13836364768",
            "sha256:2d8c6a10d87b91a36cc3f5fd51650d42dfc33a80e5ff25a20067b1f35e9b20de",
            "42974374368",
            "native-Windows-X64",
            archive,
        )
        with ZipFile(archive) as bundle:
            receipt = json.loads(bundle.read(WHEEL + ".pristine.json"))
            inputs = integrity["native_inputs"](project / "packages/codex")
            assert len(inputs) == 248 and inputs == receipt["native_inputs"]
            assert receipt["wheel_sha256"] == WHEEL_SHA
            with bundle.open(WHEEL) as member, (folder / WHEEL).open("wb") as wheel:
                shutil.copyfileobj(member, wheel)
    with (folder / WHEEL).open("rb") as wheel:
        assert sha(wheel) == WHEEL_SHA
    with ZipFile(folder / WHEEL) as wheel, wheel.open(ADDON) as addon:
        addon_sha = sha(addon)
    print(f"Official artifact 13836364768, 248 matched inputs, fingerprint {FINGERPRINT}", flush=True)
    print(f"Wheel SHA-256 {WHEEL_SHA}; addon SHA-256 {addon_sha}", flush=True)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as env:
        print(f"NOV9_VERIFIED_WHEEL={folder / WHEEL}", file=env)
        print(f"NOV9_VERIFIED_ADDON_SHA={addon_sha}", file=env)


if __name__ == "__main__":
    main()
