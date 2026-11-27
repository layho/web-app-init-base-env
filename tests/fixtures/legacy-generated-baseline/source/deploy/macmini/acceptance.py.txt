"""Run App's release acceptance in a disposable Mac environment."""
import argparse
import getpass
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


SOURCE = Path(__file__).resolve().parents[2]
REPO = "example/web-app"
ORIGIN = "https://app.example.com"
ROLES = ("feedback", "updater", "web")
REPORT_ISSUE = None


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def postgres_bin():
    candidates = []
    for prefix in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
        candidates += sorted(prefix.glob("postgresql*"), reverse=True)
    on_path = shutil.which("initdb")
    paths = ([Path(on_path).parent] if on_path else []) + [
        candidate / "bin" for candidate in candidates
    ]
    for path in paths:
        if all(os.access(path / command, os.X_OK) for command in ("initdb", "pg_ctl", "psql")):
            return path
    raise RuntimeError("找不到 PostgreSQL 命令，请先安装 PostgreSQL（例如 brew install postgresql@17）")


def pick_port():
    for port in range(18018, 18999):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("没有空闲的验收端口（18018–18998）")


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()


def local_request(port, path, expected, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        method = "GET" if body is None else "POST"
        connection.request(method, path, json.dumps(body) if method == "POST" else None, {
            "Content-Type": "application/json", "Origin": ORIGIN, **(headers or {}),
        })
        response = connection.getresponse()
        raw = response.read()
        require(response.status == expected, f"{method} {path} 返回 {response.status}，预期 {expected}")
        payload = json.loads(raw) if raw and "application/json" in response.getheader("Content-Type", "") else raw
        return payload, response.getheader("Set-Cookie", "")
    finally:
        connection.close()


def check_web(port, sha):
    version, _ = local_request(port, "/api/version", 200)
    require(version["running_sha"] == sha, "运行 SHA 不符")
    require(version["version"] and version["manual_update_available"], "版本号或手动更新未启用")
    html, _ = local_request(port, "/", 200)
    match = re.search(rb'src="(/assets/[^"]+\.js)"', html)
    require(match is not None, "首页缺少构建后的 JS")
    local_request(port, match[1].decode("ascii"), 200)
    local_request(port, "/api/v1/feedback", 401)
    local_request(port, "/api/v1/deploy/update", 401, {})


def login(port, password):
    result, header = local_request(port, "/api/login", 200, {"username": "acceptance", "password": password})
    cookie = SimpleCookie(header)["webapp_session"]
    require(cookie["secure"] and cookie["httponly"] and cookie["samesite"].lower() == "strict", "Cookie 属性不符")
    require(result["authenticated"] is True, "登录响应未认证")
    return {"Cookie": f"webapp_session={cookie.value}"}


def check_session(port, password):
    headers = login(port, password)
    refresh, _ = local_request(port, "/api/session", 200, headers=headers)
    require(refresh["username"] == "acceptance", "刷新后账号不符")
    payload = {"category": "acceptance", "message": "Local session smoke test"}
    local_request(port, "/api/v1/feedback", 403, payload, headers)
    csrf = {**headers, "X-CSRF-Token": refresh["csrf_token"]}
    local_request(port, "/api/v1/feedback", 200, headers=headers)
    local_request(port, "/api/logout", 200, {}, csrf)
    local_request(port, "/api/session", 401, headers=headers)


def trigger_update(port, password):
    path = "/api/v1/deploy/update"
    headers = login(port, password)
    refresh, _ = local_request(port, "/api/session", 200, headers=headers)
    csrf = {**headers, "X-CSRF-Token": refresh["csrf_token"]}
    local_request(port, path, 403, {}, headers)
    result, _ = local_request(port, path, 202, {}, csrf)
    require(result == {"accepted": True}, "手动更新请求未被接受")


def wait_for_agent(domain, label, output, marker, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if output.is_file() and marker in output.read_text(errors="replace"):
            state = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True, text=True)
            if re.search(r"\bstate = (not running|waiting)", state.stdout):
                return
        time.sleep(1)
    raise RuntimeError(f"{label.rsplit('.', 1)[-1]} 未在时限内产生预期验收结果")


def github(token, path, data=None):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "webapp-mac-acceptance"},
    )
    try:
        # Keep TLS verification enabled and avoid forwarding credentials across redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API 返回 {error.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("Mac 无法连接 GitHub API") from None


def wait_for_feedback(port, headers, feedback_id, output, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records, _ = local_request(port, "/api/v1/feedback", 200, headers=headers)
        entry = next((row for row in records if row["id"] == feedback_id), None)
        if entry and entry["status"] == "synced" and isinstance(entry["github_issue_number"], int):
            return entry["github_issue_number"]
        if output.is_file():
            match = re.search(rf"Feedback sync failed for ID {feedback_id}: "
                              r"(lookup|create): (GitHub HTTP [0-9]{3}|[A-Za-z]+)",
                              output.read_text(errors="replace"))
            if match:
                operation = {"lookup": "查询已有 Issue", "create": "创建 Issue"}[match[1]]
                hint = "，请核对该仓库的 Issues 读写权限" if match[2] in {"GitHub HTTP 403", "GitHub HTTP 404"} else ""
                raise RuntimeError(f"反馈同步失败：{operation}返回 {match[2]}{hint}")
        time.sleep(1)
    raise RuntimeError("反馈任务未在 120 秒内更新数据库，详见本机诊断日志")


def feedback_check(repo, port, password, domain, prefix, candidate, token, checks):
    require(re.fullmatch(r"[A-Za-z0-9_-]+", token) is not None, "Token 格式不正确")
    # Probe repo access before letting the worker run. Only the private repo above sees this token.
    github(token, "/issues?per_page=1")
    env_file = repo / ".env"
    with env_file.open("a") as stream:
        stream.write(f"APP_GITHUB_FEEDBACK_TOKEN={shlex.quote(token)}\n")
    headers = login(port, password)
    refresh, _ = local_request(port, "/api/session", 200, headers=headers)
    csrf = {**headers, "X-CSRF-Token": refresh["csrf_token"]}
    probe = secrets.token_hex(6)
    message = (f"Mac 独立验收反馈（测试数据）\n候选提交：{candidate}\n"
               f"验收编号：{probe}\n本次通过：{checks}。")
    record, _ = local_request(port, "/api/v1/feedback", 200, {"category": "acceptance", "message": message}, csrf)
    require(record["status"] == "pending", "反馈未进入待同步队列")
    label = f"{prefix}.feedback"
    command(["launchctl", "kickstart", f"{domain}/{label}"])
    issue_number = wait_for_feedback(port, headers, record["id"], repo / "logs/feedback.out.log")
    issue = github(token, f"/issues/{issue_number}")
    require(probe in issue.get("body", ""), "GitHub Issue 内容和本次验收不一致")
    local_request(port, "/api/logout", 200, {}, csrf)
    return issue["html_url"]


def run_acceptance(temp, log, pg, domain, port, feedback_only, require_feedback=False):
    candidate = git(SOURCE, "rev-parse", "HEAD")
    print(f"Mac 验收候选 SHA {candidate}", flush=True)
    origin, publisher, repo = (temp / name for name in ("origin.git", "publisher", "app"))
    command(["git", "init", "-q", "--bare", "--initial-branch=main", origin], stdout=log, stderr=log)
    command(["git", "-C", SOURCE, "push", "-q", origin, f"{candidate}:refs/heads/main"], stdout=log, stderr=log)
    for target in (publisher, repo):
        command(["git", "clone", "-q", origin, target], stdout=log, stderr=log)
    git(publisher, "config", "user.name", "App Acceptance")
    git(publisher, "config", "user.email", "webapp-acceptance@example.invalid")
    git(publisher, "config", "commit.gpgsign", "false")
    git(publisher, "config", "core.hooksPath", "/dev/null")

    identifier = secrets.token_hex(6)
    prefix = f"com.example.webapp.acceptance.{identifier}"
    stage_env = {**os.environ, "APP_ACCEPTANCE_ID": identifier, "APP_ACCEPTANCE_PORT": str(port)}
    cluster = temp / "db"
    sock = temp / "sock"
    sock.mkdir(mode=0o700)
    pgctl = [pg / "pg_ctl", "-D", cluster]
    command([pg / "initdb", "-D", cluster, "-A", "trust", "-U", "webapp_acceptance"], stdout=log, stderr=log)
    postgres_started = False
    agent_install_attempted = False
    env_file = repo / ".env"
    try:
        command([*pgctl, "-l", temp / "postgres.log", "-o", f"-h '' -k {sock}", "-w", "start"], stdout=log, stderr=log)
        postgres_started = True
        dsn = f"postgresql+psycopg://webapp_acceptance@/postgres?{urllib.parse.urlencode({'host': str(sock)})}"
        python = repo / ".venv/bin/python"
        command([sys.executable, "-m", "venv", repo / ".venv"], stdout=log, stderr=log)
        command([python, "-m", "pip", "install", "-r", "requirements.txt"], cwd=repo, stdout=log, stderr=log)
        password = secrets.token_urlsafe(30)
        password_hash = subprocess.check_output(
            [python, "-c", "import sys; from app.auth import hash_password; print(hash_password(sys.stdin.read()))"],
            cwd=repo, input=password, text=True, stderr=log,
        ).strip()
        config = {
            "DATABASE_URL": dsn, "APP_PUBLIC_ORIGIN": ORIGIN, "APP_COOKIE_SECURE": "true",
            "APP_ALLOW_LOCAL_DEV": "false", "APP_ALLOW_BOOTSTRAP_LOCK": "false",
            "APP_ADMIN_USERNAME": "acceptance", "APP_ADMIN_PASSWORD_HASH": password_hash,
            "APP_GITHUB_FEEDBACK_TOKEN": "", "APP_API_TOKEN": "",
        }
        with env_file.open("x") as stream:
            stream.write("".join(f"{key}={shlex.quote(value)}\n" for key, value in config.items()))
        command([python, "-m", "scripts.apply_migrations"], cwd=repo, env={**stage_env, **config}, stdout=log, stderr=log)
        # Leave the normal production feedback ID range alone in the shared Issues namespace.
        feedback_start = (1 << 40) + secrets.randbelow(1 << 40)
        command([pg / "psql", "-h", sock, "-U", "webapp_acceptance", "-d", "postgres",
                 "-v", "ON_ERROR_STOP=1", "-c", f"ALTER SEQUENCE user_feedback_id_seq RESTART WITH {feedback_start}"],
                stdout=log, stderr=log)
        print("PostgreSQL 隔离库迁移通过，正在构建并启动验收服务…", flush=True)
        agent_install_attempted = True
        command(["/bin/bash", "deploy/macmini/manage.sh", "install"], cwd=repo, env=stage_env, stdout=log, stderr=log)
        check_web(port, candidate)
        check_session(port, password)

        checks = "隔离 PostgreSQL 迁移、pytest、前端构建、launchd 启动、登录退出和运行 SHA"
        if not feedback_only:
            print("登录和会话通过，正在验证 launchd 更新与失败回退…", flush=True)
            git(publisher, "commit", "-qm", "Acceptance: fast-forward", "--allow-empty")
            upgraded = git(publisher, "rev-parse", "HEAD")
            git(publisher, "push", "-q", "origin", "main")
            update = f"{prefix}.updater"
            output = repo / "logs/update.out.log"
            trigger_update(port, password)
            wait_for_agent(domain, update, output, f"DEPLOY_OK {upgraded}", 900)
            check_web(port, upgraded)
            before = (repo / "frontend/dist/index.html").read_bytes()
            manifest = publisher / "frontend/package.json"
            package = json.loads(manifest.read_text())
            package["scripts"]["build"] = "exit 37"
            manifest.write_text(json.dumps(package) + "\n")
            git(publisher, "add", "frontend/package.json")
            git(publisher, "commit", "-qm", "Acceptance: intentionally failing build")
            bad = git(publisher, "rev-parse", "HEAD")
            git(publisher, "push", "-q", "origin", "main")
            command(["launchctl", "kickstart", f"{domain}/{update}"])
            wait_for_agent(domain, update, output, f"Rollback to {upgraded}; failed {bad}", 900)
            for _ in range(30):
                try:
                    check_web(port, upgraded)
                    break
                except (OSError, RuntimeError):
                    time.sleep(1)
            check_web(port, upgraded)
            require(git(repo, "rev-parse", "HEAD") == upgraded, "Git HEAD 未回退")
            require((repo / ".webapp-failed-sha").read_text().strip() == bad, "失败 SHA 未被保持")
            require((repo / "frontend/dist/index.html").read_bytes() == before, "前端资源未恢复")
            print("PASS：launchd 启动、真实 PostgreSQL、登录、更新、失败回退与运行 SHA。", flush=True)

            checks += "、手动触发更新及构建失败回退"
        else:
            print("PASS：隔离服务与登录。本次只补验反馈，跳过更新及回退。", flush=True)

        require(sys.stdin.isatty(), "请在终端交互运行，以隐藏输入反馈 Token")
        token = getpass.getpass("反馈专用 GitHub Token（隐藏输入，直接回车可跳过）：").strip()
        if not token:
            require(not require_feedback, "发布验收必须验证真实 GitHub 反馈")
            print("反馈未验收，原因：未提供 Token。", flush=True)
            return
        try:
            issue_url = feedback_check(repo, port, password, domain, prefix, candidate, token, checks)
        except Exception:
            for name in ("feedback.out.log", "feedback.err.log"):
                worker_log = repo / "logs" / name
                if worker_log.is_file():
                    log.write((f"\n--- {name} ---\n" + worker_log.read_text(errors="replace")
                               .replace(token, "[TOKEN REDACTED]")).encode())
            raise
        print(f"PASS：真实反馈 {issue_url}", flush=True)
        if REPORT_ISSUE is not None:
            report = github(token, f"/issues/{REPORT_ISSUE}/comments", {"body": (
                f"Mac {'反馈补验' if feedback_only else '隔离验收'}通过，候选 SHA `{candidate}`。\n\n"
                f"本次通过：{checks}。\n\n"
                f"真实反馈 Issue：{issue_url}\n\n公网切换与 HTTPS 验收待发布后执行。"
            )})
            print(f"验收已回帖 {report['html_url']}", flush=True)
    finally:
        env_file.unlink(missing_ok=True)
        if agent_install_attempted:
            for role in ROLES:
                subprocess.run(["launchctl", "bootout", f"{domain}/{prefix}.{role}"], stdout=log, stderr=log)
        if postgres_started:
            subprocess.run([*pgctl, "-m", "fast", "-w", "stop"], stdout=log, stderr=log)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--feedback-only", action="store_true")
    parser.add_argument("--require-feedback", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(sys.platform == "darwin" and os.getuid() != 0, "请在 Mac 已登录用户的终端运行，勿使用 sudo")
        require(sys.version_info >= (3, 10), "需要 Python 3.10 或以上")
        domain = f"gui/{os.getuid()}"
        command(["launchctl", "print", domain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.environ["PATH"] = f"/opt/homebrew/bin:/usr/local/bin:{os.environ['PATH']}"
        require(shutil.which("npm") and shutil.which("node"), "需要 Node.js 和 npm")
        pg = postgres_bin()
        port = pick_port()
        with tempfile.TemporaryDirectory(prefix="webapp-check-", dir="/tmp") as path:
            with tempfile.NamedTemporaryFile(prefix="webapp-acceptance-", suffix=".log", dir=str(Path.home()), delete=False) as log:
                try:
                    run_acceptance(Path(path), log, pg, domain, port, args.feedback_only, args.require_feedback)
                except (RuntimeError, OSError, subprocess.CalledProcessError, KeyError, StopIteration, ValueError):
                    print(f"本机诊断日志 {log.name}（请先检查是否含本机路径，再决定分享范围）", file=sys.stderr)
                    raise
            Path(log.name).unlink()
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError, KeyError, StopIteration, ValueError) as error:
        print(f"验收中止：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
