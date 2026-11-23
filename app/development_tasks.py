"""Local snapshot of GitHub Issues explicitly tracked as development tasks."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

CACHE_PATH = Path(__file__).resolve().parents[1] / "logs" / "development-tasks.json"
REFRESH_INTERVAL = timedelta(minutes=5)


def parse_issue(issue: dict, repository: str, comments: list[dict] | None = None) -> dict | None:
    if "pull_request" in issue:
        return None
    body = issue.get("body") or ""
    section = re.search(r"^## 开发进度[ \t]*\r?\n([\s\S]*?)(?=^#{1,2} |\Z)", body, re.MULTILINE)
    # All genuine issues are visible in the task list. The structured
    # progress block is optional; absent evidence must not imply development.
    progress = section[1] if section else ""
    status = re.search(r"^状态[：:]\s*(待开发|开发中|待验收|受阻|已完成)[ \t]*\r?$", progress, re.MULTILINE)
    note = re.search(r"^更新说明[：:][ \t]*([^\r\n]+)", progress, re.MULTILINE)
    checks = re.findall(r"^[ \t]*[-*] \[([ xX])\][ \t]+\S", progress, re.MULTILINE)
    number = issue["number"]
    state = status[1] if status else "未记录开发状态"
    if issue.get("state") == "closed":
        state = "已完成" if issue.get("state_reason") == "completed" else "已关闭"
    linked_section = re.search(r"^## 关联 PR[^\n]*\r?\n([\s\S]*?)(?=^#{1,2} |\Z)", body, re.MULTILINE)
    linked = sorted({int(n) for n in re.findall(r"(?<![A-Za-z0-9])#([1-9][0-9]{0,6})\b", linked_section[1])}) if linked_section else []
    assignees = issue.get("assignees") or []
    owner = ", ".join(u["login"] for u in assignees if isinstance(u, dict) and isinstance(u.get("login"), str))[:160]
    # GitHub's comment author is often the same service account for every AI
    # worker. Derive the displayed owner ONLY from a trusted structured claim.
    claim, claimed_at, started_at = None, None, None
    trusted_agents = {"Codex", "Grok", "Claude", "DeepSeek", "Qwen"}
    for comment in comments or []:
        if not isinstance(comment, dict) or (comment.get("user") or {}).get("login") != "layho":
            continue
        raw = comment.get("body") or ""
        if not isinstance(raw, str) or not raw.startswith("[APP_PROGRESS] "):
            continue
        fields = dict(re.findall(r"(?m)^([a-z_]+)=([^\r\n]{1,160})$", raw))
        agent = fields.get("agent")
        if (agent in trusted_agents and fields.get("issue") == str(number)
                and fields.get("status") in {"claimed", "in_progress", "completed"}
                and isinstance(comment.get("created_at"), str)):
            claim, claimed_at = agent, comment["created_at"]
            if fields.get("status") == "in_progress":
                started_at = comment["created_at"]
    if claim:
        owner = claim
    return {
        "linked_prs": linked, "owner": owner or "未指定",
        "claimed_at": claimed_at, "development_started_at": started_at,
        "agent": claim,
        "number": number,
        "title": issue["title"],
        "url": f"https://github.com/{repository}/issues/{number}",
        "status": state,
        "completed_steps": sum(mark != " " for mark in checks),
        "total_steps": len(checks),
        "updated_at": issue.get("updated_at"),
        "note": note[1].strip()[:320] if note else "",
    }


def snapshot() -> dict:
    try:
        with CACHE_PATH.open(encoding="utf-8") as stream:
            data = json.load(stream)
        synced_at = datetime.fromisoformat(data["synced_at"])
        if synced_at.tzinfo is None or not isinstance(data["tasks"], list):
            raise ValueError("Invalid task snapshot")
    except (OSError, ValueError, TypeError, KeyError):
        return {"synced_at": None, "stale": True, "tasks": []}
    return {**data, "stale": datetime.now(timezone.utc) - synced_at > 2 * REFRESH_INTERVAL}


def pr_linked_issues(pr: dict) -> list[int]:
    """Only explicit issue references; never guess from similar titles."""
    refs = pr.get("linked_issues") or []
    return sorted({int(n) for n in refs if type(n) is int and 0 < n <= 1000000})


def pr_stage(pr: dict) -> tuple[str, str]:
    test = pr.get("test") or {}
    merge = pr.get("merge_state") or "unknown"
    deploy = (pr.get("deployment") or {}).get("state") or "unverified"
    if (pr.get("conflict") or {}).get("blocked"):
        return "合并冲突", "受阻"
    if merge == "draft":
        return "等待开发确认", "开发中"
    if merge == "closed":
        return "已关闭", "已关闭"
    if merge == "merged":
        if deploy == "running":
            return "当前版本运行中 · 待验收确认", "待验收"
        if deploy == "deployed":
            return "已有部署记录 · 待验收确认", "待验收"
        if deploy == "failed":
            return "部署失败", "受阻"
        return "已合并 · 部署待核验", "待验收"
    if test.get("state") == "failure":
        return "测试未通过", "受阻"
    if test.get("state") == "pending":
        return "测试进行中", "开发中"
    if test.get("state") == "success":
        return "测试通过 · 待合并", "待验收"
    return "等待测试", "开发中"


def unified_tasks(issue_tasks: list[dict], tracking: dict) -> list[dict]:
    """Issue is primary; each PR belongs to an explicit Issue or its own record.

    Only explicit PR body closing/referencing phrases or a dedicated Issue
    '关联 PR' section form links. Do not infer connections from numeric ID or title.
    """
    prs = [p for p in (tracking.get("prs", []) if isinstance(tracking, dict) else [])
           if isinstance(p, dict) and type(p.get("number")) is int and p["number"] > 0]
    by_number = {p["number"]: p for p in prs}
    real_issues = [t for t in issue_tasks if isinstance(t, dict)
                   and type(t.get("number")) is int and t["number"] > 0]
    known_issue_numbers = {t["number"] for t in real_issues}
    attached: set[int] = set()
    result = []
    for task in real_issues:
        number = task["number"]
        referenced = set(task.get("linked_prs") or [])
        referenced |= {p["number"] for p in prs if number in pr_linked_issues(p)}
        linked = [by_number[n] for n in sorted(referenced) if n in by_number]
        attached.update(p["number"] for p in linked)
        tests = [p.get("test", {}).get("state", "unknown") for p in linked]
        merges = [p.get("merge_state", "unknown") for p in linked]
        deploys = [(p.get("deployment") or {}).get("state", "unverified") for p in linked]
        if not linked:
            stage = task.get("status", "未记录开发状态")
        elif any((p.get("conflict") or {}).get("blocked") for p in linked):
            stage = "关联代码存在冲突"
        elif any(x == "failure" for x in tests):
            stage = "关联代码测试失败"
        elif any(x != "merged" for x in merges):
            stage = "代码开发与合并中"
        elif any(x in {"failed", "unverified", "not_merged"} for x in deploys):
            stage = "代码已合并 · 部署待核验"
        else:
            stage = "代码已部署 · 待验收确认"
        result.append({
            **task, "key": "issue-" + str(number), "source": "issue",
            "pr_number": None, "pr_numbers": [p["number"] for p in linked],
            "prs": [pr_detail(p) for p in linked], "owner": task.get("owner") or "未指定",
            "delivery_status": stage,
            "test_state": "failure" if "failure" in tests else "pending" if "pending" in tests else
                          "success" if tests and all(x == "success" for x in tests) else None,
            "merge_state": "merged" if merges and all(x == "merged" for x in merges) else
                           "open" if merges else None,
            "deployment_state": "running" if deploys and all(x == "running" for x in deploys) else
                                "unverified" if deploys else None,
        })
    for pr in prs:
        if pr["number"] in attached:
            continue
        stage, status = pr_stage(pr)
        test = pr.get("test") or {}
        deploy = (pr.get("deployment") or {}).get("state") or "unverified"
        result.append({
            "key": "pr-" + str(pr["number"]), "source": "pr",
            "number": pr["number"], "pr_number": pr["number"],
            "pr_numbers": [pr["number"]], "prs": [pr_detail(pr)],
            "owner": pr.get("owner") or "未指定",
            "title": str(pr.get("title") or "")[:300], "url": pr.get("url") or "",
            "status": status, "delivery_status": stage,
            "completed_steps": 0, "total_steps": 0,
            "updated_at": pr.get("merged_at") or test.get("finished_at") or test.get("started_at"),
            "note": stage, "test_state": test.get("state", "unknown"),
            "merge_state": pr.get("merge_state", "unknown"),
            "deployment_state": deploy, "head": pr.get("head"), "base": pr.get("base"),
        })
    return result


def pr_detail(pr: dict) -> dict:
    """Structured evidence for the task's delivery drawer, including history."""
    return {key: pr.get(key) for key in
            ("number", "title", "url", "owner", "head", "base", "test",
             "merge_state", "merge_sha", "merged_at", "deployment", "conflict",
             "linked_issues", "review_state", "reviewed_at", "accepted_at")}
