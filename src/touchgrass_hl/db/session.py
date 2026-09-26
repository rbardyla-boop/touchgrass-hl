"""Engine, WAL mode, and sessions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from touchgrass_hl.db.schema import Base, SchemaMeta

SCHEMA_VERSION = 1


def sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    raw = database_url[len("sqlite:///") :]
    if raw == ":memory:" or raw.startswith("file:"):
        return None
    return Path(raw)


def make_engine(database_url: str) -> Engine:
    path = sqlite_path(database_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, future=True, connect_args=connect_args)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        row = session.get(SchemaMeta, 1)
        if row is None:
            session.add(SchemaMeta(id=1, version=SCHEMA_VERSION))
            session.commit()
        elif row.version != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported schema version {row.version}; this build expects {SCHEMA_VERSION}"
            )


def integrity_ok(engine: Engine) -> tuple[bool, str]:
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA integrity_check")).scalar()
        journal = conn.execute(text("PRAGMA journal_mode")).scalar()
    ok = str(result).lower() == "ok"
    return ok, f"integrity={result} journal={journal}"


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def journal_mode(engine: Engine) -> str:
    with engine.connect() as conn:
        value = conn.execute(text("PRAGMA journal_mode")).scalar()
    return str(value)


def schema_version(engine: Engine) -> int | None:
    with Session(engine) as session:
        row = session.scalar(select(SchemaMeta.version).where(SchemaMeta.id == 1))
    return None if row is None else int(row)
