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

        with patch.object(m, "api", side_effect=api), patch.object(m, "git") as git, patch.object(m,"authorization",return_value=None):
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
    @patch.object(m,"replacement_allowed",return_value=False)
    def test_freeze_appears_during_merge_no_push(self,replacement,frozen,auth,git,verify,api):
        self.assertFalse(m.merge_one("fake",7))
        self.assertFalse(any(call.args[1] == "push" for call in git.call_args_list))

    @patch.object(m,"git")
    @patch.object(m,"authorization",return_value=(H,B,C,B))
    @patch.object(m,"preview_frozen",return_value=True)
    @patch.object(m,"replacement_allowed",return_value=False)
    def test_frozen_candidate_without_replacement_never_pushes(self,replacement,frozen,auth,git):
        self.assertFalse(m.merge_one("fake",7))
        git.assert_not_called()

    def test_replacement_requires_exact_rejection_and_exact_repair_review(self):
        issue={"number":130,"title":f"[BASE-REJECTION {B}]","author_association":"OWNER",
               "user":{"login":m.OWNER},"body":"[BASE_REJECTION] "+json.dumps(
                   {"schema":1,"candidate_sha":B,"reason":"regression found"})}
        review={"id":1,"author_association":"OWNER","user":{"login":m.OWNER},
                "body":"[BASE_REPAIR_REVIEW] "+json.dumps({"schema":1,"head":H,
                    "rejected_sha":B,"rejection_issue":130,"decision":"approved"})}
        def api(token,path):
            return {"commit":{"sha":B if path.endswith("preview") else D}}
        with patch.object(m,"api",side_effect=api),patch.object(m,"all_pages",side_effect=lambda _,path:[review] if "comments" in path else [issue]),patch.object(m,"latest_release",side_effect=[((0,1,37),True),((0,1,38),True)]):
            self.assertTrue(m.replacement_allowed("fake",7,B,H,C))
        with patch.object(m,"api",side_effect=api),patch.object(m,"all_pages",side_effect=lambda _,path:[review] if "comments" in path else []),patch.object(m,"deployment_replacement_allowed",return_value=False):
            self.assertFalse(m.replacement_allowed("fake",7,B,H,C))


    def test_deployment_replacement_requires_owner_receipt_held_sha_and_review(self):
        import tempfile
        from pathlib import Path
        status={"context":"macmini/deployment-v1","state":"failure","creator":{"login":m.OWNER},
                "description":f"host_test_failed rollback={D} at=2026-11-24T22:55:18Z"}
        review={"id":1,"author_association":"OWNER","user":{"login":m.OWNER},
                "body":"[BASE_DEPLOY_REPAIR_REVIEW] "+json.dumps({"schema":1,"head":H,"failed_sha":B,"rollback_sha":D,"decision":"approved"})}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            checkout=root/"web-app-init-base-env-preview"
            checkout.mkdir()
            held=checkout/".webapp-failed-sha"
            held.write_text(B+"\n")
            held.chmod(0o600)
            with patch.object(m.Path,"home",return_value=root),patch.object(m,"git",return_value=D),patch.object(m,"api",return_value=[status]),patch.object(m,"all_pages",return_value=[review]),patch.object(m,"latest_release",side_effect=[((0,1,39),True),((0,1,40),True)]):
                self.assertTrue(m.deployment_replacement_allowed("fake",7,B,H,C,D))
                held.write_text(C)
                self.assertFalse(m.deployment_replacement_allowed("fake",7,B,H,C,D))
                held.write_text(B+"\\n")
                wrong_target={**status,"description":f"host_test_failed rollback={B} at=2026-11-24T22:55:18Z"}
                with patch.object(m,"api",return_value=[wrong_target]):
                    self.assertFalse(m.deployment_replacement_allowed("fake",7,B,H,C,D))
            with patch.object(m,"api",return_value=[{**status,"state":"success"}]):
                self.assertFalse(m.deployment_replacement_allowed("fake",7,B,H,C,D))


    def test_gate_diagnostics_do_not_expose_remote_payload(self):
        from io import StringIO
        from contextlib import redirect_stdout
        output = StringIO()
        with patch.object(m, 'api', return_value={**pr(), 'mergeable_state': 'secret-remote-value'}), redirect_stdout(output):
            self.assertIsNone(m.authorization('secret-token', 7))
        self.assertIn('pr_ineligible', output.getvalue())
        self.assertNotIn('secret-', output.getvalue())

    def test_gate_diagnostics_missing_review_and_test(self):
        from io import StringIO
        from contextlib import redirect_stdout
        for reviews, statuses, reason in [([], [], 'owner_review_missing_or_revoked'), ([comment()], [], 'exact_test_receipt_missing_or_invalid')]:
            output = StringIO()
            with patch.object(m, 'api', side_effect=[pr(), {'commit': {'sha': B}}, {'parents': [{'sha': B}, {'sha': H}]}, statuses]), patch.object(m, 'all_pages', return_value=reviews), redirect_stdout(output):
                self.assertIsNone(m.authorization('fake', 7))
            self.assertIn(reason, output.getvalue())


    def test_advisory_pending_requires_exact_owner_candidate(self):
        progress={"context":"macmini/progress-v1","state":"pending","creator":{"login":m.OWNER},"description":f"base={B} merge={C} test"}
        self.assertTrue(m.advisory_only([success(),progress],B,C))
        self.assertFalse(m.advisory_only([success(),{**progress,"context":"security"}],B,C))
        self.assertFalse(m.advisory_only([success(),{**progress,"description":"old"}],B,C))
        self.assertFalse(m.advisory_only([success(),{**progress,"creator":{"login":"other"}}],B,C))

    def test_unstable_exact_advisory_and_checks(self):
        candidate={**pr(),"mergeable_state":"unstable"}
        progress={"context":"macmini/progress-v1","state":"pending","creator":{"login":m.OWNER},"description":f"base={B} merge={C} test"}
        prefix=[candidate,{"commit":{"sha":B}},{"parents":[{"sha":B},{"sha":H}]},[success(),progress]]
        with patch.object(m,"all_pages",return_value=[comment()]),patch.object(m,"api",side_effect=prefix+[{"total_count":0,"check_runs":[]},{"commit":{"sha":B}}]):
            self.assertEqual(m.authorization("fake",7),(H,B,C,B))
        with patch.object(m,"all_pages",return_value=[comment()]),patch.object(m,"api",side_effect=prefix+[{"total_count":1,"check_runs":[{"status":"completed","conclusion":"failure"}]}]):
            self.assertIsNone(m.authorization("fake",7))
