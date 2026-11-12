#!/usr/bin/env bash
set -euo pipefail

script_dir=$(dirname -- "${BASH_SOURCE[0]}")
install_manylinux_openssl() (
  openssl_version=3.6.3
  openssl_checksum=243a86649cf6f23eeb6a2ff2456e09e5d77dd9018a54d3d96b0c6bdd6ba6c7f1
  openssl_source=$(mktemp -d "$RUNNER_TEMP/openssl-source-XXXXXX")
  trap 'rm -rf -- "$openssl_source"' EXIT
  openssl_archive="$openssl_source/source.tar.gz"
  openssl_prefix="$RUNNER_TEMP/agents-openssl"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 2 \
    --connect-timeout 8 --max-time 60 --output "$openssl_archive" \
    "https://github.com/openssl/openssl/releases/download/openssl-$openssl_version/openssl-$openssl_version.tar.gz"
  printf '%s  %s\n' "$openssl_checksum" "$openssl_archive" | sha256sum --check --strict
  tar -xzf "$openssl_archive" -C "$openssl_source" --strip-components=1
  cd -- "$openssl_source"
  ./Configure "linux-$(uname -m)" no-shared no-tests \
    --prefix="$openssl_prefix" --libdir=lib --openssldir=/etc/ssl -fPIC
  make -j"${CARGO_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN)}" install_dev
  test -f "$openssl_prefix/lib/libssl.a"
  test -f "$openssl_prefix/lib/libcrypto.a"
  cp -- LICENSE.txt "$openssl_prefix/LICENSE.txt"
  {
    printf 'OPENSSL_DIR=%s\n' "$openssl_prefix"
    printf 'OPENAI_AGENTS_NATIVE_OPENSSL_SHA256=%s\n' "$openssl_checksum"
  } >> "$GITHUB_ENV"
)

install_manylinux_github_cli() (
  version=2.103.3
  case "$(uname -m)" in
    x86_64)
      architecture=amd64
      checksum=4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d
      ;;
    aarch64)
      architecture=arm64
      checksum=ee32df2323c8847f5fac6cb6b4b69ca75828e9329a8f22f677021f7b3306ce63
      ;;
    *)
      echo "Unsupported manylinux GitHub CLI architecture: $(uname -m)" >&2
      exit 1
      ;;
  esac
  release="gh_${version}_linux_${architecture}"
  archive=$(mktemp "$RUNNER_TEMP/gh-XXXXXX.tar.gz")
  trap 'rm -f -- "$archive"' EXIT
  destination="$RUNNER_TEMP/agents-gh-$version"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 2 \
    --connect-timeout 8 --max-time 60 --output "$archive" \
    "https://github.com/cli/cli/releases/download/v$version/$release.tar.gz"
  printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --strict
  mkdir -p -- "$destination"
  tar -xzf "$archive" -C "$destination" --strip-components=2 "$release/bin/gh"
  "$destination/gh" --version
  printf '%s\n' "$destination" >> "$GITHUB_PATH"
)

if [[ -n "${OPENAI_AGENTS_NATIVE_BUILD_IMAGE:-}" ]]; then
  dnf install --assumeyes clang cmake pkgconf-pkg-config perl-core
  if [[ "${OPENAI_AGENTS_RESTORE_NATIVE_DEPENDENCIES:-0}" == 1 ]] &&
    ! gh --version >/dev/null 2>&1; then
    install_manylinux_github_cli
  fi
  install_manylinux_openssl
elif ! command -v clang >/dev/null ||
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
