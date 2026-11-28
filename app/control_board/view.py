"""Isolated view model for the unified task board.

This is the W10 UI component. It does not mount a page and does not write
authoritative state. Shared frontend files stay untouched until a later
reservation.
"""

from __future__ import annotations


def render_view(page, flags):
    items = page.get("items") or []
    banners = []
    if page.get("gap"):
        banners.append("事件序号有缺口，未跳过。")
    if page.get("code"):
        banners.append(page["code"])
    if any(item.get("schedule_evidence") != "present" for item in items):
        banners.append("调度投影不是权威证据。")
    banners.append("人工干预未接通执行器。")
    return {
        "title": "统一任务看板",
        "items": items,
        "tests": [test for item in items for test in item.get("tests", [])],
        "associations": [pr for item in items for pr in item.get("prs", [])],
        "flags": flags,
        "intervention": {"entry": "manual", "connected_executor": False},
        "banners": banners,
        "production_healthy": False,
        "mounted": False,
        "page": {
            "http": page.get("http"),
            "code": page.get("code"),
            "gap": page.get("gap"),
            "evidence_state": page.get("evidence_state"),
            "next_page_token": page.get("next_page_token"),
        },
    }


def render_text(page):
    lines = ["统一任务看板", "人工干预", "未接通执行器"]
    for item in page.get("items") or []:
        lines.append(item["task_id"])
        lines.append(item["owner"]["display"])
        lines.append(item["executor"]["display"])
        lines.append(item["schedule_mode"]["display"])
        for name, lane in item["lanes"].items():
            lines.append(f"{name}:{lane['state']}")
            if lane.get("failure_reason"):
                lines.append(lane["failure_reason"])
        for reason in item.get("failure_reasons") or []:
            lines.append(reason["display"])
        if item.get("archived"):
            lines.append("历史已归档")
    if not page.get("items"):
        lines.append("未记录")
    return "\n".join(lines)
