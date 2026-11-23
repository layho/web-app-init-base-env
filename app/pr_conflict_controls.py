"""Owner-controlled conflict triage; never resolve conflicts by unsafe blind merges."""
import re
from . import pr_tracking, release_acceptance

SHA = re.compile(r"[0-9a-f]{40}\Z")
MARKER = "[PR-CONFLICT #{number}]"

class ConflictError(Exception):
    pass

def request_repair(number, head, base):
    target = pr_tracking.target()
    if target is None:
        raise ConflictError("当前应用仓库无法验证")
    repo, branch = target
    if (type(number) is not int or number < 1 or number > 1000000
            or not all(isinstance(x, str) and SHA.fullmatch(x) for x in (head, base))):
        raise ConflictError("参数无效，请刷新任务列表")
    def api(path, data=None):
        return release_acceptance.github("/repos/" + repo + path, data)

    try:
        # Check exactly the current immutable HEAD and live branch ref.
        live = api("/branches/" + branch)["commit"]["sha"]
        pr = api("/pulls/" + str(number))
        if live != base or pr.get("state") != "open" or pr.get("merged_at"):
            raise ConflictError("目标分支已变化或 PR 已关闭，请刷新后重试")
        if (pr.get("head", {}).get("sha") != head
                or pr.get("base", {}).get("ref") != branch
                or (pr.get("head", {}).get("repo") or {}).get("full_name") != repo):
            raise ConflictError("PR 内容或仓库与当前页面不一致，操作已停止")
        if pr.get("mergeable") is not False or pr.get("mergeable_state") != "dirty":
            raise ConflictError("GitHub 尚未确认真实合并冲突，当前不需要创建冲突修复任务")
        marker = MARKER.format(number=number)
        # Pagination to prevent duplicating an existing repair issue.
        for page in range(1, 6):
            issues = api(f"/issues?state=all&per_page=100&page={page}")
            if not isinstance(issues, list):
                raise ConflictError("GitHub 任务清单不完整")
            for issue in issues:
                if "pull_request" not in issue and issue.get("title", "").startswith(marker):
                    return {"created": False, "issue_number": issue["number"],
                            "url": "https://github.com/" + repo + "/issues/" + str(issue["number"])}
            if len(issues) < 100:
                break
        else:
            raise ConflictError("历史 Issue 过多，无法安全去重；请从 GitHub 手动建立修复任务")
        changed = api("/pulls/" + str(number) + "/files?per_page=100")
        names = [f["filename"] for f in changed if isinstance(f, dict)
                 and isinstance(f.get("filename"), str)
                 and len(f["filename"]) <= 180 and not f["filename"].startswith("/")
                 and ".." not in f["filename"].split("/")] if isinstance(changed, list) else []
        names = names[:100]
        body = ("## 合并冲突自动修复任务\n"
                f"关联 PR: https://github.com/{repo}/pull/{number}\n"
                f"PR HEAD: `{head}`\n目标分支: `{branch}`\n"
                f"检查时基线: `{base}`\n"
                "GitHub 已确认 mergeable_state=dirty；MacCI 无法产生当前基线测试候选。\n\n"
                "## 需要检查的 PR 修改文件（不是已确认的冲突文件）\n"
                + "\n".join("- `" + n.replace("`", "") + "`" for n in names) +
                "\n\n## 处理要求\n"
                "- 使用真实三方 diff 判断冲突，保留原功能，修复后提交新的 PR HEAD。\n"
                "- 重新验证最新 base/HEAD/merge 的 MacCI 全套测试成功。\n"
                "- 禁止直接强制合并、覆盖最新基座代码或绕过验收门禁。\n"
                "- 修复过程在本 Issue 留下进度和最终提交 SHA。")
        result = api("/issues", {"title": marker + " 自动修复与复测", "body": body})
        if type(result.get("number")) is not int or result["number"] <= 0:
            raise ConflictError("GitHub 未确认修复任务创建")
        return {"created": True, "issue_number": result["number"],
                "url": "https://github.com/" + repo + "/issues/" + str(result["number"])}
    except release_acceptance.AcceptanceError:
        raise ConflictError("GitHub 读取或创建任务失败，保留原 PR 状态") from None
