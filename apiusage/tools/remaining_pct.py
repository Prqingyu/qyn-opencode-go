#!/usr/bin/env python3
"""只要「还剩百分之多少」—— 一个文件，25 行，只用标准库。

不需要控制台 Cookie，不需要 x-org-id，不需要数据库，不需要装任何东西：

    python tools/remaining_pct.py
    5H 剩余 82.5%   本周 剩余 39.5%   本月 剩余 29.9%
    5H 22:12 后重置

Key 从这三个地方按顺序找：环境变量 ``OPENCODE_GO_API_KEY`` → ``~/.dsh/.credentials.yaml``
的 ``OPENCODE_GO_API_KEY``。想塞进状态墙 / 定时任务 / 手机推送，直接调它就行。

（为什么不用爬网页：控制台是 SPA，页面 HTML 里只有 2KB 的空壳，占比全是 JS 渲染的 ——
实测 ``Rolling usage`` / ``usedMicroCents`` 一个字都搜不到。这个接口是同一份数据，
但是纯 JSON。）
"""

import json
import os
import re
import sys
import urllib.request

URL = "https://opencode.ai/console/api/go/status"


def key() -> str:
    env = os.environ.get("OPENCODE_GO_API_KEY")
    if env:
        return env.strip()
    path = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    with open(path, encoding="utf-8") as fh:
        m = re.search(r"OPENCODE_GO_API_KEY:\s*[\"']?(\S+?)[\"']?\s*$", fh.read(), re.M)
    if not m:
        raise SystemExit("没找到 OPENCODE_GO_API_KEY（环境变量或 ~/.dsh/.credentials.yaml）")
    return m.group(1)


def main() -> int:
    req = urllib.request.Request(URL, headers={
        "Authorization": "Bearer " + key(),
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        doc = json.load(resp)

    meters = (doc.get("access") or {}).get("meters") or {}
    labels = {"fiveHour": "5H", "week": "本周", "month": "本月"}
    parts, resets = [], []
    for name in ("fiveHour", "week", "month"):
        node = meters.get(name)
        if not node:
            continue
        limit = float(node["limitMicroCents"])
        used = float(node["usedMicroCents"])
        left = 100.0 * (limit - used) / limit if limit else 0.0
        parts.append(f"{labels[name]} 剩余 {max(0.0, left):.1f}%")
        resets.append(node.get("resetsAt"))

    if not parts:
        print("status 里没有额度窗口", file=sys.stderr)
        return 1
    print("   ".join(parts))
    if resets[0]:
        print(f"{labels['fiveHour']} {resets[0][11:16]} UTC 后重置")
    return 0


if __name__ == "__main__":
    sys.exit(main())
