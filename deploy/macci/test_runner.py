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

if __name__ == "__main__":
    unittest.main()
