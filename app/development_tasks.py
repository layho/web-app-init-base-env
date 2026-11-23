"""Local snapshot of GitHub Issues explicitly tracked as development tasks."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

CACHE_PATH = Path(__file__).resolve().parents[1] / "logs" / "development-tasks.json"
REFRESH_INTERVAL = timedelta(minutes=5)


def parse_issue(issue: dict, repository: str) -> dict | None:
    if "pull_request" in issue or issue.get("title", "").startswith("[APP-FEEDBACK #"):
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
    return {
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


def unified_tasks(issue_tasks: list[dict], tracking: dict) -> list[dict]:
    """One user-facing list from genuine Issues and tracked PR delivery facts.

    The PR itself is a task even when no separate Issue exists. Do not equate
    successful CI or merging with production delivery / user acceptance.
    """
    results = []
    seen = set()
    for task in issue_tasks:
        if not isinstance(task, dict) or type(task.get("number")) is not int:
            continue
        row = {**task, "key": "issue-" + str(task["number"]),
               "source": "issue", "pr_number": None,
               "test_state": None, "merge_state": None,
               "deployment_state": None, "delivery_status": task.get("status", "未记录开发状态")}
        results.append(row)
        seen.add(row["key"])
    if not isinstance(tracking, dict):
        return results
    for pr in tracking.get("prs", []):
        if not isinstance(pr, dict) or type(pr.get("number")) is not int:
            continue
        number = pr["number"]
        key = "pr-" + str(number)
        if key in seen:
            continue
        test = pr.get("test") or {}
        merge = pr.get("merge_state") or "unknown"
        deploy = (pr.get("deployment") or {}).get("state") or "unverified"
        conflict = bool((pr.get("conflict") or {}).get("blocked"))
        if conflict:
            stage, status = "合并冲突", "受阻"
        elif merge == "draft":
            stage, status = "等待开发确认", "开发中"
        elif merge == "closed":
            stage, status = "已关闭", "已关闭"
        elif merge == "merged":
            if deploy == "running":
                stage, status = "当前版本运行中 · 待验收确认", "待验收"
            elif deploy == "deployed":
                stage, status = "已有部署记录 · 待验收确认", "待验收"
            elif deploy == "failed":
                stage, status = "部署失败", "受阻"
            else:
                stage, status = "已合并 · 部署待核验", "待验收"
        elif test.get("state") == "failure":
            stage, status = "测试未通过", "受阻"
        elif test.get("state") == "pending":
            stage, status = "测试进行中", "开发中"
        elif test.get("state") == "success":
            stage, status = "测试通过 · 待合并", "待验收"
        else:
            stage, status = "等待测试", "开发中"
        results.append({
            "key": key, "source": "pr", "number": number,
            "pr_number": number, "title": pr.get("title", "")[:300],
            "url": pr.get("url", ""), "status": status,
            "delivery_status": stage, "completed_steps": 0, "total_steps": 0,
            "updated_at": pr.get("merged_at") or test.get("finished_at") or
                          test.get("started_at"),
            "note": stage, "test_state": test.get("state", "unknown"),
            "merge_state": merge, "deployment_state": deploy,
            "head": pr.get("head"), "base": pr.get("base"),
        })
        seen.add(key)
    return results
