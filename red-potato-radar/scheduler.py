# -*- coding: utf-8 -*-
"""红薯雷达 —— 定时调度。

规则：
  * 只在**自然时间边界**触发（09:35 点开始监控 → 首次 10:00，之后 11:00）。
  * 同一时刻只允许一轮采集；新整点到达而上一轮未完成时，打断旧轮次并立即切到新轮次。
  * 461 熔断后按冷却时间自动续采未完成商品。
"""

from __future__ import annotations

import datetime as _dt
import threading

import config as C


def next_boundary(interval_minutes: int, now: _dt.datetime | None = None) -> _dt.datetime:
    """返回严格大于 now 的下一个自然时间边界。

    interval=60 且 now=09:35 → 10:00
    interval=30 且 now=09:35 → 10:00
    interval=60 且 now=23:10 → 次日 00:00
    """
    now = now or C.now()
    interval = max(1, int(interval_minutes or 60))
    base = now.replace(second=0, microsecond=0)
    minutes = base.hour * 60 + base.minute
    nxt = ((minutes // interval) + 1) * interval
    if nxt >= 24 * 60:
        nxt -= 24 * 60
        base = C.day_start(base) + _dt.timedelta(days=1)
    return base.replace(hour=nxt // 60, minute=nxt % 60, second=0, microsecond=0)


def current_boundary(interval_minutes: int, now: _dt.datetime | None = None) -> _dt.datetime:
    """返回不晚于 now 的最近一个自然边界（用于给本轮采集归时间）。"""
    now = now or C.now()
    interval = max(1, int(interval_minutes or 60))
    base = now.replace(second=0, microsecond=0)
    minutes = base.hour * 60 + base.minute
    cur = (minutes // interval) * interval
    return base.replace(hour=cur // 60, minute=cur % 60, second=0, microsecond=0)


class CollectScheduler(threading.Thread):
    """独立调度线程。

    on_run(captured_at, manual, cancel_event) 由调用方实现，负责真正跑一轮采集；
    本线程只负责"什么时候跑"与"状态播报"。
    """

    def __init__(self, on_run, on_state=None, interval_getter=None):
        super().__init__(name="xhs-scheduler", daemon=True)
        self._on_run = on_run
        self._on_state = on_state or (lambda **kw: None)
        self._interval_getter = interval_getter or (lambda: 60)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running = False
        self._next_at: _dt.datetime | None = None
        self._current_cancel: threading.Event | None = None
        self._cooldown_until: _dt.datetime | None = None
        self._pending_ids: list[str] = []
        self._lock = threading.RLock()

    # ---- 状态 ----

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def next_at(self) -> _dt.datetime | None:
        with self._lock:
            return self._next_at

    def status_text(self) -> str:
        with self._lock:
            if not self.running:
                return "监控已停止"
            if self._cooldown_until and C.now() < self._cooldown_until:
                return "风控冷却中，{} 续采".format(self._cooldown_until.strftime("%H:%M"))
            if self._running_round:
                return "正在采集…"
            if self._next_at:
                return "下次采集 {}".format(self._next_at.strftime("%m-%d %H:%M"))
            return "运行中"

    # ---- 控制 ----

    def start_monitor(self) -> None:
        with self._lock:
            if self.running:
                return
            self._running = True
            self._stop.clear()
            self._wake.set()
        self._publish()

    def stop_monitor(self, cancel_running: bool = True) -> None:
        with self._lock:
            self._running = False
            self._next_at = None
            if cancel_running and self._current_cancel is not None:
                self._current_cancel.set()
        self._wake.set()
        self._publish()

    def shutdown(self) -> None:
        with self._lock:
            self._running = False
            self._stop.set()
            if self._current_cancel is not None:
                self._current_cancel.set()
        self._wake.set()

    def nudge(self) -> None:
        """设置变更后立即重新计算下一次触发时间。"""
        self._wake.set()

    def set_cooldown(self, until: _dt.datetime, pending_ids: list[str]) -> None:
        with self._lock:
            self._cooldown_until = until
            self._pending_ids = list(pending_ids or [])
        self._wake.set()
        self._publish()

    def clear_cooldown(self) -> None:
        with self._lock:
            self._cooldown_until = None
            self._pending_ids = []
        self._publish()

    def request_manual(self) -> threading.Event:
        """立即采集：打断正在跑的轮次，返回给新轮次使用的 cancel 事件。"""
        with self._lock:
            if self._current_cancel is not None:
                self._current_cancel.set()
        ev = threading.Event()
        with self._lock:
            self._current_cancel = ev
        return ev

    # ---- 主循环 ----

    _running_round = False

    def run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                running = self._running
                cooldown_until = self._cooldown_until
                pending = list(self._pending_ids)

            if not running:
                self._wake.wait(1.0)
                self._wake.clear()
                with self._lock:
                    self._next_at = None
                continue

            now = C.now()

            # 风控冷却优先：冷却到点后立刻续采未完成商品
            if cooldown_until and now < cooldown_until:
                self._wait_until(cooldown_until)
                if self._stop.is_set():
                    break
                with self._lock:
                    still_running = self._running
                if not still_running:
                    continue
                self._cooldown_until = None
                if pending:
                    ids = set(pending)
                    self._pending_ids = []
                    self._fire(C.hour_start(C.now()), manual=False, only_ids=ids)
                continue
            if cooldown_until and now >= cooldown_until:
                self._cooldown_until = None
                if pending:
                    ids = set(pending)
                    self._pending_ids = []
                    self._fire(C.hour_start(C.now()), manual=False, only_ids=ids)
                continue

            interval = max(1, int(self._interval_getter() or 60))
            with self._lock:
                target = self._next_at
            if target is None:
                target = next_boundary(interval, now)
                with self._lock:
                    self._next_at = target
                self._publish()

            if self._wait_until(target):
                break  # 被 shutdown 打断
            if self._stop.is_set():
                break

            # 触发时重新校验（可能期间被停止或改了周期）
            with self._lock:
                if not self._running:
                    continue
                self._next_at = None
            self._fire(C.hour_start(C.now()), manual=False, only_ids=None)

    def _wait_until(self, target: _dt.datetime) -> bool:
        """等待到 target。被 nudge 唤醒时提前返回 False 以便重算。返回 True 表示收到 shutdown。"""
        while not self._stop.is_set():
            remain = (target - C.now()).total_seconds()
            if remain <= 0:
                return False
            if self._wake.wait(min(remain, 1.0)):
                self._wake.clear()
                if self._stop.is_set():
                    return True
                # 被唤醒：若目标已被清空或变更，交由主循环重算
                with self._lock:
                    if self._next_at is None or self._next_at != target:
                        return False
        return True

    def _fire(self, captured_at: _dt.datetime, manual: bool, only_ids: set[str] | None) -> None:
        ev = threading.Event()
        with self._lock:
            if self._current_cancel is not None:
                self._current_cancel.set()   # 打断旧轮次
            self._current_cancel = ev
            self._running_round = True
        self._publish()
        try:
            self._on_run(captured_at, manual, ev, only_ids)
        except Exception:  # noqa: BLE001
            pass
        finally:
            with self._lock:
                if self._current_cancel is ev:
                    self._current_cancel = None
                self._running_round = False
            self._publish()

    def _publish(self) -> None:
        try:
            self._on_state(text=self.status_text(), next_at=self.next_at,
                           running=self.running)
        except Exception:  # noqa: BLE001
            pass
