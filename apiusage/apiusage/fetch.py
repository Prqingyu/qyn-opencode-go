"""取数层：一次「取数」产出一个**内存里的** Snapshot，用完即弃。

刻意没有数据库、没有游标、没有落盘。想要哪段时间的明细，就把那段窗口要下来；
上游自己保留 30 天，所以「历史」是问接口要的，不是自己攒的。
"""

from __future__ import annotations

from typing import Callable, Optional

from . import config, credentials, providers
from .model import Snapshot, WorkspaceData
from .timeutil import now_ms


def gather(
    creds: Optional[credentials.Credentials] = None,
    days: Optional[float] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    with_calls: bool = True,
    only_workspaces: Optional[list] = None,
    use_export: bool = True,
    page_limit: Optional[int] = None,
    log: Optional[Callable] = None,
) -> Snapshot:
    """从所有已登记的数据源取一次数。

    ``days`` 给了就算出 ``start_ms``；不给就用 ``config.DEFAULT_DAYS``。
    多工作区是天然的：每个 (档案, 工作区) 各取一份，互不影响。
    """
    creds = creds or credentials.load()
    end = end_ms or now_ms()
    if start_ms is None:
        span_days = config.DEFAULT_DAYS if days is None else days
        start_ms = max(0, end - int(span_days * 86_400_000)) if span_days else 0

    snapshot = Snapshot(generated_ms=now_ms(), start_ms=start_ms, end_ms=end)
    ctx = providers.CollectContext(
        snapshot=snapshot,
        creds=creds,
        start_ms=start_ms,
        end_ms=end,
        with_calls=with_calls,
        only_workspaces=only_workspaces,
        use_export=use_export,
        page_limit=page_limit,
        log=log or (lambda message: None),
    )
    for source in providers.SOURCES:
        try:
            source.collect(ctx)
        except Exception as exc:  # noqa: BLE001 - 一个源坏了不该拖垮别的源
            ctx.warn(f"{source.label} 取数失败：{type(exc).__name__}: {exc}")
    snapshot.multiplier, snapshot.multiplier_source = resolve_multiplier(creds)
    return snapshot


def resolve_multiplier(creds: Optional[credentials.Credentials] = None) -> tuple:
    """额度倍率（活动把标称额度放大的那个数）。

    没有持久化的采样序列，就没有「历史差分」可算 —— 所以这里只认显式配置：
    默认 1.0（不做折算），要折算就用 ``APIUSAGE_QUOTA_MULTIPLIER=1.3333``。
    ``tools/reconcile.py`` 可以在一个进程里做两点采样，把这个数实测出来。
    """
    if config.QUOTA_MULTIPLIER > 0:
        return config.QUOTA_MULTIPLIER, "配置"
    return 1.0, ""


def measure(
    creds: Optional[credentials.Credentials] = None,
    seconds: float = 60.0,
    only_workspaces: Optional[list] = None,
    log: Optional[Callable] = None,
) -> dict:
    """**两点采样**：现在量一次额度，等一会儿再量一次，中间那段明细也算出来。

    这是「不落盘」之后替代历史库的办法 —— 以前靠库里攒的时间序列算燃烧速率和
    额度倍率，现在改成现场测：等 ``seconds`` 秒即可，不需要任何额外文件。

    返回 ``{"rates": {ws_id: {window: $/小时}}, "multipliers": {ws_id: 倍率}, ...}``。
    """
    import time as _time

    creds = creds or credentials.load()
    before = gather(creds, with_calls=False, only_workspaces=only_workspaces, log=log)
    t0 = now_ms()
    _time.sleep(max(0.0, seconds))
    t1 = now_ms()
    after = gather(creds, start_ms=t0, end_ms=t1, with_calls=True,
                   only_workspaces=only_workspaces, log=log)

    span_hours = (t1 - t0) / 3_600_000.0
    rates: dict = {}
    multipliers: dict = {}
    for data in after.workspaces:
        key = data.workspace.id
        prev = next((b for b in before.workspaces if b.workspace.id == key), None)
        if prev is None or span_hours <= 0:
            continue
        per_window = {}
        for meter in data.meters:
            old = prev.meter(meter.window)
            if old is None:
                continue
            delta = meter.used_usd - old.used_usd
            if delta < 0:                    # 期间重置过，这段差值没有意义
                continue
            per_window[meter.window] = delta / span_hours
        rates[key] = per_window

        cost = sum(c.cost_usd or 0.0 for c in data.calls)
        now_month = data.meter("month")
        old_month = prev.meter("month")
        if now_month and old_month:
            month_delta = now_month.used_usd - old_month.used_usd
            if month_delta > 1e-9 and cost > 0:
                multipliers[key] = cost / month_delta
    return {"rates": rates, "multipliers": multipliers,
            "seconds": seconds, "start_ms": t0, "end_ms": t1}


def workspace_ids(snapshot: Snapshot) -> list:
    return [w.workspace.id for w in snapshot.workspaces]


def find_workspace(snapshot: Snapshot, key: str) -> Optional[WorkspaceData]:
    """按 id、name 或 label 找一个工作区（大小写不敏感，支持前缀）。"""
    key_lower = key.lower()
    for data in snapshot.workspaces:
        candidates = (data.workspace.id, data.workspace.name, data.workspace.short)
        if any(c and c.lower() == key_lower for c in candidates):
            return data
    for data in snapshot.workspaces:
        if data.workspace.id.lower().startswith(key_lower):
            return data
    return None
