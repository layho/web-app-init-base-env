import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "deploy/app-init/legacy_isolation.py"
spec = importlib.util.spec_from_file_location("legacy_isolation", SOURCE)
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)

PRESERVE = {
    "app/config/brand.json",
    "frontend/src/modules/orders/",
}
OLD = {
    "app/main.py": b"foundation-v1\n",
    "requirements.txt": b"fastapi\n",
    "app/config/brand.json": b'{"name":"fixture-brand"}\n',
    "frontend/src/modules/orders/Page.tsx": b"export const page = \"orders\"\n",
}


def test_upgrade_preserves_business_files_and_config():
    planned = fixture.plan_upgrade(OLD, {"app/main.py": b"foundation-v2\n"}, PRESERVE)
    assert planned["ok"] is True
    assert planned["authoritative"] is False
    assert planned["files"]["app/main.py"] == b"foundation-v2\n"
    assert planned["files"]["app/config/brand.json"] == OLD["app/config/brand.json"]
    assert planned["files"]["frontend/src/modules/orders/Page.tsx"] == OLD["frontend/src/modules/orders/Page.tsx"]
    assert "macos-service-start" in planned["does_not_prove"]


def test_upgrade_overlapping_business_file_does_not_change_tree():
    refused = fixture.plan_upgrade(OLD, {"app/config/brand.json": b'{"name":"replaced"}\n'}, PRESERVE)
    assert refused["ok"] is False
    assert refused["files"] == OLD
    assert refused["digest"] is None
    assert refused["rejected"] == [{"path": "app/config/brand.json", "reason": "business-file-preserved"}]
    traversed = fixture.plan_upgrade(OLD, {"../brand.json": b"x"}, PRESERVE)
    assert traversed["ok"] is False
    assert traversed["files"] == OLD


def test_injected_start_failure_rolls_back_to_old_bytes(tmp_path):
    source = tmp_path / "old-install"
    source.mkdir()
    (source / "app/config").mkdir(parents=True)
    (source / "frontend/src/modules/orders").mkdir(parents=True)
    for path, content in OLD.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    loaded = fixture.load_tree(source)
    recorded = fixture.record(loaded, "fixture-old-install")
    planned = fixture.plan_upgrade(loaded, {"app/main.py": b"foundation-v2\n"}, PRESERVE)
    start = fixture.note_start("fail")
    assert start["observed_process"] is False
    assert start["running"] is False
    assert start["reason"] == "start-failed"
    restored = fixture.rollback(recorded)
    assert restored["ok"] is True
    assert restored["files"] == loaded
    assert restored["digest"] == recorded["digest"]
    assert restored["files"] != planned["files"]
    written = fixture.materialize(restored["files"], tmp_path / "restored")
    assert (written / "app/main.py").read_bytes() == b"foundation-v1\n"
    assert (written / "app/config/brand.json").read_bytes() == OLD["app/config/brand.json"]


def test_tampered_rollback_record_and_symlink_fail_closed(tmp_path):
    recorded = fixture.record(OLD, "fixture-old-install")
    recorded["files"] = dict(recorded["files"])
    recorded["files"]["app/main.py"] = b"tampered\n"
    refused = fixture.rollback(recorded)
    assert refused["ok"] is False
    assert refused["files"] == {}
    assert refused["reason"] == "rollback-refused"
    root = tmp_path / "linked"
    root.mkdir()
    (root / "secret.txt").symlink_to(tmp_path / "outside")
    try:
        fixture.load_tree(root)
    except ValueError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("symlink fixture was accepted")


def test_two_fixture_apps_stay_independent_and_receipt_stays_empty(tmp_path):
    app_a = dict(OLD)
    app_b = dict(OLD)
    app_b["app/config/brand.json"] = b'{"name":"fixture-b"}\n'
    app_b["frontend/src/modules/orders/Page.tsx"] = b"export const page = \"b\"\n"
    upgraded = fixture.plan_upgrade(app_a, {"app/main.py": b"foundation-v2\n"}, PRESERVE)
    assert app_b["app/config/brand.json"] == b'{"name":"fixture-b"}\n'
    assert upgraded["files"]["frontend/src/modules/orders/Page.tsx"] == app_a["frontend/src/modules/orders/Page.tsx"]
    gap = fixture.receipt_gap()
    assert gap["authoritative"] is False
    assert all(value is None for value in gap["fields"].values())
    assert set(gap["fields"]) == set(fixture.MAC_RECEIPT_FIELDS)
    left = fixture.materialize(app_a, tmp_path / "a")
    right = fixture.materialize(app_b, tmp_path / "b")
    assert (left / "app/config/brand.json").read_bytes() != (right / "app/config/brand.json").read_bytes()
