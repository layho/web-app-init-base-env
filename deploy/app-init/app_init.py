"""Create an application from the shared foundation and install it on macOS."""
import argparse
from email.parser import BytesHeaderParser
from contextlib import contextmanager
from getpass import getpass
import hashlib
import html
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


SOURCE = Path(__file__).resolve().parents[2]
REPO_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
DOMAIN_PATTERN = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def git(path, *args, github_login=False):
    command = ["git", "-C", path]
    if github_login:
        command += ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    return subprocess.check_output([*command, *args], text=True).strip()


def require_github_push(repository, install):
    if not shutil.which("gh"):
        need(install and shutil.which("brew"), "请安装 GitHub CLI（gh）后重试")
        subprocess.run(["brew", "install", "gh"], check=True)
    command = ["gh", "api", "--hostname", "github.com", f"repos/{repository}", "--jq", ".permissions.push"]

    def can_push():
        result = subprocess.run(command, capture_output=True, text=True)
        return result.returncode == 0 and result.stdout.strip() == "true"

    if not can_push():
        print(f"请在浏览器登录对 {repository} 有推送权限的 GitHub 账户。", flush=True)
        subprocess.run(["gh", "auth", "login", "--hostname", "github.com",
                        "--git-protocol", "https", "--web"], check=True)
        need(can_push(), f"当前 GitHub 账户无法推送到 {repository}，请确认仓库已经创建且该账号有写权限")


def configure_service_git(root, repository, token, proxy):
    credential_file = root / ".git" / "app-init-credentials"
    subprocess.run(
        ["git", "credential-store", "--file", str(credential_file), "store"],
        input=f"protocol=https\nhost=github.com\npath={repository}.git\nusername=x-access-token\npassword={token}\n\n",
        text=True, check=True,
    )
    credential_file.chmod(0o600)
    git(root, "config", "--local", "--replace-all", "credential.helper", "")
    git(root, "config", "--local", "--add", "credential.helper", "store --file=" + shlex.quote(str(credential_file)))
    git(root, "config", "--local", "credential.useHttpPath", "true")
    if proxy:
        git(root, "config", "--local", "http.proxy", proxy)


def copy_foundation(source, target, repository, title, domain, port, label):
    paths = [
        ".gitignore", "requirements.txt", "app/__init__.py", "app/auth.py", "app/db.py",
        "app/development_tasks.py", "app/main.py", "app/models.py", "app/operations.py",
        "migrations/002_admin_sessions.sql", "migrations/003_feedback.sql",
        "scripts/apply_migrations.py", "scripts/hash_admin_password.py", "scripts/sync_feedback.py",
        "scripts/sync_development_tasks.py", "deploy/macmini/manage.sh", "deploy/macmini/acceptance.py",
        "deploy/macmini/acceptance.sh", "deploy/macmini/release.py", "tests/test_auth.py",
        "tests/test_deploy_script.py", "tests/test_development_tasks.py", "tests/test_feedback.py",
        "tests/test_feedback_issue_body.py",
    ]
    paths += [path.relative_to(source).as_posix() for path in sorted((source / "frontend/src").rglob("*")) if path.is_file()]
    paths += [f"frontend/{path}" for path in ("index.html", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts")]
    for name in paths:
        file = target / name
        file.parent.mkdir(parents=True, exist_ok=True)
        content = (source / name).read_text(encoding="utf-8")
        for old, new in (
            ("example/web-app", repository), ("app.example.com", domain),
            ("com.example.webapp", label), (r"com\.example\.webapp", re.escape(label)),
            ("web-app-foundation-ui", repository.split("/")[1].lower()),
        ):
            content = content.replace(old, new)
        if name in {"app/auth.py", "deploy/macmini/manage.sh", "deploy/macmini/release.py", "frontend/vite.config.ts"}:
            content = content.replace("18080", str(port))
        elif name == "frontend/src/main.tsx":
            content = content.replace("'Web App Foundation'", json.dumps(title, ensure_ascii=False))
        elif name == "frontend/index.html":
            content = content.replace("Web App Foundation", html.escape(title))
        elif name in {"app/main.py", "tests/test_auth.py"}:
            content = content.replace('"Web App Foundation"', json.dumps(title, ensure_ascii=False))
        file.write_text(content, encoding="utf-8")
    project = json.loads((source / "app/project_info.json").read_text(encoding="utf-8"))
    project.update(name=title, summary="应用基座已初始化。可在意见反馈登记需求。", tasks=[])
    (target / "app/project_info.json").write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notes = [{"version": "0.1.0", "title": "应用初始化", "changes": ["创建登录、工作台和应用基座。"],
              "validation": "验证登录、运行版本、自动更新和反馈同步。"}]
    (target / "app/release_notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / ".app-init.json").write_text(json.dumps({
        "repository": repository, "domain": domain, "port": port, "label": label,
    }, indent=2) + "\n", encoding="utf-8")
    (target / "README.md").write_text(f"# {title}\n\n应用基座。开发需求通过意见反馈登记。\n", encoding="utf-8")


def sync_rollback_test_fix(root):
    path = "tests/test_deploy_script.py"
    if git(root, "rev-parse", "--abbrev-ref", "HEAD") != "main":
        return
    old = git(root, "rev-parse", "HEAD")
    if not git(root, "ls-tree", old, "--", path):
        return
    if git(root, "rev-parse", f"{old}:{path}") != "82c7012bca039e0cc0a75ce10eecde4a257f04aa":
        return
    git(root, "fetch", "--quiet", "origin", "refs/heads/main")
    new = git(root, "rev-parse", "FETCH_HEAD")
    if git(root, "diff", "--name-only", old, new) == path and (
        git(root, "rev-parse", f"{new}:{path}") == git(SOURCE, "rev-parse", f"HEAD:{path}")
    ):
        need(not git(root, "status", "--porcelain", "--", path), "回滚测试文件有本地修改，无法自动同步")
        git(root, "merge", "--ff-only", new)
        print(f"已同步部署回滚测试修复，应用 SHA：{new}", flush=True)


def seed(source, remote, target, repository, title, domain, port, label, *, github_login=False):
    refs = git(source, "ls-remote", remote, github_login=github_login)
    if refs:
        git(source, "clone", "--branch", "main", remote, target, github_login=github_login)
    else:
        git(source, "clone", remote, target, github_login=github_login)
        git(target, "symbolic-ref", "HEAD", "refs/heads/main")
        copy_foundation(source, target, repository, title, domain, port, label)
        git(target, "add", ".")
        git(target, "-c", "user.name=App Init", "-c", "user.email=app-init@localhost",
            "commit", "--no-gpg-sign", "-m", "Initialize application foundation")
        if git(source, "ls-remote", remote, github_login=github_login):
            raise RuntimeError("仓库在初始化期间出现提交，请检查后重试")
        git(target, "push", "-u", "origin", "main", github_login=github_login)
    return json.loads((target / ".app-init.json").read_text(encoding="utf-8"))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method, url, *, token=None, data=None, headers=None):
    request_headers = {"User-Agent": "application-initializer", **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                  headers=request_headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=8) as response:
            return json.load(response), response.headers
    except HTTPError as exc:
        raise RuntimeError(f"{method} {url} 返回 HTTP {exc.code}") from None


def cloudflare(config, method, path, data=None):
    payload, _ = request(method, "https://api.cloudflare.com/client/v4" + path,
                         token=config["api_token"], data=data)
    need(payload["success"], f"Cloudflare 请求失败：{method} {path}")
    return payload["result"]


def map_domain(config, domain, port, *, apply=False):
    zone = config["zone_id"]
    tunnel = config["tunnel_id"]
    tunnel_path = f"/accounts/{config['account_id']}/cfd_tunnel/{tunnel}/configurations"
    dns_path = f"/zones/{zone}/dns_records"
    alias = f"{tunnel}.cfargotunnel.com"
    service = f"http://127.0.0.1:{port}"
    records = cloudflare(config, "GET", f"{dns_path}?{urlencode({'name': domain})}")
    need(isinstance(records, list), "Cloudflare DNS 响应格式不正确")
    need(not records or (len(records) == 1 and records[0].get("type") == "CNAME"
                         and records[0].get("content", "").rstrip(".") == alias and records[0].get("proxied")),
         f"{domain} 的 DNS 记录已指向其他服务")
    remote = cloudflare(config, "GET", tunnel_path)["config"]
    ingress = remote.get("ingress", [])
    need(ingress and not ingress[-1].get("hostname") and not ingress[-1].get("path"), "Tunnel 缺少最终兜底规则")
    assigned = [rule for rule in ingress if rule.get("hostname") == domain]
    if assigned:
        need(len(assigned) == 1 and assigned[0].get("service") == service and not assigned[0].get("path"),
             f"{domain} 已映射到其他回源")
    if apply and not assigned:
        cloudflare(config, "PUT", tunnel_path, {"config": {
            **remote, "ingress": [{"hostname": domain, "service": service}, *ingress],
        }})
    if apply and not records:
        cloudflare(config, "POST", dns_path, {"type": "CNAME", "name": domain, "content": alias, "proxied": True})


def private_json(path):
    need(not path.is_symlink() and path.stat().st_uid == os.getuid()
         and not stat.S_IMODE(path.stat().st_mode) & 0o077, f"配置文件必须由当前用户持有且权限为 600：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_cloudflare_config(path):
    existing = path.exists() or path.is_symlink()
    if existing:
        config = private_json(path)
    else:
        config = {
            "account_id": input("Cloudflare 账户 ID：").strip(),
            "zone_id": input("Cloudflare 区域 ID：").strip(),
            "tunnel_id": input("这台 Mac 的 Cloudflare 隧道 ID：").strip(),
            "api_token": getpass("Cloudflare API 令牌（隐藏输入）：").strip(),
        }
    patterns = {
        "account_id": r"[0-9a-f]{32}",
        "zone_id": r"[0-9a-f]{32}",
        "tunnel_id": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
        "api_token": r"[A-Za-z0-9_-]+",
    }
    for key, pattern in patterns.items():
        value = config.get(key)
        need(isinstance(value, str) and re.fullmatch(pattern, value), f"Cloudflare {key} 格式不正确")
    if not existing:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(config, out, indent=2)
            out.write("\n")
    return config


def shell_config(path, values):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        for key, value in values.items():
            out.write(f"{key}={shlex.quote(value)}\n")


def pg_bin():
    binary = shutil.which("initdb")
    candidates = [Path(binary).parent] if binary else []
    for prefix in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
        candidates += [path / "bin" for path in sorted(prefix.glob("postgresql*"), reverse=True)]
    for path in candidates:
        if all(os.access(path / command, os.X_OK) for command in ("initdb", "postgres", "pg_isready")):
            return path
    raise RuntimeError("找不到 PostgreSQL 的 initdb、postgres 或 pg_isready")


def check_dependencies(install):
    formulas = []
    node_check = (
        'let [a,b]=process.versions.node.split(".").map(Number);'
        'process.exit(((a===20&&b>=19)||(a===22&&b>=12)||a>=24)?0:1)'
    )
    node = shutil.which("node", path=SERVICE_PATH)
    if not node or not shutil.which("npm", path=SERVICE_PATH) or subprocess.run([node, "-e", node_check]).returncode:
        formulas.append("node")
    try:
        pg_bin()
    except RuntimeError:
        formulas.append("postgresql@17")
    if formulas:
        command = ["brew", "install", *formulas]
        need(install, "缺少兼容的依赖，请用 --install-deps 重试，或运行 " + shlex.join(command))
        need(shutil.which("brew"), "请先安装 Homebrew，然后重试 --install-deps")
        subprocess.run(command, check=True)
        check_dependencies(install=False)


LAUNCH_DAEMONS = Path("/Library/LaunchDaemons")


def install_job(root, label, args, *, interval=None, keepalive=False, watch=None):
    plist = LAUNCH_DAEMONS / f"{label}.plist"
    payload = {
        "Label": label, "ProgramArguments": [str(arg) for arg in args],
        "UserName": pwd.getpwuid(os.getuid()).pw_name, "WorkingDirectory": str(root), "RunAtLoad": True,
        "StandardOutPath": str(root / "logs" / f"{label}.out"),
        "StandardErrorPath": str(root / "logs" / f"{label}.err"),
        "EnvironmentVariables": {
            "HOME": str(Path.home()), "PATH": SERVICE_PATH,
            "APP_INIT_SYSTEM_SERVICE": "1",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
        },
    }
    if keepalive:
        payload["KeepAlive"] = True
        payload["ThrottleInterval"] = 10
    if interval:
        payload["StartInterval"] = interval
    if watch:
        payload["WatchPaths"] = [str(watch)]
    legacy = plistlib.dumps(payload)
    if label.endswith(".db"):
        payload["EnvironmentVariables"]["LC_ALL"] = "C"
    expected = plistlib.dumps(payload)
    write_plist = True
    if plist.exists():
        current = plist.read_bytes()
        need(current in (legacy, expected), f"已存在不同内容的服务配置：{plist.name}")
        write_plist = current != expected
        active = subprocess.run(["launchctl", "print", f"system/{label}"], capture_output=True)
        if active.returncode == 0:
            if not write_plist:
                return
            subprocess.run(["sudo", "launchctl", "bootout", f"system/{label}"], check=True)
    if write_plist:
        with TemporaryDirectory() as directory:
            temporary = Path(directory) / "job.plist"
            temporary.write_bytes(expected)
            subprocess.run(["sudo", "install", "-o", "root", "-g", "wheel", "-m", "644",
                            temporary, plist], check=True)
    subprocess.run(["sudo", "launchctl", "bootstrap", "system", plist], check=True)


def wait_for(description, check, timeout=120, *, error_log=None):
    print(f"正在验证{description}（最长 {timeout} 秒）…", flush=True)
    end = time.monotonic() + timeout
    next_update = time.monotonic() + 15
    last_error = ""
    while time.monotonic() < end:
        try:
            if check():
                print(f"通过：{description}", flush=True)
                return
            last_error = ""
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = f"（{exc}）"
        if time.monotonic() >= next_update:
            print(f"仍在等待{description}{last_error}…", flush=True)
            next_update = time.monotonic() + 15
        time.sleep(2)
    detail = f"；请运行 tail -n 40 {shlex.quote(str(error_log))}" if error_log else "，请检查应用目录下的 logs"
    raise RuntimeError(f"{description}未通过{last_error}{detail}")


def version_matches(origin, sha):
    version, _ = request("GET", f"{origin}/api/version")
    return version.get("running_sha") == sha


def public_request(method, url, *, data=None, headers=None):
    # Use the Mac's curl TLS stack. Keep credentials out of process arguments.
    with TemporaryDirectory() as directory:
        head = Path(directory) / "response-headers"
        outgoing = Path(directory) / "request-headers"
        fields = {"Content-Type": "application/json", **(headers or {})}
        need(all("\r" not in v and "\n" not in v for v in fields.values()), "请求头格式异常")
        outgoing.write_text("".join(f"{k}: {v}\n" for k, v in fields.items()))
        cmd = ["/usr/bin/curl", "--disable", "--silent", "--proto", "=https", "--max-time", "15",
               "--dump-header", str(head), "--header", "@" + str(outgoing), "--request", method]
        if data is not None:
            cmd += ["--data-binary", "@-"]
        response = subprocess.run([*cmd, url], input=json.dumps(data).encode() if data is not None else b"",
                                  capture_output=True)
        need(response.returncode == 0, f"{url} 连接失败，curl 错误码 {response.returncode}")
        block = head.read_bytes().replace(b"\r\n", b"\n").strip().split(b"\n\n")[-1]
        status, _, raw_headers = block.partition(b"\n")
        code = int(status.split()[1])
        need(200 <= code < 300, f"{url} 返回 HTTP {code}")
        try:
            return json.loads(response.stdout), BytesHeaderParser().parsebytes(raw_headers)
        except (ValueError, UnicodeError):
            raise RuntimeError(f"{url} 返回内容无法解析为 JSON") from None


def verify_login(origin, username, password):
    from http.cookies import SimpleCookie

    headers = {"Origin": origin}
    result, response_headers = public_request("POST", f"{origin}/api/login",
                                       data={"username": username, "password": password}, headers=headers)
    need(result.get("authenticated") is True, "公网登录未通过")
    cookies = SimpleCookie()
    for value in response_headers.get_all("Set-Cookie", []):
        cookies.load(value)
    cookies = {key: cookie for key, cookie in cookies.items() if key == "webapp_session"}
    need(cookies and all(cookie["secure"] and cookie["httponly"] for cookie in cookies.values()), "公网 Cookie 属性不正确")
    headers["Cookie"] = "; ".join(f"{name}={cookie.value}" for name, cookie in cookies.items())
    session, _ = public_request("GET", f"{origin}/api/session", headers=headers)
    need(session.get("username") == username, "公网会话恢复未通过")
    headers["X-CSRF-Token"] = session["csrf_token"]
    return headers


def verify_feedback(root, origin, headers, repository):
    receipt = root / "logs" / "init-feedback-id"
    if receipt.exists():
        feedback_id = int(receipt.read_text(encoding="utf-8"))
    else:
        result, _ = public_request("POST", f"{origin}/api/v1/feedback", headers=headers, data={
            "category": "acceptance", "message": "应用初始化验收：检查 GitHub Issues 同步。",
        })
        feedback_id = result["id"]
        receipt.write_text(str(feedback_id), encoding="utf-8")

    synced = None

    def check():
        nonlocal synced
        records, _ = public_request("GET", f"{origin}/api/v1/feedback", headers=headers)
        for record in records:
            if record["id"] == feedback_id and record["status"] == "synced":
                synced = record["github_issue_number"]
                return isinstance(synced, int) and synced > 0
        return False

    wait_for("GitHub 反馈同步", check)
    return f"https://github.com/{repository}/issues/{synced}"


def deploy(root, meta, password, username, token, config):
    port, label, domain = meta["port"], meta["label"], meta["domain"]
    track = meta.get("track", "main")
    need(track in ("main", "preview"), "不支持的部署分支")
    need(git(root, "branch", "--show-current") == track, f"本地应用必须位于 {track} 分支")
    need(not git(root, "status", "--porcelain", "--untracked-files=no"), "本地应用含未提交的修改")
    postgres = pg_bin()
    env = {**os.environ, "APP_INIT_SYSTEM_SERVICE": "1", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
           "GIT_TERMINAL_PROMPT": "0"}
    env.pop("SSH_AUTH_SOCK", None)
    access = subprocess.run(["git", "-C", root, "ls-remote", "origin", f"refs/heads/{track}"],
                            env=env, capture_output=True, text=True)
    need(access.returncode == 0 and access.stdout,
         f"后台无法拉取 {track}，请检查仓库读权限和代理。HTTPS 模式的仓库令牌需要 Contents 读取权限")
    map_domain(config, domain, port)
    repository = meta["repository"]
    request("GET", f"https://api.github.com/repos/{repository}/issues?per_page=1", token=token)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    # Use an independent Unix socket directory with a short path.
    socket_dir = Path("/private/tmp") / label
    cluster = root / ".postgres"
    if not (cluster / "PG_VERSION").is_file():
        subprocess.run([postgres / "initdb", "-D", cluster, "-A", "trust", "-U", "app_owner"], check=True)
    if not socket_dir.exists():
        socket_dir.mkdir(mode=0o700)
    need(not socket_dir.is_symlink() and socket_dir.stat().st_uid == os.getuid()
         and not stat.S_IMODE(socket_dir.stat().st_mode) & 0o077, "数据库 socket 目录不安全")

    # macOS clears its temporary directory on boot. Recreate the private socket
    # directory from the database job, before starting the foreground server.
    start_db = [
        "/bin/bash", "-c",
        'umask 077; mkdir -p "$1" && exec "$2" -D "$3" -h "" -k "$1"',
        "app-init-postgres", socket_dir, postgres / "postgres", cluster,
    ]
    print("正在注册数据库系统服务。如出现 sudo 的 Password:，请输入当前 Mac 管理员的登录密码。", flush=True)
    install_job(root, f"{label}.db", start_db, keepalive=True)
    wait_for("数据库启动", lambda: subprocess.run(
        [postgres / "pg_isready", "-h", socket_dir, "-U", "app_owner", "-d", "postgres"], capture_output=True,
    ).returncode == 0, error_log=logs / f"{label}.db.err")

    if not (root / ".env").exists():
        salt = os.urandom(16)
        rounds = 310_000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
        shell_config(root / ".env", {
            "DATABASE_URL": f"postgresql+psycopg://app_owner@/postgres?{urlencode({'host': str(socket_dir)})}",
            "APP_ADMIN_USERNAME": username,
            "APP_ADMIN_PASSWORD_HASH": f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}",
            "APP_PUBLIC_ORIGIN": f"https://{domain}", "APP_COOKIE_SECURE": "true",
            "APP_ALLOW_LOCAL_DEV": "false", "APP_GITHUB_FEEDBACK_TOKEN": token,
        })

    settings = root / ".env"
    need(not settings.is_symlink() and settings.stat().st_uid == os.getuid()
         and not stat.S_IMODE(settings.stat().st_mode) & 0o077, "应用 .env 文件必须由当前用户持有且权限为 600")
    manager = root / "deploy/macmini/manage.sh"
    if not (root / "frontend/dist/index.html").exists():
        venv = root / ".venv"
        if not os.access(venv / "bin/python", os.X_OK):
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        subprocess.run(["bash", manager, "prepare"], env=env, check=True)
    install_job(root, f"{label}.web", ["/bin/bash", manager, "serve"], keepalive=True)
    sha = git(root, "rev-parse", "HEAD")
    wait_for("本机服务", lambda: version_matches(f"http://127.0.0.1:{port}", sha))
    subprocess.run(["bash", manager, "verify"], env=env, check=True)
    signal = logs / "update-request"
    signal.touch(exist_ok=True)
    install_job(root, f"{label}.updater", ["/bin/bash", manager, "update"], interval=300, watch=signal)
    install_job(root, f"{label}.feedback", ["/bin/bash", manager, "feedback"], interval=60)
    map_domain(config, domain, port, apply=True)
    verify_public(root, domain, sha, username, password, repository)


def verify_public(root, domain, sha, username, password, repository):
    origin = f"https://{domain}"

    def check_sha():
        version, _ = public_request("GET", f"{origin}/api/version")
        actual = version.get("running_sha") if isinstance(version, dict) else None
        observed = actual if isinstance(actual, str) and re.fullmatch("[a-f0-9]{40}", actual) else "缺少有效 SHA"
        need(observed == sha, f"期望 {sha}，公网返回 {observed}")
        return True

    wait_for("公网 SHA 核对", check_sha)
    headers = verify_login(origin, username, password)
    try:
        issue = verify_feedback(root, origin, headers, repository)
    finally:
        public_request("POST", f"{origin}/api/logout", data={}, headers=headers)
    print(f"PASS {origin} 登录、会话与运行 SHA {sha}；反馈验收 {issue}", flush=True)


@contextmanager
def initialization_lock(path):
    import fcntl

    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一项应用初始化正在执行") from None
        yield


def find_port(start):
    for port in range(start, 29000):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("没有可用的应用端口")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-only", action="store_true", help="仅在 GitHub 初始化仓库")
    parser.add_argument("--install-deps", action="store_true", help="用 Homebrew 安装缺失的部署依赖")
    parser.add_argument("--github-https", action="store_true", help="通过 GitHub 浏览器登录初始化仓库")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print("app-init | 修复版 R3（支持单独补验公网）", flush=True)
        print(f"运行基座 SHA：{git(SOURCE, 'rev-parse', 'HEAD')}", flush=True)
        need(args.seed_only or (sys.platform == "darwin" and os.getuid() != 0), "完整初始化请用 macOS 的普通管理员账户运行")
        if not args.seed_only:
            check_dependencies(args.install_deps)
        repo_name = input("GitHub 仓库 (owner/repo)：").strip()
        need(re.fullmatch(REPO_PATTERN, repo_name), "仓库格式应为 owner/repo")
        repo_name = repo_name.lower()
        ssh_remote = f"git@github.com:{repo_name}.git"
        https_remote = f"https://github.com/{repo_name}.git"
        apps = Path.home() / "Applications" / "app-init"
        root = apps / repo_name.replace("/", "--")
        print(f"应用安装目录：{root}", flush=True)
        if root.exists():
            remote = git(root, "remote", "get-url", "origin")
            need(remote in (ssh_remote, https_remote), "本地应用仓库不匹配")
        else:
            remote = https_remote if args.github_https else ssh_remote
            if args.github_https:
                require_github_push(repo_name, args.install_deps)
        domain = input("正式域名：").strip().lower()
        need(re.fullmatch(DOMAIN_PATTERN, domain), "域名格式不正确")
        label = "com.appinit." + hashlib.sha256(repo_name.lower().encode()).hexdigest()[:12]
        title = repo_name.split("/")[1]
        apps.mkdir(parents=True, exist_ok=True)
        if not args.seed_only and (root / "frontend/dist/index.html").is_file():
            if input("已有构建，仅补验公网登录和反馈？[Y/n]：").strip().lower() in ("", "y", "yes"):
                meta = json.loads((root / ".app-init.json").read_text())
                need(meta["repository"] == repo_name and meta["domain"] == domain, "输入与现有应用不一致")
                username = input("已有管理员用户名：").strip()
                password = getpass("已有管理员密码（隐藏输入）：")
                with initialization_lock(apps / ".init-lock"):
                    verify_public(root, domain, git(root, "rev-parse", "HEAD"), username, password, repo_name)
                return 0
        config = None
        if not args.seed_only:
            config_path = Path.home() / ".config" / "app-init" / "cloudflare.json"
            config = load_cloudflare_config(config_path)
            username = input("管理员用户名：").strip()
            need(0 < len(username) <= 128, "管理员用户名不能为空")
            password = getpass("管理员密码（隐藏输入）：")
            need(12 <= len(password) <= 256, "密码须为 12–256 个字符")
            permissions = "Issues 读写 + Contents 读取" if remote == https_remote else "Issues 读写"
            token = getpass(f"目标仓库 GitHub Token（{permissions}，隐藏输入）：").strip()
            need(re.fullmatch(r"[A-Za-z0-9_-]+", token), "仓库 Token 格式不正确")
        with initialization_lock(apps / ".init-lock"):
            if not root.exists():
                github_login = remote == https_remote
                refs = git(SOURCE, "ls-remote", remote, github_login=github_login)
                port = find_port(18000 + int(label.rsplit(".", 1)[-1], 16) % 8000)
                with TemporaryDirectory(prefix=".app-init-", dir=apps) as path:
                    staging = Path(path) / "repo"
                    if not refs and config:
                        map_domain(config, domain, port)
                    seed(SOURCE, remote, staging, repo_name, title, domain, port, label, github_login=github_login)
                    staging.rename(root)
            meta = json.loads((root / ".app-init.json").read_text(encoding="utf-8"))
            need(meta.get("repository") == repo_name and meta.get("domain") == domain
                 and meta.get("label") == label and 18000 <= meta.get("port", 0) < 29000, "现有应用配置与输入不一致")
            need(git(root, "remote", "get-url", "origin") == remote, "本地应用仓库不匹配")
            if args.seed_only:
                print(f"已生成应用基座：https://github.com/{repo_name}")
            else:
                if remote == https_remote:
                    configure_service_git(root, repo_name, token, os.environ.get("APP_INIT_PROXY"))
                sync_rollback_test_fix(root)
                deploy(root, meta, password, username, token, config)
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, EOFError, subprocess.SubprocessError) as error:
        print(f"初始化中止：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
