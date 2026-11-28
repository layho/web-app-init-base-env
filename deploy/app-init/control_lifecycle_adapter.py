"""Lifecycle adapter entry. It does not install, uninstall, or switch a service."""


def main() -> int:
    print("BLOCKED: app-init lifecycle adapter is not authorized to install or remove anything")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
