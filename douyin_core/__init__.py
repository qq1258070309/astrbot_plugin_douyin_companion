"""douyin_core —— 平台无关的抖音能力核心。

从 dsh-douyin（MIT）移植的工程约束，不依赖 AstrBot SDK，也不依赖 companion_v2。
分层与陪伴小屋保持一致：main.py（宿主）→ douyin_core（领域），领域层不反向导入宿主。
"""
from .errors import DouyinError, map_error_text, normalize_code
from .gate import AsyncRateGate, GateConfig, WriteGate, clamp, jitter_ms
from .normalize import (
    dumps, extract_aweme_id, extract_sec_uid, json_safe, normalize_aweme,
    normalize_comments, normalize_hot_items, normalize_search_payload,
    normalize_stats, normalize_user, video_url,
)
from .redact import redact, redact_text, strip_ansi
from .store import DouyinStore, identity_key

__all__ = [
    "DouyinError", "map_error_text", "normalize_code",
    "AsyncRateGate", "GateConfig", "WriteGate", "clamp", "jitter_ms",
    "dumps", "extract_aweme_id", "extract_sec_uid", "json_safe",
    "normalize_aweme", "normalize_comments", "normalize_hot_items",
    "normalize_search_payload", "normalize_stats", "normalize_user", "video_url",
    "redact", "redact_text", "strip_ansi", "DouyinStore", "identity_key",
]
