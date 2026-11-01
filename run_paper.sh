#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
requirements_hash=$(sha256sum requirements.txt | cut -d ' ' -f 1)
if [[ ! -f .venv/requirements.sha256 ]] || [[ "$(cat .venv/requirements.sha256)" != "$requirements_hash" ]]; then
    .venv/bin/python -m pip install -r requirements.txt
    printf '%s\n' "$requirements_hash" > .venv/requirements.sha256
fi
exec .venv/bin/python -u cascade_final4_paper.py "$@"
