#!/usr/bin/env python3
"""One-time, offline migration to APP_GITHUB_TOKEN after base PR release.

Run from the deployed checkout. Dry-run by default. Does not call GitHub or
restart services, and never prints credentials.
"""
import argparse
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
from datetime import datetime

NAMES = ("APP_GITHUB_TOKEN", "APP_GITHUB_PR_TOKEN", "APP_GITHUB_FEEDBACK_TOKEN")
KEY = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def private_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit:
            raise ValueError("凭据文件权限或大小不安全")
        return stream.read()


def shared_migrate(root, apply=False):
    """Inspect and stage a canonical token, never overwrite legacy consumers."""
    from hmac import compare_digest
    root = root.resolve()
    values = {}
    for line in private_read(root / ".env", 1024*1024).splitlines():
        match = KEY.fullmatch(line)
        if not match or match[1] not in (*NAMES, "APP_PUBLIC_ORIGIN"):
            continue
        name, raw = match.groups()
        if name in values:
            raise ValueError("重复配置项")
        parts = shlex.split(raw, comments=True)
        if len(parts) != 1:
            raise ValueError("配置格式无效")
        values[name] = parts[0]
    if values.get("APP_PUBLIC_ORIGIN") != "https://base.layho.com":
        raise ValueError("仅支持基座验收站")
    home = Path.home() / ".local/share"
    token = private_read(home/"macmini-pr-tests/github-token",512).strip()
    scheduler = private_read(home/"macmini-foundation-upgrades/github-token",512).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{20,300}", token) or not compare_digest(token,scheduler):
        raise ValueError("宿主凭据不一致或格式无效，拒绝自动选源")
    for name in NAMES:
        if values.get(name):
            print(name + ("：与宿主一致" if compare_digest(values[name],token) else "：与宿主不同"))
    destination = home / "macmini-github"
    for label, file in (("MacCI",home/"macmini-pr-tests/github-token"),
                        ("升级调度器",home/"macmini-foundation-upgrades/github-token"),
                        ("统一凭据",destination/"github-token")):
        try:
            old = private_read(file,512).strip()
        except FileNotFoundError:
            print(label + "：未配置")
        else:
            print(label + ("：与基座一致" if compare_digest(old,token) else "：与基座不同"))
    if not apply:
        print("只读检查结束；不代表 GraphQL 写权限已验证")
        return
    if not compare_digest(next((values[name] for name in NAMES if values.get(name)), ""), token):
        raise ValueError("基座有效凭据与宿主不同，先核实并统一现有配置")
    # Explicit activation only; never replace a different existing canonical token.
    destination.mkdir(mode=0o700, exist_ok=True)
    info = destination.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError("统一凭据目录权限不安全")
    path = destination/"github-token"
    try:
        existing = private_read(path,512).strip()
    except FileNotFoundError:
        fd, temp = tempfile.mkstemp(prefix=".github-token-", dir=destination)
        try:
            with os.fdopen(fd,"w",encoding="ascii") as stream:
                stream.write(token+"\n")
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic no-overwrite publication, even if another migration runs.
            os.link(temp,path)
        finally:
            os.unlink(temp)
    else:
        if not compare_digest(existing,token):
            raise ValueError("已有不同统一凭据，拒绝覆盖")
    print("统一凭据已准备；原文件未修改。新版服务启用后需逐项验证真实写权限。")


def migrate(root: Path, apply: bool = False) -> Path | None:
    root = root.resolve()
    env = root / ".env"
    if env.is_symlink() or not env.is_file():
        raise ValueError("找不到基座 .env 或配置是符号链接")
    info = env.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(".env 必须属于当前用户且权限为 0600")
    text = env.read_text(encoding="utf-8")
    if len(text) > 1024 * 1024:
        raise ValueError(".env 大小异常")
    values = {}
    for line in text.splitlines():
        m = KEY.fullmatch(line)
        if not m or m.group(1) not in (*NAMES, "APP_PUBLIC_ORIGIN"):
            continue
        name, raw = m.groups()
        if name in values:
            raise ValueError("检测到重复环境变量：" + name)
        parsed = shlex.split(raw, comments=True, posix=True)
        if len(parsed) != 1:
            raise ValueError("配置值格式无效：" + name)
        values[name] = parsed[0]
    if values.get("APP_PUBLIC_ORIGIN") != "https://base.layho.com":
        raise ValueError("该目录不是基座 Preview，拒绝修改")
    token = values.get("APP_GITHUB_TOKEN") or values.get("APP_GITHUB_PR_TOKEN") or values.get("APP_GITHUB_FEEDBACK_TOKEN")
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{20,}", token):
        raise ValueError("没有找到有效格式的 GitHub Token；请先核对现有配置")
    if any(ch in token for ch in "\r\n"):
        raise ValueError("Token 包含非法换行")
    print("配置检查通过：目标 base.layho.com；将保留一个 APP_GITHUB_TOKEN")
    if not apply:
        print("只读预检完成，增加 --apply 才会写入文件")
        return None

    # Require the deployed tree to contain single-token support.
    source = (root / "app/release_acceptance.py").read_text(encoding="utf-8")
    manage = (root / "deploy/macmini/manage.sh").read_text(encoding="utf-8")
    if "def github_token():" not in source or 'APP_GITHUB_TOKEN' not in manage:
        raise ValueError("当前部署目录缺少单 Token 支持；先完成正式发布再迁移")

    backup = root / (".env.backup.github-token-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    with backup.open("x", encoding="utf-8") as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(text)
        out.flush()
        os.fsync(out.fileno())

    revised = "\n".join(line for line in text.splitlines()
                        if not (KEY.match(line) and KEY.match(line).group(1) in NAMES))
    revised = revised.rstrip("\n") + "\nAPP_GITHUB_TOKEN=" + token + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".env.github-token-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(revised)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, env)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("迁移完成：仅保留 APP_GITHUB_TOKEN；旧 .env 已备份（0600）")
    print("下一步按部署规范重启 system/com.layho.basepreview.web 并验证功能")
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shared", action="store_true", help="核对或准备统一宿主凭据")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        if args.shared:
            shared_migrate(args.root, args.apply)
        else:
            migrate(args.root, args.apply)
    except (ValueError, OSError) as exc:
        raise SystemExit("安全停止：" + str(exc))
