import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch

from agent import run_claim
from protocol import verify_claim


class SignedClaimTests(unittest.TestCase):
    def claim(self):
        now=datetime.now(timezone.utc).replace(microsecond=0)
        token="x"*43
        stamp=lambda t:t.strftime("%Y-%m-%dT%H:%M:%SZ")
        job={"schema":2,"task_id":"a"*32,"host_id":"macmini-01","actor":"admin",
             "operation":"host.health","parameters":{},"parameter_sha256":hashlib.sha256(b"{}").hexdigest(),
             "issued_at":stamp(now),"expires_at":stamp(now+timedelta(seconds=60)),
             "nonce":"b"*32,"policy_version":"readonly-v1","lease_sha256":hashlib.sha256(token.encode()).hexdigest()}
        encoded=json.dumps(job,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")
        return {"job":job,"lease_token":token,"signature":hmac.new(b"k"*32,b"hostops/job/v2\0"+encoded,hashlib.sha256).hexdigest()}

    def test_tamper_and_replay(self):
        claim=self.claim()
        verify_claim(claim,"macmini-01",b"k"*32,datetime.now(timezone.utc))
        with self.assertRaises(ValueError):
            verify_claim(claim,"other",b"k"*32,datetime.now(timezone.utc))
        changed={**claim,"job":{**claim["job"],"operation":"agent.snapshot"}}
        with self.assertRaises(ValueError):
            verify_claim(changed,"macmini-01",b"k"*32,datetime.now(timezone.utc))
        with tempfile.TemporaryDirectory() as root, patch("agent.execute",return_value={"operation":"host.health"}) as execute:
            path=Path(root)/"ledger.sqlite3"
            self.assertEqual(run_claim(claim,"macmini-01",b"k"*32,path)["outcome"],"SUCCESS")
            with self.assertRaises(ValueError):
                run_claim(claim,"macmini-01",b"k"*32,path)
            execute.assert_called_once()
