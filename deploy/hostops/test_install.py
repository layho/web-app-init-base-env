import hashlib
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path

from install import MODULES, LABEL, stage, activate, launch_spec


class InstallTests(unittest.TestCase):
    def test_verified_stage_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"state"
            source=Path(tmp)/"source"
            source.mkdir()
            data=b"VALUE=1\n"
            for name in MODULES:
                (source/name).write_bytes(data)
            manifest={name:hashlib.sha256(data).hexdigest() for name in MODULES}
            digest=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(",",":")).encode()).hexdigest()
            target=stage(root,source,digest)
            self.assertEqual(stage(root,source,digest),target)
            (source/"agent.py").write_text("VALUE=2")
            with self.assertRaises(ValueError):
                stage(root,source,digest)
            self.assertEqual((target/"agent.py").read_bytes(),data)

    def test_config_contains_paths_not_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            root.chmod(0o700)
            for name,value in (("agent.token","a"*40),("job.key","b"*64)):
                path=root/name
                path.write_text(value)
                path.chmod(0o600)
            spec=launch_spec(root,root,"macmini-01","https://base.example",Path(os.sys.executable),True)
            raw=plistlib.dumps(spec)
            self.assertNotIn(b"a"*40,raw)
            self.assertNotIn(b"b"*64,raw)
            self.assertEqual(spec["Label"],LABEL)
            self.assertTrue(spec["RunAtLoad"])

    def test_activation_restores_previous_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"agent.plist"
            old=plistlib.dumps({"Label":LABEL,"ProgramArguments":["old"]})
            path.write_bytes(old)
            path.chmod(0o600)
            loaded=True
            bootstraps=0
            def runner(op,*args):
                nonlocal loaded,bootstraps
                if op=="print":return 0 if loaded else 1
                if op=="bootout":loaded=False;return 0
                if op=="bootstrap":
                    bootstraps+=1
                    loaded=bootstraps>1
                    return 0 if loaded else 1
                raise AssertionError(op)
            with self.assertRaises(RuntimeError):
                activate(path,{"Label":LABEL,"ProgramArguments":["new"]},runner)
            self.assertEqual(path.read_bytes(),old)
            self.assertTrue(loaded)


    def test_installation_lock_excludes_concurrent_operator(self):
        from install import installation_lock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"state"
            with installation_lock(root):
                with self.assertRaises(RuntimeError):
                    with installation_lock(root):
                        self.fail("concurrent install allowed")
            with installation_lock(root):
                pass
            self.assertTrue((root/"install.lock").is_file())
