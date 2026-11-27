"""Isolated tests for the fixed installer's three-module digest gate."""
import hashlib
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location("runtime_installer",Path(__file__).with_name("upgrade-runtime.py"))
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class RuntimeDigestTests(unittest.TestCase):
    def test_every_module_requires_independent_digest(self):
        modules={name:("reviewed "+name).encode() for name in installer.NAMES}
        pins=[hashlib.sha256(modules[name]).hexdigest() for name in installer.NAMES]
        installer.verify_reviewed_digests(modules,*pins)
        for index in range(len(pins)):
            invalid=pins.copy()
            invalid[index]=None
            with self.assertRaises(ValueError):
                installer.verify_reviewed_digests(modules,*invalid)
        modules["preview_journal.py"]=b"changed"
        with self.assertRaises(ValueError):
            installer.verify_reviewed_digests(modules,*pins)
