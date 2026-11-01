"""Three isolated paper accounts; remember the selected session across restarts."""

import json
import os
from pathlib import Path

ASSETS = Path(__file__).resolve().parent
ROOT = ASSETS.parent
NAMES = ("final", "final3", "final4")
LABELS = {"final": "Final", "final3": "Final-3", "final4": "Final-4"}
SELECTION_FILE = ROOT / ".runtime/dashboard-accounts.json"


def load_selection():
    if not SELECTION_FILE.exists():
        return {}
    data = json.loads(SELECTION_FILE.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("states"), dict):
        raise ValueError(
            "Invalid dashboard session; refusing to resume different accounts"
        )
    paths = [data.get("runtime")] + [data["states"].get(n) for n in NAMES]
    if not all(isinstance(p, str) and Path(p).is_absolute() for p in paths):
        raise ValueError("Saved dashboard paths must be absolute")
    return data


SAVED = load_selection()
RUNTIME = (
    Path(
        os.environ.get(
            "CASCADE_RUNTIME_DIR",
            SAVED.get("runtime", str(ROOT / ".runtime/dashboard")),
        )
    )
    .expanduser()
    .resolve()
)
SAVED_STATES = SAVED.get("states", {}) if str(RUNTIME) == SAVED.get("runtime") else {}
SOURCES = {
    "final": Path(
        os.environ.get("CASCADE_FINAL_DIR", str(ROOT / ".runtime/repos/Final"))
    )
    .expanduser()
    .resolve(),
    "final3": Path(
        os.environ.get("CASCADE_FINAL3_DIR", str(ROOT / ".runtime/repos/Final-3"))
    )
    .expanduser()
    .resolve(),
    "final4": ROOT,
}
STATE_PATHS = {
    name: Path(
        os.environ.get(
            f"CASCADE_{name.upper()}_STATE",
            SAVED_STATES.get(name, str(RUNTIME / "data" / f"{name}.state.json")),
        )
    )
    .expanduser()
    .resolve()
    for name in NAMES
}
if len(set(STATE_PATHS.values())) != len(NAMES):
    raise ValueError("Each bot must use its own state file")


def save_selection():
    SELECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "runtime": str(RUNTIME),
        "states": {n: str(p) for n, p in STATE_PATHS.items()},
    }
    temp = SELECTION_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(SELECTION_FILE)
