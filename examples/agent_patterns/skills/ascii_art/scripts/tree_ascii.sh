#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: tree_ascii.sh <width> <height>

Generates an ASCII tree with the given width and height.
Width is normalized to an odd number for centering.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 2 ]]; then
  usage >&2
  exit 2
fi

width="$1"
height="$2"

if [[ ! "$width" =~ ^[0-9]+$ || ! "$height" =~ ^[0-9]+$ ]]; then
  echo "Width and height must be positive integers." >&2
  exit 2
fi

if (( width < 3 || height < 3 )); then
  echo "Width and height must be at least 3." >&2
  exit 2
fi

if (( width % 2 == 0 )); then
  width=$((width - 1))
  echo "Adjusted width to odd number: ${width}." >&2
fi

trunk_width=$((width / 5))
if (( trunk_width < 1 )); then
  trunk_width=1
fi
if (( trunk_width % 2 == 0 )); then
  trunk_width=$((trunk_width + 1))
fi
if (( trunk_width > width )); then
  trunk_width=$width
fi

trunk_height=$((height / 4))
if (( trunk_height < 1 )); then
  trunk_height=1
fi

for ((i = 1; i <= height; i++)); do
  if (( height == 1 )); then
    level_width=1
  else
    level_width=$((1 + (i - 1) * (width - 1) / (height - 1)))
  fi
  if (( level_width % 2 == 0 )); then
    level_width=$((level_width + 1))
  fi
  if (( level_width > width )); then
    level_width=$width
  fi
  padding=$(((width - level_width) / 2))
  printf "%*s" "$padding" ""
  printf "%*s\n" "$level_width" "" | tr " " "*"
done

for ((i = 0; i < trunk_height; i++)); do
  padding=$(((width - trunk_width) / 2))
  printf "%*s" "$padding" ""
  printf "%*s\n" "$trunk_width" "" | tr " " "|"
done
