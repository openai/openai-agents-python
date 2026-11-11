#!/usr/bin/env bash
set -euo pipefail

script_dir=$(dirname -- "${BASH_SOURCE[0]}")
if ! command -v clang >/dev/null ||
  ! command -v cmake >/dev/null ||
  ! command -v pkg-config >/dev/null ||
  ! pkg-config --exists openssl; then
  bash "$script_dir/use-ubuntu-archive-if-needed.sh"
  sudo apt-get update
  sudo apt-get install --yes --no-install-recommends clang cmake pkg-config libssl-dev
fi

if command -v protoc >/dev/null; then
  exit 0
fi

# Match the Protobuf compiler version in Ubuntu 24.04.
version=21.12
case "$(uname -m)" in
  x86_64)
    architecture=x86_64
    checksum=3a4c1e5f2516c639d3079b1586e703fc7bcfa2136d58bda24d1d54f949c315e8
    ;;
  aarch64)
    architecture=aarch_64
    checksum=2dd17f75d66a682640b136e31848da9fb2eefe68d55303baf8b32617374f6711
    ;;
  *)
    bash "$script_dir/use-ubuntu-archive-if-needed.sh"
    sudo apt-get update
    sudo apt-get install --yes --no-install-recommends protobuf-compiler
    exit 0
    ;;
esac

archive=$(mktemp "$RUNNER_TEMP/protoc-XXXXXX.zip")
trap 'rm -f "$archive"' EXIT
destination="$RUNNER_TEMP/agents-protoc-$version"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 2 \
  --connect-timeout 8 --max-time 30 --output "$archive" \
  "https://github.com/protocolbuffers/protobuf/releases/download/v$version/protoc-$version-linux-$architecture.zip"
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --strict
unzip -oq "$archive" -d "$destination"
"$destination/bin/protoc" --version
printf '%s\n' "$destination/bin" >> "$GITHUB_PATH"
