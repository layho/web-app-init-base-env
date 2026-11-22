"""Install the foundation itself into a dedicated, persistent macOS preview checkout."""
import argparse
from getpass import getpass
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "app-init"))
import app_init

REPOSITORY = "layho/web-app-init-base-env"
REMOTE = f"git@github.com:{REPOSITORY}.git"
DOMAIN = "base.layho.com"
LABEL = "com.layho.basepreview"
TRACK = "preview"


def read_private_port(path):
    app_init.need(not path.is_symlink() and path.stat().st_uid == os.getuid()
                  and stat.S_IMODE(path.stat().st_mode) == 0o600, f"配置必须由当前用户持有且权限为 600：{path}")
    match = re.fullmatch(r"BASE_PREVIEW_PORT=([0-9]{5})\n?", path.read_text(encoding="utf-8"))
    app_init.need(match and 18000 <= int(match[1]) < 29000, "现有验收站端口配置无效")
    return int(match[1])


def check_free(port):
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    except OSError as error:
        raise RuntimeError(f"端口 {port} 不可用；未改变端口或终止其他进程。请检查 lsof -nP -iTCP:{port} -sTCP:LISTEN") from error


def verify_checkout(root):
    app_init.need(not root.is_symlink() and root.is_dir(), "验收目录必须是真实目录")
    app_init.need(app_init.git(root, "rev-parse", "--show-toplevel") == str(root.resolve()), "不得使用其他应用目录")
    app_init.need(app_init.git(root, "remote", "get-url", "origin") == REMOTE, "验收仓库必须使用指定 SSH 地址")
    app_init.need(app_init.git(root, "branch", "--show-current") == TRACK, "验收站必须位于 preview 分支")
    app_init.need(not app_init.git(root, "status", "--porcelain", "--untracked-files=no"), "验收站有未提交修改")
    app_init.need(not (root / ".app-init.json").exists(), "拒绝覆盖普通应用")


def main():
    parser = argparse.ArgumentParser(description="安装或验收独立基座预览站")
    parser.add_argument("--install-deps", action="store_true")
    parser.add_argument("--verify", action="store_true", help="仅核对已运行站点的公网 SHA、登录和反馈")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        app_init.need(sys.platform == "darwin" and os.getuid() != 0, "请以 Mac 普通管理员身份运行，不要 sudo 整个脚本")
        root = Path.home() / "web-app-init-base-env-preview"
        print(f"基座独立验收站：{DOMAIN}\n目录：{root}\n跟踪分支：{TRACK}\nGitHub 仓库为公开仓库，反馈禁止包含秘密。", flush=True)
        settings = Path.home() / ".config" / "app-init"
        settings.mkdir(parents=True, exist_ok=True)
        # Share the existing app-init provisioning lock to avoid simultaneous service/domain allocation.
        locks = Path.home() / "Applications" / "app-init"
        locks.mkdir(parents=True, exist_ok=True)
        with app_init.initialization_lock(locks / ".init-lock"):
            if not root.exists():
                app_init.need(not root.is_symlink() and not args.verify, "验收站尚未安装")
                subprocess.run(["git", "clone", "--branch", TRACK, "--single-branch", REMOTE, root], check=True)
            verify_checkout(root)
            port_file = root / ".base-preview.env"
            private_env = root / ".env"
            if port_file.exists() or port_file.is_symlink():
                port = read_private_port(port_file)
            else:
                app_init.need(not args.verify and not private_env.exists() and not (root / ".postgres").exists(), "已有数据但缺少验收配置；拒绝覆盖")
                port = app_init.find_port(18000)
                app_init.shell_config(port_file, {"BASE_PREVIEW_PORT": str(port)})
            print(f"Web 端口：{port}", flush=True)
            username = input("验收站管理员用户名 [admin]：").strip() or "admin"
            password = getpass("验收站管理员密码（隐藏输入）：")
            app_init.need(0 < len(username) <= 128 and 12 <= len(password) <= 256, "用户名或密码长度不正确")
            sha = app_init.git(root, "rev-parse", "HEAD")
            if args.verify:
                app_init.verify_public(root, DOMAIN, sha, username, password, REPOSITORY)
                return 0
            # Do not touch Cloudflare, database or services if another process owns this port.
            check_free(port)
            app_init.check_dependencies(args.install_deps)
            config = app_init.load_cloudflare_config(settings / "cloudflare.json")
            token = getpass("基座仓库 GitHub Token（Issues 读写，隐藏输入）：").strip()
            app_init.need(re.fullmatch(r"[A-Za-z0-9_-]+", token), "GitHub Token 格式不正确")
            for unsafe in (private_env, root / ".postgres"):
                app_init.need(not unsafe.is_symlink(), f"拒绝使用符号链接：{unsafe}")
            if private_env.exists():
                app_init.need(private_env.stat().st_uid == os.getuid() and stat.S_IMODE(private_env.stat().st_mode) == 0o600,
                              "现有 .env 权限不正确")
            app_init.deploy(root, {"repository": REPOSITORY, "domain": DOMAIN, "port": port,
                                  "label": LABEL, "track": TRACK}, password, username, token, config)
            print(f"验收站启动完成：https://{DOMAIN}；端口 {port}；分支 {TRACK}；SHA {sha}", flush=True)
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, EOFError, subprocess.SubprocessError) as error:
        print(f"验收站部署中止：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
