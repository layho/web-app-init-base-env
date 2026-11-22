"""Installer must reject old Python before importing scheduler dependencies."""
import ast
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1] / "deploy/app-init"


def test_version_guard_precedes_scheduler_imports():
    tree = ast.parse((ROOT / "install-upgrade-scheduler.py").read_text())
    guard = next(i for i, node in enumerate(tree.body)
                 if isinstance(node, ast.If) and "sys.version_info" in ast.unparse(node.test))
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.Import) and any(n.name in {"upgrade_scheduler", "upgrade_pr"} for n in node.names):
            assert guard < i


def test_wrapper_shell_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "install-upgrade-scheduler.sh")], check=True)
