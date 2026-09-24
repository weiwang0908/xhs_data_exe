# -*- coding: utf-8 -*-
"""红薯雷达 —— 自检脚本。

直接运行即可验证核心逻辑（不联网、不依赖浏览器）：
    python selftest.py

覆盖：数据库建表与迁移、输入解析、销量统计口径（跨日/0点/高水位回退/
缺失基线）、调度自然边界、MCP JSON-RPC 协议、只读 MCP 的写保护。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sys
import tempfile
import traceback

import config as C

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   {}".format(label))
    else:
        FAIL += 1
        FAILURES.append(label)
        print("  [FAIL] {}{}".format(label, ("  -> " + extra) if extra else ""))


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="radar_selftest_")
    db_path = os.path.join(tmp, "monitor.db")
    # 把数据目录也重定向到临时目录，保证自检不在项目里留下任何残留
    C.DATA_DIR = os.path.join(tmp, "data")
    C.BROWSER_PROFILE_DIR = os.path.join(C.DATA_DIR, "browser_profile")
    C.TEMP_PROFILE_DIR = os.path.join(C.DATA_DIR, "temp_profiles")
    C.EXPORT_DIR = os.path.join(C.DATA_DIR, "exports")
    C.DB_PATH = db_path
    try:
        _test_parse()
        _test_db_and_analytics(db_path)
        _test_scheduler()
        _test_mcp(db_path)
        _test_wecom()
        _test_collector_parse()
    except Exception:  # noqa: BLE001
        print("\n[自检异常]\n" + traceback.format_exc())
        FAILURES.append("自检过程抛出异常")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print("自检结果：通过 {} 项，失败 {} 项".format(PASS, FAIL))
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print("  · " + f)
    print("=" * 70)
    return 0 if FAIL == 0 else 1


# --------------------------------------------------------------------------

def _test_parse() -> None:
    section("一、商品输入解析（口令 / 短链 / 链接 / 纯 ID）")
    import parser as P

    p = P.parse_line("39 【好物推荐】夏日连衣裙 🌸 http://xhslink.com/a/AbCdEf12 提取码: 4N2K")
    check(p.short_links and not p.ids, "分享口令：提取出短链", str(p.short_links))
    check(len(p.ids) == 0, "分享口令：未误判出商品 ID")

    p = P.parse_line("https://www.xiaohongshu.com/goods-detail/"
                     "65f1a2b3c4d5e6f708192a3b?xsec_token=ABC&xsec_source=pc")
    check(p.ids == ["65f1a2b3c4d5e6f708192a3b"], "完整商品链接：提取 24 位 ID", str(p.ids))

    p = P.parse_line("65F1A2B3C4D5E6F708192A3B")
    check(p.ids == ["65f1a2b3c4d5e6f708192a3b"], "纯 ID：统一转小写", str(p.ids))

    p = P.parse_line("随便写点什么，没有链接")
    check(p.invalid, "无法识别的内容标记为无效输入")

    batch = P.parse_batch(
        "65f1a2b3c4d5e6f708192a3b\n"
        "https://www.xiaohongshu.com/goods-detail/aaaabbbbccccddddeeeeffff\n"
        "https://xhslink.com/xYz123\n"
        "65f1a2b3c4d5e6f708192a3b\n"
        "乱码行")
    check(len(batch["ids"]) == 2, "批量：按 ID 去重后 2 个", str(batch["ids"]))
    check(len(batch["short_links"]) == 1, "批量：识别 1 条短链")
    check(len(batch["invalid_lines"]) == 1, "批量：识别 1 行无效输入")

    check(P.normalize_title("【爆款】 连衣裙 100% 棉！") ==
          P.normalize_title("爆款连衣裙100棉"), "标题标准化：去表情/标点/空白后一致")
    check(P.is_valid_id("65f1a2b3c4d5e6f708192a3b"), "ID 校验：合法")
    check(not P.is_valid_id("65f1a2b3c4d5e6f708192a3"), "ID 校验：长度不足不合法")


# --------------------------------------------------------------------------

def _test_db_and_analytics(db_path: str) -> None:
    section("二、数据库建表 / 迁移 / 销量统计口径")
    import database as D
    import analytics as A

    db = D.Database(db_path)
    db.init()
    check(os.path.exists(db_path), "数据库文件已创建")

    conn = db.connect()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ("products", "snapshots", "settings", "logs", "deleted_products"):
        check(t in tables, "表已建立：{}".format(t))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check(str(mode).lower() == "wal", "已开启 WAL 模式", str(mode))

    # 自动迁移：模拟旧库缺列
    conn.execute("ALTER TABLE products RENAME TO products_old")
    conn.execute("CREATE TABLE products (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
    conn.execute("INSERT INTO products(id,title) VALUES('old','旧商品')")
    conn.commit()
    db._migrate(conn)
    cols = db._table_columns(conn, "products")
    check("delisted_reason" in cols and "shop_id" in cols and "created_at" in cols,
          "自动迁移：补齐缺失字段")
    row = conn.execute("SELECT * FROM products WHERE id='old'").fetchone()
    check(row is not None and row["title"] == "旧商品", "自动迁移：旧数据保留")
    conn.execute("DROP TABLE products")
    conn.execute("ALTER TABLE products_old RENAME TO products")
    conn.commit()

    # ---- 构造快照数据 ----
    pid = "65f1a2b3c4d5e6f708192a3b"
    db.add_product(pid, "测试连衣裙", "测试小店", "shop0001", "")

    now = C.now()
    d0 = C.day_start(now)
    d1 = C.add_days(d0, -1)
    d2 = C.add_days(d0, -2)

    def snap(dt: _dt.datetime, sold: int, price: float = 99.0, shop_sold: int = 500):
        db.add_snapshot(pid, C.fmt(dt), sold, shop_sold, price, 1200, 1, 1, "api")

    snap(d2, 100)
    snap(d1, 110)                      # day-2 销量 = 10
    snap(C.add_hours(d1, 12), 130)
    snap(C.add_hours(d1, 23), 140)
    snap(d0, 150)                      # day-1 销量 = 150-110 = 40
    snap(C.add_hours(d0, 8), 160)
    snap(C.add_hours(d0, 9), 165)
    snap(C.add_hours(d0, 10), 175)

    # 高水位回退：插入一个更小的累计值，不应影响任何统计
    snap(C.add_hours(d0, 11), 100)

    an = A.Analytics(db.connect())
    ref = C.add_hours(d0, 11) + _dt.timedelta(minutes=45)      # 11:45
    ref_hour = C.add_hours(d0, 10) + _dt.timedelta(minutes=30)  # 10:30
    ref_shop = C.add_hours(d0, 14) + _dt.timedelta(minutes=30)  # 14:30

    today, partial = an.today_sales(pid, ref)
    check(today == 25, "今日销量 = 175-150 = 25（高水位不被回退值污染）", str(today))
    check(partial is False, "今日口径完整（存在 00:00 基线）")

    yest = an.yesterday_sales(pid, ref)
    check(yest == 40, "昨日销量 = 150-110 = 40", str(yest))

    # 「上小时销量」= 当前整点高水位 − 上一个完整整点小时的基线。
    # ref=10:30 时当前整点为 10:00，基线 09:00 → 175-165 = 10。
    lh = an.last_hour_sales(pid, ref_hour)
    check(lh == 10, "上小时销量 = hwm(10:00)-hwm(09:00) = 175-165 = 10", str(lh))
    # ref=11:45 时当前整点为 11:00，基线 10:00 → 175-175 = 0（该小时确实没增量）
    check(an.last_hour_sales(pid, ref) == 0,
          "上小时销量：整点基线相同时确认增量为 0")

    ds = an.day_sales(pid, d1)
    check(ds == 40, "某日销量（昨天）= 40", str(ds))

    hs = an.hourly_series(pid, d0)
    check(hs[0]["sales"] == 0,
          "当日逐时：0 点快照即当日基线，该小时增量为 0（不重复计入两天）",
          str(hs[0]["sales"]))
    check(hs[8]["sales"] == 10, "当日逐时：08:00 增量 = 160-150 = 10",
          str(hs[8]["sales"]))
    check(hs[9]["sales"] == 5, "当日逐时：09:00 增量 = 5", str(hs[9]["sales"]))
    check(hs[10]["sales"] == 10, "当日逐时：10:00 增量 = 10", str(hs[10]["sales"]))
    check(hs[11]["sales"] == 0, "当日逐时：11:00 确认相等时增量 = 0（非高水位回退）",
          str(hs[11]["sales"]))
    check(hs[12]["sales"] is None, "当日逐时：12:00 无快照为「—」，不伪造 0")

    total = an.total_sold(pid)
    check(total == 175, "累计已售取历史高水位 175", str(total))

    # 金额
    check(abs(hs[10]["amount"] - 10 * 99.0) < 1e-6,
          "模糊销售额 = 销量增量 × 当时价格", str(hs[10]["amount"]))

    # ---- 中途开始监控：没有 00:00 基线 ----
    pid2 = "aaaabbbbccccddddeeeeffff"
    db.add_product(pid2, "中途开始监控的商品", "测试小店", "shop0001", "")
    db.add_snapshot(pid2, C.fmt(C.add_hours(d0, 12)), 50, 10, 20.0, 5, 1, 1, "api")
    db.add_snapshot(pid2, C.fmt(C.add_hours(d0, 14)), 58, 10, 20.0, 5, 1, 1, "api")
    an2 = A.Analytics(db.connect())
    t2, p2 = an2.today_sales(pid2, C.add_hours(d0, 14) + _dt.timedelta(minutes=30))
    check(t2 == 8 and p2 is True,
          "中途开始监控：以当天第一条真实快照为基线，并标记为不完整口径",
          "today={} partial={}".format(t2, p2))

    # ---- 0 点快照不重复计入两天 ----
    h1 = an.day_sales(pid, d1)
    h0 = an.hourly_series(pid, d0)
    check(h1 == 40 and h0[0]["sales"] == 0,
          "0 点快照只作为前一日结束值与新一天基线，不被重复计数")

    # ---- 看板行 ----
    rows = an.product_rows(None, ref_hour)
    row = next(r for r in rows if r["id"] == pid)
    check(row["status"] == "normal", "状态：成功采集后为「正常」", row["status"])
    check(row["today"] == 25 and row["last_hour"] == 10 and row["yesterday"] == 40,
          "看板行指标正确（今日/昨日/上小时）",
          "today={} yest={} lh={}".format(row["today"], row["yesterday"],
                                          row["last_hour"]))
    row2 = next(r for r in rows if r["id"] == pid2)
    check(row2["shop_name"] == "测试小店", "看板行店铺正确")

    # ---- 店铺聚合（此时两个商品都有当天数据）----
    shops = an.shop_rows(ref_shop)
    s = next(x for x in shops if x["shop_name"] == "测试小店")
    check(s["product_count"] == 2, "店铺聚合：监控商品数 = 2")
    check(s["today"] == 33, "店铺聚合：今日销量 = 25 + 8 = 33", str(s["today"]))

    # ---- 下架与失败 ----
    db.mark_failure(pid2, "连接超时")
    failed = db.list_failed_products()
    check(any(p["id"] == pid2 for p in failed), "失败列表：包含失败商品")
    db.mark_delisted(pid2, "违规下架")
    dlist = db.list_delisted_products()
    check(any(p["id"] == pid2 for p in dlist), "下架列表：包含已下架商品")
    check(db.get_product(pid2)["active"] == 0, "确认下架后 active=0")
    check(db.get_product(pid2)["delisted_reason"] == "违规下架", "下架原因区分违规")
    check(not any(p["id"] == pid2 for p in db.list_failed_products()),
          "下架后从失败列表移除")
    check(not any(p["id"] == pid2 for p in db.list_active_products()),
          "下架后排除出后续采集任务")
    snaps = db.all_snapshots(pid2)
    check(len(snaps) == 2, "下架后保留原有历史快照", str(len(snaps)))

    # ---- 删除 ----
    db.delete_product(pid2)
    check(db.get_product(pid2) is None, "删除商品：记录已移除")
    check(db.is_deleted(pid2), "删除商品：登记到 deleted_products 防止回填")
    check(len(db.all_snapshots(pid2)) == 0, "删除商品：历史快照一并清除")

    # ---- 清理保留基线 ----
    res = db.cleanup(snapshot_keep_days=1, log_keep_days=30)
    check("error" not in res, "一键清理：执行无错误", str(res.get("error", "")))
    left = db.all_snapshots(pid)
    check(len(left) >= 1, "一键清理：每个商品保留至少一条基线", str(len(left)))

    # ---- 日志脱敏 ----
    db.log("ERROR", "测试", "请求失败",
           detail="Cookie: sessionid=abcdef123456\nhttps://qyapi.weixin.qq.com/cgi-bin/"
                  "webhook/send?key=SECRETKEY12345678")
    logs = db.query_logs("ALL", limit=5)
    check(bool(logs), "日志已写入数据库")
    detail = logs[0].get("detail") or ""
    check("SECRETKEY12345678" not in detail and "abcdef123456" not in detail,
          "日志脱敏：Cookie / Webhook key 未明文落盘")

    db.close()


# --------------------------------------------------------------------------

def _test_scheduler() -> None:
    section("三、定时调度的自然时间边界")
    import scheduler as S

    base = C.now().replace(year=2026, month=8, day=17, hour=9, minute=35, second=0,
                           microsecond=0)
    nxt = S.next_boundary(60, base)
    check(nxt.hour == 10 and nxt.minute == 0,
          "09:35 开始监控（周期60）→ 首次 10:00", nxt.strftime("%H:%M"))

    nxt = S.next_boundary(60, base.replace(hour=10, minute=0))
    check(nxt.hour == 11 and nxt.minute == 0, "10:00 整点触发后下一次为 11:00",
          nxt.strftime("%H:%M"))

    nxt = S.next_boundary(30, base)
    check(nxt.hour == 10 and nxt.minute == 0, "周期30：09:35 → 10:00",
          nxt.strftime("%H:%M"))

    nxt = S.next_boundary(60, base.replace(hour=21, minute=5))
    check(nxt.hour == 22, "21:05 → 下一次 22:00（整点边界）", nxt.strftime("%H:%M"))

    nxt = S.next_boundary(60, base.replace(hour=23, minute=30))
    check(nxt.hour == 0 and nxt.day == 18, "23:30 → 次日 00:00",
          nxt.strftime("%m-%d %H:%M"))

    cur = S.current_boundary(60, base)
    check(cur.hour == 9 and cur.minute == 0, "当前归属整点 = 09:00",
          cur.strftime("%H:%M"))


# --------------------------------------------------------------------------

def _test_mcp(db_path: str) -> None:
    section("四、只读 MCP 服务（JSON-RPC over stdio）")
    import mcp_server as M

    srv = M.McpServer(db_path)

    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {}, "clientInfo": {"name": "t"}}})
    check(resp["result"]["serverInfo"]["name"] == "xhs-sales-monitor",
          "initialize：返回服务标识")
    check("tools" in resp["result"]["capabilities"], "initialize：声明 tools 能力")

    resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"] for t in resp["result"]["tools"]}
    expect = {"list_shops", "list_products", "get_product_sales", "get_shop_sales",
              "get_product_ranking", "get_shop_ranking", "search_products",
              "get_overview"}
    check(expect.issubset(tools), "tools/list：8 个只读工具齐备",
          str(sorted(tools)))
    check(not any(k in t.lower() for t in tools
                  for k in ("insert", "update", "delete", "drop", "create")),
          "tools/list：不含任何写操作工具")
    for t in resp["result"]["tools"]:
        check("inputSchema" in t and t["inputSchema"].get("type") == "object",
              "工具 {} 提供合法 inputSchema".format(t["name"]))

    resp = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "get_overview", "arguments": {}}})
    text = resp["result"]["content"][0]["text"]
    obj = json.loads(text)
    check(resp["result"]["isError"] is False, "tools/call get_overview 成功")
    data = obj["data"]
    check(all(k in data for k in ("product_count", "shop_count", "today_sales",
                                  "yesterday_sales", "last_hour_sales", "data_time")),
          "get_overview：字段齐全")
    check("basis" in obj and "data_time" in obj, "返回结果包含统计口径与数据时间")

    resp = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "get_product_ranking",
                                  "arguments": {"kind": "today", "limit": 5}}})
    check(resp["result"]["isError"] is False, "get_product_ranking 可调用")

    resp = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "delete_product",
                                  "arguments": {"id": "x"}}})
    check(resp["result"]["isError"] is True, "写操作类工具名被拒绝并报错")

    resp = srv.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "nonexistent_tool", "arguments": {}}})
    check(resp["result"]["isError"] is True, "未知工具返回错误而非崩溃")

    resp = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "no/such/method"})
    check("error" in resp and resp["error"]["code"] == -32601,
          "未知方法返回 -32601")

    check(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None,
          "通知类消息不返回响应")

    # 只读连接确实无法写入
    import database as D
    ro = D.Database.open_readonly(db_path)
    blocked = False
    try:
        ro.execute("INSERT INTO settings(key,value) VALUES('x','1')")
        ro.commit()
    except Exception:  # noqa: BLE001
        blocked = True
    check(blocked, "只读连接：物理上无法写入数据库")
    ro.close()
    srv.close()


# --------------------------------------------------------------------------

def _test_wecom() -> None:
    section("五、企业微信通知格式化")
    import wecom as W

    ok, _ = W.validate_webhook(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=12345678-abcd-efgh")
    check(ok, "Webhook 校验：合法地址通过")
    ok, _ = W.validate_webhook("https://example.com/hook")
    check(not ok, "Webhook 校验：非法地址拒绝")
    ok, _ = W.validate_webhook("")
    check(not ok, "Webhook 校验：空值拒绝")

    masked = W.mask_webhook(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=1234567890abcdef")
    check("1234567890abcdef" not in masked, "Webhook 展示：完整 key 被掩码", masked)

    day = _dt.datetime(2026, 8, 17, 12, 0, 0, tzinfo=C.TZ)
    rows = [
        {"shop_name": "小王杂货铺", "last_hour": 22, "today": 24},
        {"shop_name": "老李百货", "last_hour": 16, "today": 20},
        {"shop_name": "阿强优选", "last_hour": 9, "today": 9},
        {"shop_name": "张记小铺", "last_hour": 3, "today": 5},
    ]
    msgs = W.build_sales_report(rows, day)
    body = "\n".join(msgs)
    check("12点店铺销量时报" in body, "时报标题包含整点")
    check("08月17日 12:00 - 12:59" in body, "时报包含时间区间", body.split("\n")[1])
    check(body.index("小王杂货铺") < body.index("老李百货"),
          "时报按上小时销量降序排列")
    check("🥇" in body and "🥈" in body and "🥉" in body, "前三名使用奖牌符号")
    check("4." in body, "第四名起使用序号")
    check("上小时销量：22 单" in body and "今日总销量：24 单" in body,
          "店铺名与销量分行展示")

    msgs = W.build_sales_report(rows, day, allowed_shops=["老李百货"])
    body = "\n".join(msgs)
    check("老李百货" in body and "小王杂货铺" not in body,
          "只发送勾选店铺")

    long_rows = [{"shop_name": "很长的店铺名称" * 3, "last_hour": 100 - i,
                  "today": 200 - i} for i in range(40)]
    msgs = W.build_sales_report(long_rows, day)
    check(len(msgs) > 1, "长消息自动拆分为多条", "{} 条".format(len(msgs)))
    check(all(len(m) <= W.MAX_CHARS + 200 for m in msgs),
          "拆分后单条长度不超限")

    fails = W.build_failure_report(
        [{"id": "65f1a2b3c4d5e6f708192a3b", "title": "测试商品",
          "error": "连接超时"}], day)
    check(fails and "采集失败通知" in fails[0], "失败通知可生成")
    check("连接超时" in fails[0], "失败通知包含失败原因")

    check(W.build_failure_report([], day) == [], "无失败时不发送失败通知")


def _test_collector_parse() -> None:
    section("六、商品接口字段解析与下架识别")
    import collector as CO

    for text, expect in (("已售123", 123), ("1.2万", 12000), ("1.2w", 12000),
                         ("已售1.2万+", 12000), ("3.5千", 3500), ("已售0", 0),
                         (12345, 12345)):
        got = CO.parse_sold_text(text)
        check(got == expect, "累计已售文本解析 {!r} → {}".format(text, expect), str(got))
    check(CO.parse_sold_text("已售") is None, "无法解析的已售文本返回 None（不当作 0）")
    check(CO.parse_sold_text(None) is None, "空已售返回 None")

    def payload(**over):
        node = {
            "descriptionMain": {"name": "测试商品标题"},
            "sellerH5": {"name": "测试小店", "id": "shop123", "salesVolume": 8888,
                         "fansAmount": 5000},
            "carouselH5": {"images": [{"url": "//ci.xiaohongshu.com/a.jpg"}],
                           "stockStatus": 1},
            "priceH5": {"itemAnalysisDataText": "已售1.2万",
                        "dealPrice": {"price": "88.50"},
                        "highlightPrice": "128.00"},
            "bottomBarMainH5": {"dealPrice": {"price": "79.90"},
                                "deliveryInfo": {"ableToDelivery": True}},
            "profitBarPopupH5": {"follow": {"fansNum": 6666}},
        }
        base = {"success": True, "data": {"template_data": [node]}}
        base.update(over)
        return base

    info = CO.parse_detail_payload(payload(), "65f1a2b3c4d5e6f708192a3b")
    check(info.title == "测试商品标题", "标题取 descriptionMain.name")
    check(info.shop_name == "测试小店" and info.shop_id == "shop123", "店铺名与店铺 ID")
    check(info.cover == "https://ci.xiaohongshu.com/a.jpg", "主图 // 前缀补全为 https:")
    check(info.sold == 12000, "累计已售 1.2万 → 12000", str(info.sold))
    check(info.shop_sold == 8888, "店铺累计销量")
    check(abs(info.price - 88.50) < 1e-6,
          "实际价格优先取 priceH5.dealPrice.price（不取优惠前原价 128）",
          str(info.price))
    check(info.fans == 6666, "粉丝数优先取 profitBarPopupH5.follow.fansNum")
    check(info.deliverable == 1, "可配送标记")

    # 常见字段缺失时的回退链
    p = payload()
    node = p["data"]["template_data"][0]
    node["descriptionMain"] = {}
    node["descriptionH5"] = {"name": "回退标题"}
    node["priceH5"] = {"itemAnalysisDataText": "已售5",
                       "highlightPrice": "66.00"}
    info = CO.parse_detail_payload(p, "a" * 24)
    check(info.title == "回退标题", "标题回退 descriptionH5.name")
    check(abs(info.price - 79.90) < 1e-6,
          "价格回退 bottomBarMainH5.dealPrice.price（优先于 highlightPrice）",
          str(info.price))
    # 两级 dealPrice 都缺失时才回退到 highlightPrice
    node["bottomBarMainH5"] = {}
    info = CO.parse_detail_payload(p, "a" * 24)
    check(abs(info.price - 66.0) < 1e-6, "价格最后回退 highlightPrice", str(info.price))
    node["priceH5"] = {"itemAnalysisDataText": "已售5"}
    node["bottomBarMainH5"] = {"dealPrice": {"price": "55.5"}}
    info = CO.parse_detail_payload(p, "a" * 24)
    check(abs(info.price - 55.5) < 1e-6, "价格取 bottomBarMainH5.dealPrice.price",
          str(info.price))

    # 空已售字段：仅当 success=true 且 template_data 完整时才是真实的 0
    p = payload()
    p["data"]["template_data"][0]["priceH5"]["itemAnalysisDataText"] = ""
    info = CO.parse_detail_payload(p, "a" * 24)
    check(info.sold == 0, "接口完整成功时空已售字段解释为真实 0")

    # success 非 true → 必须失败，绝不能用 0 顶替
    p = payload(success=False, code=461, msg="请求过于频繁")
    try:
        CO.parse_detail_payload(p, "a" * 24)
        check(False, "success=false 时判定采集失败")
    except CO.TempError:
        check(True, "success=false 时判定采集失败（不保存假快照）")
    except Exception as exc:  # noqa: BLE001
        check(False, "success=false 时判定采集失败", type(exc).__name__)

    # template_data 缺失 → 失败
    try:
        CO.parse_detail_payload({"success": True, "data": {}}, "a" * 24)
        check(False, "template_data 缺失时判定采集失败")
    except CO.TempError:
        check(True, "template_data 缺失时判定采集失败")

    # 下架特征
    try:
        CO.parse_detail_payload({"success": True, "data": {"template_data": [
            {"descriptionMain": {"name": "x"},
             "bottomBarMainH5": {"type": "unBuyableGoShop"}}]}}, "a" * 24)
        check(False, "购买按钮 unBuyableGoShop 识别为下架")
    except CO.DelistedError as exc:
        check(exc.delist_reason == "商品已下架", "购买按钮 unBuyableGoShop 识别为下架")

    for marker in ("已下架，进店逛逛", "当前商品已下架", "商品不存在", "商品已失效"):
        try:
            CO.parse_detail_payload({"success": True, "data": {"template_data": [
                {"descriptionMain": {"name": marker}}]}}, "a" * 24)
            check(False, "下架文案识别：{}".format(marker))
        except CO.DelistedError:
            check(True, "下架文案识别：{}".format(marker))

    try:
        CO.parse_detail_payload({"success": True, "data": {"template_data": [
            {"descriptionMain": {"name": "当前商品违规，无法展示"}}]}}, "a" * 24)
        check(False, "违规文案识别为违规下架")
    except CO.ViolationError as exc:
        check(exc.delist_reason == "违规下架", "违规文案识别为违规下架（原因与普通下架区分）")

    check(CO.detect_status_from_text("网络超时，请稍后重试") is None,
          "普通错误文案不会被误判为下架")
    check(CO.detect_status_from_text("") is None, "空内容不会被误判为下架")

    # 接口明确返回「商品不存在」类业务错误 → 明确下架，而不是临时失败
    real = {"data": {"template": "", "scene": "", "common_data": ""},
            "force_update": None, "error_code": 602, "success": False,
            "msg": "item not found"}
    try:
        CO.parse_detail_payload(real, "a" * 24)
        check(False, "接口 error_code=602 item not found 判定为下架")
    except CO.DelistedError:
        check(True, "接口 error_code=602 item not found 判定为下架")

    # 服务端内部错误仍属临时失败
    try:
        CO.parse_detail_payload({"success": False, "code": 500, "msg": "server error"},
                                "a" * 24)
        check(False, "服务端 500 类响应仍归为临时失败")
    except CO.TempError:
        check(True, "服务端 500 类响应仍归为临时失败")

    # 异常分类
    check(isinstance(CO._classify_http(500), CO.TempError), "HTTP 500 归类为临时失败")
    check(isinstance(CO._classify_http(461), CO.RestrictedError), "HTTP 461 归类为风控")


if __name__ == "__main__":
    raise SystemExit(main())
