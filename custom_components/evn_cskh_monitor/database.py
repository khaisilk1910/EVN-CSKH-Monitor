"""SQLite persistence for EVN CSKH Monitor.

All methods in this module are synchronous by design. Callers must execute them
with Home Assistant's executor helpers so SQLite never blocks the event loop.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

_SQLITE_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_consumption (
    customer_id TEXT NOT NULL,
    ngay TEXT NOT NULL,
    chi_so REAL,
    dien_tieu_thu_kwh REAL,
    PRIMARY KEY (customer_id, ngay)
);
CREATE INDEX IF NOT EXISTS idx_daily_customer_date
    ON daily_consumption(customer_id, ngay);

CREATE TABLE IF NOT EXISTS monthly_bill (
    customer_id TEXT NOT NULL,
    thang INTEGER NOT NULL,
    nam INTEGER NOT NULL,
    tien_dien REAL,
    san_luong_kwh REAL,
    bill_status TEXT,
    source TEXT,
    PRIMARY KEY (customer_id, thang, nam)
);
CREATE INDEX IF NOT EXISTS idx_monthly_customer_year_month
    ON monthly_bill(customer_id, nam, thang);

CREATE TABLE IF NOT EXISTS debt (
    customer_id TEXT PRIMARY KEY,
    amount REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS power_outage_schedule (
    customer_id TEXT NOT NULL,
    ngay_bat_dau TEXT NOT NULL,
    ngay_ket_thuc TEXT,
    thoi_gian_bat_dau TEXT NOT NULL DEFAULT '',
    thoi_gian_ket_thuc TEXT,
    ly_do TEXT,
    khu_vuc TEXT,
    PRIMARY KEY (customer_id, ngay_bat_dau, thoi_gian_bat_dau)
);

CREATE TABLE IF NOT EXISTS notifications (
    customer_id TEXT NOT NULL,
    notif_id TEXT NOT NULL,
    loai TEXT,
    tieu_de TEXT,
    noi_dung TEXT,
    thoi_gian TEXT,
    da_doc INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (customer_id, notif_id)
);
CREATE INDEX IF NOT EXISTS idx_notifications_customer_time
    ON notifications(customer_id, thoi_gian DESC);

CREATE TABLE IF NOT EXISTS raw_server_records (
    customer_id TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (customer_id, source, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_raw_customer_source
    ON raw_server_records(customer_id, source, fetched_at DESC);

CREATE TABLE IF NOT EXISTS integration_state (
    customer_id TEXT NOT NULL,
    state_key TEXT NOT NULL,
    state_value TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (customer_id, state_key)
);
"""


class EVNDatabase:
    """Small SQLite repository with deterministic, non-destructive upserts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        """Create all tables and migrate compatible columns from early builds."""
        with _SQLITE_LOCK, self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._ensure_schema_columns(conn)
            conn.commit()

    @staticmethod
    def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
        """Handle databases created by prerelease builds of the new domain."""
        # Keep prerelease EVN CSKH Monitor databases forward-compatible.
        for table, columns in {
            "monthly_bill": {
                "bill_status": "TEXT",
                "source": "TEXT",
            },
        }.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, sql_type in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def get_last_daily_date(self, customer_id: str) -> datetime | None:
        with _SQLITE_LOCK, self._connect() as conn:
            row = conn.execute(
                """
                SELECT ngay FROM daily_consumption
                WHERE customer_id=?
                ORDER BY substr(ngay, 7, 4) DESC,
                         substr(ngay, 4, 2) DESC,
                         substr(ngay, 1, 2) DESC
                LIMIT 1
                """,
                (customer_id,),
            ).fetchone()
        if not row or not row["ngay"]:
            return None
        try:
            return datetime.strptime(row["ngay"], "%d-%m-%Y")
        except ValueError:
            return None

    def save_daily_records(
        self,
        customer_id: str,
        records: Iterable[tuple[str, float | None, float | None]],
    ) -> None:
        with _SQLITE_LOCK, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO daily_consumption(customer_id, ngay, chi_so, dien_tieu_thu_kwh)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(customer_id, ngay) DO UPDATE SET
                    chi_so=COALESCE(excluded.chi_so, daily_consumption.chi_so),
                    dien_tieu_thu_kwh=COALESCE(
                        excluded.dien_tieu_thu_kwh,
                        daily_consumption.dien_tieu_thu_kwh
                    )
                """,
                [(customer_id, *record) for record in records],
            )
            conn.commit()

    def aggregate_monthly_from_daily(self, customer_id: str) -> None:
        """Update monthly consumption without ever wiping official bill money."""
        with _SQLITE_LOCK, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT CAST(substr(ngay,4,2) AS INTEGER) AS thang,
                       CAST(substr(ngay,7,4) AS INTEGER) AS nam,
                       ROUND(SUM(dien_tieu_thu_kwh), 3) AS san_luong
                FROM daily_consumption
                WHERE customer_id=? AND dien_tieu_thu_kwh IS NOT NULL
                GROUP BY nam, thang
                """,
                (customer_id,),
            ).fetchall()
            conn.executemany(
                """
                INSERT INTO monthly_bill(
                    customer_id, thang, nam, tien_dien, san_luong_kwh, source
                ) VALUES (?, ?, ?, NULL, ?, 'daily_aggregate')
                ON CONFLICT(customer_id, thang, nam) DO UPDATE SET
                    san_luong_kwh=CASE
                        WHEN monthly_bill.source IN ('invoice', 'monthly_api')
                             AND monthly_bill.san_luong_kwh IS NOT NULL
                            THEN monthly_bill.san_luong_kwh
                        ELSE excluded.san_luong_kwh
                    END,
                    source=CASE
                        WHEN monthly_bill.source IN ('invoice', 'monthly_api')
                            THEN monthly_bill.source
                        ELSE excluded.source
                    END
                """,
                [(customer_id, row["thang"], row["nam"], row["san_luong"]) for row in rows],
            )
            conn.commit()

    def save_monthly_reading(
        self,
        customer_id: str,
        month: int,
        year: int,
        consumption: float | None,
    ) -> None:
        if consumption is None:
            return
        with _SQLITE_LOCK, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO monthly_bill(
                    customer_id, thang, nam, tien_dien, san_luong_kwh, source
                ) VALUES (?, ?, ?, NULL, ?, 'monthly_api')
                ON CONFLICT(customer_id, thang, nam) DO UPDATE SET
                    san_luong_kwh=COALESCE(excluded.san_luong_kwh, monthly_bill.san_luong_kwh),
                    source=CASE
                        WHEN monthly_bill.source='invoice' THEN monthly_bill.source
                        ELSE excluded.source
                    END
                """,
                (customer_id, month, year, consumption),
            )
            conn.commit()

    def save_bills(self, customer_id: str, bills: list[dict[str, Any]]) -> None:
        """Persist official bills without inventing a zero debt value.

        EVN occasionally returns an empty or partial bill list. In that case the
        previous known debt must not be overwritten with 0. A zero is persisted
        only when at least one valid bill row contains an explicit payment
        status and none of those rows is marked unpaid.
        """
        now = datetime.now().astimezone().isoformat()
        debt = 0.0
        debt_known = False
        rows: list[tuple[Any, ...]] = []
        for bill in bills:
            month = _to_int(_first_value(bill, "THANG", "thang", "month"))
            year = _to_int(_first_value(bill, "NAM", "nam", "year"))
            if month is None or year is None:
                continue
            amount = _parse_number(
                _first_value(bill, "TONG_TIEN", "tong_tien", "TIEN_DIEN", "tien_dien")
            )
            consumption = _parse_number(
                _first_value(bill, "DIEN_TTHU", "dien_tthu", "SAN_LUONG", "san_luong")
            )
            status = str(
                _first_value(bill, "TTRANG_TTOAN", "trang_thai", "status") or ""
            ).strip()
            rows.append((customer_id, month, year, amount, consumption, status, "invoice"))

            normalized_status = status.upper().replace(" ", "").replace("-", "_")
            if normalized_status:
                debt_known = True
            if normalized_status in {
                "CHUATT",
                "CHUA_TT",
                "UNPAID",
                "CHUATHANHTOAN",
                "CHƯATHANHTOÁN",
            } and amount is not None:
                debt += amount

        with _SQLITE_LOCK, self._connect() as conn:
            if rows:
                conn.executemany(
                    """
                    INSERT INTO monthly_bill(
                        customer_id, thang, nam, tien_dien, san_luong_kwh,
                        bill_status, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(customer_id, thang, nam) DO UPDATE SET
                        tien_dien=COALESCE(excluded.tien_dien, monthly_bill.tien_dien),
                        san_luong_kwh=COALESCE(excluded.san_luong_kwh, monthly_bill.san_luong_kwh),
                        bill_status=COALESCE(NULLIF(excluded.bill_status,''), monthly_bill.bill_status),
                        source='invoice'
                    """,
                    rows,
                )
            if rows and debt_known:
                conn.execute(
                    """
                    INSERT INTO debt(customer_id, amount, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(customer_id) DO UPDATE SET
                        amount=excluded.amount, updated_at=excluded.updated_at
                    """,
                    (customer_id, debt, now),
                )
            conn.commit()

    def save_outages(self, customer_id: str, outages: list[dict[str, Any]]) -> None:
        rows: list[tuple[Any, ...]] = []
        for outage in outages:
            start = str(outage.get("ngay_bat_dau") or "")
            if not start:
                continue
            rows.append(
                (
                    customer_id,
                    start,
                    str(outage.get("ngay_ket_thuc") or start),
                    str(outage.get("thoi_gian_bat_dau") or ""),
                    str(outage.get("thoi_gian_ket_thuc") or ""),
                    str(outage.get("ly_do") or ""),
                    str(outage.get("khu_vuc") or ""),
                )
            )
        if not rows:
            return
        with _SQLITE_LOCK, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO power_outage_schedule(
                    customer_id, ngay_bat_dau, ngay_ket_thuc,
                    thoi_gian_bat_dau, thoi_gian_ket_thuc, ly_do, khu_vuc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(customer_id, ngay_bat_dau, thoi_gian_bat_dau)
                DO UPDATE SET
                    ngay_ket_thuc=excluded.ngay_ket_thuc,
                    thoi_gian_ket_thuc=excluded.thoi_gian_ket_thuc,
                    ly_do=excluded.ly_do,
                    khu_vuc=excluded.khu_vuc
                """,
                rows,
            )
            conn.commit()

    def sync_outages(
        self,
        customer_id: str,
        outages: list[dict[str, Any]],
        window_start: datetime,
        window_end: datetime,
    ) -> None:
        """Replace outage rows inside one authoritative EVN query window.

        The schedule endpoint is queried for a complete date range. Removing old
        rows in that same range before inserting the fresh response prevents a
        cancelled EVN outage from remaining visible until its original date.
        The coordinator only falls back to notification-derived outage rows
        when the regional schedule response is not authoritative.
        """
        rows: list[tuple[Any, ...]] = []
        for outage in outages:
            start = str(outage.get("ngay_bat_dau") or "")
            if not start:
                continue
            rows.append(
                (
                    customer_id,
                    start,
                    str(outage.get("ngay_ket_thuc") or start),
                    str(outage.get("thoi_gian_bat_dau") or ""),
                    str(outage.get("thoi_gian_ket_thuc") or ""),
                    str(outage.get("ly_do") or ""),
                    str(outage.get("khu_vuc") or ""),
                )
            )

        start_iso = window_start.date().isoformat()
        end_iso = window_end.date().isoformat()
        with _SQLITE_LOCK, self._connect() as conn:
            conn.execute(
                """
                DELETE FROM power_outage_schedule
                WHERE customer_id=?
                  AND date(
                    substr(ngay_bat_dau,7,4) || '-' ||
                    substr(ngay_bat_dau,4,2) || '-' ||
                    substr(ngay_bat_dau,1,2)
                  ) BETWEEN date(?) AND date(?)
                """,
                (customer_id, start_iso, end_iso),
            )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO power_outage_schedule(
                        customer_id, ngay_bat_dau, ngay_ket_thuc,
                        thoi_gian_bat_dau, thoi_gian_ket_thuc, ly_do, khu_vuc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(customer_id, ngay_bat_dau, thoi_gian_bat_dau)
                    DO UPDATE SET
                        ngay_ket_thuc=excluded.ngay_ket_thuc,
                        thoi_gian_ket_thuc=excluded.thoi_gian_ket_thuc,
                        ly_do=excluded.ly_do,
                        khu_vuc=excluded.khu_vuc
                    """,
                    rows,
                )
            conn.commit()

    def save_notifications(self, customer_id: str, notifications: list[dict[str, Any]]) -> None:
        rows: list[tuple[Any, ...]] = []
        for note in notifications:
            note_id = str(note.get("id") or "")
            if not note_id:
                # Stable fallback for feeds without IDs.
                raw = json.dumps(note, ensure_ascii=False, sort_keys=True, default=str)
                note_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
            rows.append(
                (
                    customer_id,
                    note_id,
                    str(note.get("loai") or "KHAC"),
                    note.get("title"),
                    note.get("summary"),
                    note.get("createdDate"),
                    1 if note.get("readStatus") else 0,
                )
            )
        if not rows:
            return
        with _SQLITE_LOCK, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO notifications(
                    customer_id, notif_id, loai, tieu_de, noi_dung, thoi_gian, da_doc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(customer_id, notif_id) DO UPDATE SET
                    loai=excluded.loai,
                    tieu_de=excluded.tieu_de,
                    noi_dung=excluded.noi_dung,
                    thoi_gian=excluded.thoi_gian,
                    da_doc=excluded.da_doc
                """,
                rows,
            )
            conn.commit()

    def save_raw_response(self, customer_id: str, source: str, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        payload_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        fetched_at = datetime.now().astimezone().isoformat()
        with _SQLITE_LOCK, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO raw_server_records(
                    customer_id, source, payload_hash, fetched_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (customer_id, source, payload_hash, fetched_at, raw),
            )
            conn.commit()

    def load_invoice_source_records(
        self, customer_id: str, limit: int = 800
    ) -> list[tuple[str, Any]]:
        """Load stored bill/month/notification payloads for attachment recovery.

        The raw table may contain many daily-history batches, so this query is
        intentionally restricted to sources that can carry invoice resources.
        Parsing is also kept here, off the Home Assistant event loop.
        """
        with _SQLITE_LOCK, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source, payload_json
                FROM raw_server_records
                WHERE customer_id=?
                  AND (
                    source='bill'
                    OR source='notifications'
                    OR source LIKE 'monthly_%'
                    OR source LIKE 'history_month_%'
                    OR source LIKE 'invoice_period_%'
                  )
                ORDER BY fetched_at DESC
                LIMIT ?
                """,
                (customer_id, max(1, min(int(limit), 2000))),
            ).fetchall()

        result: list[tuple[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            result.append((str(row["source"]), payload))
        return result

    def get_state(self, customer_id: str, key: str) -> str | None:
        with _SQLITE_LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM integration_state WHERE customer_id=? AND state_key=?",
                (customer_id, key),
            ).fetchone()
        return row["state_value"] if row else None

    def set_state(self, customer_id: str, key: str, value: str | None) -> None:
        with _SQLITE_LOCK, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO integration_state(customer_id, state_key, state_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(customer_id, state_key) DO UPDATE SET
                    state_value=excluded.state_value,
                    updated_at=excluded.updated_at
                """,
                (customer_id, key, value, datetime.now().astimezone().isoformat()),
            )
            conn.commit()

    def load_snapshot(self, customer_id: str) -> dict[str, Any]:
        """Load all entity/WebUI data in one SQLite snapshot."""
        with _SQLITE_LOCK, self._connect() as conn:
            daily_rows = conn.execute(
                """
                SELECT ngay, chi_so, dien_tieu_thu_kwh
                FROM daily_consumption
                WHERE customer_id=?
                ORDER BY substr(ngay,7,4), substr(ngay,4,2), substr(ngay,1,2)
                """,
                (customer_id,),
            ).fetchall()
            monthly_rows = conn.execute(
                """
                SELECT thang, nam, tien_dien, san_luong_kwh, bill_status, source
                FROM monthly_bill
                WHERE customer_id=?
                ORDER BY nam, thang
                """,
                (customer_id,),
            ).fetchall()
            debt_row = conn.execute(
                "SELECT amount, updated_at FROM debt WHERE customer_id=?",
                (customer_id,),
            ).fetchone()
            outage_rows = conn.execute(
                """
                SELECT ngay_bat_dau, ngay_ket_thuc, thoi_gian_bat_dau,
                       thoi_gian_ket_thuc, ly_do, khu_vuc
                FROM power_outage_schedule
                WHERE customer_id=?
                ORDER BY substr(ngay_bat_dau,7,4), substr(ngay_bat_dau,4,2),
                         substr(ngay_bat_dau,1,2), thoi_gian_bat_dau
                """,
                (customer_id,),
            ).fetchall()
            notification_rows = conn.execute(
                """
                SELECT loai, tieu_de, noi_dung, thoi_gian, da_doc
                FROM notifications
                WHERE customer_id=?
                ORDER BY thoi_gian DESC
                """,
                (customer_id,),
            ).fetchall()
            raw_count = conn.execute(
                "SELECT COUNT(*) AS count FROM raw_server_records WHERE customer_id=?",
                (customer_id,),
            ).fetchone()["count"]
            last_sync = conn.execute(
                "SELECT state_value FROM integration_state WHERE customer_id=? AND state_key='last_sync'",
                (customer_id,),
            ).fetchone()

        daily = []
        for row in daily_rows:
            iso = _display_date_to_iso(row["ngay"])
            daily.append(
                {
                    "date": iso,
                    "date_display": row["ngay"],
                    "reading": row["chi_so"],
                    "consumption": row["dien_tieu_thu_kwh"],
                }
            )
        monthly = [
            {
                "month": row["thang"],
                "year": row["nam"],
                "cost": row["tien_dien"],
                "consumption": row["san_luong_kwh"],
                "status": row["bill_status"],
                "source": row["source"],
            }
            for row in monthly_rows
        ]
        outages = [
            {
                "start_date": row["ngay_bat_dau"],
                "end_date": row["ngay_ket_thuc"],
                "start_time": row["thoi_gian_bat_dau"],
                "end_time": row["thoi_gian_ket_thuc"],
                "reason": row["ly_do"],
                "area": row["khu_vuc"],
            }
            for row in outage_rows
        ]
        notifications = [
            {
                "category": row["loai"],
                "title": row["tieu_de"],
                "content": row["noi_dung"],
                "time": row["thoi_gian"],
                "read": bool(row["da_doc"]),
            }
            for row in notification_rows
        ]
        return {
            "customer_id": customer_id,
            "daily": daily,
            "monthly": monthly,
            "debt": {
                "amount": debt_row["amount"] if debt_row else None,
                "updated_at": debt_row["updated_at"] if debt_row else None,
            },
            "outages": outages,
            "notifications": notifications,
            "raw_record_count": int(raw_count or 0),
            "last_sync": last_sync["state_value"] if last_sync else None,
        }


def _display_date_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%m-%Y").date().isoformat()
    except ValueError:
        return None


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None and data[key] != "":
            return data[key]
    return None


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_number(value: Any) -> float | None:
    """Parse common Vietnamese and international number formats safely."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    # If both separators exist, the right-most one is the decimal separator.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts[-1]) in (1, 2):
            text = "".join(parts[:-1]) + "." + parts[-1]
        else:
            text = "".join(parts)
    elif "." in text:
        parts = text.split(".")
        # 2.038.272 is a thousands-formatted integer, 15.24839 is decimal.
        if len(parts) > 2 and all(len(part) == 3 for part in parts[1:]):
            text = "".join(parts)
    try:
        return float(text)
    except ValueError:
        return None


parse_number = _parse_number
