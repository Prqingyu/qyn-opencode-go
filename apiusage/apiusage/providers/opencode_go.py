"""OpenCode GO：工作区发现、额度、调用明细。

三个接口的实测结论都写在对应函数上方 —— 那些不是猜的，是用探针一个个试出来的
（倒序、``until`` 闭区间、导出 1000/次、``limit`` 上限 100、30 天保留、游标能带
``after.id``、导出响应里的 ``until`` 是上界不是最旧一条……）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .. import config
from ..http import FetchError, UA_BROWSER, http_get
from ..model import (
    Call,
    Meter,
    Workspace,
    WorkspaceData,
    as_float,
    as_int,
    as_str,
    as_text,
)
from ..timeutil import ms_of_iso
from .base import Check, CollectContext, Source

NAME = "opencode_go"
WINDOW_ORDER = ("fiveHour", "week", "month")
MICRO_CENTS_PER_USD = 100_000_000.0


# ───────────────────────────────────────────────────────────── 请求头 ──────


def _base_headers(accept: str = "application/json") -> dict:
    return {"Accept": accept, "Accept-Encoding": "gzip, deflate", "User-Agent": UA_BROWSER}


def cookie_headers(profile, workspace: Optional[Workspace] = None, logs: bool = False) -> dict:
    headers = _base_headers()
    if profile.complete:
        headers["Cookie"] = profile.header()
    org = workspace.id if workspace and workspace.id else profile.org
    if org:
        headers["x-org-id"] = org
        page = "logs" if logs else "go"
        headers["Referer"] = f"{config.GO_BASE}/console/{org}/{page}"
    return headers


def bearer_headers(key: str) -> dict:
    headers = _base_headers()
    headers["Authorization"] = f"Bearer {key}"
    return headers


# ───────────────────────────────────────────────────────────── 地址 ──────


def supports_cursor(template: Optional[str] = None) -> bool:
    """这份模板能不能带 ``cursor``。

    自定义模板里漏写 ``{cursor}`` 时，游标会被 ``str.format`` 静默吃掉，翻页就永远
    停在第一页 —— 调用方必须据此退回 ``until`` 分页，而不是假装翻过了。
    """
    return "{cursor}" in (template or config.GO_LOGS_URL)


def logs_url(since_ms: int, limit: Optional[int] = None, template: Optional[str] = None,
             until_ms: Optional[int] = None, cursor: Optional[str] = None) -> str:
    tpl = template or config.GO_LOGS_URL
    until_part = "" if until_ms is None else f"&until={int(until_ms)}"
    cursor_part = "" if not cursor else f"&cursor={cursor}"   # 原样发，实测能被解析
    if "{since}" in tpl:
        return tpl.format(since=int(since_ms),
                          limit=int(limit or config.GO_LOGS_LIMIT),
                          until=until_part, cursor=cursor_part)
    return tpl + str(int(since_ms))          # 兼容以 since= 结尾的旧写法


def export_url(since_ms: int, until_ms: Optional[int] = None, fmt: str = "json",
               template: Optional[str] = None) -> str:
    tpl = template or config.GO_LOGS_EXPORT_URL
    until_part = "" if until_ms is None else f"&until={int(until_ms)}"
    return tpl.format(format=fmt, since=int(since_ms), until=until_part)


# ───────────────────────────────────────────────────────────── 解析 ──────


def parse_status(text: Optional[str]) -> list:
    """``GET /console/api/go/status`` → ``access.meters`` 三个窗口。"""
    if not text:
        return []
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(doc, dict):
        return []
    access = doc.get("access")
    if not isinstance(access, dict):
        return []
    meters = access.get("meters")
    if not isinstance(meters, dict):
        return []

    out = []
    for name in WINDOW_ORDER:
        node = meters.get(name)
        if not isinstance(node, dict):
            continue
        limit_micro = as_float(node.get("limitMicroCents"))
        used_micro = as_float(node.get("usedMicroCents"))
        if limit_micro is None or used_micro is None:
            continue
        out.append(Meter(
            window=name,
            limit_usd=limit_micro / MICRO_CENTS_PER_USD,
            used_usd=used_micro / MICRO_CENTS_PER_USD,
            resets_at_ms=ms_of_iso(node.get("resetsAt")),
            starts_at_ms=ms_of_iso(node.get("startsAt")),
        ))
    return out


def parse_subscription(text: Optional[str]) -> dict:
    """订阅读 Period（用来定位「本月」窗口的起点）。"""
    try:
        doc = json.loads(text or "")
    except (ValueError, TypeError):
        return {}
    access = doc.get("access") if isinstance(doc, dict) else None
    if not isinstance(access, dict):
        return {}
    return {
        "starts_at_ms": ms_of_iso(access.get("startsAt")),
        "ends_at_ms": ms_of_iso(access.get("endsAt")),
        "product": as_str(doc.get("product")),
    }


def parse_orgs(text: Optional[str]) -> list:
    """``GET /console/api/orgs`` → ``[{id,name}]``（不需要 ``x-org-id``）。"""
    try:
        doc = json.loads(text or "")
    except (ValueError, TypeError):
        return []
    if not isinstance(doc, list):
        return []
    return [{"id": item["id"], "name": as_str(item.get("name"))}
            for item in doc
            if isinstance(item, dict) and isinstance(item.get("id"), str)]


@dataclass
class LogPage:
    """列表接口的一页 —— 连翻页元信息一起。"""

    items: list = field(default_factory=list)
    next_cursor: Optional[str] = None
    until_ms: Optional[int] = None
    retention_days: Optional[int] = None


def _call_from_item(item: dict, workspace_id: str = "") -> Optional[Call]:
    call_id = as_str(item.get("id") or item.get("requestID"))
    started = as_int(item.get("startedAt"))
    if not call_id or started is None:
        return None
    policies = item.get("policies")
    return Call(
        id=call_id,
        started_at_ms=started,
        model=as_str(item.get("model") or item.get("requestedModel")),
        outcome=as_str(item.get("outcome")),
        cost_usd=as_float(item.get("cost")),
        input_tokens=as_int(item.get("inputTokens")),
        output_tokens=as_int(item.get("outputTokens")),
        reasoning_tokens=as_int(item.get("reasoningTokens")),
        cache_read_tokens=as_int(item.get("cacheReadTokens")),
        cache_write_tokens=as_int(item.get("cacheWriteTokens")),
        duration_ms=as_int(item.get("durationMs")),
        ttft_ms=as_int(item.get("timeToFirstTokenMs")),
        status_code=as_int(item.get("statusCode")),
        error_code=as_str(item.get("errorCode")),
        session_id=as_str(item.get("sessionID")),
        protocol=as_str(item.get("protocol")),
        reasoning_effort=as_str(item.get("reasoningEffort")),
        finish=as_text(item.get("finish")),
        cache_write_1h_tokens=as_int(item.get("cacheWrite1hTokens")),
        reasoning_budget_tokens=as_int(item.get("reasoningBudgetTokens")),
        policies=policies if isinstance(policies, str) else as_text(policies),
        workspace_id=as_str(item.get("workspaceID")) or workspace_id,
    )


def parse_logs_page(text: Optional[str], workspace_id: str = "") -> LogPage:
    """列表响应：``{items, nextCursor, until, retentionDays}``。"""
    page = LogPage()
    try:
        doc = json.loads(text or "")
    except (ValueError, TypeError):
        return page
    if not isinstance(doc, dict):
        return page
    raw_cursor = doc.get("nextCursor")
    page.next_cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
    page.until_ms = as_int(doc.get("until"))
    page.retention_days = as_int(doc.get("retentionDays"))
    items = doc.get("items")
    if isinstance(items, list):
        page.items = [c for c in (_call_from_item(i, workspace_id) for i in items
                                  if isinstance(i, dict)) if c]
    return page


def parse_export_items(doc: dict, workspace_id: str = "") -> list:
    """导出信封 ``{content}`` 里的 ``content`` 是个 JSON 字符串。"""
    content = doc.get("content") if isinstance(doc, dict) else None
    if not isinstance(content, str) or not content:
        return []
    try:
        inner = json.loads(content)
    except (ValueError, TypeError):
        return []
    if isinstance(inner, list):
        inner = {"items": inner}
    if not isinstance(inner, dict):
        return []
    return parse_logs_page(json.dumps(inner), workspace_id).items


# ───────────────────────────────────────────────────────────── 取数 ──────


def fetch_orgs(profile, url: Optional[str] = None) -> list:
    """列出这个登录能看到的工作区。

    ``x-org-id`` 写错时接口回的是 ``404 {"_tag":"NotFound"}``（和路径不存在长得
    一模一样），所以这个**不需要 x-org-id** 的接口是纠正它的唯一可靠来源。
    """
    if not profile.complete:
        raise FetchError("没有完整的控制台 Cookie，列不出工作区")
    body = http_get(url or config.GO_ORGS_URL, cookie_headers(profile))
    return parse_orgs(body)


def discover_workspaces(profile) -> list:
    """一个档案下的**所有**工作区。

    一个 Cookie 能看到该账号下的每个工作区，所以这里一律走 ``/api/orgs`` 全量发现
    —— 「两个工作区轮流用」正是靠这个。``profile.org`` 只是**首选**（取不到列表时
    拿它兜底），不是过滤条件。
    """
    if profile.complete:
        try:
            found = [Workspace(id=o["id"], name=o["name"], profile=profile.name)
                     for o in fetch_orgs(profile)]
            if found:
                return found
        except FetchError:
            pass
    if profile.org:
        return [Workspace(id=profile.org, name="", profile=profile.name)]
    return [Workspace(id="", name="（按 Key 直查）", profile=profile.name)]


def _status_channels(profile, workspace: Optional[Workspace]) -> list:
    """额度该按什么顺序问。

    **Key 是账号级的**：``Authorization: Bearer oc_sk_…`` 打 ``go/status`` 拿到的是
    账号下那个 GO 订阅的数，**跟 ``x-org-id`` 无关**。所以只要是在问「某个具体工作区」，
    就必须先走 Cookie＋``x-org-id``，否则两个工作区会显示一模一样的数字（真踩过）。
    只有在没有 Cookie、或不知道工作区时，才退回 Key。
    """
    if profile.complete and workspace is not None and workspace.id:
        return ["cookie"] + (["key"] if profile.key else [])
    return ([ "key"] if profile.key else []) + (["cookie"] if profile.complete else [])


def fetch_status(profile, workspace: Optional[Workspace] = None):
    """额度：返回 ``(窗口, 凭据来源, 原始报文)``。"""
    errors = []
    for kind in _status_channels(profile, workspace):
        headers = (bearer_headers(profile.key) if kind == "key"
                   else cookie_headers(profile, workspace))
        label = "key（账号级，不分工作区）" if kind == "key" else "cookie（按工作区）"
        try:
            body = http_get(config.GO_STATUS_URL, headers)
        except FetchError as exc:
            errors.append(f"{label}: {exc}")
            continue

        meters = parse_status(body)
        if meters:
            return meters, label, body

        # 200 但一个窗口都没有 = 这个工作区没有 GO 订阅（实测 `access` 缺失时
        # 整个报文就是字面量 `null`）。这时候**必须就此打住**：再去问账号级的 Key
        # 只会把别的订阅的数字显示过来。
        try:
            json.loads(body)
        except (ValueError, TypeError):
            errors.append(f"{label}：报文里没有可解析的窗口")
            continue
        return [], f"{label}·无 GO 订阅", body

    raise FetchError("GO 额度取不到（" + "；".join(errors or ["没有可用凭据"]) + "）")


def fetch_logs_page(profile, workspace, since_ms: int, limit: int = 1,
                    until_ms: Optional[int] = None, cursor: Optional[str] = None) -> LogPage:
    if not profile.complete:
        raise FetchError("没有完整的控制台 Cookie，取不到调用明细")
    url = logs_url(since_ms, limit, until_ms=until_ms, cursor=cursor)
    body = http_get(url, cookie_headers(profile, workspace, logs=True))
    return parse_logs_page(body, workspace.id)


def fetch_export(profile, workspace, since_ms: int, until_ms: Optional[int] = None,
                 fmt: str = "json") -> dict:
    """导出信封 ``{content,count,truncated,until}`` —— 一次最多 1000 条。

    ⚠️ 响应里的 ``until`` 是**区间上界**，不是「本批最旧一条」；拿它翻页会拿到
    998/1000 条重复。往回翻要用 ``min(startedAt) - 1``。
    """
    if not profile.complete:
        raise FetchError("没有完整的控制台 Cookie，取不到调用明细")
    body = http_get(export_url(since_ms, until_ms, fmt),
                    cookie_headers(profile, workspace, logs=True),
                    timeout=config.EXPORT_TIMEOUT)
    try:
        doc = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise FetchError("导出返回体不是 JSON", body=body[:200]) from exc
    if not isinstance(doc, dict) or "content" not in doc:
        raise FetchError("导出返回体里没有 content", body=body[:200])
    return doc


def fetch_calls(profile, workspace, since_ms: int, page_limit: Optional[int] = None,
                limit: Optional[int] = None, use_export: bool = True) -> tuple:
    """明细：返回 ``(calls, truncated)``，按时间升序。

    通道优先级：**先导出**（1000 条/次），不可用再退回列表（100 条/次，优先官方
    ``nextCursor``）。两条都往回翻，同一毫秒的重复靠调用方按 id 去重。
    """
    if not profile.complete:
        raise FetchError("没有完整的控制台 Cookie，取不到调用明细")
    max_pages = page_limit or config.LOGS_PAGE_LIMIT
    page_size = min(limit or config.GO_LOGS_LIMIT, 100)
    lower = max(0, int(since_ms))
    collected: dict = {}
    errors: list = []
    truncated = False

    def absorb(calls) -> int:
        fresh = 0
        for call in calls:
            if call.id not in collected:
                collected[call.id] = call
                fresh += 1
        return fresh

    if use_export:
        upper = None
        try:
            for _ in range(max_pages):
                doc = fetch_export(profile, workspace, lower, upper)
                batch = parse_export_items(doc, workspace.id)
                if not batch:
                    break
                fresh = absorb(batch)
                oldest = min(c.started_at_ms for c in batch)
                truncated = bool(doc.get("truncated"))
                if not truncated or fresh == 0 or oldest <= lower:
                    break
                upper = oldest - 1
        except FetchError as exc:
            errors.append(f"导出通道不可用（{exc}）")
        if collected:
            return sorted(collected.values(), key=lambda c: c.started_at_ms), truncated

    cursor = None
    upper = None
    cursor_ok = supports_cursor()
    for _ in range(max_pages):
        meta = fetch_logs_page(profile, workspace, lower, page_size,
                               until_ms=upper, cursor=cursor)
        page = meta.items
        if not page:
            break
        fresh = absorb(page)
        oldest = min(c.started_at_ms for c in page)

        if meta.next_cursor and cursor_ok and upper is None:
            cursor = meta.next_cursor
            if fresh == 0 or oldest <= lower:
                break
            continue
        cursor = None
        truncated = len(page) >= page_size
        if len(page) < page_size or fresh == 0 or oldest <= lower:
            break
        upper = oldest - 1

    if not collected and errors:
        raise FetchError("；".join(errors))
    return sorted(collected.values(), key=lambda c: c.started_at_ms), truncated


# ───────────────────────────────────────────────────────────── 装配 ──────


def collect(ctx: CollectContext) -> None:
    """把每个档案、每个工作区的数据塞进 Snapshot。"""
    if not ctx.creds.has_go:
        ctx.log("OpenCode GO：没有可用凭据，跳过")
        return

    for profile in ctx.creds.profiles:
        if not profile.usable:
            continue
        for workspace in discover_workspaces(profile):
            if not ctx.wants_workspace(workspace):
                continue
            data = WorkspaceData(workspace)
            ctx.snapshot.workspaces.append(data)

            try:
                meters, source, raw = fetch_status(profile, workspace)
                data.meters = meters
                data.meter_source = source
                sub = parse_subscription(raw)
                if sub.get("starts_at_ms"):
                    data.subscription_start_ms = sub["starts_at_ms"]
            except FetchError as exc:
                data.errors.append(f"额度：{exc}")

            if not ctx.with_calls:
                continue
            if not profile.complete:
                data.errors.append("明细：没有控制台 Cookie（额度照常）")
                continue
            try:
                calls, truncated = fetch_calls(
                    profile, workspace, ctx.start_ms or 0,
                    page_limit=ctx.page_limit, use_export=ctx.use_export)
                data.calls = calls
                data.calls_truncated = truncated
                page = fetch_logs_page(profile, workspace, 0, limit=1)
                data.retention_days = page.retention_days
            except FetchError as exc:
                data.errors.append(f"明细：{exc}")

    for data in ctx.snapshot.workspaces:
        for message in data.errors:
            ctx.warn(f"{data.workspace.short} {message}")


def check(creds) -> list:
    """doctor 用的自检。"""
    if not creds.has_go:
        return [Check("OpenCode GO 凭据", False, "没找到",
                      "Key（额度）或控制台 Cookie（明细）")]
    out = []
    for profile in creds.profiles:
        if not profile.usable:
            continue
        try:
            workspaces = discover_workspaces(profile)
            out.append(Check(f"{profile.name}：工作区", True,
                             "，".join(w.label for w in workspaces) or "（空）"))
        except FetchError as exc:
            out.append(Check(f"{profile.name}：工作区", False, str(exc)))
            continue
        for workspace in workspaces:
            try:
                meters, source, _ = fetch_status(profile, workspace)
                detail = "，".join(f"{m.label} 剩余 {m.remaining_percent:.1f}%" for m in meters)
                out.append(Check(f"{workspace.short}：额度（{source}）", True, detail))
            except FetchError as exc:
                out.append(Check(f"{workspace.short}：额度", False, str(exc)))
            if not profile.complete:
                out.append(Check(f"{workspace.short}：明细", None,
                                 "没有控制台 Cookie —— 只能采额度"))
                continue
            try:
                page = fetch_logs_page(profile, workspace, 0, limit=1)
                out.append(Check(f"{workspace.short}：明细接口", True,
                                 f"可用，保留 {page.retention_days} 天"))
            except FetchError as exc:
                hint = ""
                if exc.status == 404:
                    hint = (f"当前 x-org-id = {workspace.id}；404 的第一嫌疑是它不对，"
                            "不是路径不对（用 /console/api/orgs 核对）")
                out.append(Check(f"{workspace.short}：明细接口", False, str(exc), hint))
    return out


SOURCE = Source(name=NAME, label="OpenCode GO（额度 + 调用明细）",
                needs=("oc_sk_ key", "console cookie"), collect=collect, check=check)
