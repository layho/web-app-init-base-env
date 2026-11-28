"""Core/APP path boundary. Classification never writes a file."""

APP_DATA_PREFIXES = ("data/", "logs/", "secrets/")
APP_DATA_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12")
APP_CONFIG = {".app-init.json", "README.md", "app/project_info.json", "app/release_notes.json", ".env"}
CUSTOM_PREFIXES = ("app_modules/", "frontend/src/app-modules/", "migrations/modules/")
BANNED_ALLOWLIST = {
    "app",
    "app/",
    "app/*",
    "app/**",
    "frontend",
    "frontend/",
    "frontend/src",
    "frontend/src/",
    "frontend/src/*",
    "frontend/src/**",
    "*",
    "**",
}
PRESERVE = {"app_data", "app_config", "app_custom"}


def safe_relative(path):
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\0" in path:
        return False
    if path != path.strip():
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def allowlist_problem(allowlist):
    if not isinstance(allowlist, list):
        return "blanket_ownership"
    for item in allowlist:
        if not isinstance(item, str) or item in BANNED_ALLOWLIST or item.endswith("/") or "*" in item:
            return "blanket_ownership"
        if not safe_relative(item):
            return "blanket_ownership"
    return None


def classify_path(path, allowlist):
    if not safe_relative(path):
        return "rejected"
    folded = path.casefold()
    if folded.startswith(APP_DATA_PREFIXES) or folded.endswith(APP_DATA_SUFFIXES):
        return "app_data"
    if path in APP_CONFIG or folded.endswith(".env"):
        return "app_config"
    if any(folded.startswith(prefix.casefold()) for prefix in CUSTOM_PREFIXES):
        return "app_custom"
    if path in allowlist:
        return "foundation"
    if folded.startswith("app/") or folded.startswith("frontend/src/"):
        return "app_custom"
    return "unknown"


def case_collision(paths):
    seen = {}
    for path in paths:
        if not isinstance(path, str):
            continue
        key = path.casefold()
        if key in seen and seen[key] != path:
            return True
        seen[key] = path
    return False
