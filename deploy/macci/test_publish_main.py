import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import publish_main as p

A, B, H, T, E, M = [c*40 for c in "abcdef"]
DATA = {"schema":1,"candidate_sha":A,"expected_main_sha":B,"version":"0.1.2",
        "test_head":H,"test_base":T,"tested_merge":E,"tree":M}

def issue(data=DATA):
    return {"title":f"[BASE-ACCEPTANCE {A}]","state":"open","number":100,
            "author_association":"OWNER","user":{"login":"layho"},
            "body":p.MARKER+json.dumps(data)}

class PublishTests(unittest.TestCase):
    def test_owner_exact_issue(self):
        self.assertEqual(p.parse(issue()),DATA)
        self.assertIsNone(p.parse({**issue(),"author_association":"NONE"}))
        self.assertIsNone(p.parse({**issue(),"title":"ordinary feedback"}))
        self.assertIsNone(p.parse(issue({**DATA,"candidate_sha":B})))
        self.assertIsNone(p.parse(issue({**DATA,"extra":"unexpected"})))

    def test_local_receipt(self):
        with TemporaryDirectory() as root, patch.object(p,"RECEIPTS",Path(root)):
            path=Path(root)/(A+".json")
            path.write_text('{"issue":100}')
            path.chmod(0o600)
            self.assertTrue(p.local_receipt(A,100))
            self.assertFalse(p.local_receipt(A,101))
            path.chmod(0o644)
            self.assertFalse(p.local_receipt(A,100))
            path.unlink()
            path.symlink_to("/dev/null")
            self.assertFalse(p.local_receipt(A,100))

    @patch.object(p.m,"git")
    @patch.object(p,"accepted",return_value=None)
    def test_missing_receipt_never_pushes(self,accepted,git):
        self.assertFalse(p.publish_one("fake",100))
        git.assert_not_called()

    def run_scenario(self, main=B, dry=False, evidence=True, changed=False):
        preview="1"*40
        git_calls=[]
        def git(folder,*args):
            git_calls.append(args)
            if args[:2]==("rev-parse","refs/macci/main"): return main
            if args[:2]==("rev-parse","refs/macci/preview"): return preview
            if args[:3]==("show","-s","--format=%T"): return M
            if args[0]=="show": return '[{"version":"0.1.2","acceptance_required":true}]'
            return ""
        refs={"main":main,"preview":preview}
        def ref(token,name):
            if name=="main" and any(c[0]=="push" for c in git_calls): return A
            return refs[name]
        def ancestor(folder,older,newer): return (older,newer) != (A,main)
        with patch.object(p,"accepted",side_effect=[DATA,None if changed else DATA]), \
             patch.object(p,"ref",side_effect=ref),patch.object(p,"ancestor",side_effect=ancestor), \
             patch.object(p,"evidence",return_value=evidence),patch.object(p.m,"git",side_effect=git), \
             patch.object(p,"acknowledge") as ack:
            result=p.publish_one("fake",100,dry)
            return result,[c for c in git_calls if c[0]=="push"],ack.call_count

    def test_exact_accepted_sha_even_when_preview_newer(self):
        result,pushes,acks=self.run_scenario()
        self.assertTrue(result)
        self.assertEqual(pushes,[("push","--porcelain",p.m.REMOTE,A+":refs/heads/main")])
        self.assertEqual(acks,1)

    def test_blocked_or_dry_run_never_pushes(self):
        for kwargs in [{"main":H},{"dry":True},{"evidence":False},{"changed":True}]:
            result,pushes,acks=self.run_scenario(**kwargs)
            self.assertFalse(result)
            self.assertEqual(pushes,[])
            self.assertEqual(acks,0)

    @patch.object(p.m,"api")
    @patch.object(p.m,"all_pages")
    def test_evidence_must_match_tree_and_merged_pr(self,pages,api):
        commit={"parents":[{"sha":T},{"sha":H}],"tree":{"sha":M}}
        statuses=[{"context":p.m.CONTEXT,"creator":{"login":"layho"},"state":"success",
                   "description":f"base={T} merge={E} passed"}]
        pages.return_value=[{"merged_at":"2026-11-22","merge_commit_sha":A,
            "base":{"ref":"preview","repo":{"full_name":p.m.REPO}},"head":{"sha":H}}]
        api.side_effect=[commit,commit,statuses]
        self.assertTrue(p.evidence("fake",DATA))
        api.side_effect=[commit,{**commit,"tree":{"sha":B}}]
        self.assertFalse(p.evidence("fake",DATA))

if __name__=="__main__":
    unittest.main()
