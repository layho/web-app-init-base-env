"""Fail-safe TCP port ownership preflight for App Init macOS services.

A successful bind is only a snapshot. Call this both before registering a
service and immediately before launching uvicorn; the server bind/health check
remains authoritative.
"""
import argparse
import errno
from pathlib import Path
import socket
import subprocess
import sys


def _command(*argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)


def _owner_pid(root):
    file = root / "logs" / "web.pid"
    if file.is_symlink() or not file.is_file():
        return None
    try:
        value = file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return int(value) if value.isdecimal() and int(value) > 0 else None


def _listener_pids(port):
    try:
        result = _command("lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"端口 {port} 已被占用，但无法查询监听进程：{error}") from error
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"端口 {port} 已被占用，但 lsof 未能确定监听进程；为安全起见拒绝启动")
    values = result.stdout.split()
    if not values or not all(pid.isdecimal() and int(pid) > 0 for pid in values):
        raise RuntimeError(f"端口 {port} 的监听进程信息无效；拒绝启动")
    return {int(pid) for pid in values}


def _belongs_to_app(pid, root, port):
    try:
        cwd = _command("lsof", "-nP", "-a", "-p", str(pid), "-d", "cwd", "-Fn")
        command = _command("ps", "-p", str(pid), "-o", "command=")
    except (OSError, subprocess.TimeoutExpired):
        return False
    if cwd.returncode or command.returncode:
        return False
    directories = [line[1:] for line in cwd.stdout.splitlines() if line.startswith("n")]
    if len(directories) != 1 or Path(directories[0]).resolve() != root.resolve():
        return False
    expected = f"uvicorn app.main:app --host 127.0.0.1 --port {port} --workers 1"
    return expected in command.stdout


def check_port(root, port):
    """Return 'free' or 'owned'; never reassign ports or terminate processes."""
    root = Path(root).resolve()
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
        return "free"
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise RuntimeError(f"无法检测 127.0.0.1:{port}：{error}") from error

    pids = _listener_pids(port)
    owner = _owner_pid(root)
    if len(pids) == 1 and owner in pids and _belongs_to_app(owner, root, port):
        return "owned"

    details = ", ".join(str(pid) for pid in sorted(pids))
    raise RuntimeError(
        f"端口冲突：127.0.0.1:{port} 已被其他服务占用（监听 PID：{details}）；"
        "未更改端口、未终止进程。请检查占用来源后重试。"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="校验 Web 服务端口是否空闲或属于当前应用")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        status = check_port(args.root, args.port)
    except (RuntimeError, ValueError) as error:
        print(f"✗ 端口预检失败：{error}", file=sys.stderr, flush=True)
        return 1
    message = "空闲" if status == "free" else "属于当前应用的已运行服务"
    print(f"✓ 端口 127.0.0.1:{args.port}：{message}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
