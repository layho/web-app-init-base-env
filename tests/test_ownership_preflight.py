import hashlib
import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "deploy/app-init/ownership_preflight.py"
spec = importlib.util.spec_from_file_location("ownership_preflight", SOURCE)
preflight = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = preflight
spec.loader.exec_module(preflight)

BASE = "a" * 40
NEW = "b" * 40
POLICY = {
    "schema": 1,
    "origin": "trusted-caller",
    "foundation": ["app/main.py", "requirements.txt"],
    "app": ["frontend/src/modules/a/", "frontend/src/modules/b/"],
    "protected": ["docs/APP_UPGRADE_PREFLIGHT.md"],
}
TEMPLATE = ["app/main.py", "requirements.txt"]


def payload(**extra):
    body = {
        "baseline_sha": BASE,
        "candidate_sha": NEW,
        "kind": "app-change",
        "policy": POLICY,
        "template_paths": TEMPLATE,
        "observed_files": [
            "frontend/src/modules/a/Page.tsx",
            "frontend/src/modules/b/Page.tsx",
        ],
        "changes": [],
    }
    body.update(extra)
    return body


def reasons(report):
    return {item["path"]: item["reason"] for item in report["decisions"]}


def test_external_business_file_is_preserved_and_not_authoritative():
    report = preflight.check(payload())
    assert report["ok"] is True
    assert report["authoritative"] is False
    assert report["statement"] == preflight.STATEMENT
    assert reasons(report) == {
        "frontend/src/modules/a/Page.tsx": "not-in-diff",
        "frontend/src/modules/b/Page.tsx": "not-in-diff",
    }


def test_app_change_may_add_its_module_but_not_foundation_file():
    allowed = preflight.check(payload(changes=[
        {"path": "frontend/src/modules/a/Page.tsx", "op": "modify", "symlink": False},
    ]))
    assert allowed["ok"] is True
    assert reasons(allowed)["frontend/src/modules/a/Page.tsx"] == "app-owned-change"
    blocked = preflight.check(payload(changes=[
        {"path": "app/main.py", "op": "modify", "symlink": False},
    ]))
    assert blocked["ok"] is False
    assert blocked["authoritative"] is False
    assert reasons(blocked)["app/main.py"] == "app-modifies-foundation"


def test_foundation_upgrade_keeps_both_app_modules():
    report = preflight.check(payload(
        kind="foundation-upgrade",
        changes=[{"path": "app/main.py", "op": "modify", "symlink": False}],
    ))
    assert report["ok"] is True
    assert reasons(report)["app/main.py"] == "foundation-template-update"
    assert reasons(report)["frontend/src/modules/a/Page.tsx"] == "not-in-diff"
    assert reasons(report)["frontend/src/modules/b/Page.tsx"] == "not-in-diff"
    touched = preflight.check(payload(
        kind="foundation-upgrade",
        changes=[{"path": "frontend/src/modules/a/Page.tsx", "op": "modify", "symlink": False}],
    ))
    assert touched["ok"] is False
    assert reasons(touched)["frontend/src/modules/a/Page.tsx"] == "upgrade-touches-app"


def test_unknown_template_overlap_and_unclaimed_upgrade_fail_closed():
    unknown = preflight.check(payload(
        template_paths=TEMPLATE + ["app/models.py"],
        changes=[{"path": "app/models.py", "op": "modify", "symlink": False}],
    ))
    assert unknown["ok"] is False
    assert reasons(unknown)["app/models.py"] == "unknown-template-path"
    unclaimed = preflight.check(payload(
        kind="foundation-upgrade",
        changes=[{"path": "notes/local.txt", "op": "add", "symlink": False}],
    ))
    assert unclaimed["ok"] is False
    assert reasons(unclaimed)["notes/local.txt"] == "upgrade-touches-unclaimed"
    added = preflight.check(payload(changes=[
        {"path": "notes/local.txt", "op": "add", "symlink": False},
    ]))
    assert added["ok"] is True
    assert reasons(added)["notes/local.txt"] == "external-business"


def test_unsafe_diff_and_policy_self_edit_are_rejected():
    cases = [
        ({"path": "../secrets", "op": "add", "symlink": False}, "unsafe-path"),
        ({"path": "app/link.py", "op": "add", "symlink": True}, "symlink"),
        ({"path": "app/old.py", "op": "delete", "symlink": False}, "delete-requires-review"),
        ({"path": "app/renamed.py", "op": "rename", "symlink": False}, "rename-requires-review"),
        ({"path": "deploy/app-init/ownership_preflight.py", "op": "modify", "symlink": False}, "policy-self-edit"),
        ({"path": "docs/APP_UPGRADE_PREFLIGHT.md", "op": "modify", "symlink": False}, "policy-self-edit"),
        ({"path": "notes/big.bin", "op": "add", "symlink": False, "oversized": True}, "oversized"),
    ]
    for change, reason in cases:
        report = preflight.check(payload(changes=[change]))
        assert report["ok"] is False
        assert report["authoritative"] is False
        assert reasons(report)[change["path"]] == reason


def test_policy_must_come_from_trusted_caller_and_cannot_claim_whole_trees():
    forged = preflight.check(payload(policy={**POLICY, "origin": "candidate"}))
    assert forged["ok"] is False
    assert forged["authoritative"] is False
    assert "trusted-caller" in forged["errors"][0]
    broad = preflight.check(payload(policy={**POLICY, "foundation": ["frontend/src/", "app/"]}))
    assert broad["ok"] is False
    assert any(item.startswith("overbroad foundation rule") for item in broad["errors"])
    same = preflight.check(payload(baseline_sha=BASE, candidate_sha=BASE, changes=[
        {"path": "notes/local.txt", "op": "add", "symlink": False},
    ]))
    assert same["ok"] is False
    assert same["errors"] == ["candidate SHA is not distinct from baseline"]


def test_case_collision_between_template_and_change_fails():
    report = preflight.check(payload(changes=[
        {"path": "App/Main.py", "op": "add", "symlink": False},
    ]))
    assert report["ok"] is False
    assert reasons(report)["App/Main.py"] == "case-collision"


def test_two_temporary_apps_preserve_their_own_files(tmp_path):
    template = tmp_path / "template"
    app_a = tmp_path / "app-a"
    app_b = tmp_path / "app-b"
    upgraded = tmp_path / "foundation"
    secret = tmp_path / "secret.txt"
    secret.write_text("credential\n", encoding="utf-8")
    for root, page in ((app_a, "a"), (app_b, "b")):
        module = root / "frontend/src/modules" / page
        module.mkdir(parents=True)
        (module / "Page.tsx").write_text(page + "\n", encoding="utf-8")
        link = module / "secret.txt"
        link.symlink_to(secret)
    for root in (template, app_a, app_b, upgraded):
        (root / "app").mkdir(parents=True, exist_ok=True)
        text = "base-v2\n" if root == upgraded else "base\n"
        (root / "app/main.py").write_text(text, encoding="utf-8")
        (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    upgrade = preflight.describe_tree_changes(template, upgraded)
    assert upgrade == [{"path": "app/main.py", "op": "modify", "symlink": False, "oversized": False}]
    report = preflight.check(payload(kind="foundation-upgrade", changes=upgrade))
    assert report["ok"] is True
    assert report["authoritative"] is False
    assert (app_a / "frontend/src/modules/a/Page.tsx").read_text(encoding="utf-8") == "a\n"
    assert (app_b / "frontend/src/modules/b/Page.tsx").read_text(encoding="utf-8") == "b\n"

    naive = preflight.describe_tree_changes(app_a, upgraded)
    assert any(item["path"] == "frontend/src/modules/a/Page.tsx" and item["op"] == "delete" for item in naive)
    rejected = preflight.check(payload(kind="foundation-upgrade", changes=naive))
    assert rejected["ok"] is False
    assert reasons(rejected)["frontend/src/modules/a/Page.tsx"] == "delete-requires-review"
    assert (app_a / "frontend/src/modules/a/Page.tsx").read_text(encoding="utf-8") == "a\n"

    linked = preflight.describe_tree_changes(template, app_a)
    assert any(item["path"] == "frontend/src/modules/a/secret.txt" and item["symlink"] is True for item in linked)
    assert secret.read_text(encoding="utf-8") == "credential\n"
    assert preflight.check(payload(changes=linked))["ok"] is False
    digest = hashlib.sha256(secret.read_bytes()).hexdigest()
    assert digest not in repr(linked)
