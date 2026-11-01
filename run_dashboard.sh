#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$repo_dir"
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required; use Ubuntu in Termux"'
runtime_record="$repo_dir/.runtime/account-location"
if [[ -z "${CASCADE_RUNTIME_DIR:-}" && -f "$runtime_record" ]]; then
 IFS= read -r CASCADE_RUNTIME_DIR < "$runtime_record"
fi
export CASCADE_RUNTIME_DIR="${CASCADE_RUNTIME_DIR:-$repo_dir/.runtime}"
export CASCADE_FINAL_DIR="${CASCADE_FINAL_DIR:-$repo_dir/.runtime/repos/Final}"
export CASCADE_FINAL4_STATE="${CASCADE_FINAL4_STATE:-$repo_dir/paper_state_final4.json}"
python3 - <<'PY'
import fcntl, sys
sys.path.insert(0,'dashboard')
from paths import RUNTIME, STATE_PATHS
(RUNTIME/'data').mkdir(parents=True, exist_ok=True)
paths = [RUNTIME/'data'/(n+'.lock') for n in ('server','final','final2','final4')]
p = STATE_PATHS['final4']; p.parent.mkdir(parents=True, exist_ok=True)
paths.append(p.with_name(p.name+'.lock'))
for path in paths:
 with path.open('a') as f:
  try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError: raise SystemExit('An old dashboard or bot is running. Stop it with Ctrl+C first.')
print('Final account:', STATE_PATHS['final'])
print('Final-4 account:', STATE_PATHS['final4'])
PY
if [[ ! -d "$CASCADE_FINAL_DIR/.git" ]]; then
 if [[ -e "$CASCADE_FINAL_DIR" ]]; then
  echo 'Final source path exists but is not a Git checkout. Choose another CASCADE_FINAL_DIR.' >&2; exit 1
 fi
 mkdir -p -- "$(dirname -- "$CASCADE_FINAL_DIR")"
 git clone --depth 1 --branch main https://github.com/khaledhasneen1993/Final.git "$CASCADE_FINAL_DIR"
fi
if [[ ! -f "$CASCADE_FINAL_DIR/binance_cascade_hunter_dynamic_v2_paper.py" ]]; then
 echo 'Final paper entry point is missing.' >&2; exit 1
fi
if [[ ! -x .venv-dashboard/bin/python ]]; then
 python3 -m venv .venv-dashboard
fi
expected_hash="$(sha256sum dashboard/requirements.txt | cut -d ' ' -f 1)"
installed_hash="$(cat .venv-dashboard/requirements.sha256 2>/dev/null || true)"
if [[ "$installed_hash" != "$expected_hash" ]]; then
 .venv-dashboard/bin/python -m pip install -r dashboard/requirements.txt
 printf '%s\n' "$expected_hash" > .venv-dashboard/requirements.sha256
fi
.venv-dashboard/bin/python dashboard/prepare.py
mkdir -p .runtime
printf '%s\n' "$CASCADE_RUNTIME_DIR" > "$runtime_record"
printf '\nFinal + Final-4 dashboard: http://127.0.0.1:8787\nCtrl+C stops both bots and saves their state.\n\n'
exec .venv-dashboard/bin/python -u dashboard/server.py "$@"
