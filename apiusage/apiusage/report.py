"""报告：把内存里的 Snapshot 渲染成 text / md / json。

**不读数据库** —— 数据全部来自这一轮取回来的 Snapshot。多工作区是天然支持的：
先按工作区分节，工作区多于一个时再给一段合计。
"""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import aggregate, gates
from .model import Balance, Snapshot, Workspace, WorkspaceData
from .timeutil import fmt_local, humanize_ago, humanize_delta, humanize_span, reset_phrase

WINDOW_ORDER = ("fiveHour", "week", "month")
WINDOW_LABEL = {"fiveHour": "5 小时", "week": "本周", "month": "本月"}


def window_label(window: str) -> str:
    return WINDOW_LABEL.get(window, window)


def _nan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def money_usd(value: Optional[float], digits: Optional[int] = None) -> str:
    if _nan(value):
        return "-"
    if digits is None:
        digits = 4 if abs(value) < 1 else 2
    return f"${value:,.{digits}f}"


def money_cny(value: Optional[float]) -> str:
    return "-" if _nan(value) else f"¥{value:,.2f}"


def percent(value: Optional[float], digits: int = 1) -> str:
    return "-" if _nan(value) else f"{value:.{digits}f}%"


def int_fmt(value: Optional[int]) -> str:
    return "-" if value is None else f"{value:,}"


def width(text: str) -> int:
    """终端/微信里的显示宽度（中文按 2 算）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, target: int, align: str = "left") -> str:
    gap = max(0, target - width(text))
    return text + " " * gap if align == "left" else " " * gap + text


def _ago_phrase(ms_past: Optional[int], ref_ms: Optional[int]) -> str:
    ago = humanize_ago(ms_past, ref_ms)
    return ago if ago in ("刚刚", "未来", "-") else f"{ago}前"


# ────────────────────────────────────────────────────────────── 视图 ──────


@dataclass
class WindowView:
    window: str
    label: str
    limit_usd: float = 0.0
    used_usd: float = 0.0
    remaining_usd: float = 0.0
    remaining_percent: float = float("nan")
    effective_percent: float = float("nan")
    blocked_by: str = ""
    resets_at_ms: Optional[int] = None
    reset_in: str = ""
    burn_usd_per_hour: Optional[float] = None
    hours_left: Optional[float] = None
    projection: str = ""
    real_limit_usd: float = 0.0
    real_remaining_usd: float = 0.0


@dataclass
class WorkspaceReport:
    workspace: Workspace
    windows: list = field(default_factory=list)
    meter_source: str = ""
    rollup: aggregate.Rollup = field(default_factory=aggregate.Rollup)
    errors: list = field(default_factory=list)
    retention_days: Optional[int] = None
    tail_window: str = ""

    @property
    def open_windows(self) -> list:
        """被上层压住、实际花不出去的那些。"""
        return [w for w in self.windows if w.blocked_by]


@dataclass
class Report:
    label: str
    start_ms: Optional[int]
    end_ms: int
    generated_ms: int
    workspaces: list = field(default_factory=list)
    balances: list = field(default_factory=list)
    multiplier: float = 1.0
    multiplier_source: str = ""
    warnings: list = field(default_factory=list)
    alerts: list = field(default_factory=list)

    @property
    def multi(self) -> bool:
        return len(self.workspaces) > 1

    @property
    def combined(self) -> aggregate.Stats:
        return aggregate.merge_stats(w.rollup.stats for w in self.workspaces)

    @property
    def combined_days(self) -> list:
        return aggregate.merge_days(w.rollup for w in self.workspaces)

    @property
    def totals_ok(self) -> bool:
        return bool(self.workspaces or self.balances)


# ────────────────────────────────────────────────────────────── 构建 ──────


def build_windows(data: WorkspaceData, multiplier: float = 1.0,
                  rates: Optional[dict] = None) -> list:
    """额度窗口视图 —— 套娃 gate 与（可选的）燃烧速率都在这里定性。"""
    meters = data.meters_by_window
    out = []
    for meter in data.meters:
        blocked = gates.blocking_window(meters, meter.window) or ""
        pct = meter.remaining_percent
        reset_ms = gates.effective_reset_ms(meters, meter.window)

        rate = (rates or {}).get(meter.window)
        hours_left = None
        projection = ""
        remaining = meter.remaining_usd
        if blocked:
            projection = f"被「{window_label(blocked)}」压住，实际花不出去"
        elif remaining <= 0:
            projection = "已用尽"
        elif rate:
            hours_left = remaining / rate
            if reset_ms and hours_left * 3_600_000 > (reset_ms - data_now_ms()):
                projection = "按当前速率撑不到重置，会在重置前用完"
            else:
                projection = f"按当前速率还能用约 {humanize_span(hours_left * 3600)}"
        elif rate == 0.0:
            projection = "近期无消耗"

        out.append(WindowView(
            window=meter.window,
            label=meter.label,
            limit_usd=meter.limit_usd,
            used_usd=meter.used_usd,
            remaining_usd=remaining,
            remaining_percent=pct,
            effective_percent=0.0 if blocked else pct,
            blocked_by=blocked,
            resets_at_ms=reset_ms,
            reset_in=reset_phrase(reset_ms),
            burn_usd_per_hour=rate,
            hours_left=hours_left,
            projection=projection,
            real_limit_usd=meter.limit_usd * multiplier,
            real_remaining_usd=remaining * multiplier,
        ))
    return out


def data_now_ms() -> int:
    from .timeutil import now_ms
    return now_ms()


PERIOD_LABEL = {"today": "今天", "week": "本周", "month": "本月", "all": "全部历史",
                "days": "最近若干天"}


def build(snapshot: Snapshot, label: str = "自定义区间",
          rates: Optional[dict] = None) -> Report:
    """``rates`` 是 ``{workspace_id: {window: $/小时}}``（来自 :func:`apiusage.fetch.measure`）。"""
    report = Report(
        label=label,
        start_ms=snapshot.start_ms,
        end_ms=snapshot.end_ms,
        generated_ms=snapshot.generated_ms,
        balances=list(snapshot.balances),
        multiplier=snapshot.multiplier,
        multiplier_source=snapshot.multiplier_source,
        warnings=list(snapshot.warnings),
    )
    for data in snapshot.workspaces:
        report.workspaces.append(WorkspaceReport(
            workspace=data.workspace,
            windows=build_windows(data, snapshot.multiplier,
                                  (rates or {}).get(data.workspace.id)),
            meter_source=data.meter_source,
            rollup=aggregate.rollup(data.calls),
            errors=list(data.errors),
            retention_days=data.retention_days,
            tail_window=gates.worst_window(data.meters_by_window) or "",
        ))
    report.alerts = evaluate_alerts(report)
    return report


def evaluate_alerts(report: Report) -> list:
    """现场判断提醒 —— **不留状态**，所以每次跑都是「当前是不是有问题」。

    （以前靠库里记状态来做「只在变化时说话」，现在不落盘了，就退回成
    「每次都报当前状态」，由使用者决定要不要看。）
    """
    out = []
    for w in report.workspaces:
        for view in w.windows:
            if view.blocked_by or _nan(view.remaining_percent):
                continue
            if view.remaining_usd <= 0:
                out.append(f"{w.workspace.short} {view.label}额度已用尽（{view.reset_in}）")
            elif view.remaining_percent <= 15:
                out.append(f"{w.workspace.short} {view.label}额度仅剩 "
                           f"{view.remaining_percent:.1f}%（{view.reset_in}）")
    for balance in report.balances:
        if not balance.usable:
            continue
        if balance.total <= 0:
            out.append(f"DeepSeek 余额已耗尽（{balance.account}）")
        elif balance.total <= 5:
            out.append(f"DeepSeek 余额仅剩 {money_cny(balance.total)}（{balance.account}）")
    return out


# ────────────────────────────────────────────────────────────── 渲染 ──────


def _period_text(report: Report) -> str:
    start = "最早记录" if report.start_ms is None else fmt_local(report.start_ms, with_seconds=False)
    return f"{start} 至 {fmt_local(report.end_ms, with_seconds=False)}"


def _window_lines(view: WindowView, boosted: bool) -> list:
    lines = [
        f"  {pad(view.label, 7)}{money_usd(view.limit_usd)} 限额"
        + (f"（折合真实 {money_usd(view.real_limit_usd)}）" if boosted else "")
        + f"  已用 {money_usd(view.used_usd)}"
        f"  剩余 {money_usd(view.remaining_usd)} ({percent(view.remaining_percent)})"
        + (f" ≈ 真实 {money_usd(view.real_remaining_usd)}" if boosted else "")
        + f"  {view.reset_in}"
    ]
    if view.projection:
        lines.append(f"          {view.projection}")
    return lines


def _stats_lines(stats: aggregate.Stats) -> list:
    lines = [
        f"  调用 {int_fmt(stats.calls)} 次  成功 {int_fmt(stats.succeeded)}"
        f" / 失败 {int_fmt(stats.failed)}  成功率 {percent(stats.success_rate)}",
        f"  费用 {money_usd(stats.cost_usd)}"
        f"（峰 {money_usd(stats.peak_cost)} / 谷 {money_usd(stats.offpeak_cost)}）"
        f"  平均 {money_usd(stats.avg_cost_usd, 4)}/次",
        f"  Token 合计 {int_fmt(stats.total_tokens)}"
        f"（输入 {int_fmt(stats.input_tokens)} / 输出 {int_fmt(stats.output_tokens)}"
        f" / 推理 {int_fmt(stats.reasoning_tokens)}）",
        f"  缓存读 {int_fmt(stats.cache_read_tokens)} / 写 {int_fmt(stats.cache_write_tokens)}"
        f"  缓存命中率 {percent(stats.cache_hit_rate)}",
    ]
    if stats.cache_write_1h_tokens:
        lines.append(f"  1 小时缓存写 {int_fmt(stats.cache_write_1h_tokens)}")
    if stats.avg_duration_ms is not None:
        line = f"  平均耗时 {stats.avg_duration_ms / 1000:.2f}s"
        if stats.avg_ttft_ms is not None:
            line += f"  平均首字 {stats.avg_ttft_ms / 1000:.2f}s"
        lines.append(line)
    return lines


def render_text(report: Report) -> str:
    """纯文本（微信友好）：没有 Markdown 记号，靠缩进对齐。"""
    out = []
    add = out.append
    add("AI API 用量报告")
    add(f"周期：{report.label}，{_period_text(report)}")
    add(f"生成：{fmt_local(report.generated_ms)}")
    if report.multi:
        add(f"工作区 {len(report.workspaces)} 个："
            + "，".join(w.workspace.short for w in report.workspaces))
    add("")

    boosted = report.multiplier > 1.02
    for data in report.workspaces:
        head = f"【{data.workspace.label}】" if report.multi else "【额度与余额】"
        add(head)
        if data.windows:
            add(f"OpenCode GO（凭据：{data.meter_source or '未知'}）")
            if boosted:
                add(f"  额度倍率 ×{report.multiplier:.2f}（{report.multiplier_source}）"
                    f" —— 池子每 $1 等于真实调用费 ${report.multiplier:.2f}")
            for view in data.windows:
                for line in _window_lines(view, boosted):
                    add(line)
        else:
            reason = f"（{data.meter_source}）" if data.meter_source else ""
            add(f"  额度：没有取到{reason}")
        for message in data.errors:
            add(f"  · {message}")
        add("")

        add(f"【调用情况{'·' + data.workspace.short if report.multi else ''}】")
        if data.rollup.stats.calls:
            for line in _stats_lines(data.rollup.stats):
                add(line)
        else:
            add("  本周期没有明细记录")
        add("")

        if data.rollup.models:
            add(f"【按模型{'·' + data.workspace.short if report.multi else ''}】")
            name_w = max(8, min(28, max(width(m.model) for m in data.rollup.models)))
            for m in data.rollup.models:
                add(f"  {pad(m.model, name_w)}  {m.calls:>5} 次  成功 {percent(m.success_rate, 0):>5}"
                    f"  费用 {money_usd(m.cost_usd):>10}  Token {int_fmt(m.total_tokens):>12}")
            add("")

        if data.rollup.finishes:
            add("【结束原因】")
            add("  " + "   ".join(f"{reason} {n}" for reason, n in data.rollup.finishes[:6]))
            add("")

    if report.multi:
        add("【合计】")
        for line in _stats_lines(report.combined):
            add(line)
        add("")

    days = report.combined_days if report.multi else (
        report.workspaces[0].rollup.days if report.workspaces else [])
    if len(days) > 1:
        add("【按日】")
        for d in days:
            tail = f"  失败 {d.failed}" if d.failed else ""
            add(f"  {d.day}  {d.calls:>5} 次  费用 {money_usd(d.cost_usd):>10}"
                f"  Token {int_fmt(d.tokens):>12}{tail}")
        add("")

    first = report.workspaces[0] if report.workspaces else None
    if first and first.rollup.statuses and first.rollup.stats.failed:
        add("【失败明细】")
        for s in first.rollup.statuses[:8]:
            if s["status_code"] == 200 and not s["error_code"]:
                continue
            add(f"  HTTP {s['status_code']}  {s['error_code'] or '-'}  {s['n']} 次")
        add("")

    for balance in report.balances:
        if balance.error:
            add(f"DeepSeek 余额 取数失败：{balance.error}")
            continue
        add(f"DeepSeek 余额 {money_cny(balance.total)}"
            + (f"（赠送 {money_cny(balance.granted)} / 充值 {money_cny(balance.topped_up)}）"
               if balance.granted is not None and balance.topped_up is not None else "")
            + (f"  另有 " + "，".join(f"{i.currency} {money_cny(i.total)}"
                                     for i in balance.infos if i.currency != balance.currency)
               if any(i.currency != balance.currency for i in balance.infos) else ""))
    if report.balances:
        add("")

    if report.alerts:
        add("【提醒】")
        for alert in report.alerts[:10]:
            add(f"  ! {alert}")
        add("")

    add("【说明】")
    retention = next((w.retention_days for w in report.workspaces if w.retention_days), None)
    if retention:
        add(f"  明细接口保留 {retention} 天；数据每次现场从接口取，不做本地保存")
    else:
        add("  数据每次现场从接口取，不做本地保存")
    for warning in report.warnings:
        add(f"  注：{warning}")
    return "\n".join(out)


def render_markdown(report: Report) -> str:
    out = []
    add = out.append
    add(f"# AI API 用量报告（{report.label}）")
    add("")
    add(f"- 周期：{_period_text(report)}")
    add(f"- 生成：{fmt_local(report.generated_ms)}")
    if report.multi:
        add(f"- 工作区：{'、'.join(w.workspace.label for w in report.workspaces)}")
    add("")

    boosted = report.multiplier > 1.02
    if boosted:
        add(f"> 额度倍率 **×{report.multiplier:.2f}**（{report.multiplier_source}）："
            f"池子里的 $1 等于真实调用费 ${report.multiplier:.2f}。")
        add("")

    for data in report.workspaces:
        add(f"## {data.workspace.label}" if report.multi else "## 额度与余额")
        add("")
        if data.windows:
            add("| 窗口 | 限额（真实） | 已用 | 剩余 | 真实剩余 | 剩余% | 重置 |")
            add("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
            for view in data.windows:
                pct = percent(view.remaining_percent)
                if view.blocked_by:
                    pct += f"（被 {window_label(view.blocked_by)} 压住）"
                limit = money_usd(view.limit_usd)
                real = "—"
                if boosted:
                    limit += f"<br/>（真实 {money_usd(view.real_limit_usd)}）"
                    real = money_usd(view.real_remaining_usd)
                add(f"| {view.label} | {limit} | {money_usd(view.used_usd)} | "
                    f"{money_usd(view.remaining_usd)} | {real} | {pct} | {view.reset_in} |")
        else:
            add("额度：没有取到。")
        add("")
        for message in data.errors:
            add(f"- ⚠️ {message}")
        if data.errors:
            add("")

        if data.rollup.stats.calls:
            add("### 调用情况")
            add("")
            add("| 次数 | 成功 | 失败 | 费用 | 峰 | 谷 | Token | 缓存命中率 |")
            add("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
            s = data.rollup.stats
            add(f"| {int_fmt(s.calls)} | {int_fmt(s.succeeded)} | {int_fmt(s.failed)}"
                f" | {money_usd(s.cost_usd)} | {money_usd(s.peak_cost)}"
                f" | {money_usd(s.offpeak_cost)} | {int_fmt(s.total_tokens)}"
                f" | {percent(s.cache_hit_rate)} |")
            add("")
            add("| 模型 | 调用 | 成功率 | 费用 | 输入 | 输出 | 推理 | 缓存读 |")
            add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
            for m in data.rollup.models:
                add(f"| `{m.model}` | {int_fmt(m.calls)} | {percent(m.success_rate, 0)}"
                    f" | {money_usd(m.cost_usd)} | {int_fmt(m.input_tokens)}"
                    f" | {int_fmt(m.output_tokens)} | {int_fmt(m.reasoning_tokens)}"
                    f" | {int_fmt(m.cache_read_tokens)} |")
            add("")

    if len(report.combined_days) > 1:
        add("## 按日")
        add("")
        add("| 日期 | 调用 | 失败 | 费用 | Token |")
        add("| --- | ---: | ---: | ---: | ---: |")
        for d in report.combined_days:
            add(f"| {d.day} | {int_fmt(d.calls)} | {int_fmt(d.failed)}"
                f" | {money_usd(d.cost_usd)} | {int_fmt(d.tokens)} |")
        add("")

    if report.multi:
        add("## 合计")
        add("")
        for line in _stats_lines(report.combined):
            add(f"- {line.strip()}")
        add("")

    for balance in report.balances:
        add(f"**DeepSeek 余额**：{money_cny(balance.total)}")
    if report.balances:
        add("")

    if report.alerts:
        add("## 提醒")
        add("")
        for alert in report.alerts:
            add(f"- {alert}")
        add("")

    add("## 说明")
    add("")
    add("- 数据每次现场从接口取，**不做本地保存**（没有数据库、没有状态文件）")
    retention = next((w.retention_days for w in report.workspaces if w.retention_days), None)
    if retention:
        add(f"- 明细接口保留 {retention} 天，更早的取不到")
    for warning in report.warnings:
        add(f"- {warning}")
    add("")
    return "\n".join(out)


def render_json(report: Report) -> str:
    payload = {
        "label": report.label,
        "start_ms": report.start_ms,
        "end_ms": report.end_ms,
        "generated_ms": report.generated_ms,
        "multiplier": report.multiplier,
        "multiplier_source": report.multiplier_source,
        "balances": [asdict(b) for b in report.balances],
        "alerts": report.alerts,
        "warnings": report.warnings,
        "workspaces": [
            {
                "id": w.workspace.id,
                "name": w.workspace.name,
                "profile": w.workspace.profile,
                "meter_source": w.meter_source,
                "retention_days": w.retention_days,
                "errors": w.errors,
                "windows": [asdict(view) for view in w.windows],
                "stats": asdict(w.rollup.stats),
                "models": [asdict(m) for m in w.rollup.models],
                "days": [asdict(d) for d in w.rollup.days],
                "statuses": w.rollup.statuses,
                "finishes": [{"reason": r, "n": n} for r, n in w.rollup.finishes],
            }
            for w in report.workspaces
        ],
    }
    payload["totals"] = asdict(report.combined) if report.workspaces else {}
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


RENDERERS = {"text": render_text, "md": render_markdown, "json": render_json}


def render(report: Report, fmt: str = "text") -> str:
    try:
        renderer = RENDERERS[fmt]
    except KeyError as exc:
        raise ValueError(f"未知输出格式: {fmt}（可选 {'/'.join(RENDERERS)}）") from exc
    return renderer(report)
