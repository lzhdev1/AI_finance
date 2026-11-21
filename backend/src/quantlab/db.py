"""PostgreSQL 访问层。

约定
----
* 读：``get_conn()`` —— 复用一条自动提交连接，返回 dict 行。
* 写：``transaction()`` —— 新开连接、显式提交/回滚，一批写要么全成要么全滚。
* 批量写：``upsert()`` —— 依赖表上的业务主键做幂等，重复抓取不会产生脏行。
* 所有表都在 ``settings.database.schema``（默认 quantlab）下。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

import pandas as pd
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from .config import DB_DIR, load_settings

log = logging.getLogger(__name__)

_schema: str | None = None
_connections = threading.local()
_active_transaction: ContextVar[psycopg.Connection | None] = ContextVar("quantlab_transaction", default=None)


def schema() -> str:
    global _schema
    if _schema is None:
        _schema = load_settings().db_schema
    return _schema


def dsn() -> str:
    """从环境变量拼连接串（compose 注入 PGHOST=db，宿主机调试用 localhost）。"""
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    name = os.getenv("PGDATABASE", "quantlab")
    user = os.getenv("PGUSER", "quantlab")
    password = os.getenv("PGPASSWORD", "")
    return make_conninfo(host=host, port=port, dbname=name, user=user, password=password)


def qualified(table: str) -> str:
    return f"{schema()}.{table}"


def get_conn() -> psycopg.Connection:
    """读用连接：长连接，断了自动重连。"""
    active = _active_transaction.get()
    if active is not None:
        return active
    conn = getattr(_connections, "reader", None)
    if conn is None or conn.closed:
        conn = psycopg.connect(dsn(), autocommit=True, row_factory=dict_row)
        _connections.reader = conn
        timeout = load_settings().statement_timeout_ms
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(timeout)}")
    return conn


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """嵌套的库调用共用事务，最外层负责提交或回滚。"""
    active = _active_transaction.get()
    if active is not None:
        yield active
        return
    conn = psycopg.connect(dsn(), autocommit=False, row_factory=dict_row)
    token = _active_transaction.set(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _active_transaction.reset(token)
        conn.close()


def reset() -> None:
    """测试或长跑后重置读连接。"""
    conn = getattr(_connections, "reader", None)
    if conn is not None and not conn.closed:
        conn.close()
    _connections.reader = None


# ---------------------------------------------------------------- 读

def query(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    with get_conn().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def query_df(sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
    """返回 DataFrame，numeric 列（Decimal）自动转 float。"""
    with get_conn().cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [desc.name for desc in cur.description]
    frame = pd.DataFrame(rows, columns=columns)
    if frame.empty:
        return frame
    for col in frame.columns:
        if frame[col].dtype == object:
            sample = frame[col].dropna()
            if not sample.empty and isinstance(sample.iloc[0], Decimal):
                frame[col] = frame[col].astype("float64")
    return frame


# ---------------------------------------------------------------- 写

def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def execute_many(sql: str, rows: Iterable[Sequence[Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
            return cur.rowcount


def upsert(
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    conflict: Sequence[str],
    update: Sequence[str] | None = None,
) -> int:
    """INSERT ... ON CONFLICT DO UPDATE，按业务主键幂等写入。"""
    rows = list(rows)
    if not rows:
        return 0
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    conflict_cols = ", ".join(conflict)
    if update is None:
        update = [c for c in columns if c not in conflict]
    if update:
        action = "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in update)
    else:
        action = "DO NOTHING"
    sql = (
        f"INSERT INTO {qualified(table)} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_cols}) {action}"
    )
    return execute_many(sql, rows)


# ---------------------------------------------------------------- 运维

def apply_schema() -> None:
    """应用 db/schema.sql（全部 IF NOT EXISTS，重复执行安全）。"""
    path = DB_DIR / "schema.sql"
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(794021001)")
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute((DB_DIR / "agent_research.sql").read_text(encoding="utf-8"))
    log.info("schema 已应用: %s", path)


def wait_for_db(timeout: float = 60.0, interval: float = 2.0) -> None:
    """等待数据库可用（容器启动顺序用）。"""
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with get_conn().cursor() as cur:
                cur.execute("SELECT 1")
            log.info("数据库就绪")
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(interval)
    raise RuntimeError(f"数据库 {timeout}s 内未就绪: {last_error}")


def table_counts(tables: Sequence[str] | None = None) -> dict[str, int]:
    if tables is None:
        rows = query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY 1",
            [schema()],
        )
        tables = [row["table_name"] for row in rows]
    counts: dict[str, int] = {}
    for table in tables:
        row = query_one(f"SELECT count(*) AS n FROM {qualified(table)}")
        counts[table] = int(row["n"]) if row else 0
    return counts


def healthcheck() -> bool:
    try:
        with get_conn().cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False
