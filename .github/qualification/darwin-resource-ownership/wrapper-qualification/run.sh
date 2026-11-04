#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
test "$(uname -s)/$(uname -m)" = Darwin/arm64
test "$(id -u)" -ne 0

cargo +1.95.0 build --locked --manifest-path "$root/Cargo.toml" --target-dir "$root/target"
/usr/bin/clang -std=c11 -D_DARWIN_C_SOURCE -Wall -Wextra -Werror \
    "$root/fixture.c" -o "$root/target/qualification-fixture"
"$root/target/debug/sdk-darwin-wrapper-qualification" "$root/target/qualification-fixture"
