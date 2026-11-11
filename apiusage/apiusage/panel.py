"""大屏面板接口：给别的程序（另一个 Agent / 状态墙）消费的**稳定 JSON 契约**。

设计原则 —— 大屏那边不该重新实现一遍我们的业务规则：

* **数据与判断都在这边做完** —— 剩下多少、实际能不能花（套娃 gate）、该显示什么
  等级（ok / warn / critical）、倒计时文案，全部给好；消费方只管画。
* **契约稳定** —— 顶层带 ``schema`` 版本号；字段只增不改名。契约文档见
  ``docs/INTERFACE.md``。
* **失败也要能画** —— 某个数据源挂了不会抛异常，而是在 ``warnings`` / ``errors``
  里给原因、等级给 ``unknown``，屏上照样有东西可显示。

命令行：``python -m apiusage panel``（``--repeat`` 可以持续吐 NDJSON）。
"""

from __future__ import annotations

import json
from typing import Optional

from . import config, gates
from .model import Balance, Snapshot, WorkspaceData
from .timeutil import fmt_local, reset_phrase
from .report import WINDOW_LABEL, money_cny, money_usd, percent, window_label

SCHEMA = "apiusage.panel/1"
SHORT_LABEL = {"fiveHour": "5H", "week": "周", "month": "月"}

# 等级：数字越大越严重。unknown 不算「好」，但也不能把它当成告警去闪屏，
# 所以它排在 ok 同一档，另外用 ok=false 表达「没数据」。
LEVEL_RANK = {"unknown": 0, "ok": 0, "warn": 1, "critical": 2}


def worst_level(levels) -> str:
    """取最严重的等级。

    **``unknown`` 只有在没有任何「实锤」等级时才胜出** —— 否则一个没订阅的工作区
    会把整块屏的等级拖成 unknown，看起来像全挂了。没数据是用 ``ok=false`` /
    ``has_go=false`` 表达的，不是用等级。
    """
    levels = [lv for lv in levels if lv]
    if not levels:
        return "unknown"
    ranked = [lv for lv in levels if lv != "unknown"]
    if not ranked:
        return "unknown"
    return max(ranked, key=lambda lv: LEVEL_RANK.get(lv, 0))


def _nan(value) -> bool:
    return value is None or (isinstance(value, float) and value != value)


def meter_level(meter, blocked: bool = False) -> str:
    """一个额度窗口该显示什么等级。

    被上层压住（``blocked``）是**最严重**的情况：池子里明明还有钱，却一分都花不出去。
    """
    if blocked:
        return "critical"
    if _nan(meter.remaining_percent):
        return "unknown"
    if meter.remaining_usd <= 0 or meter.remaining_percent <= 0:
        return "critical"
    if meter.remaining_percent <= config.GO_LOW_PERCENT:
        return "warn"
    return "ok"


def balance_level(balance: Balance) -> str:
    if not balance.usable:
        return "unknown"
    if balance.total <= 0:
        return "critical"
    if balance.total <= config.BALANCE_LOW_CNY:
        return "warn"
    return "ok"


def meter_view(meters_by_window: dict, meter) -> dict:
    blocked = gates.blocking_window(meters_by_window, meter.window) or None
    usable = 0.0 if blocked else meter.remaining_percent
    return {
        "window": meter.window,
        "label": meter.label,
        "short": SHORT_LABEL.get(meter.window, meter.window),
        "limit_usd": round(meter.limit_usd, 6),
        "used_usd": round(meter.used_usd, 6),
        "remaining_usd": round(meter.remaining_usd, 6),
        "remaining_percent": None if _nan(meter.remaining_percent)
                             else round(meter.remaining_percent, 2),
        # 「实际能不能花」——被上层压住时是 0，大屏应该按这个上色
        "usable_percent": None if _nan(usable) else round(usable, 2),
        "blocked_by": blocked,
        "blocked_by_label": window_label(blocked) if blocked else None,
        "resets_at_ms": meter.resets_at_ms,
        "reset_in": reset_phrase(meter.resets_at_ms),
        "level": meter_level(meter, bool(blocked)),
    }


def balance_view(balance: Balance) -> dict:
    """DeepSeek 官方余额。``total`` 是人民币那条的快捷方式，全部币种在 ``infos``。"""
    return {
        "account": balance.account or "deepseek",
        "currency": balance.currency,
        "total": None if _nan(balance.total) else round(balance.total, 2),
        "granted": None if _nan(balance.granted) else round(balance.granted, 2),
        "topped_up": None if _nan(balance.topped_up) else round(balance.topped_up, 2),
        # 官方自己的「账户是否可用」标志；null = 报文没给
        "is_available": balance.is_available,
        # 全部币种，一条一个 —— 别只认 CNY
        "infos": [
            {
                "currency": item.currency,
                "total": None if _nan(item.total) else round(item.total, 2),
                "granted": None if _nan(item.granted) else round(item.granted, 2),
                "topped_up": None if _nan(item.topped_up) else round(item.topped_up, 2),
            }
            for item in balance.infos
        ],
        "usable": balance.usable,
        "low_threshold": config.BALANCE_LOW_CNY,
        # 取数失败时非空：大屏该显示「取数失败」而不是把这格删掉
        "error": balance.error or None,
        "level": balance_level(balance),
    }


def workspace_view(data: WorkspaceData) -> dict:
    by_window = data.meters_by_window
    meters = [meter_view(by_window, m) for m in data.meters]
    worst = gates.worst_window(by_window) or None
    usable = [m["usable_percent"] for m in meters if m["usable_percent"] is not None]
    return {
        "id": data.workspace.id,
        "name": data.workspace.name,
        "label": data.workspace.short,
        "profile": data.workspace.profile,
        "has_go": bool(data.meters),
        "ok": data.ok,
        "level": worst_level([m["level"] for m in meters]) if meters else "unknown",
        "source": data.meter_source,
        "retention_days": data.retention_days,
        "worst_window": worst,
        "worst_window_label": window_label(worst) if worst else None,
        "min_usable_percent": None if not usable else round(min(usable), 2),
        "meters": meters,
        "errors": list(data.errors),
    }


def _headline(workspaces: list, balances: list) -> str:
    """一句话说清「现在最该看什么」。

    取**剩余金额最少**的那个窗口 —— 必须和 ``worst_window`` 用同一把尺子，
    否则面板会自相矛盾（左边说最紧的是 5 小时，右边标题却在说本月）。
    三个窗口天花板不同，所以比金额、不比百分比。
    """
    picks = []
    for ws in workspaces:
        for meter in ws["meters"]:
            if meter["window"] == ws["worst_window"]:
                picks.append((meter["remaining_usd"], ws, meter))
    if picks:
        remaining, ws, meter = min(picks, key=lambda item: item[0])
        prefix = f"{ws['label']} " if len(workspaces) > 1 else ""
        if meter["remaining_usd"] <= 0:
            # 最紧的那个已经见底 —— 大屏上最该看到的就是这句
            return f"{prefix}{meter['label']} 已用尽（{meter['reset_in']}）"
        if meter["blocked_by_label"]:
            return f"{prefix}额度被「{meter['blocked_by_label']}」压住，实际不可用"
        pct = f"（{meter['remaining_percent']:g}%）" if meter["remaining_percent"] is not None else ""
        return f"{prefix}{meter['label']} 剩余 {money_usd(remaining)}{pct}"
    for balance in balances:
        if balance["total"] is not None:
            return f"DeepSeek 余额 {money_cny(balance['total'])}"
    return "没有取到数据"


def build(
    snapshot: Snapshot,
    *,
    days: Optional[float] = None,
    label: str = "当前",
    with_stats: bool = True,
    totals: Optional[dict] = None,
) -> dict:
    """把 Snapshot 变成大屏要的那份 JSON。

    ``totals`` 可选（形如 ``{"calls":…, "cost_usd":…, "tokens":…}``）——
    面板默认只要额度，不取明细，因为大屏是高频刷的。
    """
    workspaces = [workspace_view(d) for d in snapshot.workspaces]
    balances = [balance_view(b) for b in snapshot.balances]

    payload = {
        "schema": SCHEMA,
        "generated_at_ms": snapshot.generated_ms,
        "generated_at": fmt_local(snapshot.generated_ms),
        # 消费方据此判断「这屏数据还能不能用」；超过就显示为陈旧
        "fresh_for_ms": int(config.PANEL_FRESH_MS),
        "window": {
            "start_ms": snapshot.start_ms,
            "end_ms": snapshot.end_ms,
            "days": days,
            "label": label,
            "with_calls": with_stats,
        },
        "thresholds": {
            "go_low_percent": config.GO_LOW_PERCENT,
            "balance_low_cny": config.BALANCE_LOW_CNY,
        },
        "level": worst_level([w["level"] for w in workspaces]
                             + [b["level"] for b in balances]),
        "headline": _headline(workspaces, balances),
        "workspaces": workspaces,
        "balances": balances,
        "alerts": [],
        "warnings": list(snapshot.warnings),
        "totals": totals or {},
    }

    # 提醒用同一套阈值现场判断（不留状态）
    alerts = []
    for ws in workspaces:
        for meter in ws["meters"]:
            if meter["blocked_by_label"]:
                continue
            if meter["level"] == "critical":
                alerts.append(f"{ws['label']} {meter['label']}额度已用尽（{meter['reset_in']}）")
            elif meter["level"] == "warn":
                alerts.append(f"{ws['label']} {meter['label']}额度仅剩 "
                              f"{percent(meter['usable_percent'])}（{meter['reset_in']}）")
    for balance in balances:
        if balance["error"]:
            alerts.append(f"DeepSeek 余额取数失败：{balance['error']}")
            continue
        if balance["level"] == "critical":
            alerts.append(f"DeepSeek 余额已耗尽（{balance['account']}）")
        elif balance["level"] == "warn":
            alerts.append(f"DeepSeek 余额仅剩 {money_cny(balance['total'])}"
                          f"（{balance['account']}）")
    payload["alerts"] = alerts
    return payload


def render(payload: dict) -> str:
    """给程序用的那份：单行 JSON（NDJSON 友好）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def render_text(payload: dict) -> str:
    """给人在终端瞄一眼的紧凑版（不是大屏主路径）。"""
    out = [f"AI API 面板  {payload['generated_at']}  [{payload['level']}]"]
    out.append(f"  {payload['headline']}")
    for ws in payload["workspaces"]:
        if ws["meters"]:
            cells = "  ".join(
                f"{m['short']} {m['usable_percent']:g}%" if m["usable_percent"] is not None
                else f"{m['short']} -" for m in ws["meters"])
            # 「最紧」和「被压住」是两件事：前者是金额最少，后者是上层空了
            notes = []
            if ws["worst_window_label"]:
                notes.append(f"最紧 {ws['worst_window_label']}")
            squeezed = [m["blocked_by_label"] for m in ws["meters"] if m["blocked_by_label"]]
            if squeezed:
                notes.append("被" + "、".join(sorted(set(squeezed))) + "压住")
            tail = f"  ← {'；'.join(notes)}" if notes else ""
            out.append(f"  {ws['label']:<12} {cells}{tail}")
        else:
            out.append(f"  {ws['label']:<12} 无 GO 订阅（{ws['source']}）")
        for message in ws["errors"]:
            out.append(f"      · {message}")
    for balance in payload["balances"]:
        if balance["error"]:
            out.append(f"  {balance['account']:<12} 取数失败：{balance['error']}")
            continue
        extras = "  ".join(f"{i['currency']} {money_cny(i['total'])}" for i in balance["infos"]
                           if i["currency"] != balance["currency"])
        out.append(f"  {balance['account']:<12} {money_cny(balance['total'])}"
                   + (f"（另有 {extras}）" if extras else ""))
    for alert in payload["alerts"]:
        out.append(f"  ! {alert}")
    for warning in payload["warnings"]:
        out.append(f"  ? {warning}")
    return "\n".join(out)
