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


class ManagedStageTests(unittest.TestCase):
    def test_stage_registers_and_prepares_in_one_transaction(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"ledger.sqlite3"
            journal=PreviewJournal(path)
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE integration_version(singleton INTEGER PRIMARY KEY,version TEXT)")
                db.execute("INSERT INTO integration_version VALUES(1,'0.5.0')")
                db.execute("CREATE TABLE integration_jobs(id TEXT PRIMARY KEY,version TEXT,head TEXT,dependencies TEXT,files TEXT,priority INTEGER,state TEXT,base TEXT,lease TEXT,expires INTEGER,created INTEGER)")
                db.execute("CREATE TABLE integration_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT,event TEXT,at INTEGER)")
            self.assertEqual(journal.stage(196,H,B,["app/core.py"]),"pr-196")
            self.assertEqual(journal.stage(196,H,B,["app/core.py"]),"pr-196")
            with self.assertRaises(ValueError):
                journal.stage(197,H,B,["app/other.py"])
            self.assertFalse(journal.pending())
            new_base="d"*40
            self.assertEqual(journal.stage(196,H,new_base,["app/core.py"]),"pr-196")
            journal.prepare(196,H,new_base,C,changed_files=["app/core.py"])
            self.assertTrue(journal.pending())
            self.assertFalse(journal.reconcile(lambda:new_base))
            self.assertTrue(journal.reconcile(lambda:C))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT state FROM integration_jobs").fetchone()[0],"INTEGRATED")
                self.assertEqual([r[0] for r in db.execute("SELECT event FROM integration_events ORDER BY seq")],["QUEUED","PREPARED","PREPARATION_RETRY","PREPARED","PUSH_STARTED","INTEGRATED"])


class RejectionTests(unittest.TestCase):
    def test_definitive_rejection_releases_pending_only_at_original_base(self):
        with tempfile.TemporaryDirectory() as root:
            journal=PreviewJournal(Path(root)/"journal.sqlite3")
            journal.prepare(196,H,B,C)
            self.assertFalse(journal.reject_without_push(196,H,B,C,"d"*40))
            self.assertFalse(journal.reconcile(lambda:B))
            self.assertTrue(journal.reject_without_push(196,H,B,C,B))
            self.assertEqual(journal.history()[0]["state"],"REJECTED")
            journal.prepare(196,H,B,C)

    def test_porcelain_rejection_is_narrow(self):
        import subprocess
        from merge_preview import definitive_push_rejection
        def error(output):
            return subprocess.CalledProcessError(1,["git"],output=output)
        self.assertTrue(definitive_push_rejection(error("!\t"+C+":refs/heads/preview\t[remote rejected] (pre-receive hook declined)")))
        for output in ("fatal: connection reset","!\t"+C+":refs/heads/main\t[rejected] (fetch first)","!\t"+C+":refs/heads/preview\t[remote failure] (failed to report status)"):
            self.assertFalse(definitive_push_rejection(error(output)))
        self.assertFalse(definitive_push_rejection(subprocess.TimeoutExpired(["git"],90)))


class PreparedCancellationTests(unittest.TestCase):
    def test_closed_candidate_releases_prepared_reservation_only(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"ledger.sqlite3"
            journal=PreviewJournal(path)
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE integration_jobs(id TEXT PRIMARY KEY,head TEXT,base TEXT,state TEXT,lease TEXT,expires INTEGER)")
                db.execute("CREATE TABLE integration_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT,event TEXT,at INTEGER)")
                db.execute("INSERT INTO integration_jobs VALUES('pr-196',?,?,'PREPARED',NULL,NULL)",(H,B))
            self.assertEqual(journal.prepared(),{"id":"pr-196","head":H,"base":B})
            self.assertFalse(journal.cancel_prepared("pr-196",H,C))
            journal.prepare(197,H,B,C)
            self.assertFalse(journal.cancel_prepared("pr-196",H,B))
            self.assertTrue(journal.pending())
            with sqlite3.connect(path) as db:
                db.execute("DELETE FROM preview_attempts")
            self.assertTrue(journal.cancel_prepared("pr-196",H,B))
            self.assertIsNone(journal.prepared())
