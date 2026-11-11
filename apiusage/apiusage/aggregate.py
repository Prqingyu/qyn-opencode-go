"""内存聚合：把一堆 :class:`~apiusage.model.Call` 卷成报告要的几张表。

纯函数，不碰网络也不碰磁盘 —— 所以它既能被单测直接喂数据，也能被任何新 provider
复用（只要产出 Call）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import peakhours
from .model import Call
from .timeutil import from_ms


@dataclass
class ModelRow:
    model: str = ""
    calls: int = 0
    succeeded: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def failed(self) -> int:
        return self.calls - self.succeeded

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.reasoning_tokens
                + self.cache_read_tokens + self.cache_write_tokens)

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.calls * 100.0 if self.calls else float("nan")


@dataclass
class DayRow:
    day: str
    calls: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    tokens: int = 0


@dataclass
class Stats:
    """一段区间的总量。"""

    calls: int = 0
    succeeded: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    peak_cost: float = 0.0
    offpeak_cost: float = 0.0
    peak_calls: int = 0
    offpeak_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    duration_sum: int = 0
    duration_n: int = 0
    ttft_sum: int = 0
    ttft_n: int = 0
    first_call_ms: Optional[int] = None
    last_call_ms: Optional[int] = None

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.reasoning_tokens
                + self.cache_read_tokens + self.cache_write_tokens)

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.calls * 100.0 if self.calls else float("nan")

    @property
    def cache_hit_rate(self) -> float:
        billed = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / billed * 100.0 if billed else float("nan")

    @property
    def avg_duration_ms(self) -> Optional[float]:
        return self.duration_sum / self.duration_n if self.duration_n else None

    @property
    def avg_ttft_ms(self) -> Optional[float]:
        return self.ttft_sum / self.ttft_n if self.ttft_n else None

    @property
    def avg_cost_usd(self) -> float:
        return self.cost_usd / self.calls if self.calls else float("nan")

    def absorb(self, call: Call) -> None:
        self.calls += 1
        if call.outcome == "succeeded":
            self.succeeded += 1
        else:
            self.failed += 1
        cost = call.cost_usd or 0.0
        self.cost_usd += cost
        self.input_tokens += call.input_tokens or 0
        self.output_tokens += call.output_tokens or 0
        self.reasoning_tokens += call.reasoning_tokens or 0
        self.cache_read_tokens += call.cache_read_tokens or 0
        self.cache_write_tokens += call.cache_write_tokens or 0
        self.cache_write_1h_tokens += call.cache_write_1h_tokens or 0
        if call.duration_ms is not None:
            self.duration_sum += call.duration_ms
            self.duration_n += 1
        if call.ttft_ms is not None:
            self.ttft_sum += call.ttft_ms
            self.ttft_n += 1
        if peakhours.is_peak(call.started_at_ms, "opencode_go"):
            self.peak_cost += cost
            self.peak_calls += 1
        else:
            self.offpeak_cost += cost
            self.offpeak_calls += 1
        if self.first_call_ms is None or call.started_at_ms < self.first_call_ms:
            self.first_call_ms = call.started_at_ms
        if self.last_call_ms is None or call.started_at_ms > self.last_call_ms:
            self.last_call_ms = call.started_at_ms


@dataclass
class Rollup:
    """一段区间的全部卷表。"""

    stats: Stats = field(default_factory=Stats)
    models: list = field(default_factory=list)
    days: list = field(default_factory=list)
    statuses: list = field(default_factory=list)
    finishes: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.stats.calls == 0


def rollup(calls: Iterable[Call]) -> Rollup:
    out = Rollup()
    models: dict = {}
    days: dict = {}
    statuses: dict = {}
    finishes: dict = {}

    for call in calls:
        out.stats.absorb(call)

        row = models.get(call.model or "(未知模型)")
        if row is None:
            row = models[call.model or "(未知模型)"] = ModelRow(model=call.model or "(未知模型)")
        row.calls += 1
        row.succeeded += int(call.outcome == "succeeded")
        row.cost_usd += call.cost_usd or 0.0
        row.input_tokens += call.input_tokens or 0
        row.output_tokens += call.output_tokens or 0
        row.reasoning_tokens += call.reasoning_tokens or 0
        row.cache_read_tokens += call.cache_read_tokens or 0
        row.cache_write_tokens += call.cache_write_tokens or 0

        dt = from_ms(call.started_at_ms)
        day = dt.strftime("%Y-%m-%d") if dt else "?"
        bucket = days.get(day)
        if bucket is None:
            bucket = days[day] = DayRow(day=day)
        bucket.calls += 1
        bucket.failed += int(call.outcome != "succeeded")
        bucket.cost_usd += call.cost_usd or 0.0
        bucket.tokens += call.total_tokens

        key = (call.status_code, call.error_code or "")
        entry = statuses.get(key)
        if entry is None:
            entry = statuses[key] = {"status_code": call.status_code,
                                     "error_code": call.error_code or "", "n": 0}
        entry["n"] += 1

        reason = call.finish_reason
        if reason:
            finishes[reason] = finishes.get(reason, 0) + 1

    out.models = sorted(models.values(), key=lambda m: (-m.cost_usd, -m.calls))
    out.days = [days[k] for k in sorted(days)]
    out.statuses = sorted(statuses.values(), key=lambda s: -s["n"])
    out.finishes = sorted(finishes.items(), key=lambda kv: -kv[1])
    return out


def merge_stats(items: Iterable[Stats]) -> Stats:
    """把几个工作区的总量合成一份（字段逐项相加）。"""
    out = Stats()
    for s in items:
        out.calls += s.calls
        out.succeeded += s.succeeded
        out.failed += s.failed
        out.cost_usd += s.cost_usd
        out.peak_cost += s.peak_cost
        out.offpeak_cost += s.offpeak_cost
        out.peak_calls += s.peak_calls
        out.offpeak_calls += s.offpeak_calls
        out.input_tokens += s.input_tokens
        out.output_tokens += s.output_tokens
        out.reasoning_tokens += s.reasoning_tokens
        out.cache_read_tokens += s.cache_read_tokens
        out.cache_write_tokens += s.cache_write_tokens
        out.cache_write_1h_tokens += s.cache_write_1h_tokens
        out.duration_sum += s.duration_sum
        out.duration_n += s.duration_n
        out.ttft_sum += s.ttft_sum
        out.ttft_n += s.ttft_n
        for attr in ("first_call_ms", "last_call_ms"):
            value = getattr(s, attr)
            if value is None:
                continue
            current = getattr(out, attr)
            if current is None:
                setattr(out, attr, value)
            elif attr == "first_call_ms":
                setattr(out, attr, min(current, value))
            else:
                setattr(out, attr, max(current, value))
    return out


def merge_days(rollups: Iterable[Rollup]) -> list:
    acc: dict = {}
    for r in rollups:
        for d in r.days:
            row = acc.get(d.day)
            if row is None:
                row = acc[d.day] = DayRow(day=d.day)
            row.calls += d.calls
            row.failed += d.failed
            row.cost_usd += d.cost_usd
            row.tokens += d.tokens
    return [acc[k] for k in sorted(acc)]
