"""Base-only manual PR actions through the already trusted MacCI workers.

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
CONTEXT = "macmini/isolated-tests-v1"


class ManualActionError(Exception):
    pass


def configured():
    return (pr_tracking.target() == (REPO, "preview")
            and os.getenv("APP_BASE_PREVIEW") == "1"
            and os.getenv("APP_PUBLIC_ORIGIN") == "https://base.layho.com")


def api(path):
    try:
        return release_acceptance.api(path)
    except (release_acceptance.AcceptanceError, KeyError, ValueError, TypeError):
        raise ManualActionError("暂时无法读取 GitHub 最新状态，操作未执行") from None


def current_pr(number, head, base):
    if not configured():
        raise ManualActionError("当前应用未授权执行基座 MacCI 操作")
    if (type(number) is not int or not 1 <= number <= 1000000
            or not all(isinstance(x, str) and SHA.fullmatch(x) for x in (head, base))):
        raise ManualActionError("PR 参数无效，请刷新页面")
    branch = api("/branches/preview")["commit"]["sha"]
    pr = api(f"/pulls/{number}")
    if branch != base or pr.get("head", {}).get("sha") != head:
        raise ManualActionError("代码或目标版本已变化，请刷新后重试")
    if (pr.get("state") != "open" or pr.get("base", {}).get("ref") != "preview"
            or (pr.get("head", {}).get("repo") or {}).get("full_name") != REPO
            or pr.get("user", {}).get("login") != "layho"):
        raise ManualActionError("PR 状态或所属仓库不符合安全要求")
    if pr.get("mergeable") is not True or pr.get("mergeable_state") not in {"clean", "unstable"}:
        raise ManualActionError("存在代码冲突或 GitHub 尚未确认可以测试，请先处理冲突")
    merge_sha = pr.get("merge_commit_sha")
    if not isinstance(merge_sha, str) or not SHA.fullmatch(merge_sha):
        raise ManualActionError("GitHub 测试候选尚未就绪，请刷新后重试")
    commit = api("/git/commits/" + merge_sha)
    if [p.get("sha") for p in commit.get("parents", [])] != [base, head]:
        raise ManualActionError("GitHub 合并候选与当前版本不一致，请刷新后重试")
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


def request_test(number, head, base):
    _, merge = current_pr(number, head, base)
    trusted_runner(TEST_RUNNER)
    # Refuse concurrent launches, rather than claiming a request was queued
    # when an existing worker would immediately discard it.
    lockfile = ROOT / "worker.lock"
    try:
        with lockfile.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ManualActionError("已有测试正在运行，请在其结束后点击重新测试") from None
            log = ROOT / "manual-test-request.log"
            with log.open("ab") as out:
                process = subprocess.Popen(
                    [sys.executable, str(TEST_RUNNER), "--repo", REPO,
                     "--pr", str(number), "--force"],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                    start_new_session=True, close_fds=True,
                    env={key: value for key, value in os.environ.items()
                         if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL",
                                    "SSH_AUTH_SOCK", "DOCKER_HOST"}} |
                        {"PYTHONDONTWRITEBYTECODE": "1"},
                )
    except OSError:
        raise ManualActionError("本机测试程序启动失败，未受理此次操作") from None
    return {"accepted": True, "action": "test", "number": number,
            "head": head, "base": base, "candidate": merge,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "message": "已向本机测试程序发起指定 PR 的执行请求，请刷新确认实际 MacCI 回执"}


def latest_status(head):
    statuses = api("/commits/" + head + "/statuses?per_page=100")
    return next((s for s in statuses if s.get("context") == CONTEXT), None)


def request_merge(number, head, base):
    pr, merge = current_pr(number, head, base)
    if pr.get("draft") or pr.get("mergeable_state") != "clean":
        raise ManualActionError("PR 尚未解除草稿或 GitHub 合并条件未满足")
    status = latest_status(head)
    if (not isinstance(status, dict) or status.get("state") != "success"
            or status.get("description") != f"base={base} merge={merge} passed"):
        raise ManualActionError("最新版本的 MacCI 尚未通过，不能合并")
    # The authorized coordinator still checks review comments, ancestor
    # dependencies, exact test tree and freeze before making any git push.
    # Guard the freeze before signalling to give users actionable feedback.
    main = api("/branches/main")["commit"]["sha"]
    if main != base:
        raise ManualActionError("当前候选版本尚未完成正式验收，合并已冻结")
    try:
        subprocess.run(["/bin/launchctl", "kickstart",
                        f"gui/{os.getuid()}/{MERGE_LABEL}"],
                       check=True, timeout=6, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise ManualActionError("合并服务未启动或无法唤醒，请检查 MacCI 服务状态") from None
    return {"accepted": True, "action": "merge-check", "number": number,
            "head": head, "base": base,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "message": "已唤醒安全合并程序，仍须核验审查授权、冻结状态和实际合并回执"}
