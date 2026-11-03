#!/usr/bin/env bash
set -euo pipefail

# Resolve only remote alpha tags reachable from the requested target. GitHub
# Release publication and wheel assets still need a separate read-only check.
python3 - "${1:-origin}" "${2:-v*-alpha.*}" "${3:-origin/feat/v1.0-release}" <<'PY'
import re
import subprocess
import sys

remote, pattern, target = sys.argv[1:]

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

target = git("rev-parse", "--verify", f"{target}^{{commit}}")
refs = git("ls-remote", "--tags", "--refs", remote, pattern)
candidates = []
for line in refs.splitlines():
    sha, ref = line.split()
    tag = ref.removeprefix("refs/tags/")
    match = re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-alpha\.(0|[1-9]\d*)", tag)
    if match:
        candidates.append((tuple(map(int, match.groups())), sha, ref, tag))
for _, sha, ref, tag in sorted(candidates, reverse=True):
    subprocess.run(["git", "fetch", "--quiet", "--no-tags", remote, ref], check=True)
    if git("rev-parse", "FETCH_HEAD") != sha:
        raise SystemExit(f"Remote tag {tag} changed during inspection; retry.")
    commit = git("rev-parse", "FETCH_HEAD^{commit}")
    result = subprocess.run(["git", "merge-base", "--is-ancestor", commit, target])
    if result.returncode == 1:
        continue
    if result.returncode != 0:
        raise SystemExit("Unable to check release tag ancestry.")
    # Do not overwrite a conflicting local tag and then review different bytes.
    local = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], capture_output=True, text=True)
    if local.returncode == 0 and local.stdout.strip() != sha:
        raise SystemExit(f"Local tag {tag} differs from origin; resolve it before review.")
    if local.returncode not in (0, 1):
        raise SystemExit(f"Unable to inspect local tag {tag}.")
    if local.returncode == 1:
        subprocess.run(["git", "fetch", "--quiet", "--no-tags", remote, f"{ref}:{ref}"], check=True)
        if git("rev-parse", ref) != sha:
            raise SystemExit(f"Remote tag {tag} changed during inspection; retry.")
    print(tag)
    break
else:
    raise SystemExit("No remote alpha release tag is reachable from the review target.")
PY
