"""Regression: GitHub feedback issues must contain readable Markdown paragraphs."""
import ast
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "sync_feedback.py"

def test_feedback_issue_body_has_real_newlines():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "issue_body")
    isolated = ast.Module(body=[fn], type_ignores=[])
    namespace = {}
    exec(compile(isolated, str(SOURCE), "exec"), namespace)
    issue = SimpleNamespace(id=17, category="bug", message="Inventory list is empty")
    body = namespace["issue_body"](issue)
    assert "admin feedback\\n" not in body
    assert "admin feedback\nInternal feedback ID: 17\n" in body
    assert "Category: bug\n\nInventory list is empty\n\n" in body
    assert "Do not treat this as tested/verified" in body
