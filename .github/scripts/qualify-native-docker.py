"""Aggregate this job's trusted native/probe owners after independent cleanup."""

import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree


def require(condition, message):
    if not condition:
        raise ValueError(message)


def live_context(env=os.environ, git=None):
    if git is None:

        def git(*arguments):
            return subprocess.check_output(["git", *arguments], text=True).strip()

    require(
        env.get("GITHUB_ACTIONS") == "true" and env.get("RUNNER_ENVIRONMENT") == "github-hosted",
        "Not the disposable hosted Actions job",
    )
    sha = git("rev-parse", "HEAD")
    parents = [
        line.split()[1]
        for line in git("cat-file", "-p", "HEAD").split("\n\n", 1)[0].splitlines()
        if line.startswith("parent ")
    ]
    require(sha == env["GITHUB_SHA"], "Checkout differs from GITHUB_SHA")
    require(env["GITHUB_REPOSITORY"] == "openai/openai-agents-python", "Wrong repository")
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    require(event["repository"]["full_name"] == env["GITHUB_REPOSITORY"], "Wrong event repository")
    if env["GITHUB_EVENT_NAME"] == "pull_request":
        pr = event["pull_request"]
        require(
            env["GITHUB_REF"] == f"refs/pull/{event['number']}/merge", "Not a PR merge checkout"
        )
        require(
            pr["base"]["ref"] == "feat/codex-harness"
            and pr["base"]["repo"]["full_name"] == env["GITHUB_REPOSITORY"]
            and pr["head"]["repo"]["full_name"] == env["GITHUB_REPOSITORY"],
            "Untrusted PR lineage",
        )
        require(parents == [pr["base"]["sha"], pr["head"]["sha"]], "Wrong raw merge parents")
    else:
        require(
            env["GITHUB_EVENT_NAME"] == "push"
            and env["GITHUB_REF"] == "refs/heads/feat/codex-harness"
            and event["after"] == sha
            and event["ref"] == env["GITHUB_REF"]
            and event.get("deleted") is False,
            "Unexpected push lineage",
        )
    return {
        **{
            key: env[key]
            for key in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")
        },
        "checkoutParents": parents,
        "event": env["GITHUB_EVENT_NAME"],
        "ref": env["GITHUB_REF"],
    }


# Exact pytest case and number of inspected, separately owned containers.
NATIVE_CASES = {
    "test_native_docker_path_grants_survive_restore": ("test_native_docker_path_authority", 3),
    "test_docker_resource_keeps_read_and_write_grants_under_native_session": (
        "test_native_docker_path_authority",
        1,
    ),
    "test_native_docker_mapped_shell_and_patch_after_cold_reopen": (
        "test_native_docker_mapped_tools",
        2,
    ),
    "test_native_selected_docker_users_have_distinct_current_resources": (
        "test_native_docker_run_as",
        2,
    ),
    "test_native_selected_docker_user_does_not_add_executor_grants": (
        "test_native_docker_run_as",
        1,
    ),
    "test_native_missing_docker_user_fails_before_sampling_and_retires_resource": (
        "test_native_docker_run_as",
        1,
    ),
    "test_native_docker_file_reads_follow_selected_process_user[0:0-True]": (
        "test_native_docker_run_as",
        1,
    ),
    "test_native_docker_file_reads_follow_selected_process_user[10001:10001-False]": (
        "test_native_docker_run_as",
        1,
    ),
    "test_native_plain_docker_identity_rebinds_on_cold_restore": ("test_native_docker_run_as", 3),
}
SYSCALLS = {
    "uts-clone-denied",
    "cgroup-unshare-denied",
    "setns-denied",
    "fsopen-denied",
    "open-tree-denied",
    "fsmount-denied",
    "move-mount-denied",
    "mount-setattr-denied",
    "bpf-denied",
    "keyctl-denied",
}
ALIASES = {"metadata-within-root-bind-write-denied"} | {
    root + "-" + kind + "-bind-write-denied"
    for root in ("workspace", "oldroot", "newroot")
    for kind in ("proc", "sys")
}


def check_probes(probes, profile):
    require(
        probes.get("passed") is True and probes["outer"]["apparmor"] == profile + " (enforce)",
        "Failed/wrong-profile probes",
    )
    rows = {row["name"]: row for row in probes["checks"]}
    require(
        len(rows) == len(probes["checks"]) == 18
        and set(rows) == SYSCALLS | ALIASES | {"bwrap-positive-and-protected-access"},
        "Wrong eighteen probes",
    )
    require(
        all(row.get("passed") is True and "error" not in row for row in rows.values()),
        "Failed control",
    )
    positive = rows["bwrap-positive-and-protected-access"]
    require(
        positive.get("procMode") == "inherited"
        and positive.get("inner", {}).get("apparmor") == profile + " (enforce)",
        "Missing trusted positive witness",
    )
    for name in ALIASES:
        row = rows[name]
        if name == "metadata-within-root-bind-write-denied":
            require(
                re.fullmatch(r"/tools/policy-probe-[^/]+/metadata", row.get("source", ""))
                and row.get("alias") == row["source"] + "-alias",
                "Wrong metadata alias slot",
            )
        else:
            root, kind = name.split("-", 2)[:2]
            target = (
                r"/workspace/policy-probe-[^/]+/" + kind
                if root == "workspace"
                else "/" + root + "/" + kind
            )
            require(
                row.get("source") == "/" + kind and re.fullmatch(target, row.get("alias", "")),
                "Wrong protected alias slot",
            )
        stage = row.get("denialStage")
        if stage == "alias-bind-mount-denied":
            require(row.get("inner") is None, "Bind-denied control unexpectedly entered")
        else:
            require(
                stage == "inner-access-and-remount-denied"
                and isinstance(row.get("inner"), dict)
                and row["inner"].get("apparmor") == profile + " (enforce)",
                "Unclassified/unsupported hostile stage",
            )


def qualify(evidence, current, image):
    def data(name):
        return json.loads((evidence / name).read_text())

    manifest = data("manifest.json")
    profile = f"agents-native-ci-{current['GITHUB_RUN_ID']}-{current['GITHUB_RUN_ATTEMPT']}"
    require(
        manifest["sourceCommit"] == current["GITHUB_SHA"]
        and manifest["runId"] == current["GITHUB_RUN_ID"]
        and manifest["runAttempt"] == current["GITHUB_RUN_ATTEMPT"],
        "Source/run drift",
    )
    require(
        manifest["apparmor"]["name"] == profile and manifest["imageId"] == image,
        "Image/profile drift",
    )
    check_probes(data("probes.json"), profile)
    xml = ElementTree.parse(evidence / "native-docker.xml")
    expected = {
        ("packages.codex.tests." + module, name) for name, (module, _) in NATIVE_CASES.items()
    }
    cases = xml.findall(".//testcase")
    require(
        len(cases) == len(expected)
        and {(c.get("classname"), c.get("name")) for c in cases} == expected,
        "Wrong exact native cases",
    )
    require(
        not any(xml.findall(".//" + tag) for tag in ("skipped", "failure", "error")),
        "Native failure/skip",
    )
    require(
        all(
            int(s.get(k, 0)) == 0
            for s in xml.iter()
            if s.tag in {"testsuite", "testsuites"}
            for k in ("failures", "errors", "skipped")
        ),
        "Native summary failure",
    )
    receipts = [
        json.loads(line)
        for line in (evidence / "inspection-receipts.jsonl").read_text().splitlines()
    ]
    expected_inspections = {
        "packages/codex/tests/" + module + ".py::" + name: count
        for name, (module, count) in NATIVE_CASES.items()
    }
    require(
        Counter(r["case"] for r in receipts) == expected_inspections
        and len({r["containerId"] for r in receipts}) == len(receipts),
        "Incomplete owner inspections",
    )
    require(
        all(r["imageId"] == image and r["profile"] == profile for r in receipts),
        "Inspection owner drift",
    )
    finalized = data("finalization.json")
    require(
        finalized
        == {
            "ownedCleanup": 0,
            "loadedEnforce": 0,
            "profileRemove": 0,
            "profileAbsent": 1,
            "journal": 0,
            "filter": 0,
        },
        "Cleanup/audit/removal incomplete",
    )
    audit = (evidence / "profile-audit.log").read_text().splitlines()
    unique = r'profile="' + re.escape(profile) + r'(?:/[^"\n]*)?"'
    require(audit and all(re.search(unique, line) for line in audit), "Unrelated/empty audit")
    fields = [dict(re.findall(r'([a-z_]+)="([^"]*)"', line)) for line in audit]
    require(
        any(
            row.get("apparmor") == "DENIED"
            and row.get("operation") == "mount"
            and row.get("name") in {"/proc/sys/", "/sys/"}
            and {"rw", "remount", "bind"} <= set(row.get("flags", "").split(", "))
            for row in fields
        ),
        "No protected writable-remount denial",
    )
    require(manifest["diagnosticOnly"] is False, "Diagnostic profile has not been promoted")
    return {
        **current,
        "qualified": True,
        "nativeCases": len(NATIVE_CASES),
        "inspectedContainers": len(receipts),
        "classifiedProbes": 18,
    }


if __name__ == "__main__":
    # The independent always-cleanup step has already completed, even on parse errors.
    evidence = Path(os.environ["NATIVE_DOCKER_EVIDENCE"])
    result = qualify(evidence, live_context(), os.environ["CODEX_DOCKER_TEST_IMAGE"])
    (evidence / "qualification.json").write_text(json.dumps(result, indent=2) + "\n")
