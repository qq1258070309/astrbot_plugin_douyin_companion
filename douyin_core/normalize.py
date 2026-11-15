"""响应归一化：逐字段移植自 dsh-douyin ``lib/normalize.js``。

核心约定：**缺失即 None，绝不填 0、不猜值**。抖音数据非常稀疏，
0 与「未知」在后续分析 / 模型提示里含义完全不同。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.parse import quote


def _present(value: Any) -> Any:
    return None if value is None else value


def _number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def video_url(aweme_id: str | None) -> str | None:
    return f"https://www.douyin.com/video/{aweme_id}" if aweme_id else None


def normalize_stats(stats: Any) -> dict[str, Any]:
    if not isinstance(stats, dict):
        return {"plays": None, "likes": None, "comments": None, "shares": None, "collects": None}
    return {
        "plays": _number(stats.get("play_count")),
        "likes": _number(stats.get("digg_count")),
        "comments": _number(stats.get("comment_count")),
        "shares": _number(stats.get("share_count")),
        "collects": _number(stats.get("collect_count")),
    }


def normalize_author(author: Any) -> dict[str, Any]:
    if not isinstance(author, dict):
        return {"nickname": None, "unique_id": None, "sec_uid": None}
    return {
        "nickname": _string(author.get("nickname")),
        "unique_id": _string(author.get("unique_id")) or _string(author.get("short_id")),
        "sec_uid": _string(author.get("sec_uid")),
    }


def normalize_aweme(aweme: Any) -> dict[str, Any] | None:
    if not isinstance(aweme, dict):
        return None
    aweme_id = _string(aweme.get("aweme_id"))
    return {
        "aweme_id": aweme_id,
        "title": _string(aweme.get("desc")),
        "create_time": _number(aweme.get("create_time")),
        "media_type": _number(aweme.get("media_type")),
        "author": normalize_author(aweme.get("author")),
        "stats": normalize_stats(aweme.get("statistics")),
        "url": video_url(aweme_id),
        "is_top": _present(aweme.get("is_top")),
    }


def normalize_search_payload(data: Any) -> dict[str, Any]:
    result = (data or {}).get("result") or {} if isinstance(data, dict) else {}
    rows = result.get("data") if isinstance(result, dict) else None
    rows = rows if isinstance(rows, list) else []
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            skipped.append({"index": index, "reason": "invalid_row"})
            continue
        aweme = row.get("aweme_info")
        if aweme:
            normalized = normalize_aweme(aweme)
            if normalized:
                items.append(normalized)
            else:
                skipped.append({"index": index, "reason": "empty_aweme"})
        else:
            reason = "user_card" if row.get("user_list") else "no_aweme_info"
            skipped.append({"index": index, "reason": reason})
    if not isinstance(data, dict):
        data = {}
    return {
        "items": items,
        "skipped": skipped,
        "raw_count": len(rows),
        "has_more": bool(data.get("has_more")),
        "next_offset": data.get("next_offset"),
        "logid": data.get("logid"),
    }


def normalize_hot_items(rows: Any) -> list[dict[str, Any]]:
    rows = rows if isinstance(rows, list) else []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            item = {}
        word = _string(item.get("word")) or _string(item.get("title"))
        out.append({
            "rank": index + 1,
            "position": _number(item.get("position")),
            "title": word,
            "hot_value": _number(item.get("hot_value")),
            "view_count": _number(item.get("view_count")),
            "label": _present(item.get("label")),
            "sentence_id": _string(item.get("sentence_id")),
            "word_type": _number(item.get("word_type")),
            "search_url": f"https://www.douyin.com/search/{quote(word)}" if word else None,
        })
    return out


def normalize_comments(rows: Any) -> list[dict[str, Any]]:
    rows = rows if isinstance(rows, list) else []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            item = {}
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        out.append({
            "text": _string(item.get("text")),
            "user": {"nickname": _string(user.get("nickname")), "sec_uid": _string(user.get("sec_uid"))},
            "digg_count": _number(item.get("digg_count")),
            "reply_count": _number(item.get("reply_comment_total")),
            "create_time": _number(item.get("create_time")),
            "is_author": _present(item.get("is_author")),
        })
    return out


def normalize_user(user: Any) -> dict[str, Any] | None:
    if not isinstance(user, dict):
        return None
    sec_uid = _string(user.get("sec_uid"))
    return {
        "nickname": _string(user.get("nickname")),
        "unique_id": _string(user.get("unique_id")) or _string(user.get("short_id")),
        "sec_uid": sec_uid,
        "signature": _string(user.get("signature")),
        "follower_count": _number(user.get("follower_count")),
        "following_count": _number(user.get("following_count")),
        "total_favorited": _number(user.get("total_favorited")),
        "aweme_count": _number(user.get("aweme_count")),
        "city": _string(user.get("city")),
        "province": _string(user.get("province")),
        "ip_location": _string(user.get("ip_location")),
        "url": f"https://www.douyin.com/user/{sec_uid}" if sec_uid else None,
    }


_ID_PATTERNS = (
    re.compile(r"^\d{15,25}$"),
    re.compile(r"/video/(\d{15,25})"),
    re.compile(r"/note/(\d{15,25})"),
    re.compile(r"[?&]modal_id=(\d{15,25})"),
    re.compile(r"/share/video/(\d{15,25})"),
    re.compile(r"(\d{15,25})"),
)


def extract_aweme_id(value: Any) -> str | None:
    """从纯 id / 完整链接 / 短链文本中提取 aweme_id。"""
    if not value:
        return None
    text = str(value).strip()
    for pattern in _ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return None


def extract_sec_uid(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"/user/([A-Za-z0-9_\-.]+)", text)
    if match:
        return match.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_\-.]{20,}", text) else None


def json_safe(value: Any) -> Any:
    """递归去掉 undefined/NaN，保证结果能过 JSON 边界。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        # 保留 None：缺失即 null 是本项目的核心约定，不能被当成「无字段」丢掉。
        return {str(k): json_safe(v) for k, v in value.items()}
    return None


def dumps(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False)
