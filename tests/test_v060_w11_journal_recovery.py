"""Regression tests for isolated W05 durable redo recovery."""
import tempfile
import unittest
from pathlib import Path
from app.control_delivery.store import Journal


class JournalRecoveryTests(unittest.TestCase):
    def test_reopen_recovers_every_object_after_first_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.write("task", "one", {"revision": 1})
            journal.write("receipt", "one", {"revision": 1})
            def change(view):
                view.stage("task", "one", {"revision": 2})
                view.stage("receipt", "one", {"revision": 2})
            journal.crash_after_replacements = 1
            with self.assertRaisesRegex(RuntimeError, "crash during"):
                journal.mutate(change)
            self.assertTrue((Path(directory) / ".journal.pending.json").exists())
            reopened = Journal(directory)
            self.assertEqual(reopened.read("task", "one"), {"revision": 2})
            self.assertEqual(reopened.read("receipt", "one"), {"revision": 2})
            self.assertFalse((Path(directory) / ".journal.pending.json").exists())

    def test_abort_does_not_publish_redo(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            def change(view):
                view.stage("task", "one", {"revision": 1})
                view.abort()
            journal.mutate(change)
            self.assertIsNone(journal.read("task", "one"))
            self.assertFalse((Path(directory) / ".journal.pending.json").exists())

    def test_invalid_pending_blocks_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            (Path(directory) / ".journal.pending.json").write_text('{"version": 9}')
            with self.assertRaises(RuntimeError):
                journal.read("task", "one")
