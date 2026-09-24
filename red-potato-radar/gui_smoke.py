# -*- coding: utf-8 -*-
"""红薯雷达 —— 界面与采集轮次冒烟测试。

在临时目录里建一个独立数据库，注入示例数据后：
  1. 真实创建窗口，逐个切换全部 Tab；
  2. 打开全部弹窗（含带数据的折线图）；
  3. 用打桩的采集结果跑一整轮采集，校验成功/下架/失败三条分支的落库结果。

运行：.venv\\Scripts\\python.exe gui_smoke.py
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="radar_gui_smoke_")

import config as C  # noqa: E402

C.APP_DIR = TMP
C.DB_PATH = os.path.join(TMP, "monitor.db")
C.DATA_DIR = os.path.join(TMP, "data")
C.BROWSER_PROFILE_DIR = os.path.join(C.DATA_DIR, "browser_profile")
C.TEMP_PROFILE_DIR = os.path.join(C.DATA_DIR, "temp_profiles")
C.EXPORT_DIR = os.path.join(C.DATA_DIR, "exports")
C.ensure_dirs()

import collector as CO            # noqa: E402
from desktop import (Ctx, RadarApp, ProductDetailDialog, SingleAddDialog,   # noqa: E402
                     DedupDialog, ShopPickerDialog, LogDetailDialog, ExpandDialog)

PASS = 0
FAIL = 0


def check(cond: bool, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   {}".format(label))
    else:
        FAIL += 1
        print("  [FAIL] {}{}".format(label, ("  -> " + extra) if extra else ""))


def seed(db) -> tuple[str, str, str]:
    now = C.now()
    d0 = C.day_start(now)
    a = "65f1a2b3c4d5e6f708192a3b"
    b = "aaaabbbbccccddddeeeeffff"
    c = "111122223333444455556666"

    db.add_product(a, "夏日碎花连衣裙 显瘦款", "小王女装店", "shopA", "")
    db.add_product(b, "限量联名帆布包", "小王女装店", "shopA", "")
    db.add_product(c, "手冲咖啡豆 中深烘", "老李食品铺", "shopB", "")

    def snap(pid, dt, sold, price=99.0, shop_sold=500):
        db.add_snapshot(pid, C.fmt(dt), sold, shop_sold, price, 1200, 1, 1, "api")

    d1 = C.add_days(d0, -1)
    h0 = C.hour_start(now)
    # 三个商品的当日增量刻意不同（20 / 10 / 5），便于验证默认排序。
    # 快照落在「当前整点」，因此不依赖运行测试时的具体时刻。
    plan = ((a, 1000, 20), (b, 300, 10), (c, 80, 5))
    for pid, base, step in plan:
        snap(pid, d1, base)
        snap(pid, d0, base + 2)
        if h0 > d0:
            snap(pid, h0, base + 2 + step)

    db.mark_success(a, "api")
    db.mark_success(b, "silent")
    db.log("SUCCESS", "采集", "示例数据已注入")
    return a, b, c


def main() -> int:
    ctx = Ctx()
    db = ctx.db
    a, b, c = seed(db)

    app = RadarApp(ctx)
    app.update()

    print("一、界面与 Tab")
    for n in ["竞品看板", "添加商品", "失败列表", "下架列表", "店铺分析", "设置", "运行日志"]:
        app.nb.select(app.tabs[n])
        app.update()
        app.update_idletasks()
    check(True, "七个 Tab 全部可切换：{}".format(" / ".join(app.tabs)))

    board = app.tabs["竞品看板"]
    check(len(board.tv.get_children("")) == 3, "竞品看板显示 3 个商品",
          str(len(board.tv.get_children(""))))

    # 排序：默认今日销量降序
    vals = [board.tv.item(i, "values")[3] for i in board.tv.get_children("")]
    check(vals == ["20", "10", "5"], "默认按今日销量降序", str(vals))

    # 搜索过滤
    board._search.set("咖啡")
    app.update()
    check(len(board.tv.get_children("")) == 1, "搜索：按标题过滤生效")
    board._search.set("")
    app.update()
    check(len(board.tv.get_children("")) == 3, "搜索：清空后恢复")

    # 表头排序切换
    board.tv.heading("today", command=None)
    board._sorter()

    print("\n二、弹窗")
    rows = app.an.product_rows(None, C.now())
    d = ProductDetailDialog(app, rows[0])
    app.update()
    check(d.chart.winfo_width() > 0, "单品弹窗：折线图已渲染")
    d._fill_detail([("10:00", 10, 99.0, 990.0)])
    check(len(d.tv.get_children("")) == 1, "单品弹窗：销量明细可填充")
    d.load_daily(7)
    app.update()
    check(True, "单品弹窗：近 7 天视图可加载")
    d.destroy()

    info = CO.ProductInfo(a, title="测试商品", shop_name="测试小店", sold=123,
                          price=99.0, shop_sold=999, fans=1000, deliverable=1,
                          stock_status=1)
    d = SingleAddDialog(app, info)
    app.update()
    d.destroy()
    check(True, "确认添加弹窗可用")

    d = DedupDialog(app, [{"keep": {"id": "a" * 24, "title": "保留款", "price": 129.0,
                                    "shop_name": "某某店", "total_sold": 50,
                                    "last_hour": 3, "id_x": 1},
                           "drop": {"id": "b" * 24, "title": "重复款", "price": 99.0,
                                    "shop_name": "某某店", "total_sold": 50,
                                    "last_hour": 3}}], on_done=lambda: None)
    app.update()
    check(len(d.tv.get_children("")) == 1, "智能去重弹窗：预览行渲染")
    d.destroy()

    d = ShopPickerDialog(app, ["小王女装店", "老李食品铺"], ["小王女装店"],
                         on_save=lambda s: None)
    app.update()
    d.destroy()
    check(True, "通知店铺选择弹窗可用")

    d = ExpandDialog(app, [{"id": "f" * 24, "title": "拓品候选", "shop_name": "某店",
                            "sold": 10, "price": 20.0}], on_done=lambda: None)
    app.update()
    d.destroy()
    check(True, "自动拓品弹窗可用")

    d = LogDetailDialog(app, {"ts": C.now_str(), "level": "ERROR", "source": "采集",
                              "title": "t", "product_id": a, "message": "连接超时",
                              "detail": "traceback ..."})
    app.update()
    d.destroy()
    check(True, "日志详情弹窗可用")

    print("\n三、店铺分析")
    shop = app.tabs["店铺分析"]
    shop.reload_shops()
    app.update()
    check(shop.cmb_shop.get() != "", "店铺选择器已填充", shop.cmb_shop.get())
    check(shop.chart.winfo_width() > 0, "店铺分析：折线图已渲染")
    check(len(shop.tv_prod.get_children("")) >= 1, "店铺分析：商品表已填充")
    shop._quick(7)
    app.update()
    check(len(shop.tv_detail.get_children("")) == 7, "店铺分析：近7天明细 7 行",
          str(len(shop.tv_detail.get_children(""))))

    print("\n四、模拟采集轮次（接口成功 / 明确下架 / 临时失败）")
    calls: list[str] = []

    def fake_collect_one(self, pid):
        calls.append(pid)
        if pid == b:
            return CO.Outcome(False, "delisted", error="商品已下架（item not found）",
                              delist_reason="商品已下架")
        if pid == c:
            return CO.Outcome(False, "temp", error="连接超时")
        pi = CO.ProductInfo(pid, title="夏日碎花连衣裙 显瘦款", shop_name="小王女装店",
                            shop_id="shopA", sold=1023, shop_sold=900, price=88.0,
                            fans=2000, stock_status=1, deliverable=1)
        return CO.Outcome(True, "ok", pi, method="api")

    CO.Collector.collect_one = fake_collect_one
    CO.Collector.open = lambda self: None
    CO.Collector.close = lambda self: None

    snap_b_before = len(db.all_snapshots(b))
    res = app.run_round(C.now(), manual=True, retry=False)
    app.update()
    check(res.get("ok") == 1, "本轮成功 1 个", str(res.get("ok")))
    check(res.get("delisted") == 1, "本轮识别下架 1 个", str(res.get("delisted")))
    check(res.get("failed") == 1, "本轮临时失败 1 个", str(res.get("failed")))
    check(set(calls) == {a, b, c}, "三个商品都被采集过一次", str(calls))

    latest = db.latest_snapshot(a)
    check(latest and latest["sold"] == 1023, "成功商品已写入新快照",
          str(latest and latest["sold"]))
    check(db.get_product(a)["fail_reason"] in ("", None), "成功商品失败状态已清除")

    prod_b = db.get_product(b)
    check(prod_b["active"] == 0 and prod_b["delisted_at"], "下架商品 active=0 且记录时间")
    check(prod_b["delisted_reason"] == "商品已下架", "下架原因已记录")
    check(len(db.all_snapshots(b)) == snap_b_before,
          "下架商品历史快照全部保留",
          "{} / {}".format(len(db.all_snapshots(b)), snap_b_before))
    check(any(p["id"] == b for p in db.list_delisted_products()), "下架商品出现在下架列表")
    check(not any(p["id"] == b for p in db.list_failed_products()),
          "下架商品不出现在失败列表")

    prod_c = db.get_product(c)
    check(prod_c["active"] == 1 and prod_c["fail_reason"] == "连接超时",
          "临时失败商品仍在监控且记录原因", str(prod_c["fail_reason"]))
    check(any(p["id"] == c for p in db.list_failed_products()), "临时失败商品出现在失败列表")

    app.tabs["失败列表"].reload()
    app.tabs["下架列表"].reload()
    app.update()
    check(len(app.tabs["失败列表"].tv.get_children("")) == 1, "失败列表界面显示 1 行")
    check(len(app.tabs["下架列表"].tv.get_children("")) == 1, "下架列表界面显示 1 行")

    print("\n五、调度与收缩")
    from scheduler import next_boundary
    nb = next_boundary(60, C.now())
    check(nb.minute == 0 and nb.second == 0 and nb > C.now(),
          "下一次采集落在整点边界", nb.strftime("%m-%d %H:%M"))
    app.tabs["竞品看板"].toggle_monitor()
    app.update()
    check(app.scheduler.running, "开始监控后调度器处于运行态")
    check(app.tabs["竞品看板"].btn_monitor.cget("text") == "停止监控", "按钮文案已跟随切换")
    app.tabs["竞品看板"].toggle_monitor()
    app.update()
    check(not app.scheduler.running, "停止监控生效")

    app.scheduler.shutdown()
    db.close()
    app.destroy()

    print("\n" + "=" * 60)
    print("冒烟测试：通过 {} 项，失败 {} 项".format(PASS, FAIL))
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(rc)
