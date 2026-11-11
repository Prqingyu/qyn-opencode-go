"""命令行：``python -m apiusage <命令>``。

设计前提（按需求定的）：

* **不落盘** —— 没有数据库、没有游标、没有状态文件；每条命令现场取数、算完打印。
* **多工作区** —— 一个 Cookie 能看到该账号下所有工作区，``--workspace`` 挑一个，
  不给就是全部（报告会自动多分节 + 合计）。
"""

from __future__ import annotations

import argparse
import csv as _csv
import io
import json
import sys
from pathlib import Path
from typing import Optional

from . import (
    __version__,
    aggregate,
    config,
    credentials,
    fetch,
    gates,
    panel,
    providers,
    report as report_mod,
)
from .http import FetchError
from .model import Workspace
from .providers import opencode_go
from .timeutil import fmt_local, from_ms, now_ms, period_range, reset_phrase


def _print(lines) -> None:
    if isinstance(lines, str):
        lines = [lines]
    for line in lines:
        print(line)


def _ref_ms(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    from datetime import datetime

    from .timeutil import CN

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN)
    return int(dt.timestamp() * 1000)


def _window(args):
    """把 --today/--week/--month/--days/--since 统一成 (start_ms, end_ms, label)。"""
    ref = _ref_ms(getattr(args, "ref", None))
    if getattr(args, "since", None) or getattr(args, "until", None):
        start = _ref_ms(args.since) if args.since else None
        end = _ref_ms(args.until) if args.until else (ref or now_ms())
        return start, end, "自定义区间"
    period = getattr(args, "period", None)
    if period and period != "days":
        start, end = period_range(period, from_ms(ref) if ref else None)
        return start, end, report_mod.PERIOD_LABEL.get(period, period)
    days = getattr(args, "days", None)
    days = config.DEFAULT_DAYS if days is None else days
    end = ref or now_ms()
    start = max(0, end - int(days * 86_400_000)) if days else 0
    return start, end, f"最近 {days:g} 天"


def _selected_workspaces(args) -> Optional[list]:
    return list(args.workspace) if getattr(args, "workspace", None) else None


# ───────────────────────────────────────────────────────────── 命令 ──────


def cmd_panel(args) -> int:
    """大屏接口：输出稳定的 JSON 契约（契约见 docs/INTERFACE.md）。

    默认**不取明细**（大屏是高频刷的，明细又慢又贵）；``--with-stats`` 才带上用量。
    ``--repeat`` 会持续吐 NDJSON，一行一帧，方便消费方边收边刷新。
    """
    import time as _time

    creds = credentials.load()
    count = 0
    payload: dict = {}
    while True:
        snapshot = fetch.gather(
            creds,
            days=args.days,
            with_calls=args.with_stats,
            only_workspaces=_selected_workspaces(args),
        )
        totals = {}
        if args.with_stats:
            roll = aggregate.rollup(c for data in snapshot.workspaces for c in data.calls)
            totals = {"calls": roll.stats.calls, "succeeded": roll.stats.succeeded,
                      "failed": roll.stats.failed,
                      "cost_usd": round(roll.stats.cost_usd, 6),
                      "tokens": roll.stats.total_tokens}
        payload = panel.build(snapshot, days=args.days, label="当前",
                              with_stats=args.with_stats, totals=totals)
        print(panel.render_text(payload) if args.format == "text" else panel.render(payload),
              flush=True)
        count += 1
        if not args.repeat or (args.count and count >= args.count):
            break
        _time.sleep(args.repeat)
    return 0 if (payload.get("workspaces") or payload.get("balances")) else 1


def cmd_status(args) -> int:
    """看一眼当前状态（不取明细，快）。"""
    creds = credentials.load()
    snapshot = fetch.gather(creds, with_calls=False, only_workspaces=_selected_workspaces(args))
    fmt = args.format

    if fmt == "json":
        # 和 panel 共用同一份契约 —— 消费方只需要认一种形状
        print(json.dumps(panel.build(snapshot, label="当前"), ensure_ascii=False, indent=2))
        return 0 if snapshot.workspaces or snapshot.balances else 1

    if fmt == "pct":
        for data in snapshot.workspaces:
            prefix = f"{data.workspace.short}  " if len(snapshot.workspaces) > 1 else ""
            for meter in data.meters:
                blocked = gates.blocking_window(data.meters_by_window, meter.window)
                pct = 0.0 if blocked else meter.remaining_percent
                line = (f"{prefix}{meter.window}\t剩余 {pct:.1f}%\t已用 {100.0 - pct:.1f}%"
                        f"\t{reset_phrase(meter.resets_at_ms)}")
                if blocked:
                    line += f"\t受 {report_mod.window_label(blocked)} 限制，实际 0%"
                print(line)
        for balance in snapshot.balances:
            print(f"{balance.account}\t余额 ¥{balance.total:.2f}")
        return 0 if snapshot.workspaces or snapshot.balances else 1

    out = ["AI API 状态  " + fmt_local(snapshot.generated_ms)]
    for data in snapshot.workspaces:
        if len(snapshot.workspaces) > 1:
            out.append(f"● {data.workspace.label}")
        if data.meters:
            out.append(f"  OpenCode GO  凭据={data.meter_source}")
            for meter in data.meters:
                blocked = gates.blocking_window(data.meters_by_window, meter.window)
                flag = f"  ← 被{report_mod.window_label(blocked)}压住" if blocked else ""
                pct = 0.0 if blocked else meter.remaining_percent
                out.append(f"    {meter.label:<7} 剩余 ${meter.remaining_usd:.4f} ({pct:.1f}%)"
                           f"  已用 ${meter.used_usd:.4f} / ${meter.limit_usd:.2f}"
                           f"  {reset_phrase(meter.resets_at_ms)}{flag}")
            tail = gates.worst_window(data.meters_by_window)
            if tail:
                out.append(f"    最紧张的一层：{report_mod.window_label(tail)}")
        else:
            hint = f"（{data.meter_source}）" if data.meter_source else ""
            out.append(f"  {data.workspace.short} 额度：未取到{hint}")
        for message in data.errors:
            out.append(f"    · {message}")
    for balance in snapshot.balances:
        out.append(f"DeepSeek 余额  ¥{balance.total:,.2f}"
                   + (f"（赠送 ¥{balance.granted:,.2f} / 充值 ¥{balance.topped_up:,.2f}）"
                      if balance.granted is not None and balance.topped_up is not None else ""))
    for warning in snapshot.warnings:
        out.append(f"! {warning}")
    _print(out)
    return 0 if snapshot.workspaces or snapshot.balances else 1


def cmd_report(args) -> int:
    creds = credentials.load()
    start, end, label = _window(args)
    if args.period and args.period != "days":
        label = report_mod.PERIOD_LABEL.get(args.period, label)

    rates: dict = {}
    measured: Optional[dict] = None
    if args.watch:
        print(f"两点采样中（等 {args.watch:g} 秒，用来算燃烧速率和额度倍率）…",
              file=sys.stderr)
        measured = fetch.measure(creds, seconds=args.watch,
                                 only_workspaces=_selected_workspaces(args))
        rates = measured["rates"]

    snapshot = fetch.gather(creds, start_ms=start, end_ms=end,
                            with_calls=not args.no_calls,
                            only_workspaces=_selected_workspaces(args),
                            use_export=not args.no_export,
                            page_limit=args.pages)

    if measured and not config.QUOTA_MULTIPLIER:
        found = [v for v in measured["multipliers"].values() if 0.5 <= v <= 3.0]
        if found:
            # 现场实测的倍率只用于这一次渲染，不写任何地方
            snapshot.multiplier = sum(found) / len(found)
            snapshot.multiplier_source = "实测（两点采样）"

    report = report_mod.build(snapshot, label=label, rates=rates)
    text = report_mod.render(report, args.format)

    if args.out:
        Path(args.out).write_text(text + ("\n" if not text.endswith("\n") else ""),
                                  encoding="utf-8")
        print(f"已写出 {args.out}（报告文件，不是数据）")
    else:
        print(text)
    return 0 if report.totals_ok else 1


def cmd_workspaces(args) -> int:
    """列出能看到的工作区（靠 /console/api/orgs，不需要一个个试 x-org-id）。"""
    creds = credentials.load()
    if not creds.has_go:
        print("没有 OpenCode 凭据：先配 Key 或跑 `python -m apiusage cookie`", file=sys.stderr)
        return 2
    found = []
    for profile in creds.profiles:
        if not profile.usable:
            continue
        for workspace in opencode_go.discover_workspaces(profile):
            try:
                meters, source, _ = opencode_go.fetch_status(profile, workspace)
                detail = "，".join(
                    f"{m.label} {m.remaining_percent:.1f}%" for m in meters) or "无额度窗口"
            except FetchError as exc:
                detail = f"额度取不到（{exc}）"
                source = ""
            found.append((profile.name, workspace, source, detail))

    if args.format == "json":
        print(json.dumps([{
            "profile": name, "id": ws.id, "name": ws.name, "source": source, "detail": detail,
        } for name, ws, source, detail in found], ensure_ascii=False, indent=2))
        return 0
    if not found:
        print("没有发现任何工作区")
        return 1
    multi_profile = len({name for name, _, _, _ in found}) > 1
    for name, ws, source, detail in found:
        head = f"[{name}] " if multi_profile else ""
        print(f"{head}{ws.label}   `{ws.id}`")
        print(f"    {detail}")
    print()
    print("挑一个用：python -m apiusage report --days 7 --workspace " + found[0][1].id)
    return 0


def cmd_export(args) -> int:
    """把原始明细导出来（只看文件，不落库）。"""
    creds = credentials.load()
    profiles = [p for p in creds.profiles if p.complete]
    if not profiles:
        print("导出需要控制台 Cookie：先跑 `python -m apiusage cookie`", file=sys.stderr)
        return 2

    since = now_ms() - int(args.days * 86_400_000) if args.days else 0
    items: list = []
    csv_parts: list = []
    seen: set = set()
    pages = 0
    truncated = False

    for profile in profiles:
        for workspace in opencode_go.discover_workspaces(profile):
            if not (not args.workspace or workspace.id in args.workspace
                    or workspace.name in args.workspace):
                continue
            upper = None
            while pages < args.max_pages:
                try:
                    doc = opencode_go.fetch_export(profile, workspace, since, upper, args.format)
                except FetchError as exc:
                    if pages == 0:
                        print(f"导出失败：{exc}", file=sys.stderr)
                        return 1
                    print(f"  {workspace.short} 下一页失败（{exc}），已拿到的先留着",
                          file=sys.stderr)
                    break
                pages += 1
                content = doc.get("content") or ""
                truncated = bool(doc.get("truncated"))
                if not content:
                    break
                if args.format == "csv":
                    rows = list(_csv.DictReader(io.StringIO(content)))
                    csv_parts.append(content)
                    times = [int(r["startedAt"]) for r in rows if r.get("startedAt")]
                    count = doc.get("count") or len(rows)
                else:
                    try:
                        inner = json.loads(content)
                        rows = inner.get("items") if isinstance(inner, dict) else inner
                    except ValueError:
                        rows = []
                    rows = [r for r in (rows or []) if isinstance(r, dict)]
                    fresh = [r for r in rows if r.get("id") not in seen]
                    seen.update(r.get("id") for r in fresh)
                    items.extend(fresh)
                    times = [int(r["startedAt"]) for r in rows if r.get("startedAt")]
                    count = doc.get("count") or len(rows)
                print(f"  {workspace.short} 第 {pages} 页：{count} 条"
                      f"{'（还有更多）' if truncated else ''}", file=sys.stderr)
                if not times or not truncated:
                    break
                oldest = min(times)
                if oldest <= since:
                    break
                upper = oldest - 1

    out = Path(args.out or f"logs-export.{args.format}")
    if args.format == "csv":
        merged = "".join(csv_part for csv_part in csv_parts)
        out.write_text(merged, encoding="utf-8")
        total = sum(1 for line in merged.splitlines() if line.strip()) - 1
    else:
        out.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
        total = len(items)
    print(f"共 {pages} 页、{total} 条 → {out}")
    return 0


def cmd_doctor(args) -> int:
    ok_all = True
    print(f"apiusage {__version__}  ·  Python {sys.version.split()[0]}")
    print("模式：不落盘（每次现场取数，没有数据库/状态文件）")
    print()
    creds = credentials.load()
    print(f"凭据来源：{creds.sources or '（没有）'}")
    if creds.has_deepseek:
        print(f"  DeepSeek Key  {credentials.mask(creds.deepseek_key)}")
    for profile in creds.profiles:
        print(f"  登录档案 {profile.describe()}")
    print()

    for check in providers.checks(creds):
        mark = "OK  " if check.ok else ("WARN" if check.ok is None else "FAIL")
        if check.ok is False:
            ok_all = False
        print(f"[{mark}] {check.name}：{check.detail}")
        if check.hint:
            print(f"        {check.hint}")
    if not creds.has_go and not creds.has_deepseek:
        ok_all = False
    print()
    print("阈值：GO 低额度 %.0f%%  余额低 ¥%.2f" % (config.GO_LOW_PERCENT, config.BALANCE_LOW_CNY))
    print("额度倍率：%s" % (f"×{config.QUOTA_MULTIPLIER:g}（配置）" if config.QUOTA_MULTIPLIER
                          else "未折算（用 APIUSAGE_QUOTA_MULTIPLIER 或 report --watch 实测）"))
    return 0 if ok_all else 1


def cmd_cookie(args) -> int:
    text = args.text or ""
    if not text and args.from_file:
        text = Path(args.from_file).read_text(encoding="utf-8", errors="replace")
    elif not text:
        print("把控制台抓包内容粘进来（Ctrl-Z 回车 / Ctrl-D 结束）：")
        text = sys.stdin.read()

    scanned = credentials.scan_go_credentials(text)
    if not (scanned.auth or scanned.session or scanned.org or scanned.key):
        print("没识别出任何 GO 凭据 —— 一个字段都没保存", file=sys.stderr)
        return 2

    existing = credentials.load_go_cookie()
    merged = credentials.GoProfile(
        name="default",
        auth=scanned.auth or existing.auth,
        session=scanned.session or existing.session,
        org=scanned.org or existing.org,
        key=scanned.key or existing.key,
    )
    path = credentials.save_go_cookie(merged)
    print(f"已保存 → {path}")
    print("  auth    " + ("OK" if merged.auth else "缺"))
    print("  session " + ("OK" if merged.session else "缺"))
    print("  org     " + (merged.org or "（自动发现全部工作区）"))
    if not merged.complete:
        print("还差字段就别急着测明细：把整行 Cookie 和 x-org-id 一起复制过来最稳。")
        return 1
    print("三件套齐了，按次明细可用（跑 `python -m apiusage workspaces` 看有哪些工作区）。")
    return 0


def cmd_sources(args) -> int:
    for source in providers.SOURCES:
        print(f"{source.name:<14} {source.describe()}")
    return 0


# ───────────────────────────────────────────────────────────── 解析 ──────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apiusage",
        description="AI API 用量监测与报告（OpenCode GO 多工作区 + DeepSeek 余额）· 不落盘",
    )
    parser.add_argument("--version", action="version", version=f"apiusage {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="看当前额度/余额（不取明细，快）")
    p.add_argument("--workspace", "-w", action="append",
                   help="只看这个工作区（可重复；支持 id / 名字 / 前缀）")
    p.add_argument("--format", choices=["text", "pct", "json"], default="text")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="出用量报告（现场取数并聚合）")
    p.add_argument("--period", choices=["today", "week", "month", "days"], default="days")
    p.add_argument("--days", type=float, default=None, help=f"明细窗口天数（默认 {config.DEFAULT_DAYS:g}）")
    p.add_argument("--since", help="起始时刻（ISO，优先于 --days）")
    p.add_argument("--until", help="结束时刻（ISO）")
    p.add_argument("--ref", help="以某时刻为基准（ISO）")
    p.add_argument("--workspace", "-w", action="append", help="只看这个工作区（可重复）")
    p.add_argument("--format", choices=["text", "md", "json"], default="text")
    p.add_argument("--out", help="写进文件（内容仍在内存里算，只是把报告落一下）")
    p.add_argument("--watch", type=float, default=0,
                   help="先做一次两点采样（秒）：算燃烧速率与额度倍率")
    p.add_argument("--no-calls", action="store_true", help="不取明细（只要额度/余额）")
    p.add_argument("--no-export", action="store_true", help="不用导出通道，退回列表接口")
    p.add_argument("--pages", type=int, default=None, help="每个工作区最多翻几页")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("workspaces", help="列出能看到的工作区")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_workspaces)

    p = sub.add_parser("export", help="导出原始明细到文件（不落库）")
    p.add_argument("--days", type=float, default=30, help="往回导出多少天（上游只留 30 天）")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default="", help="输出文件（默认 logs-export.<format>）")
    p.add_argument("--workspace", "-w", action="append", help="只导这个工作区")
    p.add_argument("--max-pages", type=int, default=200, help="最多翻几页，每页 1000 条")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("panel", help="大屏接口：稳定 JSON 契约（见 docs/INTERFACE.md）")
    p.add_argument("--workspace", "-w", action="append", help="只看这个工作区（可重复）")
    p.add_argument("--format", choices=["json", "text"], default="json",
                   help="json=给程序（单行），text=给人看")
    p.add_argument("--repeat", type=float, default=0,
                   help="每隔 N 秒刷新一次，持续吐 NDJSON（0=只出一帧）")
    p.add_argument("--count", type=int, default=0, help="配合 --repeat：最多出几帧（0=不限）")
    p.add_argument("--with-stats", action="store_true",
                   help="顺便取明细并给出 totals（慢；大屏通常不需要）")
    p.add_argument("--days", type=float, default=None, help="--with-stats 时的明细窗口天数")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("doctor", help="自检：凭据、工作区、接口")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("cookie", help="存控制台 Cookie（解锁按次明细）")
    p.add_argument("--text", help="直接给文本")
    p.add_argument("--from-file", help="从文件读（HAR / cURL / DevTools 复制都行）")
    p.set_defaults(func=cmd_cookie)

    p = sub.add_parser("sources", help="列出已登记的数据源")
    p.set_defaults(func=cmd_sources)

    return parser


def main(argv: Optional[list] = None) -> int:
    # Windows 控制台默认是 GBK 代码页，中文会乱码；能改就改成 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
