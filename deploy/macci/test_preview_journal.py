import tempfile
import unittest
from pathlib import Path
from preview_journal import PreviewJournal

H,B,C=("a"*40,"b"*40,"c"*40)

class JournalTests(unittest.TestCase):
    def test_restart_reconciliation_and_no_blind_retry(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"journal.sqlite3"
            journal=PreviewJournal(path)
            journal.prepare(196,H,B,C)
            journal=PreviewJournal(path)
            self.assertFalse(journal.reconcile(lambda:B))
            with self.assertRaises(ValueError):
                journal.prepare(197,H,B,C)
            self.assertTrue(journal.reconcile(lambda:C))
            self.assertEqual(journal.history()[0]["state"],"INTEGRATED")

    def test_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            journal=PreviewJournal(Path(root)/"journal.sqlite3")
            journal.prepare(196,H,B,C)
            with self.assertRaises(ValueError):
                journal.confirm(196,H,B,C,B)
            self.assertEqual(journal.history()[0]["state"],"PENDING")
            journal.confirm(196,H,B,C,C)
            self.assertEqual(journal.history()[0]["state"],"INTEGRATED")
