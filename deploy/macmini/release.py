"""Run isolated acceptance, merge the exact PR, and verify the deployed SHA."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from acceptance import ORIGIN, REPO, SOURCE, command, git, local_request, require


UPDATER = "com.example.webapp.updater"
LOCAL_PORT = 18080
GITHUB_REMOTE = {f"git@github.com:{REPO}.git", f"https://github.com/{REPO}.git", f"ssh://git@github.com/{REPO}.git"}


def github_json(*args):
    result = subprocess.check_output(["gh", *args], text=True)
    return json.loads(result)


def pull_request(number):
    return github_json("pr", "view", str(number), "--repo", REPO,
                       "--json", "state,baseRefName,baseRefOid,headRefOid,isDraft,mergeCommit")


def require_production_checkout(path):
    require(path.is_dir() and (path / ".env").is_file(), "未找到已配置的正式安装目录及 .env")
    require(git(path, "remote", "get-url", "origin") in GITHUB_REMOTE, "正式目录的 Git 仓库不匹配")
    require(git(path, "branch", "--show-current") == "main", "正式目录必须位于 main 分支")
    require(not git(path, "status", "--porcelain", "--untracked-files=no"), "正式目录存在未提交修改")
    scope = "system" if (Path("/Library/LaunchDaemons") / f"{UPDATER}.plist").exists() else f"gui/{os.getuid()}"
    domain = f"{scope}/{UPDATER}"
    # Read only the job's label and working directory. Do not load credential files.
    info = subprocess.check_output(["/bin/launchctl", "print", domain], text=True)
    directory = re.search(r"^\s*working directory = (.+)$", info, re.MULTILINE)
    require(directory is not None and Path(directory[1]).resolve() == path, "正式更新任务的工作目录不匹配")
    return domain


def verify_deployment(sha, timeout=600):
    url = f"{ORIGIN}/api/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            local, _ = local_request(LOCAL_PORT, "/api/version", 200)
            if local.get("running_sha") == sha:
                result = subprocess.check_output(
                    ["curl", "--fail", "--silent", "--max-time", "8", "-H", "Cache-Control: no-cache", url],
                    text=True,
                )
                public = json.loads(result)
                if public.get("running_sha") == sha:
                    return
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        time.sleep(3)
    raise RuntimeError(f"部署尚未确认：本机与公网没有同时返回预期 SHA {sha}。请检查正式 updater 日志。")


def release(number, expected_head, source, production):
    target = require_production_checkout(production)
    require(git(source, "remote", "get-url", "origin") in GITHUB_REMOTE, "发布脚本必须从 App 仓库副本运行")
    production_remote = git(production, "remote", "get-url", "origin")
    initial = pull_request(number)
    require(initial["baseRefName"] == "main" and initial["headRefOid"] == expected_head, "PR 目标或提交已变化")
    require(initial["state"] in {"OPEN", "MERGED"}, "PR 已关闭")

    with tempfile.TemporaryDirectory(prefix="webapp-release-") as workspace:
        temporary = Path(workspace) / "candidate"
        command(["git", "clone", "--quiet", "--no-local", "--no-checkout", source, temporary])
        git(temporary, "remote", "set-url", "origin", production_remote)
        if production_remote.startswith("https://"):
            git(temporary, "config", "--local", "--add", "credential.helper", "")
            git(temporary, "config", "--local", "--add", "credential.helper", "!gh auth git-credential")
        if initial["state"] == "OPEN":
            git(temporary, "fetch", "--quiet", "origin", f"refs/pull/{number}/merge")
            candidate = git(temporary, "rev-parse", "FETCH_HEAD")
            parents = git(temporary, "show", "-s", "--format=%P", candidate).split()
            require(parents == [initial["baseRefOid"], expected_head], "合并预览与当前 PR 不一致")
        else:
            candidate = initial["mergeCommit"]["oid"]
            git(temporary, "fetch", "--quiet", "origin", "main")
            require(git(temporary, "rev-parse", "FETCH_HEAD") == candidate, "main 已有更新，拒绝发布旧合并版本")
        command(["git", "-C", temporary, "checkout", "--quiet", "--detach", candidate])
        tested_tree = git(temporary, "rev-parse", "HEAD^{tree}")
        acceptance_env = dict(os.environ)
        for credential in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            acceptance_env.pop(credential, None)
        command([sys.executable, temporary / "deploy/macmini/acceptance.py", "--require-feedback"],
                env=acceptance_env)

        current = pull_request(number)
        for field in ("state", "baseRefName", "baseRefOid", "headRefOid"):
            require(current[field] == initial[field], "验收期间 PR 或 main 已变化，请重新验收")
        if current["state"] == "OPEN":
            if current["isDraft"]:
                command(["gh", "pr", "ready", str(number), "--repo", REPO])
            result = github_json("api", "--method", "PUT", f"repos/{REPO}/pulls/{number}/merge",
                                 "-f", f"sha={expected_head}", "-f", "merge_method=merge")
            require(result["merged"], "GitHub 未完成合并")
            merged_sha = result["sha"]
            print(f"已合并 #{number}，提交 {merged_sha}", flush=True)
        else:
            merged_sha = candidate

        git(temporary, "fetch", "--quiet", "origin", "main")
        require(git(temporary, "rev-parse", "FETCH_HEAD") == merged_sha, "合并后 main 已变化")
        require(git(temporary, "rev-parse", f"{merged_sha}^{{tree}}") == tested_tree,
                "合并内容与验收内容不同，未触发发布")

    if target.startswith("system/"):
        with (production / "logs" / "update-request").open("ab") as signal:
            signal.write(b"1")
    else:
        command(["/bin/launchctl", "kickstart", target])
    print("已触发正式更新任务，正在核对本机和公网运行版本…", flush=True)
    verify_deployment(merged_sha)
    print(f"PASS：本机和公网运行 SHA 均为 {merged_sha}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pr", type=int)
    parser.add_argument("expected_head")
    apps = Path.home() / "Applications"
    generated = (SOURCE / ".app-init.json").is_file()
    deployment = apps / "app-init" / REPO.replace("/", "--") if generated else apps / REPO.split("/")[-1]
    parser.add_argument("--deployment", type=Path, default=deployment)
    args = parser.parse_args()
    os.environ["PATH"] = f"/opt/homebrew/bin:/usr/local/bin:{os.environ['PATH']}"
    try:
        require(sys.platform == "darwin" and os.getuid() != 0, "请在 Mac 已登录用户的终端运行")
        require(sys.version_info >= (3, 10), "需要 Python 3.10 或以上")
        require(args.pr > 0 and re.fullmatch(r"[a-f0-9]{40}", args.expected_head), "需要 PR 编号及完整候选 SHA")
        production = args.deployment.expanduser().resolve()
        proxy = subprocess.run(["git", "-C", production, "config", "--local", "--get", "http.proxy"],
                               capture_output=True, text=True).stdout.strip()
        if proxy:
            os.environ.update({name: proxy for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")})
            os.environ.update(no_proxy="localhost,127.0.0.1,::1", NO_PROXY="localhost,127.0.0.1,::1")
        release(args.pr, args.expected_head, SOURCE, production)
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError, KeyError) as error:
        print(f"发布中止：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
