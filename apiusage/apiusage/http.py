"""HTTP 小工具：一个 GET，带解压与统一的错误类型。

独立成模块，是为了让任何 provider 都能复用同一套网络行为（超时、gzip、
把 HTTP 状态翻译成带上下文的 :class:`FetchError`），而不是各写一份。
"""

from __future__ import annotations

import gzip
import urllib.error
import urllib.request
import zlib
from typing import Optional

from . import config

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """网络层或 HTTP 状态层失败（解析层失败不抛，一律降级为「没读到」）。"""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _decompress(raw: bytes, encoding: str) -> bytes:
    if not encoding:
        return raw
    encoding = encoding.lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(raw)
        if "deflate" in encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 - 解不开就当没压缩，交给 JSON 解析报错
        return raw
    return raw


def http_get(
    url: str,
    headers: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> str:
    """一个 GET，返回响应体文本；非 2xx 抛 :class:`FetchError`。"""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as resp:
            raw = resp.read()
            body = _decompress(raw, resp.headers.get("Content-Encoding", ""))
            return body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = _decompress(exc.read(), exc.headers.get("Content-Encoding", "")).decode(
                "utf-8", "replace"
            )
        except Exception:  # noqa: BLE001
            detail = ""
        raise FetchError(f"HTTP {exc.code}", status=exc.code, body=detail[:400]) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"网络不可达: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError("请求超时") from exc
