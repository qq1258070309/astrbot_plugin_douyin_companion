"""插件本地存储：共享视频条目 + 陪伴小屋写回调的幂等副本。

严格遵守 ``docs/extension-development.md`` 的身份边界：所有条目都带
``identity_key``（由 companion 传来的完整 context 规范化后取 SHA-256），
查询只在同一 identity_key 内进行，绝不靠昵称或单个平台 ID 跨角色/跨 Bot 合并。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

COUNT_KEYS = ("profile", "preference", "relationship", "private_conversation", "other", "total")


def identity_key(context: Any) -> str:
    """把陪伴小屋的完整身份对象规范化成不可逆、稳定的隔离键。"""
    try:
        canonical = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canonical = str(context)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DouyinStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS douyin_items (
                identity_key TEXT NOT NULL,
                aweme_id     TEXT NOT NULL,
                title        TEXT,
                author       TEXT,
                summary      TEXT,
                url          TEXT,
                occurred_at  REAL NOT NULL,
                PRIMARY KEY (identity_key, aweme_id)
            );
            CREATE INDEX IF NOT EXISTS idx_douyin_items_time
                ON douyin_items(identity_key, occurred_at DESC);
            CREATE TABLE IF NOT EXISTS companion_records (
                source_plugin TEXT NOT NULL,
                memory_id     TEXT NOT NULL,
                identity_key  TEXT NOT NULL,
                kind          TEXT,
                content       TEXT,
                occurred_at   REAL,
                PRIMARY KEY (source_plugin, memory_id)
            );
            """
        )
        self._db.commit()

    # ------------------------------------------------------------- 视频条目 --
    def put_video(
        self,
        key: str,
        aweme_id: str,
        *,
        title: str | None = None,
        author: str | None = None,
        summary: str | None = None,
        url: str | None = None,
        occurred_at: float | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO douyin_items"
                "(identity_key, aweme_id, title, author, summary, url, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, aweme_id, title, author, summary, url, occurred_at or time.time()),
            )
            self._db.commit()

    def search_videos(self, key: str, query: str, limit: int, max_chars: int) -> list[dict[str, str]]:
        """自动召回：只返回同一身份下、预算内的完整条目。"""
        limit = max(0, min(int(limit or 0), 20))
        max_chars = max(0, int(max_chars or 0))
        if limit == 0 or max_chars == 0:
            return []
        pattern = f"%{query[:40]}%" if query and query.strip() else "%"
        with self._lock:
            rows = self._db.execute(
                "SELECT aweme_id, title, author, summary, url, occurred_at "
                "FROM douyin_items WHERE identity_key = ? "
                "AND (title LIKE ? OR summary LIKE ? OR author LIKE ?) "
                "ORDER BY occurred_at DESC LIMIT 50",
                (key, pattern, pattern, pattern),
            ).fetchall()
        items: list[dict[str, str]] = []
        used = 0
        for aweme_id, title, author, summary, url, occurred_at in rows:
            parts = [f"抖音视频 {aweme_id}"]
            if author:
                parts.append(f"作者 {author}")
            if title:
                parts.append(f"文案：{title}")
            if summary:
                parts.append(summary)
            if url:
                parts.append(url)
            content = " | ".join(str(p) for p in parts)
            if used + len(content) > max_chars:
                continue  # 超预算跳过整条，不截断，避免半截语义
            used += len(content)
            items.append({
                "id": f"douyin:{aweme_id}",
                "content": content,
                "occurred_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(occurred_at)),
                "source": "astrbot_plugin_douyin_companion",
            })
            if len(items) >= limit:
                break
        return items

    def remember(self, key: str, content: str, *, tag: str = "manual",
                 source: str = "douyin_remember") -> str:
        """显式记忆写入：与 ``dy_remember`` 对应，但落到本插件身份隔离的 SQLite。

        返回稳定的 memory_id；同一内容按 identity_key + 内容哈希幂等。
        """
        import hashlib
        text = str(content or "").strip()[:4000]
        if not text:
            raise ValueError("记忆内容不能为空")
        memory_id = "dy-" + hashlib.sha256(
            f"{key}\n{tag}\n{text}".encode("utf-8")
        ).hexdigest()[:20]
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO companion_records"
                "(source_plugin, memory_id, identity_key, kind, content, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, memory_id, key, tag, text, time.time()),
            )
            self._db.commit()
        return memory_id

    def recent_memories(self, key: str, limit: int = 5) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT memory_id, kind, content, occurred_at FROM companion_records "
                "WHERE identity_key = ? AND source_plugin = 'douyin_remember' "
                "ORDER BY occurred_at DESC LIMIT ?",
                (key, max(0, min(int(limit), 20))),
            ).fetchall()
        return [{"id": r[0], "tag": r[1] or "", "content": r[2] or "",
                 "occurred_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[3]))}
                for r in rows]

    def search_all(self, key: str, query: str, limit: int, max_chars: int) -> list[dict[str, str]]:
        """自动召回 = 抖音共同经历 + douyin_remember 显式记忆，共用同一身份与预算。"""
        items = self.search_videos(key, query, limit, max_chars)
        used = sum(len(i["content"]) for i in items)
        if len(items) >= limit or used >= max_chars:
            return items[:limit]
        pattern = f"%{query[:40]}%" if query and query.strip() else "%"
        with self._lock:
            rows = self._db.execute(
                "SELECT memory_id, kind, content, occurred_at FROM companion_records "
                "WHERE identity_key = ? AND source_plugin = 'douyin_remember' "
                "AND content LIKE ? ORDER BY occurred_at DESC LIMIT 50",
                (key, pattern),
            ).fetchall()
        for memory_id, tag, content, occurred_at in rows:
            text = f"记忆（{tag or 'manual'}）：{content}"
            if used + len(text) > max_chars:
                continue
            used += len(text)
            items.append({
                "id": f"memory:{memory_id}",
                "content": text,
                "occurred_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(occurred_at)),
                "source": "astrbot_plugin_douyin_companion",
            })
            if len(items) >= limit:
                break
        return items

    # ------------------------------------------- companion.external-memory.v1 --
    def upsert_record(self, record: dict[str, Any]) -> str | None:
        """以 source_plugin + memory_id 幂等写入；无归属记录直接拒收。"""
        memory_id = str(record.get("memory_id") or "").strip()
        source = str(record.get("source_plugin") or "").strip()
        context = record.get("context")
        if not memory_id or not source or context is None:
            return None
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO companion_records"
                "(source_plugin, memory_id, identity_key, kind, content, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, memory_id, identity_key(context), record.get("kind"),
                 str(record.get("content") or "")[:8000], time.time()),
            )
            self._db.commit()
        return memory_id

    def counts(self, key: str) -> dict[str, int]:
        """统计只针对请求里的这一份身份，分类键固定为约定的六项。"""
        with self._lock:
            videos = self._db.execute(
                "SELECT COUNT(*) FROM douyin_items WHERE identity_key = ?", (key,)
            ).fetchone()[0]
            records = self._db.execute(
                "SELECT COUNT(*) FROM companion_records WHERE identity_key = ?", (key,)
            ).fetchone()[0]
        counts = {name: 0 for name in COUNT_KEYS}
        counts["other"] = int(videos)
        counts["private_conversation"] = int(records)
        counts["total"] = int(videos) + int(records)
        return counts

    def close(self) -> None:
        with self._lock:
            self._db.close()
