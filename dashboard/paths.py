"""Independent sources/accounts for Final and Final-4."""
import os
from pathlib import Path
ASSETS = Path(__file__).resolve().parent
ROOT = ASSETS.parent
RUNTIME = Path(os.environ.get('CASCADE_RUNTIME_DIR', str(ROOT/'.runtime'))).expanduser().resolve()
FINAL_SOURCE = Path(os.environ.get('CASCADE_FINAL_DIR', str(ROOT/'.runtime/repos/Final'))).expanduser().resolve()
SOURCES = {'final': FINAL_SOURCE, 'final4': ROOT}
STATE_PATHS = {'final': RUNTIME/'data/final.state.json',
               'final4': Path(os.environ.get('CASCADE_FINAL4_STATE', str(ROOT/'paper_state_final4.json'))).expanduser().resolve()}
