from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Final

_HELPER_INSTALL_ROOT: Final[PurePosixPath] = PurePosixPath("/tmp/openai-agents/bin")
_INSTALL_MARKER: Final[str] = "INSTALL_RUNTIME_HELPER_V1"

_RESOLVE_WORKSPACE_PATH_SCRIPT: Final[str] = """
#!/bin/sh
# RESOLVE_WORKSPACE_REALPATH_V1
set -eu

root="$1"
candidate="$2"
for_write="$3"
shift 3
max_symlink_depth=64

case "$for_write" in
    0|1) ;;
    *)
        printf 'for_write must be 0 or 1: %s\\n' "$for_write" >&2
        exit 64
        ;;
esac

if [ $(( $# % 2 )) -ne 0 ]; then
    printf 'extra path grants must be root/read_only pairs\\n' >&2
    exit 64
fi

resolve_path() {
    path="$1"
    depth="${2:-0}"
    seen="${3:-}"
    if [ "$path" = "/" ]; then
        printf '/\\n'
        return 0
    fi

    if [ "$depth" -ge "$max_symlink_depth" ]; then
        printf 'symlink resolution depth exceeded: %s\\n' "$path" >&2
        exit 112
    fi

    if [ -d "$path" ]; then
        (
            cd "$path"
            pwd -P
        )
        return 0
    fi

    parent=${path%/*}
    base=${path##*/}
    if [ -z "$parent" ] || [ "$parent" = "$path" ]; then
        parent="/"
    fi

    resolved_parent=$(resolve_path "$parent" "$depth" "$seen")
    candidate_path="$resolved_parent/$base"
    if [ -L "$candidate_path" ]; then
        case ":$seen:" in
            *":$candidate_path:"*)
                printf 'symlink resolution depth exceeded: %s\\n' "$candidate_path" >&2
                exit 112
                ;;
        esac
        target=$(readlink "$candidate_path")
        next_depth=$((depth + 1))
        next_seen="${seen}:$candidate_path"
        case "$target" in
            /*) resolve_path "$target" "$next_depth" "$next_seen" ;;
            *) resolve_path "$resolved_parent/$target" "$next_depth" "$next_seen" ;;
        esac
        return 0
    fi

    printf '%s\\n' "$candidate_path"
}

resolved_candidate=$(resolve_path "$candidate" 0)
best_grant_root=""
best_grant_original=""
best_grant_read_only="0"
best_grant_len=0

check_root() {
    allowed_root="$1"
    resolved_root=$(resolve_path "$allowed_root" 0)
    case "$resolved_candidate" in
        "$resolved_root"|"$resolved_root"/*)
            printf '%s\\n' "$resolved_candidate"
            exit 0
            ;;
    esac
}

reject_root_grant() {
    allowed_root="$1"
    resolved_root=$(resolve_path "$allowed_root" 0)
    if [ "$resolved_root" = "/" ]; then
        printf 'extra path grant must not resolve to filesystem root: %s\\n' "$allowed_root" >&2
        exit 113
    fi
}

consider_extra_grant() {
    allowed_root="$1"
    read_only="$2"
    case "$read_only" in
        0|1) ;;
        *)
            printf 'extra path grant read_only must be 0 or 1: %s\\n' "$read_only" >&2
            exit 64
            ;;
    esac

    reject_root_grant "$allowed_root"
    resolved_root=$(resolve_path "$allowed_root" 0)
    case "$resolved_candidate" in
        "$resolved_root"|"$resolved_root"/*)
            root_len=${#resolved_root}
            if [ "$root_len" -gt "$best_grant_len" ]; then
                best_grant_root="$resolved_root"
                best_grant_original="$allowed_root"
                best_grant_read_only="$read_only"
                best_grant_len="$root_len"
            fi
            ;;
    esac
}

while [ "$#" -gt 0 ]; do
    consider_extra_grant "$1" "$2"
    shift 2
done

check_root "$root"
if [ -n "$best_grant_root" ]; then
    if [ "$for_write" = "1" ] && [ "$best_grant_read_only" = "1" ]; then
        printf 'read-only extra path grant: %s\\nresolved path: %s\\n' \
            "$best_grant_original" "$resolved_candidate" >&2
        exit 114
    fi
    printf '%s\\n' "$resolved_candidate"
    exit 0
fi

printf 'workspace escape: %s\\n' "$resolved_candidate" >&2
exit 111
""".strip()

_WORKSPACE_FINGERPRINT_SCRIPT: Final[str] = """
#!/bin/sh
# WORKSPACE_FINGERPRINT_V2
set -eu

if [ "$#" -lt 4 ]; then
    printf '%s\\n' \
        "usage: $0 <workspace-root> <version> <output-path>" \
        " <manifest-digest> [exclude-relpath ...]" >&2
    exit 64
fi

workspace_root=$1
version=$2
output_path=$3
manifest_digest=$4
shift 4

if [ ! -d "$workspace_root" ]; then
    printf 'workspace root not found: %s\\n' "$workspace_root" >&2
    exit 66
fi

case "$workspace_root" in
    *"'"*)
        printf 'workspace root contains unsupported single quote: %s\\n' "$workspace_root" >&2
        exit 65
        ;;
esac

quote_sh() {
    value=$1
    case "$value" in
        *"'"*)
            printf 'unsupported single quote in argument: %s\\n' "$value" >&2
            exit 65
            ;;
        *)
            printf "'%s'" "$value"
            ;;
    esac
}

hash_stdin() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | awk '{print $1}'
        return
    fi
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 | awk '{print $1}'
        return
    fi
    if command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 | awk '{print $NF}'
        return
    fi
    printf 'workspace fingerprint helper requires sha256sum, shasum, or openssl\\n' >&2
    exit 127
}

tar_cmd="tar"
for rel in "$@"; do
    case "$rel" in
        ""|"."|"/"|*"/.."|*"/../"*|".."|../*|*/../*|/*)
            printf 'exclude relpath must be a concrete relative path: %s\\n' "$rel" >&2
            exit 65
            ;;
    esac
    quoted_rel=$(quote_sh "$rel")
    quoted_dot_rel=$(quote_sh "./$rel")
    tar_cmd="$tar_cmd --exclude=$quoted_rel --exclude=$quoted_dot_rel"
done

tar_cmd="$tar_cmd -C $(quote_sh "$workspace_root") -cf - ."

workspace_fingerprint=$(
    sh -lc "$tar_cmd" | hash_stdin
)
fingerprint=$(
    printf '%s\\n%s\\n' "$workspace_fingerprint" "$manifest_digest" | hash_stdin
)

payload=$(printf '{"fingerprint":"%s","version":"%s"}\n' "$fingerprint" "$version")
mkdir -p -- "$(dirname -- "$output_path")"
tmp_output="$output_path.tmp.$$"
printf '%s' "$payload" > "$tmp_output"
mv -f -- "$tmp_output" "$output_path"
printf '%s' "$payload"
""".strip()


@dataclass(frozen=True)
class RuntimeHelperScript:
    name: str
    content: str
    install_path: PurePath
    install_marker: str = _INSTALL_MARKER

    @classmethod
    def from_content(cls, *, name: str, content: str) -> RuntimeHelperScript:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        install_path = _HELPER_INSTALL_ROOT / f"{name}-{digest}"
        return cls(name=name, content=content, install_path=install_path)

    def install_command(self) -> tuple[str, ...]:
        tmp_template = f"{self.install_path}.tmp.$$"
        heredoc = f"OPENAI_AGENTS_HELPER_{self.install_path.name.upper().replace('-', '_')}"
        return (
            "sh",
            "-c",
            f"""
# {self.install_marker}
set -eu

dest="$1"
tmp="{tmp_template}"

mkdir -p -- "$(dirname -- "$dest")"

cleanup() {{
    rm -f -- "$tmp"
}}
trap cleanup EXIT INT TERM

cat > "$tmp" <<'{heredoc}'
{self.content}
{heredoc}
chmod 0555 "$tmp"
if [ -d "$dest" ]; then
    rm -rf -- "$dest"
fi
if [ -x "$dest" ] && command -v cmp >/dev/null 2>&1 && cmp -s "$dest" "$tmp"; then
    rm -f -- "$tmp"
    trap - EXIT INT TERM
    exit 0
fi
rm -f -- "$dest"
mv -f -- "$tmp" "$dest"
trap - EXIT INT TERM
""".strip(),
            "sh",
            str(self.install_path),
        )

    def present_command(self) -> tuple[str, ...]:
        return ("test", "-x", str(self.install_path))


RESOLVE_WORKSPACE_PATH_HELPER: Final[RuntimeHelperScript] = RuntimeHelperScript.from_content(
    name="resolve-workspace-path",
    content=_RESOLVE_WORKSPACE_PATH_SCRIPT,
)

WORKSPACE_FINGERPRINT_HELPER: Final[RuntimeHelperScript] = RuntimeHelperScript.from_content(
    name="workspace-fingerprint",
    content=_WORKSPACE_FINGERPRINT_SCRIPT,
)
