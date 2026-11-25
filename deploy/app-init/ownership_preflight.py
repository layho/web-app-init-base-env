"""Read-only ownership preflight for a caller-supplied diff.

The caller passes the trusted baseline SHA, candidate SHA, rendered template
inventory, ownership rules, and the actual change list. This module does not
read a policy out of the candidate, does not write a checkout, and does not
issue a security approval, MacCI receipt, or release authorization.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

SCHEMA = 1
STATEMENT = "预检结果不是安全批准、MacCI 回执或发布授权。"
ALWAYS_PROTECTED = ("deploy/app-init/ownership_preflight.py",)
BANNED_FOUNDATION = {
    "app", "app/", "app/*", "app/**", "app/*.py",
    "frontend", "frontend/", "frontend/src", "frontend/src/",
    "frontend/src/*", "frontend/src/**", "*", "**",
}
MAX_FILE = 8 * 1024 * 1024


def check(payload):
    """Return a deterministic preflight report. Invalid input fails closed."""
    errors = []
    if not isinstance(payload, dict):
        return _report(None, None, None, False, [], ["payload must be an object"])
    baseline = payload.get("baseline_sha")
    candidate = payload.get("candidate_sha")
    kind = payload.get("kind")
    if not _sha(baseline):
        errors.append("baseline_sha must be 40 lowercase hex characters")
    if not _sha(candidate):
        errors.append("candidate_sha must be 40 lowercase hex characters")
    if kind not in {"app-change", "foundation-upgrade"}:
        errors.append("kind must be app-change or foundation-upgrade")
    policy = payload.get("policy")
    rules, policy_errors = _policy(policy)
    errors.extend(policy_errors)
    template, template_errors = _path_list(payload.get("template_paths"), "template_paths")
    errors.extend(template_errors)
    observed, observed_errors = _path_list(payload.get("observed_files", []), "observed_files")
    errors.extend(observed_errors)
    changes = payload.get("changes")
    if not isinstance(changes, list):
        errors.append("changes must be a list")
        changes = []
    if errors:
        return _report(baseline, candidate, kind, False, [], errors)

    if changes and baseline == candidate:
        errors.append("candidate SHA is not distinct from baseline")
        return _report(baseline, candidate, kind, False, [], errors)

    decisions = []
    known = list(template) + list(observed)
    for change in changes:
        decision, fatal = _one(change, rules, set(template), kind, known)
        if fatal:
            errors.append(fatal)
            continue
        decisions.append(decision)
        known.append(decision["path"])
    changed = [item["path"] for item in decisions]
    if len(changed) != len(set(changed)):
        errors.append("duplicate change path")
    collisions = _case_collisions(known)
    if collisions:
        errors.append("case collision: " + ", ".join(collisions))
        for decision in decisions:
            if decision["path"].casefold() in {item.casefold() for item in collisions}:
                decision["verdict"] = "reject"
                decision["reason"] = "case-collision"
    for path in observed:
        if all(item["path"] != path for item in decisions):
            decisions.append({"path": path, "op": "preserve", "verdict": "preserve", "reason": "not-in-diff"})
    decisions.sort(key=lambda item: (item["path"], item["op"]))
    ok = not errors and all(item["verdict"] != "reject" for item in decisions)
    return _report(baseline, candidate, kind, ok, decisions, errors)


def describe_tree_changes(baseline, candidate):
    """Build a change list from two directories without following symlinks."""
    before = _index(Path(baseline))
    after = _index(Path(candidate))
    changes = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        marker = (old or {}).get("unsafe") or (new or {}).get("unsafe")
        if old and new:
            op = "modify"
            changed = old.get("digest") != new.get("digest") or bool(marker)
        elif new:
            op, changed = "add", True
        else:
            op, changed = "delete", True
        if changed:
            changes.append({
                "path": path,
                "op": op,
                "symlink": marker == "symlink",
                "oversized": marker == "oversized",
            })
    return changes


def _one(change, rules, template, kind, known):
    if not isinstance(change, dict):
        return None, "change must be an object"
    path = change.get("path")
    op = change.get("op")
    symlink = change.get("symlink", False)
    problem = _relative(path)
    if problem:
        return {"path": str(path), "op": str(op), "verdict": "reject", "reason": "unsafe-path"}, None
    if not isinstance(symlink, bool):
        return None, "symlink must be a boolean"
    if op not in {"add", "modify", "delete", "rename"}:
        return {"path": path, "op": str(op), "verdict": "reject", "reason": "unsafe-path"}, None
    if symlink or change.get("oversized") is True:
        reason = "symlink" if symlink else "oversized"
        return {"path": path, "op": op, "verdict": "reject", "reason": reason}, None
    if op == "delete":
        return {"path": path, "op": op, "verdict": "reject", "reason": "delete-requires-review"}, None
    if op == "rename":
        return {"path": path, "op": op, "verdict": "reject", "reason": "rename-requires-review"}, None
    if _matches(path, rules["protected"]):
        return {"path": path, "op": op, "verdict": "reject", "reason": "policy-self-edit"}, None
    owner = _owner(path, rules)
    if owner == "overlap":
        return {"path": path, "op": op, "verdict": "reject", "reason": "overlapping-ownership"}, None
    if owner == "foundation":
        if path not in template:
            return {"path": path, "op": op, "verdict": "reject", "reason": "unknown-template-path"}, None
        if kind == "app-change":
            return {"path": path, "op": op, "verdict": "reject", "reason": "app-modifies-foundation"}, None
        return {"path": path, "op": op, "verdict": "allow", "reason": "foundation-template-update"}, None
    if owner == "app":
        if kind == "foundation-upgrade":
            return {"path": path, "op": op, "verdict": "reject", "reason": "upgrade-touches-app"}, None
        return {"path": path, "op": op, "verdict": "allow", "reason": "app-owned-change"}, None
    if path in template:
        return {"path": path, "op": op, "verdict": "reject", "reason": "unknown-template-path"}, None
    if _case_collisions(known + [path]):
        return {"path": path, "op": op, "verdict": "reject", "reason": "case-collision"}, None
    if kind == "foundation-upgrade":
        return {"path": path, "op": op, "verdict": "reject", "reason": "upgrade-touches-unclaimed"}, None
    return {"path": path, "op": op, "verdict": "allow", "reason": "external-business"}, None


def _policy(policy):
    if not isinstance(policy, dict):
        return None, ["policy must be an object"]
    if policy.get("schema") != SCHEMA or policy.get("origin") != "trusted-caller":
        return None, ["policy schema must be 1 and origin must be trusted-caller"]
    errors = []
    parsed = {}
    for name in ("foundation", "app", "protected"):
        values, item_errors = _rules(policy.get(name, []), name)
        errors.extend(item_errors)
        parsed[name] = values
    parsed["protected"] = list(ALWAYS_PROTECTED) + parsed["protected"]
    if errors:
        return None, errors
    return parsed, []


def _rules(values, name):
    if not isinstance(values, list):
        return [], [name + " must be a list"]
    rules = []
    errors = []
    for value in values:
        if not isinstance(value, str) or any(char in value for char in "*?"):
            errors.append(name + " rule is not an explicit path")
            continue
        prefix = value.endswith("/")
        stem = value[:-1] if prefix else value
        if _relative(stem):
            errors.append(name + " rule is unsafe: " + value)
            continue
        stored = stem + "/" if prefix else stem
        if name == "foundation" and stored in BANNED_FOUNDATION:
            errors.append("overbroad foundation rule: " + value)
            continue
        rules.append(stored)
    return rules, errors


def _path_list(values, name):
    if not isinstance(values, list):
        return [], [name + " must be a list"]
    paths = []
    errors = []
    for value in values:
        if _relative(value):
            errors.append(name + " contains an unsafe path")
        else:
            paths.append(value)
    return paths, errors


def _owner(path, rules):
    app = _specificity(path, rules["app"])
    foundation = _specificity(path, rules["foundation"])
    if app and foundation and app == foundation:
        return "overlap"
    if app and (not foundation or app > foundation):
        return "app"
    if foundation:
        return "foundation"
    return "external"


def _specificity(path, rules):
    best = 0
    for rule in rules:
        if rule.endswith("/"):
            if path.startswith(rule):
                best = max(best, len(rule))
        elif path == rule:
            best = max(best, len(rule) + 1)
    return best


def _matches(path, rules):
    return _specificity(path, rules) > 0


def _relative(path):
    if not isinstance(path, str) or path == "" or path.startswith("/") or "\\" in path or "\x00" in path:
        return "unsafe-path"
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return "unsafe-path"
    return None


def _sha(value):
    return isinstance(value, str) and len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _case_collisions(paths):
    groups = {}
    for path in paths:
        if isinstance(path, str):
            groups.setdefault(path.casefold(), set()).add(path)
    found = []
    for names in groups.values():
        if len(names) > 1:
            found.extend(sorted(names))
    return found


def _report(baseline, candidate, kind, ok, decisions, errors):
    return {
        "schema": SCHEMA,
        "role": "preflight",
        "authoritative": False,
        "statement": STATEMENT,
        "ok": ok,
        "baseline_sha": baseline,
        "candidate_sha": candidate,
        "kind": kind,
        "decisions": decisions,
        "errors": errors,
    }


def _index(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("snapshot is not a directory")
    result = {}
    for folder, dirs, names in os.walk(root, followlinks=False):
        kept = []
        for name in list(dirs):
            directory = Path(folder) / name
            relative = directory.relative_to(root).as_posix()
            if _relative(relative):
                raise ValueError("unsafe snapshot path")
            if directory.is_symlink():
                result[relative] = {"unsafe": "symlink"}
            else:
                kept.append(name)
        dirs[:] = kept
        for name in names:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if _relative(relative):
                raise ValueError("unsafe snapshot path")
            if path.is_symlink() or not path.is_file():
                result[relative] = {"unsafe": "symlink"}
                continue
            if path.stat().st_size > MAX_FILE:
                result[relative] = {"unsafe": "oversized"}
                continue
            result[relative] = {"digest": hashlib.sha256(path.read_bytes()).hexdigest()}
    return result
