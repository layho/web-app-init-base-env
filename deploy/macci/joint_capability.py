"""MacCI capability probe for the joint tree.

The probe names missing runtimes. It does not start a container, fall back
to the host, or write a receipt.
"""

from app.control_joint.capability import host_capability


def main() -> int:
    report = host_capability()
    print(report["status"])
    for item in report["missing"]:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
