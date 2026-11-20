#!/usr/bin/env bash
# Reuse the harness's verified manylinux native prerequisites.
set -euo pipefail

# The pinned manylinux image supplies the compiler and these build tools.
for command in cmake pkg-config perl unzip; do
  command -v "$command" >/dev/null
done
if ! perl -MIPC::Cmd -e 1 >/dev/null 2>&1; then
  dnf install --assumeyes perl-core
fi
openssl_version=3.6.3
openssl_checksum=243a86649cf6f23eeb6a2ff2456e09e5d77dd9018a54d3d96b0c6bdd6ba6c7f1
openssl_source=$(mktemp -d "$RUNNER_TEMP/openssl-source-XXXXXX")
trap 'rm -rf -- "$openssl_source"' EXIT
openssl_archive="$openssl_source/source.tar.gz"
openssl_prefix="$RUNNER_TEMP/codex-openssl"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 2 \
  --connect-timeout 8 --max-time 60 --output "$openssl_archive" \
  "https://github.com/openssl/openssl/releases/download/openssl-$openssl_version/openssl-$openssl_version.tar.gz"
printf '%s  %s\n' "$openssl_checksum" "$openssl_archive" | sha256sum --check --strict
tar -xzf "$openssl_archive" -C "$openssl_source" --strip-components=1
(
  cd -- "$openssl_source"
  ./Configure "linux-$(uname -m)" no-shared no-tests \
    --prefix="$openssl_prefix" --libdir=lib --openssldir=/etc/ssl -fPIC
  make -j"${CARGO_BUILD_JOBS:-3}" install_dev
  cp -- LICENSE.txt "$openssl_prefix/LICENSE.txt"
)
printf 'OPENSSL_DIR=%s\nOPENSSL_STATIC=1\n' "$openssl_prefix" >> "$GITHUB_ENV"

protoc_version=21.12
case "$(uname -m)" in
  x86_64)
    architecture=x86_64
    checksum=3a4c1e5f2516c639d3079b1586e703fc7bcfa2136d58bda24d1d54f949c315e8
    ;;
  aarch64)
    architecture=aarch_64
    checksum=2dd17f75d66a682640b136e31848da9fb2eefe68d55303baf8b32617374f6711
    ;;
  *) echo 'Unsupported manylinux architecture.' >&2; exit 1 ;;
esac
archive=$(mktemp "$RUNNER_TEMP/protoc-XXXXXX.zip")
trap 'rm -rf -- "$openssl_source"; rm -f -- "$archive"' EXIT
destination="$RUNNER_TEMP/codex-protoc"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 2 \
  --connect-timeout 8 --max-time 30 --output "$archive" \
  "https://github.com/protocolbuffers/protobuf/releases/download/v$protoc_version/protoc-$protoc_version-linux-$architecture.zip"
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --strict
unzip -oq "$archive" -d "$destination"
"$destination/bin/protoc" --version
printf '%s\n' "$destination/bin" >> "$GITHUB_PATH"
