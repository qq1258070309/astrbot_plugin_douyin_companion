"""astrbot_plugin_douyin_companion —— 把 dsh-douyin 的只读抖音能力搬进 AstrBot。

设计要点（详见同目录 README.md 与上层 04 号文档）：
  * 遵循 AstrBot 插件规范：纯 ``Star`` 子类 + ``metadata.yaml`` + ``_conf_schema.json``。
  * 普通聊天能力通过 ``@filter.llm_tool`` 注册，交给 AstrBot 核心回复管线调用；
    陪伴小屋的 ``on_llm_request`` 不会清除核心工具集。
  * 与陪伴小屋**只通过官方稳定契约** ``companion.external-memory.v1`` 交互：
    不继承内部类、不写它的 SQLite 表、不改它的 system_prompt。
  * 不提供任何写工具（不代发抖音私信 / 评论 / 发布），写闸门默认 dry-run。

许可：核心原理移植自 JianHeShui/dsh-douyin（MIT），归属见 NOTICE.md。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools

from .douyin_core import (
    DouyinError,
    AsyncRateGate,
    GateConfig,
    WriteGate,
    clamp,
    extract_aweme_id,
    extract_sec_uid,
    identity_key,
    redact_text,
)
from .douyin_core.backend import DyCliBackend
from .douyin_core.media import MediaService
from .douyin_core.store import DouyinStore

PLUGIN_NAME = "astrbot_plugin_douyin_companion"
COMPANION_PLUGIN_ID = "astrbot_plugin_private_companion"
MEMORY_API_VERSION = "companion.external-memory.v1"
MEMORY_PROVIDER_ID = "douyin_companion.shared_videos"

_URL_RE = re.compile(r"https?://[^\s<>\u4e00-\u9fff]+", re.I)
_DOUYIN_HOST_RE = re.compile(r"(v\.douyin\.com|douyin\.com|iesdouyin\.com)", re.I)


class DouyinCompanionPlugin(Star):
    """独立 Star 插件：陪伴小屋缺席时也能单独工作。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.config: dict[str, Any] = config if config is not None else {}
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME)).resolve()
        self._data_dir.mkdir(parents=True, exist_ok=True)

        gate_cfg = GateConfig(
            hourly_request_budget=clamp(self.config.get("hourly_request_budget"), 1, 500, 60),
            call_delay_min_ms=clamp(self.config.get("call_delay_min_ms"), 1200, 10000, 1800),
            call_delay_max_ms=clamp(self.config.get("call_delay_max_ms"), 1200, 15000, 3200),
            page_delay_min_ms=clamp(self.config.get("page_delay_min_ms"), 3000, 8000, 3000),
            page_delay_max_ms=clamp(self.config.get("page_delay_max_ms"), 3000, 8000, 8000),
            max_results=clamp(self.config.get("max_results"), 1, 20, 20),
            max_search_pages=clamp(self.config.get("max_search_pages"), 1, 3, 1),
        )
        self.gate = AsyncRateGate(gate_cfg)
        cookie_raw = str(self.config.get("cookie_file") or "").strip()
        cookie_file = Path(cookie_raw) if cookie_raw else (
            self._data_dir / "home" / ".dy" / "cookies" / "default.json"
        )
        self.backend = DyCliBackend(
            gate=self.gate,
            data_dir=self._data_dir,
            python_path=str(self.config.get("python_path") or ""),
            dy_query_script=Path(__file__).resolve().parent / "tools" / "dy_query_adapter.py",
            cookie_file=cookie_file,
            log_dir=self._data_dir / "logs",
            log_level=str(self.config.get("log_level") or "info"),
        )
        self.media = MediaService(
            data_dir=self._data_dir,
            cookie_file=cookie_file,
            ffmpeg=str(self.config.get("ffmpeg_path") or "ffmpeg"),
            ffprobe=str(self.config.get("ffprobe_path") or "ffprobe"),
            cache_limit_mb=clamp(self.config.get("media_cache_mb"), 50, 5000, 800),
        )
        self.store = DouyinStore(self._data_dir / "douyin_companion.sqlite3")
        self.write_gate = WriteGate(
            writes_enabled=bool(self.config.get("writes_enabled", False)),
            allow_targets=tuple(str(self.config.get("allow_send_targets") or "").split(",")),
        )

        self.enabled = bool(self.config.get("enabled", True))
        self.enable_llm_tools = bool(self.config.get("enable_llm_tools", True))
        self.enable_memory_provider = bool(self.config.get("enable_memory_provider", True))
        self.auto_capture_links = bool(self.config.get("auto_capture_links", True))
        self.enable_frames = bool(self.config.get("enable_frames", False))
        self._companion_api: Any = None
        self._registration: Any = None
        self._last_discovery = 0.0
        self._tasks: set[asyncio.Task] = set()

    # ================================================================ 生命周期
    async def initialize(self) -> None:
        self._connect_companion()
        probe = await self.backend.check_dy_module()
        if not probe.get("ok"):
            logger.warning("[DouyinCompanion] dy-cli 未就绪，只读能力将返回 backend_unavailable：%s",
                           redact_text(probe.get("error")))

    @filter.on_astrbot_loaded()
    async def _on_host_loaded(self) -> None:
        """宿主全部加载完再发现一次，覆盖「陪伴小屋比本插件晚加载」的情况。"""
        self._connect_companion()

    async def terminate(self) -> None:
        self._unregister_companion()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        try:
            await self.media.close()
        except Exception:
            logger.warning("[DouyinCompanion] 媒体会话关闭失败", exc_info=True)
        await self.backend.close()
        self.store.close()

    # ============================================== 陪伴小屋 extension_api 集成
    def _unregister_companion(self) -> None:
        api, token = self._companion_api, self._registration
        self._companion_api, self._registration = None, None
        if api is None or token is None:
            return
        try:
            api.unregister_memory_provider(token)
        except Exception:
            logger.debug("[DouyinCompanion] 旧注册注销失败（宿主可能已重载）", exc_info=True)

    def _connect_companion(self) -> bool:
        """按官方规范精确发现：目录名匹配 + activated + API 版本探测。

        纯同步方法：``get_all_stars()`` 与 ``register_memory_provider()`` 都是同步 API，
        这样记忆回调里的重发现不会引入「协程未 await」风险。
        """
        if not self.enable_memory_provider:
            return False
        self._last_discovery = time.time()
        try:
            stars = self.context.get_all_stars()
        except Exception:
            logger.debug("[DouyinCompanion] 无法枚举已加载插件", exc_info=True)
            return False
        for star in stars:
            plugin_id = getattr(star, "root_dir_name", None) or getattr(star, "name", None)
            if plugin_id != COMPANION_PLUGIN_ID or not getattr(star, "activated", False):
                continue
            api = getattr(getattr(star, "star_cls", None), "extension_api", None)
            if getattr(api, "memory_provider_api_version", None) != MEMORY_API_VERSION:
                continue
            if api is self._companion_api and self._registration is not None:
                return True  # 同一实例，避免重复注册
            self._unregister_companion()  # 宿主重载过，旧凭据作废
            try:
                self._registration = api.register_memory_provider(
                    MEMORY_PROVIDER_ID,
                    plugin=self,
                    display_name="抖音共同经历",
                    read=self._memory_read,
                    write=self._memory_write,
                    summary=self._memory_summary,
                    is_active=lambda: bool(self.enabled and self.enable_memory_provider),
                    priority=0,
                    handles_model_memory=False,
                    timeout_seconds=5.0,
                )
                self._companion_api = api
                logger.info("[DouyinCompanion] 已接入陪伴小屋 %s", MEMORY_API_VERSION)
                return True
            except Exception:
                logger.warning("[DouyinCompanion] 注册记忆 Provider 失败，降级为独立模式", exc_info=True)
                return False
        return False

    def _maybe_rediscover(self) -> None:
        """陪伴小屋热重载后重新发现；120 s 节流，回调里不做耗时操作。"""
        if not self.enable_memory_provider:
            return
        if self._registration is None or time.time() - self._last_discovery > 120:
            self._connect_companion()

    async def _memory_read(self, request: dict[str, Any]) -> dict[str, Any]:
        """自动召回：只返回同一身份下的抖音共同经历，群聊不会被宿主调用。"""
        try:
            self._maybe_rediscover()
            key = identity_key(request.get("context"))
            items = self.store.search_videos(
                key,
                str(request.get("query") or ""),
                int(request.get("limit") or 0),
                int(request.get("max_chars") or 0),
            )
            return {"items": items}
        except Exception:
            logger.warning("[DouyinCompanion] 召回失败，返回空结果", exc_info=True)
            return {"items": []}

    async def _memory_write(self, record: dict[str, Any]) -> str | None:
        """接收陪伴小屋推送的日记/梦境/主动消息副本，幂等归档（不参与模型召回）。"""
        try:
            return self.store.upsert_record(record)
        except Exception:
            logger.warning("[DouyinCompanion] 记忆副本写入失败", exc_info=True)
            return None

    async def _memory_summary(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"counts": self.store.counts(identity_key(request.get("context")))}
        except Exception:
            return {"counts": {}}

    # ==================================================== 旁路：抖音链接自动归档
    @filter.on_llm_request(priority=-900000)
    async def _observe_links(self, event: AstrMessageEvent, req: Any) -> None:
        """只做观察，不改 req、不注入提示词、不 stop_event（不与陪伴小屋抢主链）。

        用后台任务避免拖慢 LLM 请求；产出写入本插件的 store，由陪伴小屋的
        记忆召回在后续轮次按需取用。
        """
        if not (self.enabled and self.auto_capture_links):
            return
        text = str(getattr(event, "message_str", "") or "")
        urls = [u for u in _URL_RE.findall(text) if _DOUYIN_HOST_RE.search(u)]
        if not urls:
            return
        key = identity_key({
            "umo": getattr(event, "unified_msg_origin", ""),
            "sender": event.get_sender_id(),
            "group": event.get_group_id(),
            "platform": event.get_platform_name(),
        })
        task = asyncio.create_task(self._capture(urls[0], key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(self, url: str, key: str) -> None:
        try:
            expanded = await asyncio.wait_for(self.media.expand_short_url(url), timeout=20.0)
            aweme_id = extract_aweme_id(expanded) or extract_aweme_id(url)
            if not aweme_id:
                return
            summary = ""
            title = ""
            author = ""
            try:
                detail = await self.backend.video(aweme_id)
                video = detail.get("video") or {}
                title = str(video.get("title") or "")
                author = str((video.get("author") or {}).get("nickname") or "")
            except DouyinError as error:
                summary = f"详情读取失败：{error.code}"
            if not title:
                try:
                    media = await self.media.resolve(aweme_id)
                    title, author = media["desc"], media["author"]
                except DouyinError as error:
                    if summary:
                        summary += f"；分享页解析失败：{error.code}"
                    else:
                        summary = f"分享页解析失败：{error.code}"
            self.store.put_video(
                key, aweme_id,
                title=title or None, author=author or None, summary=summary or None,
                url=f"https://www.douyin.com/video/{aweme_id}",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[DouyinCompanion] 链接归档失败（不影响主链）", exc_info=True)

    # ================================================================== 指令
    @filter.command("抖音状态", alias={"dystatus"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看抖音后端、登录态、配额与陪伴小屋接入状态。"""
        status = await self.backend.status()
        linked = self._registration is not None
        lines = [
            status["summary"],
            f"登录 Cookie：{'已配置' if status['backend']['cookie_present'] else '未配置'}",
            f"dy-cli：{'就绪 ' + str(status['backend']['dy_cli'].get('version', '')) if status['backend']['dy_cli'].get('ok') else '未就绪'}",
            f"本小时请求：{status['quota']['used']}/{status['quota']['limit']}，剩余 {status['quota']['remaining']}",
            f"陪伴小屋记忆接入：{'已连接' if linked else '未连接（独立模式仍可用）'}",
            f"写能力：{self.write_gate.describe()['default']}（随包写工具 0 个）",
        ]
        if status["problems"]:
            lines.append("提示：" + "；".join(status["problems"]))
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音写权限", alias={"douyin_write_gate"})
    async def cmd_write_gate(self, event: AstrMessageEvent):
        """查看写操作三道闸的当前状态（本插件不提供任何真实写工具）。"""
        info = self.write_gate.describe()
        allowed, reason = self.write_gate.evaluate("", explicit_confirm=False)
        yield event.plain_result(
            f"总闸 writes_enabled={info['writes_enabled']}；"
            f"白名单条目={info['target_count']}；随包写工具={info['shipped_write_tools']}；"
            f"默认调用结果={reason}（dry-run）。"
        )

    @filter.command("抖音热榜", alias={"dyhot"})
    async def cmd_hot(self, event: AstrMessageEvent, count: int = 10):
        """查看抖音热榜。"""
        if not self.enabled:
            yield event.plain_result("插件已停用。")
            return
        count = max(1, min(int(count), self.gate.config.max_results))
        try:
            result = await self.backend.hot(count)
        except DouyinError as error:
            yield event.plain_result(f"读取失败：{redact_text(error.message)}")
            return
        lines = [f"抖音热榜（{result['count']} 条）"]
        for item in result["items"]:
            hot = item["hot_value"] if item["hot_value"] is not None else "?"
            lines.append(f"{item['rank']}. {item['title'] or '(无标题)'} · 热度 {hot}")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音搜索", alias={"dysearch"})
    async def cmd_search(self, event: AstrMessageEvent, keyword: str, limit: int = 5):
        """按关键词搜索抖音视频。"""
        if not self.enabled:
            yield event.plain_result("插件已停用。")
            return
        keyword = str(keyword or "").strip()
        if not keyword:
            yield event.plain_result("关键词不能为空。")
            return
        limit = max(1, min(int(limit), self.gate.config.max_results))
        try:
            result = await self.backend.search(keyword, limit, 1)
        except DouyinError as error:
            yield event.plain_result(f"搜索失败：{redact_text(error.message)}")
            return
        lines = [f"搜索「{keyword}」：{result['count']} 条" + ("（部分成功）" if result["partial"] else "")]
        for item in result["items"]:
            author = (item.get("author") or {}).get("nickname") or "?"
            likes = (item.get("stats") or {}).get("likes")
            likes = likes if likes is not None else "?"
            lines.append(f"- {item.get('title') or '(无文案)'} | {author} | 赞 {likes} | {item.get('aweme_id')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音", alias={"douyin"})
    async def cmd_video(self, event: AstrMessageEvent, target: str = ""):
        """解析一个抖音视频：传 aweme_id 或分享链接。"""
        if not self.enabled:
            yield event.plain_result("插件已停用。")
            return
        expanded = await self.media.expand_short_url(target) if target else target
        aweme_id = extract_aweme_id(expanded) or extract_aweme_id(target)
        if not aweme_id:
            yield event.plain_result("请给出 aweme_id 或抖音分享链接，例如：抖音 https://v.douyin.com/xxxx/")
            return
        try:
            detail = await self.backend.video(aweme_id)
            video = detail["video"]
        except DouyinError as error:
            yield event.plain_result(f"详情读取失败：{redact_text(error.message)}（可试 /抖音状态 排查）")
            return
        stats = video.get("stats") or {}
        author = (video.get("author") or {}).get("nickname") or "?"
        lines = [
            f"视频 {aweme_id}：{video.get('title') or '(无文案)'}",
            f"作者 {author} | 赞 {stats.get('likes') if stats.get('likes') is not None else '?'}"
            f" | 评 {stats.get('comments') if stats.get('comments') is not None else '?'}",
            str(video.get("url") or ""),
        ]
        yield event.plain_result("\n".join(lines))
        if self.enable_frames:
            try:
                frames = await self.media.frames(aweme_id, 6)
            except DouyinError as error:
                yield event.plain_result(f"抽帧失败：{redact_text(error.message)}")
                return
            import astrbot.api.message_components as Comp
            chain = [Comp.Image.fromFileSystem(path) for path in frames["frames"][:3]]
            if chain:
                yield event.chain_result(chain)

    # ============================================================== LLM 工具
    @filter.llm_tool(name="douyin_hot")
    async def tool_hot(self, event: AstrMessageEvent, count: int = 10) -> MessageEventResult:
        """读取抖音实时热榜，返回榜单标题、排名与热度值。只读，不需要登录。

        Args:
            count(number): 要读取的榜单条数，1 到 20，默认 10
        """
        if not (self.enabled and self.enable_llm_tools):
            yield event.plain_result("抖音能力未启用。")
            return
        count = max(1, min(int(count or 10), self.gate.config.max_results))
        try:
            result = await self.backend.hot(count)
        except DouyinError as error:
            yield event.plain_result(f"[读取失败] {redact_text(error.message)}")
            return
        lines = [f"抖音热榜（{result['count']} 条）"]
        lines += [f"{i['rank']}. {i['title'] or '(无标题)'} (热度 {i['hot_value'] if i['hot_value'] is not None else '未知'})"
                  for i in result["items"]]
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="douyin_search")
    async def tool_search(self, event: AstrMessageEvent, keyword: str, limit: int = 5) -> MessageEventResult:
        """按关键词搜索抖音视频，返回标题、作者、点赞数与 aweme_id。只读取，需已配置登录 Cookie。

        Args:
            keyword(string): 搜索关键词，不能为空
            limit(number): 返回条数，1 到 20，默认 5
        """
        if not (self.enabled and self.enable_llm_tools):
            yield event.plain_result("抖音能力未启用。")
            return
        keyword = str(keyword or "").strip()
        if not keyword:
            yield event.plain_result("[invalid_args] 关键词不能为空。")
            return
        limit = max(1, min(int(limit or 5), self.gate.config.max_results))
        try:
            result = await self.backend.search(keyword, limit, 1)
        except DouyinError as error:
            yield event.plain_result(f"[{error.code}] {redact_text(error.message)}")
            return
        lines = [f"搜索「{keyword}」：{result['count']} 条" + ("（部分成功）" if result["partial"] else "")]
        for item in result["items"]:
            author = (item.get("author") or {}).get("nickname") or "?"
            likes = (item.get("stats") or {}).get("likes")
            lines.append(f"- {item.get('title') or '(无文案)'} | {author} | 赞 {likes if likes is not None else '未知'} | {item.get('aweme_id')}")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="douyin_video")
    async def tool_video(self, event: AstrMessageEvent, target: str) -> MessageEventResult:
        """读取一条抖音视频的详情：文案、作者、互动数据。可接受 aweme_id、完整链接或短分享链接。

        Args:
            target(string): aweme_id 或抖音视频/分享链接
        """
        if not (self.enabled and self.enable_llm_tools):
            yield event.plain_result("抖音能力未启用。")
            return
        expanded = await self.media.expand_short_url(str(target or ""))
        aweme_id = extract_aweme_id(expanded) or extract_aweme_id(target)
        if not aweme_id:
            yield event.plain_result("[invalid_args] 需要合法的 aweme_id 或抖音链接。")
            return
        try:
            detail = await self.backend.video(aweme_id)
        except DouyinError as error:
            yield event.plain_result(f"[{error.code}] {redact_text(error.message)}")
            return
        video = detail["video"]
        stats = video.get("stats") or {}
        author = (video.get("author") or {}).get("nickname") or "未知"
        yield event.plain_result(
            f"视频 {aweme_id}：{video.get('title') or '(无文案)'}\n"
            f"作者：{author} | 赞 {stats.get('likes') if stats.get('likes') is not None else '未知'}"
            f" | 评 {stats.get('comments') if stats.get('comments') is not None else '未知'}"
            f" | 藏 {stats.get('collects') if stats.get('collects') is not None else '未知'}\n"
            f"链接：{video.get('url')}"
        )

    @filter.llm_tool(name="douyin_frames")
    async def tool_frames(self, event: AstrMessageEvent, target: str, count: int = 6) -> MessageEventResult:
        """下载抖音视频并均匀抽取关键帧，返回本地帧路径列表，供支持视觉的模型理解视频内容。

        Args:
            target(string): aweme_id 或抖音视频/分享链接
            count(number): 抽帧数量，4 到 12，默认 6
        """
        if not (self.enabled and self.enable_llm_tools and self.enable_frames):
            yield event.plain_result("视频抽帧能力未启用（需在插件配置里打开 enable_frames 并安装 ffmpeg）。")
            return
        expanded = await self.media.expand_short_url(str(target or ""))
        aweme_id = extract_aweme_id(expanded) or extract_aweme_id(target)
        if not aweme_id:
            yield event.plain_result("[invalid_args] 需要合法的 aweme_id 或抖音链接。")
            return
        try:
            result = await self.media.frames(aweme_id, int(count or 6))
        except DouyinError as error:
            yield event.plain_result(f"[{error.code}] {redact_text(error.message)}")
            return
        yield event.plain_result(
            f"视频 {aweme_id}（{result['author'] or '未知作者'}）：{result['desc'] or '(无文案)'}\n"
            f"共 {len(result['frames'])} 帧：\n" + "\n".join(result["frames"])
        )
