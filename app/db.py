import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./webapp.db")
_SQLITE = DATABASE_URL.startswith("sqlite")
# A file database gets a new connection per session. Memory URLs stay on the
# default pool because each NullPool connection would be a different database.
_FILE_SQLITE = _SQLITE and ":memory:" not in DATABASE_URL and DATABASE_URL not in {"sqlite://", "sqlite:///"}
connect_args = {"check_same_thread": False, "timeout": 30} if _SQLITE else {}
_pool = {"poolclass": NullPool} if _FILE_SQLITE else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args, **_pool)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _configure_sqlite(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout=5000")
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    cursor.close()


if _SQLITE:
    event.listen(engine, "connect", _configure_sqlite)


def serialize_sqlite(target):
    """Configure SQLite for callers that still share a process.

    A cursor lock does not cover commit or fetch, and a transaction lock cannot
    be released by another thread on a shared connection. Isolation comes from
    giving each session its own connection. This helper only sets the busy
    timeout and WAL mode.
    """

    if target.dialect.name != "sqlite":
        return
    event.listen(target, "connect", _configure_sqlite)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db
