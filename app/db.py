"""SQLite 持久层：审计记录与 SOA 序列号预留的建表、写入与查询。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from .suggest import SuggestionError, suggest_next_serial

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    serials TEXT NOT NULL,
    items TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS serial_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone TEXT NOT NULL,
    request_id TEXT NOT NULL,
    current INTEGER NOT NULL,
    serial INTEGER NOT NULL,
    date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (zone, request_id)
);
CREATE INDEX IF NOT EXISTS idx_serial_reservations_zone_id
    ON serial_reservations (zone, id);
"""

#: 预留接口 409 的机器可读原因
ERR_STALE_CURRENT = "stale_current"


class ReservationError(Exception):
    """预留分配失败（对应 HTTP 409 Conflict）。

    :param code: 原因代码（stale_current，或透传 SuggestionError 的代码）
    :param latest_serial: 该区域服务端已预留的最新值（可能为 None）
    """

    def __init__(
        self,
        message: str,
        code: str,
        latest_serial: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.latest_serial = latest_serial


def default_db_path() -> str:
    return os.environ.get("DNS_AUDIT_DB", "audits.db")


def _connect(db_path: str) -> sqlite3.Connection:
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(db_path: str) -> None:
    """启动时初始化数据库（建表，幂等）。"""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def insert_audit(
    db_path: str,
    zone: str,
    serials: list[int],
    items: list[dict],
    summary: dict,
) -> dict:
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO audits (zone, created_at, serials, items, summary)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                zone,
                created_at,
                json.dumps(serials),
                json.dumps(items, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
            ),
        )
        audit_id = cursor.lastrowid
    return {
        "id": audit_id,
        "zone": zone,
        "created_at": created_at,
        "serials": serials,
        "items": items,
        "summary": summary,
    }


def get_audit(db_path: str, audit_id: int) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM audits WHERE id = ?", (audit_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_audits(db_path: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, zone, created_at, serials, summary FROM audits"
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    result = []
    for row in rows:
        summary = json.loads(row["summary"])
        result.append(
            {
                "id": row["id"],
                "zone": row["zone"],
                "created_at": row["created_at"],
                "total": summary["total"],
                "risk_count": len(summary["risk_indexes"]),
            }
        )
    return result


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "zone": row["zone"],
        "created_at": row["created_at"],
        "serials": json.loads(row["serials"]),
        "items": json.loads(row["items"]),
        "summary": json.loads(row["summary"]),
    }


def reserve_serial(
    db_path: str,
    zone: str,
    request_id: str,
    current: int,
    today: date | None = None,
) -> tuple[dict, bool]:
    """在单个 SQLite 写事务内为区域原子预留下一个 YYYYMMDDnn 序列号。

    语义：
    - 同一 (zone, request_id) 重试：原样返回首次预留结果，不再次推进；
    - current 与该区域已预留的最新值不一致：抛 ReservationError（stale_current），
      并在 latest_serial 中带回最新值；
    - 当日编号耗尽 / 领先当日区间 / 半区间歧义：透传 SuggestionError；
    - 并发分配通过 ``BEGIN IMMEDIATE`` 串行化，UNIQUE(zone, request_id)
      约束兜底，保证不会为不同请求分配重复 serial。

    :return: (预留记录, replayed)；replayed=True 表示命中幂等重试。
    """
    effective_today = today or datetime.now(timezone.utc).date()
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # isolation_level=None：手工管理事务，BEGIN IMMEDIATE 立即获取写锁
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM serial_reservations"
            " WHERE zone = ? AND request_id = ?",
            (zone, request_id),
        ).fetchone()
        if existing is not None:
            # 幂等重试：无论 current 是否变化，都返回首次结果且不推进
            conn.execute("COMMIT")
            return _reservation_row_to_dict(existing), True

        latest_row = conn.execute(
            "SELECT serial FROM serial_reservations WHERE zone = ?"
            " ORDER BY id DESC LIMIT 1",
            (zone,),
        ).fetchone()
        latest_serial: int | None = latest_row["serial"] if latest_row else None
        if latest_serial is not None and current != latest_serial:
            conn.execute("ROLLBACK")
            raise ReservationError(
                f"current 与服务端已预留的最新序列号 {latest_serial} 不一致",
                code=ERR_STALE_CURRENT,
                latest_serial=latest_serial,
            )

        try:
            next_serial = suggest_next_serial(current, effective_today)
        except SuggestionError:
            conn.execute("ROLLBACK")
            raise

        try:
            cursor = conn.execute(
                "INSERT INTO serial_reservations"
                " (zone, request_id, current, serial, date, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    zone,
                    request_id,
                    current,
                    next_serial,
                    effective_today.strftime("%Y%m%d"),
                    created_at,
                ),
            )
        except sqlite3.IntegrityError:
            # 并发兜底：理论上 BEGIN IMMEDIATE 已串行化写入，此处仅在
            # 极端竞争下命中 UNIQUE(zone, request_id) —— 回滚后按幂等重试处理
            conn.execute("ROLLBACK")
            winner = conn.execute(
                "SELECT * FROM serial_reservations"
                " WHERE zone = ? AND request_id = ?",
                (zone, request_id),
            ).fetchone()
            if winner is not None:
                return _reservation_row_to_dict(winner), True
            raise
        conn.execute("COMMIT")
        return (
            {
                "id": cursor.lastrowid,
                "zone": zone,
                "request_id": request_id,
                "current": current,
                "serial": next_serial,
                "date": effective_today.strftime("%Y%m%d"),
                "created_at": created_at,
            },
            False,
        )
    except sqlite3.OperationalError:
        # 锁竞争（超出 busy_timeout）：回滚后交由调用方重试
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


def list_serial_reservations(
    db_path: str,
    zone: str,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """按区域查询预留记录，按预留先后（id 升序）返回。"""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM serial_reservations WHERE zone = ?"
            " ORDER BY id ASC LIMIT ? OFFSET ?",
            (zone, limit, offset),
        ).fetchall()
    return [_reservation_row_to_dict(row) for row in rows]


def _reservation_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "zone": row["zone"],
        "request_id": row["request_id"],
        "current": row["current"],
        "serial": row["serial"],
        "date": row["date"],
        "created_at": row["created_at"],
    }
