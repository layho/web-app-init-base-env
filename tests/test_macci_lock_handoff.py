"""Regression checks for MacCI manual test lock handoff (offline)."""
import fcntl
import importlib.util
from pathlib import Path
import os
import pytest


SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "macci_worker_lock_test", SOURCE / "deploy/macci/runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_inherited_lock_keeps_exclusivity_until_child_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    lock = tmp_path / "worker.lock"
    with lock.open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited = os.dup(owner.fileno())
        with runner.inherited_lock(inherited):
            with lock.open("a") as competing:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The web handler still owns the lock after worker's fd closes.
        with lock.open("a") as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with lock.open("a") as competing:
        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_rejects_non_worker_lock_fd(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "worker.lock").write_text("")
    other = tmp_path / "other.lock"
    other.write_text("")
    fd = os.open(other, os.O_RDWR)
    try:
        with pytest.raises(ValueError, match="Unsafe inherited"):
            with runner.inherited_lock(fd):
                pass
    finally:
        os.close(fd)


def test_rejects_invalid_file_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="Invalid inherited"):
        with runner.inherited_lock(2):
            pass
