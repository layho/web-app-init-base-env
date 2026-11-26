"""Outbound heartbeat client. No remote execution or inbound listener."""
import argparse
import json
import os
import re
import stat
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError

from diagnostics import execute


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def config():
    host = os.getenv("HOSTOPS_HOST_ID", "")
    origin = os.getenv("HOSTOPS_GATEWAY_ORIGIN", "")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c.isspace() for c in origin)):
        raise ValueError("HTTPS gateway origin required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", host):
        raise ValueError("invalid host identity")
    path = os.getenv("HOSTOPS_AGENT_TOKEN_FILE", "")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 512):
            raise ValueError("unsafe credential file")
        token = stream.read(513).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("invalid credential")
    return host, origin.rstrip("/"), token


def heartbeat_payload(host):
    snap = execute("agent.snapshot", {})["result"]
    health = snap["health"]
    return {"host_id": host, "system": health["system"],
            "architecture": health["architecture"], "disk": health["disk"],
            "services": snap["services"], "capabilities": snap["capabilities"]}


def send_heartbeat(host, origin, token, opener=None):
    body = json.dumps(heartbeat_payload(host), ensure_ascii=True).encode("ascii")
    if len(body) > 16384:
        raise ValueError("heartbeat exceeds bound")
    request = Request(origin + "/api/hostops/v1/agent/heartbeat", data=body,
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"}, method="POST")
    with (opener or build_opener(NoRedirect())).open(request, timeout=10) as response:
        raw = response.read(4097)
        if len(raw) > 4096:
            raise ValueError("response exceeds bound")
        result = json.loads(raw)
    if type(result) is not dict or result.get("accepted") is not True or result.get("schema") != 1:
        raise ValueError("heartbeat not acknowledged")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        host, origin, token = config()
    except (OSError, ValueError):
        print("HostOps agent configuration unavailable", flush=True)
        return 2
    delay = 15
    while True:
        try:
            send_heartbeat(host, origin, token)
            print("HostOps heartbeat accepted", flush=True)
            delay = 15
            if args.once:
                return 0
        except (OSError, URLError, ValueError):
            print("HostOps heartbeat unavailable; retry deferred", flush=True)
            delay = min(delay * 2, 60)
            if args.once:
                return 1
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
