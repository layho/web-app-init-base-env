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
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        migrate(args.root, args.apply)
    except (ValueError, OSError) as exc:
        raise SystemExit("安全停止：" + str(exc))
