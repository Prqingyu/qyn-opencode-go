#!/usr/bin/env python3
"""离线端到端自检：一台假控制台，把「多工作区发现 → 额度 → 明细 → 报告」整条链跑通。

不碰真接口、不落任何盘（连临时库都没有）：
    python tools/verify_offline.py

它盯的是单元测试覆盖不到的那几件：

* 真实 HTTP 往返（Cookie 有没有真的带上去、没带是不是 403）
* **一个 Cookie 发现多个工作区**，且每个工作区的额度/明细互不串（Key 是账号级的，
  串了就会被下面「两个工作区数字必须不同」的断言抓住）
* 导出通道（1000 条/次）与列表通道（100 条/次 + nextCursor）两条都通
* 报告能按工作区分节 + 出合计
* **跑完不产生任何文件** —— 这是「不做本地保存」的硬断言
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先把环境指向假服务端（config 是导入期求值的），并且**不指向任何真实凭据文件**
_FAKE_HOME = ROOT / ".verify-profile"          # 只是给路径，不会真的创建
os.environ.setdefault("APIUSAGE_GO_COOKIE", str(_FAKE_HOME / "go.cookie"))
os.environ.setdefault("APIUSAGE_DSH_CREDENTIALS", str(_FAKE_HOME / "none.yaml"))
os.environ.setdefault("APIUSAGE_OPENCODE_AUTH", str(_FAKE_HOME / "none.json"))
for name in ("OPENCODE_GO_AUTH", "OPENCODE_GO_SESSION", "OPENCODE_GO_ORG",
             "OPENCODE_GO_API_KEY", "DEEPSEEK_API_KEY"):
    os.environ.pop(name, None)

from apiusage import config, credentials, fetch, providers, report  # noqa: E402
from apiusage.model import Call  # noqa: E402
from apiusage.providers import opencode_go  # noqa: E402
from apiusage.timeutil import CN, to_ms  # noqa: E402

EXPECTED_AUTH = "Fe26.2**verify-only-fake-cookie"

# ── 两个工作区：额度不同，明细也不同（串了就会被抓住）──────────────────────
WORKSPACES = [
    {"id": "wrk_VERIFY000000000000000A", "name": "甲", "used": 1.0, "limit": 12.0, "seat": 1},
    {"id": "org_VERIFY000000000000000B", "name": "乙", "used": 5.0, "limit": 40.0, "seat": 2},
]
BASE_MS = to_ms(datetime(2026, 10, 14, 9, 0, 0, tzinfo=CN))
CALLS_PER_WORKSPACE = 125


def make_calls(workspace: dict) -> list:
    items = []
    for i in range(CALLS_PER_WORKSPACE):
        started = BASE_MS + i * 300_000
        rejected = i % 17 == 0
        items.append({
            "id": f"{workspace['id']}-{i:04d}",
            "startedAt": started,
            "durationMs": 800 + (i % 40) * 25,
            "outcome": "rejected" if rejected else "succeeded",
            "model": "deepseek-v4.1-flash" if i % 3 else "deepseek-v4.1-pro",
            "statusCode": 429 if rejected else 200,
            "errorCode": "inference_failed" if rejected else "",
            "workspaceID": workspace["id"],
            "cost": None if rejected else round((0.0009 + (i % 23) * 0.00017) * workspace["seat"], 6),
            "inputTokens": None if rejected else 1200 + (i % 300),
            "outputTokens": None if rejected else 180 + (i % 90),
            "reasoningTokens": None if rejected else 60 + (i % 40),
            "cacheReadTokens": None if rejected else 400 + (i % 200),
            "cacheWriteTokens": None if rejected else 25,
            "timeToFirstTokenMs": None if rejected else 260 + (i % 120),
            "finish": {"reason": "tool_calls"},
        })
    return items


CALLS = {w["id"]: make_calls(w) for w in WORKSPACES}
HITS: list = []


def status_payload(workspace: dict) -> dict:
    micro = 100_000_000
    return {
        "product": "go",
        "access": {
            "startsAt": "2025-01-01T00:00:00.000Z",
            "endsAt": "2025-02-01T00:00:00.000Z",
            "meters": {
                "fiveHour": {"resetsAt": "2025-01-15T14:12:34.000Z",
                             "limitMicroCents": int(workspace["limit"] * micro),
                             "usedMicroCents": int(workspace["used"] * micro)},
                "week": {"resetsAt": "2026-10-19T00:00:00.000Z",
                         "limitMicroCents": 30 * micro, "usedMicroCents": 10 * micro},
                "month": {"resetsAt": "2025-02-01T00:00:00.000Z",
                          "limitMicroCents": 60 * micro, "usedMicroCents": 20 * micro},
            },
        },
    }


def _select(raw_query: str, workspace_id: str) -> list:
    from urllib.parse import parse_qs

    query = parse_qs(raw_query)
    since = int(query.get("since", ["0"])[0])
    until = int(query["until"][0]) if "until" in query else None
    rows = [c for c in CALLS[workspace_id]
            if c["startedAt"] >= since and (until is None or c["startedAt"] <= until)]
    rows.sort(key=lambda c: c["startedAt"], reverse=True)      # 真实接口是倒序
    return rows


BALANCE_PAYLOAD = {
    "is_available": True,
    "balance_infos": [
        {"currency": "USD", "total_balance": "1.00",
         "granted_balance": "0.00", "topped_up_balance": "1.00"},
        {"currency": "CNY", "total_balance": "40.00",
         "granted_balance": "20.00", "topped_up_balance": "20.00"},
    ],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _workspace(self):
        org = self.headers.get("x-org-id") or ""
        for w in WORKSPACES:
            if w["id"] == org:
                return w
        return None

    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse

        url = urlparse(self.path)
        HITS.append(self.path)
        cookie = self.headers.get("Cookie") or ""

        if url.path.endswith("/user/balance"):
            # DeepSeek 官方：只认 Bearer Key；不带就 401（实测就是这个行为）
            if "Bearer " not in (self.headers.get("Authorization") or ""):
                return self._send("Authentication Fails (governor)", 401)
            return self._send(BALANCE_PAYLOAD)

        if url.path.endswith("/api/orgs"):
            if f"auth={EXPECTED_AUTH}" not in cookie:
                return self._send({"_tag": "Forbidden"}, 403)
            return self._send([{"id": w["id"], "name": w["name"], "avatarUrl": None}
                               for w in WORKSPACES])

        if url.path.endswith("/go/status"):
            if "Bearer " in (self.headers.get("Authorization") or ""):
                # 账号级：真实接口会忽略 x-org-id，这里照抄这个坑
                return self._send(status_payload(WORKSPACES[0]))
            workspace = self._workspace()
            if workspace is None:
                # 不带 x-org-id 或写错 → 真实接口回 400/404，这里用 404 复刻
                return self._send({"_tag": "NotFound"}, 404)
            return self._send(status_payload(workspace))

        if url.path.endswith("/request-logs/export"):
            if f"auth={EXPECTED_AUTH}" not in cookie:
                return self._send({"_tag": "Forbidden"}, 403)
            workspace = self._workspace()
            if workspace is None:
                return self._send({"_tag": "NotFound"}, 404)
            rows = _select(url.query, workspace["id"])
            batch = rows[:1000]
            return self._send({
                "content": json.dumps({"items": batch}, ensure_ascii=False),
                "contentType": "application/json", "filename": "request-logs.json",
                "count": len(batch), "truncated": len(rows) > len(batch),
                "until": 9_999_999_999_999,
            })

        if url.path.endswith("/request-logs"):
            if f"auth={EXPECTED_AUTH}" not in cookie:
                return self._send({"_tag": "Forbidden"}, 403)
            workspace = self._workspace()
            if workspace is None:
                return self._send({"_tag": "NotFound"}, 404)
            from urllib.parse import parse_qs

            query = parse_qs(url.query)
            rows = _select(url.query, workspace["id"])
            limit = int(query.get("limit", ["100"])[0])
            index = int(query["cursor"][0].split("-")[1]) if "cursor" in query else 0
            page = rows[index:index + limit]
            return self._send({
                "items": page,
                "nextCursor": f"off-{index + len(page)}"
                              if index + len(page) < len(rows) else None,
                "until": 9_999_999_999_999,
                "retentionDays": 30,
            })

        return self._send({"_tag": "NotFound"}, 404)


# ── 断言框架 ───────────────────────────────────────────────────────────────
FAILURES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _files_under(root: Path) -> set:
    return {p for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc")}


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    os.environ["APIUSAGE_GO_ORGS_URL"] = f"{base}/console/api/orgs"
    os.environ["APIUSAGE_GO_STATUS_URL"] = f"{base}/console/api/go/status"
    os.environ["APIUSAGE_GO_LOGS_URL"] = (
        f"{base}/console/api/request-logs?category=inference&limit={{limit}}&since={{since}}{{until}}{{cursor}}")
    os.environ["APIUSAGE_GO_LOGS_EXPORT_URL"] = (
        f"{base}/console/api/request-logs/export?category=inference&format={{format}}&since={{since}}{{until}}")
    os.environ["APIUSAGE_DEEPSEEK_URL"] = f"{base}/user/balance"
    config.GO_ORGS_URL = os.environ["APIUSAGE_GO_ORGS_URL"]
    config.GO_STATUS_URL = os.environ["APIUSAGE_GO_STATUS_URL"]
    config.GO_LOGS_URL = os.environ["APIUSAGE_GO_LOGS_URL"]
    config.GO_LOGS_EXPORT_URL = os.environ["APIUSAGE_GO_LOGS_EXPORT_URL"]
    config.DEEPSEEK_BALANCE_URL = os.environ["APIUSAGE_DEEPSEEK_URL"]

    before = _files_under(ROOT)
    print(f"假控制台 {base}  ·  两个工作区，各 {CALLS_PER_WORKSPACE} 条明细")
    print()

    profile = credentials.GoProfile(name="verify", auth=EXPECTED_AUTH,
                                    session="st_" + "0" * 24, key="oc_sk_verify")
    creds = credentials.Credentials(profiles=[profile], deepseek_key="sk-verify",
                                    sources={"go_cookie": "（自检用，内存里）"})

    # 1) 凭据底线
    try:
        opencode_go.fetch_calls(credentials.GoProfile(key="oc_sk_x"),
                                opencode_go.Workspace("wrk_X", "X"), 0)
        check("没有 Cookie 时明细被挡住", False, "居然没抛错")
    except opencode_go.FetchError as exc:
        check("没有 Cookie 时明细被挡住", "Cookie" in str(exc), str(exc))

    # 2) 一个 Cookie 发现两个工作区
    found = opencode_go.discover_workspaces(profile)
    check("一个 Cookie 发现两个工作区", [w.id for w in found] == [w["id"] for w in WORKSPACES],
          str([w.id for w in found]))

    # 3) 取数（导出通道）
    snapshot = fetch.gather(creds, days=3)
    check("取到两个工作区", len(snapshot.workspaces) == 2,
          f"实际 {len(snapshot.workspaces)}")

    by_id = {d.workspace.id: d for d in snapshot.workspaces}
    check("每个工作区都拿到额度",
          all(len(d.meters) == 3 for d in snapshot.workspaces),
          str({k: len(v.meters) for k, v in by_id.items()}))

    five = {k: d.meter("fiveHour") for k, d in by_id.items()}
    check("两个工作区的额度**不串**（Key 是账号级的，串了这里就会相等）",
          five["wrk_VERIFY000000000000000A"].limit_usd != five["org_VERIFY000000000000000B"].limit_usd
          and five["wrk_VERIFY000000000000000A"].limit_usd == 12.0
          and five["org_VERIFY000000000000000B"].limit_usd == 40.0,
          str({k: (m.limit_usd, m.used_usd) for k, m in five.items()}))

    check("明细按工作区各归各的",
          all(len(d.calls) == CALLS_PER_WORKSPACE for d in snapshot.workspaces)
          and all(c.workspace_id == d.workspace.id for d in snapshot.workspaces for c in d.calls),
          str({k: len(d.calls) for k, d in by_id.items()}))

    export_hits = [h for h in HITS if "/export" in h]
    check("走的是导出通道（2 个工作区各 1 次）", len(export_hits) == 2, str(export_hits))

    check("明细字段解析到位（finish 是对象 / token / 保留天数）",
          by_id["wrk_VERIFY000000000000000A"].calls[1].finish_reason == "tool_calls"
          and by_id["wrk_VERIFY000000000000000A"].calls[1].total_tokens > 0
          and by_id["wrk_VERIFY000000000000000A"].retention_days == 30)

    # 4) 列表通道兜底
    HITS.clear()
    calls, _ = opencode_go.fetch_calls(profile, opencode_go.Workspace(WORKSPACES[0]["id"], "甲"),
                                       0, use_export=False)
    list_hits = [h for h in HITS if "/request-logs" in h]
    check("关掉导出后列表通道也能拿全（125 条 → 2 页）",
          len(calls) == CALLS_PER_WORKSPACE and len(list_hits) == 2, str(list_hits))
    check("列表通道用官方 nextCursor 翻页",
          any("cursor=" in h for h in list_hits[1:]), str(list_hits))

    # 5) 报告：多工作区分节 + 合计
    data = report.build(snapshot, label="最近 3 天")
    check("报告按工作区分节", len(data.workspaces) == 2 and data.multi)
    check("合计等于两边之和",
          data.combined.calls == CALLS_PER_WORKSPACE * 2,
          str(data.combined.calls))
    text = report.render(data, "text")
    for needle in ("【甲", "【乙", "【合计】", "工作区 2 个", "不做本地保存"):
        check(f"纯文本报告含「{needle}」", needle in text)
    check("纯文本不带 Markdown 表格记号", "|" not in text)
    check("按工作区的费用不同（没串）",
          data.workspaces[0].rollup.stats.cost_usd != data.workspaces[1].rollup.stats.cost_usd)

    for fmt in ("md", "json"):
        try:
            check(f"{fmt} 报告渲染", len(report.render(data, fmt)) > 200)
        except Exception as exc:  # noqa: BLE001
            check(f"{fmt} 报告渲染", False, repr(exc))
    payload = json.loads(report.render(data, "json"))
    check("json 报告里有两个工作区的明细统计",
          len(payload["workspaces"]) == 2 and payload["totals"]["calls"] == 250)

    # 6) 大屏面板契约（另一个 Agent 消费的就是这份）
    from apiusage import panel

    frame = panel.build(snapshot, label="当前")
    check("面板契约字段齐全",
          all(k in frame for k in ("schema", "generated_at_ms", "fresh_for_ms", "thresholds",
                                   "level", "headline", "workspaces", "balances",
                                   "alerts", "warnings", "totals")),
          str(sorted(frame)))
    check("契约版本正确", frame["schema"] == panel.SCHEMA, frame["schema"])
    check("两个工作区都在帧里", len(frame["workspaces"]) == 2)
    meters = [m for ws in frame["workspaces"] for m in ws["meters"]]
    check("每个窗口都带可用百分比与等级",
          len(meters) == 6
          and all(m["usable_percent"] is not None
                  and m["level"] in ("ok", "warn", "critical", "unknown") for m in meters))
    check("headline 指向最紧的那个窗口",
          any(m["label"] in frame["headline"] for m in meters), frame["headline"])
    check("面板是一行 JSON（NDJSON 友好）", "\n" not in panel.render(frame))

    # 6b) DeepSeek 官方余额也是一等接口（账户级，跟工作区无关）
    check("余额出现在面板里", len(frame["balances"]) == 1, str(frame["balances"]))
    balance = frame["balances"][0]
    check("余额字段齐全",
          all(k in balance for k in ("account", "currency", "total", "granted", "topped_up",
                                     "is_available", "infos", "usable", "low_threshold",
                                     "error", "level")),
          str(sorted(balance)))
    check("多币种全都在（没只挑 CNY）",
          [i["currency"] for i in balance["infos"]] == ["USD", "CNY"],
          str(balance["infos"]))
    check("展示值取 CNY", balance["currency"] == "CNY" and balance["total"] == 40.00,
          f"{balance['currency']} {balance['total']}")
    check("余额等级正常", balance["level"] == "ok" and balance["error"] is None)

    # 7) 不落盘 —— 这是需求里的硬要求
    after = _files_under(ROOT)
    created = sorted(str(p.relative_to(ROOT)) for p in after - before)
    check("跑完没有产生任何文件（不做本地保存）", not created, f"新增：{created}")
    check("没有数据库/状态目录", not (ROOT / "data").exists() and not _FAKE_HOME.exists(),
          f"data={ROOT / 'data'} exists={(ROOT / 'data').exists()}")

    server.shutdown()
    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过：" + "，".join(FAILURES))
        return 1
    print("✅ 全部通过：多工作区发现 → 逐工作区取数 → 详情聚合 → 报告，且全程不落盘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
