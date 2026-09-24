# -*- coding: utf-8 -*-
"""红薯雷达 —— 只读 MCP 数据服务（stdio）。

刻意只用标准库实现 JSON-RPC 2.0 over stdio，好处：
  * 打包后无需额外依赖，单文件 EXE 体积可控；
  * 数据库以 URI `mode=ro` + `PRAGMA query_only` 双重锁定，物理上无法写入；
  * 全程只暴露 8 个读取类工具，没有任何增删改能力。

统计口径与桌面界面共用 analytics.py 的同一套函数，保证两处结果一致。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import sys
import traceback
from typing import Any, Callable

import config as C
import database as D
import analytics as A

SERVER_NAME = "xhs-sales-monitor"
SERVER_VERSION = C.APP_VERSION
DEFAULT_PROTOCOL = "2024-11-05"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")


# --------------------------------------------------------------------------
# 工具定义
# --------------------------------------------------------------------------

def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: list[dict] = [
    {
        "name": "list_shops",
        "description": "列出所有在监控的店铺，含监控商品数、今日销量、上小时销量、累计已售。",
        "inputSchema": _schema({}),
    },
    {
        "name": "list_products",
        "description": "列出监控中的商品，可按店铺、状态筛选。返回今日/昨日/上小时销量、累计已售与价格。",
        "inputSchema": _schema({
            "shop": {"type": "string", "description": "店铺名称，精确匹配"},
            "keyword": {"type": "string", "description": "标题或商品ID关键词"},
            "status": {"type": "string", "enum": ["all", "normal", "pending", "error", "delisted"],
                       "description": "商品状态，默认 all"},
            "limit": {"type": "integer", "description": "返回条数上限，默认 100"},
        }),
    },
    {
        "name": "get_product_sales",
        "description": "查询单个商品的销量：某日 24 小时逐时销量，或近 N 天每日销量。",
        "inputSchema": _schema({
            "product_id": {"type": "string", "description": "24位商品ID"},
            "mode": {"type": "string", "enum": ["hourly", "daily"],
                     "description": "hourly=某日逐时；daily=近N天每日。默认 hourly"},
            "date": {"type": "string", "description": "YYYY-MM-DD，hourly 模式使用，默认今天"},
            "days": {"type": "integer", "description": "daily 模式天数，默认 7"},
        }, ["product_id"]),
    },
    {
        "name": "get_shop_sales",
        "description": "查询某个店铺的销量：某日逐时，或近 N 天每日（店内在监控商品合计）。",
        "inputSchema": _schema({
            "shop_name": {"type": "string", "description": "店铺名称"},
            "mode": {"type": "string", "enum": ["hourly", "daily"], "description": "默认 hourly"},
            "date": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
            "days": {"type": "integer", "description": "daily 模式天数，默认 7"},
        }, ["shop_name"]),
    },
    {
        "name": "get_product_ranking",
        "description": "商品排行，支持按今日、昨日、上小时、累计已售、模糊销售额排名。",
        "inputSchema": _schema({
            "kind": {"type": "string",
                     "enum": ["today", "yesterday", "last_hour", "total_sold", "amount_today"],
                     "description": "排名维度，默认 today"},
            "limit": {"type": "integer", "description": "条数上限，默认 20"},
        }),
    },
    {
        "name": "get_shop_ranking",
        "description": "店铺排行，支持按今日、昨日、上小时销量排名。",
        "inputSchema": _schema({
            "kind": {"type": "string", "enum": ["today", "yesterday", "last_hour"],
                     "description": "排名维度，默认 today"},
            "limit": {"type": "integer", "description": "条数上限，默认 20"},
        }),
    },
    {
        "name": "search_products",
        "description": "按标题、店铺或商品 ID 搜索监控中的商品。",
        "inputSchema": _schema({
            "keyword": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "条数上限，默认 50"},
        }, ["keyword"]),
    },
    {
        "name": "get_overview",
        "description": "总览：商品数、店铺数、今日/昨日/上小时销量、数据截止时间与统计口径。",
        "inputSchema": _schema({}),
    },
]


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------

class Tools:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.ana = A.Analytics(conn)

    # ---- 通用 ----

    def _envelope(self, data: Any, basis: str) -> dict:
        return {
            "data": data,
            "basis": basis,
            "data_time": self.ana.data_cutoff(),
            "query_time": C.now_str(),
            "timezone": "Asia/Shanghai",
            "source": C.APP_NAME,
        }

    @staticmethod
    def _product_brief(r: dict) -> dict:
        return {
            "id": r["id"],
            "title": r["title"],
            "shop_name": r["shop_name"],
            "price": r["price"],
            "today_sales": r["today"],
            "today_partial": r["today_partial"],
            "yesterday_sales": r["yesterday"],
            "last_hour_sales": r["last_hour"],
            "total_sold": r["total_sold"],
            "status": r["status"],
            "last_captured": r["last_captured"],
            "fuzzy_amount_today": r["amount_today"],
        }

    # ---- 各工具 ----

    def list_shops(self, params: dict) -> dict:
        rows = self.ana.shop_rows()
        data = [{
            "shop_name": r["shop_name"],
            "shop_id": r["shop_id"],
            "product_count": r["product_count"],
            "today_sales": r["today"],
            "yesterday_sales": r["yesterday"],
            "last_hour_sales": r["last_hour"],
            "total_sold": r["total_sold"],
            "fuzzy_amount_today": r["amount_today"],
        } for r in rows]
        return self._envelope(data, "销量为累计已售快照差值，按店铺合计；销售额为模糊试算")

    def list_products(self, params: dict) -> dict:
        limit = int(params.get("limit") or 100)
        rows = self.ana.search_products(
            keyword=params.get("keyword") or "",
            shop=params.get("shop") or "",
            status=params.get("status") or "all",
            limit=max(1, min(limit, 1000)),
        )
        return self._envelope(
            [self._product_brief(r) for r in rows],
            "销量为累计已售快照差值；价格为最近一次采集的实际到手价")

    def get_product_sales(self, params: dict) -> dict:
        pid = str(params.get("product_id") or "").strip().lower()
        if not pid:
            raise ValueError("缺少参数 product_id")
        prod = self.ana.product(pid)
        if prod is None:
            raise ValueError("未找到商品 {}（可能未监控或已删除）".format(pid))
        mode = params.get("mode") or "hourly"
        if mode == "daily":
            days = int(params.get("days") or 7)
            days = max(1, min(days, 90))
            series = self.ana.daily_series(pid, days)
            basis = "每日销量 = 次日00:00累计高水位 − 当日00:00累计高水位"
        else:
            day = params.get("date") or C.now().strftime(C.DAY_FMT)
            series = self.ana.hourly_series(pid, day)
            basis = "逐时销量 = 相邻整点累计高水位差值；未采到数据的小时返回 null"
        row = self.ana.product_rows([prod])[0]
        return self._envelope({
            "product": self._product_brief(row),
            "mode": mode,
            "series": series,
            "fuzzy_note": A.FUZZY_NOTE,
        }, basis)

    def get_shop_sales(self, params: dict) -> dict:
        shop = str(params.get("shop_name") or "").strip()
        if not shop:
            raise ValueError("缺少参数 shop_name")
        mode = params.get("mode") or "hourly"
        if mode == "daily":
            days = max(1, min(int(params.get("days") or 7), 90))
            series = self.ana.shop_daily_series(shop, days)
            basis = "每日销量 = 店内监控商品每日销量之和"
        else:
            day = params.get("date") or C.now().strftime(C.DAY_FMT)
            series = self.ana.shop_hourly_series(shop, day)
            basis = "逐时销量 = 店内监控商品逐时销量之和；未采到数据的小时返回 null"
        detail = self.ana.shop_detail(shop)
        return self._envelope({
            "shop_name": shop,
            "product_count": detail["product_count"],
            "today_sales": detail["today"],
            "last_hour_sales": detail["last_hour"],
            "total_sold": detail["total_sold"],
            "mode": mode,
            "series": series,
            "fuzzy_note": A.FUZZY_NOTE,
        }, basis)

    def get_product_ranking(self, params: dict) -> dict:
        kind = params.get("kind") or "today"
        limit = max(1, min(int(params.get("limit") or 20), 200))
        data = self.ana.product_ranking(kind, limit)
        data["basis"] = "{}排名；{}".format(data["basis"],
                                            "销量为累计已售快照差值；销售额为模糊试算")
        return self._envelope(data, data["basis"])

    def get_shop_ranking(self, params: dict) -> dict:
        kind = params.get("kind") or "today"
        limit = max(1, min(int(params.get("limit") or 20), 200))
        data = self.ana.shop_ranking(kind, limit)
        return self._envelope(data, "{}排名（店铺合计）".format(data["basis"]))

    def search_products(self, params: dict) -> dict:
        kw = str(params.get("keyword") or "").strip()
        if not kw:
            raise ValueError("缺少参数 keyword")
        limit = max(1, min(int(params.get("limit") or 50), 500))
        rows = self.ana.search_products(keyword=kw, limit=limit)
        return self._envelope([self._product_brief(r) for r in rows],
                              "在监控商品的标题/店铺/商品ID 中模糊匹配")

    def get_overview(self, params: dict) -> dict:
        return self._envelope(self.ana.overview(), "总览口径见 basis 字段")


TOOL_MAP: dict[str, str] = {t["name"]: t["name"] for t in TOOLS}
TOOL_METHODS = {
    "list_shops": "list_shops",
    "list_products": "list_products",
    "get_product_sales": "get_product_sales",
    "get_shop_sales": "get_shop_sales",
    "get_product_ranking": "get_product_ranking",
    "get_shop_ranking": "get_shop_ranking",
    "search_products": "search_products",
    "get_overview": "get_overview",
}

# 明确声明不支持写操作——即使客户端尝试也不存在对应工具
FORBIDDEN_HINTS = ("insert", "update", "delete", "drop", "alter", "write", "remove", "create")


# --------------------------------------------------------------------------
# JSON-RPC / stdio 传输
# --------------------------------------------------------------------------

class McpServer:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or C.DB_PATH
        self.conn: sqlite3.Connection | None = None
        self.tools: Tools | None = None

    def _connect(self) -> None:
        if self.conn is None:
            if not os.path.exists(self.db_path):
                raise RuntimeError("数据库不存在：{}".format(self.db_path))
            self.conn = D.Database.open_readonly(self.db_path)
            self.tools = Tools(self.conn)

    # ---- 消息处理 ----

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        is_notification = mid is None

        if method == "initialize":
            client_ver = str((params or {}).get("protocolVersion") or DEFAULT_PROTOCOL)
            ver = client_ver if client_ver in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            return self._ok(mid, {
                "protocolVersion": ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "红薯雷达只读数据服务。仅提供读取小红书店铺/商品销量与排名的工具，"
                    "不允许任何修改数据库的操作。所有销量均为累计已售快照差值，"
                    "时间口径为 Asia/Shanghai。"
                ),
            })

        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            return self._ok(mid, {})

        if method == "tools/list":
            return self._ok(mid, {"tools": TOOLS})

        if method == "resources/list":
            return self._ok(mid, {"resources": []})

        if method == "prompts/list":
            return self._ok(mid, {"prompts": []})

        if method == "tools/call":
            return self._handle_call(mid, params)

        if is_notification:
            return None
        return self._err(mid, -32601, "Method not found: {}".format(method))

    def _handle_call(self, mid: Any, params: dict) -> dict:
        name = str((params or {}).get("name") or "")
        args = (params or {}).get("arguments") or {}
        if not isinstance(args, dict):
            args = {}

        low = name.lower()
        if any(h in low for h in FORBIDDEN_HINTS):
            return self._ok(mid, {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": "本服务为只读 MCP，不提供任何写操作工具。"},
                    ensure_ascii=False, indent=2)}],
                "isError": True,
            })

        method_name = TOOL_METHODS.get(name)
        if not method_name:
            known = ", ".join(TOOL_METHODS)
            return self._ok(mid, {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": "未知工具：{}。可用工具：{}".format(name, known)},
                    ensure_ascii=False, indent=2)}],
                "isError": True,
            })

        try:
            self._connect()
            func: Callable[[dict], dict] = getattr(self.tools, method_name)
            result = func(args)
            text = json.dumps(result, ensure_ascii=False, indent=2, default=_json_default)
            return self._ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except ValueError as exc:
            return self._ok(mid, {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": str(exc)}, ensure_ascii=False)}],
                "isError": True,
            })
        except Exception as exc:  # noqa: BLE001
            _stderr("tools/call {} failed: {}".format(name, traceback.format_exc()))
            return self._ok(mid, {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": "{}: {}".format(type(exc).__name__, str(exc)[:300])},
                    ensure_ascii=False)}],
                "isError": True,
            })

    # ---- 响应封装 ----

    @staticmethod
    def _ok(mid: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid: Any, code: int, message: str, data: Any = None) -> dict:
        err: dict = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": mid, "error": err}

    # ---- 主循环 ----

    def serve(self) -> None:
        _stderr("{} MCP server started, db={}".format(SERVER_NAME, self.db_path))
        stream = sys.stdin
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, TypeError):
                self._write(self._err(None, -32700, "Parse error"))
                continue
            if isinstance(msg, list):
                responses = []
                for m in msg:
                    r = self.handle(m) if isinstance(m, dict) else self._err(
                        None, -32600, "Invalid Request")
                    if r is not None:
                        responses.append(r)
                if responses:
                    self._write(responses)
                continue
            if not isinstance(msg, dict):
                self._write(self._err(None, -32600, "Invalid Request"))
                continue
            try:
                resp = self.handle(msg)
            except Exception as exc:  # noqa: BLE001
                _stderr(traceback.format_exc())
                resp = self._err(msg.get("id"), -32603, "Internal error: {}".format(str(exc)[:200]))
            if resp is not None:
                self._write(resp)

    @staticmethod
    def _write(obj: Any) -> None:
        try:
            out = json.dumps(obj, ensure_ascii=False)
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.strftime(C.TIME_FMT)
    return str(obj)


def _stderr(text: str) -> None:
    try:
        sys.stderr.write(text.rstrip() + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    db_path = C.DB_PATH
    for i, a in enumerate(argv):
        if a in ("--db", "-d") and i + 1 < len(argv):
            db_path = argv[i + 1]
        elif a.startswith("--db="):
            db_path = a.split("=", 1)[1]
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    server = McpServer(db_path)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
