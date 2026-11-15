#!/usr/bin/env bash
# Load only a unique, confined diagnostic profile on a disposable hosted runner.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]]
: "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}" "${RUNNER_TEMP:?}"
: "${CODEX_DOCKER_TEST_IMAGE:?}" "${NATIVE_DOCKER_RECIPE:?}" "${NATIVE_DOCKER_EVIDENCE:?}"
[[ $GITHUB_RUN_ID =~ ^[0-9]+$ && $GITHUB_RUN_ATTEMPT =~ ^[0-9]+$ ]]
[[ $CODEX_DOCKER_TEST_IMAGE =~ ^sha256:[0-9a-f]{64}$ ]]
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_commit=$(git rev-parse HEAD)
recipe_sha256=$(sha256sum -- "$NATIVE_DOCKER_RECIPE" | cut -d ' ' -f 1)
date --iso-8601=seconds > "$NATIVE_DOCKER_EVIDENCE/start-time"
stage_parent=$(mktemp -d -- "$RUNNER_TEMP/native-profile.XXXXXXXX")
trap 'rm -rf -- "$stage_parent"' EXIT
python3 "$base/generate-native-docker-profiles.py" --output-dir "$stage_parent/generated" \
  --run-id "$GITHUB_RUN_ID" --run-attempt "$GITHUB_RUN_ATTEMPT" \
  --source-commit "$source_commit" --image-id "$CODEX_DOCKER_TEST_IMAGE" \
  --recipe-sha256 "$recipe_sha256"
apparmor_parser --skip-kernel-load --skip-cache --stdout \
  "$stage_parent/generated/apparmor.profile" > "$NATIVE_DOCKER_EVIDENCE/apparmor.compiled"
cp "$stage_parent/generated/"* "$NATIVE_DOCKER_EVIDENCE/"
sudo test ! -e /etc/openai-agents-native-docker
sudo install -d -o root -g root -m 0755 /etc/openai-agents-native-docker
for file in seccomp.json apparmor.profile manifest.json; do
  sudo install -o root -g root -m 0444 "$stage_parent/generated/$file" \
    "/etc/openai-agents-native-docker/$file"
done
sudo apparmor_parser --replace --skip-cache /etc/openai-agents-native-docker/apparmor.profile
sudo grep -Fx "agents-native-ci-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT (enforce)" \
  /sys/kernel/security/apparmor/profiles
sha256sum "$base/generate-native-docker-profiles.py" "$base/install-native-docker-profiles.sh" \
  "$base/probe-native-docker-profiles.py" "$base/cleanup-native-docker-containers.py" \
  > "$NATIVE_DOCKER_EVIDENCE/profile-code.sha256"
