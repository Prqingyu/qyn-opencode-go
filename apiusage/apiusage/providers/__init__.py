"""数据源注册表。

**加一个新数据源只要改这里一行**：写个模块，导出 ``SOURCE``，加进 ``SOURCES``。
"""

from __future__ import annotations

from typing import Optional

from . import deepseek, opencode_go
from .base import Check, CollectContext, Source  # noqa: F401 - 对外转出

SOURCES = (opencode_go.SOURCE, deepseek.SOURCE)

__all__ = ["SOURCES", "Source", "CollectContext", "Check", "get", "checks", "opencode_go",
           "deepseek"]


def get(name: str) -> Optional[Source]:
    for source in SOURCES:
        if source.name == name:
            return source
    return None


def checks(creds) -> list:
    """把所有数据源的自检串起来。"""
    out = []
    for source in SOURCES:
        if source.check is None:
            continue
        out.extend(source.check(creds))
    return out
