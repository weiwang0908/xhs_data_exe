# -*- coding: utf-8 -*-
"""红薯雷达 —— SQLite 数据层。

职责：
  * 建表 / 自动迁移（旧库缺字段时用 PRAGMA table_info 补齐，绝不要求用户删库）
  * WAL 模式、合理超时、参数化 SQL、事务写入
  * settings / products / snapshots / logs / deleted_products 的增删改查
  * 只读连接（供 MCP 使用，物理上无法写入）
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Iterable, Optional

import config as C

# --------------------------------------------------------------------------
# 建表语句
# --------------------------------------------------------------------------

SCHEMA_PRODUCTS = """
CREATE TABLE IF NOT EXISTS products (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    shop_name       TEXT DEFAULT '',
    shop_id         TEXT DEFAULT '',
    cover           TEXT DEFAULT '',
    active          INTEGER DEFAULT 1,
    created_at      TEXT,
    last_error      TEXT,
    delisted_at     TEXT,
    delisted_reason TEXT
)
"""

SCHEMA_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS snapshots (
    product_id   TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    sold         INTEGER,
    shop_sold    INTEGER,
    price        REAL,
    fans         INTEGER,
    stock_status INTEGER,
    deliverable  INTEGER,
    PRIMARY KEY (product_id, captured_at)
)
"""

SCHEMA_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

SCHEMA_LOGS = """
CREATE TABLE IF NOT EXISTS logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT,
    level    TEXT,
    source   TEXT,
    product_id TEXT,
    title    TEXT,
    message  TEXT,
    detail   TEXT
)
"""

SCHEMA_DELETED = """
CREATE TABLE IF NOT EXISTS deleted_products (
    id         TEXT PRIMARY KEY,
    title      TEXT,
    deleted_at TEXT
)
"""

# 迁移用的期望字段（缺哪个补哪个）
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "products": {
        "id": "TEXT",
        "title": "TEXT NOT NULL DEFAULT ''",
        "shop_name": "TEXT DEFAULT ''",
        "shop_id": "TEXT DEFAULT ''",
        "cover": "TEXT DEFAULT ''",
        "active": "INTEGER DEFAULT 1",
        "created_at": "TEXT",
        "last_error": "TEXT",
        "delisted_at": "TEXT",
        "delisted_reason": "TEXT",
        "last_ok_at": "TEXT",
        "last_method": "TEXT DEFAULT ''",
        "fail_count": "INTEGER DEFAULT 0",
        "fail_reason": "TEXT DEFAULT ''",
        "fail_at": "TEXT",
        "ignored": "INTEGER DEFAULT 0",
        "source": "TEXT DEFAULT 'manual'",
    },
    "snapshots": {
        "product_id": "TEXT",
        "captured_at": "TEXT",
        "sold": "INTEGER",
        "shop_sold": "INTEGER",
        "price": "REAL",
        "fans": "INTEGER",
        "stock_status": "INTEGER",
        "deliverable": "INTEGER",
        "method": "TEXT DEFAULT ''",
    },
    "logs": {
        "id": "INTEGER",
        "ts": "TEXT",
        "level": "TEXT",
        "source": "TEXT",
        "product_id": "TEXT",
        "title": "TEXT",
        "message": "TEXT",
        "detail": "TEXT",
    },
    "settings": {
        "key": "TEXT",
        "value": "TEXT",
    },
    "deleted_products": {
        "id": "TEXT",
        "title": "TEXT",
        "deleted_at": "TEXT",
    },
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_snap_prod_time ON snapshots(product_id, captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_snap_time ON snapshots(captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_snap_prod_sold ON snapshots(product_id, sold)",
    "CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)",
    "CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)",
    "CREATE INDEX IF NOT EXISTS idx_products_active ON products(active)",
    "CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop_id)",
]

LEVELS = ("INFO", "SUCCESS", "WARN", "ERROR")

_SAFE_SETTING_KEYS = set(C.DEFAULT_SETTINGS)


class Database:
    """线程安全的 SQLite 封装。

    GUI 主线程、采集线程、调度线程都会访问同一个实例；
    SQLite 连接以 check_same_thread=False 打开，所有写操作走同一把可重入锁。
    """

    def __init__(self, path: str | None = None):
        self.path = path or C.DB_PATH
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._uri = False
        C.ensure_dirs()

    # ---------------- 连接管理 ----------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA foreign_keys=ON")
            except sqlite3.Error:
                pass
            self._conn = conn
        return self._conn

    @staticmethod
    def open_readonly(path: str | None = None) -> sqlite3.Connection:
        """MCP 专用：以 URI 只读方式打开，物理上禁止写入。"""
        p = path or C.DB_PATH
        uri = "file:{}?mode=ro".format(p.replace("\\", "/").replace("?", "%3f").replace("#", "%23"))
        conn = sqlite3.connect(uri, uri=True, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=15000")
        except sqlite3.Error:
            pass
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except sqlite3.Error:
                    pass
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # ---------------- 初始化 / 迁移 ----------------

    def init(self) -> None:
        with self._lock:
            conn = self.connect()
            cur = conn.cursor()
            for ddl in (SCHEMA_PRODUCTS, SCHEMA_SNAPSHOTS, SCHEMA_SETTINGS,
                        SCHEMA_LOGS, SCHEMA_DELETED):
                cur.execute(ddl)
            conn.commit()
            self._migrate(conn)
            for ddl in INDEXES:
                try:
                    cur.execute(ddl)
                except sqlite3.Error:
                    pass
            conn.commit()
            self._seed_settings(conn)

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        try:
            rows = conn.execute("PRAGMA table_info({})".format(table)).fetchall()
        except sqlite3.Error:
            return set()
        return {r["name"] for r in rows}

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧库缺字段时 ALTER TABLE 补齐，保证升级不丢数据。"""
        for table, cols in EXPECTED_COLUMNS.items():
            existing = self._table_columns(conn, table)
            if not existing:
                continue
            for name, decl in cols.items():
                if name in existing:
                    continue
                try:
                    conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, name, decl))
                except sqlite3.Error:
                    pass
            conn.commit()

    def _seed_settings(self, conn: sqlite3.Connection) -> None:
        for key, value in C.DEFAULT_SETTINGS.items():
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value)
                )
            except sqlite3.Error:
                pass
        conn.commit()

    # ---------------- settings ----------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            conn = self.connect()
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            if default is not None:
                return default
            return C.DEFAULT_SETTINGS.get(key)
        return row["value"]

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get_setting(key, default)))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get_setting(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.get_setting(key, "1" if default else "0")
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_setting(key, None)
        if not raw:
            return default if default is not None else None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default if default is not None else None

    def set_setting(self, key: str, value: Any) -> None:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            value = "1" if value else "0"
        with self._lock:
            conn = self.connect()
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            conn.commit()

    def set_settings(self, mapping: dict[str, Any]) -> None:
        with self._lock:
            conn = self.connect()
            with conn:
                for k, v in mapping.items():
                    if isinstance(v, (dict, list)):
                        v = json.dumps(v, ensure_ascii=False)
                    if isinstance(v, bool):
                        v = "1" if v else "0"
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, str(v)),
                    )

    # ---------------- products ----------------

    @staticmethod
    def _row_to_product(row: sqlite3.Row | None) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        d["active"] = int(d.get("active") or 0)
        return d

    def product_exists(self, pid: str) -> bool:
        with self._lock:
            conn = self.connect()
            row = conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone()
        return row is not None

    def get_product(self, pid: str) -> Optional[dict]:
        with self._lock:
            conn = self.connect()
            row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        return self._row_to_product(row)

    def add_product(self, pid: str, title: str = "", shop_name: str = "",
                    shop_id: str = "", cover: str = "", source: str = "manual") -> None:
        with self._lock:
            conn = self.connect()
            with conn:
                conn.execute(
                    "INSERT INTO products(id, title, shop_name, shop_id, cover, active, "
                    "created_at, source, fail_count, ignored) "
                    "VALUES(?,?,?,?,?,1,?,?,0,0) "
                    "ON CONFLICT(id) DO UPDATE SET active=1, "
                    "title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE products.title END, "
                    "shop_name=CASE WHEN excluded.shop_name<>'' THEN excluded.shop_name ELSE products.shop_name END, "
                    "shop_id=CASE WHEN excluded.shop_id<>'' THEN excluded.shop_id ELSE products.shop_id END",
                    (pid, title or "", shop_name or "", shop_id or "", cover or "",
                     C.now_str(), source),
                )
                conn.execute("DELETE FROM deleted_products WHERE id=?", (pid,))

    def update_product_meta(self, pid: str, **fields: Any) -> None:
        allowed = {"title", "shop_name", "shop_id", "cover", "active", "last_error",
                   "delisted_at", "delisted_reason", "last_ok_at", "last_method",
                   "fail_count", "fail_reason", "fail_at", "ignored"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append("{}=?".format(k))
                vals.append(v)
        if not sets:
            return
        vals.append(pid)
        with self._lock:
            conn = self.connect()
            with conn:
                conn.execute("UPDATE products SET {} WHERE id=?".format(", ".join(sets)), vals)

    def mark_delisted(self, pid: str, reason: str) -> None:
        """确认下架：active=0，记录时间与原因，清除临时失败状态，保留历史快照。"""
        self.update_product_meta(
            pid, active=0, delisted_at=C.now_str(), delisted_reason=reason,
            fail_reason="", fail_at=None, ignored=0, last_error="",
        )

    def mark_failure(self, pid: str, reason: str) -> None:
        with self._lock:
            conn = self.connect()
            with conn:
                conn.execute(
                    "UPDATE products SET fail_count=COALESCE(fail_count,0)+1, "
                    "fail_reason=?, fail_at=?, last_error=? WHERE id=?",
                    (reason, C.now_str(), reason, pid),
                )

    def mark_success(self, pid: str, method: str) -> None:
        with self._lock:
            conn = self.connect()
            with conn:
                conn.execute(
                    "UPDATE products SET last_ok_at=?, last_method=?, last_error='', "
                    "fail_reason='', fail_at=NULL, fail_count=0, ignored=0, active=1 "
                    "WHERE id=?",
                    (C.now_str(), method, pid),
                )

    def ignore_failure(self, pid: str) -> None:
        """忽略：只清除当前失败提示，下一轮继续采集。"""
        with self._lock:
            conn = self.connect()
            with conn:
                conn.execute(
                    "UPDATE products SET ignored=1, fail_reason='', fail_at=NULL WHERE id=?",
                    (pid,),
                )

    def restore_product(self, pid: str) -> None:
        with self._lock:
            conn = self.connect()
            with conn:
                conn.execute(
                    "UPDATE products SET active=1, delisted_at=NULL, delisted_reason='', "
                    "fail_reason='', fail_at=NULL, ignored=0 WHERE id=?",
                    (pid,),
                )

    def deactivate(self, pid: str) -> None:
        with self._lock:
            conn = self.connect()
            with conn:
                conn.execute("UPDATE products SET active=0 WHERE id=?", (pid,))

    def list_products(self, active_only: bool = False, include_delisted: bool = False) -> list[dict]:
        sql = "SELECT * FROM products"
        conds, vals = [], []
        if active_only:
            conds.append("active=1")
        if not include_delisted:
            conds.append("(delisted_at IS NULL OR delisted_at='')")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            conn = self.connect()
            rows = conn.execute(sql, vals).fetchall()
        return [self._row_to_product(r) for r in rows]

    def list_active_products(self) -> list[dict]:
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                "SELECT * FROM products WHERE active=1 AND (delisted_at IS NULL OR delisted_at='') "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_product(r) for r in rows]

    def list_failed_products(self) -> list[dict]:
        """失败列表：有 fail_at 且仍 active、未下架。"""
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                "SELECT * FROM products WHERE active=1 AND fail_at IS NOT NULL AND fail_at<>'' "
                "AND (delisted_at IS NULL OR delisted_at='') ORDER BY fail_at DESC"
            ).fetchall()
        return [self._row_to_product(r) for r in rows]

    def list_delisted_products(self) -> list[dict]:
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                "SELECT * FROM products WHERE delisted_at IS NOT NULL AND delisted_at<>'' "
                "ORDER BY delisted_at DESC"
            ).fetchall()
        return [self._row_to_product(r) for r in rows]

    def delete_product(self, pid: str) -> None:
        """删除商品及其历史数据，并登记到 deleted_products 防止自动拓品回填。"""
        with self._lock:
            conn = self.connect()
            with conn:
                row = conn.execute("SELECT title FROM products WHERE id=?", (pid,)).fetchone()
                title = row["title"] if row else ""
                conn.execute("DELETE FROM snapshots WHERE product_id=?", (pid,))
                conn.execute("DELETE FROM products WHERE id=?", (pid,))
                conn.execute(
                    "INSERT INTO deleted_products(id, title, deleted_at) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET deleted_at=excluded.deleted_at",
                    (pid, title, C.now_str()),
                )

    def delete_products(self, ids: Iterable[str]) -> int:
        n = 0
        for pid in list(ids):
            self.delete_product(pid)
            n += 1
        return n

    def is_deleted(self, pid: str) -> bool:
        with self._lock:
            conn = self.connect()
            row = conn.execute("SELECT 1 FROM deleted_products WHERE id=?", (pid,)).fetchone()
        return row is not None

    def deleted_ids(self) -> set[str]:
        with self._lock:
            conn = self.connect()
            rows = conn.execute("SELECT id FROM deleted_products").fetchall()
        return {r["id"] for r in rows}

    def shop_names(self) -> list[str]:
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                "SELECT DISTINCT shop_name FROM products "
                "WHERE shop_name IS NOT NULL AND shop_name<>'' "
                "AND (delisted_at IS NULL OR delisted_at='') ORDER BY shop_name"
            ).fetchall()
        return [r["shop_name"] for r in rows]

    # ---------------- snapshots ----------------

    def add_snapshot(self, pid: str, captured_at: str, sold: Optional[int],
                     shop_sold: Optional[int], price: Optional[float],
                     fans: Optional[int], stock_status: Optional[int],
                     deliverable: Optional[int], method: str = "") -> None:
        """写入一条快照。同一 (product_id, captured_at) 已存在时覆盖。"""
        with self._lock:
            conn = self.connect()
            with conn:
                conn.execute(
                    "INSERT INTO snapshots(product_id, captured_at, sold, shop_sold, price, "
                    "fans, stock_status, deliverable, method) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(product_id, captured_at) DO UPDATE SET "
                    "sold=excluded.sold, shop_sold=excluded.shop_sold, price=excluded.price, "
                    "fans=excluded.fans, stock_status=excluded.stock_status, "
                    "deliverable=excluded.deliverable, method=excluded.method",
                    (pid, captured_at, sold, shop_sold, price, fans, stock_status,
                     deliverable, method),
                )

    def latest_snapshot(self, pid: str) -> Optional[dict]:
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                "SELECT * FROM snapshots WHERE product_id=? ORDER BY captured_at DESC LIMIT 1",
                (pid,),
            ).fetchone()
        return dict(row) if row else None

    def snapshots_between(self, pid: str, start: str, end: str) -> list[dict]:
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE product_id=? AND captured_at>=? AND captured_at<=? "
                "ORDER BY captured_at ASC",
                (pid, start, end),
            ).fetchall()
        return [dict(r) for r in rows]

    def all_snapshots(self, pid: str) -> list[dict]:
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE product_id=? ORDER BY captured_at ASC", (pid,)
            ).fetchall()
        return [dict(r) for r in rows]

    def snapshot_count(self) -> int:
        with self._lock:
            conn = self.connect()
            row = conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()
        return int(row["c"]) if row else 0

    # ---------------- logs ----------------

    def log(self, level: str, source: str, message: str, detail: str = "",
            product_id: str = "", title: str = "") -> None:
        level = (level or "INFO").upper()
        if level not in LEVELS:
            level = "INFO"
        message = _sanitize(message)
        detail = _sanitize(detail)
        try:
            with self._lock:
                conn = self.connect()
                with conn:
                    conn.execute(
                        "INSERT INTO logs(ts, level, source, product_id, title, message, detail) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (C.now_str(), level, source, product_id or "", title or "",
                         message, detail),
                    )
        except sqlite3.Error:
            pass  # 日志失败不能影响主流程

    def query_logs(self, level: str = "ALL", keyword: str = "",
                   limit: int = C.LOG_PAGE_SIZE) -> list[dict]:
        sql = "SELECT * FROM logs WHERE 1=1"
        vals: list[Any] = []
        if level and level != "ALL":
            if level == "SUCCESS":
                sql += " AND level IN ('SUCCESS','INFO')"
            else:
                sql += " AND level=?"
                vals.append(level)
        if keyword:
            sql += " AND (message LIKE ? OR title LIKE ? OR product_id LIKE ? OR detail LIKE ?)"
            kw = "%{}%".format(keyword)
            vals.extend([kw, kw, kw, kw])
        sql += " ORDER BY id DESC LIMIT ?"
        vals.append(int(limit))
        with self._lock:
            conn = self.connect()
            rows = conn.execute(sql, vals).fetchall()
        return [dict(r) for r in rows]

    def clear_logs(self) -> int:
        with self._lock:
            conn = self.connect()
            with conn:
                cur = conn.execute("DELETE FROM logs")
                return cur.rowcount or 0

    def log_count(self) -> int:
        with self._lock:
            conn = self.connect()
            row = conn.execute("SELECT COUNT(*) AS c FROM logs").fetchone()
        return int(row["c"]) if row else 0

    # ---------------- 维护 ----------------

    def db_size_bytes(self) -> int:
        """数据库占用（含 -wal/-shm）。"""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def cleanup(self, snapshot_keep_days: int = 90, log_keep_days: int = 30) -> dict:
        """一键清理：保留近 N 天快照 + 每个商品一条更早基线，日志保留 N 天。

        基线保留是硬性要求——删掉早期基线会导致近期销量无法计算。
        """
        from datetime import timedelta
        now = C.now()
        snap_cut = C.fmt(now - timedelta(days=max(1, int(snapshot_keep_days))))
        log_cut = C.fmt(now - timedelta(days=max(1, int(log_keep_days))))

        result = {"snapshots_deleted": 0, "logs_deleted": 0, "baselines_kept": 0,
                  "freed": 0, "size_before": 0, "size_after": 0}
        with self._lock:
            conn = self.connect()
            result["size_before"] = self.db_size_bytes()
            try:
                before = conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()
                before_n = int(before["c"]) if before else 0

                with conn:
                    cur = conn.execute("DELETE FROM logs WHERE ts < ?", (log_cut,))
                    result["logs_deleted"] = cur.rowcount or 0

                    # 每个商品在切点之前保留最后一条快照作为基线，其余旧快照删除。
                    # 只删除 captured_at < snap_cut 的行，新快照一律不动。
                    conn.execute("DROP TABLE IF EXISTS _baseline")
                    conn.execute(
                        "CREATE TEMP TABLE _baseline AS "
                        "SELECT product_id, MAX(captured_at) AS ts FROM snapshots "
                        "WHERE captured_at < ? GROUP BY product_id",
                        (snap_cut,),
                    )
                    cur = conn.execute(
                        "DELETE FROM snapshots WHERE captured_at < ? AND NOT EXISTS ("
                        "  SELECT 1 FROM _baseline b WHERE b.product_id = snapshots.product_id"
                        "  AND b.ts = snapshots.captured_at)",
                        (snap_cut,),
                    )
                    result["snapshots_deleted"] = cur.rowcount or 0

                    row = conn.execute("SELECT COUNT(*) AS c FROM _baseline").fetchone()
                    result["baselines_kept"] = int(row["c"]) if row else 0
                    conn.execute("DROP TABLE IF EXISTS _baseline")

                after = conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()
                after_n = int(after["c"]) if after else 0
                result["snapshots_deleted"] = max(0, before_n - after_n)
            except sqlite3.Error as exc:
                result["error"] = str(exc)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            try:
                conn.execute("VACUUM")
            except sqlite3.Error:
                pass
            result["size_after"] = self.db_size_bytes()
        result["freed"] = max(0, result["size_before"] - result["size_after"])
        return result

    def purge_old_snapshots(self, keep_days: int = 90) -> int:
        """仅清理快照（保留基线），返回估算删除条数。"""
        from datetime import timedelta
        cut = C.fmt(C.now() - timedelta(days=max(1, int(keep_days))))
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM snapshots WHERE captured_at < ?", (cut,)
            ).fetchone()
        return int(row["c"]) if row else 0


def _sanitize(text: str) -> str:
    """日志脱敏：绝不落盘 Cookie / Webhook 完整值。"""
    if not text:
        return ""
    s = str(text)
    lowered = s.lower()
    for marker in ("cookie", "webhook", "authorization", "set-cookie", "token="):
        if marker in lowered:
            # 逐行处理，保留结构但截断敏感值
            out_lines = []
            for line in s.splitlines():
                low = line.lower()
                if any(m in low for m in ("cookie", "webhook", "authorization", "token=")):
                    if "webhook" in low and "key=" in low:
                        head = line.split("key=")[0]
                        out_lines.append(head + "key=***")
                    else:
                        out_lines.append(line.split(":")[0] + ": ***")
                else:
                    out_lines.append(line)
            s = "\n".join(out_lines)
            break
    return s[:20000]
