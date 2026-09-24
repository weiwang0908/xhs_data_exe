# -*- coding: utf-8 -*-
"""红薯雷达 —— Windows 桌面界面（Tkinter + ttk）。

七个横向 Tab：竞品看板 / 添加商品 / 失败列表 / 下架列表 / 店铺分析 / 设置 / 运行日志。
所有网络与数据库重任务都在后台线程执行，界面更新统一通过主线程事件队列。
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import queue
import random
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

import analytics as A
import collector as CO
import config as C
import database as D
import parser as P
import scheduler as SCH
import wecom as W

# --------------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------------

BG = "#F4F5F7"
CARD = "#FFFFFF"
BORDER = "#E2E5EA"
TEXT = "#1F2329"
SUB = "#6B7280"
ACCENT = "#12A150"      # 主强调色（绿）
ACCENT_DARK = "#0E7F3F"
DANGER = "#D9342B"      # 仅用于错误与风险
WARN = "#C77700"
HEADER_BG = "#FFFFFF"
ROW_ALT = "#FAFBFC"
SEL = "#DCF3E6"

FONT_FAMILY = "Microsoft YaHei UI"
FONT_FALLBACK = "Microsoft YaHei"

TABS = ("竞品看板", "添加商品", "失败列表", "下架列表", "店铺分析", "设置", "运行日志")


def _pick_font(root: tk.Misc, family: str) -> str:
    try:
        from tkinter import font as tkfont
        families = set(tkfont.families(root))
        for cand in (family, FONT_FALLBACK, "微软雅黑", "SimHei", "Segoe UI"):
            if cand in families:
                return cand
    except Exception:  # noqa: BLE001
        pass
    return "TkDefaultFont"


def fmt_num(v: Any) -> str:
    if v is None:
        return A.DASH
    try:
        return "{:,}".format(int(v))
    except (TypeError, ValueError):
        return str(v)


def fmt_price(v: Any) -> str:
    if v is None:
        return A.DASH
    try:
        return "¥{:.2f}".format(float(v))
    except (TypeError, ValueError):
        return A.DASH


def fmt_dt(v: Any, with_sec: bool = False) -> str:
    dt = C.to_dt(v)
    if dt is None:
        return A.DASH
    return dt.strftime("%m-%d %H:%M:%S" if with_sec else "%m-%d %H:%M")


def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    f = float(max(0, int(n)))
    for u in units:
        if f < 1024 or u == units[-1]:
            return "{:.0f}B".format(f) if u == "B" else "{:.1f}{}".format(f, u)
        f /= 1024
    return "{:.1f}GB".format(f)


def load_image_async(app, url: str, label: tk.Label, size: tuple[int, int]) -> None:
    """后台下载并缩放图片；失败时保留占位区域，不影响其它数据展示。"""
    def worker():
        try:
            req = urllib.request.Request(url, headers=dict(C.DEFAULT_HEADERS))
            with urllib.request.urlopen(req, timeout=C.IMAGE_TIMEOUT) as resp:
                data = resp.read()
            if not data:
                raise ValueError("空图片")

            def apply():
                try:
                    from PIL import Image, ImageTk
                    import io
                    img = Image.open(io.BytesIO(data))
                    img = img.convert("RGB")
                    img.thumbnail(size)
                    photo = ImageTk.PhotoImage(img)
                    label.configure(image=photo, text="", width=img.width,
                                    height=img.height)
                    label.image = photo  # 防止被 GC
                except Exception:  # noqa: BLE001
                    label.configure(text="图片无法显示", width=size[0] // 10,
                                    height=size[1] // 22)
            app.after(0, apply)
        except Exception:  # noqa: BLE001
            def fail():
                try:
                    label.configure(text="图片加载失败", width=size[0] // 10,
                                    height=size[1] // 22)
                except Exception:  # noqa: BLE001
                    pass
            app.after(0, fail)
    threading.Thread(target=worker, daemon=True).start()


def prompt_delete_product(app, pid: str, single: bool = True) -> bool:
    """删除商品（含历史数据）二次确认。"""
    prod = app.db.get_product(pid) or {"id": pid}
    name = (prod.get("title") or pid)[:50]
    if not messagebox.askyesno(
            C.APP_NAME,
            "⚠️ 此操作非常危险，可能导致不可逆的数据丢失！\n\n"
            "将删除商品：\n{}\n（ID：{}）\n\n"
            "同时删除该商品的全部历史销量快照，并在以后所有采集任务中不再采集。\n\n"
            "确定删除吗？".format(name, pid), icon="warning", default="no"):
        return False
    if single:
        if not messagebox.askyesno(
                C.APP_NAME,
                "请再次确认：删除后无法恢复。\n\n要删除「{}」吗？".format(name),
                icon="warning", default="no"):
            return False
    app.db.delete_product(pid)
    app.db.log("WARN", "商品", "用户删除商品：{}".format(name),
               product_id=pid, title=prod.get("title") or "")
    app.set_status("已删除商品 {}".format(name))
    return True


# --------------------------------------------------------------------------
# 折线图（纯 Canvas 绘制，不依赖第三方图表库）
# --------------------------------------------------------------------------

class LineChart(tk.Canvas):
    def __init__(self, master, **kw):
        super().__init__(master, background=CARD, highlightthickness=0, bd=0, **kw)
        self._points: list[tuple[str, Optional[float]]] = []
        self._title = ""
        self.bind("<Configure>", lambda e: self.redraw())

    def set_data(self, points: list[tuple[str, Optional[float]]], title: str = "") -> None:
        self._points = points or []
        self._title = title
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())
        pad_l, pad_r, pad_t, pad_b = 56, 20, 32, 36
        if self._title:
            self.create_text(pad_l - 44, 16, text=self._title, anchor="w",
                             fill=TEXT, font=(FONT_FAMILY, 10, "bold"))
        pts = list(self._points)
        if not pts:
            self.create_text(w / 2, h / 2, text="暂无数据", fill=SUB,
                             font=(FONT_FAMILY, 10))
            return
        values = [v for _l, v in pts if v is not None]
        if not values:
            self.create_text(w / 2, h / 2, text="暂无足够基线，无法计算销量",
                             fill=SUB, font=(FONT_FAMILY, 10))
            return

        vmax = max(1, max(values))
        plot_w = max(1, w - pad_l - pad_r)
        plot_h = max(1, h - pad_t - pad_b)

        for i in range(5):
            y = pad_t + plot_h * i / 4
            self.create_line(pad_l, y, w - pad_r, y, fill=BORDER)
            val = vmax * (4 - i) / 4
            lab = "{:.0f}".format(val) if val >= 10 else "{:.1f}".format(val)
            self.create_text(pad_l - 8, y, text=lab, anchor="e", fill=SUB,
                             font=(FONT_FAMILY, 8))

        n = len(pts)
        step = plot_w / max(1, n - 1) if n > 1 else 0
        coords: list[tuple[float, float, Optional[float]]] = []
        for i, (_lab, val) in enumerate(pts):
            x = pad_l + (step * i if n > 1 else plot_w / 2)
            y = pad_t + plot_h - (plot_h * (val or 0) / vmax) if val is not None else 0.0
            coords.append((x, y, val))

        seg: list[float] = []
        for x, y, val in coords:
            if val is None:
                if len(seg) >= 4:
                    self.create_line(*seg, fill=ACCENT, width=2)
                seg = []
            else:
                seg.extend([x, y])
        if len(seg) >= 4:
            self.create_line(*seg, fill=ACCENT, width=2)

        every = max(1, n // 12)
        for i, (x, y, val) in enumerate(coords):
            if val is not None:
                self.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=ACCENT,
                                 outline=CARD)
            if i % every == 0:
                self.create_text(x, h - pad_b + 15, text=pts[i][0], fill=SUB,
                                 font=(FONT_FAMILY, 8))


# --------------------------------------------------------------------------
# 共享上下文与树表辅助
# --------------------------------------------------------------------------

class Ctx:
    """应用共享服务容器。"""

    def __init__(self) -> None:
        C.ensure_dirs()
        self.db = D.Database()
        self.db.init()
        self.analytics = A.Analytics(self.db.connect())
        self.ui_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.round_lock = threading.Lock()
        self.collector: Optional[CO.Collector] = None
        self.main: Optional["RadarApp"] = None

    def post(self, kind: str, payload: Any = None) -> None:
        self.ui_queue.put((kind, payload))


def make_tree(parent: tk.Misc, columns: list[tuple[str, str, int]],
              height: int = 18, selectmode: str = "extended") -> tuple[ttk.Frame, ttk.Treeview]:
    wrap = ttk.Frame(parent)
    wrap.rowconfigure(0, weight=1)
    wrap.columnconfigure(0, weight=1)
    keys = [c[0] for c in columns]
    tv = ttk.Treeview(wrap, columns=keys, show="headings", height=height,
                      selectmode=selectmode)
    for key, label, width in columns:
        tv.heading(key, text=label)
        tv.column(key, width=width, minwidth=max(50, width // 2),
                  anchor="w" if key in ("title", "shop", "reason", "detail", "msg",
                                        "product") else "center",
                  stretch=False)
    vsb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
    hsb = ttk.Scrollbar(wrap, orient="horizontal", command=tv.xview)
    tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tv.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    tv.tag_configure("odd", background=ROW_ALT)
    tv.tag_configure("even", background=CARD)
    tv.tag_configure("error", foreground=DANGER)
    tv.tag_configure("warn", foreground=WARN)
    tv.tag_configure("delisted", foreground=SUB)
    return wrap, tv


def bind_sort(tv: ttk.Treeview, keys: list[str], default_key: str = "",
              default_desc: bool = True) -> Callable[..., None]:
    """表头点击排序：再次点击切换升降序。数字列按数值比较，「—」排在最后。"""
    state = {"key": default_key, "desc": default_desc}
    numeric_keys = {"price", "today", "yesterday", "last_hour", "total", "sold",
                    "hour", "sales", "amount", "today_sales"}

    def headtext(k: str) -> str:
        base = tv.heading(k, "text").replace(" ▲", "").replace(" ▼", "")
        if state["key"] == k:
            return base + (" ▼" if state["desc"] else " ▲")
        return base

    def refresh_heading() -> None:
        for k in keys:
            tv.heading(k, text=headtext(k))

    def sort_rows() -> None:
        k = state["key"]
        if not k:
            return
        items = list(tv.get_children(""))
        if not items:
            return
        idx = keys.index(k)
        numeric = k in numeric_keys

        def keyfunc(item):
            vals = tv.item(item, "values")
            raw = str(vals[idx]) if idx < len(vals) else ""
            if numeric:
                cleaned = raw.replace(",", "").replace("¥", "").strip()
                if cleaned in ("", A.DASH, "-", "--"):
                    return (1, 0.0)
                try:
                    return (0, float(cleaned))
                except ValueError:
                    return (1, 0.0)
            return (0, raw)

        try:
            items.sort(key=keyfunc, reverse=state["desc"])
        except Exception:  # noqa: BLE001
            return
        for i, item in enumerate(items):
            tv.move(item, "", i)
            tags = [t for t in tv.item(item, "tags") if t not in ("odd", "even")]
            tags.append("odd" if i % 2 else "even")
            tv.item(item, tags=tags)

    def on_click(k: str) -> None:
        if state["key"] == k:
            state["desc"] = not state["desc"]
        else:
            state["key"] = k
            state["desc"] = True
        refresh_heading()
        sort_rows()

    for k in keys:
        tv.heading(k, command=lambda kk=k: on_click(kk))
    refresh_heading()
    return lambda: sort_rows()


class BaseTab(ttk.Frame):
    def __init__(self, master, app: "RadarApp"):
        super().__init__(master)
        self.app = app
        self.db: D.Database = app.db
        self.an: A.Analytics = app.an

    def on_show(self) -> None:
        self.reload()

    def reload(self) -> None:
        pass


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

class RadarApp(tk.Tk):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        ctx.main = self
        self.db = ctx.db
        self.an = ctx.analytics
        self.title("{} v{} — 小红书公开商品销量监控".format(C.APP_NAME, C.APP_VERSION))
        self.geometry("1280x780")
        self.minsize(1050, 650)
        self.configure(bg=BG)

        self._round_running = False
        self._init_style()
        self._set_icon()
        self._build_header()
        self._build_tabs()
        self._build_statusbar()

        self.scheduler = SCH.CollectScheduler(
            on_run=self._scheduler_run,
            on_state=self._scheduler_state,
            interval_getter=lambda: self.db.get_int("collect_interval_minutes", 60),
        )
        self.scheduler.start()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._pump)
        self.after(1000, self._tick_header)
        self._refresh_usage()
        self.log("INFO", "界面", "程序启动完成，数据库：{}".format(self.db.path))

    # ---------------- 外观 ----------------

    def _init_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", font=(FONT_FAMILY, 10), background=BG, foreground=TEXT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=TEXT, font=(FONT_FAMILY, 10))
        st.configure("TButton", padding=(12, 6), font=(FONT_FAMILY, 10))
        st.configure("Accent.TButton", padding=(14, 6), foreground="#FFFFFF",
                     background=ACCENT, font=(FONT_FAMILY, 10, "bold"))
        st.map("Accent.TButton",
               background=[("active", ACCENT_DARK), ("disabled", "#9CCDB4")],
               foreground=[("disabled", "#EEEEEE")])
        st.configure("Danger.TButton", padding=(12, 6), foreground="#FFFFFF",
                     background=DANGER, font=(FONT_FAMILY, 10))
        st.map("Danger.TButton", background=[("active", "#B3271F")])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(20, 9), font=(FONT_FAMILY, 10))
        st.map("TNotebook.Tab",
               background=[("selected", CARD), ("!selected", "#E9EBEF")],
               foreground=[("selected", ACCENT_DARK), ("!selected", SUB)])
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                     rowheight=34, borderwidth=0, font=(FONT_FAMILY, 10))
        st.configure("Treeview.Heading", background="#F0F2F5", foreground=TEXT,
                     font=(FONT_FAMILY, 10, "bold"), relief="flat", padding=(6, 8))
        st.map("Treeview.Heading", background=[("active", "#E4E7EC")])
        st.map("Treeview", background=[("selected", SEL)], foreground=[("selected", TEXT)])
        st.configure("TEntry", padding=6, fieldbackground="#FFFFFF")
        st.configure("TCombobox", padding=4)
        st.configure("TCheckbutton", background=CARD, font=(FONT_FAMILY, 10))
        st.configure("TScrollbar", background="#E9EBEF", troughcolor=BG, borderwidth=0)

    def _set_icon(self) -> None:
        path = C.icon_path()
        if not path:
            return
        try:
            self.iconbitmap(path)
        except Exception:  # noqa: BLE001
            pass

    def _build_header(self) -> None:
        bar = tk.Frame(self, bg=HEADER_BG, height=66)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=HEADER_BG)
        left.pack(side="left", fill="both", expand=True, padx=(16, 0))
        tk.Label(left, text=C.APP_NAME, bg=HEADER_BG, fg=TEXT,
                 font=(FONT_FAMILY, 14, "bold")).pack(anchor="w", pady=(11, 0))
        tk.Label(left, text=C.DISCLAIMER, bg=HEADER_BG, fg=DANGER,
                 font=(FONT_FAMILY, 9)).pack(anchor="w")

        right = tk.Frame(bar, bg=HEADER_BG)
        right.pack(side="right", padx=(0, 16))
        top = tk.Frame(right, bg=HEADER_BG)
        top.pack(anchor="e", pady=(11, 0))
        self.lbl_usage = tk.Label(top, text="数据占用：—", bg=HEADER_BG, fg=SUB,
                                  font=(FONT_FAMILY, 9))
        self.lbl_usage.pack(side="left", padx=(0, 14))
        self.lbl_run = tk.Label(top, text="● 已停止", bg=HEADER_BG, fg=SUB,
                                font=(FONT_FAMILY, 9))
        self.lbl_run.pack(side="left", padx=(0, 14))
        self.lbl_next = tk.Label(top, text="下次采集：—", bg=HEADER_BG, fg=SUB,
                                 font=(FONT_FAMILY, 9))
        self.lbl_next.pack(side="left", padx=(0, 14))
        author = tk.Label(top, text="by：{}".format(C.AUTHOR), bg=HEADER_BG, fg=ACCENT,
                          font=(FONT_FAMILY, 9, "underline"), cursor="hand2")
        author.pack(side="left")
        author.bind("<Button-1>", lambda e: webbrowser.open(C.AUTHOR_URL))

        bottom = tk.Frame(right, bg=HEADER_BG)
        bottom.pack(anchor="e", pady=(2, 8))
        self.lbl_progress = tk.Label(bottom, text="", bg=HEADER_BG, fg=SUB,
                                     font=(FONT_FAMILY, 9))
        self.lbl_progress.pack(side="left", padx=(0, 12))
        for text, cmd in (("刷新", self.refresh_all), ("一键清理", self.do_cleanup)):
            tk.Button(bottom, text=text, command=cmd, bg="#F0F2F5", fg=TEXT,
                      relief="flat", font=(FONT_FAMILY, 9), cursor="hand2",
                      padx=10, pady=2, activebackground="#E4E7EC").pack(
                side="right", padx=(6, 0))
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", side="top")

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=BG, height=26)
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.status_var, bg=BG, fg=SUB,
                 font=(FONT_FAMILY, 9), anchor="w").pack(side="left", padx=16)

    def _build_tabs(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(8, 4))
        self.tabs: dict[str, BaseTab] = {}
        self.tabs["竞品看板"] = BoardTab(self.nb, self)
        self.tabs["添加商品"] = AddTab(self.nb, self)
        self.tabs["失败列表"] = FailTab(self.nb, self)
        self.tabs["下架列表"] = DelistedTab(self.nb, self)
        self.tabs["店铺分析"] = ShopTab(self.nb, self)
        self.tabs["设置"] = SettingsTab(self.nb, self)
        self.tabs["运行日志"] = LogTab(self.nb, self)
        for name in TABS:
            self.nb.add(self.tabs[name], text=name)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab)

    def _on_tab(self, _e=None) -> None:
        try:
            cur = self.nb.nametowidget(self.nb.select())
            if hasattr(cur, "on_show"):
                cur.on_show()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 日志 / 事件 ----------------

    def log(self, level: str, source: str, message: str, detail: str = "",
            product_id: str = "", title: str = "") -> None:
        self.db.log(level, source, message, detail, product_id, title)

    def set_status(self, text: str) -> None:
        try:
            self.status_var.set(text)
        except Exception:  # noqa: BLE001
            pass

    def _pump(self) -> None:
        try:
            for _ in range(300):
                kind, payload = self.ctx.ui_queue.get_nowait()
                self._dispatch(kind, payload)
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            pass
        self.after(120, self._pump)

    def _dispatch(self, kind: str, payload: Any) -> None:
        try:
            if kind == "log":
                self.log(**payload)
            elif kind == "status":
                self.set_status(payload)
            elif kind == "progress":
                self.lbl_progress.config(text=payload)
            elif kind == "refresh_board":
                self.tabs["竞品看板"].reload()
            elif kind == "refresh_fail":
                self.tabs["失败列表"].reload()
            elif kind == "refresh_delisted":
                self.tabs["下架列表"].reload()
            elif kind == "refresh_logs":
                self.tabs["运行日志"].reload()
            elif kind == "refresh_shops":
                self.tabs["店铺分析"].reload_shops()
            elif kind == "refresh_usage":
                self._refresh_usage()
            elif kind == "round_state":
                self._set_round_state(payload)
            elif kind == "add_summary":
                self.tabs["添加商品"].show_summary(payload)
            elif kind == "confirm_add":
                self.tabs["添加商品"].confirm_single(payload)
            elif kind == "expand_preview":
                self.tabs["竞品看板"].show_expand_preview(payload)
            elif kind == "message":
                messagebox.showinfo(C.APP_NAME, payload)
            elif kind == "error":
                messagebox.showerror(C.APP_NAME, payload)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_usage(self) -> None:
        try:
            self.lbl_usage.config(text="数据占用：{} · {:,}条".format(
                human_size(self.db.db_size_bytes()), self.db.snapshot_count()))
        except Exception:  # noqa: BLE001
            pass

    def _tick_header(self) -> None:
        try:
            nxt = self.scheduler.next_at
            self.lbl_next.config(text="下次采集：{}".format(
                nxt.strftime("%m-%d %H:%M") if nxt else "—"))
        except Exception:  # noqa: BLE001
            pass
        self.after(1000, self._tick_header)

    def _scheduler_state(self, **kw) -> None:
        self.ctx.post("round_state", {"running": bool(kw.get("running"))})

    def _set_round_state(self, payload: dict) -> None:
        self._round_running = bool(payload.get("running"))
        on = self.scheduler.running
        self.lbl_run.config(text="● 运行中" if on else "● 已停止",
                            fg=ACCENT if on else SUB)
        try:
            self.tabs["竞品看板"].sync_monitor_button()
        except Exception:  # noqa: BLE001
            pass

    def refresh_all(self) -> None:
        for name in ("竞品看板", "失败列表", "下架列表", "运行日志", "店铺分析"):
            try:
                tab = self.tabs[name]
                tab.reload_shops() if name == "店铺分析" else tab.reload()
            except Exception:  # noqa: BLE001
                pass
        self._refresh_usage()
        self.set_status("已刷新")

    # ---------------- 采集轮次 ----------------

    def _scheduler_run(self, captured_at: _dt.datetime, manual: bool,
                       cancel_event: threading.Event,
                       only_ids: Optional[set[str]]) -> None:
        self.run_round(captured_at, manual=manual, cancel_event=cancel_event,
                       only_ids=only_ids)

    def request_manual_round(self) -> None:
        if self._round_running:
            if not messagebox.askyesno(C.APP_NAME, "当前已有采集任务在运行。\n\n"
                                                   "是否中断并立即重新采集？"):
                return
        ev = self.scheduler.request_manual()
        threading.Thread(target=self.run_round, args=(C.now(),),
                         kwargs={"manual": True, "cancel_event": ev, "only_ids": None},
                         name="xhs-manual-round", daemon=True).start()

    def run_round(self, captured_at: _dt.datetime, manual: bool = False,
                  cancel_event: Optional[threading.Event] = None,
                  only_ids: Optional[set[str]] = None,
                  retry: bool = True) -> dict:
        """后台线程执行一轮采集（全局锁避免定时与手动重叠）。"""
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=C.TZ)
        captured_at = C.hour_start(captured_at)
        captured_str = C.fmt(captured_at)

        if not self.ctx.round_lock.acquire(timeout=1800):
            self.ctx.post("error", "上一轮采集长时间未结束，本次采集已跳过。")
            return {}
        try:
            if cancel_event is not None and cancel_event.is_set():
                return {}
            products = self.db.list_active_products()
            if only_ids:
                products = [p for p in products if p["id"] in only_ids]
            if not products:
                self.ctx.post("status", "没有在监控的商品，本轮跳过")
                self.ctx.post("log", {"level": "INFO", "source": "采集",
                                      "message": "没有可采集的商品，本轮跳过"})
                return {}

            self.ctx.post("round_state", {"running": True})
            self.ctx.post("status", "正在采集（{} 个商品）…".format(len(products)))

            col = CO.Collector(
                self.db,
                on_log=lambda lv, src, msg, **k: self.ctx.post("log", {
                    "level": lv, "source": src, "message": msg,
                    "detail": k.get("detail", ""), "product_id": k.get("pid", ""),
                    "title": k.get("title", "")}),
                on_event=self._on_collect_event,
            )
            self.ctx.collector = col
            try:
                col.open()
            except Exception as exc:  # noqa: BLE001
                self.ctx.post("log", {"level": "WARN", "source": "采集",
                                      "message": "静默浏览器不可用，本轮仅用接口直采：{}".format(
                                          str(exc)[:160])})
            try:
                result = col.run_round(products, captured_str, cancel_event=cancel_event,
                                       retry=retry)
            finally:
                try:
                    col.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ctx.collector = None

            if result.get("failed_items"):
                self.db.set_setting("pending_fail_items",
                                    json.dumps(result["failed_items"], ensure_ascii=False))
            else:
                self.db.set_setting("pending_fail_items", "[]")

            if result.get("restricted") and result.get("cooldown_until"):
                until = C.to_dt(result["cooldown_until"])
                if until:
                    self.scheduler.set_cooldown(
                        until, [p["id"] for p in result.get("pending", [])])

            try:
                W.send_report(self.db, self.an, captured_at)
            except Exception as exc:  # noqa: BLE001
                self.ctx.post("log", {"level": "ERROR", "source": "通知",
                                      "message": "发送通知异常：{}".format(str(exc)[:160])})

            self.db.set_setting("last_round_at", captured_str)
            self.db.set_setting("last_round_summary", json.dumps({
                "captured_at": captured_str, "ok": result.get("ok", 0),
                "failed": result.get("failed", 0), "delisted": result.get("delisted", 0),
                "over_limit": result.get("over_limit", 0),
                "cancelled": result.get("cancelled", False)}, ensure_ascii=False))

            summary = "本轮采集完成：成功 {ok}，失败 {failed}，下架 {delisted}，超限 {over}".format(
                ok=result.get("ok", 0), failed=result.get("failed", 0),
                delisted=result.get("delisted", 0), over=result.get("over_limit", 0))
            if result.get("cancelled"):
                summary += "（被新整点轮次打断）"
            if result.get("restricted"):
                summary += "；已触发 461 熔断，冷却后续采"
            self.ctx.post("status", summary)
            self.ctx.post("log", {"level": "SUCCESS" if not result.get("failed") else "WARN",
                                  "source": "采集", "message": summary})
            return result
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.ctx.post("log", {"level": "ERROR", "source": "采集",
                                  "message": "本轮采集发生未预期异常：{}".format(str(exc)[:160]),
                                  "detail": traceback.format_exc()})
            self.ctx.post("status", "本轮采集异常，详见运行日志")
            return {}
        finally:
            self.ctx.round_lock.release()
            self.ctx.post("round_state", {"running": False})
            for kind in ("refresh_board", "refresh_fail", "refresh_delisted",
                         "refresh_usage", "refresh_logs", "refresh_shops"):
                self.ctx.post(kind)

    def _on_collect_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "product_start":
            self.ctx.post("progress", "正在采集 {}/{}：{}".format(
                ev.get("index", 0) + 1, ev.get("total", 0),
                (ev.get("title") or "")[:28]))
        elif t == "retry_wait":
            self.ctx.post("progress", "等待 {:.0f}s 后统一重试 {} 个失败商品…".format(
                ev.get("seconds", 0), ev.get("count", 0)))
        elif t == "restricted":
            self.ctx.post("error", "连续请求受限（HTTP 461），已停止本轮采集并进入冷却，\n"
                                   "{} 分钟后自动续采未完成商品。".format(
                                       C.RESTRICT_COOLDOWN_MIN))
        elif t == "round_done":
            self.ctx.post("progress", "")

    # ---------------- 一键清理 ----------------

    def do_cleanup(self) -> None:
        keep_snap = self.db.get_int("snapshot_keep_days", 90)
        keep_log = self.db.get_int("log_keep_days", 30)
        if not messagebox.askyesno(
                C.APP_NAME,
                "将执行一键清理：\n\n"
                "· 保留最近 {} 天销量快照（每个商品额外保留 1 条更早基线，"
                "保证近期销量仍可计算）\n"
                "· 日志保留最近 {} 天\n"
                "· 执行 WAL checkpoint 与 VACUUM 回收空间\n\n"
                "当前数据占用 {}，快照 {} 条。\n\n确定继续吗？".format(
                    keep_snap, keep_log, human_size(self.db.db_size_bytes()),
                    self.db.snapshot_count())):
            return

        def worker():
            self.ctx.post("status", "正在清理…")
            try:
                before = self.db.snapshot_count()
                res = self.db.cleanup(keep_snap, keep_log)
                after = self.db.snapshot_count()
                msg = ("清理完成\n\n删除快照：{} 条\n删除日志：{} 条\n保留基线：{} 条\n"
                       "释放空间：{}\n\n当前占用：{}").format(
                    max(0, before - after), res.get("logs_deleted", 0),
                    res.get("baselines_kept", 0), human_size(res.get("freed", 0)),
                    human_size(res.get("size_after", 0)))
                self.ctx.post("log", {"level": "SUCCESS", "source": "维护",
                                      "message": "一键清理完成，释放 {}".format(
                                          human_size(res.get("freed", 0))), "detail": msg})
                self.ctx.post("message", msg)
            except Exception as exc:  # noqa: BLE001
                self.ctx.post("error", "清理失败：{}".format(str(exc)[:200]))
            finally:
                for kind in ("refresh_usage", "refresh_board", "refresh_logs"):
                    self.ctx.post(kind)
                self.ctx.post("status", "清理结束")

        threading.Thread(target=worker, name="xhs-cleanup", daemon=True).start()

    # ---------------- 关闭 ----------------

    def on_close(self) -> None:
        if not messagebox.askyesno(C.APP_NAME, "确定要退出 {} 吗？\n"
                                               "（正在进行的采集会被中断）".format(C.APP_NAME)):
            return
        try:
            self.set_status("正在关闭浏览器与后台任务…")
            self.update_idletasks()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.scheduler.shutdown()
        except Exception:  # noqa: BLE001
            pass
        col = self.ctx.collector
        if col is not None:
            try:
                col.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.db.log("INFO", "界面", "程序退出")
            self.db.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.destroy()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 竞品看板
# --------------------------------------------------------------------------

class BoardTab(BaseTab):
    COLS = [
        ("title", "商品名称", 280),
        ("shop", "店铺", 150),
        ("price", "当前价格", 90),
        ("today", "今日销量", 90),
        ("yesterday", "昨日销量", 90),
        ("last_hour", "上小时销量", 100),
        ("total", "累计已售", 100),
        ("last", "最近采集", 130),
        ("status", "状态", 80),
    ]

    def __init__(self, master, app):
        super().__init__(master, app)
        self._rows: list[dict] = []
        self._search = tk.StringVar()
        self._build()

    def _build(self) -> None:
        top = tk.Frame(self, bg=CARD)
        top.pack(fill="x", pady=(0, 6))
        tk.Label(top, text="竞品监控看板", bg=CARD, fg=TEXT,
                 font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=(12, 16), pady=10)

        self.ent_search = ttk.Entry(top, textvariable=self._search, width=32)
        self.ent_search.pack(side="left", ipady=2)
        tk.Label(top, text="搜索", bg=CARD, fg=SUB,
                 font=(FONT_FAMILY, 9)).pack(side="left", padx=(8, 0))
        self._search.trace_add("write", lambda *_: self.apply_filter())

        btn_box = tk.Frame(top, bg=CARD)
        btn_box.pack(side="right", padx=12)
        # 从右向左：添加商品、自动拓品、智能去重、立即采集、开始/停止监控
        self.btn_monitor = ttk.Button(btn_box, text="开始监控", style="Accent.TButton",
                                      command=self.toggle_monitor)
        self.btn_manual = ttk.Button(btn_box, text="立即采集", command=self.manual_collect)
        self.btn_dedup = ttk.Button(btn_box, text="智能去重", command=self.dedup)
        self.btn_expand = ttk.Button(btn_box, text="自动拓品", command=self.expand_shop)
        self.btn_add = ttk.Button(btn_box, text="添加商品", command=self.goto_add)
        for b in (self.btn_monitor, self.btn_manual, self.btn_dedup, self.btn_expand,
                  self.btn_add):
            b.pack(side="right", padx=(6, 0))
        self.btn_monitor.pack_configure(padx=(0, 0))

        self.lbl_hint = tk.Label(self, text="", bg=BG, fg=SUB, font=(FONT_FAMILY, 9),
                                 anchor="w")
        self.lbl_hint.pack(fill="x", padx=4, pady=(0, 4))

        wrap, self.tv = make_tree(self, self.COLS, height=20)
        wrap.pack(fill="both", expand=True)
        self._sorter = bind_sort(self.tv, [c[0] for c in self.COLS],
                                 default_key="today", default_desc=True)

        self.tv.bind("<Double-1>", self._open_detail)
        self.tv.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="查看详情", command=self._menu_detail)
        self.menu.add_command(label="立即采集选中商品", command=self._menu_collect)
        self.menu.add_separator()
        self.menu.add_command(label="删除商品", command=self._menu_delete)

    # ---- 数据 ----

    def reload(self) -> None:
        try:
            self._rows = self.an.product_rows(None, C.now())
        except Exception as exc:  # noqa: BLE001
            self.app.set_status("加载看板失败：{}".format(str(exc)[:120]))
            self._rows = []
        self.apply_filter()

    def apply_filter(self) -> None:
        kw = (self._search.get() or "").strip().lower()
        rows = self._rows
        if kw:
            rows = [r for r in rows
                    if kw in (r["title"] or "").lower()
                    or kw in (r["shop_name"] or "").lower()
                    or kw in (r["id"] or "").lower()]
        self._fill(rows)
        self.lbl_hint.config(text="共 {} 个在监控商品{}　|　今日销量、昨日销量、上小时销量、"
                                  "累计已售四列可点击表头排序".format(
                                      len(self._rows),
                                      "，筛选出 {} 个".format(len(rows)) if kw else ""))

    def _fill(self, rows: list[dict]) -> None:
        tv = self.tv
        for item in tv.get_children(""):
            tv.delete(item)
        for i, r in enumerate(rows):
            tag = "odd" if i % 2 else "even"
            if r["status"] == "error":
                tag = "error"
            elif r["status"] == "pending":
                tag = "warn"
            elif r["status"] == "delisted":
                tag = "delisted"
            tv.insert("", "end", iid=r["id"], tags=(tag,), values=(
                (r["title"] or r["id"])[:60],
                (r["shop_name"] or A.DASH)[:24],
                fmt_price(r["price"]),
                fmt_num(r["today"]),
                fmt_num(r["yesterday"]),
                fmt_num(r["last_hour"]),
                fmt_num(r["total_sold"]),
                fmt_dt(r["last_captured"]),
                r["status_text"],
            ))
        self._sorter()

    def selected_ids(self) -> list[str]:
        return list(self.tv.selection())

    # ---- 操作 ----

    def sync_monitor_button(self) -> None:
        self.btn_monitor.config(text="停止监控" if self.app.scheduler.running
                                else "开始监控")

    def toggle_monitor(self) -> None:
        if self.app.scheduler.running:
            self.app.scheduler.stop_monitor()
            self.db.log("INFO", "调度", "用户停止监控")
            self.app.set_status("监控已停止")
        else:
            if not self.db.list_active_products():
                messagebox.showinfo(C.APP_NAME, "还没有在监控的商品，请先在「添加商品」里添加。")
                return
            self.app.scheduler.start_monitor()
            interval = self.db.get_int("collect_interval_minutes", 60)
            nxt = SCH.next_boundary(interval, C.now())
            self.db.log("INFO", "调度", "用户开始监控，下一次正式采集 {}".format(
                nxt.strftime("%Y-%m-%d %H:%M")))
            self.app.set_status("已开始监控。当前不是整点，第一次正式采集为 {}".format(
                nxt.strftime("%H:%M")))
        self.sync_monitor_button()
        self.app.ctx.post("round_state", {"running": self.app._round_running})

    def manual_collect(self) -> None:
        if not self.db.list_active_products():
            messagebox.showinfo(C.APP_NAME, "没有在监控的商品。")
            return
        self.app.request_manual_round()

    def goto_add(self) -> None:
        self.app.nb.select(self.app.tabs["添加商品"])

    def _menu_detail(self) -> None:
        ids = self.selected_ids()
        if ids:
            self._open_detail_for(ids[0])

    def _menu_collect(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        if len(ids) > 1 and not messagebox.askyesno(
                C.APP_NAME, "将立即采集选中的 {} 个商品，是否继续？".format(len(ids))):
            return
        ev = self.app.scheduler.request_manual()
        threading.Thread(target=self.app.run_round, args=(C.now(),),
                         kwargs={"manual": True, "cancel_event": ev,
                                 "only_ids": set(ids)},
                         name="xhs-manual-selected", daemon=True).start()

    def _menu_delete(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        for pid in list(ids):
            prompt_delete_product(self.app, pid, single=(len(ids) == 1))
        self.reload()
        self.app.ctx.post("refresh_usage")

    def _open_detail(self, event) -> None:
        item = self.tv.identify_row(event.y)
        if item:
            self._open_detail_for(item)

    def _open_detail_for(self, pid: str) -> None:
        row = next((r for r in self._rows if r["id"] == pid), None)
        if row is None:
            try:
                row = next((r for r in self.an.product_rows(None, C.now())
                            if r["id"] == pid), None)
            except Exception:  # noqa: BLE001
                row = None
        if row:
            ProductDetailDialog(self.app, row)

    def _popup_menu(self, event) -> None:
        item = self.tv.identify_row(event.y)
        if item:
            if item not in self.tv.selection():
                self.tv.selection_set(item)
            self.menu.tk_popup(event.x_root, event.y_root)

    # ---- 智能去重 ----

    def dedup(self) -> None:
        try:
            rows = self.an.product_rows(None, C.now())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(C.APP_NAME, "读取数据失败：{}".format(str(exc)[:150]))
            return

        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            # 任一关键字段为「—」不参与去重，避免误删
            if r["total_sold"] is None or r["last_hour"] is None or r["price"] is None:
                continue
            if not r["shop_name"]:
                continue
            key = (r["shop_name"], P.normalize_title(r["title"]),
                   int(r["total_sold"]), int(r["last_hour"]))
            groups.setdefault(key, []).append(r)

        plan: list[dict] = []
        for _key, items in groups.items():
            if len(items) < 2:
                continue
            prices = {round(float(i["price"]), 2) for i in items}
            ids = {i["id"] for i in items}
            if len(prices) < 2 or len(ids) < 2:
                continue  # 必须价格不同、商品 ID 不同才算重复
            keep = max(items, key=lambda i: float(i["price"]))
            for i in items:
                if i["id"] != keep["id"]:
                    plan.append({"keep": keep, "drop": i})

        if not plan:
            messagebox.showinfo(
                C.APP_NAME,
                "没有发现符合条件的重复商品。\n\n"
                "判定条件（需全部满足）：同一店铺 + 标题标准化后完全相同 + "
                "累计已售相同 + 上小时销量相同 + 商品 ID 不同 + 价格不同。")
            return
        DedupDialog(self.app, plan, on_done=self.reload)

    def show_expand_preview(self, payload: dict) -> None:
        ExpandDialog(self.app, payload.get("items") or [], on_done=self.reload)

    # ---- 自动拓品 ----

    def expand_shop(self) -> None:
        shops: dict[str, str] = {}
        for p in self.db.list_active_products():
            if p.get("shop_id"):
                shops[p["shop_id"]] = p.get("shop_name") or p["shop_id"]
        if not shops:
            messagebox.showinfo(C.APP_NAME, "当前没有带店铺 ID 的在监控商品。")
            return
        if not messagebox.askyesno(
                C.APP_NAME,
                "自动拓品会尝试打开店铺主页扫描公开商品。\n\n"
                "注意：小红书已不再支持未登录状态在电脑版查看店铺商品，"
                "该功能很可能取不到数据，届时会记录失败并跳过，不影响现有监控。\n\n"
                "共 {} 个店铺，是否继续？".format(len(shops))):
            return

        def worker():
            self.app.ctx.post("status", "正在扫描店铺商品…")
            found: list[dict] = []
            failed = 0
            db = self.db
            col = CO.Collector(db)
            try:
                try:
                    col.open()
                except Exception as exc:  # noqa: BLE001
                    self.app.ctx.post("error", "浏览器不可用，无法自动拓品：{}".format(
                        str(exc)[:200]))
                    return
                for shop_id, shop_name in shops.items():
                    self.app.ctx.post("status", "正在扫描店铺：{}".format(shop_name))
                    try:
                        items = col.scan_shop_products(shop_id)
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        self.app.ctx.post("log", {
                            "level": "WARN", "source": "拓品",
                            "message": "店铺扫描失败：{}".format(shop_name),
                            "detail": str(exc)[:400]})
                        continue
                    exist = {p["id"] for p in db.list_products(include_delisted=True)}
                    deleted = db.deleted_ids()
                    for it in items:
                        if it["id"] in exist or it["id"] in deleted:
                            continue
                        it["shop_name"] = it.get("shop_name") or shop_name
                        found.append(it)
            finally:
                try:
                    col.close()
                except Exception:  # noqa: BLE001
                    pass
            if found:
                self.app.ctx.post("expand_preview", {"items": found, "failed": failed})
            else:
                self.app.ctx.post("message",
                                  "自动拓品未发现可添加的新商品。\n\n"
                                  "失败店铺：{} 个。\n\n该功能依赖店铺主页的公开数据，"
                                  "目前小红书在未登录状态下多数情况取不到，属预期现象。".format(failed))
            self.app.ctx.post("status", "自动拓品结束")

        threading.Thread(target=worker, name="xhs-expand", daemon=True).start()


# --------------------------------------------------------------------------
# 添加商品
# --------------------------------------------------------------------------

class AddTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        self._busy = False
        self._build()

    def _build(self) -> None:
        tk.Label(self, text="添加商品", bg=BG, fg=TEXT,
                 font=(FONT_FAMILY, 12, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
        tk.Label(self, text="支持四种输入混排，一行一个：① 分享口令（整段复制粘贴） "
                            "② xhslink.com 短链　③ 完整商品链接　④ 单独的 24 位商品 ID",
                 bg=BG, fg=SUB, font=(FONT_FAMILY, 9),
                 justify="left").pack(anchor="w", padx=12)

        box = tk.Frame(self, bg=CARD)
        box.pack(fill="both", expand=True, padx=12, pady=8)
        tk.Label(box, text="粘贴商品链接 / 分享口令（可多行批量）", bg=CARD, fg=TEXT,
                 font=(FONT_FAMILY, 10, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        self.txt = tk.Text(box, height=11, font=(FONT_FAMILY, 10), wrap="word",
                           relief="solid", bd=1, bg="#FFFFFF", fg=TEXT,
                           insertbackground=TEXT)
        self.txt.pack(fill="both", expand=True, padx=10)

        bar = tk.Frame(box, bg=CARD)
        bar.pack(fill="x", padx=10, pady=10)
        self.btn_add = ttk.Button(bar, text="解析并添加", style="Accent.TButton",
                                  command=self.start_add)
        self.btn_add.pack(side="left")
        ttk.Button(bar, text="清空输入",
                   command=lambda: self.txt.delete("1.0", "end")).pack(side="left", padx=8)
        ttk.Button(bar, text="解析预览", command=self.preview).pack(side="left")
        self.lbl_state = tk.Label(bar, text="", bg=CARD, fg=SUB, font=(FONT_FAMILY, 9))
        self.lbl_state.pack(side="left", padx=12)

        tk.Label(box, text="处理结果", bg=CARD, fg=TEXT,
                 font=(FONT_FAMILY, 10, "bold")).pack(anchor="w", padx=10, pady=(6, 2))
        self.out = tk.Text(box, height=10, font=(FONT_FAMILY, 10), wrap="word",
                           relief="solid", bd=1, bg="#FAFBFC", fg=TEXT, state="disabled")
        self.out.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def preview(self) -> None:
        parsed = P.parse_batch(self.txt.get("1.0", "end"))
        lines = ["识别到商品 ID：{} 个".format(len(parsed["ids"])),
                 "短链（需跟随重定向）：{} 个".format(len(parsed["short_links"])),
                 "无效输入：{} 行".format(len(parsed["invalid_lines"]))]
        if parsed["ids"]:
            lines.append("")
            lines.extend("· " + i for i in parsed["ids"][:50])
        if parsed["short_links"]:
            lines.append("")
            lines.extend("↗ " + u for u in parsed["short_links"][:20])
        if parsed["invalid_lines"]:
            lines.append("")
            lines.extend("✗ " + ln[:60] for ln in parsed["invalid_lines"][:20])
        self._write("\n".join(lines))

    def _write(self, text: str) -> None:
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)
        self.out.config(state="disabled")

    def show_summary(self, payload: dict) -> None:
        lines = ["处理完成", "",
                 "成功加入：{} 个".format(payload.get("ok", 0)),
                 "已加入待采集：{} 个".format(payload.get("pending", 0)),
                 "已下架跳过：{} 个".format(payload.get("delisted", 0)),
                 "重复跳过：{} 个".format(payload.get("duplicate", 0)),
                 "超过销量上限跳过：{} 个".format(payload.get("over_limit", 0)),
                 "无效输入：{} 行".format(payload.get("invalid", 0))]
        details = payload.get("details") or []
        if details:
            lines.append("")
            lines.extend(details[:200])
        self._write("\n".join(lines))
        self.lbl_state.config(text="处理完成")
        self.btn_add.config(state="normal")
        self._busy = False
        self.app.ctx.post("refresh_board")
        self.app.ctx.post("refresh_usage")
        self.app.ctx.post("refresh_shops")

    def start_add(self) -> None:
        if self._busy:
            return
        text = self.txt.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo(C.APP_NAME, "请先粘贴商品链接或分享口令。")
            return
        self._busy = True
        self.btn_add.config(state="disabled")
        self.lbl_state.config(text="正在解析…")
        threading.Thread(target=self._worker, args=(text,), name="xhs-add",
                         daemon=True).start()

    def _worker(self, text: str) -> None:
        db = self.db
        try:
            parsed = P.parse_batch(text)
            ids: list[str] = list(parsed["ids"])
            short_errors: list[str] = []

            for url in parsed["short_links"]:
                self.app.ctx.post("status", "正在解析短链 {}…".format(url[:60]))
                r = P.resolve_short_link(url)
                if r["ids"]:
                    for i in r["ids"]:
                        if i not in ids:
                            ids.append(i)
                else:
                    short_errors.append("{}（{}）".format(
                        url, r.get("error") or "未取得商品 ID"))

            summary = {"ok": 0, "pending": 0, "delisted": 0, "duplicate": 0,
                       "over_limit": 0, "invalid": len(parsed["invalid_lines"]),
                       "details": []}
            for line in parsed["invalid_lines"][:50]:
                summary["details"].append("✗ 无效输入：{}".format(line[:70]))
            for e in short_errors[:20]:
                summary["details"].append("✗ 短链解析失败：{}".format(e))

            if not ids:
                self.app.ctx.post("add_summary", summary)
                return

            # 单个添加：先预采集，再弹窗让用户确认
            if len(ids) == 1:
                pid = ids[0]
                if db.product_exists(pid):
                    summary["duplicate"] = 1
                    summary["details"].append("= 该商品已在监控中：{}".format(pid))
                    self.app.ctx.post("add_summary", summary)
                    return
                self.app.ctx.post("status", "正在获取商品信息…")
                outcome = CO.probe_product(pid, db)
                if outcome.kind == "delisted":
                    summary["delisted"] = 1
                    summary["details"].append("✗ 已下架：{}（{}）".format(
                        outcome.error, pid))
                    self.app.ctx.post("add_summary", summary)
                    return
                if not outcome.ok:
                    if messagebox.askyesno(
                            C.APP_NAME,
                            "该商品暂未取到完整信息：\n\n{}\n\n"
                            "是否仍然加入监控？加入后会在后续轮次继续自动采集。".format(
                                (outcome.error or "未知原因")[:200])):
                        db.add_product(pid, "", "", "", "", source="manual")
                        db.mark_failure(pid, "首次采集未取到数据，已加入待采集")
                        summary["pending"] += 1
                        summary["details"].append("… 加入待采集：{}".format(pid))
                        db.log("INFO", "添加", "商品 {} 加入待采集队列".format(pid))
                    else:
                        summary["details"].append("× 用户取消：{}".format(pid))
                    self.app.ctx.post("add_summary", summary)
                    return
                self.app.ctx.post("confirm_add", {"pid": pid, "info": outcome.info,
                                                  "summary": summary})
                return

            # 批量添加
            total = len(ids)
            for n, pid in enumerate(ids, 1):
                self.app.ctx.post("status", "正在处理 {}/{}：{}".format(n, total, pid))
                if db.product_exists(pid) or db.is_deleted(pid):
                    summary["duplicate"] += 1
                    summary["details"].append("= 已存在或曾删除，跳过：{}".format(pid))
                    continue
                outcome = CO.probe_product(pid, db)
                if outcome.kind == "delisted":
                    summary["delisted"] += 1
                    summary["details"].append("✗ 已下架：{}".format(pid))
                    continue
                if outcome.ok and outcome.info.sold is not None \
                        and int(outcome.info.sold) > C.MAX_SOLD_TO_MONITOR:
                    summary["over_limit"] += 1
                    summary["details"].append("✗ 累计已售 {} 超过 {} 上限：{} {}".format(
                        outcome.info.sold, C.MAX_SOLD_TO_MONITOR,
                        (outcome.info.title or "")[:30], pid))
                    continue
                if outcome.ok:
                    info = outcome.info
                    db.add_product(pid, info.title, info.shop_name, info.shop_id,
                                   info.cover, source="batch")
                    db.mark_success(pid, outcome.method)
                    db.add_snapshot(pid, C.fmt(C.hour_start(C.now())), info.sold,
                                    info.shop_sold, info.price, info.fans,
                                    info.stock_status, info.deliverable, outcome.method)
                    summary["ok"] += 1
                    summary["details"].append("✓ {}｜{}｜{}｜已售 {}".format(
                        (info.title or pid)[:30], info.shop_name or A.DASH,
                        fmt_price(info.price), fmt_num(info.sold)))
                else:
                    db.add_product(pid, "", "", "", "", source="batch")
                    db.mark_failure(pid, "首次采集未取到数据，已加入待采集")
                    summary["pending"] += 1
                    summary["details"].append("… 加入待采集：{}（{}）".format(
                        pid, (outcome.error or "")[:60]))
                time.sleep(random.uniform(0.1, 0.3))

            db.log("SUCCESS", "添加",
                   "批量添加完成：成功 {}，待采集 {}，下架 {}，重复 {}，超限 {}，无效 {}".format(
                       summary["ok"], summary["pending"], summary["delisted"],
                       summary["duplicate"], summary["over_limit"], summary["invalid"]))
            self.app.ctx.post("add_summary", summary)
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.app.ctx.post("log", {"level": "ERROR", "source": "添加",
                                      "message": "添加流程异常：{}".format(str(exc)[:160]),
                                      "detail": traceback.format_exc()})
            self.app.ctx.post("error", "添加失败：{}".format(str(exc)[:200]))
            self.app.ctx.post("add_summary", {"ok": 0, "pending": 0, "delisted": 0,
                                              "duplicate": 0, "over_limit": 0,
                                              "invalid": 0, "details": []})

    def confirm_single(self, payload: dict) -> None:
        """主线程：弹窗展示信息并让用户确认是否加入。"""
        pid = payload["pid"]
        info = payload["info"]
        summary = payload["summary"]
        dlg = SingleAddDialog(self.app, info)
        self.app.wait_window(dlg)
        if not dlg.result:
            summary["details"].append("× 用户取消：{}".format(pid))
            self.app.ctx.post("add_summary", summary)
            return
        if info.sold is not None and int(info.sold) > C.MAX_SOLD_TO_MONITOR:
            summary["over_limit"] += 1
            summary["details"].append("✗ 累计已售 {} 超过 {} 上限：{}".format(
                info.sold, C.MAX_SOLD_TO_MONITOR, (info.title or "")[:30]))
            messagebox.showwarning(C.APP_NAME, "该商品累计已售 {}，超过监控上限 {}，不支持监控。"
                                   .format(info.sold, C.MAX_SOLD_TO_MONITOR))
            self.app.ctx.post("add_summary", summary)
            return

        db = self.db
        db.add_product(pid, info.title, info.shop_name, info.shop_id, info.cover,
                       source="manual")
        db.mark_success(pid, "api")
        db.add_snapshot(pid, C.fmt(C.hour_start(C.now())), info.sold, info.shop_sold,
                        info.price, info.fans, info.stock_status, info.deliverable, "api")
        summary["ok"] += 1
        summary["details"].append("✓ {}｜{}｜{}｜已售 {}".format(
            (info.title or pid)[:30], info.shop_name or A.DASH,
            fmt_price(info.price), fmt_num(info.sold)))
        db.log("SUCCESS", "添加", "商品已加入监控：{}".format((info.title or pid)[:40]),
               product_id=pid, title=info.title)
        self.app.ctx.post("add_summary", summary)

        # 同店其它已发现的未监控商品提示
        try:
            others = [p for p in db.list_products(include_delisted=False)
                      if p.get("shop_id") == info.shop_id and p["id"] != pid]
            if others:
                messagebox.showinfo(C.APP_NAME,
                                    "店铺「{}」在本机还发现了 {} 个已监控商品：\n\n{}".format(
                                        info.shop_name or A.DASH, len(others),
                                        "\n".join("· {}　{}".format(
                                            (o.get("title") or o["id"])[:26],
                                            fmt_price(self._price_of(o["id"])))
                                            for o in others[:10])))
        except Exception:  # noqa: BLE001
            pass

    def _price_of(self, pid: str):
        try:
            snap = self.db.latest_snapshot(pid)
            return snap.get("price") if snap else None
        except Exception:  # noqa: BLE001
            return None


# --------------------------------------------------------------------------
# 失败列表
# --------------------------------------------------------------------------

class FailTab(BaseTab):
    COLS = [
        ("title", "商品名称", 300),
        ("shop", "店铺", 160),
        ("id", "商品 ID", 220),
        ("time", "最近失败时间", 150),
        ("reason", "失败原因", 360),
    ]

    def __init__(self, master, app):
        super().__init__(master, app)
        self._rows: list[dict] = []
        self._build()

    def _build(self) -> None:
        top = tk.Frame(self, bg=CARD)
        top.pack(fill="x", pady=(0, 6))
        tk.Label(top, text="失败列表", bg=CARD, fg=TEXT,
                 font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=12, pady=10)
        tk.Label(top, text="只记录临时采集失败与环境错误；明确下架的商品会移到「下架列表」",
                 bg=CARD, fg=SUB, font=(FONT_FAMILY, 9)).pack(side="left")

        box = tk.Frame(top, bg=CARD)
        box.pack(side="right", padx=12)
        for text, cmd in (("刷新", self.reload), ("删除选中", self.delete_selected),
                          ("忽略选中并保留监控", self.ignore_selected),
                          ("重试选中", self.retry_selected), ("全选", self.select_all)):
            ttk.Button(box, text=text, command=cmd).pack(side="right", padx=(6, 0))

        wrap, self.tv = make_tree(self, self.COLS, height=22)
        wrap.pack(fill="both", expand=True)
        bind_sort(self.tv, [c[0] for c in self.COLS], default_key="time")
        self.tv.bind("<Double-1>", self._detail)

        self.lbl = tk.Label(self, text="", bg=BG, fg=SUB, font=(FONT_FAMILY, 9), anchor="w")
        self.lbl.pack(fill="x", padx=4, pady=4)

    def reload(self) -> None:
        try:
            self._rows = self.db.list_failed_products()
        except Exception:  # noqa: BLE001
            self._rows = []
        tv = self.tv
        for item in tv.get_children(""):
            tv.delete(item)
        for p in self._rows:
            tv.insert("", "end", iid=p["id"], tags=("error",), values=(
                (p.get("title") or p["id"])[:60],
                (p.get("shop_name") or A.DASH)[:24],
                p["id"],
                fmt_dt(p.get("fail_at"), True),
                (p.get("fail_reason") or "未知错误")[:200],
            ))
        self.lbl.config(text="共 {} 个失败商品　|　双击可查看已有历史数据".format(len(self._rows)))

    def select_all(self) -> None:
        self.tv.selection_set(self.tv.get_children(""))
        return "break"

    def selected(self) -> list[str]:
        return list(self.tv.selection())

    def _detail(self, event) -> None:
        item = self.tv.identify_row(event.y)
        if not item:
            return
        row = next((r for r in self.an.product_rows(None, C.now()) if r["id"] == item), None)
        if row:
            ProductDetailDialog(self.app, row)

    def retry_selected(self) -> None:
        ids = self.selected()
        if not ids:
            messagebox.showinfo(C.APP_NAME, "请先选择要重试的商品。")
            return
        for pid in ids:
            self.db.update_product_meta(pid, ignored=0)
        self.app.set_status("正在重试 {} 个商品…".format(len(ids)))

        def worker():
            db = self.db
            col = CO.Collector(db)
            ok = still = delisted = 0
            try:
                try:
                    col.open()
                except Exception:  # noqa: BLE001
                    pass
                for pid in ids:
                    prod = db.get_product(pid) or {"id": pid}
                    outcome = col.collect_one(pid)
                    if outcome.ok:
                        col.save(pid, outcome.info, C.fmt(C.hour_start(C.now())),
                                 outcome.method)
                        ok += 1
                    elif outcome.kind == "delisted":
                        db.mark_delisted(pid, outcome.delist_reason or "商品已下架")
                        delisted += 1
                    else:
                        db.mark_failure(pid, outcome.error)
                        still += 1
                    db.log("SUCCESS" if outcome.ok else "WARN", "重试",
                           "重试{}：{}".format("成功" if outcome.ok else "失败",
                                             (prod.get("title") or pid)[:40]),
                           detail=outcome.error, product_id=pid,
                           title=prod.get("title") or "")
            finally:
                try:
                    col.close()
                except Exception:  # noqa: BLE001
                    pass
            self.app.ctx.post("message", "重试完成：成功 {}，仍失败 {}，确认下架 {}".format(
                ok, still, delisted))
            for kind in ("refresh_fail", "refresh_board", "refresh_delisted", "refresh_usage"):
                self.app.ctx.post(kind)
            self.app.ctx.post("status", "重试完成")

        threading.Thread(target=worker, name="xhs-retry", daemon=True).start()

    def ignore_selected(self) -> None:
        ids = self.selected()
        if not ids:
            messagebox.showinfo(C.APP_NAME, "请先选择商品。")
            return
        for pid in ids:
            self.db.ignore_failure(pid)
        self.db.log("INFO", "失败列表", "已忽略 {} 个商品的失败提示，下一轮继续采集".format(len(ids)))
        self.reload()
        self.app.ctx.post("refresh_board")

    def delete_selected(self) -> None:
        ids = self.selected()
        if not ids:
            messagebox.showinfo(C.APP_NAME, "请先选择商品。")
            return
        for pid in list(ids):
            prompt_delete_product(self.app, pid, single=(len(ids) == 1))
        self.reload()
        self.app.ctx.post("refresh_board")


# --------------------------------------------------------------------------
# 下架列表
# --------------------------------------------------------------------------

class DelistedTab(BaseTab):
    COLS = [
        ("title", "商品名称", 320),
        ("shop", "店铺", 180),
        ("id", "商品 ID", 220),
        ("time", "下架时间", 160),
        ("status", "下架状态", 140),
    ]

    def __init__(self, master, app):
        super().__init__(master, app)
        self._rows: list[dict] = []
        self._build()

    def _build(self) -> None:
        top = tk.Frame(self, bg=CARD)
        top.pack(fill="x", pady=(0, 6))
        tk.Label(top, text="下架列表", bg=CARD, fg=TEXT,
                 font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=12, pady=10)
        tk.Label(top, text="已从竞品看板、店铺分析与后续采集任务中排除，历史快照保留",
                 bg=CARD, fg=SUB, font=(FONT_FAMILY, 9)).pack(side="left")

        box = tk.Frame(top, bg=CARD)
        box.pack(side="right", padx=12)
        for text, cmd in (("刷新", self.reload), ("删除选中", self.delete_selected),
                          ("恢复监控", self.restore_selected)):
            ttk.Button(box, text=text, command=cmd).pack(side="right", padx=(6, 0))

        wrap, self.tv = make_tree(self, self.COLS, height=22)
        wrap.pack(fill="both", expand=True)
        bind_sort(self.tv, [c[0] for c in self.COLS], default_key="time")
        self.tv.bind("<Double-1>", self._detail)
        self.lbl = tk.Label(self, text="", bg=BG, fg=SUB, font=(FONT_FAMILY, 9), anchor="w")
        self.lbl.pack(fill="x", padx=4, pady=4)

    def reload(self) -> None:
        try:
            self._rows = self.db.list_delisted_products()
        except Exception:  # noqa: BLE001
            self._rows = []
        tv = self.tv
        for item in tv.get_children(""):
            tv.delete(item)
        for p in self._rows:
            tv.insert("", "end", iid=p["id"], tags=("delisted",), values=(
                (p.get("title") or p["id"])[:60],
                (p.get("shop_name") or A.DASH)[:24],
                p["id"],
                fmt_dt(p.get("delisted_at"), True),
                p.get("delisted_reason") or "商品已下架",
            ))
        self.lbl.config(text="共 {} 个已下架商品　|　双击可查看下架前的历史数据".format(
            len(self._rows)))

    def selected(self) -> list[str]:
        return list(self.tv.selection())

    def _detail(self, event) -> None:
        item = self.tv.identify_row(event.y)
        if not item:
            return
        row = next((r for r in self.an.product_rows(None, C.now()) if r["id"] == item), None)
        if row:
            ProductDetailDialog(self.app, row)

    def restore_selected(self) -> None:
        ids = self.selected()
        if not ids:
            messagebox.showinfo(C.APP_NAME, "请先选择商品。")
            return
        if not messagebox.askyesno(C.APP_NAME,
                                   "确认将选中的 {} 个商品恢复监控？".format(len(ids))):
            return
        for pid in ids:
            self.db.restore_product(pid)
        self.db.log("INFO", "下架列表", "恢复监控 {} 个商品".format(len(ids)))
        self.reload()
        self.app.ctx.post("refresh_board")

    def delete_selected(self) -> None:
        ids = self.selected()
        if not ids:
            messagebox.showinfo(C.APP_NAME, "请先选择商品。")
            return
        for pid in list(ids):
            prompt_delete_product(self.app, pid, single=(len(ids) == 1))
        self.reload()
        self.app.ctx.post("refresh_board")


# --------------------------------------------------------------------------
# 店铺分析
# --------------------------------------------------------------------------

class ShopTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        self._date = tk.StringVar(value=C.now().strftime(C.DAY_FMT))
        self._mode = "hourly"
        self._days = 7
        self._shop_var = tk.StringVar()
        self._prod_rows: list[dict] = []
        self._build()

    def _build(self) -> None:
        top = tk.Frame(self, bg=CARD)
        top.pack(fill="x", pady=(0, 6))
        tk.Label(top, text="店铺分析", bg=CARD, fg=TEXT,
                 font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=(12, 12), pady=10)
        tk.Label(top, text="店铺", bg=CARD, fg=SUB,
                 font=(FONT_FAMILY, 9)).pack(side="left")
        self.cmb_shop = ttk.Combobox(top, textvariable=self._shop_var, width=28,
                                     state="readonly")
        self.cmb_shop.pack(side="left", padx=(6, 14))
        self.cmb_shop.bind("<<ComboboxSelected>>", lambda e: self.load_shop())

        tk.Label(top, text="日期", bg=CARD, fg=SUB,
                 font=(FONT_FAMILY, 9)).pack(side="left")
        ttk.Entry(top, textvariable=self._date, width=13).pack(side="left", padx=(6, 6))
        ttk.Button(top, text="查看该日", command=self.load_shop).pack(side="left")
        ttk.Button(top, text="近7天", command=lambda: self._quick(7)).pack(
            side="left", padx=(6, 0))
        ttk.Button(top, text="近10天", command=lambda: self._quick(10)).pack(
            side="left", padx=(6, 0))
        ttk.Button(top, text="刷新", command=self.reload_shops).pack(side="right", padx=12)

        self.lbl_info = tk.Label(self, text="", bg=BG, fg=TEXT, font=(FONT_FAMILY, 10),
                                 anchor="w")
        self.lbl_info.pack(fill="x", padx=6, pady=(0, 4))

        self.chart = LineChart(self, height=215)
        self.chart.configure(bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self.chart.pack(fill="x", padx=4, pady=(0, 6))

        split = tk.Frame(self, bg=BG)
        split.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        left = tk.LabelFrame(split, text="销量明细", bg=CARD, fg=ACCENT_DARK,
                             font=(FONT_FAMILY, 10, "bold"))
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        w1, self.tv_detail = make_tree(
            left, [("time", "时间", 140), ("sales", "销量增量", 100),
                   ("amount", "模糊销售额", 130)], height=12, selectmode="browse")
        w1.pack(fill="both", expand=True, padx=6, pady=6)
        tk.Label(left, text="{}（销量增量 × 当时实际价格）".format(A.FUZZY_NOTE), bg=CARD,
                 fg=SUB, font=(FONT_FAMILY, 8)).pack(anchor="w", padx=8, pady=(0, 6))

        right = tk.LabelFrame(split, text="店铺内监控商品（按今日销量降序）", bg=CARD,
                              fg=ACCENT_DARK, font=(FONT_FAMILY, 10, "bold"))
        right.pack(side="left", fill="both", expand=True)
        w2, self.tv_prod = make_tree(
            right, [("title", "商品名称", 220), ("today", "今日销量", 90),
                    ("price", "当前价格", 90), ("total", "累计已售", 90),
                    ("id", "商品 ID", 200)], height=12, selectmode="browse")
        w2.pack(fill="both", expand=True, padx=6, pady=6)
        self.tv_prod.bind("<Double-1>", self._open_product)

    def _quick(self, days: int) -> None:
        self._mode = "daily"
        self._days = days
        self.load_shop()

    def on_show(self) -> None:
        self.reload_shops()

    def reload(self) -> None:
        self.reload_shops()

    def reload_shops(self) -> None:
        try:
            shops = self.db.shop_names()
        except Exception:  # noqa: BLE001
            shops = []
        self.cmb_shop["values"] = shops
        if shops and self._shop_var.get() not in shops:
            self._shop_var.set(shops[0])
        if not shops:
            self.lbl_info.config(text="暂无店铺数据，请先添加商品并完成一次采集。")
            self.chart.set_data([], "")
            self._clear_tables()
            return
        self.load_shop()

    def _clear_tables(self) -> None:
        for tv in (self.tv_detail, self.tv_prod):
            for i in tv.get_children(""):
                tv.delete(i)

    def load_shop(self) -> None:
        shop = self._shop_var.get()
        if not shop:
            return
        try:
            detail = self.an.shop_detail(shop, C.now())
        except Exception as exc:  # noqa: BLE001
            self.lbl_info.config(text="加载失败：{}".format(str(exc)[:120]))
            return

        self.lbl_info.config(
            text="店铺：{}　|　监控商品数：{}　|　累计已售：{}　|　今日销量：{}　|　"
                 "上小时销量：{}".format(
                     shop, detail["product_count"], fmt_num(detail["total_sold"]),
                     fmt_num(detail["today"]), fmt_num(detail["last_hour"])))

        for i in self.tv_prod.get_children(""):
            self.tv_prod.delete(i)
        prods = sorted(detail["products"], key=lambda r: -(r["today"] or 0))
        self._prod_rows = prods
        for i, r in enumerate(prods):
            self.tv_prod.insert("", "end", iid=r["id"],
                                tags=("odd" if i % 2 else "even",),
                                values=((r["title"] or r["id"])[:48], fmt_num(r["today"]),
                                        fmt_price(r["price"]), fmt_num(r["total_sold"]),
                                        r["id"]))

        if self._mode == "daily":
            series = self.an.shop_daily_series(shop, self._days)
            self.chart.set_data([(s["label"], s["sales"]) for s in series],
                                "{} 近{}天每日销量".format(shop, self._days))
            self._fill_detail([(s["label"], s["sales"], s["amount"]) for s in series])
        else:
            day = (self._date.get() or "").strip() or C.now().strftime(C.DAY_FMT)
            series = self.an.shop_hourly_series(shop, day)
            self.chart.set_data([(s["label"], s["sales"]) for s in series],
                                "{} {} 每小时销量".format(shop, day))
            self._fill_detail([(s["label"], s["sales"], s["amount"]) for s in series])

    def _fill_detail(self, rows: list[tuple]) -> None:
        for i in self.tv_detail.get_children(""):
            self.tv_detail.delete(i)
        for i, (label, sales, amount) in enumerate(rows):
            self.tv_detail.insert("", "end", tags=("odd" if i % 2 else "even",),
                                  values=(label, fmt_num(sales),
                                          A.DASH if amount is None
                                          else "¥{:.2f}".format(amount)))

    def _open_product(self, event) -> None:
        item = self.tv_prod.identify_row(event.y)
        if not item:
            return
        row = next((r for r in self._prod_rows if r["id"] == item), None)
        if row:
            ProductDetailDialog(self.app, row)


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------

class SettingsTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        self._build()

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        def _wheel(e):
            try:
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:  # noqa: BLE001
                pass
        canvas.bind_all("<MouseWheel>", _wheel)

        self._group_monitor(self.inner)
        self._group_wecom(self.inner)
        self._group_mcp(self.inner)
        tk.Frame(self.inner, bg=BG, height=20).pack()

    def _card(self, parent, title: str) -> tk.Frame:
        box = tk.LabelFrame(parent, text=title, bg=CARD, fg=ACCENT_DARK,
                            font=(FONT_FAMILY, 10, "bold"), bd=1, relief="solid")
        box.pack(fill="x", padx=12, pady=(10, 0))
        return box

    # ---- 第一组：监控设置 ----

    def _group_monitor(self, parent) -> None:
        box = self._card(parent, "监控设置")
        grid = tk.Frame(box, bg=CARD)
        grid.pack(fill="x", padx=14, pady=12)

        self.v_interval = tk.StringVar(value=str(self.db.get_int("collect_interval_minutes", 60)))
        self.v_gap_min = tk.StringVar(value=str(self.db.get_float("min_gap_seconds", 0.1)))
        self.v_gap_max = tk.StringVar(value=str(self.db.get_float("max_gap_seconds", 0.3)))

        rows = [
            ("采集周期（分钟）", self.v_interval,
             "按自然时间边界调度；60 表示每个整点采集一次"),
            ("商品间隔最小秒数", self.v_gap_min, "取值范围 0.1 ~ 60，实际间隔在区间内随机"),
            ("商品间隔最大秒数", self.v_gap_max, "取值范围 0.1 ~ 60，实际间隔在区间内随机"),
        ]
        for i, (label, var, hint) in enumerate(rows):
            tk.Label(grid, text=label, bg=CARD, fg=TEXT, font=(FONT_FAMILY, 10),
                     width=18, anchor="w").grid(row=i, column=0, sticky="w", pady=6)
            ttk.Entry(grid, textvariable=var, width=12).grid(row=i, column=1, sticky="w",
                                                             padx=(0, 12))
            tk.Label(grid, text=hint, bg=CARD, fg=SUB, font=(FONT_FAMILY, 9),
                     anchor="w").grid(row=i, column=2, sticky="w")

        bar = tk.Frame(box, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(0, 10))
        ttk.Button(bar, text="保存设置", style="Accent.TButton",
                   command=self.save_monitor).pack(side="left")
        ttk.Button(bar, text="打开浏览器处理验证",
                   command=self.open_browser).pack(side="left", padx=8)
        self.lbl_count = tk.Label(bar, text="", bg=CARD, fg=SUB, font=(FONT_FAMILY, 9))
        self.lbl_count.pack(side="left", padx=12)

    def save_monitor(self) -> None:
        try:
            interval = int(float(self.v_interval.get()))
            gmin = float(self.v_gap_min.get())
            gmax = float(self.v_gap_max.get())
        except ValueError:
            messagebox.showerror(C.APP_NAME, "请填写合法数字。")
            return
        if not (1 <= interval <= 24 * 60):
            messagebox.showerror(C.APP_NAME, "采集周期应在 1 ~ 1440 分钟之间。")
            return
        if not (0.1 <= gmin <= 60 and 0.1 <= gmax <= 60):
            messagebox.showerror(C.APP_NAME, "商品间隔秒数应在 0.1 ~ 60 之间。")
            return
        if gmin > gmax:
            gmin, gmax = gmax, gmin
        self.db.set_settings({
            "collect_interval_minutes": str(interval),
            "min_gap_seconds": str(gmin),
            "max_gap_seconds": str(gmax),
        })
        self.db.log("INFO", "设置", "监控设置已保存：周期 {} 分钟，商品间隔 {}-{} 秒".format(
            interval, gmin, gmax))
        self.app.scheduler.nudge()
        self._update_count()
        messagebox.showinfo(C.APP_NAME, "设置已保存。\n\n下一次采集会按新的自然时间边界重新计算。")

    def open_browser(self) -> None:
        def worker():
            col = CO.Collector(self.db)
            try:
                b = col._ensure_visible()
                self.app.ctx.post("status", "已打开可见浏览器，请完成验证后关闭窗口")
                self.app.ctx.post("message",
                                  "已打开可见浏览器。\n\n请在窗口中完成可能出现的验证，"
                                  "完成后直接关闭窗口即可。\n\n浏览器：{}".format(
                                      b.browser_label or "Chromium"))
                self.db.log("INFO", "设置", "已打开可见浏览器用于处理验证")
            except Exception as exc:  # noqa: BLE001
                self.app.ctx.post("error", "打开浏览器失败：{}".format(str(exc)[:220]))
        threading.Thread(target=worker, name="xhs-open-browser", daemon=True).start()

    def _update_count(self) -> None:
        try:
            n = len(self.db.list_active_products())
            gmin = self.db.get_float("min_gap_seconds", 0.1)
            gmax = self.db.get_float("max_gap_seconds", 0.3)
            self.lbl_count.config(
                text="当前商品数量：{} 个　|　预估单轮等待时间约 {:.1f} 秒".format(
                    n, n * (gmin + gmax) / 2))
        except Exception:  # noqa: BLE001
            pass

    # ---- 第二组：企业微信 ----

    def _group_wecom(self, parent) -> None:
        box = self._card(parent, "企业微信机器人通知")
        grid = tk.Frame(box, bg=CARD)
        grid.pack(fill="x", padx=14, pady=12)

        self.v_hook = tk.StringVar(value=self.db.get_setting("wecom_webhook", "") or "")
        self.v_enabled = tk.BooleanVar(value=self.db.get_bool("wecom_enabled", False))

        tk.Label(grid, text="Webhook 地址", bg=CARD, fg=TEXT, font=(FONT_FAMILY, 10),
                 width=18, anchor="w").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(grid, textvariable=self.v_hook, width=80).grid(row=0, column=1,
                                                                 sticky="w", pady=6)
        ttk.Checkbutton(grid, text="启用通知", variable=self.v_enabled).grid(
            row=1, column=1, sticky="w", pady=4)

        bar = tk.Frame(box, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(0, 10))
        ttk.Button(bar, text="保存", style="Accent.TButton",
                   command=self.save_wecom).pack(side="left")
        ttk.Button(bar, text="发送测试通知", command=self.test_wecom).pack(side="left",
                                                                          padx=8)
        ttk.Button(bar, text="设置通知店铺", command=self.pick_shops).pack(side="left")
        self.lbl_wecom = tk.Label(bar, text="", bg=CARD, fg=SUB, font=(FONT_FAMILY, 9))
        self.lbl_wecom.pack(side="left", padx=12)
        self._refresh_wecom_status()

    def _refresh_wecom_status(self) -> None:
        hook = self.db.get_setting("wecom_webhook", "") or ""
        enabled = self.db.get_bool("wecom_enabled", False)
        shops = self.db.get_json("wecom_shops", []) or []
        ok, reason = W.validate_webhook(hook)
        state = "已启用" if enabled else "未启用"
        scope = "全部店铺" if not shops else "{} 个店铺".format(len(shops))
        self.lbl_wecom.config(
            text="当前状态：{}　|　Webhook：{}　|　通知范围：{}".format(
                state, W.mask_webhook(hook) if ok else "格式无效（{}）".format(reason), scope),
            fg=SUB if ok else DANGER)

    def save_wecom(self) -> None:
        hook = (self.v_hook.get() or "").strip()
        if hook:
            ok, reason = W.validate_webhook(hook)
            if not ok and not messagebox.askyesno(
                    C.APP_NAME, "Webhook 校验未通过：{}\n\n仍然保存吗？".format(reason)):
                return
        self.db.set_settings({"wecom_webhook": hook,
                              "wecom_enabled": "1" if self.v_enabled.get() else "0"})
        self.db.log("INFO", "设置", "企业微信通知配置已更新（{}）".format(
            "已启用" if self.v_enabled.get() else "未启用"))
        self._refresh_wecom_status()
        messagebox.showinfo(C.APP_NAME, "已保存。")

    def test_wecom(self) -> None:
        hook = (self.v_hook.get() or "").strip()
        ok, reason = W.validate_webhook(hook)
        if not ok:
            messagebox.showerror(C.APP_NAME, "Webhook 格式不正确：{}".format(reason))
            return
        self.lbl_wecom.config(text="正在发送测试通知…")

        def worker():
            ok2, detail = W.send_test(hook)
            self.db.log("SUCCESS" if ok2 else "ERROR", "通知",
                        "测试通知{}：{}".format("发送成功" if ok2 else "发送失败", detail))
            self.app.ctx.post("message", "测试通知{}。\n\n{}".format(
                "发送成功" if ok2 else "发送失败", detail))
            self.app.ctx.post("status", "测试通知已执行")
        threading.Thread(target=worker, daemon=True).start()

    def pick_shops(self) -> None:
        shops = self.db.shop_names()
        if not shops:
            messagebox.showinfo(C.APP_NAME, "还没有店铺数据。")
            return
        ShopPickerDialog(self.app, shops, self.db.get_json("wecom_shops", []) or [],
                         on_save=self._on_shops_saved)

    def _on_shops_saved(self, selected: list[str]) -> None:
        self.db.set_setting("wecom_shops", selected)
        self.db.log("INFO", "设置", "通知店铺已更新：{}".format(
            "全部" if not selected else "、".join(selected[:20])))
        self._refresh_wecom_status()

    # ---- 第三组：MCP ----

    def _group_mcp(self, parent) -> None:
        box = self._card(parent, "AI / MCP 数据读取（只读）")
        tk.Label(box, text="本软件提供一个只读 MCP 服务，供 AI 助手读取本机的店铺、商品、"
                           "销量、销售额与排名数据。该服务不提供任何增删改工具，"
                           "无法修改数据库。",
                 bg=CARD, fg=SUB, font=(FONT_FAMILY, 9), justify="left",
                 wraplength=900).pack(anchor="w", padx=14, pady=(10, 6))
        self.txt_mcp = tk.Text(box, height=10, font=(FONT_FAMILY, 9), wrap="word",
                               relief="solid", bd=1, bg="#FAFBFC", fg=TEXT)
        self.txt_mcp.pack(fill="x", padx=14, pady=(0, 8))
        bar = tk.Frame(box, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(bar, text="复制 MCP 配置说明", style="Accent.TButton",
                   command=self.copy_mcp).pack(side="left")
        ttk.Button(bar, text="刷新", command=self.refresh_mcp).pack(side="left", padx=8)

    @staticmethod
    def mcp_command() -> str:
        exe = os.path.join(C.APP_DIR, C.MCP_EXE_NAME)
        if os.path.exists(exe):
            return exe
        script = os.path.join(C.APP_DIR, C.MCP_SCRIPT_NAME)
        if os.path.exists(script):
            return script
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            C.MCP_SCRIPT_NAME)

    def mcp_text(self) -> str:
        return ("请帮我配置一个本地 MCP 服务器，名称设为 `xhs-sales-monitor`。"
                "服务器使用 `stdio` 通信，命令路径是：\n{}\n"
                "配置完成后请连接并调用 `tools/list` 验证。"
                "这个 MCP 用于读取我本机红薯雷达中的小红书店铺、商品、销量、"
                "销售额和排名数据；只允许读取，不要修改数据库。".format(self.mcp_command()))

    def refresh_mcp(self) -> None:
        tools = ("list_shops / list_products / get_product_sales / get_shop_sales / "
                 "get_product_ranking / get_shop_ranking / search_products / get_overview")
        text = (self.mcp_text() + "\n\n—— 服务信息 ——\n"
                "通信方式：stdio\n"
                "数据库：{}（以只读方式打开）\n"
                "可用工具：{}\n"
                "权限：只读，不提供任何增删改工具".format(self.db.path, tools))
        self.txt_mcp.config(state="normal")
        self.txt_mcp.delete("1.0", "end")
        self.txt_mcp.insert("1.0", text)
        self.txt_mcp.config(state="disabled")

    def copy_mcp(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(self.mcp_text())
            self.app.set_status("MCP 配置说明已复制到剪贴板")
            messagebox.showinfo(C.APP_NAME, "已复制 MCP 配置说明到剪贴板，可直接粘贴给 AI 助手。")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(C.APP_NAME, "复制失败：{}".format(str(exc)[:150]))

    def on_show(self) -> None:
        self._update_count()
        self._refresh_wecom_status()
        self.refresh_mcp()


# --------------------------------------------------------------------------
# 运行日志
# --------------------------------------------------------------------------

class LogTab(BaseTab):
    COLS = [
        ("ts", "时间", 160),
        ("level", "级别", 80),
        ("source", "来源", 100),
        ("product", "商品", 220),
        ("pid", "商品 ID", 200),
        ("msg", "摘要", 480),
    ]

    def __init__(self, master, app):
        super().__init__(master, app)
        self._filter = tk.StringVar(value="全部")
        self._rows: list[dict] = []
        self._build()

    def _build(self) -> None:
        top = tk.Frame(self, bg=CARD)
        top.pack(fill="x", pady=(0, 6))
        tk.Label(top, text="运行日志", bg=CARD, fg=TEXT,
                 font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=12, pady=10)
        tk.Label(top, text="筛选", bg=CARD, fg=SUB,
                 font=(FONT_FAMILY, 9)).pack(side="left")
        cmb = ttk.Combobox(top, textvariable=self._filter, width=10, state="readonly",
                           values=["全部", "成功", "失败", "信息"])
        cmb.pack(side="left", padx=6)
        cmb.bind("<<ComboboxSelected>>", lambda e: self.reload())

        box = tk.Frame(top, bg=CARD)
        box.pack(side="right", padx=12)
        for text, cmd in (("导出 CSV", self.export_csv), ("清空日志", self.clear_logs),
                          ("刷新", self.reload)):
            ttk.Button(box, text=text, command=cmd).pack(side="right", padx=(6, 0))

        wrap, self.tv = make_tree(self, self.COLS, height=22, selectmode="browse")
        wrap.pack(fill="both", expand=True)
        bind_sort(self.tv, [c[0] for c in self.COLS], default_key="ts")
        self.tv.bind("<Double-1>", self._detail)

        self.lbl = tk.Label(self, text="", bg=BG, fg=SUB, font=(FONT_FAMILY, 9), anchor="w")
        self.lbl.pack(fill="x", padx=4, pady=4)

    def _level_filter(self) -> str:
        return {"全部": "ALL", "成功": "SUCCESS", "失败": "ERROR",
                "信息": "INFO"}.get(self._filter.get(), "ALL")

    def reload(self) -> None:
        try:
            self._rows = self.db.query_logs(self._level_filter(), limit=C.LOG_PAGE_SIZE)
        except Exception:  # noqa: BLE001
            self._rows = []
        tv = self.tv
        for i in tv.get_children(""):
            tv.delete(i)
        for i, r in enumerate(self._rows):
            lvl = r.get("level") or "INFO"
            tag = {"ERROR": "error", "WARN": "warn"}.get(lvl, "odd" if i % 2 else "even")
            tv.insert("", "end", iid=str(r["id"]), tags=(tag,), values=(
                fmt_dt(r.get("ts"), True), lvl, r.get("source") or "",
                (r.get("title") or "")[:40], r.get("product_id") or "",
                (r.get("message") or "")[:160],
            ))
        self.lbl.config(text="显示最新 {} 条　|　日志总数 {} 条　|　双击查看完整详情".format(
            len(self._rows), self.db.log_count()))

    def _detail(self, event) -> None:
        item = self.tv.identify_row(event.y)
        if not item:
            return
        row = next((r for r in self._rows if str(r["id"]) == item), None)
        if row:
            LogDetailDialog(self.app, row)

    def clear_logs(self) -> None:
        if not messagebox.askyesno(C.APP_NAME, "确定清空全部运行日志吗？该操作不可撤销。"):
            return
        n = self.db.clear_logs()
        self.app.set_status("已清空 {} 条日志".format(n))
        self.reload()

    def export_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出日志", initialdir=C.EXPORT_DIR,
            initialfile="红薯雷达日志_{}.csv".format(C.now().strftime("%Y%m%d_%H%M%S")),
            defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            rows = self.db.query_logs(self._level_filter(), limit=200000)
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["时间", "级别", "来源", "商品", "商品ID", "摘要", "完整详情"])
                for r in rows:
                    w.writerow([r.get("ts"), r.get("level"), r.get("source"),
                                r.get("title"), r.get("product_id"),
                                r.get("message"), r.get("detail")])
            self.db.log("SUCCESS", "日志", "日志已导出：{}".format(path))
            messagebox.showinfo(C.APP_NAME, "已导出 {} 条日志到：\n{}".format(len(rows), path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(C.APP_NAME, "导出失败：{}".format(str(exc)[:200]))


# --------------------------------------------------------------------------
# 弹窗
# --------------------------------------------------------------------------

class _Modal(tk.Toplevel):
    def __init__(self, app: RadarApp, title: str, size: str = "520x420"):
        super().__init__(app)
        self.app = app
        self.title(title)
        self.geometry(size)
        self.configure(bg=BG)
        self.transient(app)
        self.grab_set()
        self.resizable(True, True)
        try:
            self.iconbitmap(C.icon_path())
        except Exception:  # noqa: BLE001
            pass
        self.bind("<Escape>", lambda e: self.destroy())


class SingleAddDialog(_Modal):
    """单个添加前的确认弹窗：展示名称、店铺、实际价格、累计已售。"""

    def __init__(self, app: RadarApp, info: CO.ProductInfo):
        super().__init__(app, "确认添加商品", "640x470")
        self.result = False

        tk.Label(self, text="请确认商品信息", bg=BG, fg=TEXT,
                 font=(FONT_FAMILY, 12, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
        body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True, padx=16)

        self.img_label = tk.Label(body, text="图片加载中…", bg="#F0F2F5", fg=SUB,
                                  font=(FONT_FAMILY, 9), width=20, height=10)
        self.img_label.pack(side="left", padx=(0, 14), pady=12)
        if info.cover:
            load_image_async(app, info.cover, self.img_label, (200, 200))

        info_box = tk.Frame(body, bg=CARD)
        info_box.pack(side="left", fill="both", expand=True, pady=12)
        rows = [
            ("商品名称", info.title or A.DASH),
            ("店铺", info.shop_name or A.DASH),
            ("商品 ID", info.id),
            ("实际价格", fmt_price(info.price)),
            ("累计已售", fmt_num(info.sold)),
            ("店铺累计销量", fmt_num(info.shop_sold)),
            ("粉丝数", fmt_num(info.fans)),
            ("可配送", {1: "可配送", 0: "不可配送"}.get(info.deliverable, A.DASH)),
        ]
        for i, (k, v) in enumerate(rows):
            tk.Label(info_box, text=k, bg=CARD, fg=SUB, font=(FONT_FAMILY, 9),
                     width=12, anchor="w").grid(row=i, column=0, sticky="w", pady=3)
            tk.Label(info_box, text=str(v)[:80], bg=CARD, fg=TEXT,
                     font=(FONT_FAMILY, 10), anchor="w", justify="left",
                     wraplength=310).grid(row=i, column=1, sticky="w", pady=3)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=12)
        ttk.Button(bar, text="加入监控", style="Accent.TButton",
                   command=self._yes).pack(side="right")
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=8)

    def _yes(self) -> None:
        self.result = True
        self.destroy()


class ProductDetailDialog(_Modal):
    """单品数据弹窗：主图 + 关键指标 + 逐时/多日折线图 + 明细。"""

    def __init__(self, app: RadarApp, row: dict):
        super().__init__(app,
                         "单品数据 — {}".format((row.get("title") or row["id"])[:40]),
                         "980x740")
        self.row = row
        self._date = tk.StringVar(value=C.now().strftime(C.DAY_FMT))
        self._build()
        self.load_hourly()

    def _build(self) -> None:
        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x")
        self.img_label = tk.Label(head, text="图片加载中…", bg="#F0F2F5", fg=SUB,
                                  font=(FONT_FAMILY, 9), width=16, height=8)
        self.img_label.pack(side="left", padx=12, pady=10)
        if self.row.get("cover"):
            load_image_async(self.app, self.row["cover"], self.img_label, (150, 150))

        info = tk.Frame(head, bg=CARD)
        info.pack(side="left", fill="both", expand=True, pady=10)
        tk.Label(info, text=(self.row.get("title") or self.row["id"])[:70], bg=CARD,
                 fg=TEXT, font=(FONT_FAMILY, 11, "bold"), anchor="w", wraplength=700,
                 justify="left").pack(anchor="w")
        tk.Label(info, text="店铺：{}　|　商品 ID：{}　|　状态：{}".format(
            self.row.get("shop_name") or A.DASH, self.row["id"],
            self.row.get("status_text") or A.DASH), bg=CARD, fg=SUB,
            font=(FONT_FAMILY, 9), anchor="w").pack(anchor="w", pady=(2, 6))

        metrics = tk.Frame(info, bg=CARD)
        metrics.pack(anchor="w")
        for label, value, color in (
                ("当前价格", fmt_price(self.row.get("price")), TEXT),
                ("累计已售", fmt_num(self.row.get("total_sold")), TEXT),
                ("今日销量", fmt_num(self.row.get("today")), ACCENT_DARK),
                ("昨日销量", fmt_num(self.row.get("yesterday")), TEXT),
                ("上小时销量", fmt_num(self.row.get("last_hour")), TEXT)):
            cell = tk.Frame(metrics, bg=CARD)
            cell.pack(side="left", padx=(0, 20))
            tk.Label(cell, text=label, bg=CARD, fg=SUB,
                     font=(FONT_FAMILY, 8)).pack(anchor="w")
            tk.Label(cell, text=value, bg=CARD, fg=color,
                     font=(FONT_FAMILY, 12, "bold")).pack(anchor="w")

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=8)
        tk.Label(bar, text="日期", bg=BG, fg=SUB, font=(FONT_FAMILY, 9)).pack(side="left")
        ttk.Entry(bar, textvariable=self._date, width=13).pack(side="left", padx=6)
        ttk.Button(bar, text="查看该日逐时", command=self.load_hourly).pack(side="left")
        ttk.Button(bar, text="近7天", command=lambda: self.load_daily(7)).pack(
            side="left", padx=6)
        ttk.Button(bar, text="近10天", command=lambda: self.load_daily(10)).pack(side="left")
        self.lbl_state = tk.Label(bar, text="", bg=BG, fg=SUB, font=(FONT_FAMILY, 9))
        self.lbl_state.pack(side="left", padx=12)

        self.chart = LineChart(self, height=225)
        self.chart.configure(bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self.chart.pack(fill="x", padx=12)

        tk.Label(self, text="销量明细（模糊销售额 = 销量增量 × 当时实际价格，{}）".format(
            A.FUZZY_NOTE), bg=BG, fg=SUB, font=(FONT_FAMILY, 9)).pack(
            anchor="w", padx=12, pady=(8, 2))
        wrap, self.tv = make_tree(
            self, [("time", "时间", 200), ("sales", "销量增量", 120),
                   ("price", "当时价格", 120), ("amount", "模糊销售额", 140)],
            height=9, selectmode="browse")
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def load_hourly(self) -> None:
        day = (self._date.get() or "").strip() or C.now().strftime(C.DAY_FMT)
        try:
            series = self.app.an.hourly_series(self.row["id"], day)
        except Exception as exc:  # noqa: BLE001
            self.lbl_state.config(text="加载失败：{}".format(str(exc)[:100]))
            return
        self.chart.set_data([(s["label"], s["sales"]) for s in series],
                            "{} 每小时销量".format(day))
        self._fill_detail([(s["label"], s["sales"], s["price"], s["amount"])
                           for s in series])
        known = sum(1 for s in series if s["sales"] is not None)
        self.lbl_state.config(text="已加载 {} 小时数据（共 24 小时）".format(known))

    def load_daily(self, days: int) -> None:
        try:
            series = self.app.an.daily_series(self.row["id"], days)
        except Exception as exc:  # noqa: BLE001
            self.lbl_state.config(text="加载失败：{}".format(str(exc)[:100]))
            return
        self.chart.set_data([(s["label"], s["sales"]) for s in series],
                            "近 {} 天每日销量".format(days))
        self._fill_detail([(s["date"], s["sales"], s["price"], s["amount"])
                           for s in series])
        self.lbl_state.config(text="已加载近 {} 天数据".format(days))

    def _fill_detail(self, rows: list[tuple]) -> None:
        for i in self.tv.get_children(""):
            self.tv.delete(i)
        for i, (label, sales, price, amount) in enumerate(rows):
            self.tv.insert("", "end", tags=("odd" if i % 2 else "even",), values=(
                label, fmt_num(sales), fmt_price(price),
                A.DASH if amount is None else "¥{:.2f}".format(amount)))


class ShopPickerDialog(_Modal):
    def __init__(self, app: RadarApp, shops: list[str], selected: list[str],
                 on_save: Callable[[list[str]], None]):
        super().__init__(app, "设置通知店铺", "540x580")
        self.on_save = on_save
        self.vars: dict[str, tk.BooleanVar] = {}
        self._search = tk.StringVar()
        self._search.trace_add("write", lambda *_: self._filter())

        tk.Label(self, text="勾选的店铺才会发送销量时报；全部不勾选表示通知全部店铺。",
                 bg=BG, fg=SUB, font=(FONT_FAMILY, 9), wraplength=470,
                 justify="left").pack(anchor="w", padx=14, pady=(12, 6))
        ttk.Entry(self, textvariable=self._search).pack(fill="x", padx=14)
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=14, pady=6)
        ttk.Button(bar, text="全选", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(bar, text="取消全选", command=lambda: self._set_all(False)).pack(
            side="left", padx=6)

        wrap = tk.Frame(self, bg=CARD)
        wrap.pack(fill="both", expand=True, padx=14)
        canvas = tk.Canvas(wrap, bg=CARD, highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(canvas, bg=CARD)
        canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        sel = set(selected)
        for s in shops:
            self.vars[s] = tk.BooleanVar(value=(s in sel))
        self._filter()

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=14, pady=12)
        ttk.Button(foot, text="保存", style="Accent.TButton",
                   command=self._save).pack(side="right")
        ttk.Button(foot, text="取消", command=self.destroy).pack(side="right", padx=8)

    def _visible(self, name: str) -> bool:
        kw = (self._search.get() or "").strip().lower()
        return (not kw) or (kw in name.lower())

    def _set_all(self, value: bool) -> None:
        for name, v in self.vars.items():
            if self._visible(name):
                v.set(value)

    def _filter(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()
        for name, v in self.vars.items():
            if self._visible(name):
                ttk.Checkbutton(self.inner, text=name, variable=v).pack(
                    anchor="w", padx=8, pady=3)

    def _save(self) -> None:
        self.on_save([name for name, v in self.vars.items() if v.get()])
        self.destroy()


class LogDetailDialog(_Modal):
    def __init__(self, app: RadarApp, row: dict):
        super().__init__(app, "日志详情", "840x620")
        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="{}　[{}]　{}".format(
            row.get("ts") or "", row.get("level") or "", row.get("source") or ""),
            bg=CARD, fg=TEXT, font=(FONT_FAMILY, 10, "bold"), anchor="w").pack(
            anchor="w", padx=14, pady=(12, 2))
        tk.Label(head, text="商品：{}　|　ID：{}".format(
            row.get("title") or A.DASH, row.get("product_id") or A.DASH),
            bg=CARD, fg=SUB, font=(FONT_FAMILY, 9), anchor="w").pack(anchor="w", padx=14)
        tk.Label(self, text=row.get("message") or "", bg=BG, fg=TEXT,
                 font=(FONT_FAMILY, 10), wraplength=780, justify="left",
                 anchor="w").pack(fill="x", padx=14, pady=10)

        tk.Label(self, text="完整详情", bg=BG, fg=SUB,
                 font=(FONT_FAMILY, 9)).pack(anchor="w", padx=14)
        self.txt = tk.Text(self, font=("Consolas", 9), wrap="none", relief="solid",
                           bd=1, bg="#FAFBFC", fg=TEXT)
        xsb = ttk.Scrollbar(self, orient="horizontal", command=self.txt.xview)
        self.txt.configure(xscrollcommand=xsb.set)
        self.txt.pack(fill="both", expand=True, padx=14, pady=(2, 4))
        xsb.pack(fill="x", padx=14)
        self.txt.insert("1.0", row.get("detail") or "（无额外详情）")
        self.txt.config(state="disabled")

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=14, pady=12)
        ttk.Button(bar, text="复制全部内容", style="Accent.TButton",
                   command=self._copy).pack(side="right")
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right", padx=8)

    def _copy(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(self.txt.get("1.0", "end"))
            self.app.set_status("已复制日志详情")
        except Exception:  # noqa: BLE001
            pass


class DedupDialog(_Modal):
    """智能去重预览：每组保留价格最高的商品，其余候选二次确认后删除。"""

    def __init__(self, app: RadarApp, plan: list[dict], on_done: Callable[[], None]):
        super().__init__(app, "智能去重预览", "920x620")
        self.plan = plan
        self.on_done = on_done

        tk.Label(self, text="共发现 {} 组重复商品。每组只保留价格最高的商品，"
                            "其余将在你确认后删除（含历史数据，之后不再采集）。".format(
            len(plan)), bg=BG, fg=TEXT, font=(FONT_FAMILY, 10), wraplength=860,
            justify="left").pack(anchor="w", padx=14, pady=(12, 6))

        cols = [("keep", "保留（价格最高）", 270), ("drop", "将删除", 270),
                ("shop", "店铺", 150), ("sold", "累计已售", 95), ("hour", "上小时", 85)]
        wrap, self.tv = make_tree(self, cols, height=16, selectmode="none")
        wrap.pack(fill="both", expand=True, padx=14)
        for i, item in enumerate(plan):
            k, d = item["keep"], item["drop"]
            self.tv.insert("", "end", tags=("odd" if i % 2 else "even",), values=(
                "{} ({})".format((k["title"] or k["id"])[:26], fmt_price(k["price"])),
                "{} ({})".format((d["title"] or d["id"])[:26], fmt_price(d["price"])),
                (k["shop_name"] or A.DASH)[:18],
                fmt_num(k["total_sold"]), fmt_num(k["last_hour"]),
            ))
        bind_sort(self.tv, [c[0] for c in cols])

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=14, pady=12)
        ttk.Button(foot, text="确认删除 {} 个重复商品".format(len(plan)),
                   style="Danger.TButton", command=self._apply).pack(side="right")
        ttk.Button(foot, text="取消", command=self.destroy).pack(side="right", padx=8)

    def _apply(self) -> None:
        if not messagebox.askyesno(
                C.APP_NAME,
                "⚠️ 此操作非常危险，可能导致不可逆的数据丢失！\n\n"
                "将删除 {} 个商品及其全部历史快照，之后不会再采集。\n"
                "每组保留价格最高的那一个。\n\n确定继续吗？".format(len(self.plan)),
                icon="warning", default="no"):
            return
        db = self.app.db
        for item in self.plan:
            db.delete_product(item["drop"]["id"])
        db.log("WARN", "去重", "智能去重删除 {} 个重复商品".format(len(self.plan)),
               detail="\n".join("{} → 保留 {}".format(i["drop"]["id"], i["keep"]["id"])
                                for i in self.plan[:50]))
        self.destroy()
        self.on_done()
        self.app.refresh_all()
        messagebox.showinfo(C.APP_NAME, "已删除 {} 个重复商品。".format(len(self.plan)))


class ExpandDialog(_Modal):
    """自动拓品结果选择窗口。"""

    def __init__(self, app: RadarApp, items: list[dict], on_done: Callable[[], None]):
        super().__init__(app, "自动拓品 — 选择要加入监控的商品", "900x620")
        self.items = items
        self.on_done = on_done
        self.vars: list[tk.BooleanVar] = []

        tk.Label(self, text="发现 {} 个未监控的商品，勾选后批量加入监控：".format(len(items)),
                 bg=BG, fg=TEXT, font=(FONT_FAMILY, 10)).pack(anchor="w", padx=14,
                                                              pady=(12, 6))
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=14)
        ttk.Button(bar, text="全选", command=lambda: self._all(True)).pack(side="left")
        ttk.Button(bar, text="取消全选", command=lambda: self._all(False)).pack(
            side="left", padx=6)

        cols = [("check", "选择", 60), ("title", "标题", 300), ("shop", "店铺", 150),
                ("sold", "销量", 90), ("price", "价格", 90), ("id", "商品 ID", 200)]
        wrap, self.tv = make_tree(self, cols, height=18, selectmode="none")
        wrap.pack(fill="both", expand=True, padx=14, pady=8)
        for i, it in enumerate(items):
            v = tk.BooleanVar(value=True)
            self.vars.append(v)
            self.tv.insert("", "end", iid=it["id"], tags=("odd" if i % 2 else "even",),
                           values=("☑" if v.get() else "☐",
                                   (it.get("title") or it["id"])[:40],
                                   (it.get("shop_name") or A.DASH)[:18],
                                   fmt_num(it.get("sold")), fmt_price(it.get("price")),
                                   it["id"]))
        self.tv.bind("<Button-1>", self._toggle)

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=14, pady=12)
        ttk.Button(foot, text="批量加入监控", style="Accent.TButton",
                   command=self._add).pack(side="right")
        ttk.Button(foot, text="取消", command=self.destroy).pack(side="right", padx=8)

    def _toggle(self, event) -> None:
        item = self.tv.identify_row(event.y)
        if not item:
            return
        idx = self.tv.index(item)
        if idx >= len(self.vars):
            return
        v = self.vars[idx]
        v.set(not v.get())
        vals = list(self.tv.item(item, "values"))
        vals[0] = "☑" if v.get() else "☐"
        self.tv.item(item, values=vals)

    def _all(self, value: bool) -> None:
        for i, item in enumerate(self.tv.get_children("")):
            if i < len(self.vars):
                self.vars[i].set(value)
            vals = list(self.tv.item(item, "values"))
            vals[0] = "☑" if value else "☐"
            self.tv.item(item, values=vals)

    def _add(self) -> None:
        chosen = [self.items[i] for i, v in enumerate(self.vars) if v.get()]
        if not chosen:
            messagebox.showinfo(C.APP_NAME, "请至少选择一个商品。")
            return
        db = self.app.db
        added = 0
        for it in chosen:
            if db.product_exists(it["id"]) or db.is_deleted(it["id"]):
                continue
            db.add_product(it["id"], it.get("title") or "", it.get("shop_name") or "",
                           it.get("shop_id") or "", it.get("cover") or "", source="expand")
            db.log("SUCCESS", "拓品", "自动拓品加入商品：{}".format(
                (it.get("title") or it["id"])[:40]), product_id=it["id"],
                title=it.get("title") or "")
            added += 1
        db.log("SUCCESS", "拓品", "自动拓品完成，加入 {} 个商品".format(added))
        self.destroy()
        self.on_done()
        self.app.ctx.post("refresh_usage")
        messagebox.showinfo(C.APP_NAME, "已加入 {} 个商品，将在下一轮采集。".format(added))




