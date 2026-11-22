"""Owner-only runtime settings stored outside tracked source and build assets.

No database migration and no .env rewrite. Password hash is never exposed by APIs.
"""
import json
import errno
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile

SETTINGS = Path(__file__).resolve().parents[1] / "logs" / "admin-settings.json"
TITLE = re.compile(r"[^\x00-\x1f\x7f]{1,80}\Z")


def initial():
    if os.getenv("APP_BASE_PREVIEW") == "1":
        return "基座验收中心"
    path = Path(__file__).with_name("project_info.json")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        name = info.get("name")
    except (OSError, ValueError, TypeError):
        name = None
    return name if isinstance(name, str) and TITLE.fullmatch(name) else "Web App Foundation"


def read():
    try:
        parent = SETTINGS.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o022):
            raise ValueError("Unsafe settings parent")
        try:
            fd = os.open(SETTINGS, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("Unsafe settings symlink") from None
            raise
        with os.fdopen(fd, encoding="utf-8") as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or st.st_mode & 0o077 or st.st_size > 8192):
                raise ValueError("Unsafe settings permissions")
            obj = json.load(stream)
        if (not isinstance(obj, dict) or obj.get("schema") != 1
                or set(obj) != {"schema", "workspace_name", "reference_name", "password_hash"}
                or not all(isinstance(obj[k], str) and TITLE.fullmatch(obj[k])
                           for k in ("workspace_name", "reference_name"))
                or not isinstance(obj["password_hash"], str)
                or len(obj["password_hash"]) > 300):
            raise ValueError("Invalid settings")
        return obj
    except FileNotFoundError:
        return {"schema": 1, "workspace_name": initial(), "reference_name": initial(), "password_hash": ""}


def public():
    obj = read()
    return {"workspace_name": obj["workspace_name"], "reference_name": obj["reference_name"]}


def validate_name(value):
    if not isinstance(value, str) or not TITLE.fullmatch(value.strip()) or value != value.strip():
        raise ValueError("名称须为 1–80 个非控制字符，不得包含首尾空格")
    return value


def save(obj):
    parent = SETTINGS.parent
    parent.mkdir(parents=True, exist_ok=True)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Unsafe settings directory")
    # Reject an existing symlink, even though os.replace itself does not follow it.
    try:
        old = SETTINGS.lstat()
        if not stat.S_ISREG(old.st_mode) or old.st_uid != os.getuid() or old.st_mode & 0o077:
            raise ValueError("Unsafe existing settings")
    except FileNotFoundError:
        pass
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(obj, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(tmp, SETTINGS)
        finally:
            tmp.unlink(missing_ok=True)
