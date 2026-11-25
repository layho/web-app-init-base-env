import tempfile
from pathlib import Path
import subprocess
import tarfile
import unittest
from unittest.mock import patch
import runner

HEAD, BASE, MERGE = 'a'*40, 'b'*40, 'c'*40
REPO = 'layho/web-app-init-base-env'

def pr():
    return {'state': 'open', 'author_association': 'OWNER', 'number': 14, 'head': {'repo': {'full_name': REPO}, 'sha': HEAD}, 'base': {'ref': 'preview', 'sha': BASE}}

class RunnerTests(unittest.TestCase):
    def test_allowlist(self):
        self.assertTrue(runner.eligible(REPO, pr()))
        for key, value in [('author_association', 'NONE'), ('state', 'closed'), ('number', -1)]:
            candidate = pr(); candidate[key] = value
            self.assertFalse(runner.eligible(REPO, candidate))
        candidate = pr(); candidate['head']['repo']['full_name'] = 'other/repo'
        self.assertFalse(runner.eligible(REPO, candidate))
        candidate = pr(); candidate['base']['ref'] = 'main'
        self.assertFalse(runner.eligible(REPO, candidate))

    def test_merge_identity(self):
        self.assertEqual(MERGE, runner.verify_parents(f'{MERGE} {BASE} {HEAD}', HEAD, BASE))
        for data in [f'{MERGE} {HEAD} {BASE}', f'{MERGE} {BASE}', f'bad {BASE} {HEAD}']:
            with self.assertRaises(RuntimeError):
                runner.verify_parents(data, HEAD, BASE)

    def test_dedup_includes_base(self):
        status = {'context': runner.CONTEXT, 'state': 'success', 'description': runner.description(BASE, MERGE, 'passed')}
        self.assertTrue(runner.completed([status], BASE))
        self.assertFalse(runner.completed([status], HEAD))
        self.assertFalse(runner.completed([{**status, 'state': 'pending'}, status], BASE))
        self.assertLessEqual(len(runner.description(BASE, MERGE, 'dependencies-failed')), 140)

    def test_current_candidate_ignores_stale_pr_base(self):
        candidate = pr()
        candidate['merge_commit_sha'] = MERGE
        candidate['base']['sha'] = 'd'*40
        with patch.object(runner, 'api', side_effect=[
            {'commit': {'sha': BASE}}, {'parents': [{'sha': BASE}, {'sha': HEAD}]}
        ]):
            self.assertEqual(runner.current_candidate('fake', REPO, candidate), (HEAD, BASE, MERGE))
        with patch.object(runner, 'api', side_effect=[
            {'commit': {'sha': BASE}}, {'parents': [{'sha': 'd'*40}, {'sha': HEAD}]}
        ]):
            self.assertIsNone(runner.current_candidate('fake', REPO, candidate))

    def test_same_base_new_merge_requires_retest(self):
        status = {'context': runner.CONTEXT, 'state': 'success',
                  'description': runner.description(BASE, MERGE, 'passed')}
        self.assertTrue(runner.completed([status], BASE, MERGE))
        self.assertFalse(runner.completed([status], BASE, 'd'*40))
        status.update(state='error', description=runner.description(BASE, MERGE, 'ref-changed'))
        self.assertFalse(runner.completed([status], BASE, MERGE))

    def test_fetch_failure_retries_at_most_three_per_exact_candidate(self):
        def status(state='failure', result='fetch-failed', merge=MERGE):
            return {'context': runner.CONTEXT, 'state': state,
                    'description': runner.description(BASE, merge, result)}
        failure = status()
        self.assertFalse(runner.completed([failure], BASE, MERGE))
        self.assertFalse(runner.completed([failure, failure], BASE, MERGE))
        self.assertTrue(runner.completed([failure, failure, failure], BASE, MERGE))
        self.assertFalse(runner.completed([failure] * 3, BASE, 'd' * 40))
        self.assertTrue(runner.completed([status(result='pytest-failed')], BASE, MERGE))
        self.assertTrue(runner.completed([status('success', 'passed')], BASE, MERGE))
        self.assertFalse(runner.completed([status('pending'), status('success', 'passed')], BASE, MERGE))

    def test_merge_ref_fetch_retries_only_transport_failure(self):
        import io
        err = subprocess.CalledProcessError(128, ['git', 'fetch'])
        with patch.object(runner, 'run', side_effect=[err, None]) as mocked, \
             patch.object(runner.time, 'sleep') as sleeper:
            runner.fetch_merge_ref(Path('/tmp/readonly.git'), REPO, 14, io.StringIO(), {'GIT_TERMINAL_PROMPT': '0'})
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(sleeper.call_args.args, (2,))
            args = mocked.call_args.args[0]
            self.assertEqual(args[-1], 'refs/pull/14/merge')
        with patch.object(runner, 'run', side_effect=err) as mocked, \
             patch.object(runner.time, 'sleep'):
            with self.assertRaises(subprocess.CalledProcessError):
                runner.fetch_merge_ref(Path('/tmp/readonly.git'), REPO, 14, io.StringIO(), {})
            self.assertEqual(mocked.call_count, 3)

    def test_real_git_archive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)/'repo'; root.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)
            git('init', '-q')
            git('config', 'user.email', 'test@example.invalid'); git('config', 'user.name', 'test')
            for name in ['requirements.txt','frontend/package.json','frontend/package-lock.json','tests/test_example.py']:
                path = root/name; path.parent.mkdir(exist_ok=True); path.write_text('placeholder')
            git('add','.'); git('commit','-qm','fixture')
            sha = git('rev-parse','HEAD').decode().strip()
            archive=Path(d)/'source.tar'; runner.make_context(root,sha,archive)
            with tarfile.open(archive) as tf:
                self.assertNotIn('.git/config',tf.getnames())
                self.assertEqual(tf.extractfile('.macci.Dockerfile').read().decode(),runner.DOCKERFILE)
                self.assertIn('tests/test_example.py',tf.getnames())
            (root/'frontend/package-lock.json').unlink(); git('add','-u'); git('commit','-qm','missing lock')
            with self.assertRaises(RuntimeError):
                runner.make_context(root,git('rev-parse','HEAD').decode().strip(),archive)



class ReportTests(unittest.TestCase):
    def test_safe_failure_summary(self):
        raw="FAILED tests/test_demo.py::test_login - SECRET_TOKEN=ghp_secret\nFAILED /tmp/private.py::test_x - private\n"
        result=runner.failure_summary(raw,"pytest-failed")
        self.assertEqual(result,[{"file":"tests/test_demo.py","test":"test_login","category":"pytest_failure"}])
        self.assertNotIn("SECRET",str(result))
    def test_ts_error_only(self):
        result=runner.failure_summary("src/main.tsx(12,3): error TS2552: private details","frontend-failed")
        self.assertEqual(result,[{"file":"frontend/src/main.tsx","line":12,"column":3,"category":"TS2552"}])
    def test_bounded_report(self):
        raw="\n".join("FAILED tests/test_x.py::test_"+str(i) for i in range(30))
        self.assertLessEqual(len(runner.failure_summary(raw,"pytest-failed")),12)

class ProgressTests(unittest.TestCase):
    def test_progress_uses_separate_context(self):
        with patch.object(runner,"api") as api:
            progress=runner.Progress("fake",REPO,HEAD,BASE,MERGE)
            progress.update("dependencies")
            payload=api.call_args.args[2]
            self.assertEqual(payload["context"],"macmini/progress-v1")
            self.assertEqual(payload["state"],"pending")
            self.assertIn("dependencies",payload["description"])

    def test_reconcile_exact_terminal_receipt(self):
        final={"context":runner.CONTEXT,"state":"success","creator":{"login":"layho"},
               "description":runner.description(BASE,MERGE,"passed")}
        progress={"context":runner.PROGRESS_CONTEXT,"state":"pending","creator":{"login":"layho"},
                  "description":runner.description(BASE,MERGE,"test")}
        with patch.object(runner,"api") as api:
            self.assertTrue(runner.reconcile_progress("fake",REPO,HEAD,BASE,MERGE,[final,progress]))
            self.assertEqual(api.call_args.args[2]["context"],runner.PROGRESS_CONTEXT)
            self.assertEqual(api.call_args.args[2]["state"],"success")
            api.reset_mock()
            self.assertFalse(runner.reconcile_progress("fake",REPO,HEAD,BASE,MERGE,[{**final,"state":"pending"},final,progress]))
            self.assertFalse(runner.reconcile_progress("fake",REPO,HEAD,BASE,MERGE,[final,{**progress,"creator":{"login":"other"}}]))
            self.assertFalse(runner.reconcile_progress("fake",REPO,HEAD,BASE,"d"*40,[final,progress]))
            self.assertFalse(runner.reconcile_progress("fake",REPO,HEAD,BASE,MERGE,[final,{**progress,"state":"success"}]))
            api.assert_not_called()

    def test_reconcile_failure_and_transport_retry(self):
        final={"context":runner.CONTEXT,"state":"failure","creator":{"login":"layho"},
               "description":runner.description(BASE,MERGE,"pytest-failed")}
        progress={"context":runner.PROGRESS_CONTEXT,"state":"pending","creator":{"login":"layho"},
                  "description":runner.description(BASE,MERGE,"test")}
        with patch.object(runner,"api",side_effect=[OSError("offline"),None]) as api:
            self.assertFalse(runner.reconcile_progress("fake",REPO,HEAD,BASE,MERGE,[final,progress]))
            self.assertTrue(runner.reconcile_progress("fake",REPO,HEAD,BASE,MERGE,[final,progress]))
            self.assertEqual(api.call_args.args[2]["state"],"failure")

class DeploymentJournalTests(unittest.TestCase):
    def test_rollback_failure_receipt(self):
        lines=[("update_started",BASE),("update_candidate",MERGE),("update_test_build",MERGE),
               ("rollback",MERGE),("rollback_ok",BASE),("update_test_build_failed",MERGE)]
        raw="\n".join("2026-11-24T22:55:18Z\t"+code+"\t"+sha for code,sha in lines)
        result=runner.deployment_events(raw)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]["candidate"],MERGE)
        self.assertEqual(result[0]["rollback"],"ok")
        self.assertEqual(result[0]["previous"],BASE)
        self.assertEqual(runner.deployment_events(raw.split("rollback_ok")[0]),[])


class ReceiptAvailabilityTests(unittest.TestCase):
    def test_report_outbox_retries_lost_ack_without_duplicate_comment(self):
        import json
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {
                "schema": 1, "repository": REPO, "pr": 14,
                "head": HEAD, "base": BASE, "merge": MERGE,
                "run_id": "d"*32, "state": "failure", "phase": "pytest-failed",
                "started_at": 1, "finished_at": 2, "failures": [],
            }
            with patch.object(runner, "ROOT", root):
                path = runner.save_report(report)
                body = runner.REPORT_MARKER + json.dumps(report, sort_keys=True, separators=(",", ":"))
                api = Mock(side_effect=[[], OSError("lost acknowledgement"),
                                        [{"body": body, "author_association": "OWNER"}]])
                with patch.object(runner, "api", api):
                    self.assertFalse(runner.flush_reports("token"))
                    self.assertTrue(path.exists())
                    self.assertTrue(runner.flush_reports("token"))
                self.assertEqual(api.call_count, 3)
                self.assertFalse(path.exists())

    def test_failure_report_outbox_is_bounded_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = lambda run_id: {
                "schema": 1, "repository": REPO, "pr": 14,
                "head": HEAD, "base": BASE, "merge": MERGE,
                "run_id": run_id, "state": "failure", "phase": "pytest-failed",
                "started_at": 1, "finished_at": 2, "failures": [],
            }
            with patch.object(runner, "ROOT", root), \
                 patch.object(runner, "MAX_OUTBOX_REPORTS", 2), \
                 patch.object(runner, "MAX_OUTBOX_BYTES", 100000):
                first = runner.save_report(report("1"*32))
                self.assertEqual(runner.save_report(report("1"*32)), first)
                runner.save_report(report("2"*32))
                with self.assertRaisesRegex(ValueError, "capacity"):
                    runner.save_report(report("3"*32))
                self.assertEqual(sorted(p.name for p in (root/"report-outbox").glob("*.json")),
                                 ["1"*32+".json", "2"*32+".json"])
                self.assertEqual(first.stat().st_mode & 0o777, 0o600)

    def test_failure_report_outbox_byte_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = {
                "schema": 1, "repository": REPO, "pr": 14,
                "head": HEAD, "base": BASE, "merge": MERGE,
                "run_id": "a"*32, "state": "failure", "phase": "pytest-failed",
                "started_at": 1, "finished_at": 2, "failures": [],
            }
            with patch.object(runner, "ROOT", Path(tmp)), \
                 patch.object(runner, "MAX_OUTBOX_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "capacity"):
                    runner.save_report(report)
                self.assertFalse((Path(tmp)/"report-outbox"/("a"*32+".json")).exists())

    def test_permanent_report_failure_does_not_starve_later_outbox_entries(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            report=lambda index:{
                "schema":1,"repository":REPO,"pr":14,"head":HEAD,"base":BASE,"merge":MERGE,
                "run_id":f"{index:032x}","state":"failure","phase":"pytest-failed",
                "started_at":1,"finished_at":2,"failures":[],
            }
            with patch.object(runner,"ROOT",root):
                paths=[runner.save_report(report(i)) for i in range(41)]
                failed=paths[0]
                def api(_token,endpoint,data=None):
                    if endpoint.endswith("?per_page=100&page=1"):
                        return []
                    if data and failed.stem in data["body"]:
                        raise OSError("permanent report failure")
                    if data:
                        return {}
                    raise AssertionError(endpoint)
                with patch.object(runner,"api",side_effect=api):
                    self.assertFalse(runner.flush_reports("token"))
                    self.assertTrue(failed.exists())
                    self.assertFalse(runner.flush_reports("token"))
                    self.assertTrue(failed.exists())
                    self.assertFalse(runner.flush_reports("token"))
                self.assertEqual([p.stem for p in (root/"report-outbox").glob("*.json")],[failed.stem])

    def test_permanent_report_failure_does_not_starve_later_outbox_entries(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            report=lambda index:{
                "schema":1,"repository":REPO,"pr":14,"head":HEAD,"base":BASE,"merge":MERGE,
                "run_id":f"{index:032x}","state":"failure","phase":"pytest-failed",
                "started_at":1,"finished_at":2,"failures":[],
            }
            with patch.object(runner,"ROOT",root):
                paths=[runner.save_report(report(i)) for i in range(41)]
                failed=paths[0]
                def api(_token,endpoint,data=None):
                    if endpoint.endswith("?per_page=100&page=1"):
                        return []
                    if data and failed.stem in data["body"]:
                        raise OSError("permanent report failure")
                    if data:
                        return {}
                    raise AssertionError(endpoint)
                with patch.object(runner,"api",side_effect=api):
                    self.assertFalse(runner.flush_reports("token"))
                    self.assertTrue(failed.exists())
                    self.assertFalse(runner.flush_reports("token"))
                    self.assertTrue(failed.exists())
                    self.assertFalse(runner.flush_reports("token"))
                self.assertEqual([p.stem for p in (root/"report-outbox").glob("*.json")],[failed.stem])

    def test_receipts_precede_docker_failure(self):
        from unittest.mock import Mock, call
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "github-token"
            token.write_text("a" * 40)
            token.chmod(0o600)
            events = Mock()
            events.docker.side_effect = RuntimeError("unavailable")
            with patch.object(runner, "ROOT", root), \
                 patch.object(runner, "token_file", return_value=token), \
                 patch.object(runner, "lock", return_value=nullcontext()), \
                 patch.object(runner, "report_base_deployments", events.deploy), \
                 patch.object(runner, "flush_reports", events.flush), \
                 patch.object(runner, "docker_command", events.docker), \
                 patch("sys.argv", ["runner.py"]):
                self.assertEqual(runner.main(), 1)
            self.assertEqual(events.mock_calls, [call.deploy("a"*40), call.flush("a"*40), call.docker()])

    def test_receipts_only_never_accesses_docker_or_candidates(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "github-token"
            token.write_text("a"*40)
            token.chmod(0o600)
            with patch.object(runner, "ROOT", root), \
                 patch.object(runner, "token_file", return_value=token), \
                 patch.object(runner, "lock", return_value=nullcontext()), \
                 patch.object(runner, "report_base_deployments", side_effect=OSError("offline")), \
                 patch.object(runner, "flush_reports", return_value=True) as flush, \
                 patch.object(runner, "docker_command") as docker, \
                 patch.object(runner, "test_candidate") as test, \
                 patch("sys.argv", ["runner.py", "--receipts-only"]):
                self.assertEqual(runner.main(), 1)
                flush.assert_called_once_with("a"*40)
                docker.assert_not_called()
                test.assert_not_called()


    def test_receipts_only_runs_while_worker_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "github-token"
            token.write_text("a"*40)
            token.chmod(0o600)
            with patch.object(runner, "ROOT", root), \
                 patch.object(runner, "token_file", return_value=token), \
                 patch.object(runner, "report_base_deployments", return_value=True) as deploy, \
                 patch.object(runner, "flush_reports", return_value=True) as flush, \
                 patch.object(runner, "docker_command") as docker, \
                 patch("sys.argv", ["runner.py", "--receipts-only"]):
                with runner.lock():
                    self.assertEqual(runner.main(), 0)
                deploy.assert_called_once_with("a"*40)
                flush.assert_called_once_with("a"*40)
                docker.assert_not_called()

    def test_concurrent_receipt_sender_defers_without_duplicate_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "ROOT", Path(tmp)), \
                 patch.object(runner, "report_base_deployments") as deploy, \
                 patch.object(runner, "flush_reports") as flush:
                with runner.receipt_lock():
                    runner.deliver_pending_reports("fake")
                deploy.assert_not_called()
                flush.assert_not_called()
                runner.deliver_pending_reports("fake")
                deploy.assert_called_once_with("fake")
                flush.assert_called_once_with("fake")

    def test_unsafe_receipt_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "other"
            target.write_text("unchanged")
            (root / "receipt-delivery.lock").symlink_to(target)
            with patch.object(runner, "ROOT", root), \
                 patch.object(runner, "report_base_deployments") as deploy, \
                 patch.object(runner, "flush_reports") as flush:
                runner.deliver_pending_reports("fake")
                deploy.assert_not_called()
                flush.assert_not_called()
            self.assertEqual(target.read_text(), "unchanged")


    def test_delivery_status_distinguishes_failure_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "ROOT", Path(tmp)), \
                 patch.object(runner, "report_base_deployments", return_value=True), \
                 patch.object(runner, "flush_reports", side_effect=[False, True]):
                self.assertFalse(runner.deliver_pending_reports("fake"))
                self.assertTrue(runner.deliver_pending_reports("fake"))

    def test_missing_deployment_journal_is_not_delivery_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner.Path, "home", return_value=Path(tmp)), \
                 patch.object(runner, "api") as api:
                self.assertFalse(runner.report_base_deployments("fake"))
                api.assert_not_called()

    def test_deployment_receipt_failure_does_not_block_later_rows_or_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            journal=root/"web-app-init-base-env-preview"/"logs"/"operations.tsv"
            journal.parent.mkdir(parents=True)
            journal.write_text("\n".join([
                f"2026-11-25T00:00:00Z\tupdate_started\t{BASE}",
                f"2026-11-25T00:00:01Z\tupdate_candidate\t{HEAD}",
                f"2026-11-25T00:00:02Z\tupdate_preflight_failed\t{HEAD}",
                f"2026-11-25T00:00:03Z\tupdate_started\t{BASE}",
                f"2026-11-25T00:00:04Z\tupdate_candidate\t{MERGE}",
                f"2026-11-25T00:00:05Z\tupdate_preflight_failed\t{MERGE}",
            ])+"\n")
            posted=[]
            def api(token, endpoint, data=None):
                if endpoint.endswith("/statuses?per_page=100"):
                    return []
                if endpoint.endswith(f"/statuses/{HEAD}"):
                    raise OSError("first candidate status unavailable")
                if endpoint.endswith(f"/statuses/{MERGE}"):
                    posted.append(("status",MERGE))
                    return {}
                if "/issues?state=all&" in endpoint:
                    return []
                if endpoint.endswith("/issues"):
                    if data["title"].endswith(HEAD+"]"):
                        raise OSError("first candidate issue unavailable")
                    posted.append(("issue",MERGE))
                    return {}
                raise AssertionError(endpoint)
            with patch.object(runner.Path,"home",return_value=root), \
                 patch.object(runner,"api",side_effect=api):
                self.assertFalse(runner.report_base_deployments("fake"))
            self.assertEqual(posted,[("status",MERGE),("issue",MERGE)])

    def test_deployment_receipt_failure_does_not_block_later_rows_or_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            journal=root/"web-app-init-base-env-preview"/"logs"/"operations.tsv"
            journal.parent.mkdir(parents=True)
            journal.write_text("\n".join([
                f"2026-11-25T00:00:00Z\tupdate_started\t{BASE}",
                f"2026-11-25T00:00:01Z\tupdate_candidate\t{HEAD}",
                f"2026-11-25T00:00:02Z\tupdate_preflight_failed\t{HEAD}",
                f"2026-11-25T00:00:03Z\tupdate_started\t{BASE}",
                f"2026-11-25T00:00:04Z\tupdate_candidate\t{MERGE}",
                f"2026-11-25T00:00:05Z\tupdate_preflight_failed\t{MERGE}",
            ])+"\n")
            posted=[]
            def api(token, endpoint, data=None):
                if endpoint.endswith("/statuses?per_page=100"):
                    return []
                if endpoint.endswith(f"/statuses/{HEAD}"):
                    raise OSError("first candidate status unavailable")
                if endpoint.endswith(f"/statuses/{MERGE}"):
                    posted.append(("status",MERGE))
                    return {}
                if "/issues?state=all&" in endpoint:
                    return []
                if endpoint.endswith("/issues"):
                    if data["title"].endswith(HEAD+"]"):
                        raise OSError("first candidate issue unavailable")
                    posted.append(("issue",MERGE))
                    return {}
                raise AssertionError(endpoint)
            with patch.object(runner.Path,"home",return_value=root), \
                 patch.object(runner,"api",side_effect=api):
                self.assertFalse(runner.report_base_deployments("fake"))
            self.assertEqual(posted,[("status",MERGE),("issue",MERGE)])

if __name__ == "__main__":
    unittest.main()
