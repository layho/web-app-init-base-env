import os
from threading import Lock, RLock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./webapp.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
# One SQLite connection cannot serve overlapping statements. A concurrent
# session lookup can miss a committed row and answer 401. The lock covers
# only the cursor call, so a test that already holds a session can still
# issue another request. Postgres does not use this lock.
_sqlite_lock = RLock()


def serialize_sqlite(target):
    """Serialize cursor use of one SQLite engine. No-op for other dialects."""

    if target.dialect.name != "sqlite":
        return
    lock = Lock()

    @event.listens_for(target, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        del cursor, statement, parameters, context, executemany
        lock.acquire()
        conn.info["_sqlite_cursor_lock"] = lock

    @event.listens_for(target, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        del cursor, statement, parameters, context, executemany
        if conn.info.pop("_sqlite_cursor_lock", None) is lock:
            lock.release()

    @event.listens_for(target, "handle_error")
    def _error(context):
        conn = context.connection
        if conn is not None and conn.info.pop("_sqlite_cursor_lock", None) is lock:
            lock.release()


serialize_sqlite(engine)

class Base(DeclarativeBase):
    pass

def get_db():
    if DATABASE_URL.startswith("sqlite"):
        with _sqlite_lock:
            with SessionLocal() as db:
                yield db
        return
    with SessionLocal() as db:
        yield db
