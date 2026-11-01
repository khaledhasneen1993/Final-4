"""Prepare each upstream account in a separate process without fetching market data."""

import fcntl
import os
import subprocess
import sys
from contextlib import ExitStack
from paths import NAMES, LABELS, RUNTIME, SOURCES, STATE_PATHS, save_selection

ENTRIES = {
    "final": ("binance_cascade_hunter_dynamic_v2_paper", "V2PaperHunter"),
    "final3": ("cascade_final3_paper", "Final3PaperHunter"),
    "final4": ("cascade_final4_paper", "Final4PaperHunter"),
}


def prepare():
    with ExitStack() as stack:
        (RUNTIME / "data").mkdir(parents=True, exist_ok=True)
        locks = [RUNTIME / "data" / f"{n}.lock" for n in ("server", *NAMES)]
        locks += [p.with_name(p.name + ".lock") for p in STATE_PATHS.values()]
        for path in locks:
            path.parent.mkdir(parents=True, exist_ok=True)
            f = stack.enter_context(path.open("a"))
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Validate every source before creating or migrating any account.
        for name in NAMES:
            module, _ = ENTRIES[name]
            for file in (module + ".py", "continuous_scanner.py"):
                if not (SOURCES[name] / file).is_file():
                    raise ValueError(f"Missing {LABELS[name]} source: {file}")
        for name in NAMES:
            module, cls = ENTRIES[name]
            code = f"from {module} import {cls}; import sys; bot={cls}(state_path=sys.argv[1]); bot.save(); print('wallet=', bot.exchange.balance, '| positions=', len(bot.positions), '| fills=', len(bot.exchange.trades))"
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            print(f"{LABELS[name]} account: {STATE_PATHS[name]}", flush=True)
            subprocess.run(
                [sys.executable, "-c", code, str(STATE_PATHS[name])],
                cwd=SOURCES[name],
                env=env,
                check=True,
            )
        save_selection()


if __name__ == "__main__":
    prepare()
