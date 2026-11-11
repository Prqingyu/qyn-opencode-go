"""运行配置 —— 全部走环境变量，**不需要任何本地目录**。

这个工具刻意不落盘：没有数据库、没有状态文件、没有游标。要什么现场从接口取，
取完在内存里算完就丢。唯一可能落在磁盘上的是**凭据**（你要登录总得存在某处），
而且它也完全可以只放环境变量。
"""

from __future__ import annotations

import os
from pathlib import Path


HOME = Path(os.path.expanduser("~"))
DSH_CREDENTIALS = Path(os.environ.get("APIUSAGE_DSH_CREDENTIALS")
                       or HOME / ".dsh" / ".credentials.yaml")
OPENCODE_AUTH = Path(os.environ.get("APIUSAGE_OPENCODE_AUTH")
                     or HOME / ".local" / "share" / "opencode" / "auth.json")
# 单档案的 Cookie 文件（多档案用 APIUSAGE_GO_PROFILES）
GO_COOKIE_FILE = Path(os.environ.get("APIUSAGE_GO_COOKIE")
                      or HOME / ".apiusage" / "opencode_go.cookie")
# 多档案：JSON 数组，每项 {name, auth, session, org?, key?}
GO_PROFILES_FILE = os.environ.get("APIUSAGE_GO_PROFILES", "")

# ── 接口 ─────────────────────────────────────────────────────────────────
DEEPSEEK_BALANCE_URL = os.environ.get(
    "APIUSAGE_DEEPSEEK_URL", "https://api.deepseek.com/user/balance")
GO_BASE = os.environ.get("APIUSAGE_GO_BASE", "https://opencode.ai")
GO_STATUS_URL = os.environ.get("APIUSAGE_GO_STATUS_URL", f"{GO_BASE}/console/api/go/status")
GO_ORGS_URL = os.environ.get("APIUSAGE_GO_ORGS_URL", f"{GO_BASE}/console/api/orgs")
GO_LOGS_URL = os.environ.get(
    "APIUSAGE_GO_LOGS_URL",
    f"{GO_BASE}/console/api/request-logs?category=inference"
    "&limit={limit}&since={since}{until}{cursor}")
GO_LOGS_EXPORT_URL = os.environ.get(
    "APIUSAGE_GO_LOGS_EXPORT_URL",
    f"{GO_BASE}/console/api/request-logs/export?category=inference"
    "&format={format}&since={since}{until}")

# ── 取数参数 ─────────────────────────────────────────────────────────────
HTTP_TIMEOUT = float(os.environ.get("APIUSAGE_TIMEOUT", "20"))
# 导出是服务端现攒的，1 天量实测能超 20 秒 —— 单独给它一个更长的超时
EXPORT_TIMEOUT = float(os.environ.get("APIUSAGE_EXPORT_TIMEOUT", "90"))
GO_LOGS_LIMIT = int(os.environ.get("APIUSAGE_LOGS_LIMIT", "100"))
LOGS_PAGE_LIMIT = int(os.environ.get("APIUSAGE_LOGS_PAGE_LIMIT", "200"))
# 明细窗口的默认天数（上游只保留 30 天）
DEFAULT_DAYS = float(os.environ.get("APIUSAGE_DAYS", "1"))

# ── 提醒阈值（每次跑现场判断，不留状态）──────────────────────────────────
GO_LOW_PERCENT = float(os.environ.get("APIUSAGE_GO_LOW_PERCENT", "15"))
BALANCE_LOW_CNY = float(os.environ.get("APIUSAGE_BALANCE_LOW_CNY", "5"))

# 大屏面板：这份数据多久算「陈旧」（消费方超时后应显示为过期，而不是继续当准的）
PANEL_FRESH_MS = int(os.environ.get("APIUSAGE_PANEL_FRESH_MS", "300000"))

# 额度倍率：额度池里的 1 美元等于多少真实调用费。
# 0 = 自动（在进程内做一次两点采样实测）；活动会把标称额度放大
# （2026-10 那次 $60 → $80，倍率 80/60 = 1.3333），活动结束会自己回到 1.0。
QUOTA_MULTIPLIER = float(os.environ.get("APIUSAGE_QUOTA_MULTIPLIER", "0"))
# 自动实测倍率时，两次采样之间等多久（秒）
MULTIPLIER_SAMPLE_SECONDS = float(os.environ.get("APIUSAGE_MULTIPLIER_SAMPLE", "0"))
