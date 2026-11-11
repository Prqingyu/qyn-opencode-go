"""DeepSeek 官方 API —— 账户级数据源，与 OpenCode 工作区无关。

实测（本机，`tools/_probe_deepseek.py`）：官方这一侧**只有余额一个口子**：

| 接口 | 结果 |
| --- | --- |
| ``GET /user/balance`` | 200，余额（``/v1/user/balance`` 同样可用） |
| ``GET /models`` | 200，模型清单（id / 名字 / 上下文窗口 / 模态） |
| ``/user/usage``、``/user/billing``、``/user/info``、``/usage``、``/billing`` | 全部 **404** |
| 不带 Key | 401 ``Authentication Fails (governor)`` |

所以「DS 官方余额」就是这一个接口，没有用量/账单可拉。
"""

from __future__ import annotations

import json
from typing import Optional

from .. import config
from ..http import FetchError, UA_BROWSER, http_get
from ..model import Balance, BalanceInfo, as_float
from .base import Check, CollectContext, Source

NAME = "deepseek"


def parse(text: Optional[str]) -> Optional[Balance]:
    """``GET /user/balance`` → 全部币种的 ``balance_infos``。

    逐条收，不按「恰好出现的那一条」取值：报文里可能同时有多币种，
    只认第一条会在字段换序或多加币种时**静默取错数**。
    """
    if not text:
        return None
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    infos = doc.get("balance_infos")
    if not isinstance(infos, list):
        return None

    parsed = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        currency = info.get("currency")
        if not isinstance(currency, str) or not currency:
            continue
        total = as_float(info.get("total_balance"))
        if total is None:
            # 有币种没数字 = 这条不可用；跳过它，而不是把一个空壳塞进结果里
            continue
        parsed.append(BalanceInfo(
            currency=currency,
            total=total,
            granted=as_float(info.get("granted_balance")),
            topped_up=as_float(info.get("topped_up_balance")),
        ))
    if not parsed:
        return None

    available = doc.get("is_available")
    primary = next((i for i in parsed if i.currency == "CNY"), parsed[0])
    return Balance(
        currency=primary.currency,
        total=primary.total,
        granted=primary.granted,
        topped_up=primary.topped_up,
        is_available=available if isinstance(available, bool) else None,
        infos=parsed,
    )


def fetch(key: str, url: Optional[str] = None) -> Balance:
    body = http_get(url or config.DEEPSEEK_BALANCE_URL, {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": UA_BROWSER,
    })
    if "is_available" not in body:
        raise FetchError("返回体不像余额接口（没有 is_available）", body=body[:200])
    balance = parse(body)
    if balance is None:
        raise FetchError("余额报文里没有可用的 balance_infos", body=body[:200])
    return balance


def collect(ctx: CollectContext) -> None:
    account = getattr(ctx.creds, "deepseek_account", "") or "deepseek"
    if not ctx.creds.has_deepseek:
        ctx.log("DeepSeek 余额：没有 Key，跳过")
        return
    try:
        balance = fetch(ctx.creds.deepseek_key)
    except FetchError as exc:
        # 失败也要留一条「取了但失败」的余额条目：否则大屏上这一格会凭空消失，
        # 看起来像「没配 DeepSeek」，而不是「取数失败」。
        ctx.snapshot.balances.append(Balance(account=account, error=str(exc)))
        ctx.warn(f"DeepSeek 余额取数失败：{exc}")
        return
    balance.account = account
    ctx.snapshot.balances.append(balance)


def check(creds) -> list:
    if not creds.has_deepseek:
        return [Check("DeepSeek Key", False, "没找到",
                      "~/.dsh/.credentials.yaml 里的 DEEPSEEK_API_KEY，或环境变量")]
    try:
        balance = fetch(creds.deepseek_key)
    except FetchError as exc:
        return [Check("DeepSeek 余额接口", False, str(exc))]

    def show(value) -> str:
        return "-" if value is None or value != value else f"{value:,.2f}"

    amounts = "，".join(f"{i.currency} {show(i.total)}" for i in balance.infos)
    return [Check("DeepSeek 余额接口", True,
                  f"{amounts}（is_available={balance.is_available}）")]


SOURCE = Source(name=NAME, label="DeepSeek 官方余额", needs=("deepseek_key",),
                collect=collect, check=check)
