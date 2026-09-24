# -*- coding: utf-8 -*-
"""红薯雷达 —— 企业微信机器人通知。

每轮正式采集结束后推送店铺销量时报（按上小时销量降序），
以及采集失败通知。消息过长自动拆分，不超过机器人单条限制。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import urllib.error
import urllib.request

import config as C

RE_WEBHOOK = re.compile(
    r"^https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[0-9a-zA-Z\-_]{10,}$"
)

# 机器人单条消息体上限约 4096 字节，留出余量
MAX_CHARS = 1800
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def validate_webhook(url: str) -> tuple[bool, str]:
    url = (url or "").strip()
    if not url:
        return False, "Webhook 为空"
    if not url.startswith("https://"):
        return False, "Webhook 必须是 https 地址"
    if "qyapi.weixin.qq.com" not in url:
        return False, "不是企业微信机器人地址"
    if "/cgi-bin/webhook/send" not in url:
        return False, "地址路径不正确，应为 /cgi-bin/webhook/send?key=..."
    if not RE_WEBHOOK.match(url):
        return False, "key 参数缺失或格式不正确"
    return True, "格式正确"


def mask_webhook(url: str) -> str:
    if not url:
        return "（未配置）"
    key = url.split("key=")[-1] if "key=" in url else ""
    if len(key) > 8:
        return url.split("key=")[0] + "key={}...{}".format(key[:4], key[-4:])
    return url.split("key=")[0] + "key=***"


def post_json(url: str, payload: dict, timeout: int = C.WECOM_TIMEOUT) -> tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = (resp.read() or b"").decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return False, "HTTP {}".format(exc.code)
    except Exception as exc:  # noqa: BLE001
        return False, "{}: {}".format(type(exc).__name__, str(exc)[:120])
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return False, "返回体无法解析：{}".format(body[:120])
    if obj.get("errcode") == 0:
        return True, "OK"
    return False, "errcode={} errmsg={}".format(obj.get("errcode"), obj.get("errmsg"))


def send_text(webhook: str, content: str) -> tuple[bool, str]:
    return post_json(webhook, {"msgtype": "text", "text": {"content": content}})


def send_markdown(webhook: str, content: str) -> tuple[bool, str]:
    return post_json(webhook, {"msgtype": "markdown", "markdown": {"content": content}})


def split_message(text: str, limit: int = MAX_CHARS) -> list[str]:
    """按空行/行边界拆分长消息，保证每条不超限。"""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n"):
        piece = para + "\n"
        if size + len(piece) > limit and buf:
            parts.append("".join(buf).rstrip("\n"))
            buf, size = [], 0
        if len(piece) > limit:
            for i in range(0, len(piece), limit):
                chunk = piece[i:i + limit]
                if size + len(chunk) > limit and buf:
                    parts.append("".join(buf).rstrip("\n"))
                    buf, size = [], 0
                buf.append(chunk)
                size += len(chunk)
            continue
        buf.append(piece)
        size += len(piece)
    if buf:
        parts.append("".join(buf).rstrip("\n"))
    return parts


# --------------------------------------------------------------------------
# 时报
# --------------------------------------------------------------------------

def build_sales_report(shop_rows: list[dict], captured_at: _dt.datetime,
                       allowed_shops: list[str] | None = None) -> list[str]:
    """构建店铺销量时报，按上小时销量降序。返回待发送的消息列表。"""
    rows = [r for r in shop_rows if r.get("last_hour") is not None]
    if allowed_shops:
        allow = set(allowed_shops)
        rows = [r for r in rows if r.get("shop_name") in allow]
    rows.sort(key=lambda r: (-(r.get("last_hour") or 0), r.get("shop_name") or ""))

    start = captured_at
    end = captured_at + _dt.timedelta(minutes=59)
    head = "📊 {}点店铺销量时报\n{} - {}".format(
        captured_at.hour, start.strftime("%m月%d日 %H:%M"), end.strftime("%H:%M"))

    if not rows:
        return [head + "\n\n本轮暂无可统计的店铺销量。"]

    blocks: list[str] = []
    for i, r in enumerate(rows, 1):
        badge = MEDALS.get(i, "{}.".format(i))
        today = r.get("today")
        blocks.append(
            "{} {}\n上小时销量：{} 单\n今日总销量：{} 单".format(
                badge, r.get("shop_name") or "（未知店铺）",
                _num(r.get("last_hour")), _num(today),
            )
        )

    full = head + "\n\n" + "\n\n".join(blocks)
    parts = split_message(full)
    if len(parts) > 1:
        parts = ["{}（{}/{}）".format(p.split("\n")[0], i + 1, len(parts))
                 + "\n" + "\n".join(p.split("\n")[1:]) for i, p in enumerate(parts)]
    return parts


def build_failure_report(failed_items: list[dict], captured_at: _dt.datetime,
                         method_hint: str = "接口/静默浏览器/可见浏览器") -> list[str]:
    """采集失败通知。明确下架的商品不会出现在这里。"""
    if not failed_items:
        return []
    lines = ["⚠️ {}点采集失败通知".format(captured_at.hour),
             "时间：{}".format(captured_at.strftime("%Y-%m-%d %H:%M")),
             "采集方式：{}".format(method_hint),
             "失败数量：{} 个".format(len(failed_items)),
             ""]
    for item in failed_items[:30]:
        lines.append("· {}（{}）".format(item.get("title") or item.get("id"),
                                        item.get("id") or ""))
        lines.append("  原因：{}".format((item.get("error") or "未知错误")[:120]))
    if len(failed_items) > 30:
        lines.append("…… 其余 {} 个请查看运行日志。".format(len(failed_items) - 30))
    return split_message("\n".join(lines))


def _num(v) -> str:
    return "—" if v is None else str(v)


def send_report(db, analytics, captured_at: _dt.datetime) -> dict:
    """整轮采集完成后推送时报 + 失败通知。发送失败只写日志，不影响采集数据。"""
    result = {"sent": 0, "failed": 0, "enabled": False, "errors": []}
    if not db.get_bool("wecom_enabled", False):
        return result
    webhook = (db.get_setting("wecom_webhook", "") or "").strip()
    ok, reason = validate_webhook(webhook)
    if not ok:
        db.log("ERROR", "通知", "Webhook 配置无效，跳过通知：{}".format(reason))
        result["errors"].append(reason)
        return result
    result["enabled"] = True

    allowed = db.get_json("wecom_shops", []) or []
    try:
        shop_rows = analytics.shop_rows(captured_at)
    except Exception as exc:  # noqa: BLE001
        db.log("ERROR", "通知", "统计店铺数据失败：{}".format(str(exc)[:150]))
        return result

    messages = build_sales_report(shop_rows, captured_at, allowed)
    for msg in messages:
        ok, detail = send_text(webhook, msg)
        if ok:
            result["sent"] += 1
        else:
            result["failed"] += 1
            result["errors"].append(detail)
            db.log("ERROR", "通知", "时报发送失败：{}".format(detail))
    if result["sent"]:
        db.log("SUCCESS", "通知", "店铺销量时报已发送（{} 条消息，{} 个店铺）".format(
            result["sent"], len(shop_rows)))

    pending_failures = db.get_json("pending_fail_items", []) or []
    if pending_failures:
        for msg in build_failure_report(pending_failures, captured_at):
            ok, detail = send_text(webhook, msg)
            if ok:
                result["sent"] += 1
            else:
                result["failed"] += 1
                db.log("ERROR", "通知", "失败通知发送失败：{}".format(detail))
        db.set_setting("pending_fail_items", "[]")
    return result


def send_test(webhook: str) -> tuple[bool, str]:
    ok, reason = validate_webhook(webhook)
    if not ok:
        return False, reason
    text = ("【{}】通知测试\n时间：{}\n如果你看到这条消息，说明机器人配置正确。".format(
        C.APP_NAME, C.now_str()))
    return send_text(webhook, text)
