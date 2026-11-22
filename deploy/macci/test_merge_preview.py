"""Gate tests for the local coordinator; never touch live GitHub."""
import json
import base64
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
    def test_freeze_when_preview_release_is_ahead_of_main(self):
        def api(token, path):
            if path == "/branches/preview":
                return {"commit": {"sha": C}}
            if path == "/branches/main":
                return {"commit": {"sha": B}}
            if path.startswith("/contents/app/release_notes.json?ref="):
                version = "0.1.18" if path.endswith(C) else "0.1.17"
                return {"encoding": "base64", "content": base64.b64encode(
                    json.dumps([{"version": version, "acceptance_required": True}]).encode()).decode()}
            raise AssertionError(path)
        with patch.object(m, "api", side_effect=api):
            self.assertTrue(m.preview_frozen("fake"))

    def test_unfreeze_after_main_catches_up(self):
        with patch.object(m, "api") as api:
            def data(token, path):
                if path.startswith("/branches/"):
                    return {"commit": {"sha": C}}
                return {"encoding": "base64", "content": base64.b64encode(
                    b'[{"version":"0.1.18","acceptance_required":true}]').decode()}
            api.side_effect = data
            self.assertFalse(m.preview_frozen("fake"))

    def test_amended_same_version_candidate_blocks_push_until_published(self):
        old = {"version": "0.1.18", "acceptance_required": True, "changes": ["initial"]}
        amended = {**old, "changes": ["initial", "freeze coordinator"]}
        refs = {"main": B, "preview": C}

        def api(token, path):
            if path.startswith("/branches/"):
                return {"commit": {"sha": refs[path.split("/")[-1]]}}
            note = {B: old, C: amended}[path.split("?ref=")[1]]
            content = base64.b64encode(json.dumps([note]).encode()).decode()
            return {"encoding": "base64", "content": content}

        with patch.object(m, "api", side_effect=api), patch.object(m, "git") as git:
            self.assertFalse(m.merge_one("fake", 54))
            git.assert_not_called()
            amended["changes"] = old["changes"]
            self.assertTrue(m.preview_frozen("fake"))
            refs["main"] = C
            self.assertFalse(m.preview_frozen("fake"))

    def test_freeze_fails_closed_on_unreachable_release_data(self):
        with patch.object(m, "api", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                m.preview_frozen("fake")


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

    @patch.object(m,"all_pages",return_value=[comment()])
    @patch.object(m,"api")
    def test_actual_preview_over_stale_pr_base(self,api,pages):
        candidate=pr()
        candidate["base"]["sha"]=D
        api.side_effect=[candidate,{"commit":{"sha":B}},
                         {"parents":[{"sha":B},{"sha":H}]},[success()],{"commit":{"sha":B}}]
        self.assertEqual(m.authorization("fake",7),(H,B,C,B))
        api.side_effect=[candidate,{"commit":{"sha":B}},{"parents":[{"sha":D},{"sha":H}]}]
        self.assertIsNone(m.authorization("fake",7))

    @patch.object(m,"preview_frozen",return_value=False)
    @patch.object(m,"git")
    @patch.object(m,"authorization",return_value=None)
    def test_no_review_no_push(self,auth,git,frozen):
        self.assertFalse(m.merge_one("fake",7))
        git.assert_not_called()

    @patch.object(m,"preview_frozen",return_value=False)
    @patch.object(m,"api")
    @patch.object(m,"verify_candidate")
    @patch.object(m,"git")
    @patch.object(m,"authorization",return_value=(H,B,C,B))
    def test_exact_nonforce_preview_push(self,auth,git,verify,api,frozen):
        api.side_effect=[{"commit":{"sha":C}},{"merged":True}]
        self.assertTrue(m.merge_one("fake",7))
        pushes=[call.args[1:] for call in git.call_args_list if call.args[1]=="push"]
        self.assertEqual(pushes,[("push","--porcelain",m.REMOTE,C+":refs/heads/preview")])
        git.reset_mock()
        self.assertFalse(m.merge_one("fake",7,True))
        self.assertFalse(any(call.args[1]=="push" for call in git.call_args_list))
    
    @patch.object(m,"preview_frozen",return_value=False)
    @patch.object(m,"verify_candidate")
    @patch.object(m,"git")
    @patch.object(m,"authorization",side_effect=[(H,B,C,B),None])
    def test_changed_gate_no_push(self,auth,git,verify,frozen):
        self.assertFalse(m.merge_one("fake",7))
        self.assertFalse(any(call.args[1]=="push" for call in git.call_args_list))
    
    @patch.object(m,"api")
    @patch.object(m,"verify_candidate")
    @patch.object(m,"git")
    @patch.object(m,"authorization",return_value=(H,B,C,B))
    @patch.object(m,"preview_frozen",side_effect=[False,True])
    def test_freeze_appears_during_merge_no_push(self,frozen,auth,git,verify,api):
        self.assertFalse(m.merge_one("fake",7))
        self.assertFalse(any(call.args[1] == "push" for call in git.call_args_list))

    @patch.object(m,"git")
    @patch.object(m,"authorization")
    @patch.object(m,"preview_frozen",return_value=True)
    def test_frozen_candidate_never_calls_authorization_or_git(self,frozen,auth,git):
        self.assertFalse(m.merge_one("fake",7))
        auth.assert_not_called()
        git.assert_not_called()
