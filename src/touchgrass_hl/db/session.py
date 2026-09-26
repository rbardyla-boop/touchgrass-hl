"""Engine, WAL mode, and sessions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from touchgrass_hl.db.schema import Base, SchemaMeta, WalletFillRow

SCHEMA_VERSION = 2


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
    preexisting = _raw_schema_version(engine)
    Base.metadata.create_all(engine)
    _migrate(engine, preexisting)


def _raw_schema_version(engine: Engine) -> int | None:
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'")
        ).first()
        if found is None:
            return None
        value = conn.execute(text("SELECT version FROM schema_meta WHERE id=1")).scalar()
    return None if value is None else int(value)


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {str(row[1]) for row in rows}


def _has_table(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": name},
    ).first()
    return row is not None


def _drop_indexes(conn, table: str) -> None:
    indexes = conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:table "
            "AND name NOT LIKE 'sqlite_autoindex%'"
        ),
        {"table": table},
    ).fetchall()
    for (index_name,) in indexes:
        conn.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))


def _copy_wallet_fills(conn) -> None:
    conn.execute(
        text(
            """
            INSERT OR IGNORE INTO wallet_fills (
                address, tid, coin, market_id, time_ms, price, size, side,
                direction_raw, start_position, closed_pnl, fee, fee_token,
                oid, hash, crossed, raw_json, fill_key
            )
            SELECT
                address, tid, coin, market_id, time_ms, price, size, side,
                direction_raw, start_position, closed_pnl, fee, fee_token,
                oid, hash, crossed, raw_json,
                CAST(time_ms AS TEXT) || '|' || coin || '|' || tid
            FROM wallet_fills_v1
            """
        )
    )
    conn.execute(text("DROP TABLE wallet_fills_v1"))


def _migrate_wallet_fills(conn) -> None:
    if _has_table(conn, "wallet_fills") and "fill_key" not in _columns(conn, "wallet_fills"):
        conn.execute(text("ALTER TABLE wallet_fills RENAME TO wallet_fills_v1"))
        _drop_indexes(conn, "wallet_fills_v1")
        WalletFillRow.__table__.create(conn)
    if _has_table(conn, "wallet_fills_v1"):
        if not _has_table(conn, "wallet_fills"):
            _drop_indexes(conn, "wallet_fills_v1")
            WalletFillRow.__table__.create(conn)
        _copy_wallet_fills(conn)


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    if not _has_table(conn, table):
        return
    if column not in _columns(conn, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _migrate(engine: Engine, preexisting: int | None) -> None:
    if preexisting is not None and preexisting > SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported schema version {preexisting}; this build expects {SCHEMA_VERSION}"
        )
    with engine.begin() as conn:
        if preexisting is None:
            if conn.execute(text("SELECT version FROM schema_meta WHERE id=1")).scalar() is None:
                conn.execute(text("INSERT INTO schema_meta (id, version) VALUES (1, :v)"), {"v": SCHEMA_VERSION})
            return
        if preexisting == SCHEMA_VERSION:
            return
        _add_column(conn, "markets", "deployer_fee_scale", "VARCHAR(32)")
        _add_column(conn, "markets", "last_fee_scale_change_ms", "BIGINT")
        _add_column(conn, "markets", "collateral_token", "INTEGER")
        _add_column(conn, "wallets", "performance_verified", "BOOLEAN DEFAULT 0")
        _add_column(conn, "wallets", "verification_stage", "VARCHAR(16) DEFAULT 'none'")
        _add_column(conn, "wallets", "copy_observations", "INTEGER DEFAULT 0")
        _add_column(conn, "paper_fills", "fee_inputs_json", "TEXT DEFAULT ''")
        _migrate_wallet_fills(conn)
        if _has_table(conn, "wallets") and "verified" in _columns(conn, "wallets"):
            conn.execute(
                text(
                    """
                    UPDATE wallets
                    SET performance_verified = 1,
                        verification_stage = 'performance',
                        tracked = 1,
                        verified = 0
                    WHERE verified = 1
                    """
                )
            )
        conn.execute(
            text("UPDATE schema_meta SET version = :v WHERE id = 1"),
            {"v": SCHEMA_VERSION},
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
