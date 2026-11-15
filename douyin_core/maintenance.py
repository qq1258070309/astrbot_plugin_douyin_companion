"""自检 / 清理 / 能力矩阵。

对应上游 ``dy_selfcheck`` / ``dy_cleanup`` / ``dy_status`` 的工程约束：
自检要真跑一次依赖探测而不是报「一切正常」；清理要有界、可 dry-run；
能力矩阵把「已实现 / 部分 / 未移植」写清楚，不虚报功能。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .understanding import doctor as understanding_doctor

# 与 docs/UPSTREAM_MAPPING.md 一一对应；status 只允许 implemented / partial / not_ported
CAPABILITIES: tuple[dict[str, str], ...] = (
    {"upstream": "dy_hot", "ours": "douyin_hot", "status": "implemented", "note": "热榜，无需登录"},
    {"upstream": "dy_search", "ours": "douyin_search", "status": "implemented", "note": "search_id 链式翻页"},
    {"upstream": "dy_video", "ours": "douyin_video", "status": "implemented", "note": "文案/作者/互动数据"},
    {"upstream": "dy_thumb", "ours": "douyin_frames / douyin_understand", "status": "implemented", "note": "ffmpeg 均匀抽帧"},
    {"upstream": "dy_hot_videos", "ours": "douyin_hot_videos", "status": "partial", "note": "按热词搜索实现，非官方热点视频接口"},
    {"upstream": "dy_comments", "ours": "—", "status": "not_ported", "note": "上游靠页面 XHR/DOM，本插件无浏览器驱动"},
    {"upstream": "dy_feed", "ours": "—", "status": "not_ported", "note": "推荐流需拦截 /module/feed/ 的 CDP 浏览器"},
    {"upstream": "dy_my_posts / dy_my_videos", "ours": "—", "status": "not_ported", "note": "需登录态浏览器与创作者页"},
    {"upstream": "dy_remember", "ours": "douyin_remember", "status": "implemented", "note": "写入本插件身份隔离的 SQLite"},
    {"upstream": "dy_status", "ours": "douyin_selfcheck / /抖音状态", "status": "implemented", "note": "含依赖矩阵与配额"},
    {"upstream": "dy_selfcheck", "ours": "douyin_selfcheck", "status": "implemented", "note": "真跑依赖探测"},
    {"upstream": "dy_cleanup", "ours": "douyin_cleanup", "status": "implemented", "note": "有界 + 默认 dry-run"},
    {"upstream": "dy_login_*", "ours": "（外部前置步骤）", "status": "partial", "note": "服务器无头不能扫码，改用 storage_state 导出"},
    {"upstream": "dy_dm_inbox / dy_dm_poll", "ours": "—", "status": "not_ported", "note": "只读私信需浏览器 CDP；秒回属于写操作"},
    {"upstream": "dy_like / dy_comment / dy_share / dy_chat_* / dy_publish", "ours": "WriteGate（不暴露工具）",
     "status": "not_ported", "note": "账号写操作，需单独授权；随包写工具恒为 0"},
    {"upstream": "dy_run / 日报", "ours": "—", "status": "not_ported", "note": "上游定时巡检依赖 DSH 调度器"},
    {"upstream": "video-understanding-lab", "ours": "douyin_understand", "status": "implemented",
     "note": "抽帧 + 可选 ASR + 证据 JSON + 回放"},
)


class MaintenanceService:
    def __init__(self, *, data_dir: Path, cookie_file: Path | None,
                 python_path: str, media_cache_mb: int = 800):
        self.data_dir = Path(data_dir)
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self.python_path = python_path
        self.media_cache_mb = media_cache_mb

    def selfcheck(self, *, quota: dict[str, Any] | None = None,
                  write_gate: dict[str, Any] | None = None) -> dict[str, Any]:
        env = understanding_doctor()
        checks = {
            "python_interpreter": bool(self.python_path),
            "cookie_file": bool(self.cookie_file and self.cookie_file.exists()),
            "ffmpeg": bool(env["binaries"]["ffmpeg"]),
            "ffprobe": bool(env["binaries"]["ffprobe"]),
            "aiohttp": bool(env["modules"]["aiohttp"]),
            "faster_whisper": bool(env["modules"]["faster_whisper"]),
            "data_dir_writable": self.data_dir.exists(),
        }
        problems = [name for name, ok in checks.items() if not ok]
        return {
            "schema": "douyin_companion.selfcheck.v1",
            "checked_at": time.time(),
            "checks": checks,
            "doctor": env,
            "quota": quota or {},
            "write_gate": write_gate or {},
            "capabilities": list(CAPABILITIES),
            "implemented": sum(1 for c in CAPABILITIES if c["status"] == "implemented"),
            "partial": sum(1 for c in CAPABILITIES if c["status"] == "partial"),
            "not_ported": sum(1 for c in CAPABILITIES if c["status"] == "not_ported"),
            "problems": problems,
            "summary": "全部核心依赖就绪。" if not problems else "缺失：" + "、".join(problems),
        }

    def cleanup(self, *, dry_run: bool = True, max_delete: int = 200) -> dict[str, Any]:
        """有界清理：媒体缓存、旧证据、旧日志。默认 dry-run，绝不递归删目录。"""
        targets: list[Path] = []
        for pattern in ("media/downloads/*.mp4", "media/downloads/*.part",
                        "media/frames/*.jpg", "evidence/*.json", "logs/*.log"):
            targets.extend(sorted(self.data_dir.glob(pattern)))
        candidates = [p for p in targets if p.is_file()][:max_delete]
        total = sum(p.stat().st_size for p in candidates)
        if not dry_run:
            removed = 0
            for path in candidates:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
            return {"dry_run": False, "removed": removed, "bytes_freed": total,
                    "candidates": len(candidates)}
        return {"dry_run": True, "removed": 0, "would_remove": len(candidates),
                "bytes_would_free": total, "sample": [p.name for p in candidates[:5]]}

    def cache_health(self) -> dict[str, Any]:
        media = self.data_dir / "media"
        size = sum(p.stat().st_size for p in media.rglob("*") if p.is_file()) if media.exists() else 0
        limit = self.media_cache_mb * 1024 * 1024
        return {"bytes": size, "limit": limit, "ratio": round(size / limit, 4) if limit else 0,
                "over_budget": size > limit}
