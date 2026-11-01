import fcntl
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import prepare
from migrate_state import current_settings, previous_final3_settings

class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'final4.json'
        self.patch = patch.object(prepare, 'STATE_PATHS', {'final4': self.path})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.state = dict(version=1, settings=previous_final3_settings(), balance_usdt=20.5,
            equity_usdt=20.5, free_usdt=20.5, used_usdt=0, account_positions={},
            strategy_positions={}, last_trade={}, trades=[{'id': 'old-fill'}])

    def test_idle_migration_keeps_balance_and_fills(self):
        self.path.write_text(json.dumps(self.state))
        prepare.prepare()
        self.assertEqual(json.loads(self.path.read_text()), {**self.state,'settings':current_settings()})
        self.assertEqual(len(list(self.path.parent.glob('*.bak'))), 1)
        before = self.path.read_bytes(); prepare.prepare()
        self.assertEqual(self.path.read_bytes(), before)

    def test_old_open_positions_refused_without_changes(self):
        self.state['strategy_positions'] = {'BTC': {'sl': 95}}
        self.path.write_text(json.dumps(self.state)); before = self.path.read_bytes()
        with self.assertRaises(ValueError): prepare.prepare()
        self.assertEqual(self.path.read_bytes(), before)

    def test_standalone_writer_lock_blocks_migration(self):
        self.path.write_text(json.dumps(self.state)); before = self.path.read_bytes()
        with self.path.with_name(self.path.name+'.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): prepare.prepare()
        self.assertEqual(self.path.read_bytes(), before)
