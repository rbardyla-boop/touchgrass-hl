#!/usr/bin/env bash
# Install TOUCHGRASS-HL into a local virtualenv. Does not start trading.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found; installing uv for the current user"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
mkdir -p data logs
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi
.venv/bin/touchgrass-hl init-db
echo "Installed. Next: .venv/bin/touchgrass-hl doctor"
