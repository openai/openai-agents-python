#!/usr/bin/env bash
set -euo pipefail

mirror_list=/etc/apt/apt-mirrors.txt
azure='^https?://azure[.]archive[.]ubuntu[.]com/ubuntu/?([[:space:]]|$)'
archive='^https://archive[.]ubuntu[.]com/ubuntu/?([[:space:]]|$)'

# A runner image retries the first mirror independently for each apt index.
if [[ "${RUNNER_ENVIRONMENT:-}" == github-hosted && -f "$mirror_list" ]] &&
  grep -Eq "$azure" "$mirror_list" &&
  grep -Eq "$archive" "$mirror_list" &&
  ! timeout 10s getent ahosts azure.archive.ubuntu.com >/dev/null; then
  sudo sed -i -E "s@$azure@https://mirrors.edge.kernel.org/ubuntu\\1@" "$mirror_list"
fi
