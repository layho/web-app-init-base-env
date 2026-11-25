import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import runtime_recovery as r

H,B,M,T=("a"*40,"b"*40,"c"*40,"d"*40)

class RecoveryTests(unittest.TestCase):
    def test_exact_authorization_and_revocation(self):
        pr={"state":"open","user":{"login":"layho"},"author_association":"OWNER",
            "head":{"sha":H,"repo":{"full_name":r.REPO}},
            "base":{"sha":B,"ref":"preview","repo":{"full_name":r.REPO}},
            "merge_commit_sha":M}
        approval={"schema":1,"head":H,"base":B,"merge":M,"tree":T,"decision":"approved","scope":"macci-runtime"}
        comment={"id":1,"author_association":"OWNER","user":{"login":"layho"},"body":r.MARKER+json.dumps(approval)}
        status={"context":"macmini/isolated-tests-v1","creator":{"login":"layho"},"state":"success","description":f"base={B} merge={M} passed"}
        def api(token,path):
            if path=="/pulls/9": return pr
            if path=="/branches/preview": return {"commit":{"sha":B}}
            if path=="/git/commits/"+M: return {"parents":[{"sha":B},{"sha":H}],"tree":{"sha":T}}
            if path.startswith("/commits/"): return [status]
            if path.startswith("/issues/"): return [comment]
            raise AssertionError(path)
        with patch.object(r,"api",side_effect=api):
            self.assertEqual(r.authorized("fake",9),approval)
            status["state"]="pending"
            self.assertIsNone(r.authorized("fake",9))
            status["state"]="success"
            comment["body"]=r.MARKER+json.dumps({**approval,"decision":"revoked"})
            self.assertIsNone(r.authorized("fake",9))
            comment["body"]=r.MARKER+json.dumps(approval)
            comment["user"]["login"]="outsider"
            self.assertIsNone(r.authorized("fake",9))

    def test_durable_receipt_retry_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r,"ROOT",Path(tmp)):
            root=Path(tmp)
            marker=root/"runtime-installed-sha"
            marker.write_text(M)
            marker.chmod(0o600)
            r.save_receipt(H,M)
            with patch.object(r,"api",side_effect=OSError("offline")):
                with self.assertRaises(OSError): r.flush_receipt("fake")
            self.assertTrue((root/"runtime-receipt.pending").exists())
            receipt={"context":r.CONTEXT,"state":"success","description":f"runtime={M} installed","creator":{"login":"layho"}}
            with patch.object(r,"api",return_value=[receipt]) as api:
                r.flush_receipt("fake")
                self.assertEqual(api.call_count,1)
            self.assertFalse((root/"runtime-receipt.pending").exists())

    def test_wrong_installed_sha_never_reports_success(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r,"ROOT",Path(tmp)):
            marker=Path(tmp)/"runtime-installed-sha"
            marker.write_text(B)
            marker.chmod(0o600)
            r.save_receipt(H,M)
            with patch.object(r,"api") as api:
                with self.assertRaises(ValueError): r.flush_receipt("fake")
                api.assert_not_called()
