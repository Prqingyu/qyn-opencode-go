"""峰 / 谷判定。

两家服务商的峰时段相同：**北京时间 09:00–12:00、14:00–18:00**，区别只在节假日：

* **DeepSeek 官方**：周末与中国法定节假日**不算峰**（也认调休上班日）。
* **OpenCode GO**：**没有节假日豁免** —— 只看「周一到周五」。这是 DSH-PET 按真实
  账单核对出来的：国庆当天 09:45–11:59 的调用按全额峰价计费。

节假日本身会变，所以 2026 年的表写在下面，并允许用 ``APIUSAGE_HOLIDAYS``
（逗号分隔 ``YYYY-MM-DD``）整表覆盖。表只影响 DeepSeek 侧的峰谷拆分；
GO 侧因为不看节假日，永远不需要它。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Iterable, Optional

from .timeutil import CN, from_ms

# 国办发明电〔2025〕7 号：2026 年部分节假日安排
HOLIDAYS_2026 = {
    # 元旦
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    # 清明
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07",
}

# 调休上班的周末（这些日子算法定工作日，DeepSeek 侧照常算峰）
MAKEUP_WORKDAYS_2026 = {
    "2026-01-04",  # 周日
    "2026-02-14", "2026-02-28",  # 周六
    "2026-05-09",  # 周六
    "2026-09-20",  # 周日
    "2026-10-10",  # 周六
}

# 峰时段（北京时间，左闭右开）
PEAK_WINDOWS = ((9, 12), (14, 18))


def _env_dates(name: str) -> Optional[set]:
    raw = os.environ.get(name)
    if not raw:
        return None
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.add(part)
    return out


def holidays() -> set:
    override = _env_dates("APIUSAGE_HOLIDAYS")
    return override if override is not None else set(HOLIDAYS_2026)


def makeup_workdays() -> set:
    override = _env_dates("APIUSAGE_MAKEUP_WORKDAYS")
    return override if override is not None else set(MAKEUP_WORKDAYS_2026)


def in_peak_window(dt: datetime) -> bool:
    local = dt.astimezone(CN)
    return any(start <= local.hour < end for start, end in PEAK_WINDOWS)


def is_workday(d: date, holiday_set: Optional[set] = None, makeup_set: Optional[set] = None) -> bool:
    """工作日 = 周一~周五 或 调休上班的周末，且不是法定节假日。"""
    key = d.isoformat()
    hol = holidays() if holiday_set is None else holiday_set
    if key in hol:
        return False
    if key in (makeup_workdays() if makeup_set is None else makeup_set):
        return True
    return d.weekday() < 5


def is_peak(ms: int, provider: str = "opencode_go") -> bool:
    """某个时刻是否处于峰时段（provider: ``opencode_go`` 或 ``deepseek``）。"""
    dt = from_ms(ms)
    if dt is None:
        return False
    if not in_peak_window(dt):
        return False
    if provider == "deepseek":
        return is_workday(dt.astimezone(CN).date())
    # GO：只要周一到周五，不看节假日、也不认调休（调休只影响「法定节假日」这一层）
    return dt.astimezone(CN).weekday() < 5


def split(calls: Iterable, provider: str = "opencode_go") -> dict:
    """把调用按峰/谷切开，返回成本与次数。"""
    peak_cost = offpeak_cost = 0.0
    peak_calls = offpeak_calls = 0
    peak_tokens = offpeak_tokens = 0
    for call in calls:
        cost = getattr(call, "cost_usd", None) or 0.0
        tokens = getattr(call, "total_tokens", 0) or 0
        if is_peak(call.started_at_ms, provider):
            peak_cost += cost
            peak_calls += 1
            peak_tokens += tokens
        else:
            offpeak_cost += cost
            offpeak_calls += 1
            offpeak_tokens += tokens
    return {
        "peak_cost": peak_cost,
        "offpeak_cost": offpeak_cost,
        "peak_calls": peak_calls,
        "offpeak_calls": offpeak_calls,
        "peak_tokens": peak_tokens,
        "offpeak_tokens": offpeak_tokens,
    }
