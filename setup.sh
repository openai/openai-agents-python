#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# OpenAI Agents SDK project setup (uv edition)
# Runtimes: Python 3.12.10, Node 22.15.1, uv 0.7.5
###############################################################################

PYTHON_VERSION="3.12.10"
NODE_VERSION="22.15.1"
AGENTS_SDK_VERSION="0.0.15"
UV_VERSION="0.7.5"

echo "➤ apt-get update (with Release workaround)"
if command -v apt-get >/dev/null 2>&1; then
  set +e
  sudo apt-get update -y
  status=$?
  set -e
  if [ $status -ne 0 ]; then
    sudo apt-get update -o Acquire::AllowReleaseInfoChange::Suite=true -y
  fi
  sudo apt-get install -y --no-install-recommends        build-essential curl git zlib1g-dev libssl-dev        libreadline-dev libbz2-dev libsqlite3-dev
fi

echo "➤ pyenv"
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

echo "➤ virtualenv"
python -m venv .venv
source .venv/bin/activate

echo "➤ pip & uv $UV_VERSION"
python -m pip install --upgrade pip
python -m pip install "uv==$UV_VERSION"

echo "➤ deps via uv"
uv pip install --system "openai-agents==${AGENTS_SDK_VERSION}" ruff pytest pre-commit

echo "➤ lockfile"
uv pip compile --generate-hashes -o requirements.lock

echo "➤ nvm Node $NODE_VERSION"
export NVM_DIR="$HOME/.nvm"
if [ ! -d "$NVM_DIR" ]; then
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
fi
source "$NVM_DIR/nvm.sh"
nvm install "$NODE_VERSION" || { nvm ls-remote; nvm install "$NODE_VERSION"; }
nvm use "$NODE_VERSION"

echo "➤ pre-commit"
pre-commit install

echo "Setup complete — run: source .venv/bin/activate"
