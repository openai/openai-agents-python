#!/usr/bin/env bash
set -euo pipefail

###########################################################################
# OpenAI Agents SDK project setup – Codespaces edition
###########################################################################

PYTHON_VERSION="3.12.10"
NODE_VERSION="22.15.1"
AGENTS_SDK_VERSION="0.0.15"
UV_VERSION="0.7.5"

echo "➤ Updating apt (non-fatal)…"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y || echo "• apt update failed – continuing (Codespaces has tools pre-installed)"
  sudo apt-get install -y --no-install-recommends \
       build-essential curl git zlib1g-dev libssl-dev \
       libreadline-dev libbz2-dev libsqlite3-dev \
       || echo "• apt install skipped – packages likely present"
fi

echo "➤ Installing pyenv"
if ! command -v pyenv >/dev/null 2>&1; then
  curl -fsSL https://pyenv.run | bash
  export PATH="$HOME/.pyenv/bin:$PATH"
  eval "$(pyenv init -)" >/dev/null
else
  pyenv update
fi

echo "➤ Python $PYTHON_VERSION"
pyenv install -s "$PYTHON_VERSION" || { pyenv update && pyenv install -s "$PYTHON_VERSION"; }
pyenv local "$PYTHON_VERSION"

echo "➤ Creating virtualenv"
python -m venv .venv
source .venv/bin/activate

echo "➤ pip & uv $UV_VERSION"
python -m pip install --upgrade pip
python -m pip install "uv==$UV_VERSION"

echo "➤ Installing project deps with uv"
uv pip install --system "openai-agents==$AGENTS_SDK_VERSION" ruff pytest pre-commit

echo "➤ Lockfile"
uv pip compile --generate-hashes -o requirements.lock

echo "➤ nvm + Node $NODE_VERSION"
export NVM_DIR="$HOME/.nvm"
if [ ! -d "$NVM_DIR" ]; then
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
fi
# shellcheck source=/dev/null
source "$NVM_DIR/nvm.sh"
nvm install "$NODE_VERSION" || nvm install node --reinstall-packages-from=node
nvm use "$NODE_VERSION" || nvm use node

echo "➤ pre-commit"
pre-commit install

echo "✔️  Setup complete — run: source .venv/bin/activate"
