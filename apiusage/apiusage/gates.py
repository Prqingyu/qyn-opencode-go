"""GO 三个额度窗口的「套娃」关系。

5 小时窗口的额度是月额度的 20%、周是 50%，而且**一次调用同时扣这三个池子** ——
所以一个窗口自己还有钱，照样可能一分都花不出去。规则只有一句：

    **更高一层空了，当前这层就显示 0%，倒计时换成那一层的重置时间。**

「更高一层」只有月 > 周 > 5 小时这一条链；月空了就取月的时间，因为月重置得最晚
（报周的时间等于承诺你提前两三周就能用）。
"""

from __future__ import annotations

from typing import Optional

ABOVE = {"fiveHour": ("week", "month"), "week": ("month",), "month": ()}

ORDER = ("month", "week", "fiveHour")


def is_empty(meters: dict, window: str) -> bool:
    """窗口用完没有；**没读到的窗口（缺项）不算空**，不会把整个面板拖黑。"""
    meter = meters.get(window)
    if meter is None:
        return False
    return _remaining_usd(meter) <= 0


def _remaining_usd(meter) -> float:
    if hasattr(meter, "remaining_usd"):
        return meter.remaining_usd
    limit = meter["limit_usd"]
    used = meter["used_usd"]
    return max(0.0, (limit or 0.0) - (used or 0.0))


def blocking_window(meters: dict, window: str) -> Optional[str]:
    """卡住这个窗口的最高那一层（没有就返回 None）。"""
    for name in ("month", "week"):
        if name in ABOVE[window] and is_empty(meters, name):
            return name
    return None


def effective_percent(meters: dict, window: str) -> float:
    """面板上该显示的剩余百分比：被上层压住就是 0。"""
    meter = meters.get(window)
    if meter is None:
        return float("nan")
    if blocking_window(meters, window):
        return 0.0
    return _percent(meter)


def _percent(meter) -> float:
    if hasattr(meter, "remaining_percent"):
        return meter.remaining_percent
    limit = meter["limit_usd"] or 0.0
    used = meter["used_usd"] or 0.0
    if limit <= 0:
        return float("nan")
    return min(100.0, max(0.0, (limit - used) / limit * 100.0))


def effective_reset_ms(meters: dict, window: str) -> Optional[int]:
    """面板上该显示的倒计时基准时刻：被上层压住时取那一层的重置时间。"""
    blocker = blocking_window(meters, window)
    source = meters.get(blocker) if blocker else meters.get(window)
    if source is None:
        return None
    return source["resets_at_ms"] if not hasattr(source, "resets_at_ms") else source.resets_at_ms


def worst_window(meters: dict) -> Optional[str]:
    """最紧张的那一层（按剩余**金额**，不是百分比 —— 三个窗口天花板不同）。"""
    usable = {
        name: _remaining_usd(m)
        for name, m in meters.items()
        if m is not None and not _is_nan(_remaining_usd(m))
    }
    if not usable:
        return None
    empty = [name for name in ORDER if usable.get(name) is not None and usable[name] <= 0]
    if empty:
        # 并列时取更宽的那层：它是最后恢复的，也就是真正卡住你的那层
        for name in ORDER:
            if name in empty:
                return name
    return min(usable, key=lambda n: usable[n])


def _is_nan(value: float) -> bool:
    return value != value
