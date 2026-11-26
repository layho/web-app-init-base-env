import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from service_recovery import recover, recovery_lock


class RecoveryTests(unittest.TestCase):
    def options(self,states):
        return {"system":lambda:"Darwin","uid":lambda:501,
                "inventory":Mock(side_effect=[[{"service_id":"macci.receipts","state":s}] for s in states]),
                "runner":Mock(return_value=0),"sleeper":lambda _:None}

    def test_preview_never_mutates(self):
        opts=self.options(["last_exit_failed"])
        self.assertEqual(recover("macci.receipts",**opts)["state"],"eligible")
        opts["runner"].assert_not_called()

    def test_fixed_target_and_postcheck(self):
        opts=self.options(["last_exit_failed","last_exit_failed","running"])
        with tempfile.TemporaryDirectory() as d:
            result=recover("macci.receipts",execute=True,root=Path(d)/"state",**opts)
        opts["runner"].assert_called_once_with("gui/501/com.layho.macci-receipt-delivery")
        self.assertEqual(result["state"],"running_observed")

    def test_running_and_race_not_restarted(self):
        for states in (["running"],["last_exit_failed","running"]):
            opts=self.options(states)
            with tempfile.TemporaryDirectory() as d:
                recover("macci.receipts",execute=True,root=Path(d)/"state",**opts)
            opts["runner"].assert_not_called()

    def test_release_coordinator_and_unknown_target_denied(self):
        for target in ("macci.publish","macci.preview","panel.merge","../../bin/sh"):
            opts=self.options([])
            with self.assertRaises(ValueError):
                recover(target,**opts)
            opts["runner"].assert_not_called()

    def test_lock_and_unconfirmed_result(self):
        opts=self.options(["last_exit_failed","last_exit_failed","unknown","unknown","unknown"])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"state"
            with recovery_lock(root):
                with self.assertRaises(ValueError):
                    recover("macci.receipts",execute=True,root=root,**opts)
            self.assertEqual(recover("macci.receipts",execute=True,root=root,**opts)["state"],"outcome_unknown")
