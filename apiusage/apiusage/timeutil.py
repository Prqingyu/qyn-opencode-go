"""时间工具：统一用「北京时间 (UTC+8)」做日历切分，用 unix 毫秒做存储。

Windows 上没有 IANA 时区库（zoneinfo 会找不到 Asia/Shanghai），所以这里固定
用 UTC+8 偏移量 —— 中国全境只有一个时区、且没有夏令时，固定偏移是精确的，
不是近似。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

CN = timezone(timedelta(hours=8), "Asia/Shanghai")

MS_PER_SECOND = 1000
MS_PER_DAY = 86_400_000


def now() -> datetime:
    """当前北京时间。"""
    return datetime.now(CN)


def now_ms() -> int:
    """当前 unix 毫秒。"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def to_ms(dt: datetime) -> int:
    """按 UTC 转 unix 毫秒；naive 的输入按北京时间解释。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN)
    return int(dt.timestamp() * 1000)


def from_ms(ms: Optional[int], tz: timezone = CN) -> Optional[datetime]:
    """unix 毫秒 → 带时区的 datetime（默认北京时间）。"""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz)


def parse_iso(raw: Optional[str]) -> Optional[datetime]:
    """解析接口给的 ISO-8601 瞬时（如 ``2026-10-05T00:00:00.000Z``）为 UTC。"""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ms_of_iso(raw: Optional[str]) -> Optional[int]:
    dt = parse_iso(raw)
    return None if dt is None else int(dt.timestamp() * 1000)


def day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=CN)


def period_range(period: str, ref: Optional[datetime] = None):
    """把 today / week / month / all 换算成 [start_ms, end_ms)。

    week 以周一为起点（与 DSH-PET 的「本周」一致）。
    """
    ref = ref or now()
    end = ref
    today = ref.date()
    if period == "today":
        start = day_start(today)
    elif period == "week":
        start = day_start(today - timedelta(days=today.weekday()))
    elif period == "month":
        start = day_start(today.replace(day=1))
    elif period in ("all", "total"):
        return None, to_ms(end)
    else:
        raise ValueError(f"未知周期: {period}")
    return to_ms(start), to_ms(end)


def fmt_local(ms: Optional[int], with_seconds: bool = True) -> str:
    dt = from_ms(ms)
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def fmt_clock(ms: Optional[int]) -> str:
    dt = from_ms(ms)
    return "-" if dt is None else dt.strftime("%H:%M")


def humanize_delta(ms_future: Optional[int], ref_ms: Optional[int] = None) -> str:
    """把「距现在还有多久」写成人话：``2 天 3 小时`` / ``19 分钟`` / ``已过``。"""
    if ms_future is None:
        return "未开始计时"
    ref = now_ms() if ref_ms is None else ref_ms
    left = (ms_future - ref) / 1000.0
    if left <= 0:
        return "已重置"
    if left < 60:
        return "不到 1 分钟"
    if left < 3600:
        return f"{int(left // 60)} 分钟"
    if left < 86400:
        hours = int(left // 3600)
        minutes = int(left % 3600 // 60)
        return f"{hours} 小时" + (f" {minutes} 分" if minutes else "")
    days = int(left // 86400)
    hours = int(left % 86400 // 3600)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


def reset_phrase(ms_future: Optional[int], ref_ms: Optional[int] = None) -> str:
    """「还有多久重置」的完整说法；没开始计时就别硬接一个「后重置」。"""
    if ms_future is None:
        return "未开始计时"
    return f"{humanize_delta(ms_future, ref_ms)}后重置"


def humanize_ago(ms_past: Optional[int], ref_ms: Optional[int] = None) -> str:
    """「多久以前」——注意方向和 :func:`humanize_delta` 相反，别传错。"""
    if ms_past is None:
        return "-"
    ref = now_ms() if ref_ms is None else ref_ms
    diff = (ref - ms_past) / 1000.0
    if diff < 0:
        return "未来"
    if diff < 90:
        return "刚刚"
    if diff < 3600:
        return f"{int(diff // 60)} 分钟"
    if diff < 86400:
        return f"{diff / 3600:.1f} 小时"
    return f"{diff / 86400:.1f} 天"


def humanize_span(seconds: float) -> str:
    """一段时长的人话描述，用于「按当前速率还能撑多久」。"""
    if seconds is None:
        return "-"
    if seconds <= 0:
        return "已耗尽"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"
