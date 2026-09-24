# -*- coding: utf-8 -*-
"""红薯雷达 —— 销量统计口径。

设计原则（GUI 与只读 MCP 共用同一套函数，保证两处结果永远一致）：

  * 所有销量都来自「累计已售」快照的差值，绝不把累计已售当当日销量。
  * 累计已售按**高水位**处理：取历史可信最大值，偶发读到更小的值不回退。
  * 缺少足够基线时返回 None，界面显示「—」，绝不伪造 0。
  * 只有两个真实快照累计值确实相等，增量才显示 0。
  * 0 点快照既是前一天结束值，也是新一天基线，不重复计入两天。
  * 中间才开始监控时用当天第一条真实快照作为起始基线，并标记为不完整口径。
"""

from __future__ import annotations

import bisect
import datetime as _dt
import sqlite3
from typing import Any, Iterable, Optional, Sequence

import config as C

DASH = "—"
FUZZY_NOTE = "模糊试算，不代表真实成交额"


# --------------------------------------------------------------------------
# 快照序列（前缀高水位，O(log n) 查询任意时刻的累计高水位）
# --------------------------------------------------------------------------

class SnapshotSeries:
    """某个商品的全部快照，按时间升序；提供任意时刻的高水位与前值查询。"""

    __slots__ = ("rows", "times", "_pref_max", "_pref_has", "_last_price", "_last_shop_sold")

    def __init__(self, rows: Sequence[dict]):
        self.rows = list(rows)
        self.times = [str(r.get("captured_at") or "") for r in self.rows]
        pref_max: list[Optional[int]] = []
        pref_has: list[bool] = []
        cur_max: Optional[int] = None
        cur_has = False
        for r in self.rows:
            v = r.get("sold")
            if v is not None:
                iv = int(v)
                if cur_max is None or iv > cur_max:
                    cur_max = iv
                cur_has = True
            pref_max.append(cur_max)
            pref_has.append(cur_has)
        self._pref_max = pref_max
        self._pref_has = pref_has

        # 每个下标处「最近一次已知价格 / 店铺销量」
        last_price: list[Optional[float]] = []
        last_shop: list[Optional[int]] = []
        p: Optional[float] = None
        s: Optional[int] = None
        for r in self.rows:
            if r.get("price") is not None:
                p = float(r["price"])
            if r.get("shop_sold") is not None:
                s = int(r["shop_sold"])
            last_price.append(p)
            last_shop.append(s)
        self._last_price = last_price
        self._last_shop_sold = last_shop

    def __len__(self) -> int:
        return len(self.rows)

    def _idx(self, ts: str) -> int:
        """返回 captured_at <= ts 的元素个数。"""
        return bisect.bisect_right(self.times, ts)

    # ---- 高水位 ----

    def hwm(self, ts: _dt.datetime | str) -> Optional[int]:
        """累计已售高水位（截至 ts，含）。无任何可信读数返回 None。"""
        key = ts if isinstance(ts, str) else C.fmt(ts)
        i = self._idx(key)
        if i <= 0:
            return None
        if not self._pref_has[i - 1]:
            return None
        return self._pref_max[i - 1]

    def has_data(self, ts: _dt.datetime | str) -> bool:
        """ts 之前是否存在任何快照（用于区分「没有基线」与「基线为 0」）。"""
        key = ts if isinstance(ts, str) else C.fmt(ts)
        return self._idx(key) > 0

    def latest_value(self, ts: _dt.datetime | str) -> Optional[int]:
        """ts（含）之前最后一条快照的原始 sold 值。"""
        key = ts if isinstance(ts, str) else C.fmt(ts)
        i = self._idx(key)
        for j in range(i - 1, -1, -1):
            v = self.rows[j].get("sold")
            if v is not None:
                return int(v)
        return None

    def price_at(self, ts: _dt.datetime | str) -> Optional[float]:
        key = ts if isinstance(ts, str) else C.fmt(ts)
        i = self._idx(key)
        if i <= 0:
            return None
        return self._last_price[i - 1]

    def latest_price(self) -> Optional[float]:
        if not self.rows:
            return None
        return self._last_price[-1]

    def shop_sold_at(self, ts: _dt.datetime | str) -> Optional[int]:
        key = ts if isinstance(ts, str) else C.fmt(ts)
        i = self._idx(key)
        if i <= 0:
            return None
        return self._last_shop_sold[i - 1]

    def last_time(self) -> Optional[str]:
        return self.times[-1] if self.times else None

    def first_time(self) -> Optional[str]:
        return self.times[0] if self.times else None

    # ---- 区间内是否有真实快照 ----

    def count_between(self, start: _dt.datetime, end: _dt.datetime) -> int:
        a = C.fmt(start)
        b = C.fmt(end)
        return max(0, bisect.bisect_right(self.times, b) - bisect.bisect_left(self.times, a))

    def rows_between(self, start: _dt.datetime, end: _dt.datetime) -> list[dict]:
        a = C.fmt(start)
        b = C.fmt(end)
        i = bisect.bisect_left(self.times, a)
        j = bisect.bisect_right(self.times, b)
        return self.rows[i:j]


# --------------------------------------------------------------------------
# 统计引擎
# --------------------------------------------------------------------------

class Analytics:
    """基于快照的销量统计。conn 可以是可写连接，也可以是 MCP 的只读连接。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- 基础读取 ----------------

    def _series(self, pid: str) -> SnapshotSeries:
        rows = self.conn.execute(
            "SELECT * FROM snapshots WHERE product_id=? ORDER BY captured_at ASC", (pid,)
        ).fetchall()
        return SnapshotSeries([dict(r) for r in rows])

    def _series_many(self, pids: Iterable[str]) -> dict[str, SnapshotSeries]:
        pids = list(pids)
        out: dict[str, SnapshotSeries] = {p: SnapshotSeries([]) for p in pids}
        if not pids:
            return out
        chunk = 400
        for i in range(0, len(pids), chunk):
            part = pids[i:i + chunk]
            marks = ",".join("?" * len(part))
            rows = self.conn.execute(
                "SELECT * FROM snapshots WHERE product_id IN ({}) ORDER BY product_id, captured_at"
                .format(marks), part
            ).fetchall()
            buckets: dict[str, list[dict]] = {p: [] for p in part}
            for r in rows:
                d = dict(r)
                buckets.setdefault(d["product_id"], []).append(d)
            for p in part:
                out[p] = SnapshotSeries(buckets.get(p, []))
        return out

    def all_products(self, include_delisted: bool = False) -> list[dict]:
        sql = "SELECT * FROM products"
        if not include_delisted:
            sql += " WHERE (delisted_at IS NULL OR delisted_at='')"
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def product(self, pid: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None

    def data_cutoff(self) -> Optional[str]:
        row = self.conn.execute("SELECT MAX(captured_at) AS m FROM snapshots").fetchone()
        return (row["m"] if row else None) or None

    # ---------------- 单商品指标 ----------------

    def today_sales(self, pid: str, ref: _dt.datetime | None = None,
                    series: SnapshotSeries | None = None) -> tuple[Optional[int], bool]:
        """返回 (今日销量, 是否为不完整口径)。"""
        ref = ref or C.now()
        s = series or self._series(pid)
        d0 = C.day_start(ref)
        cur = s.hwm(ref)
        if cur is None:
            return None, False
        base = s.hwm(d0)
        partial = False
        if base is None:
            # 当天中途才开始监控：用当天第一条真实成功快照作为起始基线
            today_rows = s.rows_between(d0, ref)
            first = next((r for r in today_rows if r.get("sold") is not None), None)
            if first is None:
                return None, True
            base = int(first["sold"])
            partial = True
        return max(0, cur - base), partial

    def yesterday_sales(self, pid: str, ref: _dt.datetime | None = None,
                        series: SnapshotSeries | None = None) -> Optional[int]:
        ref = ref or C.now()
        s = series or self._series(pid)
        d0 = C.day_start(ref)
        dprev = C.add_days(d0, -1)
        start = s.hwm(dprev)
        end = s.hwm(d0)
        if start is None or end is None:
            return None
        return max(0, end - start)

    def last_hour_sales(self, pid: str, ref: _dt.datetime | None = None,
                        series: SnapshotSeries | None = None) -> Optional[int]:
        """上小时销量 = 当前整点累计高水位 − 上一个完整整点小时的基线。"""
        ref = ref or C.now()
        s = series or self._series(pid)
        cur_hour = C.hour_start(ref)
        prev_hour = C.add_hours(cur_hour, -1)
        end = s.hwm(cur_hour)
        start = s.hwm(prev_hour)
        if end is None or start is None:
            return None
        return max(0, end - start)

    def day_sales(self, pid: str, day: _dt.datetime, series: SnapshotSeries | None = None
                  ) -> Optional[int]:
        """某自然日销量：次日 00:00 高水位 − 当日 00:00 高水位。"""
        s = series or self._series(pid)
        d0 = C.day_start(day)
        start = s.hwm(d0)
        end = s.hwm(C.add_days(d0, 1))
        if start is None or end is None:
            return None
        return max(0, end - start)

    def total_sold(self, pid: str, series: SnapshotSeries | None = None) -> Optional[int]:
        s = series or self._series(pid)
        if not len(s):
            return None
        return s.hwm(s.last_time() or C.now_str())

    # ---------------- 序列 ----------------

    def hourly_series(self, pid: str, day: _dt.datetime | str,
                      series: SnapshotSeries | None = None) -> list[dict]:
        """某日 24 小时的销量增量与模糊销售额。

        每个整点区间内若没有任何真实快照，该小时判定为「无数据」，sales=None，
        避免把「采集失败」误显示成「0 销量」。
        """
        s = series or self._series(pid)
        d0 = C.day_start(_to_dt(day))
        ref = C.now()
        base = s.hwm(d0)
        partial = False
        if base is None:
            today_rows = s.rows_between(d0, d0 + _dt.timedelta(days=1))
            first = next((r for r in today_rows if r.get("sold") is not None), None)
            if first is not None:
                base = int(first["sold"])
                partial = True

        out: list[dict] = []
        running = base
        for h in range(24):
            hs = C.add_hours(d0, h)
            he = C.add_hours(d0, h + 1)
            hour_rows = s.rows_between(hs, he - _dt.timedelta(seconds=1))
            has = len(hour_rows) > 0
            price = None
            if hour_rows:
                price = next((float(r["price"]) for r in reversed(hour_rows)
                              if r.get("price") is not None), None)
            if price is None:
                price = s.price_at(he - _dt.timedelta(seconds=1))
            sales = None
            if has and running is not None:
                hwm = running
                for r in hour_rows:
                    v = r.get("sold")
                    if v is not None and int(v) > hwm:
                        hwm = int(v)
                sales = max(0, hwm - running)
                running = hwm
            out.append({
                "hour": h,
                "label": "{:02d}:00".format(h),
                "time": C.fmt(hs),
                "sales": sales,
                "has_data": has,
                "price": price,
                "amount": (sales * price) if (sales is not None and price is not None) else None,
                "partial": partial,
                "future": hs > ref,
            })
        return out

    def daily_series(self, pid: str, days: Sequence[_dt.datetime] | int,
                     series: SnapshotSeries | None = None) -> list[dict]:
        """多日每日销量。days 可以是天数（含今天往前推）或日期列表。"""
        s = series or self._series(pid)
        if isinstance(days, int):
            today = C.day_start(C.now())
            day_list = [C.add_days(today, -i) for i in range(days - 1, -1, -1)]
        else:
            day_list = [C.day_start(_to_dt(d)) for d in days]

        out: list[dict] = []
        for d in day_list:
            nxt = C.add_days(d, 1)
            start = s.hwm(d)
            end = s.hwm(nxt)
            price = s.price_at(nxt)
            if start is None or end is None:
                sales = None
            else:
                sales = max(0, end - start)
            out.append({
                "date": d.strftime(C.DAY_FMT),
                "label": d.strftime("%m-%d"),
                "sales": sales,
                "price": price,
                "amount": (sales * price) if (sales is not None and price is not None) else None,
            })
        return out

    # ---------------- 看板批量 ----------------

    def product_rows(self, products: Sequence[dict] | None = None,
                     ref: _dt.datetime | None = None) -> list[dict]:
        """竞品看板 / 店铺分析共用的批量计算，一次查询算完全部商品。"""
        ref = ref or C.now()
        prods = list(products) if products is not None else self.all_products()
        pids = [p["id"] for p in prods]
        series_map = self._series_many(pids)

        d0 = C.day_start(ref)
        rows: list[dict] = []
        for p in prods:
            pid = p["id"]
            s = series_map.get(pid) or SnapshotSeries([])
            today, partial = self.today_sales(pid, ref, s)
            yesterday = self.yesterday_sales(pid, ref, s)
            last_hour = self.last_hour_sales(pid, ref, s)
            price = s.latest_price()
            total = s.hwm(s.last_time() or C.fmt(ref)) if len(s) else None
            last_time = s.last_time()
            ignored = int(p.get("ignored") or 0)
            delisted = bool(p.get("delisted_at"))
            if delisted:
                status, status_text = "delisted", "已下架"
            elif last_time is None:
                status, status_text = "pending", "待采集"
            elif p.get("fail_at") and not ignored:
                status, status_text = "error", "异常"
            else:
                status, status_text = "normal", "正常"
            rows.append({
                "id": pid,
                "title": p.get("title") or pid,
                "shop_name": p.get("shop_name") or "",
                "shop_id": p.get("shop_id") or "",
                "cover": p.get("cover") or "",
                "price": price,
                "today": today,
                "today_partial": partial,
                "yesterday": yesterday,
                "last_hour": last_hour,
                "total_sold": total,
                "last_captured": last_time,
                "status": status,
                "status_text": status_text,
                "last_method": p.get("last_method") or "",
                "fail_reason": p.get("fail_reason") or "",
                "fail_at": p.get("fail_at"),
                "delisted_at": p.get("delisted_at"),
                "delisted_reason": p.get("delisted_reason"),
                "created_at": p.get("created_at"),
                "amount_today": (today * price) if (today is not None and price) else None,
            })
        return rows

    # ---------------- 店铺维度 ----------------

    def shop_rows(self, ref: _dt.datetime | None = None) -> list[dict]:
        ref = ref or C.now()
        products = self.all_products()
        rows = self.product_rows(products, ref)
        agg: dict[str, dict] = {}
        for r in rows:
            key = r["shop_name"] or "（未知店铺）"
            item = agg.setdefault(key, {
                "shop_name": key,
                "shop_id": r["shop_id"],
                "product_count": 0,
                "total_sold": 0,
                "total_sold_known": False,
                "today": 0,
                "today_known": False,
                "yesterday": 0,
                "yesterday_known": False,
                "last_hour": 0,
                "last_hour_known": False,
                "amount_today": 0.0,
                "products": [],
            })
            item["product_count"] += 1
            item["products"].append(r)
            if r["total_sold"] is not None:
                item["total_sold"] += r["total_sold"]
                item["total_sold_known"] = True
            if r["today"] is not None:
                item["today"] += r["today"]
                item["today_known"] = True
            if r["yesterday"] is not None:
                item["yesterday"] += r["yesterday"]
                item["yesterday_known"] = True
            if r["last_hour"] is not None:
                item["last_hour"] += r["last_hour"]
                item["last_hour_known"] = True
            if r["amount_today"]:
                item["amount_today"] += r["amount_today"]
        out = []
        for item in agg.values():
            out.append({
                "shop_name": item["shop_name"],
                "shop_id": item["shop_id"],
                "product_count": item["product_count"],
                "total_sold": item["total_sold"] if item["total_sold_known"] else None,
                "today": item["today"] if item["today_known"] else None,
                "yesterday": item["yesterday"] if item["yesterday_known"] else None,
                "last_hour": item["last_hour"] if item["last_hour_known"] else None,
                "amount_today": round(item["amount_today"], 2),
            })
        out.sort(key=lambda x: (-(x["today"] or 0), x["shop_name"]))
        return out

    def _shop_pids(self, shop_name: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT id FROM products WHERE shop_name=? AND (delisted_at IS NULL OR delisted_at='')",
            (shop_name,),
        ).fetchall()
        return [r["id"] for r in rows]

    def shop_hourly_series(self, shop_name: str, day: _dt.datetime | str) -> list[dict]:
        """店铺某日每小时销量（店内在监控商品的合计）。"""
        pids = self._shop_pids(shop_name)
        merged = [{"hour": h, "label": "{:02d}:00".format(h), "time": "", "sales": None,
                   "has_data": False, "price": None, "amount": None, "future": False}
                  for h in range(24)]
        any_data = [False] * 24
        amount_acc = [0.0] * 24
        amount_known = [False] * 24
        series_map = self._series_many(pids) if pids else {}
        for pid in pids:
            hs = self.hourly_series(pid, day, series_map.get(pid))
            for h, item in enumerate(hs):
                if item["has_data"] and item["sales"] is not None:
                    merged[h]["sales"] = (merged[h]["sales"] or 0) + item["sales"]
                    any_data[h] = True
                    merged[h]["time"] = item["time"]
                    if item["amount"] is not None:
                        amount_acc[h] += item["amount"]
                        amount_known[h] = True
                if item.get("future"):
                    merged[h]["future"] = True
        for h in range(24):
            merged[h]["has_data"] = any_data[h]
            if not any_data[h]:
                merged[h]["sales"] = None
            merged[h]["amount"] = round(amount_acc[h], 2) if amount_known[h] else None
        return merged

    def shop_daily_series(self, shop_name: str, days: Sequence[_dt.datetime] | int) -> list[dict]:
        pids = self._shop_pids(shop_name)
        series_map = self._series_many(pids) if pids else {}
        if isinstance(days, int):
            today = C.day_start(C.now())
            day_list = [C.add_days(today, -i) for i in range(days - 1, -1, -1)]
        else:
            day_list = [C.day_start(_to_dt(d)) for d in days]
        out = []
        for d in day_list:
            acc, known, amt, amt_known = 0, False, 0.0, False
            for pid in pids:
                hs = self.daily_series(pid, [d], series_map.get(pid))[0]
                if hs["sales"] is not None:
                    acc += hs["sales"]
                    known = True
                if hs["amount"] is not None:
                    amt += hs["amount"]
                    amt_known = True
            out.append({
                "date": d.strftime(C.DAY_FMT),
                "label": d.strftime("%m-%d"),
                "sales": acc if known else None,
                "amount": round(amt, 2) if amt_known else None,
                "product_count": len(pids),
            })
        return out

    def shop_detail(self, shop_name: str, ref: _dt.datetime | None = None) -> dict:
        ref = ref or C.now()
        pids = self._shop_pids(shop_name)
        products = [self.product(p) for p in pids]
        products = [p for p in products if p]
        rows = self.product_rows(products, ref)
        total_known = [r["total_sold"] for r in rows if r["total_sold"] is not None]
        return {
            "shop_name": shop_name,
            "product_count": len(rows),
            "total_sold": sum(total_known) if total_known else None,
            "today": sum(r["today"] for r in rows if r["today"] is not None) if any(
                r["today"] is not None for r in rows) else None,
            "last_hour": sum(r["last_hour"] for r in rows if r["last_hour"] is not None) if any(
                r["last_hour"] is not None for r in rows) else None,
            "products": rows,
        }

    # ---------------- 排名 ----------------

    RANK_FIELDS = {
        "today": "今日销量",
        "yesterday": "昨日销量",
        "last_hour": "上小时销量",
        "total_sold": "累计已售",
        "amount_today": "模糊销售额",
    }

    def product_ranking(self, kind: str = "today", limit: int = 50,
                        ref: _dt.datetime | None = None) -> dict:
        if kind not in self.RANK_FIELDS:
            kind = "today"
        rows = self.product_rows(None, ref)
        rows = [r for r in rows if r.get(kind) is not None]
        rows.sort(key=lambda r: -(r.get(kind) or 0))
        return {
            "basis": self.RANK_FIELDS[kind],
            "data_time": self.data_cutoff(),
            "count": len(rows[:limit]),
            "items": [
                {
                    "rank": i + 1,
                    "id": r["id"],
                    "title": r["title"],
                    "shop_name": r["shop_name"],
                    "value": r.get(kind),
                    "price": r["price"],
                    "today": r["today"],
                    "yesterday": r["yesterday"],
                    "last_hour": r["last_hour"],
                    "total_sold": r["total_sold"],
                }
                for i, r in enumerate(rows[:limit])
            ],
        }

    def shop_ranking(self, kind: str = "today", limit: int = 50,
                     ref: _dt.datetime | None = None) -> dict:
        if kind not in ("today", "yesterday", "last_hour"):
            kind = "today"
        rows = self.shop_rows(ref)
        rows = [r for r in rows if r.get(kind) is not None]
        rows.sort(key=lambda r: -(r.get(kind) or 0))
        return {
            "basis": self.RANK_FIELDS[kind],
            "data_time": self.data_cutoff(),
            "count": len(rows[:limit]),
            "items": [
                {"rank": i + 1, "shop_name": r["shop_name"], "value": r.get(kind),
                 "product_count": r["product_count"], "today": r["today"],
                 "yesterday": r["yesterday"], "last_hour": r["last_hour"],
                 "total_sold": r["total_sold"]}
                for i, r in enumerate(rows[:limit])
            ],
        }

    # ---------------- 搜索 / 概览 ----------------

    def search_products(self, keyword: str = "", shop: str = "",
                        status: str = "all", limit: int = 200,
                        ref: _dt.datetime | None = None) -> list[dict]:
        rows = self.product_rows(None, ref)
        out = []
        kw = (keyword or "").strip().lower()
        for r in rows:
            if shop and (r["shop_name"] or "") != shop:
                continue
            if status and status != "all" and r["status"] != status:
                continue
            if kw and kw not in (r["title"] or "").lower() \
                    and kw not in (r["shop_name"] or "").lower() \
                    and kw not in (r["id"] or "").lower():
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def overview(self, ref: _dt.datetime | None = None) -> dict:
        ref = ref or C.now()
        rows = self.product_rows(None, ref)
        shops = {r["shop_name"] for r in rows if r["shop_name"]}
        def _sum(key):
            vals = [r[key] for r in rows if r.get(key) is not None]
            return sum(vals) if vals else None
        return {
            "product_count": len(rows),
            "shop_count": len(shops),
            "today_sales": _sum("today"),
            "yesterday_sales": _sum("yesterday"),
            "last_hour_sales": _sum("last_hour"),
            "total_sold": _sum("total_sold"),
            "amount_today": round(sum(r["amount_today"] for r in rows if r["amount_today"]), 2),
            "data_time": self.data_cutoff(),
            "query_time": C.now_str(),
            "basis": "销量均为累计已售快照差值；模糊销售额为销量×当时价格，非真实成交额",
            "partial_today": any(r["today_partial"] for r in rows),
        }


# --------------------------------------------------------------------------

def _to_dt(value: _dt.datetime | str) -> _dt.datetime:
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=C.TZ)
    dt = C.to_dt(value)
    if dt is None:
        return C.now()
    return dt
