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
    if not section:
        return None
    progress = section[1]
    status = re.search(r"^状态[：:]\s*(待开发|开发中|待验收|受阻|已完成)[ \t]*\r?$", progress, re.MULTILINE)
    note = re.search(r"^更新说明[：:][ \t]*([^\r\n]+)", progress, re.MULTILINE)
    checks = re.findall(r"^[ \t]*[-*] \[([ xX])\][ \t]+\S", progress, re.MULTILINE)
    number = issue["number"]
    state = status[1] if status else "未标记"
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
