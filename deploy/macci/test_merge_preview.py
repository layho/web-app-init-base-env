"""Gate tests for the local coordinator; never touch live GitHub."""
import json
import unittest
from unittest.mock import patch
import merge_preview as m

B, H, C, D = "b"*40, "a"*40, "c"*40, "d"*40

def pr():
    return {"state":"open","draft":False,"mergeable":True,"mergeable_state":"clean",
            "author_association":"OWNER","user":{"login":m.OWNER},"number":7,
            "base":{"ref":"preview","sha":B,"repo":{"full_name":m.REPO}},
            "head":{"sha":H,"repo":{"full_name":m.REPO}},"merge_commit_sha":C}

def comment(decision="approved", head=H, number=1, owner=True):
    return {"id":number,"author_association":"OWNER" if owner else "NONE",
            "user":{"login":m.OWNER if owner else "stranger"},
            "body":m.MARKER+json.dumps({"schema":1,"head":head,"decision":decision,"requires_ancestor":B})}

def success():
    return {"context":m.CONTEXT,"creator":{"login":m.OWNER},"state":"success",
            "description":f"base={B} merge={C} passed"}

class MergeTests(unittest.TestCase):
    def test_scope(self):
        p=pr()
        self.assertTrue(m.eligible(p))
        p["base"]["ref"]="main"
        self.assertFalse(m.eligible(p))
        p=pr()
        p["draft"]=True
        self.assertFalse(m.eligible(p))
        p=pr()
        p["head"]["repo"]["full_name"]="fork/repo"
        self.assertFalse(m.eligible(p))

    def test_owner_review_and_revocation(self):
        self.assertEqual(m.review([comment()],H),B)
        self.assertIsNone(m.review([comment(owner=False)],H))
        self.assertIsNone(m.review([comment(head=D)],H))
        self.assertIsNone(m.review([comment(),comment("revoked",number=2)],H))

    def test_status_gate(self):
        s=success()
        self.assertTrue(m.tested([s],H,B,C))
        self.assertFalse(m.tested([s],H,D,C))
        self.assertFalse(m.tested([s],H,B,D))
        self.assertFalse(m.tested([{**s,"state":"pending"},s],H,B,C))
        self.assertFalse(m.tested([{**s,"creator":{"login":"stranger"}}],H,B,C))

    @patch.object(m,"git")
    @patch.object(m,"authorization",return_value=None)
    def test_no_review_no_push(self,auth,git):
        self.assertFalse(m.merge_one("fake",7))
        git.assert_not_called()

    @patch.object(m,"api")
    @patch.object(m,"verify_candidate")
    @patch.object(m,"git")
    @patch.object(m,"authorization",return_value=(H,B,C,B))
    def test_exact_nonforce_preview_push(self,auth,git,verify,api):
        api.side_effect=[{"commit":{"sha":C}},{"merged":True}]
        self.assertTrue(m.merge_one("fake",7))
        pushes=[call.args[1:] for call in git.call_args_list if call.args[1]=="push"]
        self.assertEqual(pushes,[("push","--porcelain",m.REMOTE,C+":refs/heads/preview")])
        git.reset_mock()
        self.assertFalse(m.merge_one("fake",7,True))
        self.assertFalse(any(call.args[1]=="push" for call in git.call_args_list))
    
    @patch.object(m,"verify_candidate")
    @patch.object(m,"git")
    @patch.object(m,"authorization",side_effect=[(H,B,C,B),None])
    def test_changed_gate_no_push(self,auth,git,verify):
        self.assertFalse(m.merge_one("fake",7))
        self.assertFalse(any(call.args[1]=="push" for call in git.call_args_list))
