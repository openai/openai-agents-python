from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_HELPER_INSTALL_ROOT: Final[Path] = Path("/tmp/openai-agents/bin")
_INSTALL_MARKER: Final[str] = "INSTALL_RUNTIME_HELPER_V1"

_RESOLVE_WORKSPACE_PATH_SCRIPT: Final[str] = """
#!/bin/sh
# RESOLVE_WORKSPACE_REALPATH_V1
set -eu

root="$1"
candidate="$2"
max_symlink_depth=64

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

resolved_root=$(resolve_path "$root" 0)
resolved_candidate=$(resolve_path "$candidate" 0)

case "$resolved_candidate" in
    "$resolved_root"|"$resolved_root"/*)
        printf '%s\\n' "$resolved_candidate"
        ;;
    *)
        printf 'workspace escape: %s\\n' "$resolved_candidate" >&2
        exit 111
        ;;
esac
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
    install_path: Path
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
