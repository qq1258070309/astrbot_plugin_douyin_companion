"""结构化错误码：与 dsh-douyin ``lib/backend.js`` 的 7 个码保持一致。

移植自 JianHeShui/dsh-douyin（MIT）。错误码收敛的意义是：上游无论返回中文、
英文还是 JSON 信封，插件对模型 / 对用户只暴露有限、可预期的几种状态。
"""
from __future__ import annotations

import re

ERROR_CODES: tuple[str, ...] = (
    "not_authenticated",
    "challenge_required",
    "rate_limited",
    "not_found",
    "parse_failed",
    "profile_in_use",
    "backend_unavailable",
    "invalid_args",
)


class DouyinError(Exception):
    """带稳定 code 的领域异常；未知码一律降级，避免泄漏上游细节。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "backend_unavailable"
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"[{self.code}] {self.message}"


_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"请先登录|未登录|登录已失效|cookie\s*已失效|需要登录|重新登录|not_authenticated", re.I), "not_authenticated"),
    (re.compile(r"验证码|verify_check|verify|challenge|滑块|安全验证|人机验证|risk_control", re.I), "challenge_required"),
    (re.compile(r"429|rate.?limit|限流|请求频繁|操作频繁|too many", re.I), "rate_limited"),
    (re.compile(r"不存在|未找到|not found|无数据|数据为空|已删除", re.I), "not_found"),
    (re.compile(r"JSON|解析失败|parse|expecting|unexpected token", re.I), "parse_failed"),
    (re.compile(r"profile.?in.?use|目录被占用|EBUSY|ProcessSingleton|user data directory is already in use", re.I), "profile_in_use"),
)


def map_error_text(text: str) -> str:
    """把上游 stdout/stderr / error.message 映射为稳定错误码。"""
    sample = str(text or "")
    for pattern, code in _PATTERNS:
        if pattern.search(sample):
            return code
    return "backend_unavailable"


def normalize_code(raw: str | None, fallback_text: str = "") -> str:
    """信封里的 error.code 优先；未知码不信任，改按错误文本重判（对齐上游 backend.js）。"""
    if raw and raw in ERROR_CODES:
        return raw
    return map_error_text(fallback_text)
