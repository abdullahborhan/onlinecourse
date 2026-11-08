"""
SQLite persistence layer (local-first).

Design rules
------------
* Original records are preserved: every row keeps ``raw_json`` and the id of
  the source file it came from; original files are archived byte-for-byte.
* Duplicate protection: files are de-duplicated by SHA-256, records by their
  natural key (account + deal ticket, account + position id, ...).
* NULL means "not provided by the source". It is never replaced with 0.
* Destructive operations require an explicit confirmation token.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from modules.audit_logger import get_logger

log = get_logger("database")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    file_kind TEXT NOT NULL,
    size_bytes INTEGER,
    imported_at TEXT NOT NULL,
    account_id TEXT,
    period_start TEXT,
    period_end TEXT,
    status TEXT NOT NULL,
    records_new INTEGER DEFAULT 0,
    records_duplicate INTEGER DEFAULT 0,
    validation_json TEXT,
    archive_path TEXT,
    is_synthetic INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS account_info (
    account_id TEXT PRIMARY KEY,
    name TEXT,
    currency TEXT,
    server TEXT,
    company TEXT,
    updated_at TEXT,
    info_json TEXT
);

CREATE TABLE IF NOT EXISTS deals (
    account_id TEXT NOT NULL,
    deal_id TEXT NOT NULL,
    time TEXT NOT NULL,
    symbol TEXT,
    deal_type TEXT,
    entry TEXT,
    volume REAL,
    price REAL,
    order_id TEXT,
    position_id TEXT,
    magic TEXT,
    commission REAL,
    swap REAL,
    fee REAL,
    profit REAL,
    balance REAL,
    sl REAL,
    tp REAL,
    comment TEXT,
    source_file_id INTEGER REFERENCES source_files(id),
    raw_json TEXT,
    PRIMARY KEY (account_id, deal_id)
);
CREATE INDEX IF NOT EXISTS idx_deals_time ON deals(account_id, time);
CREATE INDEX IF NOT EXISTS idx_deals_pos ON deals(account_id, position_id);

CREATE TABLE IF NOT EXISTS report_positions (
    account_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    open_time TEXT,
    symbol TEXT,
    direction TEXT,
    volume REAL,
    open_price REAL,
    sl REAL,
    tp REAL,
    close_time TEXT,
    close_price REAL,
    commission REAL,
    swap REAL,
    profit REAL,
    comment TEXT,
    source_file_id INTEGER REFERENCES source_files(id),
    raw_json TEXT,
    PRIMARY KEY (account_id, position_id)
);

CREATE TABLE IF NOT EXISTS orders (
    account_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    open_time TEXT,
    symbol TEXT,
    order_type TEXT,
    volume_initial REAL,
    volume_filled REAL,
    price REAL,
    sl REAL,
    tp REAL,
    done_time TEXT,
    state TEXT,
    comment TEXT,
    source_file_id INTEGER REFERENCES source_files(id),
    raw_json TEXT,
    PRIMARY KEY (account_id, order_id)
);

CREATE TABLE IF NOT EXISTS equity_points (
    account_id TEXT NOT NULL,
    time TEXT NOT NULL,
    balance REAL,
    equity REAL,
    source_file_id INTEGER REFERENCES source_files(id),
    PRIMARY KEY (account_id, time)
);

CREATE TABLE IF NOT EXISTS ea_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    time TEXT,
    event_type TEXT,
    strategy TEXT,
    signal_id TEXT,
    ticket TEXT,
    layer INTEGER,
    spread REAL,
    requested_price REAL,
    message TEXT,
    source_file_id INTEGER REFERENCES source_files(id),
    UNIQUE (account_id, time, message)
);

CREATE TABLE IF NOT EXISTS ea_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER REFERENCES source_files(id),
    loaded_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    raw_json TEXT,
    mapped_json TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    is_data_missing INTEGER DEFAULT 0,
    data_fingerprint TEXT,
    metrics_json TEXT,
    report_json TEXT,
    md_path TEXT,
    pdf_path TEXT,
    csv_path TEXT,
    superseded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reports_period ON reports(account_id, period_type, period_start);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
"""

DEAL_COLUMNS = ["account_id", "deal_id", "time", "symbol", "deal_type", "entry", "volume",
                "price", "order_id", "position_id", "magic", "commission", "swap", "fee",
                "profit", "balance", "sl", "tp", "comment", "source_file_id", "raw_json"]
POSITION_COLUMNS = ["account_id", "position_id", "open_time", "symbol", "direction", "volume",
                    "open_price", "sl", "tp", "close_time", "close_price", "commission", "swap",
                    "profit", "comment", "source_file_id", "raw_json"]
ORDER_COLUMNS = ["account_id", "order_id", "open_time", "symbol", "order_type",
                 "volume_initial", "volume_filled", "price", "sl", "tp", "done_time", "state",
                 "comment", "source_file_id", "raw_json"]
EQUITY_COLUMNS = ["account_id", "time", "balance", "equity", "source_file_id"]
EVENT_COLUMNS = ["account_id", "time", "event_type", "strategy", "signal_id", "ticket", "layer",
                 "spread", "requested_price", "message", "source_file_id"]

RESET_CONFIRMATION = "DELETE ALL SVPI DATA"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(v):
    """Convert pandas/numpy values to SQLite-safe Python values (NaN -> NULL)."""
    if v is None:
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        return v.item()
    return v


class Database:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else config.DATABASE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                      (str(SCHEMA_VERSION),))
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('environment', ?)",
                      (config.ENVIRONMENT,))

    # ------------------------------------------------------------------ meta
    def environment(self) -> str:
        with self.connect() as c:
            row = c.execute("SELECT value FROM meta WHERE key='environment'").fetchone()
        return row["value"] if row else "unknown"

    # ---------------------------------------------------------------- audit
    def add_audit_event(self, action: str, detail: str = "", actor: str = "system") -> None:
        with self.connect() as c:
            c.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES (?,?,?,?)",
                      (_now(), actor, action, detail))

    def audit_events(self, limit: int = 500) -> pd.DataFrame:
        with self.connect() as c:
            return pd.read_sql_query(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", c, params=(limit,))

    # --------------------------------------------------------- source files
    def find_source_by_sha(self, sha256: str):
        with self.connect() as c:
            row = c.execute("SELECT * FROM source_files WHERE sha256=?", (sha256,)).fetchone()
        return dict(row) if row else None

    def register_source_file(self, *, filename: str, sha256: str, file_kind: str,
                             size_bytes: int, account_id: str | None, status: str,
                             validation: dict, archive_path: str | None,
                             period_start=None, period_end=None,
                             is_synthetic: bool = False) -> int:
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO source_files(filename, sha256, file_kind, size_bytes, imported_at,
                   account_id, period_start, period_end, status, validation_json, archive_path,
                   is_synthetic) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (filename, sha256, file_kind, size_bytes, _now(), account_id,
                 _clean(period_start), _clean(period_end), status,
                 json.dumps(validation, default=str), archive_path, int(is_synthetic)))
            return int(cur.lastrowid)

    def update_source_counts(self, source_id: int, new: int, dup: int, status: str | None = None,
                             validation: dict | None = None) -> None:
        with self.connect() as c:
            c.execute("UPDATE source_files SET records_new=?, records_duplicate=? WHERE id=?",
                      (new, dup, source_id))
            if status:
                c.execute("UPDATE source_files SET status=? WHERE id=?", (status, source_id))
            if validation is not None:
                c.execute("UPDATE source_files SET validation_json=? WHERE id=?",
                          (json.dumps(validation, default=str), source_id))

    def source_files(self) -> pd.DataFrame:
        with self.connect() as c:
            return pd.read_sql_query("SELECT * FROM source_files ORDER BY id DESC", c)

    # ---------------------------------------------------------- generic insert
    def _insert_rows(self, table: str, columns: list[str], df: pd.DataFrame) -> tuple[int, int]:
        if df is None or df.empty:
            return 0, 0
        cols = [c for c in columns if c in df.columns]
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
        rows = [tuple(_clean(v) for v in r) for r in df[cols].itertuples(index=False, name=None)]
        with self.connect() as c:
            before = c.total_changes
            c.executemany(sql, rows)
            inserted = c.total_changes - before
        return inserted, len(rows) - inserted

    def insert_deals(self, df: pd.DataFrame) -> tuple[int, int]:
        return self._insert_rows("deals", DEAL_COLUMNS, df)

    def insert_report_positions(self, df: pd.DataFrame) -> tuple[int, int]:
        return self._insert_rows("report_positions", POSITION_COLUMNS, df)

    def insert_orders(self, df: pd.DataFrame) -> tuple[int, int]:
        return self._insert_rows("orders", ORDER_COLUMNS, df)

    def insert_equity(self, df: pd.DataFrame) -> tuple[int, int]:
        return self._insert_rows("equity_points", EQUITY_COLUMNS, df)

    def insert_ea_events(self, df: pd.DataFrame) -> tuple[int, int]:
        return self._insert_rows("ea_events", EVENT_COLUMNS, df)

    def upsert_account(self, account_id: str, info: dict) -> None:
        with self.connect() as c:
            c.execute(
                """INSERT INTO account_info(account_id, name, currency, server, company,
                   updated_at, info_json) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(account_id) DO UPDATE SET
                     name=COALESCE(excluded.name, account_info.name),
                     currency=COALESCE(excluded.currency, account_info.currency),
                     server=COALESCE(excluded.server, account_info.server),
                     company=COALESCE(excluded.company, account_info.company),
                     updated_at=excluded.updated_at, info_json=excluded.info_json""",
                (account_id, info.get("name"), info.get("currency"), info.get("server"),
                 info.get("company"), _now(), json.dumps(info, default=str)))

    # ------------------------------------------------------------- loaders
    def accounts(self) -> list[str]:
        with self.connect() as c:
            rows = c.execute(
                """SELECT account_id FROM deals UNION SELECT account_id FROM report_positions
                   UNION SELECT account_id FROM equity_points ORDER BY 1""").fetchall()
        return [r[0] for r in rows]

    def account_info(self, account_id: str) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT * FROM account_info WHERE account_id=?",
                            (account_id,)).fetchone()
        return dict(row) if row else {}

    def _load(self, table: str, account_id: str, time_col: str | None,
              start=None, end=None, time_cols=()) -> pd.DataFrame:
        sql = f"SELECT * FROM {table} WHERE account_id=?"
        params: list = [account_id]
        if time_col and start is not None:
            sql += f" AND {time_col} >= ?"
            params.append(_clean(pd.Timestamp(start)))
        if time_col and end is not None:
            sql += f" AND {time_col} < ?"
            params.append(_clean(pd.Timestamp(end)))
        if time_col:
            sql += f" ORDER BY {time_col}"
        with self.connect() as c:
            df = pd.read_sql_query(sql, c, params=params)
        for col in time_cols:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        return df

    def load_deals(self, account_id: str, start=None, end=None) -> pd.DataFrame:
        df = self._load("deals", account_id, "time", start, end, ("time",))
        if not df.empty:
            df = df.sort_values(["time", "deal_id"], kind="stable").reset_index(drop=True)
        return df

    def load_report_positions(self, account_id: str) -> pd.DataFrame:
        return self._load("report_positions", account_id, "open_time", None, None,
                          ("open_time", "close_time"))

    def load_orders(self, account_id: str) -> pd.DataFrame:
        return self._load("orders", account_id, "open_time", None, None,
                          ("open_time", "done_time"))

    def load_equity(self, account_id: str, start=None, end=None) -> pd.DataFrame:
        return self._load("equity_points", account_id, "time", start, end, ("time",))

    def load_ea_events(self, account_id: str) -> pd.DataFrame:
        return self._load("ea_events", account_id, "time", None, None, ("time",))

    def data_coverage(self, account_id: str) -> dict:
        with self.connect() as c:
            d = c.execute("SELECT MIN(time), MAX(time), COUNT(*) FROM deals WHERE account_id=?",
                          (account_id,)).fetchone()
            p = c.execute("SELECT MIN(open_time), MAX(close_time), COUNT(*) FROM report_positions "
                          "WHERE account_id=?", (account_id,)).fetchone()
            e = c.execute("SELECT MIN(time), MAX(time), COUNT(*) FROM equity_points "
                          "WHERE account_id=?", (account_id,)).fetchone()
            ev = c.execute("SELECT COUNT(*) FROM ea_events WHERE account_id=?",
                           (account_id,)).fetchone()
        return {"deals": {"first": d[0], "last": d[1], "count": d[2]},
                "report_positions": {"first": p[0], "last": p[1], "count": p[2]},
                "equity_points": {"first": e[0], "last": e[1], "count": e[2]},
                "ea_events": {"count": ev[0]}}

    # ------------------------------------------------------------- EA config
    def save_ea_config(self, raw: dict, mapped: dict, origin: str,
                       source_file_id: int | None = None) -> int:
        with self.connect() as c:
            c.execute("UPDATE ea_configs SET active=0")
            cur = c.execute(
                "INSERT INTO ea_configs(source_file_id, loaded_at, origin, raw_json, mapped_json, "
                "active) VALUES (?,?,?,?,?,1)",
                (source_file_id, _now(), origin, json.dumps(raw), json.dumps(mapped)))
            return int(cur.lastrowid)

    def active_ea_config(self) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM ea_configs WHERE active=1 ORDER BY id DESC LIMIT 1"
                            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["raw"] = json.loads(d.pop("raw_json") or "{}")
        d["mapped"] = json.loads(d.pop("mapped_json") or "{}")
        return d

    # ---------------------------------------------------------------- reports
    def save_report(self, *, account_id: str, period_type: str, period_start, period_end,
                    status: str, is_data_missing: bool, fingerprint: str, metrics: dict,
                    report: dict, md_path=None, pdf_path=None, csv_path=None) -> int:
        ps, pe = _clean(pd.Timestamp(period_start)), _clean(pd.Timestamp(period_end))
        with self.connect() as c:
            # A regenerated report for the same period supersedes earlier versions
            c.execute("UPDATE reports SET superseded=1 WHERE account_id=? AND period_type=? "
                      "AND period_start=? AND period_end=?", (account_id, period_type, ps, pe))
            cur = c.execute(
                """INSERT INTO reports(account_id, period_type, period_start, period_end,
                   generated_at, status, is_data_missing, data_fingerprint, metrics_json,
                   report_json, md_path, pdf_path, csv_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, period_type, ps, pe, _now(), status, int(is_data_missing),
                 fingerprint, json.dumps(metrics, default=str), json.dumps(report, default=str),
                 _str(md_path), _str(pdf_path), _str(csv_path)))
            return int(cur.lastrowid)

    def find_report(self, account_id: str, period_type: str, period_start, period_end,
                    fingerprint: str | None = None):
        ps, pe = _clean(pd.Timestamp(period_start)), _clean(pd.Timestamp(period_end))
        sql = ("SELECT * FROM reports WHERE account_id=? AND period_type=? AND period_start=? "
               "AND period_end=? AND superseded=0")
        params = [account_id, period_type, ps, pe]
        if fingerprint:
            sql += " AND data_fingerprint=?"
            params.append(fingerprint)
        with self.connect() as c:
            row = c.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return _report_row(row)

    def list_reports(self, account_id: str | None = None, period_type: str | None = None,
                     include_superseded: bool = False) -> pd.DataFrame:
        sql = ("SELECT id, account_id, period_type, period_start, period_end, generated_at, "
               "status, is_data_missing, md_path, pdf_path, csv_path, superseded FROM reports "
               "WHERE 1=1")
        params: list = []
        if account_id:
            sql += " AND account_id=?"
            params.append(account_id)
        if period_type:
            sql += " AND period_type=?"
            params.append(period_type)
        if not include_superseded:
            sql += " AND superseded=0"
        sql += " ORDER BY period_start DESC, id DESC"
        with self.connect() as c:
            return pd.read_sql_query(sql, c, params=params)

    def get_report(self, report_id: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return _report_row(row)

    def previous_report(self, account_id: str, period_type: str, before_start) -> dict | None:
        """Latest non-superseded, non-data-missing report that ended on/before ``before_start``."""
        bs = _clean(pd.Timestamp(before_start))
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM reports WHERE account_id=? AND period_type=? AND period_end<=? "
                "AND superseded=0 AND is_data_missing=0 ORDER BY period_end DESC, id DESC LIMIT 1",
                (account_id, period_type, bs)).fetchone()
        return _report_row(row)

    def historical_reports(self, account_id: str, period_type: str, before_start) -> list[dict]:
        bs = _clean(pd.Timestamp(before_start))
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM reports WHERE account_id=? AND period_type=? AND period_end<=? "
                "AND superseded=0 AND is_data_missing=0 ORDER BY period_end",
                (account_id, period_type, bs)).fetchall()
        return [_report_row(r) for r in rows]

    # ------------------------------------------------------- backup / reset
    def backup(self, dest_dir: Path | None = None) -> Path:
        dest_dir = Path(dest_dir) if dest_dir else config.BACKUP_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{self.path.stem}_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        self.add_audit_event("DATABASE_BACKUP", json.dumps({"path": str(dest)}))
        return dest

    def reset_all_data(self, confirmation: str) -> Path:
        """Delete all imported data and reports. Requires the exact confirmation phrase.

        A backup is always taken first and its path returned.
        """
        if confirmation != RESET_CONFIRMATION:
            raise PermissionError(
                f"Destructive operation refused: type exactly '{RESET_CONFIRMATION}'.")
        backup_path = self.backup()
        with self.connect() as c:
            for t in ("deals", "report_positions", "orders", "equity_points", "ea_events",
                      "ea_configs", "reports", "account_info", "source_files"):
                c.execute(f"DELETE FROM {t}")
        self.add_audit_event("DATABASE_RESET", json.dumps({"backup": str(backup_path)}),
                             actor="user")
        return backup_path

    def delete_source_file(self, source_id: int, confirmation: str) -> None:
        if confirmation != f"DELETE SOURCE {source_id}":
            raise PermissionError(
                f"Destructive operation refused: type exactly 'DELETE SOURCE {source_id}'.")
        self.backup()
        with self.connect() as c:
            for t in ("deals", "report_positions", "orders", "equity_points", "ea_events"):
                c.execute(f"DELETE FROM {t} WHERE source_file_id=?", (source_id,))
            c.execute("UPDATE ea_configs SET source_file_id=NULL WHERE source_file_id=?",
                      (source_id,))
            c.execute("DELETE FROM source_files WHERE id=?", (source_id,))
        self.add_audit_event("SOURCE_DELETED", json.dumps({"source_id": source_id}), actor="user")


def _str(p):
    return str(p) if p else None


def _report_row(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["metrics"] = json.loads(d.get("metrics_json") or "{}")
    d["report"] = json.loads(d.get("report_json") or "{}")
    return d


def archive_original(src: Path, sha256: str) -> Path:
    """Copy an original file byte-for-byte into the archive (never modified)."""
    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.ARCHIVE_DIR / f"{sha256[:16]}_{Path(src).name}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest
