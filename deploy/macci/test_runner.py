import tempfile
from pathlib import Path
import subprocess
import tarfile
import unittest
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

if __name__ == '__main__':
    unittest.main()
