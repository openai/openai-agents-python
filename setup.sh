#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# OpenAI Agents SDK project setup (uv edition)
# Author: Codex Scaffold Architect
# Date: 19‑May‑2025 (AEST)
# This script provisions the local dev environment. All outbound traffic occurs
# here, satisfying the “sandbox net‑access only during setup.sh” rule.
###############################################################################

# -------- Configurable versions ---------------------------------------------
PYTHON_VERSION="3.11.9"     # Matches SDK classifiers
NODE_VERSION="20.11.1"      # Active LTS at 19‑May‑2025
AGENTS_SDK_VERSION="0.0.15" # OpenAI Agents SDK
UV_VERSION="0.1.36"         # uv version pinned

echo "➤ Installing build prerequisites"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y build-essential curl git zlib1g-dev libssl-dev                           libreadline-dev libbz2-dev libsqlite3-dev
fi

echo "➤ Installing pyenv"
if ! command -v pyenv >/dev/null 2>&1; then
  curl -fsSL https://pyenv.run | bash
  export PATH="$HOME/.pyenv/bin:$PATH"
  eval "$(pyenv init -)" >/dev/null
fi

echo "➤ Installing Python $PYTHON_VERSION"
pyenv install -s "$PYTHON_VERSION"
pyenv local "$PYTHON_VERSION"

echo "➤ Creating virtual environment (.venv)"
python -m venv .venv
source .venv/bin/activate

echo "➤ Upgrading pip (bootstrap) and installing uv $UV_VERSION"
python -m pip install --upgrade pip
python -m pip install "uv==$UV_VERSION"

echo "➤ Installing project dependencies with uv (lock > sync)"
if [ ! -f pyproject.toml ] && [ ! -f requirements.txt ]; then
  echo "ERROR: No dependency manifest (pyproject.toml or requirements.txt) found."
  exit 1
fi

if [ -f pyproject.toml ]; then
  uv pip install --system -r <(uv pip compile --generate-hashes)
else
  uv pip install --system --requirement requirements.txt
fi

echo "➤ Installing OpenAI Agents SDK $AGENTS_SDK_VERSION"
uv pip install --system "openai-agents==${AGENTS_SDK_VERSION}"

echo "➤ Freezing lockfile for reproducibility"
uv pip compile --generate-hashes -o requirements.lock

echo "➤ Installing nvm for Node $NODE_VERSION"
export NVM_DIR="$HOME/.nvm"
if [ ! -d "$NVM_DIR" ]; then
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
fi
# shellcheck source=/dev/null
source "$NVM_DIR/nvm.sh"
nvm install "$NODE_VERSION"
nvm use "$NODE_VERSION"

echo "➤ Installing pre-commit hooks"
uv pip install --system pre-commit
pre-commit install

echo "➤ Setup complete. Activate with 'source .venv/bin/activate'"
