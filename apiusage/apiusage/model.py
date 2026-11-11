"""中立的领域模型：所有 provider 都产出这些类型，report 只认它们。

**这里没有任何存储逻辑**，也不该有 —— 数据来自接口、在内存里用完就丢。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Optional

MICRO_CENTS_PER_USD = 100_000_000.0


def is_nan(value) -> bool:
    """缺值判断：``None`` 和 ``NaN`` 都算「没有数」。

    别直接 ``math.isnan(x)`` —— 它遇到 ``None`` 会抛 TypeError，
    而接口少给一个字段是完全正常的事。
    """
    return value is None or (isinstance(value, float) and value != value)


@dataclass
class BalanceInfo:
    """``balance_infos[]`` 里的一条 —— **一个币种一条**。

    官方接口可以同时给多个币种（现在本机只有 CNY 一条），所以逐条收着，
    别只挑一条就走：哪天多出一条 USD，只看 CNY 的实现会静默少报。
    """

    currency: str = ""
    total: float = math.nan
    granted: Optional[float] = None
    topped_up: Optional[float] = None

    @property
    def usable(self) -> bool:
        return not is_nan(self.total)


@dataclass
class Balance:
    """DeepSeek 官方 API 账户余额。**账户级**，不属于任何 OC 工作区。

    ``total`` / ``granted`` / ``topped_up`` 是**人民币那条**的快捷方式（展示用），
    全部币种在 ``infos`` 里。取数失败时 ``error`` 有值、``total`` 是 NaN ——
    这样大屏能显示「余额取数失败」，而不是让这一格凭空消失。
    """

    currency: str = "CNY"
    total: float = math.nan
    granted: Optional[float] = None
    topped_up: Optional[float] = None
    is_available: Optional[bool] = None
    account: str = ""
    infos: list = field(default_factory=list)
    error: str = ""

    @property
    def usable(self) -> bool:
        return not is_nan(self.total)

    def info(self, currency: str) -> Optional[BalanceInfo]:
        for item in self.infos:
            if item.currency == currency:
                return item
        return None


@dataclass
class Meter:
    """OpenCode GO 的一个额度窗口。"""

    window: str
    limit_usd: float
    used_usd: float
    resets_at_ms: Optional[int] = None
    starts_at_ms: Optional[int] = None

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.used_usd)

    @property
    def remaining_percent(self) -> float:
        if self.limit_usd <= 0:
            return math.nan
        return min(100.0, max(0.0, (self.limit_usd - self.used_usd) / self.limit_usd * 100.0))

    @property
    def label(self) -> str:
        return {"fiveHour": "5 小时", "week": "本周", "month": "本月"}.get(self.window, self.window)


@dataclass
class Call:
    """一条调用明细。"""

    id: str
    started_at_ms: int
    model: str = ""
    outcome: str = ""
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    duration_ms: Optional[int] = None
    ttft_ms: Optional[int] = None
    status_code: Optional[int] = None
    error_code: str = ""
    session_id: str = ""
    protocol: str = ""
    reasoning_effort: str = ""
    # 只有 CSV 导出才带的字段（实测本账号恒空，出现就收）
    finish: str = ""
    cache_write_1h_tokens: Optional[int] = None
    reasoning_budget_tokens: Optional[int] = None
    policies: str = ""
    workspace_id: str = ""

    @property
    def billable(self) -> bool:
        return self.outcome == "succeeded" and bool(self.cost_usd and self.cost_usd > 0)

    @property
    def finish_reason(self) -> str:
        """``finish`` 里真正想要的那个词：``tool_calls`` / ``stop`` / ``length``。

        实测 ``finish`` 是个对象 ``{"reason":"tool_calls"}``，早期当成字符串解析，
        整个字段被静默丢掉 —— 所以这里对两种形状都兜住。
        """
        if not self.finish:
            return ""
        if self.finish.startswith("{"):
            try:
                doc = json.loads(self.finish)
            except ValueError:
                return ""
            reason = doc.get("reason") if isinstance(doc, dict) else None
            return reason if isinstance(reason, str) else ""
        return self.finish

    @property
    def total_tokens(self) -> int:
        """总量只算这五个 —— ``cache_write_1h_tokens`` 是缓存写的变体，可能重叠，不折进来。"""
        return sum(
            v or 0
            for v in (
                self.input_tokens,
                self.output_tokens,
                self.reasoning_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        )


@dataclass
class Workspace:
    """一个 OpenCode 工作区（``wrk_…`` 或 ``org_…``）。

    它属于某个**登录档案**（profile）：同一个 Cookie 能看到该账号下的所有工作区，
    所以「多工作区」不需要多套凭据，只需要换 ``x-org-id``。
    """

    id: str
    name: str = ""
    profile: str = ""
    has_go: bool = True

    @property
    def label(self) -> str:
        return f"{self.name}（{self.id}）" if self.name else self.id

    @property
    def short(self) -> str:
        return self.name or self.id


@dataclass
class WorkspaceData:
    """一个工作区这一轮取到的全部东西。"""

    workspace: Workspace
    meters: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    meter_source: str = ""
    calls_truncated: bool = False
    retention_days: Optional[int] = None
    subscription_start_ms: Optional[int] = None

    @property
    def ok(self) -> bool:
        return bool(self.meters) or bool(self.calls)

    def meter(self, window: str) -> Optional[Meter]:
        for m in self.meters:
            if m.window == window:
                return m
        return None

    @property
    def meters_by_window(self) -> dict:
        return {m.window: m for m in self.meters}


@dataclass
class Snapshot:
    """一次「取数」的全部产出 —— 内存里的，用完即弃。

    ``start_ms`` / ``end_ms`` 是这次要的明细时间窗；``workspaces`` 每个都带着
    自己的额度和明细；``balances`` 是账户级的 DeepSeek 余额（与工作区无关）。
    """

    generated_ms: int
    end_ms: int
    start_ms: Optional[int] = None
    workspaces: list = field(default_factory=list)
    balances: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    multiplier: float = 1.0
    multiplier_source: str = ""


def as_float(value: Any) -> Optional[float]:
    """接口有时把数字写成字符串（micro-cents 就是），两种都吃。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def as_int(value: Any) -> Optional[int]:
    f = as_float(value)
    return None if f is None else int(f)


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_text(value: Any) -> str:
    """字符串原样；对象/数组存成 JSON 文本；其余给空串（``finish`` 就是这种）。"""
    if isinstance(value, str):
        return value
    if value is None or value == "":
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return str(value)
