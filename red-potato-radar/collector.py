# -*- coding: utf-8 -*-
"""红薯雷达 —— 采集层。

三级兜底，严格按商品逐个执行（不是"全部走完第一级再走第二级"）：
  1. 轻量接口直采（urllib / curl.exe）
  2. 静默浏览器（Playwright 持久化 Context，headless，整轮复用）
  3. 可见浏览器（每次新建临时 Profile，作为最后兜底）

异常严格区分三类：临时失败 / 风控(461) / 明确下架。
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Optional

import config as C
import parser as P

# --------------------------------------------------------------------------
# 异常体系
# --------------------------------------------------------------------------

class CollectError(Exception):
    kind = "temp"
    delist_reason = ""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


class TempError(CollectError):
    """HTTP 500 / 超时 / 连接重置 / DNS 抖动 / 执行上下文销毁 —— 可重试。"""
    kind = "temp"


class RestrictedError(CollectError):
    """HTTP 461 / 页面前置校验 —— 请求环境受限，需要冷却。"""
    kind = "restricted"
    code = 461


class IncompleteError(TempError):
    """返回体不完整、JSON 解析失败、关键元素缺失 —— 绝不能用 0 顶替。"""
    kind = "temp"


class DelistedError(CollectError):
    kind = "delisted"
    delist_reason = "商品已下架"


class ViolationError(DelistedError):
    kind = "delisted"
    delist_reason = "违规下架"


class BrowserUnavailable(TempError):
    kind = "temp"


# --------------------------------------------------------------------------
# 字段解析
# --------------------------------------------------------------------------

RE_SOLD_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*(万|w|W|千|k|K)?")


def parse_sold_text(text: Any) -> Optional[int]:
    """解析「已售123」「1.2万」「1.2w」「已售1.2万+」等。无法解析返回 None。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        try:
            return int(text)
        except (TypeError, ValueError):
            return None
    s = str(text).strip()
    if not s:
        return None
    m = RE_SOLD_NUM.search(s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except (TypeError, ValueError):
        return None
    unit = m.group(2)
    if unit in ("万", "w", "W"):
        val *= 10000
    elif unit in ("千", "k", "K"):
        val *= 1000
    return int(round(val))


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return parse_sold_text(value)


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"(\d+(?:\.\d+)?)", str(value).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return default if cur is None else cur


def _norm_cover(url: Any) -> str:
    if not url:
        return ""
    s = str(url).strip()
    if s.startswith("//"):
        return "https:" + s
    if s.startswith("http://"):
        return "https://" + s[len("http://"):]
    return s


# 明确下架 / 违规的业务特征
DELIST_MARKERS = (
    "unBuyableGoShop",
    "已下架，进店逛逛",
    "当前商品已下架",
    "商品已下架",
    "商品不存在",
    "商品已失效",
    "该商品已下架",
)
VIOLATION_MARKERS = (
    "当前商品违规，无法展示",
    "商品违规",
    "item freeze",
    "itemFreeze",
    "item_freeze",
    '"freeze":true',
    "已冻结",
)
RESTRICT_MARKERS = (
    "访问频繁",
    "请稍后再试",
    "security check",
    "verify",
    "461",
    "滑块验证",
)


def detect_status_from_text(text: str) -> Optional[str]:
    """从 JSON / 页面文本里识别明确下架或违规，返回 'delisted' / 'violation' / None。

    注意：网络错误、空白页、超时**不**能判定下架，只有明确业务特征才算。
    """
    if not text:
        return None
    for mk in VIOLATION_MARKERS:
        if mk in text:
            return "violation"
    for mk in DELIST_MARKERS:
        if mk in text:
            return "delisted"
    return None


def looks_restricted(text: str) -> bool:
    if not text:
        return False
    hits = sum(1 for mk in RESTRICT_MARKERS if mk in text)
    return hits >= 2


_ITEM_KEY_RE = re.compile(
    r'"(?:itemId|item_id|skuId|sku_id)"\s*:\s*"([0-9a-fA-F]{24})"')
_TITLE_KEY_RE = re.compile(r'"(?:title|name)"\s*:\s*"([^"]{2,80})"')
_PRICE_KEY_RE = re.compile(r'"(?:price|dealPrice|salePrice)"\s*:\s*"?(\d+(?:\.\d+)?)')
_SOLD_KEY_RE = re.compile(r'"(?:soldCount|salesVolume|sold)"\s*:\s*"?(\d+(?:\.\d+)?)')


def _extract_shop_items(text: str) -> list[dict]:
    """尽力从店铺页面文本中抽取商品条目。取不到就返回空列表。"""
    if not text:
        return []
    found: dict[str, dict] = {}
    for m in _ITEM_KEY_RE.finditer(text):
        pid = normalize_id_hex(m.group(1))
        if not pid or pid in found:
            continue
        # 以匹配位置为锚，就近取标题/价格/销量
        window = text[max(0, m.start() - 400): m.start() + 400]
        title = ""
        tm = _TITLE_KEY_RE.search(window)
        if tm:
            title = tm.group(1)
        pm = _PRICE_KEY_RE.search(window)
        sm = _SOLD_KEY_RE.search(window)
        found[pid] = {
            "id": pid,
            "title": title,
            "shop_id": "",
            "shop_name": "",
            "cover": "",
            "price": float(pm.group(1)) if pm else None,
            "sold": parse_sold_text(sm.group(1)) if sm else None,
        }
    return list(found.values())


def normalize_id_hex(raw: str) -> str:
    s = (raw or "").strip().lower()
    return s if re.fullmatch(r"[0-9a-f]{24}", s) else ""


# --------------------------------------------------------------------------
# 商品信息
# --------------------------------------------------------------------------

class ProductInfo:
    __slots__ = ("id", "title", "shop_name", "shop_id", "cover", "sold",
                 "shop_sold", "price", "fans", "stock_status", "deliverable")

    def __init__(self, pid: str = "", **kw: Any):
        self.id = pid
        self.title = kw.get("title") or ""
        self.shop_name = kw.get("shop_name") or ""
        self.shop_id = kw.get("shop_id") or ""
        self.cover = kw.get("cover") or ""
        self.sold = kw.get("sold")
        self.shop_sold = kw.get("shop_sold")
        self.price = kw.get("price")
        self.fans = kw.get("fans")
        self.stock_status = kw.get("stock_status")
        self.deliverable = kw.get("deliverable")

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def parse_detail_payload(payload: Any, item_id: str) -> ProductInfo:
    """从商品接口 JSON 解析字段。

    只有当 success=true 且 template_data 完整存在时，空的已售字段才解释为真实 0；
    否则一律抛 IncompleteError，绝不保存假快照。
    """
    if not isinstance(payload, dict):
        raise IncompleteError("返回体不是合法 JSON 对象")

    compact = json.dumps(payload, ensure_ascii=False)[:400000]
    status = detect_status_from_text(compact)
    if status == "violation":
        raise ViolationError("当前商品违规，无法展示")
    if status == "delisted":
        raise DelistedError("商品已下架")

    if payload.get("success") is not True:
        code = payload.get("error_code")
        if code is None:
            code = payload.get("code")
        msg = str(payload.get("msg") or payload.get("message") or "")
        low = msg.lower()
        # 接口明确返回「商品不存在」类业务错误 —— 属于明确下架，而不是临时失败。
        # 这与网络错误 / 超时 / 空白页有本质区别：它是服务端给出的确定业务结论。
        if str(code) == "602" or "item not found" in low or "item_not_found" in low \
                or "商品不存在" in msg or "商品已下架" in msg or "已下架" in msg \
                or "商品已失效" in msg or "已失效" in msg:
            raise DelistedError("商品已下架（{}）".format(msg or code))
        if looks_restricted(compact):
            raise RestrictedError("接口前置校验未通过", detail=msg or compact[:400])
        raise IncompleteError(
            "接口 success 非 true（code={} msg={}）".format(code, msg[:120]),
            detail=compact[:400],
        )

    data = payload.get("data")
    template = _get(data, "template_data")
    if not isinstance(template, list) or not template:
        raise IncompleteError("template_data 缺失或不完整", detail=compact[:400])

    node = template[0]
    if not isinstance(node, dict):
        raise IncompleteError("template_data[0] 结构异常")

    title = (_get(node, "descriptionMain", "name")
             or _get(node, "descriptionH5", "name") or "")
    shop_name = _get(node, "sellerH5", "name") or ""
    shop_id = str(_get(node, "sellerH5", "id") or "")

    cover = ""
    images = _get(node, "carouselH5", "images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            cover = _norm_cover(first.get("url"))
        elif isinstance(first, str):
            cover = _norm_cover(first)

    sold_raw = _get(node, "priceH5", "itemAnalysisDataText")
    sold = parse_sold_text(sold_raw)
    if sold is None:
        # 接口明确成功且结构完整时，才允许空字段 = 真实 0
        if sold_raw is None or str(sold_raw).strip() == "":
            sold = 0
        else:
            raise IncompleteError(
                "累计已售字段无法解析：{!r}".format(str(sold_raw)[:60]),
                detail=compact[:400],
            )

    shop_sold = _to_int(_get(node, "sellerH5", "salesVolume"))

    price = _to_float(_get(node, "priceH5", "dealPrice", "price"))
    if price is None:
        price = _to_float(_get(node, "bottomBarMainH5", "dealPrice", "price"))
    if price is None:
        price = _to_float(_get(node, "priceH5", "highlightPrice"))
    # 绝不能拿优惠前原价覆盖实际到手价

    fans = _to_int(_get(node, "profitBarPopupH5", "follow", "fansNum"))
    if fans is None:
        fans = _to_int(_get(node, "sellerH5", "fansAmount"))

    stock_status = _to_int(_get(node, "carouselH5", "stockStatus"))
    deliverable_raw = _get(node, "bottomBarMainH5", "deliveryInfo", "ableToDelivery")
    deliverable = None if deliverable_raw is None else (1 if deliverable_raw else 0)

    info = ProductInfo(
        item_id, title=str(title).strip(), shop_name=str(shop_name).strip(),
        shop_id=shop_id, cover=cover, sold=sold, shop_sold=shop_sold, price=price,
        fans=fans, stock_status=stock_status, deliverable=deliverable,
    )
    return info


# --------------------------------------------------------------------------
# 第一级：轻量接口
# --------------------------------------------------------------------------

def _classify_http(code: int, body: str = "") -> CollectError:
    if code == 461:
        return RestrictedError("HTTP 461 请求环境受限", detail=body[:400])
    if code in (500, 502, 503, 504, 408, 429):
        return TempError("HTTP {}".format(code), detail=body[:400])
    if code in (403, 401):
        return RestrictedError("HTTP {} 被拒绝".format(code), detail=body[:400])
    if code == 404:
        return TempError("HTTP 404", detail=body[:400])
    return TempError("HTTP {}".format(code), detail=body[:400])


def fetch_api_raw(item_id: str, timeout: int = C.HTTP_TIMEOUT,
                  opener: urllib.request.OpenerDirector | None = None) -> str:
    url = C.API_URL_TMPL.format(item_id=item_id)
    req = urllib.request.Request(url, headers=dict(C.DEFAULT_HEADERS), method="GET")
    try:
        if opener is None:
            resp = urllib.request.urlopen(req, timeout=timeout)
        else:
            resp = opener.open(req, timeout=timeout)
        with resp:
            body = resp.read() or b""
            return body.decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = (exc.read() or b"").decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            pass
        raise _classify_http(exc.code, body) from None
    except urllib.error.URLError as exc:
        raise TempError("连接失败：{}".format(str(exc.reason)[:120])) from None
    except TimeoutError:
        raise TempError("请求超时") from None
    except ConnectionResetError:
        raise TempError("连接被重置") from None
    except Exception as exc:  # noqa: BLE001
        raise TempError("{}: {}".format(type(exc).__name__, str(exc)[:150])) from None


def fetch_via_curl(item_id: str, timeout: int = C.HTTP_TIMEOUT) -> str:
    """curl.exe 补充请求方式（Windows 10+ 自带）。"""
    url = C.API_URL_TMPL.format(item_id=item_id)
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        raise TempError("系统未找到 curl")
    cmd = [curl, "-sS", "-L", "--max-time", str(timeout),
           "-H", "User-Agent: " + C.DEFAULT_HEADERS["User-Agent"],
           "-H", "Referer: " + C.DEFAULT_HEADERS["Referer"],
           "-H", "Accept: application/json, text/plain, */*",
           "-w", "\n__HTTP__%{http_code}", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5,
                              **C.popen_kwargs())
    except subprocess.TimeoutExpired:
        raise TempError("curl 请求超时") from None
    except Exception as exc:  # noqa: BLE001
        raise TempError("curl 执行失败：{}".format(str(exc)[:120])) from None
    out = (proc.stdout or b"").decode("utf-8", errors="ignore")
    code = 0
    m = re.search(r"__HTTP__(\d{3})\s*$", out)
    if m:
        code = int(m.group(1))
        out = out[:m.start()]
    if code and code != 200:
        raise _classify_http(code, out)
    if not out.strip():
        raise TempError("curl 返回空内容", detail=(proc.stderr or b"").decode("utf-8", "ignore")[:300])
    return out


def collect_via_api(item_id: str, allow_curl: bool = True) -> tuple[ProductInfo, str]:
    """第一级采集：轻量接口直采。"""
    text = fetch_api_raw(item_id)
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        if looks_restricted(text):
            raise RestrictedError("接口返回疑似风控页面") from None
        if allow_curl:
            text2 = fetch_via_curl(item_id)
            try:
                payload = json.loads(text2)
                text = text2
            except (ValueError, TypeError):
                raise IncompleteError("JSON 解析失败：{}".format(str(exc)[:120]),
                                      detail=text[:400]) from None
        else:
            raise IncompleteError("JSON 解析失败：{}".format(str(exc)[:120]),
                                  detail=text[:400]) from None
    return parse_detail_payload(payload, item_id), text


# --------------------------------------------------------------------------
# 浏览器层
# --------------------------------------------------------------------------

PLAYWRIGHT_HINT = ("浏览器兜底不可用。请安装最新版 Google Chrome 或 Microsoft Edge 后重试，"
                   "或执行：python -m playwright install chromium")


class _BrowserBase:
    def __init__(self) -> None:
        self.pw = None
        self.ctx = None
        self._user_dir: Optional[str] = None
        self._temp_dir = False

    def _launch_kwargs(self, headless: bool) -> dict:
        browser = C.detect_browser()
        kwargs: dict = {
            "headless": headless,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1366, "height": 900},
            "user_agent": C.DEFAULT_HEADERS["User-Agent"],
            "ignore_https_errors": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate,BackForwardCache,AcceptCHFrame",
            ],
        }
        if browser.get("channel"):
            kwargs["channel"] = browser["channel"]
        return kwargs, browser

    def start(self, headless: bool, user_dir: str, temp: bool = False) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserUnavailable(PLAYWRIGHT_HINT) from None
        os.makedirs(user_dir, exist_ok=True)
        self._user_dir = user_dir
        self._temp_dir = temp
        try:
            self.pw = sync_playwright().start()
            kwargs, browser = self._launch_kwargs(headless)
            self.ctx = self.pw.chromium.launch_persistent_context(user_dir, **kwargs)
            self.ctx.set_default_timeout(20000)
            self.ctx.set_default_navigation_timeout(25000)
            self._browser_label = browser.get("label", "")
        except BrowserUnavailable:
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001
            self.close()
            msg = str(exc)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                raise BrowserUnavailable(PLAYWRIGHT_HINT) from None
            raise BrowserUnavailable(
                "启动浏览器失败：{}。{}".format(msg[:180], PLAYWRIGHT_HINT), detail=msg[:600]
            ) from None

    @property
    def browser_label(self) -> str:
        return getattr(self, "_browser_label", "")

    def _fetch_in_page(self, page, item_id: str) -> tuple[str, int]:
        """在已建立会话的页面里，用页面自身的 fetch 请求接口，能带上 Cookie。"""
        api_url = C.API_URL_TMPL.format(item_id=item_id)
        try:
            result = page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url, {
                            credentials: 'include',
                            headers: {'Accept': 'application/json, text/plain, */*'}
                        });
                        const t = await r.text();
                        return {status: r.status, text: t};
                    } catch (e) {
                        return {status: -1, text: String(e)};
                    }
                }""",
                api_url,
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "Execution context was destroyed" in msg:
                raise TempError("Execution context was destroyed") from None
            if "Network.getResponseBody" in msg:
                raise TempError("Network.getResponseBody 失败") from None
            raise TempError("页面内请求失败：{}".format(msg[:150])) from None
        if not isinstance(result, dict):
            raise TempError("页面内请求返回异常")
        return str(result.get("text") or ""), int(result.get("status") or 0)

    def collect(self, item_id: str) -> ProductInfo:
        page = self.ctx.new_page()
        try:
            # 1) 直接打开接口
            api_url = C.API_URL_TMPL.format(item_id=item_id)
            text, status = "", 0
            try:
                resp = page.goto(api_url, wait_until="domcontentloaded", timeout=25000)
                status = resp.status if resp else 0
                text = self._page_body(page)
            except Exception as exc:  # noqa: BLE001
                if not self._is_transient(exc):
                    raise

            if status == 461 or (text and looks_restricted(text)):
                raise RestrictedError("浏览器请求被限制（461/校验）")

            info = self._try_parse(text, item_id)
            if info is not None:
                return info

            # 2) 接口数据不完整 —— 先打开详情页建立正常会话，再重新读接口
            goods_url = C.GOODS_PAGE_TMPL.format(item_id=item_id)
            try:
                page.goto(goods_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1800)
            except Exception as exc:  # noqa: BLE001
                if not self._is_transient(exc):
                    raise
            page_text = self._page_body(page)
            st = detect_status_from_text(page_text)
            if st == "violation":
                raise ViolationError("当前商品违规，无法展示")
            if st == "delisted":
                raise DelistedError("商品已下架")

            text2, status2 = self._fetch_in_page(page, item_id)
            if status2 == 461:
                raise RestrictedError("浏览器请求被限制（461）")
            if status2 and status2 >= 500:
                raise TempError("HTTP {}".format(status2))
            info = self._try_parse(text2, item_id)
            if info is not None:
                return info
            # 页面本身也可能直接给出下架提示
            st = detect_status_from_text(text2)
            if st == "violation":
                raise ViolationError("当前商品违规，无法展示")
            if st == "delisted":
                raise DelistedError("商品已下架")
            raise IncompleteError("浏览器采集未获得完整数据", detail=(text2 or page_text)[:400])
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        msg = str(exc)
        for key in ("Timeout", "timeout", "ERR_", "net::", "Target closed",
                    "Execution context was destroyed", "Navigation failed"):
            if key in msg:
                return True
        return False

    @staticmethod
    def _page_body(page) -> str:
        try:
            return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:  # noqa: BLE001
            try:
                return page.content() or ""
            except Exception:  # noqa: BLE001
                return ""

    @staticmethod
    def _try_parse(text: str, item_id: str) -> Optional[ProductInfo]:
        if not text:
            return None
        stripped = text.strip()
        if not stripped.startswith("{") and not stripped.startswith("["):
            idx = stripped.find('{"')
            if idx < 0:
                idx = stripped.find('{')
            if idx < 0:
                return None
            stripped = stripped[idx:]
        try:
            payload = json.loads(stripped)
        except (ValueError, TypeError):
            return None
        try:
            return parse_detail_payload(payload, item_id)
        except (DelistedError, ViolationError, RestrictedError):
            raise
        except IncompleteError:
            return None

    def close(self) -> None:
        try:
            if self.ctx is not None:
                self.ctx.close()
        except Exception:  # noqa: BLE001
            pass
        self.ctx = None
        try:
            if self.pw is not None:
                self.pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self.pw = None
        if self._temp_dir and self._user_dir:
            _rmtree_retry(self._user_dir)
        self._user_dir = None


def _rmtree_retry(path: str, attempts: int = 4) -> None:
    """Windows 下 Chrome 可能短暂占用文件，重试清理临时 Profile。"""
    for i in range(attempts):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except Exception:  # noqa: BLE001
            if i == attempts - 1:
                shutil.rmtree(path, ignore_errors=True)
            else:
                time.sleep(0.4 * (i + 1))


class SilentBrowser(_BrowserBase):
    """整轮任务复用的静默浏览器（headless，持久化 Context）。"""


class VisibleBrowser(_BrowserBase):
    """可见兜底：每次新建临时 Profile，用完即清。"""


# --------------------------------------------------------------------------
# 采集器
# --------------------------------------------------------------------------

class Outcome:
    __slots__ = ("ok", "kind", "info", "error", "detail", "method", "delist_reason")

    def __init__(self, ok: bool, kind: str, info: Optional[ProductInfo] = None,
                 error: str = "", detail: str = "", method: str = "",
                 delist_reason: str = ""):
        self.ok = ok
        self.kind = kind            # ok | delisted | temp | restricted
        self.info = info
        self.error = error
        self.detail = detail
        self.method = method        # api | curl | silent | visible
        self.delist_reason = delist_reason


class Collector:
    """三级兜底采集器。一个实例服务一轮采集，内部复用静默浏览器。"""

    def __init__(self, db, on_log: Callable[[str, str, str], None] | None = None,
                 on_event: Callable[[dict], None] | None = None):
        self.db = db
        self._log = on_log or (lambda *a, **k: None)
        self._emit = on_event or (lambda ev: None)
        self._silent: Optional[SilentBrowser] = None
        self._visible: Optional[VisibleBrowser] = None
        self.restrict_hits = 0

    # ---- 生命周期 ----

    def open(self) -> None:
        self._ensure_silent()

    def close(self) -> None:
        for obj in (self._visible, self._silent):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
        self._visible = None
        self._silent = None

    def _ensure_silent(self) -> SilentBrowser:
        if self._silent is None:
            b = SilentBrowser()
            b.start(headless=True, user_dir=C.BROWSER_PROFILE_DIR, temp=False)
            self._silent = b
            self._log("INFO", "浏览器", "静默浏览器已启动（{}）".format(b.browser_label or "Chromium"))
        return self._silent

    def _ensure_visible(self) -> VisibleBrowser:
        if self._visible is not None:
            try:
                self._visible.close()
            except Exception:  # noqa: BLE001
                pass
            self._visible = None
        C.ensure_dirs()
        user_dir = tempfile.mkdtemp(prefix="vis_", dir=C.TEMP_PROFILE_DIR)
        b = VisibleBrowser()
        b.start(headless=False, user_dir=user_dir, temp=True)
        self._visible = b
        self._log("INFO", "浏览器", "已打开可见浏览器兜底（临时 Profile）")
        return b

    # ---- 店铺扫描（自动拓品） ----

    def scan_shop_products(self, shop_id: str) -> list[dict]:
        """尝试扫描店铺主页的公开商品。

        小红书已不再支持未登录状态在电脑版查看店铺商品，因此这里只做尽力而为的解析；
        取不到内容时抛 TempError，由上层记录该店铺失败并跳过，不影响其它店铺。
        """
        if not shop_id:
            raise TempError("店铺 ID 为空")
        b = self._ensure_silent()
        url = C.SHOP_PAGE_TMPL.format(shop_id=shop_id)
        page = b.ctx.new_page()
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(2500)
            except Exception as exc:  # noqa: BLE001
                raise TempError("打开店铺主页失败：{}".format(str(exc)[:120])) from None
            text = _BrowserBase._page_body(page)
            if looks_restricted(text):
                raise RestrictedError("店铺主页触发风控校验")
            items = _extract_shop_items(text)
            if not items:
                raise TempError("未解析到店铺商品（小红书未登录状态下通常无法查看店铺商品）")
            return items
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- 单商品三级采集 ----

    def collect_one(self, pid: str) -> Outcome:
        """严格按 接口 → 静默 → 可见 的顺序为一个商品采集。"""
        # 一级：轻量接口
        try:
            info, _raw = collect_via_api(pid)
            return Outcome(True, "ok", info, method="api")
        except DelistedError as exc:
            return Outcome(False, "delisted", error=exc.message,
                           detail=exc.detail, delist_reason=exc.delist_reason)
        except ViolationError as exc:
            return Outcome(False, "delisted", error=exc.message,
                           detail=exc.detail, delist_reason=exc.delist_reason)
        except RestrictedError as exc:
            self.restrict_hits += 1
            first = exc
        except CollectError as exc:
            first = exc

        if first.kind == "restricted":
            # 461 已经发生，直接进入静默浏览器再试（若也失败则交回上层熔断判断）
            pass
        self._log("WARN", "采集", "接口直采失败，转静默浏览器：{}".format(first.message),
                  detail=first.detail, pid=pid)

        # 二级：静默浏览器
        try:
            b = self._ensure_silent()
            info = b.collect(pid)
            return Outcome(True, "ok", info, method="silent")
        except DelistedError as exc:
            return Outcome(False, "delisted", error=exc.message,
                           detail=exc.detail, delist_reason=exc.delist_reason)
        except ViolationError as exc:
            return Outcome(False, "delisted", error=exc.message,
                           detail=exc.detail, delist_reason=exc.delist_reason)
        except RestrictedError as exc:
            self.restrict_hits += 1
            second = exc
        except CollectError as exc:
            second = exc
        except Exception as exc:  # noqa: BLE001
            second = TempError("{}: {}".format(type(exc).__name__, str(exc)[:150]))

        self._log("WARN", "采集", "静默浏览器失败，转可见浏览器兜底：{}".format(second.message),
                  detail=second.detail, pid=pid)

        # 三级：可见浏览器
        try:
            b = self._ensure_visible()
            info = b.collect(pid)
            return Outcome(True, "ok", info, method="visible")
        except DelistedError as exc:
            return Outcome(False, "delisted", error=exc.message,
                           detail=exc.detail, delist_reason=exc.delist_reason)
        except ViolationError as exc:
            return Outcome(False, "delisted", error=exc.message,
                           detail=exc.detail, delist_reason=exc.delist_reason)
        except RestrictedError as exc:
            self.restrict_hits += 1
            return Outcome(False, "restricted", error=exc.message, detail=exc.detail)
        except CollectError as exc:
            return Outcome(False, "temp", error=exc.message, detail=exc.detail)
        except Exception as exc:  # noqa: BLE001
            return Outcome(False, "temp",
                           error="{}: {}".format(type(exc).__name__, str(exc)[:150]))
        finally:
            # 可见兜底用完即关，继续下一个商品
            if self._visible is not None:
                try:
                    self._visible.close()
                except Exception:  # noqa: BLE001
                    pass
                self._visible = None

    # ---- 单商品落库 ----

    def save(self, pid: str, info: ProductInfo, captured_at: str, method: str,
             shop_sold_limit: int | None = None) -> dict:
        """把一次成功采集写入数据库，并做累计已售上限判定。"""
        db = self.db
        limit = C.MAX_SOLD_TO_MONITOR if shop_sold_limit is None else shop_sold_limit
        db.update_product_meta(
            pid, title=info.title or "", shop_name=info.shop_name or "",
            shop_id=info.shop_id or "", cover=info.cover or "",
        )
        db.add_snapshot(
            pid, captured_at, info.sold, info.shop_sold, info.price,
            info.fans, info.stock_status, info.deliverable, method,
        )
        db.mark_success(pid, method)
        over_limit = info.sold is not None and int(info.sold) > limit
        if over_limit:
            db.deactivate(pid)
            db.log("WARN", "采集",
                   "累计已售 {} 超过监控上限 {}，已移出监控".format(info.sold, limit),
                   product_id=pid, title=info.title)
        return {"over_limit": over_limit, "info": info.as_dict()}

    # ---- 整轮 ----

    def run_round(self, products: list[dict], captured_at: str | None = None,
                  gap: tuple[float, float] | None = None,
                  retry: bool = True,
                  cancel_event=None) -> dict:
        """执行一轮完整采集。

        products: 商品记录列表（已按 active/未下架 过滤）
        captured_at: 本轮归属整点时间字符串
        gap: (min, max) 商品间隔秒数
        cancel_event: threading.Event；被置位后立即停止本轮剩余任务
                      （用于"上一整点轮次未完成、下一整点已到"的场景）
        """
        captured_at = captured_at or C.fmt(C.hour_start(C.now()))
        gap = gap or (self.db.get_float("min_gap_seconds", 0.1),
                      self.db.get_float("max_gap_seconds", 0.3))
        gap = (max(0.1, min(60.0, gap[0])), max(0.1, min(60.0, gap[1])))
        if gap[0] > gap[1]:
            gap = (gap[1], gap[0])

        def _cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        result = {
            "captured_at": captured_at,
            "total": len(products),
            "ok": 0, "failed": 0, "delisted": 0, "over_limit": 0,
            "restricted": False, "restricted_count": 0, "cancelled": False,
            "failed_items": [], "delisted_items": [], "pending": [],
            "cooldown_until": None,
        }
        self.restrict_hits = 0
        self._emit({"type": "round_start", "captured_at": captured_at, "total": len(products)})

        retry_pool: list[dict] = []
        for idx, prod in enumerate(products):
            if _cancelled():
                result["cancelled"] = True
                for p in products[idx:]:
                    result["pending"].append(p)
                self._log("WARN", "采集",
                          "本轮被新的整点轮次打断，剩余 {} 个商品转入等待队列".format(
                              len(result["pending"])))
                break
            pid = prod["id"]
            title = prod.get("title") or pid
            self._emit({"type": "product_start", "index": idx, "total": len(products),
                        "id": pid, "title": title})
            outcome = self.collect_one(pid)
            handled = self._handle_outcome(prod, outcome, captured_at, result, retry_pool)
            self._emit({"type": "product_done", "index": idx, "total": len(products),
                        "id": pid, "title": title, "outcome": outcome.kind,
                        "method": outcome.method, "error": outcome.error, **handled})

            if outcome.kind == "restricted" and self.restrict_hits >= C.RESTRICT_THRESHOLD:
                # 461 熔断：立即停止继续请求，剩余商品进队列，冷却后续采
                remaining = products[idx + 1:]
                for p in remaining:
                    result["pending"].append(p)
                for p in retry_pool:
                    if p not in result["pending"]:
                        result["pending"].append(p)
                result["restricted"] = True
                result["restricted_count"] = self.restrict_hits
                cooldown = C.now() + __import__("datetime").timedelta(
                    minutes=C.RESTRICT_COOLDOWN_MIN)
                result["cooldown_until"] = C.fmt(cooldown)
                self.db.set_setting("cooldown_until", C.fmt(cooldown))
                self.db.set_setting("pending_ids",
                                    json.dumps([p["id"] for p in result["pending"]]))
                self.db.log("ERROR", "采集",
                            "连续 {} 次 HTTP 461，触发熔断，停止本轮并冷却 {} 分钟".format(
                                self.restrict_hits, C.RESTRICT_COOLDOWN_MIN),
                            detail="剩余 {} 个商品已加入等待队列".format(len(result["pending"])))
                self._emit({"type": "restricted", "cooldown_until": result["cooldown_until"],
                            "count": len(result["pending"])})
                break

            if idx < len(products) - 1:
                time.sleep(random.uniform(gap[0], gap[1]))

        # 整轮结束后统一重试一次临时失败的商品（等待 5~15 秒）
        if retry and retry_pool and not result["restricted"] and not result["cancelled"]:
            wait = random.uniform(C.RETRY_WAIT_MIN, C.RETRY_WAIT_MAX)
            self._emit({"type": "retry_wait", "seconds": round(wait, 1),
                        "count": len(retry_pool)})
            self._log("INFO", "采集", "整轮结束，{} 个临时失败商品将在 {:.1f}s 后统一重试".format(
                len(retry_pool), wait))
            slept = 0.0
            while slept < wait and not _cancelled():
                time.sleep(0.25)
                slept += 0.25
            recovered: list[dict] = []
            still_failed: list[dict] = []
            for prod in retry_pool:
                if _cancelled():
                    still_failed.append({"id": prod["id"],
                                         "title": prod.get("title") or prod["id"],
                                         "error": "本轮被打断，未完成重试", "detail": ""})
                    continue
                pid = prod["id"]
                outcome = self.collect_one(pid)
                if outcome.ok:
                    self._apply_ok(prod, outcome, captured_at, result)
                    recovered.append(prod)
                    result["failed"] = max(0, result["failed"] - 1)
                    self.db.log("SUCCESS", "采集", "重试成功",
                                product_id=pid, title=prod.get("title") or pid)
                elif outcome.kind == "delisted":
                    self._apply_delisted(prod, outcome, result)
                    result["failed"] = max(0, result["failed"] - 1)
                    recovered.append(prod)
                else:
                    still_failed.append({"id": pid, "title": prod.get("title") or pid,
                                         "error": outcome.error, "detail": outcome.detail})
                if outcome.kind == "restricted" and self.restrict_hits >= C.RESTRICT_THRESHOLD:
                    result["restricted"] = True
                    break
            result["retried_recovered"] = len(recovered)
            result["retried_failed"] = len(still_failed)
            result["failed_items"] = still_failed
            # 重试仍失败的重新登记
            for item in still_failed:
                self.db.mark_failure(item["id"], item["error"])
            result["failed_items"] = still_failed

        self._emit({"type": "round_done", **{k: v for k, v in result.items()
                                             if k != "pending"}})
        return result

    # ---- 结果处理 ----

    def _handle_outcome(self, prod: dict, outcome: Outcome, captured_at: str,
                        result: dict, retry_pool: list[dict]) -> dict:
        pid = prod["id"]
        title = prod.get("title") or pid
        if outcome.ok:
            return self._apply_ok(prod, outcome, captured_at, result)
        if outcome.kind == "delisted":
            return self._apply_delisted(prod, outcome, result)
        if outcome.kind == "restricted":
            result["failed"] += 1
            result["failed_items"].append({"id": pid, "title": title,
                                           "error": outcome.error, "detail": outcome.detail})
            self.db.mark_failure(pid, outcome.error)
            self.db.log("ERROR", "采集", "请求环境受限（461）：{}".format(outcome.error),
                        detail=outcome.detail, product_id=pid, title=title)
            return {"status": "restricted", "over_limit": False}
        # 临时失败
        result["failed"] += 1
        retry_pool.append(prod)
        self.db.mark_failure(pid, outcome.error)
        self.db.log("ERROR", "采集", "采集失败：{}".format(outcome.error),
                    detail=outcome.detail, product_id=pid, title=title)
        return {"status": "failed", "over_limit": False}

    def _apply_ok(self, prod: dict, outcome: Outcome, captured_at: str, result: dict) -> dict:
        pid = prod["id"]
        saved = self.save(pid, outcome.info, captured_at, outcome.method)
        result["ok"] += 1
        if saved["over_limit"]:
            result["over_limit"] += 1
            self._emit({"type": "over_limit", "id": pid,
                        "title": outcome.info.title or prod.get("title") or pid,
                        "sold": outcome.info.sold})
        self.db.log("SUCCESS", "采集",
                    "采集成功（{}）已售={} 价格={}".format(
                        outcome.method, outcome.info.sold, outcome.info.price),
                    product_id=pid, title=outcome.info.title or prod.get("title") or pid)
        return {"status": "ok", "over_limit": saved["over_limit"]}

    def _apply_delisted(self, prod: dict, outcome: Outcome, result: dict) -> dict:
        pid = prod["id"]
        title = prod.get("title") or pid
        reason = outcome.delist_reason or "商品已下架"
        self.db.mark_delisted(pid, reason)
        result["delisted"] += 1
        result["delisted_items"].append({"id": pid, "title": title, "reason": reason})
        self.db.log("WARN", "采集", "确认下架：{}".format(reason),
                    detail=outcome.detail, product_id=pid, title=title)
        return {"status": "delisted", "reason": reason}


# --------------------------------------------------------------------------
# 添加商品时的预采集（不入库）
# --------------------------------------------------------------------------

def probe_product(pid: str, db=None) -> Outcome:
    """单个添加商品时的预采集：拿到名称/店铺/价格/累计已售供用户确认。"""
    collector = Collector(db) if db is not None else Collector(_NullDB())
    try:
        return collector.collect_one(pid)
    finally:
        try:
            collector.close()
        except Exception:  # noqa: BLE001
            pass


class _NullDB:
    """不给数据库时的占位实现。"""

    def log(self, *a, **k):
        pass

    def get_float(self, key, default=0.0):
        return default

    def set_setting(self, *a, **k):
        pass
