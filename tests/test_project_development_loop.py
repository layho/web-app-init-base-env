"""Foundation-only versioned workflow documentation checks."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_development_loop_documents_order_and_cadence():
    project = json.loads((ROOT / "app/project_info.json").read_text())
    stages = project["development_loop"]
    assert [s["id"] for s in stages] == ["feedback", "development", "pr", "test", "result", "preview", "deploy", "accept", "publish"]
    assert [s["id"] for s in stages if s["mode"] == "manual"] == ["accept"]
    assert "每分钟" in stages[3]["cadence"]
    assert "每小时" in stages[5]["cadence"]
    assert "每五分钟" in stages[6]["cadence"]
    assert all(s.get("failure") for s in stages if s["id"] in {"test", "preview", "deploy", "accept", "publish"})

def test_development_loop_is_base_specific():
    source = (ROOT / "deploy/app-init/app_init.py").read_text()
    assert 'project.pop("development_loop", None)' in source
