"""Subprocess probe. Exits after a durable write and before the matching commit or remote call."""

import argparse
import json
from pathlib import Path

from app.control_events.atomic import atomic_write
from app.control_events.engine import Engine


def _remote(root: Path):
    marker = root / "remote-called.json"

    def remote(channel, report):
        atomic_write(marker, json.dumps({"channel": channel, "run_id": report.get("run_id")}).encode())

    return remote


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--mode", required=True, choices=("before-commit", "after-file", "before-remote"))
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    root = Path(args.root)
    engine = Engine.open(root, args.token)
    payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
    if args.mode == "before-commit":
        engine.append(payload, crash="before-commit")
    else:
        engine.submit_report(payload["report"], channels=payload["channels"], remote=_remote(root), crash=args.mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
