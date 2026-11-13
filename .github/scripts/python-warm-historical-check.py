import json
import os
import runpy
import subprocess
from pathlib import Path

assert os.environ["GITHUB_REPOSITORY"] == "openai/openai-agents-python"
assert os.environ["GITHUB_EVENT_NAME"] == "push"
assert os.environ["GITHUB_REF"] == "refs/heads/ci/hillclimb/python-warm-historical-11"
base = "8e1a7067b00249432dae979379ab68554af00033"
donor = "43324263604"
expected_identity = "71048ea1d3793c8dd2d94885cf1804ba35c23eb8665b180b92ac7d1490104270"
identity = runpy.run_path("packages/codex/scripts/source_integrity.py")["native_bundle_identity"](
    Path(".")
)
assert identity == expected_identity, "Historical native inputs changed"
allowed = {
    ".github/workflows/python-warm-historical.yml",
    ".github/workflows/codex-python-warm-historical.yml",
    ".github/actions/use-native-historical/action.yml",
    ".github/scripts/python-warm-historical-check.py",
    ".github/workflows/codeql.yml",
    "packages/codex/tests/conftest.py",
    ".github/scripts/update_released_api_contract.py",
}
changed = subprocess.check_output(
    ["git", "diff", "--name-only", base, "HEAD"], text=True
).splitlines()
assert set(changed) <= allowed, "Unexpected source change in historical replay"
provenance = {
    "head_ref": "feat/codex-harness",
    "pull_request": None,
    "repository_id": "946380199",
    "run_attempt": "1",
    "run_id": "43324263604",
    "source_sha": "8e1a7067b00249432dae979379ab68554af00033",
}
snapshots = {
    "macOS-ARM64": {
        "id": 13999891258,
        "digest": "sha256:2d772c725642a24e915b18e75bc7b867ac8202db485b84afd364f59c87f6b3f1",
    },
    "Windows-X64": {
        "id": 13996441291,
        "digest": "sha256:60173f6b642205cd878a9350d4bf47554c55f78fc233a5ae3f1fb4d75ca10c34",
    },
    "Linux-ARM64": {
        "id": 13995122857,
        "digest": "sha256:25e5bb87372379bccf1dd8332c7154eefb2f108d8dfe1f4a12775dcca8a2ade8",
    },
    "Linux-X64": {
        "id": 13993960846,
        "digest": "sha256:e42de4fb0dc341c9ea28f065f56409717bd2657d5d070ebbe79f9f00ee5991ae",
    },
}
for platform, selected in snapshots.items():
    selected["name"] = f"native-snapshot-{platform}-{expected_identity}"
    selected["provenance"] = provenance


def api(path):
    return json.loads(
        subprocess.check_output(
            ["gh", "api", "repos/openai/openai-agents-python/" + path], text=True, timeout=30
        )
    )


run = api("actions/runs/" + donor)
assert run["head_sha"] == base and run["head_branch"] == "feat/codex-harness"
assert run["event"] == "push" and run["path"] == ".github/workflows/tests.yml"
assert run["status"] == "completed" and run["conclusion"] == "success" and run["run_attempt"] == 1
assert (
    run["repository"]["full_name"]
    == run["head_repository"]["full_name"]
    == "openai/openai-agents-python"
)
page = api("actions/runs/" + donor + "/attempts/1/jobs?per_page=100")
assert len(page["jobs"]) == page["total_count"], "Incomplete donor chronology"
jobs = page["jobs"]
starts = [
    job["started_at"]
    for job in jobs
    if job["conclusion"] != "skipped"
    and not job["name"].startswith("produce-")
    and job["name"] != "changes"
]
assert starts
first_sdk = min(starts)
platforms = {
    "Linux-X64": ("linux", "ubuntu-24.04"),
    "Linux-ARM64": ("linux-arm64", "ubuntu-24.04-arm"),
    "macOS-ARM64": ("macos", "macos-latest"),
    "Windows-X64": ("windows", "windows-latest"),
}
for platform, selected in snapshots.items():
    key, runner = platforms[platform]
    meta = api("actions/artifacts/" + str(selected["id"]))
    assert (
        meta["workflow_run"]["id"] == int(donor)
        and meta["name"] == selected["name"]
        and meta["digest"] == selected["digest"]
        and not meta["expired"]
    )
    producers = [
        job
        for job in jobs
        if job["name"] == f"produce-{key} / Native snapshot ({runner})"
        and job["conclusion"] == "success"
    ]
    assert len(producers) == 1
    publications = [
        step
        for step in producers[0]["steps"]
        if step["name"] == "Publish the native snapshot" and step["conclusion"] == "success"
    ]
    assert len(publications) == 1
    publication = publications[0]
    assert (
        publication["started_at"] <= meta["created_at"] <= publication["completed_at"] < first_sdk
    )
    qualifiers = [
        job
        for job in jobs
        if job["name"] == f"native-{key} / Native package ({runner})"
        and job["conclusion"] == "success"
    ]
    assert len(qualifiers) == 1
with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as out:
    out.write("code=true\nwindows=true\n")
    out.write("native_source_run=" + donor + "\n")
    out.write("native_artifacts=" + json.dumps(snapshots, separators=(",", ":")) + "\n")
    out.write(
        "native_provenance="
        + json.dumps(
            {
                "head_ref": "feat/codex-harness",
                "pull_request": None,
                "repository_id": "946380199",
                "run_attempt": "1",
                "run_id": "43324263604",
                "source_sha": "8e1a7067b00249432dae979379ab68554af00033",
            },
            separators=(",", ":"),
        )
        + "\n"
    )
print(
    "Verified fixed donor metadata and publication ordering; "
    "each consumer authenticates archive bytes."
)
