"""Shared, instance-scoped manual PR actions through the already trusted MacCI workers.

No direct merges, deployments, arbitrary commands, user-supplied repositories,
or web-facing GitHub tokens. A 202 response means triggered, not succeeded.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from . import pr_tracking, release_acceptance

REPO = "layho/web-app-init-base-env"
SHA = re.compile(r"[0-9a-f]{40}\Z")
ROOT = Path.home() / ".local/share/macmini-pr-tests"
TEST_RUNNER = ROOT / "runner.py"
MERGE_LABEL = "com.layho.macci-preview-merge"
APP_MERGE_LABEL = "com.layho.macci-app-main-panel"
APP_MAIN_PUBLISHERS = frozenset({"layho/macmini-office-app-panel"})
CONTEXT = "macmini/isolated-tests-v1"


class ManualActionError(Exception):
    pass


def action_scope():
    """Server-trusted installation, never user-selected repo or branch."""
    scope = pr_tracking.target()
    if (scope == (REPO, "preview") and os.getenv("APP_BASE_PREVIEW") == "1"
            and os.getenv("APP_PUBLIC_ORIGIN") == "https://base.layho.com"):
        return scope
    if (scope and scope[0] in APP_MAIN_PUBLISHERS and scope[1] == "main"
            and os.getenv("APP_BASE_PREVIEW") != "1"):
        return scope
    return None


def configured():
    return action_scope() is not None


def api(path):
    scope = action_scope()
    if scope is None:
        raise ManualActionError("当前应用没有经过授权的本机 MacCI 协调器")
    try:
        if scope == (REPO, "preview"):
            return release_acceptance.api(path)
        return release_acceptance.github("/repos/" + scope[0] + path)
    except (release_acceptance.AcceptanceError, KeyError, ValueError, TypeError):
        raise ManualActionError("暂时无法读取 GitHub 最新状态，操作未执行") from None


def current_pr(number, head, base):
    if not configured():
        raise ManualActionError("当前应用未授权执行基座 MacCI 操作")
    if (type(number) is not int or not 1 <= number <= 1000000
            or not all(isinstance(x, str) and SHA.fullmatch(x) for x in (head, base))):
        raise ManualActionError("PR 参数无效，请刷新页面")
    repo, branch_name = action_scope()
    branch = api("/branches/" + branch_name)["commit"]["sha"]
    pr = api(f"/pulls/{number}")
    if branch != base or pr.get("head", {}).get("sha") != head:
        raise ManualActionError("代码或目标版本已变化，请刷新后重试")
    if (pr.get("state") != "open" or pr.get("base", {}).get("ref") != branch_name
            or (pr.get("head", {}).get("repo") or {}).get("full_name") != repo
            or pr.get("user", {}).get("login") != "layho"):
        raise ManualActionError("PR 状态或所属仓库不符合安全要求")
    if pr.get("mergeable") is not True or pr.get("mergeable_state") not in {"clean", "unstable"}:
        raise ManualActionError("存在代码冲突或 GitHub 尚未确认可以测试，请先处理冲突")
    merge_sha = pr.get("merge_commit_sha")
    if not isinstance(merge_sha, str) or not SHA.fullmatch(merge_sha):
        raise ManualActionError("GitHub 测试候选尚未就绪，请刷新后重试")
    commit = api("/git/commits/" + merge_sha)
    parents = [p.get("sha") for p in commit.get("parents", [])]
    if parents != [base, head]:
        if len(parents) == 2 and parents[1] == head:
            raise ManualActionError(
                "GitHub 合并候选仍基于旧 preview（候选基线 " + str(parents[0])[:12] +
                "，当前基线 " + base[:12] + "）。"
                "刷新页面不会重新生成合并候选；请先安全更新 PR 开发分支到当前 preview，"
                "再对新的 HEAD/base/merge 执行 MacCI。旧测试结果不得复用。"
            )
        raise ManualActionError(
            "GitHub 合并候选的父提交与当前 HEAD/base 不匹配；未触发测试。"
            "请核对 PR mergeability 和当前分支，禁止复用旧 MacCI 回执。"
        )
    return pr, merge_sha


def trusted_runner(path):
    # Only fixed files under the service user's protected MacCI directory.
    try:
        root = ROOT.lstat()
        info = path.lstat()
        if (not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid()
                or root.st_mode & 0o022 or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise ManualActionError("本机测试程序权限异常，已停止")
    except OSError:
        raise ManualActionError("本机测试程序尚未安装，无法立即运行") from None


def runner_supports_lock_handoff(path):
    """Recognize only the reviewed host runner protocol, not a guessed version."""
    try:
        info = path.stat()
        if info.st_size > 512 * 1024:
            return False
        return re.search(r"(?m)^LOCK_HANDOFF_PROTOCOL = 1(?:[ \t]*#.*)?$",
                         path.read_text(encoding="utf-8")) is not None
    except (OSError, UnicodeError):
        return False


def request_test(number, head, base):
    _, merge = current_pr(number, head, base)
    repo, _ = action_scope()
    trusted_runner(TEST_RUNNER)
    handoff = runner_supports_lock_handoff(TEST_RUNNER)
    lockfile = ROOT / "worker.lock"
    command = [sys.executable, str(TEST_RUNNER), "--repo", repo,
               "--pr", str(number), "--force"]
    spawn_options = {
        "stdin": subprocess.DEVNULL, "start_new_session": True, "close_fds": True,
        "env": {key: value for key, value in os.environ.items()
                if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL",
                           "SSH_AUTH_SOCK", "DOCKER_HOST"}} |
               {"PYTHONDONTWRITEBYTECODE": "1"},
    }
    try:
        with lockfile.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ManualActionError("已有测试正在运行，请在其结束后点击重新测试") from None
            if handoff:
                # The same file description remains locked in the child.
                # No gap exists between the test for a busy runner and spawn.
                with (ROOT / "manual-test-request.log").open("ab") as out:
                    subprocess.Popen(command + ["--lock-fd", str(lock.fileno())],
                                     stdout=out, stderr=out,
                                     pass_fds=(lock.fileno(),), **spawn_options)
        if not handoff:
            # A separately installed host worker may still be on the old
            # protocol. Preserve its old invocation until reviewed upgrade.
            # This legacy path retains the old scheduling race.
            with (ROOT / "manual-test-request.log").open("ab") as out:
                subprocess.Popen(command, stdout=out, stderr=out, **spawn_options)
    except OSError:
        raise ManualActionError("本机测试程序启动失败，未受理此次操作") from None
    return {"accepted": True, "action": "test", "number": number,
            "head": head, "base": base, "candidate": merge,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "lock_handoff": handoff,
            "message": ("已向本机测试程序发起指定 PR 的执行请求，请刷新确认实际 MacCI 回执"
                        if handoff else
                        "已调用旧版 MacCI Runner（兼容模式，可能被并行测试抢锁），"
                        "请升级本机 Runner 并核对实际测试回执")}


def latest_status(head):
    statuses = api("/commits/" + head + "/statuses?per_page=100")
    return next((s for s in statuses if s.get("context") == CONTEXT), None)


def published_preview_equivalent(read, preview, main):
    """Recognize an exact, officially merged preview-to-main release.

    Equal trees alone are not publication evidence. Only an actual GitHub
    release merge with the preview as a parent and matching PR is accepted.
    A missing or inconsistent receipt leaves the candidate frozen.
    """
    if not all(isinstance(x, str) and SHA.fullmatch(x) for x in (preview, main)):
        return False
    if preview == main:
        return True
    published = read("/git/commits/" + main)
    candidate = read("/git/commits/" + preview)
    if not isinstance(published, dict) or not isinstance(candidate, dict):
        return False
    if published.get("sha") != main or candidate.get("sha") != preview:
        return False
    tree = (candidate.get("tree") or {}).get("sha")
    parents = published.get("parents")
    if (not isinstance(tree, str) or not SHA.fullmatch(tree)
            or (published.get("tree") or {}).get("sha") != tree
            or not isinstance(parents, list) or len(parents) != 2
            or not all(isinstance(p, dict) for p in parents)
            or parents[1].get("sha") != preview):
        return False
    message = published.get("message")
    match = re.match(r"Merge pull request #([1-9][0-9]{0,8}) from layho/preview(?:\\n|$)",
                     message) if isinstance(message, str) else None
    if not match:
        return False
    release = read("/pulls/" + match.group(1))
    if not isinstance(release, dict):
        return False
    source, target = release.get("head") or {}, release.get("base") or {}
    return (release.get("merged") is True and release.get("state") == "closed"
            and release.get("merge_commit_sha") == main
            and source.get("sha") == preview and source.get("ref") == "preview"
            and target.get("ref") == "main"
            and (source.get("repo") or {}).get("full_name") == REPO
            and (target.get("repo") or {}).get("full_name") == REPO)


def request_merge(number, head, base):
    pr, merge = current_pr(number, head, base)
    if pr.get("draft"):
        raise ManualActionError("PR 尚未解除草稿，请先完成审查并解除 Draft")
    if pr.get("mergeable_state") not in {"clean", "unstable"}:
        raise ManualActionError("GitHub 合并状态为 " + ("unstable（存在未完成或未通过的状态检查）" if pr.get("mergeable_state") == "unstable" else "非 clean") + "；安全合并门禁尚未满足，请核对状态检查")
    status = latest_status(head)
    if (not isinstance(status, dict) or status.get("state") != "success"
            or status.get("description") != f"base={base} merge={merge} passed"):
        raise ManualActionError("最新版本的 MacCI 尚未通过，不能合并")
    # The independent host worker always rechecks exact candidate, MacCI,
    # OWNER review and expected main/preview before any non-force push.
    scope = action_scope()
    if scope == (REPO, "preview"):
        main = api("/branches/main")["commit"]["sha"]
        if not published_preview_equivalent(api, base, main):
            raise ManualActionError("当前候选版本尚未完成正式验收，合并已冻结")
        if (api("/branches/main")["commit"]["sha"] != main
                or api("/branches/preview")["commit"]["sha"] != base):
            raise ManualActionError("代码或目标版本已变化，请刷新后重试")
        label = MERGE_LABEL
    else:
        label = APP_MERGE_LABEL
    try:
        subprocess.run(["/bin/launchctl", "kickstart",
                        f"gui/{os.getuid()}/{label}"],
                       check=True, timeout=6, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise ManualActionError("合并服务未启动或无法唤醒，请检查 MacCI 服务状态") from None
    return {"accepted": True, "action": "merge-check", "number": number,
            "head": head, "base": base,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "message": "已唤醒实例专属安全合并程序，仍须核验 OWNER 审查和实际合并回执"}
