"""移动端分享页解析 / 无水印直链 / 下载 / 抽帧。

移植自 dsh-douyin ``python/douyin_http.py::_resolve_media / _download_sync /
_extract_frames``，把 ``urllib + yt-dlp`` 换成 AstrBot 官方允许的异步 ``aiohttp``。

为什么不用 yt-dlp 自带 Douyin 提取器（上游原话）：
  它请求 ``/aweme/v1/web/aweme/detail/``，需要 ``a_bogus`` 签名，
  即使带登录 cookie，机房 IP 通常也只拿到空响应。
替代路径：
  分享页内嵌 ``_ROUTER_DATA`` → ``play_addr.url_list`` → ``/playwm/`` 换 ``/play/``
  → 直链流式下载 → ffmpeg 均匀抽帧 → 帧路径交给视觉模型。
媒体直链有时效（几十分钟），只缓存成片，绝不缓存 URL。
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
from pathlib import Path
from typing import Any

from .errors import DouyinError
from .redact import redact_text

SHARE_URL = "https://www.iesdouyin.com/share/video/{aid}/"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
REFERER = "https://www.iesdouyin.com/"
_ROUTER_DATA = re.compile(r"_ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", re.S)


def clean_aweme_id(raw: Any) -> str:
    """容忍粘了整段分享链接。"""
    text = str(raw or "").strip()
    match = re.search(r"/video/(\d{8,})", text)
    if match:
        return match.group(1)
    return re.sub(r"\D", "", text)


class MediaService:
    def __init__(
        self,
        *,
        data_dir: Path,
        cookie_file: Path | None,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        cache_limit_mb: int = 800,
        timeout: float = 30.0,
    ):
        self.data_dir = Path(data_dir)
        self.download_dir = self.data_dir / "media" / "downloads"
        self.frame_dir = self.data_dir / "media" / "frames"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.cache_limit_mb = cache_limit_mb
        self.timeout = timeout
        self._session: Any = None
        self._cookie_cache: tuple[float, str] = (0.0, "")

    async def _get_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp  # 延迟导入：未安装时插件仍可加载（只是媒体能力不可用）
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": MOBILE_UA, "Referer": REFERER},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------- cookie ---
    def _cookie_header(self) -> str:
        """Playwright storage_state → Cookie 头，只取 douyin.com 域，绝不记录值。"""
        if not self.cookie_file or not self.cookie_file.exists():
            return ""
        try:
            mtime = self.cookie_file.stat().st_mtime
            if self._cookie_cache[0] == mtime and self._cookie_cache[1]:
                return self._cookie_cache[1]
            state = json.loads(self.cookie_file.read_text(encoding="utf-8"))
            parts = []
            for cookie in state.get("cookies") or []:
                if "douyin.com" not in str(cookie.get("domain") or ""):
                    continue
                name, value = cookie.get("name"), cookie.get("value")
                if name and value is not None:
                    parts.append(f"{name}={value}")
            header = "; ".join(parts)
            self._cookie_cache = (mtime, header)
            return header
        except (OSError, ValueError, TypeError):
            return ""

    # ------------------------------------------------------------- resolve ---
    async def expand_short_url(self, url: str) -> str:
        """把 ``v.douyin.com`` 短链跟随重定向展开成含 aweme_id 的完整链接。

        非短链原样返回；网络异常也原样返回，让调用方退回到「从原文提数字 ID」。
        """
        text = str(url or "").strip()
        if not text.startswith("http") or "v.douyin.com" not in text.lower():
            return text
        try:
            session = await self._get_session()
            async with session.get(text, allow_redirects=True, headers={"User-Agent": MOBILE_UA}) as resp:
                return str(resp.url)
        except Exception:
            return text

    async def resolve(self, aweme_id: str) -> dict[str, Any]:
        """解析分享页，返回无水印直链与元信息。不缓存 URL。"""
        aweme_id = clean_aweme_id(aweme_id)
        if not aweme_id.isdigit():
            raise DouyinError("invalid_args", "aweme_id 不合法。")
        session = await self._get_session()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        try:
            async with session.get(SHARE_URL.format(aid=aweme_id), headers=headers) as resp:
                html = await resp.text(errors="ignore")
        except Exception as error:
            raise DouyinError("backend_unavailable", redact_text(f"分享页请求失败：{error}")) from error

        match = _ROUTER_DATA.search(html)
        if not match:
            raise DouyinError("challenge_required", "分享页里没找到 _ROUTER_DATA（可能被风控或视频已删）。")
        try:
            router = json.loads(match.group(1))
        except ValueError as error:
            raise DouyinError("parse_failed", "_ROUTER_DATA 解析失败。") from error

        page = ((router.get("loaderData") or {}).get("video_(id)/page") or {})
        items = (page.get("videoInfoRes") or {}).get("item_list") or []
        if not items:
            raise DouyinError("not_found", "分享页没返回视频数据（视频可能已删/私密/被风控）。")
        item = items[0] if isinstance(items[0], dict) else {}
        video = item.get("video") if isinstance(item.get("video"), dict) else {}

        urls: list[str] = list((video.get("play_addr") or {}).get("url_list") or [])
        for bit_rate in video.get("bit_rate") or []:
            if isinstance(bit_rate, dict):
                urls += list(((bit_rate.get("play_addr") or {}).get("url_list")) or [])
        if not urls:
            raise DouyinError("parse_failed", "没拿到播放地址。")

        play_url = str(urls[0]).replace("/playwm/", "/play/").replace("playwm", "play")
        try:
            duration = round(float(video.get("duration") or item.get("duration") or 0) / 1000.0, 1)
        except (TypeError, ValueError):
            duration = 0.0
        return {
            "aweme_id": aweme_id,
            "play_url": play_url,  # 内部字段，禁止回显到消息/日志
            "desc": str(item.get("desc") or "")[:200],
            "author": str((item.get("author") or {}).get("nickname") or ""),
            "duration": duration,
        }

    # ------------------------------------------------------------ download ---
    def _prune_cache(self) -> None:
        budget = self.cache_limit_mb * 1024 * 1024
        for pattern in (str(self.download_dir / "*.mp4"), str(self.frame_dir / "*.jpg")):
            files = []
            for path in glob.glob(pattern):
                try:
                    files.append((os.path.getmtime(path), path, os.path.getsize(path)))
                except OSError:
                    continue
            total = sum(size for _, _, size in files)
            if total <= budget:
                continue
            for _, path, size in sorted(files):
                try:
                    os.remove(path)
                    total -= size
                except OSError:
                    continue
                if total <= budget:
                    break

    def _meta_path(self, aweme_id: str) -> Path:
        return self.download_dir / f"{aweme_id}.meta.json"

    async def download(self, aweme_id: str) -> dict[str, Any]:
        """下载成片（带缓存），返回本地路径与元信息。"""
        aweme_id = clean_aweme_id(aweme_id)
        cached = self.download_dir / f"{aweme_id}.mp4"
        self._prune_cache()
        if cached.exists() and cached.stat().st_size > 100 * 1024:
            try:
                meta = json.loads(self._meta_path(aweme_id).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            return {"ok": True, "path": str(cached), "cached": True, **meta}

        media = await self.resolve(aweme_id)
        session = await self._get_session()
        part = self.download_dir / f"{aweme_id}.mp4.part"
        try:
            async with session.get(
                media["play_url"], headers={"User-Agent": MOBILE_UA, "Referer": REFERER}
            ) as resp:
                if resp.status >= 400:
                    raise DouyinError("backend_unavailable", f"媒体直链返回 HTTP {resp.status}。")
                with part.open("wb") as handle:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        handle.write(chunk)
        except DouyinError:
            raise
        except Exception as error:
            raise DouyinError("backend_unavailable", redact_text(f"下载失败：{error}")) from error
        if not part.exists() or part.stat().st_size <= 100 * 1024:
            part.unlink(missing_ok=True)
            raise DouyinError("backend_unavailable", "下载结果过小，已丢弃。")
        os.replace(part, cached)
        meta = {
            "aweme_id": aweme_id,
            "desc": media["desc"],
            "author": media["author"],
            "duration": media["duration"],
            "size": cached.stat().st_size,
        }
        self._meta_path(aweme_id).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "path": str(cached), "cached": False, **meta}

    # --------------------------------------------------------------- frames ---
    async def _run(self, args: list[str], timeout: float = 60.0) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode("utf-8", "ignore").strip()
        except (OSError, asyncio.TimeoutError):
            return ""

    async def frames(self, aweme_id: str, n: int = 8) -> dict[str, Any]:
        """均匀抽 4–12 帧，返回 jpg 路径列表，交给支持视觉的模型去理解。"""
        downloaded = await self.download(aweme_id)
        aweme_id = downloaded["aweme_id"]
        for old in glob.glob(str(self.frame_dir / f"{aweme_id}_*.jpg")):
            try:
                os.remove(old)
            except OSError:
                pass
        try:
            duration = float(await self._run([
                self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", downloaded["path"],
            ], 30.0) or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            duration = 60.0
        count = max(4, min(int(n or 8), 12))
        paths: list[str] = []
        for index in range(count):
            timestamp = duration * (index + 0.5) / count
            out = self.frame_dir / f"{aweme_id}_{index}.jpg"
            await self._run([
                self.ffmpeg, "-y", "-ss", f"{timestamp:.2f}", "-i", downloaded["path"],
                "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(out),
            ])
            if out.exists() and out.stat().st_size > 1000:
                paths.append(str(out))
        return {
            "ok": True,
            "aweme_id": aweme_id,
            "path": downloaded["path"],
            "frames": paths,
            "desc": downloaded.get("desc", ""),
            "author": downloaded.get("author", ""),
            "duration": downloaded.get("duration", 0),
        }
