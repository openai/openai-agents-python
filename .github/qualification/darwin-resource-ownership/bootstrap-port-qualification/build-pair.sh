#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target="${CARGO_TARGET_DIR:-$here/target}"
manifest="$here/Cargo.toml"
cargo +1.95.0 build --locked --manifest-path "$manifest" --target-dir "$target"
cp "$target/debug/sdk-darwin-bootstrap-entry" "$target/debug/bootstrap-original"
cargo +1.95.0 build --locked --manifest-path "$manifest" --target-dir "$target" \
    --features blocking-worker
cp "$target/debug/sdk-darwin-bootstrap-entry" "$target/debug/bootstrap-stock"
cargo +1.95.0 build --locked --manifest-path "$manifest" --target-dir "$target" \
    --features clear-bootstrap
cp "$target/debug/sdk-darwin-bootstrap-entry" "$target/debug/bootstrap-cleared"
printf '%s\n' "$target/debug/bootstrap-original" "$target/debug/bootstrap-stock" \
    "$target/debug/bootstrap-cleared"
