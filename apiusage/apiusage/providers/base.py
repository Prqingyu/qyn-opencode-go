"""Provider 插件的公共约定。

加一个新数据源 = 写一个模块 + 在 :mod:`apiusage.providers` 里登记一个
:class:`Source`，别无他处需要改。每个 Source 只负责「往内存 Snapshot 里塞东西」，
不关心报告怎么写、更不关心存不存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..model import Snapshot


@dataclass
class CollectContext:
    """一次取数的上下文，交给每个 Source 用。"""

    snapshot: Snapshot
    creds: object
    start_ms: Optional[int]
    end_ms: int
    with_calls: bool = True
    # 只要这些工作区（None = 全部）；元素是 workspace id
    only_workspaces: Optional[list] = None
    use_export: bool = True
    page_limit: Optional[int] = None
    log: Callable = field(default=lambda message: None)

    def wants_workspace(self, workspace) -> bool:
        """``only_workspaces`` 里可以是 id、名字或名字前缀 —— 用户不会去记 ``wrk_…``。"""
        if not self.only_workspaces:
            return True
        keys = [str(k).lower() for k in self.only_workspaces]
        candidates = [workspace.id, workspace.name, workspace.short]
        for key in keys:
            for candidate in candidates:
                if candidate and candidate.lower() == key:
                    return True
            if workspace.id and workspace.id.lower().startswith(key):
                return True
        return False

    def warn(self, message: str) -> None:
        self.snapshot.warnings.append(message)


@dataclass
class Check:
    """一条自检结论（doctor 用）。``ok=None`` 表示只提示、不算失败。"""

    name: str
    ok: Optional[bool]
    detail: str = ""
    hint: str = ""


@dataclass
class Source:
    """一个数据源。"""

    name: str
    label: str
    needs: tuple
    collect: Callable[[CollectContext], None]
    check: Optional[Callable] = None

    def describe(self) -> str:
        return f"{self.label}（需要 {'/'.join(self.needs) or '无'}）"
