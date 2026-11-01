import fcntl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.states = {n: self.root / n / "state.json" for n in prepare.NAMES}
        self.sources = {n: self.root / "sources" / n for n in prepare.NAMES}
        for name, source in self.sources.items():
            source.mkdir(parents=True)
            (source / (prepare.ENTRIES[name][0] + ".py")).touch()
            (source / "continuous_scanner.py").touch()
        for key, value in [
            ("STATE_PATHS", self.states),
            ("SOURCES", self.sources),
            ("RUNTIME", self.root / "runtime"),
        ]:
            p = patch.object(prepare, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.selection = patch.object(prepare, "save_selection")
        self.saved = self.selection.start()
        self.addCleanup(self.selection.stop)

    def test_each_source_prepared_in_an_isolated_process_under_state_locks(self):
        def run(*args, **kwargs):
            name = next(n for n, p in self.sources.items() if p == kwargs["cwd"])
            path = self.states[name]
            with path.with_name(path.name + ".lock").open("a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with patch.object(prepare.subprocess, "run", side_effect=run) as spawn:
            prepare.prepare()
        self.assertEqual(spawn.call_count, 3)
        self.assertEqual(
            {c.kwargs["cwd"] for c in spawn.call_args_list}, set(self.sources.values())
        )
        self.saved.assert_called_once()

    def test_missing_source_refused_before_any_account_changes(self):
        (self.sources["final3"] / "cascade_final3_paper.py").unlink()
        with patch.object(prepare.subprocess, "run") as spawn:
            with self.assertRaises(ValueError):
                prepare.prepare()
        spawn.assert_not_called()
        self.saved.assert_not_called()

    def test_existing_writer_blocks_all_preparation(self):
        path = self.states["final3"]
        path.parent.mkdir(parents=True)
        with path.with_name(path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(prepare.subprocess, "run") as spawn:
                with self.assertRaises(BlockingIOError):
                    prepare.prepare()
        spawn.assert_not_called()
        self.saved.assert_not_called()

    def test_failed_source_does_not_commit_new_session_selection(self):
        with patch.object(
            prepare.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, "worker"),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                prepare.prepare()
        self.saved.assert_not_called()
