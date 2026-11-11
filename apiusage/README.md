# apiusage —— AI API 用量监测与报告

看「我的 AI API 花了多少钱、还能用多久」，**不落盘**：没有数据库、没有游标、没有状态文件。
要什么现场从接口取，在内存里算完打印，结束就没了。

* **多工作区** —— 一个 Cookie 能看到该账号下的**所有** OpenCode 工作区，逐个取数、
  逐个分节；`--workspace` 挑一个，不给就当全部。
* **插件化** —— 加一个数据源只要写一个模块 + 在注册表加一行。
* **纯标准库** —— `urllib` + 内建模块，没有第三方依赖。

```
OPENCODEGO/apiusage/
├── apiusage/
│   ├── model.py          ← 中立领域模型（Balance / Meter / Call / Workspace / Snapshot）
│   ├── http.py           ← 一个 GET + 统一错误类型（任何 provider 复用）
│   ├── credentials.py    ← 登录档案（profile）：一个 Cookie = 一个账号 = N 个工作区
│   ├── providers/
│   │   ├── base.py       ← Source / CollectContext 协议（插件接口）
│   │   ├── opencode_go.py← 工作区发现 + 额度 + 调用明细
│   │   └── deepseek.py   ← DeepSeek 余额
│   ├── fetch.py          ← 取数装配（内存 Snapshot；`measure()` 两点采样）
│   ├── aggregate.py      ← 内存聚合（按模型/按日/峰谷/结束原因）
│   ├── panel.py          ← **大屏接口**：稳定 JSON 契约（给别的程序消费）
│   ├── report.py         ← 渲染 text / md / json
│   ├── peakhours.py      ← 峰谷（含 2026 法定节假日表）
│   ├── gates.py          ← GO 三窗口「套娃」规则
│   ├── timeutil.py       ← 北京时间、区间计算
│   └── cli.py            ← 命令行
├── docs/INTERFACE.md     ← **大屏对接契约**（给另一个 Agent 看的）
├── tests/                ← 82 个单测（不联网、不落盘）
└── tools/
    ├── verify_offline.py ← 离线端到端自检（假控制台，两个工作区）
    └── remaining_pct.py  ← 只要剩余占比（零依赖单文件）
```

## 快速开始

```powershell
cd <repo-root>

python -m apiusage doctor                          # 自检：凭据、工作区、三个接口
python -m apiusage workspaces                      # 列出能看到的工作区
python -m apiusage status                          # 当前额度/余额（不取明细，快）
python -m apiusage status --format pct             # 只要占比，喂状态墙/脚本
python -m apiusage panel                           # 大屏：一帧 JSON（契约见 docs/INTERFACE.md）
python -m apiusage panel --repeat 60               # 大屏：每 60 秒一帧，持续吐 NDJSON
python -m apiusage report --days 1                 # 最近 1 天的用量报告
python -m apiusage report --days 7 --workspace 甲  # 只看一个工作区（支持名字/前缀）
python -m apiusage report --days 7 --watch 120     # 先两点采样，把燃烧速率也测出来
python -m apiusage export --days 7 --format csv --out 全量.csv
```

## 大屏接口（给别的程序消费）

`python -m apiusage panel` 输出一份**稳定的 JSON 契约**，专门给另一个 Agent / 状态墙用：

```powershell
python -m apiusage panel                       # 一帧 JSON，单行
python -m apiusage panel --repeat 60           # 每 60 秒一帧（NDJSON，一行一帧）
python -m apiusage panel -w Default            # 只看一个工作区
```

设计原则是**消费方不用重算任何业务规则**：剩余多少、实际能不能花（套娃 gate）、
该显示什么等级（`ok` / `warn` / `critical` / `unknown`）、倒计时文案、该说的那句话
（`headline`），全部算好在帧里。上色只看 `usable_percent` 和 `level` 两个字段。

**完整字段表、等级规则、失败模式、稳定性承诺 → [`docs/INTERFACE.md`](docs/INTERFACE.md)**

单帧约 1.5–3 秒（每个工作区一次额度请求），建议 30–60 秒刷一次。

## 不落盘意味着什么

| | 以前（有库） | 现在 |
| --- | --- | --- |
| 历史数据 | 自己攒 SQLite | **问接口要**（上游保留 30 天） |
| 增量游标 | 存 `kv` 表 | 不需要 —— 每次按 `--days/--since` 要一段 |
| 燃烧速率 | 采样时间序列差分 | `--watch N` 一个进程内**两点采样** |
| 额度倍率 | 从序列里实测 | `--watch N` 实测，或 `APIUSAGE_QUOTA_MULTIPLIER` |
| 提醒状态 | 存 `alert` 表去抖 | 现场判断「当前是不是有问题」 |
| 计划任务 | 每 5 分钟喂库 | **已卸载**（没有库可喂） |
| 凭据 | `data/opencode_go.cookie` | `~/.apiusage/opencode_go.cookie` 或环境变量 |

代价是**取数变慢**：30 天全量约 2 分钟（导出通道 11 次请求）。收益是没有状态要维护、
没有文件会过期、换台机器拷过去就能跑。

`tools/verify_offline.py` 里有一条硬断言：**跑完整条链不产生任何文件**。

## 多工作区

**一个 Cookie 能看到该账号下的所有工作区**（`GET /console/api/orgs`），逐个换
`x-org-id` 就能分别取数 —— 所以「两个工作区轮流用」不需要两套凭据。

```powershell
python -m apiusage workspaces                      # Default（wrk_…）、示例账号（org_…）
python -m apiusage report --days 3                 # 两个都出，末尾给【合计】
python -m apiusage report --days 3 -w Default      # 只要甲
python -m apiusage status -w wrk_EXAMPLE          # 支持 id 前缀
```

**两个账号**（两套登录）时，用 `APIUSAGE_GO_PROFILES` 给一个 JSON 数组：

```powershell
$env:APIUSAGE_GO_PROFILES = '[{"name":"主号","auth":"Fe26.2**…","session":"st_…"},
                              {"name":"小号","auth":"Fe26.2**…","session":"st_…"}]'
```

`org` 可省 —— 省了就自动发现该档案下的全部工作区。

### ⚠️ Key 是**账号级**的，分不出工作区

`Authorization: Bearer oc_sk_…` 打 `go/status` 拿到的是**账号下那个 GO 订阅**的数字，
**跟 `x-org-id` 无关**。所以只要是在问「某个具体工作区」，代码就强制先走
Cookie + `x-org-id`，否则两个工作区会显示一模一样的数字（这是真踩过的 bug，
`verify_offline.py` 里有一条断言专门盯着它）。

Key 只在没有 Cookie、或不知道工作区时兜底，来源会标成 `key（账号级，不分工作区）`。

## 凭据从哪来

| 来源 | 取到的东西 |
| --- | --- |
| 环境变量 `OPENCODE_GO_AUTH` / `_SESSION` / `_ORG` | 登录 Cookie（优先级最高） |
| `~/.apiusage/opencode_go.cookie` | 同上（`python -m apiusage cookie` 写的） |
| `APIUSAGE_GO_PROFILES` | 多档案（JSON 文本或文件路径） |
| `~/.dsh/.credentials.yaml` | `DEEPSEEK_API_KEY`、`OPENCODE_GO_API_KEY` |
| `~/.local/share/opencode/auth.json` | `opencode-go.key` |

### 一条文件都不读：`APIUSAGE_CREDENTIALS=env`

```powershell
$env:APIUSAGE_CREDENTIALS = 'env'
$env:OPENCODE_GO_AUTH = 'Fe26.2**…'
$env:OPENCODE_GO_SESSION = 'st_…'
python -m apiusage panel
```

`env` 模式下 DSH 凭据库、opencode 的 `auth.json`、Cookie 文件**统统不碰**，只看环境变量。
适合共享/多用户机器、CI，或者"临时跑一次不想留痕"。`doctor` 会打出 `mode: env`。

**什么时候该用哪边** —— 实测两个文件的 ACL 完全同级（SYSTEM / Administrators / 本人），
所以默认的 Cookie 文件并不比 `~/.dsh/.credentials.yaml`（里面就有你的 Key）更危险；
而环境变量在 Windows 上要写进 profile、计划任务 XML（存在注册表里）或命令行，
反而更容易被读到。**默认用文件，共享机器上用 `env`。**

### 抓 Cookie

1. 浏览器打开 `https://opencode.ai/console/<工作区>/go`，F12 → **网络**；
2. **点筛选栏的 `Fetch/XHR`**（停在「全部」上看到的全是 js/css，会以为抓不到）；
3. 刷新页面，点开任意一条 `opencode.ai/console/api/...` 请求；
4. 右键 → **复制 → 以 cURL 格式复制**（整段最好，`x-org-id` 也在里面）；
5. 粘进来：

```powershell
python -m apiusage cookie                  # 直接粘到终端，Ctrl-Z 回车
python -m apiusage cookie --from-file 抓包.txt
```

只覆盖**认出来的**字段，粘半截不会把已经能用的凭据毁掉。

### ⚠️ 最容易卡住人的坑：`x-org-id` 写错会伪装成 404

| 请求 | 返回 |
| --- | --- |
| Cookie + 对的 `x-org-id` | `200` + 数据 |
| Cookie + **错的** `x-org-id` | `404 {"_tag":"NotFound"}` |
| Cookie + 不带 `x-org-id` | `400 {"_tag":"OrgRequired"}` |
| 只有 Bearer Key | `403 {"_tag":"Forbidden"}` |

**404 的第一嫌疑不是路径，是 `x-org-id`。** `doctor` 会替你问
`GET /console/api/orgs`（这个接口**不需要** `x-org-id`）并把正确的工作区列出来。

## 接口（前端 bundle + 本机实测）

控制台的接口表就写在前端 JS 里（`/console/assets/*.js`），比猜 URL 靠谱。

| 接口 | 需要 | 一次多少 | 实测结论 |
| --- | --- | ---: | --- |
| `GET /api/orgs` | Cookie | — | 列出所有工作区；**不需要 `x-org-id`** |
| `GET /api/go/status` | Cookie+`x-org-id`（Key 是账号级） | — | 三窗口 `limitMicroCents`/`usedMicroCents`/`resetsAt`，**1 美元 = 1e8 micro-cents** |
| `GET /api/request-logs` | Cookie+`x-org-id` | **100** | 倒序；返回 `nextCursor` 与 `retentionDays`；`limit` >100 直接 400 |
| `GET /api/request-logs/export` | Cookie+`x-org-id` | **1000** | `{content,count,truncated,until}`，`format=json\|csv`；**`limit` 被忽略** |
| `GET /api/request-logs/<id>` | Cookie+`x-org-id` | — | 字段与列表**完全相同**（43 个） |
| `GET /api/activity/usage` | — | — | **403**：聚合用量只给 workspace 管理员，个人账号拿不到 |
| `GET /api/user`、`/api/policies/current-budgets` | Cookie | — | `acc_…`+邮箱；本账号预算 `[]` |
| `GET https://api.deepseek.com/user/balance` | `Bearer sk-…` | — | `is_available` + `balance_infos[]`，**多币种全收**（展示取 CNY） |
| `GET https://api.deepseek.com/models` | `Bearer sk-…` | — | 模型清单（id / 名字 / 上下文窗口 / 模态）；**当前没接** |

**DS 官方这一侧只有余额一个口子**：`/user/usage`、`/user/billing`、`/user/info`、
`/usage`、`/billing` 实测**全部 404**，没有用量/账单接口可拉；不带 Key 是
401 `Authentication Fails (governor)`。

**token 用量**（每条明细里就有）：

```
inputTokens  outputTokens  reasoningTokens  cacheReadTokens  cacheWriteTokens
cacheWrite1hTokens   reasoningBudgetTokens   ← 实测本账号恒为 0，出现就收
timeToFirstTokenMs  durationMs  finish={"reason":"tool_calls"}   ← tool_calls / stop / length
```

### 两条取数通道

| 通道 | 一次 | 实测 |
| --- | ---: | --- |
| **导出**（首选） | 1000 | 30 天全量 ≈ 111 秒 |
| 列表（兜底） | 100（优先 `nextCursor`） | 7 天全量 ≈ 286 秒 |

导出是服务端现攒的，**单次能超过 20 秒**，所以它有独立的 90 秒超时
（`APIUSAGE_EXPORT_TIMEOUT`）。导出失败自动退回列表，不丢数据。

明细**只保留 30 天**（`retentionDays`）—— 再往回翻也没有，`doctor` 会报这个数。

三条实测踩出来的规矩：

* **明细是倒序给的**，所以「把 `since` 往前推」只会反复拿同一批最新数据 ——
  补历史必须**往回翻**：拿本批最旧的 `startedAt - 1` 当下一批的 `until`（闭区间）；
* **导出响应里的 `until` 是「区间上界」不是「本批最旧」** —— 直接拿它翻页会拿到
  998/1000 条重复；
* 自定义模板里漏写 `{cursor}` 时游标会被 `format` 静默吃掉、翻页永远停在第一页 ——
  代码会检测并自动退回 `until` 分页。

## 报告里有什么

* **额度与余额** —— 三窗口的限额/已用/剩余/剩余百分比/重置倒计时；高一层空了会标注
  「实际可用 0%，被《本月》压住」；`--watch` 还会给「按当前速率还能用约 X 小时」。
* **调用情况** —— 次数、成功率、费用（**峰 / 谷分开**）、token（输入/输出/推理）、
  缓存读写与命中率、平均耗时与平均首字。
* **按模型 / 按日 / 结束原因 / 失败明细**。
* **合计** —— 工作区多于一个时给一份总和。
* **提醒** —— 现场判断（额度低于阈值、用尽、余额不足），不留状态。

### ⚠️ 活动会把标称额度放大（额度倍率）

2026-10 那次 OpenCode 把 $60 的月池提到了 **$80**（DS4.1），`80/60 = 1.3333`，
与实测的 1.33 一致。接口仍按标称的 $60 记账，所以**明细里每 $1 的真实调用费只扣池子
$0.75**。三个窗口完全自洽（这是最硬的证据）：

| 窗口 | 标称 | ×4/3 | 占月池 |
| --- | ---: | ---: | --- |
| 5 小时 | $12 | **$16** | 20% of $80 ✓ |
| 本周 | $30 | **$40** | 50% of $80 ✓ |
| 本月 | $60 | **$80** | 100% ✓ |

不落盘之后没法靠历史差分算它，所以：`report --watch 120` 现场两点采样实测，
或者直接给 `APIUSAGE_QUOTA_MULTIPLIER=1.3333`。没配、没测就不折算（倍率按 1.0）。

## 加一个新数据源

1. 写 `apiusage/providers/xxx.py`，实现一个 `collect(ctx: CollectContext)`，往
   `ctx.snapshot` 里塞东西；
2. 导出 `SOURCE = Source(name=..., label=..., needs=..., collect=collect, check=check)`；
3. 在 `apiusage/providers/__init__.py` 的 `SOURCES` 里加一项。

报告、聚合、CLI 都不需要改 —— 它们只认 `model.py` 里的中立类型。`python -m apiusage sources`
可以看当前登记了哪些。

## 验证

| 层 | 命令 | 覆盖 |
| --- | --- | --- |
| 单测 82 个 | `python -m unittest discover -s tests -t .` | 解析边界、凭据与多档案、`env` 凭据模式、峰谷与节假日、三窗口套娃、额度通道选择（Key vs Cookie）、多工作区装配、导出/列表两条通道与游标降级、聚合、三种报告格式、**大屏契约（字段/等级/headline 自洽/unknown 不拖垮全局）** |
| 离线端到端 35 项 | `python tools\verify_offline.py` | 假控制台里**两个工作区各 125 条明细**：一个 Cookie 发现两个工作区、额度与明细不串、两条通道、报告分节与合计、**面板契约**、**跑完不产生任何文件** |
| 真接口 | `python -m apiusage doctor` | 凭据、工作区发现、额度、明细、保留天数 |

## 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| 两个工作区数字**一模一样** | 说明走了账号级的 Key。确认 Cookie 有效（`status` 里来源应显示 `cookie（按工作区）`） |
| `明细：HTTP 404` | **八成是 `x-org-id` 不对**，不是路径 —— `doctor` 会列出正确的工作区 |
| `明细：HTTP 403` | Cookie 过期，重新 `python -m apiusage cookie` |
| 某工作区显示「无 GO 订阅」 | 该工作区确实没有 GO（接口返回 `null`），不是错 |
| 报告很慢 | 导出通道每次 1000 条、单次可能 >20 秒；用 `--days 1` 或 `--workspace` 缩小 |
| 想要更早的历史 | 上游只留 30 天，取不到 |
| 想让它定时跑 | 计划任务已卸载（没有库可喂）；要定时建议接到手机推送，别再落盘 |
| 只要一个数字 | `python tools\remaining_pct.py`（零依赖单文件）或 `status --format pct` |
