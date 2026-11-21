"""Explicit, transactional, additive PostgreSQL migrations, never automatic in updater."""
import argparse
from pathlib import Path
from sqlalchemy import create_engine, text
from app.db import DATABASE_URL

def apply(check_only: bool):
    if not DATABASE_URL.startswith("postgresql+psycopg://"):
        raise SystemExit("PostgreSQL required for deployment migrations")
    engine=create_engine(DATABASE_URL, pool_pre_ping=True)
    folder=Path(__file__).resolve().parents[1]/"migrations"
    paths=sorted(folder.glob("[0-9][0-9][0-9]_*.sql"))
    if not paths: raise SystemExit("No SQL migration files")
    with engine.begin() as db:
        if check_only:
            existing=db.execute(text("SELECT to_regclass('public.schema_migrations')")).scalar()
            if not existing:
                raise SystemExit("No migration ledger. Run manual migration after backup.")
        else:
            db.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
        applied={r[0] for r in db.execute(text("SELECT filename FROM schema_migrations")).all()}
        unexpected=applied-{p.name for p in paths}
        if unexpected: raise SystemExit(f"Unknown applied migrations: {sorted(unexpected)}")
        pending=[p for p in paths if p.name not in applied]
        if check_only:
            if pending: raise SystemExit(f"Pending migration(s): {[p.name for p in pending]}")
            print(f"Schema current: {len(applied)} migrations")
            return
        for path in pending:
            source=path.read_text()
            # SQL files in this repo contain simple semicolon-terminated statements;
            # no procedural SQL or semicolons inside quoted strings.
            for statement in source.split(";"):
                if statement.strip(): db.execute(text(statement))
            db.execute(text("INSERT INTO schema_migrations(filename) VALUES (:name)"),{"name":path.name})
            print("Applied",path.name)
    print("Migration transaction committed")
if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--check",action="store_true")
    args=p.parse_args()
    apply(args.check)
