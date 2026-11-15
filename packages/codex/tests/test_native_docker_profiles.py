"""Runner profiles stay inside the trusted Docker fixture creation boundary."""

import ast
import hashlib
import json
import os
import runpy
import sys
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
        {"DeviceRequests": [{"Count": -1, "Capabilities": [["gpu"]]}]},
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


@pytest.fixture
def qualification_evidence(tmp_path):
    """A small positive owner receipt; hostile mutations exercise the terminal gate."""
    gate = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / ".github/scripts/qualify-native-docker.py")
    )
    current = {
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": "openai/openai-agents-python",
    }
    profile = "agents-native-ci-123-1"

    def save(name, value):
        (tmp_path / name).write_text(json.dumps(value))

    manifest = {
        "sourceCommit": current["GITHUB_SHA"],
        "runId": "123",
        "runAttempt": "1",
        "imageId": IMAGE,
        "apparmor": {"name": profile},
        "diagnosticOnly": False,
    }
    save("manifest.json", manifest)
    checks = [{"name": n, "passed": True} for n in sorted(gate["SYSCALLS"])]
    checks += [
        {
            "name": "bwrap-positive-and-protected-access",
            "passed": True,
            "procMode": "inherited",
            "outerProcLinks": "denied",
            "inner": {"apparmor": profile + " (enforce)"},
        }
    ]
    checks += [
        {
            "name": n,
            "passed": True,
            "denialStage": "alias-bind-mount-denied",
            "inner": None,
            "source": "/tools/policy-probe-unit/metadata"
            if n.startswith("metadata-")
            else "/" + n.split("-")[1],
            "alias": (
                "/tools/policy-probe-unit/metadata-alias"
                if n.startswith("metadata-")
                else "/workspace/policy-probe-unit/" + n.split("-")[1]
                if n.startswith("workspace-")
                else "/" + n.split("-")[0] + "/" + n.split("-")[1]
            ),
        }
        for n in sorted(gate["ALIASES"])
    ]
    save(
        "probes.json",
        {"passed": True, "outer": {"apparmor": profile + " (enforce)"}, "checks": checks},
    )
    from xml.etree import ElementTree

    suite = ElementTree.Element("testsuite", tests="8", errors="0", failures="0", skipped="0")
    receipts = []
    for i, (name, module) in enumerate(gate["NATIVE_CASES"].items()):
        ElementTree.SubElement(
            suite, "testcase", name=name, classname="packages.codex.tests." + module
        )
        receipts.append(
            {
                "case": "packages/codex/tests/" + module + ".py::" + name,
                "containerId": f"{i + 1:064x}",
                "imageId": IMAGE,
                "profile": profile,
            }
        )
    ElementTree.ElementTree(suite).write(tmp_path / "native-docker.xml")
    (tmp_path / "inspection-receipts.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in receipts)
    )
    save(
        "finalization.json",
        {
            "ownedCleanup": 0,
            "loadedEnforce": 0,
            "profileRemove": 0,
            "profileAbsent": 1,
            "journal": 0,
            "filter": 0,
        },
    )
    (tmp_path / "profile-audit.log").write_text(
        'apparmor="DENIED" operation="mount" profile="'
        + profile
        + '" name="/proc/sys/" flags="rw, remount, bind, silent"\n'
    )
    return gate, current, save


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "diagnostic",
        "skip",
        "stage",
        "cleanup",
        "source",
        "audit",
        "slot",
        "missing-outer-proc-links",
        "wrong-outer-proc-links",
    ],
)
def test_qualification_requires_complete_current_postcleanup_owners(
    tmp_path, qualification_evidence, failure
):
    gate, current, save = qualification_evidence
    if failure in {"diagnostic", "source"}:
        data = json.loads((tmp_path / "manifest.json").read_text())
        data["diagnosticOnly" if failure == "diagnostic" else "sourceCommit"] = (
            True if failure == "diagnostic" else "f" * 40
        )
        save("manifest.json", data)
    elif failure == "skip":
        path = tmp_path / "native-docker.xml"
        path.write_text(path.read_text().replace('skipped="0"', 'skipped="1"'))
    elif failure in {"missing-outer-proc-links", "wrong-outer-proc-links"}:
        data = json.loads((tmp_path / "probes.json").read_text())
        positive = next(
            row for row in data["checks"] if row["name"] == "bwrap-positive-and-protected-access"
        )
        if failure == "missing-outer-proc-links":
            del positive["outerProcLinks"]
        else:
            positive["outerProcLinks"] = "accessible"
        save("probes.json", data)
    elif failure == "stage":
        data = json.loads((tmp_path / "probes.json").read_text())
        data["checks"][-1]["denialStage"] = "namespace-startup-failed"
        save("probes.json", data)
    elif failure == "slot":
        data = json.loads((tmp_path / "probes.json").read_text())
        data["checks"][-1]["source"] = "/unrelated"
        save("probes.json", data)
    elif failure == "cleanup":
        data = json.loads((tmp_path / "finalization.json").read_text())
        data["ownedCleanup"] = 1
        save("finalization.json", data)
    elif failure == "audit":
        (tmp_path / "profile-audit.log").write_text('apparmor="DENIED" profile="unrelated"')
    if failure:
        with pytest.raises(ValueError):
            gate["qualify"](tmp_path, current, IMAGE)
    else:
        assert gate["qualify"](tmp_path, current, IMAGE)["qualified"]


WORKFLOW_AUTHORITY_STUB = (
    "#!/usr/bin/python3\n"
    "import os,sys,json,subprocess\n"
    "from pathlib import Path\n"
    "name=Path(sys.argv[0]).name;args=sys.argv[1:];case=os.environ['MATRIX_CASE'"
    "];root=Path(os.environ['MATRIX_ROOT']);profile='agents-native-ci-123-1'\n"
    "def event(x):\n"
    " with (root/'events').open('a') as out:out.write(x+'\\n')\n"
    "if name=='python3':\n"
    " if args and args[0]=='.github/scripts/cleanup-native-docker-containers.py'"
    ":\n"
    "  event('cleanup');print('{\"removedOwnedContainers\":[],\"ownedContainersRema"
    "in\":false}');sys.exit(7 if case=='cleanup' else 0)\n"
    " if case=='record':event('record-failed');sys.exit(9)\n"
    " os.execv('/usr/bin/python3',['/usr/bin/python3',*args])\n"
    "if name=='sudo':\n"
    " if args[0]=='grep':\n"
    "  if '-Fx' in args:event('loaded');sys.exit(1 if case=='partial-install' el"
    "se 0)\n"
    "  event('absent');line='' if (root/'removed').exists() or case=='partial-in"
    "stall' else profile+' (enforce)\\n';sys.exit(subprocess.run(['/usr/bin/grep'"
    ",*args[1:-1]],input=line,text=True).returncode)\n"
    " if args[0]=='apparmor_parser':\n"
    "  if '--names' in args:\n"
    "   event('names')\n"
    "   if case=='malformed-profile':sys.exit(2)\n"
    "   print('unrelated-profile' if case=='unrelated-profile' else profile);sys"
    ".exit(0)\n"
    "  event('unload')\n"
    "  if case=='unload':sys.exit(3)\n"
    "  (root/'removed').touch();sys.exit(0)\n"
    " if args[0]=='stat':event('owner');print('1000:444' if case=='unowned-profi"
    "le' else '0:444');sys.exit(0)\n"
    " if args[0]=='test':event('path');sys.exit(1 if case=='symlink-profile' els"
    "e 0)\n"
    " if args[0]=='journalctl':\n"
    "  event('audit');assert args[1]=='-k'\n"
    "  if case=='journal':sys.exit(4)\n"
    '  if case!=\'empty\':print(\'audit apparmor="DENIED" operation="mount" profile'
    '="\'+profile+\'" name="/proc/sys/"\')\n'
    "  print('audit profile=\"unrelated-profile\"');sys.exit(0)\n"
    "raise SystemExit('Unexpected mock invocation '+repr([name,*args]))\n"
)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux AppArmor workflow shell")
@pytest.mark.parametrize(
    "case",
    [
        "success",
        "empty",
        "journal",
        "cleanup",
        "unload",
        "record",
        "malformed-manifest",
        "unrelated-profile",
    ],
)
def test_workflow_revokes_before_evidence_and_refuses_other_profiles(tmp_path, monkeypatch, case):
    import subprocess
    import textwrap

    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/tests.yml").read_text()
    owner = workflow.split(
        "      - name: Collect audit evidence and finalize owned isolation\n", 1
    )[1]
    script = textwrap.dedent(owner.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
    binary = tmp_path / "bin"
    binary.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "manifest.json").write_text("{")
    for name in ("sudo", "python3"):
        path = binary / name
        path.write_text(WORKFLOW_AUTHORITY_STUB)
        path.chmod(0o755)
    for key, value in {
        "PATH": str(binary) + ":/usr/bin:/bin",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "NATIVE_DOCKER_EVIDENCE": str(evidence),
        "MATRIX_CASE": case,
        "MATRIX_ROOT": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", script], capture_output=True, text=True
    )
    events = (tmp_path / "events").read_text().splitlines()
    assert (result.returncode == 0) == (case in {"success", "malformed-manifest"}), result.stderr
    assert "cleanup" in events and "audit" in events
    assert ("unload" in events) == (case not in {"cleanup", "unrelated-profile"})
    if "unload" in events:
        assert events.index("cleanup") < events.index("unload") < events.index("audit")


@pytest.mark.parametrize("wrong", [None, "checkout", "runner", "repository", "parent"])
def test_qualification_uses_live_hosted_raw_merge_context(tmp_path, wrong):
    script = Path(__file__).resolve().parents[3] / ".github/scripts/qualify-native-docker.py"
    context = runpy.run_path(str(script))["live_context"]
    merge, base, head = "a" * 40, "b" * 40, "c" * 40
    repository = {"full_name": "openai/openai-agents-python"}
    event = {
        "repository": repository,
        "number": 6020,
        "pull_request": {
            "base": {"ref": "feat/codex-harness", "sha": base, "repo": repository},
            "head": {"sha": head, "repo": repository},
        },
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    env = {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_REPOSITORY": repository["full_name"],
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_REF": "refs/pull/6020/merge",
        "GITHUB_SHA": head if wrong == "checkout" else merge,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    if wrong == "runner":
        env["RUNNER_ENVIRONMENT"] = "self-hosted"
    if wrong == "repository":
        env["GITHUB_REPOSITORY"] = "another/repository"
    answers = {
        ("rev-parse", "HEAD"): merge,
        ("cat-file", "-p", "HEAD"): "tree "
        + "d" * 40
        + "\nparent "
        + base
        + "\nparent "
        + (base if wrong == "parent" else head)
        + "\n\nmerge",
    }

    def git(*args):
        return answers[args]

    if wrong:
        with pytest.raises(ValueError):
            context(env, git)
    else:
        assert context(env, git)["checkoutParents"] == [base, head]


@pytest.mark.parametrize(
    "error", ["Permission denied", "Operation not permitted", "Read-only file system"]
)
def test_alias_mount_denial_requires_exact_mount_and_permission_error(tmp_path, error):
    script = Path(__file__).resolve().parents[3] / ".github/scripts/probe-native-docker-profiles.py"
    tree = ast.parse(script.read_text(), filename=str(script))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "is_alias_mount_denial"
    )
    namespace = {"os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(script), "exec"), namespace)
    classify = namespace["is_alias_mount_denial"]
    source = tmp_path / "source"
    source.mkdir()
    link = tmp_path / "source-link"
    link.symlink_to(source, target_is_directory=True)
    alias = "/workspace/policy-probe-unit/proc"

    def result(source_name=source, destination=alias, message=error, returncode=1):
        return SimpleNamespace(
            returncode=returncode,
            stderr=(
                f"bwrap: Can't bind mount {source_name} on {destination}: "
                f"Unable to mount source on destination: {message}\n"
            ),
        )

    assert classify(result(), str(link), alias)
    assert not classify(result(source_name=link), str(link), alias)
    assert not classify(result(source_name="/unrelated"), str(link), alias)
    assert not classify(result(destination=alias + "-other"), str(link), alias)
    assert not classify(result(message="No such file or directory"), str(link), alias)
    assert not classify(result(returncode=0), str(link), alias)
    assert not classify(
        SimpleNamespace(returncode=1, stderr=f"bwrap: Creating new namespace failed: {error}\n"),
        str(link),
        alias,
    )
