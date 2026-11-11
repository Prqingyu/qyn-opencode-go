"""apiusage 单元测试（只用标准库 unittest，不联网、不落盘）。

跑法（在 OPENCODEGO/apiusage 下）::

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

from apiusage import (
    aggregate,
    config,
    credentials,
    fetch,
    gates,
    panel,
    peakhours,
    providers,
    report,
)
from apiusage.model import Balance, Call, Meter, Snapshot, Workspace, WorkspaceData
from apiusage.providers import deepseek, opencode_go
from apiusage.timeutil import CN, now_ms, to_ms

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "DSH-PET-main"
    / "tests"
    / "DshPet.Core.Tests"
    / "Fixtures"
    / "request-logs-sample.json"
)

BALANCE_JSON = json.dumps({
    "is_available": True,
    "balance_infos": [
        {"currency": "USD", "total_balance": "1.00"},
        {"currency": "CNY", "total_balance": "40.00", "granted_balance": "20.00",
         "topped_up_balance": "20.00"},
    ],
})

STATUS_JSON = json.dumps({
    "product": "go",
    "access": {
        "startsAt": "2025-01-01T00:00:00.000Z",
        "endsAt": "2025-02-01T00:00:00.000Z",
        "meters": {
            "fiveHour": {"startsAt": "2025-01-15T09:12:34.000Z",
                         "resetsAt": "2025-01-15T14:12:34.000Z",
                         "limitMicroCents": "1200000000", "usedMicroCents": "0"},
            "week": {"resetsAt": "2026-10-19T00:00:00.000Z",
                     "limitMicroCents": "3000000000", "usedMicroCents": "0"},
            "month": {"resetsAt": "2025-02-01T00:00:00.000Z",
                      "limitMicroCents": "6000000000", "usedMicroCents": "0"},
        },
    },
})


def meter(window, limit=12.0, used=6.0, resets_at=None):
    return Meter(window=window, limit_usd=limit, used_usd=used, resets_at_ms=resets_at)


def make_items(count, step=1000, start=1_000):
    return [{"id": f"c{i:05d}", "startedAt": start + i * step, "model": "m",
             "outcome": "succeeded", "cost": 0.001} for i in range(count)]


# ─────────────────────────────────────────────────────────────── 解析 ──────


class ParseBalanceTests(unittest.TestCase):
    def test_picks_cny_entry_and_keeps_every_currency(self):
        balance = deepseek.parse(BALANCE_JSON)
        self.assertIsNotNone(balance)
        self.assertEqual(balance.currency, "CNY", "有 CNY 就优先用它做展示值")
        self.assertAlmostEqual(balance.total, 40.00)
        self.assertAlmostEqual(balance.granted, 20.00)
        self.assertTrue(balance.is_available)
        # 别的币种不能丢 —— 只认第一条的实现会在多币种时静默少报
        self.assertEqual([i.currency for i in balance.infos], ["USD", "CNY"])
        self.assertAlmostEqual(balance.info("USD").total, 1.00)

    def test_falls_back_to_first_currency_when_no_cny(self):
        balance = deepseek.parse(json.dumps({
            "balance_infos": [{"currency": "USD", "total_balance": "1.5"}]}))
        self.assertIsNotNone(balance, "没有 CNY 也该给数据，而不是当成取不到")
        self.assertEqual(balance.currency, "USD")
        self.assertAlmostEqual(balance.total, 1.5)

    def test_numbers_as_numbers(self):
        text = json.dumps({"balance_infos": [{"currency": "CNY", "total_balance": 12.5}]})
        self.assertAlmostEqual(deepseek.parse(text).total, 12.5)

    def test_garbage(self):
        for text in (None, "", "not json", "[]", "{}", '{"balance_infos": "x"}',
                     json.dumps({"balance_infos": []}),
                     json.dumps({"balance_infos": [{"currency": "", "total_balance": "1"}]}),
                     json.dumps({"balance_infos": [{"currency": "CNY"}]})):
            self.assertIsNone(deepseek.parse(text), text)


class ParseStatusTests(unittest.TestCase):
    def test_string_micro_cents(self):
        meters = opencode_go.parse_status(STATUS_JSON)
        self.assertEqual([m.window for m in meters], ["fiveHour", "week", "month"])
        self.assertAlmostEqual(meters[0].limit_usd, 12.0)
        self.assertAlmostEqual(meters[0].used_usd, 0.0)
        self.assertAlmostEqual(meters[1].limit_usd, 30.0)
        self.assertAlmostEqual(meters[2].used_usd, 0.0)
        # 2025-01-15T14:12:34Z == 北京时间 22:12:34
        self.assertEqual(meters[0].resets_at_ms,
                         to_ms(datetime(2026, 10, 15, 22, 12, 34, tzinfo=CN)))

    def test_bad_shapes(self):
        for text in (None, "", "nope", "[]", '{"access": 3}', '{"access": {"meters": []}}'):
            self.assertEqual(opencode_go.parse_status(text), [], text)

    def test_null_resets_at_is_absent(self):
        text = json.dumps({"access": {"meters": {
            "fiveHour": {"limitMicroCents": 100, "usedMicroCents": 0, "resetsAt": None}}}})
        meters = opencode_go.parse_status(text)
        self.assertEqual(len(meters), 1)
        self.assertIsNone(meters[0].resets_at_ms)

    def test_subscription_window(self):
        sub = opencode_go.parse_subscription(STATUS_JSON)
        self.assertEqual(sub["starts_at_ms"], to_ms(datetime(2026, 10, 5, 3, 15, 24, tzinfo=CN)))
        self.assertEqual(sub["product"], "go")


class ParseLogsTests(unittest.TestCase):
    def test_real_sample_fixture(self):
        if not FIXTURE.exists():
            self.skipTest("参考仓库的样本不在")
        page = opencode_go.parse_logs_page(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(page.items), 50)
        self.assertTrue(all(c.id for c in page.items))
        billed = [c for c in page.items if c.billable]
        self.assertTrue(billed and all(c.cost_usd > 0 for c in billed))

    def test_metadata_and_new_fields(self):
        text = json.dumps({
            "items": [{"id": "a", "startedAt": 5, "cost": "0.5", "finish": {"reason": "tool_calls"},
                       "cacheWrite1hTokens": "7", "reasoningBudgetTokens": 3,
                       "policies": [{"id": "p1"}], "workspaceID": "wrk_X"}],
            "nextCursor": '{"v":2,"after":{"id":"a"}}',
            "until": 999,
            "retentionDays": 30,
        })
        page = opencode_go.parse_logs_page(text)
        self.assertEqual(page.next_cursor, '{"v":2,"after":{"id":"a"}}')
        self.assertEqual(page.retention_days, 30)
        self.assertEqual(page.until_ms, 999)
        call = page.items[0]
        self.assertEqual(call.finish_reason, "tool_calls", "finish 是对象，必须解析出来")
        self.assertAlmostEqual(call.cost_usd, 0.5)
        self.assertEqual(call.cache_write_1h_tokens, 7)
        self.assertEqual(call.reasoning_budget_tokens, 3)
        self.assertEqual(call.workspace_id, "wrk_X")
        self.assertIn("p1", call.policies)
        # 1h 缓存写是变体，不折进总量（避免与 cacheWriteTokens 重复计数）
        self.assertEqual(call.total_tokens, 0)

    def test_items_without_id_or_time_are_dropped(self):
        text = json.dumps({"items": [{"startedAt": 1}, {"id": "a"}, {"id": "b", "startedAt": 2}]})
        self.assertEqual([c.id for c in opencode_go.parse_logs_page(text).items], ["b"])

    def test_export_envelope(self):
        inner = json.dumps({"items": [{"id": "z", "startedAt": 1, "cost": 0.25}]})
        calls = opencode_go.parse_export_items({"content": inner})
        self.assertEqual([c.id for c in calls], ["z"])

    def test_export_envelope_tolerates_garbage(self):
        for doc in ({}, {"content": ""}, {"content": "not json"}, {"content": "[1,2]"}):
            self.assertEqual(opencode_go.parse_export_items(doc), [], repr(doc))


# ─────────────────────────────────────────────────────────────── 凭据 ──────


class CredentialTests(unittest.TestCase):
    def test_scan_cookie_header(self):
        text = ("Cookie: oc_locale=zh; auth=Fe26.2**abc-def_ghi; "
                "__Host-console_session=st_00000000-0000-0000-0000-000000000000; "
                "x-org-id: org_EXAMPLE000000000000000000")
        profile = credentials.scan_go_credentials(text)
        self.assertEqual(profile.auth, "Fe26.2**abc-def_ghi")
        self.assertEqual(profile.session, "st_00000000-0000-0000-0000-000000000000")
        self.assertEqual(profile.org, "org_EXAMPLE000000000000000000")
        self.assertTrue(profile.complete)

    def test_scan_har_style_and_workspace_prefixes(self):
        text = '{"name":"auth","value":"Fe26.2**zzz"},{"name":"x-org-id","value":"wrk_ABC123"}'
        patch = credentials.scan_go_credentials(text)
        self.assertEqual(patch.auth, "Fe26.2**zzz")
        self.assertEqual(patch.org, "wrk_ABC123")
        self.assertEqual(credentials.scan_go_credentials("x-org-id: org_ABC").org, "org_ABC")

    def test_unrecognised_text(self):
        patch = credentials.scan_go_credentials("hello world")
        self.assertFalse(patch.auth or patch.session or patch.org)

    def test_mask_never_leaks_full_secret(self):
        masked = credentials.mask("sk-abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", masked)
        self.assertIn("len=", masked)

    def test_profiles_from_inline_json(self):
        spec = json.dumps([
            {"name": "主号", "auth": "Fe26.2**a", "session": "st_" + "0" * 20, "org": "wrk_A"},
            {"name": "小号", "auth": "Fe26.2**b", "session": "st_" + "1" * 20},
        ])
        profiles = credentials.load_go_profiles(spec)
        self.assertEqual([p.name for p in profiles], ["主号", "小号"])
        self.assertTrue(all(p.complete for p in profiles))
        self.assertEqual(profiles[0].org, "wrk_A")

    def test_profiles_tolerate_garbage(self):
        for spec in ("", "not json", "{}", "[1,2]", '{"profiles": "x"}'):
            self.assertEqual(credentials.load_go_profiles(spec), [], repr(spec))

    def test_dsh_credentials_regex(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".credentials.yaml"
        path.write_text(
            "refs:\n  OPENCODE_GO_API_KEY: oc_sk_XXXX\n"
            "  DEEPSEEK_API_KEY: sk-0123456789abcdef0123456789abcdef\n", encoding="utf-8")
        keys = credentials.load_dsh_keys(path)
        self.assertEqual(keys["opencode_go"], "oc_sk_XXXX")
        self.assertEqual(keys["deepseek"], "sk-0123456789abcdef0123456789abcdef")

    def test_opencode_auth_json(self):
        text = json.dumps({"opencode-go": {"type": "api", "key": "oc_sk_x" + "y" * 12}})
        self.assertEqual(credentials.key_from_opencode_auth(text), "oc_sk_x" + "y" * 12)
        self.assertIsNone(credentials.key_from_opencode_auth('{"opencode-go":{"key":"nope"}}'))


# ─────────────────────────────────────────────── 峰谷 / 套娃 / 聚合 ───────


class PeakHourTests(unittest.TestCase):
    def ms(self, *args):
        return to_ms(datetime(*args, tzinfo=CN))

    def test_windows(self):
        self.assertTrue(peakhours.is_peak(self.ms(2026, 10, 15, 9, 0), "opencode_go"))
        self.assertTrue(peakhours.is_peak(self.ms(2026, 10, 15, 11, 59), "opencode_go"))
        self.assertFalse(peakhours.is_peak(self.ms(2026, 10, 15, 12, 0), "opencode_go"))
        self.assertTrue(peakhours.is_peak(self.ms(2026, 10, 15, 14, 0), "opencode_go"))
        self.assertFalse(peakhours.is_peak(self.ms(2026, 10, 15, 18, 0), "opencode_go"))

    def test_go_has_no_holiday_exemption(self):
        self.assertTrue(peakhours.is_peak(self.ms(2026, 10, 1, 10, 0), "opencode_go"))
        self.assertFalse(peakhours.is_peak(self.ms(2026, 10, 1, 10, 0), "deepseek"))

    def test_deepseek_skips_weekend_but_honours_makeup_days(self):
        self.assertFalse(peakhours.is_peak(self.ms(2026, 10, 17, 10, 0), "deepseek"))
        self.assertTrue(peakhours.is_peak(self.ms(2026, 10, 10, 10, 0), "deepseek"))


class GateTests(unittest.TestCase):
    def test_month_empty_zeroes_lower_windows(self):
        meters = {"month": meter("month", limit=60.0, used=60.0),
                  "week": meter("week", limit=30.0, used=1.0),
                  "fiveHour": meter("fiveHour", limit=12.0, used=0.5, resets_at=999)}
        self.assertEqual(gates.blocking_window(meters, "week"), "month")
        self.assertEqual(gates.blocking_window(meters, "fiveHour"), "month")
        self.assertEqual(gates.effective_percent(meters, "fiveHour"), 0.0)
        self.assertIsNone(gates.blocking_window(meters, "month"))

    def test_week_empty_blocks_only_fivehour(self):
        meters = {"month": meter("month", used=1.0), "week": meter("week", limit=30.0, used=30.0),
                  "fiveHour": meter("fiveHour", used=0.1)}
        self.assertEqual(gates.blocking_window(meters, "fiveHour"), "week")
        self.assertIsNone(gates.blocking_window(meters, "week"))

    def test_missing_meter_is_not_empty(self):
        meters = {"fiveHour": meter("fiveHour", used=0.1)}
        self.assertFalse(gates.is_empty(meters, "month"))

    def test_worst_window_prefers_the_blocking_wide_one(self):
        meters = {"month": meter("month", limit=60.0, used=60.0),
                  "week": meter("week", limit=30.0, used=30.0),
                  "fiveHour": meter("fiveHour", limit=12.0, used=12.0)}
        self.assertEqual(gates.worst_window(meters), "month")


class AggregateTests(unittest.TestCase):
    def calls(self):
        peak = to_ms(datetime(2026, 10, 15, 10, 0, tzinfo=CN))     # 峰
        off = to_ms(datetime(2026, 10, 15, 13, 0, tzinfo=CN))      # 谷
        return [
            Call("a", peak, model="m1", outcome="succeeded", cost_usd=1.0,
                 input_tokens=100, output_tokens=20, cache_read_tokens=50,
                 duration_ms=1000, ttft_ms=200, finish='{"reason":"tool_calls"}'),
            Call("b", off, model="m1", outcome="failed", status_code=429,
                 error_code="inference_failed"),
            Call("c", off, model="m2", outcome="succeeded", cost_usd=2.0,
                 reasoning_tokens=5, finish='{"reason":"stop"}'),
        ]

    def test_rollup(self):
        r = aggregate.rollup(self.calls())
        self.assertEqual(r.stats.calls, 3)
        self.assertEqual((r.stats.succeeded, r.stats.failed), (2, 1))
        self.assertAlmostEqual(r.stats.cost_usd, 3.0)
        self.assertAlmostEqual(r.stats.peak_cost, 1.0)
        self.assertAlmostEqual(r.stats.offpeak_cost, 2.0)
        self.assertEqual(r.stats.total_tokens, 175)
        self.assertEqual(len(r.models), 2)
        self.assertEqual(r.models[0].model, "m2", "按费用排序")
        self.assertEqual(len(r.days), 1)
        self.assertEqual(r.stats.avg_duration_ms, 1000.0)
        self.assertEqual(dict(r.finishes), {"tool_calls": 1, "stop": 1})
        self.assertTrue(any(s["status_code"] == 429 for s in r.statuses), str(r.statuses))

    def test_merge_stats_and_days(self):
        a = aggregate.rollup(self.calls())
        b = aggregate.rollup([Call("d", self.calls()[0].started_at_ms, model="m1",
                                   outcome="succeeded", cost_usd=0.5)])
        merged = aggregate.merge_stats([a.stats, b.stats])
        self.assertEqual(merged.calls, 4)
        self.assertAlmostEqual(merged.cost_usd, 3.5)
        days = aggregate.merge_days([a, b])
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0].calls, 4)

    def test_empty_rollup(self):
        r = aggregate.rollup([])
        self.assertTrue(r.empty)
        self.assertTrue(r.stats.success_rate != r.stats.success_rate)   # NaN


# ───────────────────────────────────────────────── 多工作区 / 取数 ───────


class StatusChannelTests(unittest.TestCase):
    """额度该走 Cookie 还是 Key —— Key 是账号级的，问具体工作区时不能用它。"""

    def profile(self, complete=True, key=True):
        return credentials.GoProfile(
            name="p",
            auth="Fe26.2**a" if complete else "",
            session="st_" + "0" * 20 if complete else "",
            key="oc_sk_x" if key else "")

    def test_workspace_query_prefers_cookie(self):
        channels = opencode_go._status_channels(self.profile(), Workspace("wrk_A", "A"))
        self.assertEqual(channels[0], "cookie", "问具体工作区必须先走 Cookie+x-org-id")

    def test_key_only_profile_uses_key(self):
        channels = opencode_go._status_channels(self.profile(complete=False), Workspace("", ""))
        self.assertEqual(channels, ["key"])

    def test_no_workspace_context_uses_key_first(self):
        channels = opencode_go._status_channels(self.profile(), None)
        self.assertEqual(channels[0], "key")


class GatherTests(unittest.TestCase):
    """用一个假数据源验证取数层的多工作区装配（不联网）。"""

    def setUp(self):
        self.orig = providers.SOURCES
        self.seen: list = []

        def fake_collect(ctx):
            for ws_id, name in (("wrk_A", "甲"), ("wrk_B", "乙")):
                workspace = Workspace(ws_id, name, "default")
                if not ctx.wants_workspace(workspace):
                    continue
                self.seen.append(ws_id)
                data = WorkspaceData(workspace)
                data.meters = [meter("month", limit=60.0, used=10.0)]
                if ctx.with_calls:
                    data.calls = [Call(f"{ws_id}-1", ctx.start_ms or 0, model="m",
                                       outcome="succeeded", cost_usd=1.0)]
                ctx.snapshot.workspaces.append(data)
            ctx.snapshot.balances.append(Balance(total=40.0, account="deepseek"))

        providers.SOURCES = (providers.Source("fake", "假源", (), fake_collect),)

    def tearDown(self):
        providers.SOURCES = self.orig

    def test_gather_builds_snapshot(self):
        snapshot = fetch.gather(_empty_creds(), days=1)
        self.assertEqual([w.workspace.id for w in snapshot.workspaces], ["wrk_A", "wrk_B"])
        self.assertEqual(self.seen, ["wrk_A", "wrk_B"])
        self.assertEqual(len(snapshot.balances), 1)
        self.assertEqual(len(snapshot.workspaces[0].calls), 1)

    def test_only_workspaces_filters_by_id_name_or_prefix(self):
        self.seen.clear()
        fetch.gather(days=1, only_workspaces=["乙"])
        self.assertEqual(self.seen, ["wrk_B"])
        self.seen.clear()
        fetch.gather(days=1, only_workspaces=["wrk_A"])
        self.assertEqual(self.seen, ["wrk_A"])

    def test_with_calls_false_skips_details(self):
        snapshot = fetch.gather(_empty_creds(), days=1, with_calls=False)
        self.assertTrue(all(not w.calls for w in snapshot.workspaces))

    def test_a_broken_source_does_not_sink_the_others(self):
        def boom(ctx):
            raise RuntimeError("炸了")

        providers.SOURCES = (providers.Source("bad", "坏源", (), boom),
                             providers.SOURCES[0])
        snapshot = fetch.gather(_empty_creds(), days=1)
        self.assertTrue(snapshot.workspaces, "坏源不该拖垮好源")
        self.assertTrue(any("坏源" in w for w in snapshot.warnings))

    def test_find_workspace(self):
        snapshot = fetch.gather(_empty_creds(), days=1)
        self.assertEqual(fetch.find_workspace(snapshot, "wrk_A").workspace.id, "wrk_A")
        self.assertEqual(fetch.find_workspace(snapshot, "乙").workspace.id, "wrk_B")
        self.assertIsNone(fetch.find_workspace(snapshot, "没有"))


# ─────────────────────────────────────────────── 报告（不读库，只看内存）──


class ReportTests(unittest.TestCase):
    def snapshot(self, workspaces=2, calls=True, multiplier=1.0):
        end = to_ms(datetime(2026, 10, 15, 20, 0, tzinfo=CN))
        start = end - 86_400_000
        snap = Snapshot(generated_ms=end, start_ms=start, end_ms=end,
                        multiplier=multiplier,
                        multiplier_source="配置" if multiplier != 1.0 else "")
        peak = to_ms(datetime(2026, 10, 15, 10, 0, tzinfo=CN))
        off = to_ms(datetime(2026, 10, 15, 13, 0, tzinfo=CN))
        for index, (ws_id, name) in enumerate([("wrk_A", "甲"), ("wrk_B", "乙")][:workspaces]):
            data = WorkspaceData(Workspace(ws_id, name, "default"))
            data.meters = [meter("fiveHour", limit=12.0, used=1.0),
                           meter("week", limit=30.0, used=10.0),
                           meter("month", limit=60.0, used=20.0)]
            data.meter_source = "cookie（按工作区）"
            data.retention_days = 30
            if calls:
                data.calls = [
                    Call(f"{ws_id}-a", peak, model="deepseek-v4.1-flash",
                         outcome="succeeded", cost_usd=1.0 + index, input_tokens=100,
                         output_tokens=20, cache_read_tokens=50, duration_ms=1000,
                         ttft_ms=200, finish='{"reason":"tool_calls"}'),
                    Call(f"{ws_id}-b", off, model="deepseek-v4-flash", outcome="failed",
                         status_code=429, error_code="inference_failed"),
                ]
            snap.workspaces.append(data)
        snap.balances.append(Balance(total=40.00, granted=20.00, topped_up=20.00,
                                     account="deepseek"))
        return snap

    def test_single_workspace_text(self):
        data = report.build(self.snapshot(workspaces=1), label="今天")
        self.assertEqual(len(data.workspaces), 1)
        self.assertFalse(data.multi)
        text = report.render(data, "text")
        for needle in ("AI API 用量报告", "【额度与余额】", "【调用情况】", "5 小时",
                       "DeepSeek 余额", "不做本地保存"):
            self.assertIn(needle, text)
        self.assertNotIn("|", text, "纯文本版不该出现 Markdown 表格记号")
        self.assertIn("tool_calls", text)

    def test_multi_workspace_sections_and_totals(self):
        data = report.build(self.snapshot(workspaces=2), label="今天")
        self.assertTrue(data.multi)
        text = report.render(data, "text")
        self.assertIn("【甲（wrk_A）】", text)
        self.assertIn("【乙（wrk_B）】", text)
        self.assertIn("【合计】", text)
        self.assertEqual(data.combined.calls, 4)
        self.assertAlmostEqual(data.combined.cost_usd, 1.0 + 2.0)

    def test_multiplier_is_folded_into_windows(self):
        data = report.build(self.snapshot(workspaces=1, multiplier=1.3333), label="今天")
        view = data.workspaces[0].windows[0]
        self.assertAlmostEqual(view.real_limit_usd, view.limit_usd * 1.3333, places=4)
        self.assertGreater(view.real_remaining_usd, view.remaining_usd)
        text = report.render(data, "text")
        self.assertIn("额度倍率 ×1.33", text)
        self.assertIn("折合真实", text)

    def test_no_multiplier_line_when_one(self):
        data = report.build(self.snapshot(workspaces=1), label="今天")
        self.assertNotIn("额度倍率", report.render(data, "text"))

    def test_rates_fill_the_projection(self):
        snap = self.snapshot(workspaces=1)
        data = report.build(snap, label="今天",
                            rates={"wrk_A": {"fiveHour": 1.0, "week": 1.0, "month": 1.0}})
        view = data.workspaces[0].windows[0]
        self.assertEqual(view.burn_usd_per_hour, 1.0)
        self.assertIsNotNone(view.hours_left)
        self.assertTrue(view.projection)

    def test_gated_window_reports_zero_and_explains(self):
        snap = self.snapshot(workspaces=1)
        snap.workspaces[0].meters = [meter("fiveHour", limit=12.0, used=1.0),
                                     meter("week", limit=30.0, used=10.0),
                                     meter("month", limit=60.0, used=60.0)]
        data = report.build(snap, label="今天")
        five = next(w for w in data.workspaces[0].windows if w.window == "fiveHour")
        self.assertEqual(five.blocked_by, "month")
        self.assertEqual(five.effective_percent, 0.0)
        self.assertIn("压住", report.render(data, "text"))

    def test_alerts_are_computed_live(self):
        snap = self.snapshot(workspaces=1)
        snap.workspaces[0].meters = [meter("month", limit=60.0, used=59.0)]
        snap.balances = [Balance(total=2.0, account="deepseek")]
        data = report.build(snap, label="今天")
        joined = " ".join(data.alerts)
        self.assertIn("仅剩", joined)

    def test_markdown_and_json_render(self):
        data = report.build(self.snapshot(workspaces=2), label="今天")
        md = report.render(data, "md")
        self.assertIn("| 窗口 |", md)
        self.assertIn("## 合计", md)
        payload = json.loads(report.render(data, "json"))
        self.assertEqual(len(payload["workspaces"]), 2)
        self.assertEqual(payload["totals"]["calls"], 4)
        self.assertEqual(payload["workspaces"][0]["retention_days"], 30)

    def test_empty_snapshot_renders(self):
        snap = Snapshot(generated_ms=now_ms(), start_ms=None, end_ms=now_ms())
        data = report.build(snap, label="全部历史")
        text = report.render(data, "text")
        self.assertIn("AI API 用量报告", text)
        self.assertFalse(data.totals_ok)
        report.render(data, "md")
        report.render(data, "json")

    def test_workspace_errors_surface_in_report(self):
        snap = self.snapshot(workspaces=1)
        snap.workspaces[0].errors = ["明细：没有控制台 Cookie（额度照常）"]
        snap.warnings = ["乙 明细：HTTP 403"]
        text = report.render(report.build(snap, label="今天"), "text")
        self.assertIn("没有控制台 Cookie", text)
        self.assertIn("HTTP 403", text)


# ─────────────────────────────────────────────────── 大屏面板契约 ─────────


def panel_snapshot(meters=None, calls=False, balance=40.0, warning=None):
    end = to_ms(datetime(2026, 10, 15, 20, 0, tzinfo=CN))
    snap = Snapshot(generated_ms=end, start_ms=end - 86_400_000, end_ms=end)
    data = WorkspaceData(Workspace("wrk_A", "甲", "default"))
    data.meters = meters if meters is not None else [
        meter("fiveHour", limit=12.0, used=1.0),
        meter("week", limit=30.0, used=10.0),
        meter("month", limit=60.0, used=20.0),
    ]
    data.meter_source = "cookie（按工作区）"
    data.retention_days = 30
    if calls:
        data.calls = [Call("a", end - 1000, model="m", outcome="succeeded", cost_usd=1.0)]
    snap.workspaces.append(data)
    if balance is not None:
        snap.balances.append(Balance(total=balance, account="deepseek"))
    if warning:
        snap.warnings.append(warning)
    return snap


class PanelContractTests(unittest.TestCase):
    """大屏契约：字段、等级、headline 与 worst_window 必须自洽。"""

    def test_top_level_contract_fields(self):
        payload = panel.build(panel_snapshot(), label="当前")
        for key in ("schema", "generated_at_ms", "generated_at", "fresh_for_ms", "window",
                    "thresholds", "level", "headline", "workspaces", "balances",
                    "alerts", "warnings", "totals"):
            self.assertIn(key, payload, f"契约缺字段 {key}")
        self.assertEqual(payload["schema"], panel.SCHEMA)
        self.assertIn("go_low_percent", payload["thresholds"])
        self.assertIn("balance_low_cny", payload["thresholds"])

    def test_meter_fields_and_units(self):
        payload = panel.build(panel_snapshot(), label="当前")
        ws = payload["workspaces"][0]
        self.assertTrue(ws["has_go"])
        meter_payload = ws["meters"][0]
        for key in ("window", "label", "short", "limit_usd", "used_usd", "remaining_usd",
                    "remaining_percent", "usable_percent", "blocked_by", "blocked_by_label",
                    "resets_at_ms", "reset_in", "level"):
            self.assertIn(key, meter_payload, f"契约缺字段 {key}")
        self.assertEqual([m["short"] for m in ws["meters"]], ["5H", "周", "月"])

    def test_levels(self):
        self.assertEqual(panel.meter_level(meter("fiveHour", limit=12.0, used=1.0)), "ok")
        self.assertEqual(panel.meter_level(meter("fiveHour", limit=12.0, used=11.0)), "warn")
        self.assertEqual(panel.meter_level(meter("fiveHour", limit=12.0, used=12.0)), "critical")
        self.assertEqual(panel.meter_level(meter("fiveHour", limit=0.0, used=0.0)), "unknown")
        self.assertEqual(panel.meter_level(meter("fiveHour", limit=12.0, used=1.0),
                                           blocked=True), "critical")
        self.assertEqual(panel.balance_level(Balance(total=1.0)), "warn")
        self.assertEqual(panel.balance_level(Balance(total=0.0)), "critical")
        self.assertEqual(panel.balance_level(Balance(total=float("nan"))), "unknown")
        self.assertEqual(panel.worst_level(["ok", "warn", "unknown"]), "warn")
        self.assertEqual(panel.worst_level([]), "unknown")

    def test_gated_window_zeroes_usable_percent_but_keeps_remaining_percent(self):
        payload = panel.build(panel_snapshot(meters=[
            meter("fiveHour", limit=12.0, used=1.0),
            meter("week", limit=30.0, used=10.0),
            meter("month", limit=60.0, used=60.0),      # 本月空了
        ]), label="当前")
        by_window = {m["window"]: m for m in payload["workspaces"][0]["meters"]}
        self.assertEqual(by_window["fiveHour"]["usable_percent"], 0.0)
        self.assertGreater(by_window["fiveHour"]["remaining_percent"], 0.0,
                           "池子口径的百分比仍然有值，消费方不该用它上色")
        self.assertEqual(by_window["fiveHour"]["blocked_by"], "month")
        self.assertEqual(by_window["fiveHour"]["blocked_by_label"], "本月")
        self.assertEqual(by_window["fiveHour"]["level"], "critical")

    def test_headline_agrees_with_worst_window(self):
        """headline 必须和 worst_window 用同一把尺子，否则面板自相矛盾。"""
        payload = panel.build(panel_snapshot(meters=[
            meter("fiveHour", limit=12.0, used=2.5),     # 剩余 $9.5 ← 金额最少
            meter("week", limit=30.0, used=18.0),        # 剩余 $12
            meter("month", limit=60.0, used=42.0),       # 剩余 $18，但百分比最低
        ]), label="当前")
        ws = payload["workspaces"][0]
        self.assertEqual(ws["worst_window"], "fiveHour")
        self.assertIn("5 小时", payload["headline"])
        self.assertIn("$9.5", payload["headline"])

    def test_headline_when_a_window_is_dry(self):
        """有窗口见底时，headline 直接说「已用尽 + 多久回来」—— 大屏最该看到这句。"""
        payload = panel.build(panel_snapshot(meters=[
            meter("fiveHour", limit=12.0, used=1.0),
            meter("week", limit=30.0, used=10.0),
            meter("month", limit=60.0, used=60.0),
        ]), label="当前")
        self.assertIn("已用尽", payload["headline"])
        self.assertIn("本月", payload["headline"])

    def test_balance_fields_are_first_class(self):
        snap = panel_snapshot(balance=40.00)
        snap.balances[0].infos = deepseek.parse(BALANCE_JSON).infos
        snap.balances[0].is_available = True
        payload = panel.build(snap, label="当前")
        balance = payload["balances"][0]
        for key in ("account", "currency", "total", "granted", "topped_up",
                    "is_available", "infos", "usable", "low_threshold", "error", "level"):
            self.assertIn(key, balance, f"契约缺字段 {key}")
        self.assertEqual([i["currency"] for i in balance["infos"]], ["USD", "CNY"])
        self.assertAlmostEqual(balance["infos"][1]["total"], 40.00)
        self.assertEqual(balance["level"], "ok")

    def test_failed_balance_shows_as_unknown_not_missing(self):
        """取数失败要留一格显示「失败」，不能凭空消失 —— 消失看起来像「没配」。"""
        snap = panel_snapshot(balance=None)
        snap.balances.append(Balance(account="deepseek", error="HTTP 401"))
        payload = panel.build(snap, label="当前")
        balance = payload["balances"][0]
        self.assertEqual(balance["level"], "unknown")
        self.assertIsNone(balance["total"])
        self.assertEqual(balance["error"], "HTTP 401")
        self.assertTrue(any("取数失败" in a for a in payload["alerts"]))
        self.assertIn("取数失败", panel.render_text(payload))

    def test_unknown_does_not_drag_the_global_level_down(self):
        """一个没订阅的工作区不该把整块屏判成 unknown。"""
        self.assertEqual(panel.worst_level(["unknown", "ok"]), "ok")
        self.assertEqual(panel.worst_level(["unknown", "warn"]), "warn")
        self.assertEqual(panel.worst_level(["unknown"]), "unknown")

    def test_no_go_subscription_is_normal_not_error(self):
        snap = panel_snapshot(meters=[])
        payload = panel.build(snap, label="当前")
        ws = payload["workspaces"][0]
        self.assertFalse(ws["has_go"])
        self.assertEqual(ws["meters"], [])
        self.assertEqual(ws["level"], "unknown")
        self.assertEqual(ws["errors"], [])
        self.assertEqual(payload["level"], "ok", "余额正常就不该整体告警")

    def test_missing_data_is_null_not_zero(self):
        snap = panel_snapshot(meters=[meter("fiveHour", limit=0.0, used=0.0)], balance=None)
        payload = panel.build(snap, label="当前")
        meter_payload = payload["workspaces"][0]["meters"][0]
        self.assertIsNone(meter_payload["remaining_percent"])
        self.assertIsNone(meter_payload["usable_percent"])
        self.assertEqual(meter_payload["level"], "unknown")

    def test_alerts_and_global_level(self):
        payload = panel.build(panel_snapshot(balance=2.0), label="当前")
        self.assertEqual(payload["level"], "warn")
        self.assertTrue(any("余额" in a for a in payload["alerts"]))
        payload = panel.build(panel_snapshot(balance=0.0), label="当前")
        self.assertEqual(payload["level"], "critical")

    def test_warnings_pass_through(self):
        payload = panel.build(panel_snapshot(warning="乙 明细：HTTP 403"), label="当前")
        self.assertIn("乙 明细：HTTP 403", payload["warnings"])

    def test_render_is_one_line(self):
        payload = panel.build(panel_snapshot(), label="当前")
        line = panel.render(payload)
        self.assertEqual(line.count("\n"), 0, "NDJSON 必须一行一帧")
        parsed = json.loads(line)
        self.assertEqual(parsed["schema"], panel.SCHEMA)

    def test_text_render_separates_worst_from_blocked(self):
        payload = panel.build(panel_snapshot(meters=[
            meter("fiveHour", limit=12.0, used=2.5),
            meter("week", limit=30.0, used=10.0),
            meter("month", limit=60.0, used=60.0),
        ]), label="当前")
        text = panel.render_text(payload)
        self.assertIn("最紧", text)
        self.assertIn("压住", text)
        self.assertNotIn("被5 小时压住", text, "最紧 ≠ 被压住，别混为一谈")


class CredentialModeTests(unittest.TestCase):
    """``APIUSAGE_CREDENTIALS=env`` 时必须一个文件都不读。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "go.cookie"
        self.path.write_text(
            "auth=Fe26.2**fromfile\nsession=st_" + "0" * 20 + "\norg=wrk_FILE\n",
            encoding="utf-8")

    def test_file_mode_reads_the_file(self):
        with unittest.mock.patch.dict(os.environ, {"APIUSAGE_CREDENTIALS": "file"}, clear=False):
            profile = credentials.load_go_cookie(self.path)
        self.assertEqual(profile.auth, "Fe26.2**fromfile")
        self.assertEqual(profile.org, "wrk_FILE")

    def test_env_mode_ignores_the_file(self):
        with unittest.mock.patch.dict(os.environ, {"APIUSAGE_CREDENTIALS": "env"}, clear=False):
            os.environ.pop("OPENCODE_GO_AUTH", None)
            os.environ.pop("OPENCODE_GO_SESSION", None)
            profile = credentials.load_go_cookie(self.path)
        self.assertEqual(profile.auth, "", "env 模式下不该读文件")
        self.assertFalse(profile.complete)

    def test_env_mode_still_uses_environment(self):
        with unittest.mock.patch.dict(os.environ, {
            "APIUSAGE_CREDENTIALS": "env",
            "OPENCODE_GO_AUTH": "Fe26.2**fromenv",
            "OPENCODE_GO_SESSION": "st_" + "1" * 20,
            "OPENCODE_GO_ORG": "wrk_ENV",
        }, clear=False):
            profile = credentials.load_go_cookie(self.path)
        self.assertEqual(profile.auth, "Fe26.2**fromenv")
        self.assertEqual(profile.org, "wrk_ENV")
        self.assertTrue(profile.complete)

    def test_load_reports_the_mode(self):
        with unittest.mock.patch.dict(os.environ, {"APIUSAGE_CREDENTIALS": "env"}, clear=False):
            creds = credentials.load()
        self.assertEqual(creds.sources.get("mode"), "env")

    def test_unknown_mode_falls_back_to_file(self):
        with unittest.mock.patch.dict(os.environ, {"APIUSAGE_CREDENTIALS": "wat"}, clear=False):
            self.assertEqual(credentials.credential_mode(), "file")


def _empty_creds():
    return credentials.Credentials(profiles=[])


# ───────────────────────────────────────────────────────────── URL / 分页 ───


class UrlTests(unittest.TestCase):
    def test_logs_url_carries_everything(self):
        url = opencode_go.logs_url(1234, limit=50, until_ms=999, cursor="off-9")
        self.assertIn("since=1234", url)
        self.assertIn("limit=50", url)
        self.assertIn("until=999", url)
        self.assertIn("cursor=off-9", url)
        self.assertNotIn("until", opencode_go.logs_url(1234, limit=50))
        self.assertNotIn("cursor", opencode_go.logs_url(1234, limit=50))

    def test_export_url(self):
        url = opencode_go.export_url(1234, until_ms=999, fmt="csv")
        self.assertIn("/export", url)
        self.assertIn("format=csv", url)
        self.assertIn("since=1234", url)
        self.assertIn("until=999", url)

    def test_supports_cursor_detects_missing_placeholder(self):
        self.assertTrue(opencode_go.supports_cursor("http://x?a={cursor}"))
        self.assertFalse(opencode_go.supports_cursor("http://x?a={since}"))


class FakeServerMixin:
    """假服务端：导出/列表两条通道，语义照实测的来。"""

    def use_server(self, items, export=True):
        self.urls = []

        def fake(url, headers=None, timeout=None):
            self.urls.append(url)
            query = {}
            if "?" in url:
                query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&") if "=" in p)
            since = int(query.get("since", 0))
            until = int(query["until"]) if "until" in query else None
            rows = [i for i in items
                    if i["startedAt"] >= since and (until is None or i["startedAt"] <= until)]
            rows.sort(key=lambda i: i["startedAt"], reverse=True)

            if "/export" in url:
                if not export:
                    raise opencode_go.FetchError("HTTP 404", status=404)
                batch = rows[:1000]
                return json.dumps({
                    "content": json.dumps({"items": batch}),
                    "count": len(batch), "truncated": len(rows) > len(batch),
                    # 真实接口这里是「区间上界」；用错这个值下面的断言会挂
                    "until": 9_999_999_999_999,
                })
            index = int(query["cursor"].split("-")[1]) if "cursor" in query else 0
            limit = int(query.get("limit", 100))
            page = rows[index:index + limit]
            return json.dumps({
                "items": page,
                "nextCursor": f"off-{index + len(page)}" if index + len(page) < len(rows) else None,
                "until": 9_999_999_999_999,
                "retentionDays": 30,
            })

        self._orig_get = opencode_go.http_get
        opencode_go.http_get = fake

    def tearDownServer(self):
        if hasattr(self, "_orig_get"):
            opencode_go.http_get = self._orig_get


class FetchCallsTests(unittest.TestCase, FakeServerMixin):
    def setUp(self):
        self.profile = credentials.GoProfile(
            name="p", auth="Fe26.2**a", session="st_" + "0" * 20, key="oc_sk_x")
        self.workspace = Workspace("wrk_A", "A", "p")

    def tearDown(self):
        self.tearDownServer()

    def test_export_channel_gets_250_in_one_request(self):
        self.use_server(make_items(250))
        calls, truncated = opencode_go.fetch_calls(self.profile, self.workspace, 0)
        self.assertEqual(len(calls), 250)
        self.assertFalse(truncated)
        self.assertEqual(len(self.urls), 1)
        self.assertIn("/export", self.urls[0])

    def test_export_channel_pages_backwards(self):
        self.use_server(make_items(2500))
        calls, truncated = opencode_go.fetch_calls(self.profile, self.workspace, 0)
        self.assertEqual(len(calls), 2500)
        self.assertFalse(truncated, "翻到底了就不该说自己被截断")
        self.assertEqual(len(self.urls), 3)
        self.assertIn("until=", self.urls[1])

    def test_page_cap_marks_result_truncated(self):
        self.use_server(make_items(2500))
        calls, truncated = opencode_go.fetch_calls(self.profile, self.workspace, 0,
                                                   page_limit=1)
        self.assertEqual(len(calls), 1000, "只让翻一页就只该拿 1000 条")
        self.assertTrue(truncated, "还有更多没拿完，必须如实标记")

    def test_falls_back_to_list(self):
        self.use_server(make_items(250), export=False)
        calls, _ = opencode_go.fetch_calls(self.profile, self.workspace, 0)
        self.assertEqual(len(calls), 250)
        self.assertTrue(any("/export" not in u for u in self.urls))

    def test_list_uses_official_cursor(self):
        self.use_server(make_items(250))
        calls, _ = opencode_go.fetch_calls(self.profile, self.workspace, 0,
                                           use_export=False)
        self.assertEqual(len(calls), 250)
        self.assertIn("cursor=off-100", self.urls[1])

    def test_cursor_ignored_when_template_cannot_carry_it(self):
        self.use_server(make_items(250))
        orig = config.GO_LOGS_URL
        try:
            config.GO_LOGS_URL = "http://x/logs?limit={limit}&since={since}{until}"
            calls, _ = opencode_go.fetch_calls(self.profile, self.workspace, 0,
                                               use_export=False)
            self.assertEqual(len(calls), 250, "带不上游标时必须靠 until 翻到底")
            self.assertTrue(any("until=" in u for u in self.urls))
        finally:
            config.GO_LOGS_URL = orig

    def test_stops_at_since(self):
        items = make_items(2500)
        cutoff = items[1200]["startedAt"]
        self.use_server(items)
        calls, _ = opencode_go.fetch_calls(self.profile, self.workspace, cutoff)
        self.assertTrue(all(c.started_at_ms >= cutoff for c in calls))
        self.assertEqual(len(calls), 1300)

    def test_requires_complete_cookie(self):
        with self.assertRaises(opencode_go.FetchError):
            opencode_go.fetch_calls(credentials.GoProfile(key="oc_sk_x"), self.workspace, 0)


class DiscoveryTests(unittest.TestCase):
    def test_discovery_falls_back_to_profile_org(self):
        orig_orgs, orig_get = opencode_go.fetch_orgs, opencode_go.http_get

        def boom(profile, url=None):
            raise opencode_go.FetchError("HTTP 500")

        try:
            opencode_go.fetch_orgs = boom
            profile = credentials.GoProfile(
                name="p", auth="Fe26.2**a", session="st_" + "0" * 20, org="wrk_ONLY")
            found = opencode_go.discover_workspaces(profile)
            self.assertEqual([w.id for w in found], ["wrk_ONLY"])
        finally:
            opencode_go.fetch_orgs, opencode_go.http_get = orig_orgs, orig_get

    def test_key_only_profile_yields_placeholder_workspace(self):
        profile = credentials.GoProfile(name="p", key="oc_sk_x")
        found = opencode_go.discover_workspaces(profile)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].id, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
