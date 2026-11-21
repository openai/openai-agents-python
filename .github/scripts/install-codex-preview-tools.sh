#!/usr/bin/env bash
# Build the missing native prerequisites from checksum-pinned sources.
set -euo pipefail

# The pinned manylinux image supplies the compiler and these build tools.
for command in cmake pkg-config perl; do
  command -v "$command" >/dev/null
done

source_root=$(mktemp -d "$RUNNER_TEMP/codex-build-tools-XXXXXX")
trap 'rm -rf -- "$source_root"' EXIT
if ! perl -MIPC::Cmd -e 1 >/dev/null 2>&1; then
  # Minimal manylinux Perl omits modules required by OpenSSL's Configure.
  perl_version=5.40.3
  perl_checksum=4c155b4e6160682b38919b55ac319081b898db11857cf18a7d9ffed2648ccaff
  perl_archive="$source_root/perl.tar.gz"
  perl_source="$source_root/perl"
  perl_prefix="$RUNNER_TEMP/codex-perl"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 2 \
    --connect-timeout 8 --max-time 60 --output "$perl_archive" \
    "https://cpan.metacpan.org/authors/id/S/SH/SHAY/perl-$perl_version.tar.gz"
  printf '%s  %s\n' "$perl_checksum" "$perl_archive" | sha256sum --check --strict
  mkdir -- "$perl_source"
  tar -xzf "$perl_archive" -C "$perl_source" --strip-components=1
  (
    cd -- "$perl_source"
    sh Configure -des -Dprefix="$perl_prefix" -Dman1dir=none -Dman3dir=none
    make -j"${CARGO_BUILD_JOBS:-3}"
    make install
  )
  export PATH="$perl_prefix/bin:$PATH"
  printf '%s\n' "$perl_prefix/bin" >> "$GITHUB_PATH"
fi

openssl_version=3.6.3
openssl_checksum=243a86649cf6f23eeb6a2ff2456e09e5d77dd9018a54d3d96b0c6bdd6ba6c7f1
openssl_source="$source_root/openssl"
openssl_archive="$source_root/openssl.tar.gz"
openssl_prefix="$RUNNER_TEMP/codex-openssl"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 2 \
  --connect-timeout 8 --max-time 60 --output "$openssl_archive" \
  "https://github.com/openssl/openssl/releases/download/openssl-$openssl_version/openssl-$openssl_version.tar.gz"
printf '%s  %s\n' "$openssl_checksum" "$openssl_archive" | sha256sum --check --strict
mkdir -- "$openssl_source"
tar -xzf "$openssl_archive" -C "$openssl_source" --strip-components=1
(
  cd -- "$openssl_source"
  ./Configure "linux-$(uname -m)" no-shared no-tests \
    --prefix="$openssl_prefix" --libdir=lib --openssldir=/etc/ssl -fPIC
  make -j"${CARGO_BUILD_JOBS:-3}" install_dev
  cp -- LICENSE.txt "$openssl_prefix/LICENSE.txt"
)
printf 'OPENSSL_DIR=%s\nOPENSSL_STATIC=1\n' "$openssl_prefix" >> "$GITHUB_ENV"
