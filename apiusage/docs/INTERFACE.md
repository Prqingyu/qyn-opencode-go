# 大屏接口契约 —— 给消费方（另一个 Agent）看的

本文是**对接文档**。你只需要认一个命令、一种 JSON 形状，不需要读 apiusage 的源码，
也**不需要自己重算任何业务规则**（剩余多少、实际能不能花、该显示什么等级，我们算好给你）。

## 一条命令

工作目录：`<repo-root>`

```powershell
python -m apiusage panel                        # 出一帧 JSON（单行，UTF-8）
python -m apiusage panel --repeat 60            # 每 60 秒一帧，持续吐 NDJSON
python -m apiusage panel --repeat 30 --count 5  # 只出 5 帧
python -m apiusage panel -w Default             # 只看一个工作区
python -m apiusage panel --with-stats           # 附带用量合计（慢，见下）
```

| 参数 | 说明 |
| --- | --- |
| `--format json`（默认） | 单行 JSON，一行一帧，直接 readline 即可 |
| `--format text` | 给人瞄一眼的紧凑文本，**不要拿它做数据源** |
| `--repeat N` | 每 N 秒刷新一帧；不加就只出一帧 |
| `--count N` | 配合 `--repeat`，最多出 N 帧 |
| `-w/--workspace X` | 只要某个工作区（id / 名字 / id 前缀） |
| `--with-stats` | 额外取调用明细并给 `totals`。**会让单帧从 ~2 秒变成几十秒**，大屏一般不需要 |

**耗时**：单帧约 1.5–3 秒（每个工作区一次额度请求 + 一次工作区列表）。
**退出码**：`0` = 至少拿到一块数据；`1` = 什么都没拿到（全挂，看 `warnings`）。

**刷新建议**：30–60 秒。额度本来就是 5 分钟粒度的东西，刷太快没意义。

## 最小消费示例

```python
import json, subprocess

proc = subprocess.Popen(
    ["python", "-m", "apiusage", "panel", "--repeat", "60"],
    cwd=r"<repo-root>",
    stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
)
for line in proc.stdout:                     # 一行一帧
    frame = json.loads(line)
    if frame["schema"] != "apiusage.panel/1":   # 版本守卫
        continue
    render(frame["level"], frame["headline"])   # 直接用，别自己算
    for ws in frame["workspaces"]:
        for m in ws["meters"]:
            color = {"ok": GREEN, "warn": AMBER,
                     "critical": RED, "unknown": GRAY}[m["level"]]
            draw_cell(ws["label"], m["short"], m["usable_percent"], color)
```

```powershell
# PowerShell：一帧一帧读
python -m apiusage panel --repeat 60 | ForEach-Object {
    $f = $_ | ConvertFrom-Json
    "$($f.level)  $($f.headline)"
}
```

## 这一帧里有哪两路数据

| 数据源 | 在帧里的位置 | 粒度 | 需要什么 |
| --- | --- | --- | --- |
| **OpenCode GO 额度** | `workspaces[].meters[]` | 每个工作区 3 个窗口（5 小时 / 本周 / 本月） | `oc_sk_` Key（只要额度）或控制台 Cookie（含明细） |
| **DeepSeek 官方余额** | `balances[]` | **账户级，一条**（与工作区数量无关） | `sk-…` API Key |

两路互相独立：一路挂了不影响另一路，各自的失败都记在自己身上（`errors` / `error`）。

## 顶层结构

```json
{
  "schema": "apiusage.panel/1",
  "generated_at_ms": 1700000000000,
  "generated_at": "2025-01-01 00:00:00",
  "fresh_for_ms": 300000,
  "window": {"start_ms": 1791981012959, "end_ms": 1700000000000,
             "days": null, "label": "当前", "with_calls": false},
  "thresholds": {"go_low_percent": 15.0, "balance_low_cny": 5.0},
  "level": "ok",
  "headline": "Default 5 小时 剩余 $9.60（80.00%）",
  "workspaces": [ ... ],
  "balances": [ ... ],
  "alerts": [],
  "warnings": [],
  "totals": {}
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema` | string | **契约版本**，当前 `apiusage.panel/1`。不匹配就别解析 |
| `generated_at_ms` | int | 这一帧的生成时刻，unix 毫秒（UTC） |
| `generated_at` | string | 北京时间文案，仅供直接上屏 |
| `fresh_for_ms` | int | 这份数据多久算新鲜（毫秒）。**`now - generated_at_ms > fresh_for_ms` 就该显示「数据陈旧」** |
| `window` | object | 额度是**实时快照**，与窗口无关；`with_calls=true` 时 `totals` 才对应这个窗口 |
| `thresholds` | object | 判定 `warn` 用的阈值，随数据一起给 —— **别在消费方硬编码 15 / 5** |
| `level` | string | 全局最严重等级（见下） |
| `headline` | string | 一句话摘要，可直接上大字 |
| `alerts` | string[] | 当前触发的提醒（**是状态，不是事件流**：条件还在就会一直出现） |
| `warnings` | string[] | 取数过程中的问题（某个源挂了等） |
| `totals` | object | 仅 `--with-stats` 时有值 |

## `workspaces[]`

```json
{
  "id": "wrk_EXAMPLE000000000000000000",
  "name": "Default",
  "label": "Default",
  "profile": "default",
  "has_go": true,
  "ok": true,
  "level": "ok",
  "source": "cookie（按工作区）",
  "retention_days": null,
  "worst_window": "fiveHour",
  "worst_window_label": "5 小时",
  "min_usable_percent": 29.28,
  "meters": [ ... ],
  "errors": []
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 工作区 id（`wrk_…` / `org_…`）。**只有 Key 没有 Cookie 时可能是空串**，别当主键用 |
| `name` / `label` | `label` 是给屏幕用的短名 |
| `profile` | 哪个登录档案（多账号时用于区分） |
| `has_go` | **false = 这个工作区没有 GO 订阅**，`meters` 会是空数组。这是正常状态，不是错误 |
| `ok` | 本轮有没有取到东西（额度或明细） |
| `source` | 额度来自哪条通道：`cookie（按工作区）` / `key（账号级，不分工作区）` / 以 `·无 GO 订阅` 结尾 |
| `retention_days` | 明细保留天数（默认 30）；只有取过明细才会有值，否则 `null` |
| `worst_window` | 剩余**金额**最少的窗口（三个池子天花板不同，所以比金额不比百分比） |
| `min_usable_percent` | 三个窗口里 `usable_percent` 的最小值 |
| `errors` | 该工作区自己的取数失败原因 |

## `meters[]` —— 上色只看这两个字段

```json
{
  "window": "fiveHour", "label": "5 小时", "short": "5H",
  "limit_usd": 12.0, "used_usd": 2.400000, "remaining_usd": 9.600000,
  "remaining_percent": 80.00,
  "usable_percent": 80.00,
  "blocked_by": null, "blocked_by_label": null,
  "resets_at_ms": 1700000000000,
  "reset_in": "1 小时 43 分后重置",
  "level": "ok"
}
```

| 字段 | 说明 |
| --- | --- |
| `window` | `fiveHour` / `week` / `month` |
| `*_usd` | 美元 |
| `remaining_percent` | **池子口径**的剩余百分比 |
| **`usable_percent`** | **实际能不能花**：上层窗口空了就是 `0`。**上色、算进度条都用这个** |
| `blocked_by` | 非 null 表示上层空了（值同 `window`），此时 `usable_percent` 为 0 |
| `resets_at_ms` / `reset_in` | 重置时刻与现成文案（可能取值 `未开始计时`） |
| `level` | `ok` / `warn` / `critical` / `unknown` |

> ⚠️ **不要用 `remaining_percent` 上色。** 池子里有钱 ≠ 花得出去：本月空了的时候，
> 周和 5 小时的 `remaining_percent` 可能还有 40%，但一次调用都过不了。
> 用 `usable_percent` 或直接用 `level`。

## `balances[]` —— DeepSeek 官方账户余额（账户级）

**不属于任何工作区**：一个 DeepSeek 账号只有一条余额，跟你有几个 OC 工作区无关。
官方接口是 `GET https://api.deepseek.com/user/balance`（`Bearer sk-…`）。

```json
{
  "account": "deepseek",
  "currency": "CNY",
  "total": 40.00,
  "granted": 20.0,
  "topped_up": 20.00,
  "is_available": true,
  "infos": [
    {"currency": "USD", "total": 1.00, "granted": 0.0, "topped_up": 1.00},
    {"currency": "CNY", "total": 40.00, "granted": 20.0, "topped_up": 20.00}
  ],
  "usable": true,
  "low_threshold": 5.0,
  "error": null,
  "level": "ok"
}
```

| 字段 | 说明 |
| --- | --- |
| `account` | 账户标识，目前恒为 `deepseek` |
| `currency` / `total` / `granted` / `topped_up` | **展示用的快捷方式**：有 CNY 就取 CNY，没有就取第一条。**单位人民币** |
| `infos[]` | **全部币种**，一条一个（`currency` / `total` / `granted` / `topped_up`）。接口给几个就报几个，不止 CNY |
| `is_available` | 官方自己的「账户是否可用」标志；`null` = 报文没给 |
| `usable` | 有没有拿到有效数字（`false` 时 `total` 是 `null`） |
| `low_threshold` | 判定 `warn` 的阈值（人民币），同 `thresholds.balance_low_cny` |
| `error` | **取数失败时非空**（如 `"HTTP 401"`），此时 `total: null`、`level: "unknown"` |
| `level` | `ok` / `warn` / `critical` / `unknown` |

> **单位别混**：`balances[]` 里的钱是**人民币**，`workspaces[].meters[]` 里的钱是**美元**。

> **API 边界（实测）**：DeepSeek 官方这一侧**只有余额一个口子**。`/user/usage`、
> `/user/billing`、`/user/info`、`/usage`、`/billing` 全部 404，没有用量/账单接口可拉。
> 另一个能用的是 `GET /models`（模型清单：id / 名字 / 上下文窗口 / 模态），
> 当前没有接进面板。

## 等级规则（消费方直接拿来用，不用复算）

| 等级 | meter | balance |
| --- | --- | --- |
| `critical` | 被上层压住、或已用尽 | `total <= 0` |
| `warn` | `usable_percent <= thresholds.go_low_percent`（默认 15） | `total <= thresholds.balance_low_cny`（默认 5） |
| `unknown` | 没取到数 | 数无效 |
| `ok` | 其余 | 其余 |

* 工作区的等级 = 它所有窗口里最严重的那个
* 全局 `level` = 所有工作区与余额里最严重的那个
* 严重度排序：`ok` = `unknown` < `warn` < `critical`（`unknown` 不该当告警闪屏，
  但要用 `ok=false` / `has_go=false` 表达"没数据"，配合 `warnings` 提示）

## 失败模式

| 情况 | JSON 里长什么样 |
| --- | --- |
| 某个工作区取数失败 | 该工作区 `errors` 有原因、`level="unknown"`；**其他工作区照常**；`warnings` 也有一条 |
| 整个网络断了 | `workspaces: []`、`warnings` 有原因、`level="unknown"`、**退出码 1** |
| Cookie 过期 | 额度还在（走 Key 或 Cookie 兜底），明细相关的 `retention_days` 为 null |
| 某工作区没有 GO 订阅 | `has_go: false`、`meters: []`、`level: "unknown"` —— **正常状态** |
| DeepSeek Key 无效/过期 | `balances[0].error = "HTTP 401"`、`total: null`、`level: "unknown"`，并进 `alerts` |
| 没配 DeepSeek Key | `balances: []`（和上面那条不同：这条是「没配」，那条是「配了但取不到」） |
| 刚启动还没取到数 | `usable_percent: null` —— **别渲染成 0%**，那是"没数据"不是"用完了" |

## 不要做的事

1. **别自己算剩余百分比/是否可用** —— 用 `usable_percent` 和 `level`，套娃规则在
   `apiusage/gates.py` 里，复刻一遍迟早不一致。
2. **别把 `unknown` 当 0** —— 见上表。
3. **别硬编码阈值** —— 读 `thresholds`。
4. **别指望 `id` 一定非空** —— 只有 Key 时它是空串。
5. **别高频刷** —— 30–60 秒足够；`--with-stats` 更慢，别放进轮询。
6. **别解析 `--format text`** —— 那是给人看的，随时会变。

## 稳定性承诺

* 顶层 `schema` 版本号只在**破坏性变更**时递增；消费方按它做守卫。
* 现有字段**只增不改名、不改语义、不改单位**；新增字段消费方忽略即可。
* `level` 的四个取值、`window` 的三个取值、`usable_percent` 的语义属于契约的一部分。
