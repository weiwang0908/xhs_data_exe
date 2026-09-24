# -*- coding: utf-8 -*-
"""红薯雷达 —— 全局配置层。

集中管理：文件路径、时区、常量、默认设置、跨平台辅助函数。
所有其它模块都从这里取路径与时间口径，避免散落的硬编码。
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys

try:  # Python 3.9+ 标准库
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - 极端环境兜底
    ZoneInfo = None  # type: ignore

# --------------------------------------------------------------------------
# 应用元信息
# --------------------------------------------------------------------------

APP_NAME = "红薯雷达"
APP_NAME_EN = "RedPotatoRadar"
APP_VERSION = "1.0.0"
AUTHOR = "冬青"
AUTHOR_URL = "https://scys.com/personal/3941891?number=201255&tab=posts"
DISCLAIMER = "仅供学习交流，严禁用于商业用途，请于24小时内删除"


def _build_timezone():
    """固定使用 Asia/Shanghai，不随本机时区漂移。"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Asia/Shanghai")
        except Exception:
            pass
    try:  # tzdata 缺失时退回到固定偏移（上海无夏令时，+08:00 恒定）
        return _dt.timezone(_dt.timedelta(hours=8), "Asia/Shanghai")
    except Exception:  # pragma: no cover
        return _dt.timezone.utc


TZ = _build_timezone()
TIME_FMT = "%Y-%m-%d %H:%M:%S"
DAY_FMT = "%Y-%m-%d"


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def app_dir() -> str:
    """返回程序所在目录。

    打包为单文件 EXE 后，__file__ 指向临时解包目录，必须改用 sys.executable，
    否则数据库会被写进临时目录、退出即丢失。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_path(*parts: str) -> str:
    """读取被打包进 EXE 的只读资源（图标等）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, *parts)
    return os.path.join(app_dir(), *parts)


APP_DIR = app_dir()
DB_PATH = os.path.join(APP_DIR, "monitor.db")
DATA_DIR = os.path.join(APP_DIR, "data")
BROWSER_PROFILE_DIR = os.path.join(DATA_DIR, "browser_profile")
TEMP_PROFILE_DIR = os.path.join(DATA_DIR, "temp_profiles")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# MCP 打包后的文件名（打包脚本与设置页文案共用）
MCP_EXE_NAME = "红薯雷达MCP.exe"
MCP_SCRIPT_NAME = "mcp_server.py"
MAIN_EXE_NAME = "红薯雷达.exe"


def ensure_dirs() -> None:
    for d in (DATA_DIR, BROWSER_PROFILE_DIR, TEMP_PROFILE_DIR, EXPORT_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def icon_path() -> str:
    """窗口/任务栏图标：优先打包内资源，其次源码目录。"""
    for candidate in (
        resource_path("assets", "app-icon.ico"),
        os.path.join(ASSETS_DIR, "app-icon.ico"),
        os.path.join(APP_DIR, "assets", "app-icon.ico"),
    ):
        if os.path.exists(candidate):
            return candidate
    return ""


# --------------------------------------------------------------------------
# 时间工具
# --------------------------------------------------------------------------

def now() -> _dt.datetime:
    """当前上海时间（带时区）。"""
    return _dt.datetime.now(TZ)


def now_str() -> str:
    return now().strftime(TIME_FMT)


def to_dt(value) -> _dt.datetime | None:
    """把数据库中的时间字符串/时间戳还原为带时区的 datetime。"""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ")
    for fmt in (TIME_FMT, "%Y-%m-%d %H:%M", DAY_FMT):
        try:
            return _dt.datetime.strptime(text[: len(fmt) + 2].strip()[:19], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    for fmt in (TIME_FMT, "%Y-%m-%d %H:%M", DAY_FMT):
        try:
            return _dt.datetime.strptime(text[:19], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def fmt(dt: _dt.datetime | None) -> str:
    return dt.strftime(TIME_FMT) if dt else ""


def day_start(dt: _dt.datetime) -> _dt.datetime:
    """当天 00:00:00。"""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def hour_start(dt: _dt.datetime) -> _dt.datetime:
    """所在整点。"""
    return dt.replace(minute=0, second=0, microsecond=0)


def add_days(dt: _dt.datetime, n: int) -> _dt.datetime:
    return dt + _dt.timedelta(days=n)


def add_hours(dt: _dt.datetime, n: int) -> _dt.datetime:
    return dt + _dt.timedelta(hours=n)


# --------------------------------------------------------------------------
# 默认设置
# --------------------------------------------------------------------------

DEFAULT_SETTINGS: dict[str, str] = {
    "collect_interval_minutes": "60",   # 采集周期（分钟）
    "min_gap_seconds": "0.1",           # 商品间隔最小值
    "max_gap_seconds": "0.3",           # 商品间隔最大值
    "wecom_webhook": "",                # 企业微信机器人 Webhook
    "wecom_enabled": "0",               # 是否启用通知
    "wecom_shops": "[]",                # 需要通知的店铺名 JSON 列表
    "snapshot_keep_days": "90",         # 快照保留天数
    "log_keep_days": "30",              # 日志保留天数
    "sold_limit": "10000",              # 累计已售监控上限
    "browser_mode": "auto",             # auto | channel | chromium
    "last_round_at": "",                # 上一轮采集归属整点
    "mcp_last_hint": "",                # 设置页展示用
}

# 采集常量
API_URL_TMPL = (
    "https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc"
    "?version=0.0.5&item_id={item_id}"
)
GOODS_PAGE_TMPL = "https://www.xiaohongshu.com/goods-detail/{item_id}"
SHOP_PAGE_TMPL = "https://www.xiaohongshu.com/user/profile/{shop_id}"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.xiaohongshu.com/",
    "Origin": "https://www.xiaohongshu.com",
}

HTTP_TIMEOUT = 15          # 单次 HTTP 请求超时秒数
IMAGE_TIMEOUT = 12         # 图片下载超时秒数
WECOM_TIMEOUT = 10         # 企业微信推送超时秒数

RETRY_WAIT_MIN = 5         # 整轮结束后统一重试前的等待
RETRY_WAIT_MAX = 15
RESTRICT_COOLDOWN_MIN = 15  # 461 熔断冷却分钟
RESTRICT_THRESHOLD = 3      # 单轮 461 触发熔断的阈值

MAX_SOLD_TO_MONITOR = 10000  # 累计已售超过该值不监控

LOG_PAGE_SIZE = 1000       # 日志界面单次加载上限


# --------------------------------------------------------------------------
# 子进程 / 浏览器
# --------------------------------------------------------------------------

# Windows: 避免采集时反复弹出黑色控制台窗口
CREATE_NO_WINDOW = 0x08000000


def popen_kwargs() -> dict:
    """返回抑制黑框的子进程参数（非 Windows 返回空字典）。"""
    if os.name != "nt":
        return {}
    kwargs: dict = {"creationflags": CREATE_NO_WINDOW}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
    except Exception:
        pass
    return kwargs


def chrome_candidates() -> list[dict]:
    """按 Chrome → Edge → Chromium 顺序返回可用的浏览器通道。"""
    return [
        {
            "key": "chrome",
            "label": "Google Chrome",
            "channel": "chrome",
            "paths": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ],
        },
        {
            "key": "msedge",
            "label": "Microsoft Edge",
            "channel": "msedge",
            "paths": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ],
        },
    ]


def detect_browser() -> dict:
    """探测本机可用浏览器，返回 {key,label,channel,path} 或 chromium 兜底。"""
    for item in chrome_candidates():
        for p in item["paths"]:
            if p and os.path.exists(p):
                return {
                    "key": item["key"],
                    "label": item["label"],
                    "channel": item["channel"],
                    "path": p,
                }
    return {"key": "chromium", "label": "Playwright Chromium", "channel": None, "path": ""}
