"""凭证脱敏：移植自 dsh-douyin ``lib/backend.js::redact``。

铁律：cookie / sessionid / passport_* / storage_state / Authorization
不得出现在返回值、日志、异常消息里。任何要出插件边界的字符串，
都必须先过 ``redact_text`` 或 ``redact``。
"""
from __future__ import annotations

import re
from typing import Any

_SECRET_KEYS = re.compile(
    r"^(cookie|cookies|sessionid|sessionid_ss|sid_guard|passport|"
    r"localStorage|storage_state|authorization|set-cookie)$",
    re.I,
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"((?:sessionid|sessionid_ss|sid_guard|odin_tt|msToken|ttwid|"
            r"passport_csrf_token|passport_auth_status|s_v_web_id|sid_tt|"
            r"uid_tt|uid_tt_ss)\s*[=:]\s*)[^;&,\s\"'}]+",
            re.I,
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"((?:cookie|set-cookie|authorization)\s*[:=]\s*)[^\n\r]+", re.I), r"\1<redacted>"),
    (re.compile(r"((?:localStorage|storage_state)[^\n]{0,40})[\"'][^\"']{20,}[\"']", re.I), r"\1<redacted>"),
)

_ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """去掉 Rich 进度条等 ANSI 色码，便于解析 JSON 信封。"""
    return _ANSI.sub("", str(text or ""))


def redact_text(value: Any) -> str:
    value = str(value if value is not None else "")
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact(value: Any) -> Any:
    """递归脱敏；命中的键整值替换，其余递归处理。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[str(key)] = "<redacted>" if _SECRET_KEYS.match(str(key)) else redact(item)
        return out
    return redact_text(value)
