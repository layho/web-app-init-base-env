"""Legacy baseline produced by copy_foundation, then checked by the preflight.

The committed digests come from deploy/app-init/app_init.py at the recorded
source SHA. This is not a production install. The preflight result is not a
macOS service start, health check, or host receipt.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "tests/fixtures/legacy-generated-baseline/provenance.json"
BUSINESS = "frontend/src/modules/orders/Page.tsx"
CANDIDATE = hashlib.sha256(b"fixture-candidate-not-a-commit").hexdigest()[:40]


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = load_module(ROOT / "deploy/app-init/ownership_preflight.py", "ownership_preflight_legacy")


def provenance():
    return json.loads(PROVENANCE.read_text(encoding="utf-8"))


def generate(tmp_path, record):
    source = tmp_path / "source"
    subprocess.check_call(["git", "clone", "--local", "--quiet", str(ROOT), str(source)], timeout=60)
    subprocess.check_call(["git", "-C", str(source), "checkout", "--detach", "--quiet", record["source_sha"]], timeout=60)
    app_init = load_module(source / "deploy/app-init/app_init.py", "app_init_legacy_baseline")
    target = tmp_path / "generated"
    target.mkdir()
    params = record["parameters"]
    app_init.copy_foundation(
        source, target, params["repository"], params["title"], params["domain"], params["port"], params["label"],
    )
    files = {}
    for path in sorted(item for item in target.rglob("*") if item.is_file() and not item.is_symlink()):
        files[path.relative_to(target).as_posix()] = path.read_bytes()
    return files


def test_recorded_baseline_matches_copy_foundation(tmp_path):
    record = provenance()
    assert record["source_sha"] == "c06cecce1efbe4f51468cc069ec44a4ce367473c"
    assert record["not_a_production_install"] is True
    files = generate(tmp_path, record)
    actual = {path: hashlib.sha256(content).hexdigest() for path, content in files.items()}
    assert actual == record["files"]
    meta = json.loads(files[".app-init.json"])
    assert meta["foundation"]["sha"] == record["source_sha"]
    assert meta["foundation"]["repository"] == "layho/web-app-init-base-env"
    assert BUSINESS not in files


def test_preflight_preserves_business_module_and_rejects_unsafe_paths(tmp_path):
    record = provenance()
    files = generate(tmp_path, record)
    protected = set(record["protected_paths"])
    template = sorted(path for path in files if path not in protected)
    assert "app/main.py" in template
    policy = {
        "schema": 1,
        "origin": "trusted-caller",
        "foundation": template,
        "app": ["frontend/src/modules/orders/"],
        "protected": [],
    }
    base = {
        "baseline_sha": record["source_sha"],
        "candidate_sha": CANDIDATE,
        "policy": policy,
        "template_paths": template,
        "observed_files": [BUSINESS],
        "changes": [],
    }
    upgrade = preflight.check({
        **base,
        "kind": "foundation-upgrade",
        "changes": [{"path": "app/main.py", "op": "modify", "symlink": False}],
    })
    assert upgrade["ok"] is True
    assert upgrade["authoritative"] is False
    reasons = {item["path"]: item["reason"] for item in upgrade["decisions"]}
    assert reasons["app/main.py"] == "foundation-template-update"
    assert reasons[BUSINESS] == "not-in-diff"

    touched = preflight.check({
        **base,
        "kind": "foundation-upgrade",
        "changes": [{"path": BUSINESS, "op": "modify", "symlink": False}],
    })
    assert touched["ok"] is False
    assert {item["path"]: item["reason"] for item in touched["decisions"]}[BUSINESS] == "upgrade-touches-app"

    added = preflight.check({
        **base,
        "kind": "app-change",
        "changes": [{"path": BUSINESS, "op": "add", "symlink": False}],
    })
    assert added["ok"] is True
    assert {item["path"]: item["reason"] for item in added["decisions"]}[BUSINESS] == "app-owned-change"

    core = preflight.check({
        **base,
        "kind": "app-change",
        "changes": [{"path": "app/main.py", "op": "modify", "symlink": False}],
    })
    assert core["ok"] is False
    assert {item["path"]: item["reason"] for item in core["decisions"]}["app/main.py"] == "app-modifies-foundation"

    for change, reason in (
        ({"path": "../legacy", "op": "add", "symlink": False}, "unsafe-path"),
        ({"path": "legacy/link.py", "op": "add", "symlink": True}, "symlink"),
    ):
        report = preflight.check({**base, "kind": "app-change", "changes": [change]})
        assert report["ok"] is False
        assert report["authoritative"] is False
        assert {item["path"]: item["reason"] for item in report["decisions"]}[change["path"]] == reason
