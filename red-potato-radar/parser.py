# -*- coding: utf-8 -*-
"""红薯雷达 —— 商品输入解析。

支持四种输入混排（一行一个，可批量）：
  1. 小红书分享口令（含标题、表情、短链、提取码的一整段文字）
  2. xhslink.com 短链
  3. 完整商品链接 https://www.xiaohongshu.com/goods-detail/<24位ID>?...
  4. 单独的 24 位十六进制商品 ID
"""

from __future__ import annotations

import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

import config as C

# 24 位十六进制商品 ID
RE_ITEM_ID = re.compile(r"\b([0-9a-fA-F]{24})\b")
# /goods-detail/<id>
RE_GOODS_DETAIL = re.compile(r"/goods-detail/([0-9a-fA-F]{24})")
# /goods/<id>
RE_GOODS_ALT = re.compile(r"/goods/([0-9a-fA-F]{24})")
# 商品详情接口里的 item_id=xxx
RE_ITEM_PARAM = re.compile(r"[?&](?:item_id|itemId|id)=([0-9a-fA-F]{24})")
# xhslink 短链
RE_SHORT_LINK = re.compile(r"(?:https?://)?(?:www\.)?xhslink\.com/[A-Za-z0-9/_\-\?=&%.]+")
# 任意 http(s) 链接
RE_ANY_URL = re.compile(r"https?://[^\s\u4e00-\u9fff，。！？、；：""''（）【】]+")
# 提取码
RE_CODE = re.compile(r"(?:提取码|口令|复制|密码)[:：\s]*([A-Za-z0-9]{4,12})")


def normalize_id(raw: str) -> str:
    return (raw or "").strip().lower()


def is_valid_id(raw: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{24}", normalize_id(raw)))


def normalize_title(title: str) -> str:
    """标题标准化，用于智能去重比对。

    先做 NFKC 归一化（全角→半角、％→%），再只保留字母/数字/汉字，
    从而忽略表情、标点、空白、大小写带来的差异。
    """
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", str(title))
    s = re.sub(r"[^\w]", "", s)      # 去掉除单词字符（含汉字）之外的一切
    s = s.replace("_", "")
    return s.lower()


# --------------------------------------------------------------------------
# 单行解析
# --------------------------------------------------------------------------

class ParsedInput:
    __slots__ = ("raw", "ids", "short_links", "needs_resolve", "invalid")

    def __init__(self) -> None:
        self.raw = ""
        self.ids: list[str] = []
        self.short_links: list[str] = []
        self.needs_resolve = False   # 需要跟随重定向才能拿到 ID
        self.invalid = False


def parse_line(line: str) -> ParsedInput:
    """解析一行输入，可能同时得到多个 ID 与若干短链。"""
    out = ParsedInput()
    text = (line or "").strip()
    out.raw = text
    if not text:
        out.invalid = True
        return out

    seen: set[str] = set()

    def _add(pid: str) -> None:
        pid = normalize_id(pid)
        if is_valid_id(pid) and pid not in seen:
            seen.add(pid)
            out.ids.append(pid)

    # 1) /goods-detail/<id>
    for m in RE_GOODS_DETAIL.finditer(text):
        _add(m.group(1))
    # 2) /goods/<id>
    if not out.ids:
        for m in RE_GOODS_ALT.finditer(text):
            _add(m.group(1))
    # 3) 接口参数 item_id=
    if not out.ids:
        for m in RE_ITEM_PARAM.finditer(text):
            _add(m.group(1))
    # 4) 短链：先记下来，稍后跟随重定向
    for m in RE_SHORT_LINK.finditer(text):
        url = m.group(0)
        if not url.startswith("http"):
            url = "http://" + url
        if url not in out.short_links:
            out.short_links.append(url)
    # 5) 其它链接里也可能直接带 ID
    for m in RE_ANY_URL.finditer(text):
        url = m.group(0)
        for rx in (RE_GOODS_DETAIL, RE_GOODS_ALT, RE_ITEM_PARAM):
            for mm in rx.finditer(url):
                _add(mm.group(1))

    # 6) 纯 24 位 ID（单独输入，或分享口令正文里带）
    if not out.ids:
        for m in RE_ITEM_ID.finditer(text):
            _add(m.group(1))

    if out.short_links and not out.ids:
        out.needs_resolve = True
    elif not out.ids and not out.short_links:
        # 允许「纯数字/其它」判定为无效输入
        out.invalid = True
    return out


def extract_code(line: str) -> str:
    m = RE_CODE.search(line or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# 短链重定向
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def resolve_short_link(url: str, timeout: int = C.HTTP_TIMEOUT) -> dict:
    """跟随 xhslink 短链重定向，返回 {final_url, ids, text}。"""
    result = {"final_url": url, "ids": [], "text": "", "error": ""}
    redirect_chain: list[str] = []
    current = url
    for _hop in range(6):
        req = urllib.request.Request(current, headers=dict(C.DEFAULT_HEADERS), method="GET")
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read() or b""
                text = _decode(body)
                result["final_url"] = resp.geturl() or current
                result["text"] = text
                break
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("Location") if exc.headers else None
            if loc:
                redirect_chain.append(current)
                current = urllib.parse.urljoin(current, loc)
                result["final_url"] = current
                continue
            # 非重定向错误：退回普通请求再试一次
            result["error"] = "HTTP {}".format(exc.code)
            break
        except Exception as exc:  # noqa: BLE001
            result["error"] = _short_err(exc)
            break

    target = result["final_url"]
    found: list[str] = []

    def _collect(text: str) -> None:
        for rx in (RE_GOODS_DETAIL, RE_GOODS_ALT, RE_ITEM_PARAM):
            for m in rx.finditer(text or ""):
                pid = normalize_id(m.group(1))
                if pid not in found:
                    found.append(pid)
        if not found:
            for m in RE_ITEM_ID.finditer(text or ""):
                pid = normalize_id(m.group(1))
                if pid not in found:
                    found.append(pid)

    _collect(target)
    _collect(result.get("text", ""))
    result["ids"] = found
    return result


def _short_err(exc: Exception) -> str:
    name = type(exc).__name__
    text = str(exc)
    if isinstance(exc, urllib.error.URLError):
        text = str(exc.reason)
    return "shortlink:{}:{}".format(name, text[:200])


def _decode(body: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return body.decode(enc, errors="ignore")
        except Exception:  # noqa: BLE001
            continue
    return ""


# --------------------------------------------------------------------------
# 批量解析
# --------------------------------------------------------------------------

def parse_batch(text: str) -> dict:
    """解析多行输入。

    返回 {ids, short_links, invalid_lines, total_lines}
    """
    result = {
        "ids": [],
        "short_links": [],
        "invalid_lines": [],
        "total_lines": 0,
    }
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        result["total_lines"] += 1
        parsed = parse_line(line)
        if parsed.ids:
            for pid in parsed.ids:
                if pid not in seen:
                    seen.add(pid)
                    result["ids"].append(pid)
            continue
        if parsed.short_links:
            for url in parsed.short_links:
                if url not in result["short_links"]:
                    result["short_links"].append(url)
            continue
        result["invalid_lines"].append(line)
    return result


def parse_ids_only(text: str) -> list[str]:
    return parse_batch(text)["ids"]


if __name__ == "__main__":  # 手工自测
    samples = [
        "39 【标题】某某连衣裙 😊 http://xhslink.com/a/AbCdEf 提取码: 4N2K",
        "https://xhslink.com/xYz123",
        "https://www.xiaohongshu.com/goods-detail/65f1a2b3c4d5e6f708192a3b?xsec_token=abc",
        "65f1a2b3c4d5e6f708192a3b",
    ]
    for s in samples:
        p = parse_line(s)
        print(repr(s[:40]), "->", p.ids, p.short_links, "invalid" if p.invalid else "")
