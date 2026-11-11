"""凭据：一个**登录档案**（profile）能看到该账号下的所有工作区。

关键认识：**多工作区不需要多套 Cookie**。同一个登录会话换 ``x-org-id`` 就能查
每个工作区（``GET /console/api/orgs`` 会把它能看到的都列出来）。所以这里把
「登录」和「工作区」分开建模：

* :class:`GoProfile` = 一次登录（Cookie 三件套 + 可选的 ``oc_sk_`` Key）
* 工作区 = (profile, org_id)，由 :mod:`apiusage.providers.opencode_go` 现场发现

确实有两套登录（两个账号）时，用 ``APIUSAGE_GO_PROFILES`` 给一个 JSON 数组即可。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config

# ── 识别规则：与 DSH-PET 的 GoCredentials.Scan 同源 ──────────────────────────
RE_AUTH_COOKIE = re.compile(r"(Fe26\.2\*\*[^\s\"';,)]+)")
RE_AUTH_ASSIGN = re.compile(r"auth\s*=\s*([^\s;\"]+)")
RE_AUTH_PAIR = re.compile(r"\"\s*auth\s*\"\s*,\s*\"([^\"]+)\"")
RE_SESSION = re.compile(r"(st_[0-9a-fA-F\-]{16,})")
RE_WORKSPACE = re.compile(r"((?:wrk|org)_[0-9A-Za-z]+)")
RE_SERVICE_KEY = re.compile(r"(oc_sk_[0-9A-Za-z_\-]{10,})")

RE_DSH_DEEPSEEK = re.compile(r"DEEPSEEK_API_KEY:\s*[\"']?(\S+?)[\"']?\s*$", re.M)
RE_DSH_GO_KEY = re.compile(r"OPENCODE_GO_API_KEY:\s*[\"']?(\S+?)[\"']?\s*$", re.M)


def _clean(value: Optional[str]) -> Optional[str]:
    """Cookie 可能是 URL 编码过的（HAR 里常见），解一下再交出去。"""
    if value is None:
        return None
    text = value.strip().strip('"').strip("'")
    if not text:
        return None
    if "%" in text:
        try:
            unquoted = urllib.parse.unquote(text)
            if len(unquoted) >= len(text):
                text = unquoted
        except Exception:  # noqa: BLE001 - 解不开就用原值，别让凭据读取出错
            pass
    return text or None


def _first(pattern: re.Pattern, text: str) -> Optional[str]:
    m = pattern.search(text)
    return _clean(m.group(1)) if m else None


def mask(secret: Optional[str], keep: int = 6) -> str:
    """日志/报告里打印凭据时一律走这里。"""
    if not secret:
        return "(none)"
    if len(secret) <= keep * 2:
        return secret[:keep] + "…"
    return f"{secret[:keep]}…{secret[-4:]} (len={len(secret)})"


@dataclass
class GoProfile:
    """一次登录。``auth`` + ``session`` 是明细接口的入场券。"""

    name: str = "default"
    auth: str = ""
    session: str = ""
    org: str = ""          # 可选：只看这个工作区
    key: str = ""          # oc_sk_…：够拿额度，不够拿明细

    @property
    def complete(self) -> bool:
        return bool(self.auth and self.session)

    @property
    def usable(self) -> bool:
        return self.complete or bool(self.key)

    def header(self) -> str:
        return f"oc_locale=zh; auth={self.auth}; __Host-console_session={self.session}"

    def describe(self) -> str:
        return (f"{self.name}: auth={'OK' if self.auth else '缺'} "
                f"session={'OK' if self.session else '缺'} "
                f"key={'OK' if self.key else '缺'} org={self.org or '（自动发现）'}")


@dataclass
class Credentials:
    profiles: list
    deepseek_key: Optional[str] = None
    sources: Optional[dict] = None

    @property
    def has_deepseek(self) -> bool:
        return bool(self.deepseek_key)

    @property
    def has_go(self) -> bool:
        return any(p.usable for p in self.profiles)

    @property
    def has_go_logs(self) -> bool:
        return any(p.complete for p in self.profiles)

    @property
    def default_profile(self) -> Optional[GoProfile]:
        return self.profiles[0] if self.profiles else None


# ────────────────────────────────────────────────────────────── 扫描 ──────


def scan_go_credentials(text: Optional[str]) -> GoProfile:
    """从任意粘贴文本里挑出 Cookie 三件套（HAR / cURL / DevTools / 裸 Cookie 行都行）。"""
    if not text:
        return GoProfile(name="")
    auth = (_first(RE_AUTH_COOKIE, text) or _first(RE_AUTH_ASSIGN, text)
            or _first(RE_AUTH_PAIR, text))
    return GoProfile(
        name="default",
        auth=auth or "",
        session=_first(RE_SESSION, text) or "",
        org=_first(RE_WORKSPACE, text) or "",
        key=_first(RE_SERVICE_KEY, text) or "",
    )


def credential_mode() -> str:
    """凭据读取模式：``file``（默认）或 ``env``。

    ``APIUSAGE_CREDENTIALS=env`` 时**一个文件都不读** —— 只看环境变量。适合
    多用户/共享机器、CI、或者"临时跑一次不想留痕"。不设就是 ``file``。
    """
    mode = (os.environ.get("APIUSAGE_CREDENTIALS") or "file").strip().lower()
    return "env" if mode == "env" else "file"


def load_go_cookie(path: Optional[Path] = None) -> GoProfile:
    """单档案 Cookie：环境变量优先；``env`` 模式下**不读文件**。"""
    env = os.environ.get
    profile = GoProfile(name="default")
    if credential_mode() != "env":
        file_path = Path(path or config.GO_COOKIE_FILE)
        if file_path.exists():
            try:
                profile = scan_go_credentials(file_path.read_text(encoding="utf-8"))
            except OSError:
                profile = GoProfile(name="default")
    return GoProfile(
        name="default",
        auth=env("OPENCODE_GO_AUTH") or profile.auth,
        session=env("OPENCODE_GO_SESSION") or profile.session,
        org=env("OPENCODE_GO_ORG") or profile.org,
        key=env("OPENCODE_GO_API_KEY") or profile.key,
    )


def save_go_cookie(profile: GoProfile, path: Optional[Path] = None) -> Path:
    file_path = Path(path or config.GO_COOKIE_FILE)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# OpenCode 控制台会话 —— 按次明细用，别外发\n"
        f"auth={profile.auth}\n"
        f"session={profile.session}\n"
        f"org={profile.org}\n"
    )
    file_path.write_text(body, encoding="utf-8")
    try:
        # POSIX 上设成 0600。**Windows 上 chmod 只动只读位、形同虚设** ——
        # 那边的实际保护来自用户 profile 的 ACL（实测与本机的 ~/.dsh/.credentials.yaml
        # 完全同级：SYSTEM / Administrators / 本人）。
        file_path.chmod(0o600)
    except OSError:
        pass
    return file_path


def load_go_profiles(spec: Optional[str] = None) -> list:
    """多档案：``APIUSAGE_GO_PROFILES`` 可以是 JSON 文本，也可以是个存在的文件路径。

    形状：``[{"name":"主号","auth":"Fe26.2**…","session":"st_…","org":"wrk_…","key":"oc_sk_…"}]``
    其中 ``org`` 可省 —— 省了就自动发现该档案下的所有工作区。
    """
    spec = spec if spec is not None else config.GO_PROFILES_FILE
    if not spec:
        return []
    raw = spec
    candidate = Path(os.path.expanduser(spec))
    if "\n" not in spec and candidate.exists():
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError:
            return []
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(doc, dict):
        doc = doc.get("profiles") or []
    if not isinstance(doc, list):
        return []

    profiles = []
    for index, item in enumerate(doc):
        if not isinstance(item, dict):
            continue
        profiles.append(GoProfile(
            name=str(item.get("name") or f"profile{index + 1}"),
            auth=_clean(item.get("auth")) or "",
            session=_clean(item.get("session")) or "",
            org=_clean(item.get("org")) or "",
            key=_clean(item.get("key")) or "",
        ))
    return profiles


# ────────────────────────────────────────────────────────────── 汇总 ──────


def load_dsh_keys(path: Optional[Path] = None) -> dict:
    """从 DSH 凭据库取 Key（正则，不引 YAML 依赖 —— 只要那两行）。"""
    file_path = Path(path or config.DSH_CREDENTIALS)
    keys = {"deepseek": None, "opencode_go": None}
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return keys
    m = RE_DSH_DEEPSEEK.search(text)
    if m:
        keys["deepseek"] = _clean(m.group(1))
    m = RE_DSH_GO_KEY.search(text)
    if m:
        keys["opencode_go"] = _clean(m.group(1))
    return keys


def key_from_opencode_auth(text: Optional[str]) -> Optional[str]:
    """``{"opencode-go":{"type":"api","key":"oc_sk_…"}}`` → key。"""
    if not text:
        return None
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    entry = doc.get("opencode-go") if isinstance(doc, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    return key if isinstance(key, str) and key.startswith("oc_sk_") else None


def load() -> Credentials:
    """把各处来源合成凭据；多档案优先，其次单档案，最后只有 Key。

    ``APIUSAGE_CREDENTIALS=env`` 时**只读环境变量**：DSH 凭据库、opencode 的
    auth.json、Cookie 文件统统不碰。这样在共享机器上也敢跑。
    """
    sources: dict = {}
    env = os.environ.get
    mode = credential_mode()
    sources["mode"] = mode

    if mode == "env":
        keys = {"deepseek": None, "opencode_go": None}
    else:
        keys = load_dsh_keys()

    deepseek = env("DEEPSEEK_API_KEY") or env("DSHPET_KEY") or keys.get("deepseek")
    if env("DEEPSEEK_API_KEY") or env("DSHPET_KEY"):
        sources["deepseek"] = "env"
    elif keys.get("deepseek"):
        sources["deepseek"] = f"dsh:{config.DSH_CREDENTIALS.name}"

    shared_key = env("OPENCODE_GO_API_KEY") or env("DSHPET_GO_KEY") or keys.get("opencode_go")
    if env("OPENCODE_GO_API_KEY") or env("DSHPET_GO_KEY"):
        sources["go_key"] = "env"
    elif keys.get("opencode_go"):
        sources["go_key"] = f"dsh:{config.DSH_CREDENTIALS.name}"
    if not shared_key and mode != "env":
        try:
            shared_key = key_from_opencode_auth(config.OPENCODE_AUTH.read_text(encoding="utf-8"))
            if shared_key:
                sources["go_key"] = f"opencode:{config.OPENCODE_AUTH.name}"
        except OSError:
            pass

    profiles = load_go_profiles()
    if profiles:
        sources["go_profiles"] = f"{len(profiles)} 个档案"
    else:
        single = load_go_cookie()
        if single.auth or single.session or single.key:
            profiles = [single]
            sources["go_cookie"] = ("（来自环境变量）" if mode == "env"
                                    else str(config.GO_COOKIE_FILE))

    if not profiles and shared_key:
        profiles = [GoProfile(name="default", key=shared_key)]
        sources.setdefault("go_profiles", "只有 Key（额度可用，明细不可用）")

    # Key 是账号级的：档案自己没带就补上共享的那把
    for profile in profiles:
        if not profile.key:
            profile.key = shared_key or ""

    return Credentials(profiles=profiles, deepseek_key=deepseek, sources=sources)
