"""Foundation and downstream-app operation guides must describe different flows."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_development_loop_documents_order_cadence_and_failure_paths():
    project = json.loads((ROOT / "app/project_info.json").read_text())
    stages = project["development_loop"]
    assert [s["id"] for s in stages] == [
        "feedback", "triage", "implementation", "review", "macci", "merge-preview",
        "preview-deploy", "web-accept", "publish-main", "upgrade-scheduler",
        "app-test-deploy", "feedback-loop"
    ]
    assert [s["id"] for s in stages if s["mode"] == "manual"] == ["web-accept"]
    by_id = {s["id"]: s for s in stages}
    for name in ["macci", "merge-preview", "publish-main"]:
        assert "每分钟" in by_id[name]["cadence"]
        assert by_id[name]["actor"] == "Mac mini"
    for name in ["preview-deploy", "upgrade-scheduler"]:
        assert "每五分钟" in by_id[name]["cadence"]
    for name in ["macci", "merge-preview", "preview-deploy", "web-accept",
                 "publish-main", "upgrade-scheduler", "app-test-deploy"]:
        assert by_id[name].get("failure")
    assert all(s["title"] and s["description"] for s in stages)


def test_foundation_guide_is_distinct_from_app_init_guide():
    project = json.loads((ROOT / "app/project_info.json").read_text())
    base_guide = project["operating_guide"]
    app_guide = project["application_guide"]
    assert base_guide["scope"] == "foundation"
    assert app_guide["scope"] == "application"
    assert len(base_guide["sections"]) >= 5
    assert {s["id"] for s in base_guide["sections"]} >= {
        "before", "initialize", "develop", "downstream", "rebuild"
    }
    for guide in [base_guide, app_guide]:
        assert all(s["steps"] and s["summary"] for s in guide["sections"])
    assert "publish-main" not in {s["id"] for s in project["app_development_loop"]}
    generator = (ROOT / "deploy/app-init/app_init.py").read_text()
    assert 'project.pop("app_development_loop", [])' in generator
    assert 'project.pop("application_guide", {})' in generator
    assert 'project.pop("development_loop", None)' not in generator
