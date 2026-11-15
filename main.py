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
    ACTION_LABELS,
    WRITE_ACTIONS,
    ActionPolicy,
    DouyinError,
    AsyncRateGate,
    BrowserService,
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
from .douyin_core.understanding import UnderstandingService
from .douyin_core.maintenance import CAPABILITIES, MaintenanceService

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
        self.understanding = UnderstandingService(
            media=self.media, data_dir=self._data_dir,
            enable_asr=bool(self.config.get("enable_asr", False)),
            asr_model=str(self.config.get("asr_model") or "base"),
        )
        self.maintenance = MaintenanceService(
            data_dir=self._data_dir, cookie_file=cookie_file,
            python_path=self.backend.python_path,
            media_cache_mb=clamp(self.config.get("media_cache_mb"), 50, 5000, 800),
        )
        self.write_gate = WriteGate(
            writes_enabled=bool(self.config.get("writes_enabled", False)),
            allow_targets=tuple(str(self.config.get("allow_send_targets") or "").split(",")),
        )

        # ------------------------------------------------ 浏览器（账号侧能力） --
        self.browser = BrowserService(
            data_dir=self._data_dir,
            headless=bool(self.config.get("browser_headless", False)),
            channel=str(self.config.get("browser_channel") or "auto"),
            cdp_url=str(self.config.get("browser_cdp_url") or ""),
            chrome_path=str(self.config.get("browser_chrome_path") or ""),
            profile_port=clamp(self.config.get("browser_profile_port"), 1024, 65535, 9333),
            default_timeout=clamp(self.config.get("browser_timeout"), 30, 600, 90),
        )
        targets_raw = str(
            self.config.get("dm_allow_targets")
            or self.config.get("allow_send_targets")
            or ""
        )
        quotas = {
            "like": clamp(self.config.get("like_per_day"), 0, 500, 0),
            "comment": clamp(self.config.get("comment_per_day"), 0, 200, 0),
            "share": clamp(self.config.get("share_per_day"), 0, 200, 0),
            "dm": clamp(self.config.get("dm_per_day"), 0, 200, 0),
            "publish": clamp(self.config.get("publish_per_day"), 0, 50, 0),
        }
        self.policy = ActionPolicy(
            writes_enabled=bool(self.config.get("writes_enabled", False)),
            dry_run=bool(self.config.get("write_dry_run", True)),
            actions=tuple(
                a.strip() for a in str(self.config.get("write_actions") or "").split(",")
                if a.strip() in WRITE_ACTIONS
            ),
            quotas=quotas,
            targets=tuple(targets_raw.split(",")),
            publish_dir=str(self.config.get("publish_allow_dir") or ""),
            require_confirm=True,
            store=self.store,
        )
        self.enable_browser = bool(self.config.get("enable_browser", False))
        self.dm_private_only = bool(self.config.get("dm_private_only", True))
        self.feed_max_scrolls = clamp(self.config.get("feed_max_scrolls"), 1, 30, 6)
        self.comment_mention = str(self.config.get("comment_mention") or "").strip().lstrip("@")

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
        if self.enable_browser:
            info = self.browser.probe()
            if info.get("ok"):
                logger.info("[DouyinCompanion] 浏览器内核就绪：channel=%s headless=%s profile=%s",
                            info.get("browser_channel"), info.get("headless"), info.get("profile_dir"))
            else:
                logger.warning("[DouyinCompanion] 浏览器内核不可用：%s", info.get("error"))

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
        try:
            await self.browser.close()
        except Exception:
            logger.warning("[DouyinCompanion] 浏览器关闭失败", exc_info=True)
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
            items = self.store.search_all(
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
    async def cmd_status(self, event: AstrMessageEvent, what: str = ""):
        """查看抖音后端、登录态、配额与陪伴小屋接入状态。加「登录」会实际探测登录态。"""
        status = await self.backend.status()
        linked = self._registration is not None
        lines = [
            status["summary"],
            f"登录 Cookie：{'已配置' if status['backend']['cookie_present'] else '未配置'}",
            f"dy-cli：{'就绪 ' + str(status['backend']['dy_cli'].get('version', '')) if status['backend']['dy_cli'].get('ok') else '未就绪'}",
            f"本小时请求：{status['quota']['used']}/{status['quota']['limit']}，剩余 {status['quota']['remaining']}",
            f"陪伴小屋记忆接入：{'已连接' if linked else '未连接（独立模式仍可用）'}",
        ]
        if self.enable_browser:
            info = self.browser.probe()
            lines.append(
                "浏览器：" + ("就绪" if info.get("ok") else f"不可用（{info.get('error')}）")
                + f"；channel={info.get('browser_channel') or '?'}；"
                f"{'无头' if info.get('headless') else '带界面'}"
            )
            if "登录" in str(what or ""):
                state = await self.browser.login_state()
                if state.get("ok"):
                    lines.append(
                        "登录态：" + ("已登录" if state.get("loggedIn") else "未登录")
                        + f"；cookie={bool(state.get('cookie'))}；头像={bool(state.get('avatar'))}"
                        + (f"；昵称={state.get('nickname')}" if state.get("nickname") else "")
                    )
                else:
                    lines.append(f"登录态探测失败：{state.get('error')}")
        else:
            lines.append("浏览器：未启用（enable_browser=false，点赞/评论/私信/投稿不可用）")
        gate = self.policy.describe()
        lines.append(
            f"写能力：总闸={'开' if gate['writes_enabled'] else '关'}，"
            f"演练={'开' if gate['dry_run'] else '关'}；"
            f"开启动作={[a for a, on in gate['actions'].items() if on] or '无'}"
        )
        if status["problems"]:
            lines.append("提示：" + "；".join(status["problems"]))
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音写权限", alias={"douyin_write_gate"})
    async def cmd_write_gate(self, event: AstrMessageEvent):
        """查看写操作闸门矩阵（总闸 / 动作 / 每日额度 / 收件人白名单）。"""
        yield event.plain_result("写操作闸门：\n" + "\n".join(self.policy.summary_lines()))

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

    def _event_key(self, event: AstrMessageEvent) -> str:
        return identity_key({
            "umo": getattr(event, "unified_msg_origin", ""),
            "sender": event.get_sender_id(),
            "group": event.get_group_id(),
            "platform": event.get_platform_name(),
        })

    @filter.command("抖音能力", alias={"dycaps"})
    async def cmd_capabilities(self, event: AstrMessageEvent):
        """列出与上游 dsh-douyin-life 的能力映射（已实现 / 部分 / 未移植）。"""
        lines = ["抖音陪伴 · 能力矩阵（对照 kdcadmin/dsh_douyin_tools）"]
        for row in CAPABILITIES:
            mark = {"implemented": "✅", "partial": "🟡", "not_ported": "⬜"}[row["status"]]
            lines.append(f"{mark} {row['upstream']} → {row['ours']}：{row['note']}")
        lines.append("⬜ 账号写操作一律不提供；闸门默认全关。")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音自检", alias={"dydoctor"})
    async def cmd_selfcheck(self, event: AstrMessageEvent):
        """真跑一次依赖自检：Python / cookie / ffmpeg / ffprobe / aiohttp / ASR / 数据目录。"""
        report = self.maintenance.selfcheck(
            quota=self.gate.quota(), write_gate=self.write_gate.describe()
        )
        lines = [report["summary"],
                 f"核心依赖：{sum(report['checks'].values())}/{len(report['checks'])} 就绪",
                 f"能力：已实现 {report['implemented']} / 部分 {report['partial']} / 未移植 {report['not_ported']}",
                 f"缓存：{self.maintenance.cache_health()['ratio'] * 100:.1f}% 占用",
                 f"上游：kdcadmin/dsh_douyin_tools（哨兵见 .github/workflows/upstream-watch.yml）"]
        for name, ok in report["checks"].items():
            lines.append(f"  {'✅' if ok else '⚠️'} {name}")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音清理", alias={"dyclean"})
    async def cmd_cleanup(self, event: AstrMessageEvent, confirm: str = ""):
        """清理媒体/证据/日志缓存；默认 dry-run，传 confirm 才真正删除。"""
        really = str(confirm or "").strip().lower() in ("confirm", "yes", "确认")
        result = self.maintenance.cleanup(dry_run=not really)
        if result["dry_run"]:
            yield event.plain_result(
                f"DRY_RUN：将删除 {result['would_remove']} 个文件、"
                f"约 {result['bytes_would_free'] // 1024} KB。确认请发 /抖音清理 confirm"
            )
        else:
            yield event.plain_result(
                f"已删除 {result['removed']} 个文件、约 {result['bytes_freed'] // 1024} KB。"
            )

    @filter.command("抖音理解", alias={"dyunderstand"})
    async def cmd_understand(self, event: AstrMessageEvent, target: str = "", count: int = 6):
        """下载并抽帧 + 可选 ASR，生成视频理解证据 JSON。"""
        if not self.enabled:
            yield event.plain_result("插件已停用。")
            return
        expanded = await self.media.expand_short_url(target) if target else target
        aweme_id = extract_aweme_id(expanded) or extract_aweme_id(target)
        if not aweme_id:
            yield event.plain_result("请给出 aweme_id 或抖音链接。")
            return
        evidence = await self.understanding.understand(aweme_id, max(4, min(int(count or 6), 12)))
        lines = [
            f"视频 {aweme_id}：{evidence['meta'].get('desc') or '(无文案)'}",
            f"抽帧 {evidence['frame_count']} 张；ASR {evidence['transcript']['status']}",
            f"证据：{evidence['evidence_path']}",
        ]
        if evidence["transcript"].get("text"):
            lines.append("转写：" + evidence["transcript"]["text"][:200])
        if evidence["notes"]:
            lines.append("提示：" + "；".join(evidence["notes"][:3]))
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音记忆", alias={"dyremember"})
    async def cmd_remember(self, event: AstrMessageEvent, content: str = "", tag: str = "manual"):
        """显式记一条与抖音相关的共同经历，供陪伴小屋召回。"""
        text = str(content or "").strip()
        if not text:
            recent = self.store.recent_memories(self._event_key(event), 5)
            yield event.plain_result(
                "最近记忆：\n" + "\n".join(f"- {m['content']}" for m in recent)
                if recent else "还没有记忆。用法：/抖音记忆 <内容>"
            )
            return
        memory_id = self.store.remember(self._event_key(event), text, tag=str(tag))
        yield event.plain_result(f"已记住（{memory_id}）：{text[:80]}")

    # ==================================================== 账号侧：浏览器能力
    def _command_args(self, event: AstrMessageEvent, *names: str) -> str:
        """取指令后面的原始文本。

        AstrBot 的指令参数按空格切分，评论/私信正文含空格会被截断，所以这里自己解析。
        兼容两种情形：``message_str`` 仍带指令前缀，或框架已把前缀剥掉。
        """
        text = str(getattr(event, "message_str", "") or "").strip()
        for name in sorted(names, key=len, reverse=True):
            for prefix in (f"/{name}", name):
                if text.startswith(prefix):
                    return text[len(prefix):].strip()
        return text

    @staticmethod
    def _split_target_text(rest: str) -> tuple[str, str]:
        """``<目标> <正文>``；目标可用双引号包住以容纳空格。"""
        rest = str(rest or "").strip()
        if rest.startswith('"'):
            end = rest.find('"', 1)
            if end > 0:
                return rest[1:end].strip(), rest[end + 1:].strip()
        parts = rest.split(None, 1)
        return (parts[0], parts[1].strip() if len(parts) > 1 else "")

    async def _browser_ready(self) -> str:
        if not self.enabled:
            return "插件已停用。"
        if not self.enable_browser:
            return ("浏览器能力未启用：请在插件配置里打开 enable_browser，"
                    "并在 AstrBot 的 Python 环境安装 playwright（pip install playwright）。")
        info = self.browser.probe()
        if not info.get("ok"):
            return f"浏览器不可用：{info.get('error')}。{info.get('detail') or ''}".strip()
        return ""

    @staticmethod
    def _need_login_hint(result: dict[str, Any]) -> str:
        if result.get("needLogin") or "登录" in str(result.get("error") or ""):
            return "\n登录态不足：先 /抖音登录 在浏览器窗口里扫码，再用 /抖音状态 复查。"
        return ""

    def _action_gate(self, action: str, target: str = "", *, confirm: bool = True):
        """返回 (decision, 回复文本)。指令由人触发，视为已确认。"""
        decision = self.policy.check(action, target, confirm=confirm)
        if decision.mode == "blocked":
            return decision, f"⛔ {decision.message()}\n用 /抖音写权限 查看闸门状态。"
        if decision.mode == "dry_run":
            return decision, (f"🧪 {decision.message()}\n"
                              f"动作：{ACTION_LABELS[action]}；目标：{target or '（见参数）'}\n"
                              "未对账号做任何操作。要真跑：关掉 write_dry_run 并打开 writes_enabled。")
        return decision, ""

    @filter.command("抖音推荐", alias={"dyfeed"})
    async def cmd_feed(self, event: AstrMessageEvent, count: int = 10):
        """刷抖音推荐流（不搜索），返回露出的视频。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        num = clamp(count, 1, 30, 10)
        result = await self.browser.feed(num, self.feed_max_scrolls)
        if not result.get("ok"):
            yield event.plain_result(f"推荐流读取失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        items = result.get("items") or []
        lines = [f"推荐流 {len(items)} 条（页面自身 XHR，不是搜索）"]
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. {item.get('title') or '(无文案)'} · @{item.get('author') or '?'} "
                f"· 赞 {item.get('digg') or 0}\n   {item.get('url')}"
            )
        for note in result.get("notes") or []:
            lines.append(f"· {note}")
        if not items:
            lines.append("没有抓到视频：可能未登录、被风控，或首页结构变了。")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音评论", alias={"dycomments"})
    async def cmd_comments(self, event: AstrMessageEvent, target: str = "", count: int = 30):
        """读取一条视频的评论（只读）。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        if not str(target or "").strip():
            yield event.plain_result("用法：/抖音评论 <视频链接或ID> [条数]")
            return
        num = clamp(count, 1, 100, 30)
        result = await self.browser.comments(target, num)
        if not result.get("ok"):
            yield event.plain_result(f"评论读取失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        items = result.get("items") or result.get("comments") or []
        lines = [f"评论 {len(items)} 条（{result.get('total') or '?'} 总数）"]
        for i, item in enumerate(items[:num], 1):
            text = str(item.get("text") or "").replace("\n", " ")[:120]
            lines.append(f"{i}. @{item.get('nickname') or item.get('author') or '?'}：{text}"
                         f"（赞 {item.get('digg_count') or item.get('likes') or 0}）")
        if not items:
            lines.append("没有读到评论：可能要登录，或这条作品关闭了评论。")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音私信", alias={"dyinbox"})
    async def cmd_inbox(self, event: AstrMessageEvent, nickname: str = "", count: int = 12):
        """读取私信收件箱（只读；默认只在私聊里可用）。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        if self.dm_private_only and event.get_group_id():
            yield event.plain_result("私信内容默认只在私聊里展示（dm_private_only=true），避免在群里泄露。")
            return
        num = clamp(count, 1, 12, 12)
        result = await self.browser.inbox(str(nickname or "").strip(), num)
        if not result.get("ok"):
            yield event.plain_result(f"私信读取失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        items = result.get("items") or []
        key = self._event_key(event)
        stored = 0
        for item in items:
            if self.store.put_dm(key, str(item.get("name") or ""), item):
                stored += 1
        lines = [f"私信 {len(items)} 条（新入库 {stored} 条）"]
        for item in items:
            when = str(item.get("at") or "")
            lines.append(
                f"· {item.get('name') or item.get('conversationId') or '?'}"
                f"（{item.get('kind') or 'text'}）：{str(item.get('text') or '')[:100]}"
                + (f" [{when}]" if when else "")
            )
        if not items:
            lines.append("没有「有明确消息 ID 且方向为入站」的消息；预览文字不算消息。")
        yield event.plain_result("\n".join(lines))

    @filter.command("抖音点赞", alias={"dylike"})
    async def cmd_like(self, event: AstrMessageEvent, target: str = "", undo: str = ""):
        """给视频点赞 / 取消点赞（受写策略约束）。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        target = str(target or "").strip()
        if not target:
            yield event.plain_result("用法：/抖音点赞 <视频链接或ID> [取消]")
            return
        is_undo = str(undo or "").strip() in ("取消", "undo", "取消点赞")
        decision, refusal = self._action_gate("like", target)
        if refusal:
            yield event.plain_result(refusal)
            return
        result = await self.browser.like(target, is_undo)
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"点赞失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        state = "已取消" if is_undo else "已点赞"
        if result.get("changed") is False:
            state = "本来就是目标状态，没有重复点"
        yield event.plain_result(
            f"{state}：{result.get('aweme_id') or target}\n"
            f"页面状态={result.get('state') or '?'} 已核对={bool(result.get('verified'))}"
            + (f"\n{result.get('note')}" if result.get("note") else "")
        )

    @filter.command("抖音发评论", alias={"dycomment"})
    async def cmd_comment(self, event: AstrMessageEvent, target: str = "", text: str = ""):
        """在视频下发评论（受写策略约束）。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        rest = self._command_args(event, "抖音发评论", "dycomment")
        target, text = self._split_target_text(rest)
        if not target or not text:
            yield event.plain_result("用法：/抖音发评论 <视频链接或ID> <评论正文>")
            return
        decision, refusal = self._action_gate("comment", target)
        if refusal:
            yield event.plain_result(refusal)
            return
        result = await self.browser.comment(target, text, mention=self.comment_mention)
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            status = result.get("status") or ""
            yield event.plain_result(
                f"评论未确认成功：{result.get('error')}"
                + (f"（status={status}，已提交的操作不会自动重发）" if status == "unknown" else "")
                + self._need_login_hint(result)
            )
            return
        yield event.plain_result(
            f"评论已发：{result.get('cid') or '(无 CID)'}\n"
            f"取证方式={result.get('evidence') or '?'}；正文={result.get('text') or text}"
        )

    @filter.command("抖音转发", alias={"dyshare"})
    async def cmd_share(self, event: AstrMessageEvent, target: str = "", nickname: str = ""):
        """把视频转发给指定好友（受收件人白名单约束）。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        rest = self._command_args(event, "抖音转发", "dyshare")
        target, nickname = self._split_target_text(rest)
        if not target or not nickname:
            yield event.plain_result("用法：/抖音转发 <视频链接或ID> <好友完整昵称>")
            return
        decision, refusal = self._action_gate("share", nickname)
        if refusal:
            yield event.plain_result(refusal)
            return
        result = await self.browser.share(target, nickname)
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"转发失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        yield event.plain_result(
            f"已转发给 {nickname}：{result.get('aweme_id') or target}\n"
            f"取证={result.get('evidence') or '?'}；已核对={bool(result.get('verified'))}"
            + (f"\n{result.get('note')}" if result.get("note") else "")
        )

    @filter.command("抖音发私信", alias={"dydm"})
    async def cmd_dm(self, event: AstrMessageEvent, nickname: str = "", text: str = ""):
        """给指定好友发私信（受白名单 + 每日额度约束）。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        if self.dm_private_only and event.get_group_id():
            yield event.plain_result("发私信默认只在私聊里允许（dm_private_only=true）。")
            return
        rest = self._command_args(event, "抖音发私信", "dydm")
        nickname, text = self._split_target_text(rest)
        if not nickname or not text:
            yield event.plain_result("用法：/抖音发私信 <好友完整昵称> <正文>")
            return
        decision, refusal = self._action_gate("dm", nickname)
        if refusal:
            yield event.plain_result(refusal)
            return
        result = await self.browser.dm_send(nickname, text)
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"私信未确认成功：{result.get('error')}{self._need_login_hint(result)}")
            return
        yield event.plain_result(
            f"私信已发：{nickname}，消息 ID={result.get('messageId') or '?'}，"
            f"取证={result.get('evidence') or '?'}"
        )

    @filter.command("抖音投稿", alias={"dypublish"})
    async def cmd_publish(self, event: AstrMessageEvent, path: str = "", title: str = ""):
        """投稿视频（默认只存草稿；受允许目录约束）。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        rest = self._command_args(event, "抖音投稿", "dypublish")
        if rest.startswith('"'):
            path, tail = self._split_target_text(rest)
        else:
            parts = rest.split(None, 1)
            path, tail = (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
        really = "--publish" in tail
        tail = tail.replace("--publish", "").strip()
        title = tail or title
        if not path:
            yield event.plain_result(
                "用法：/抖音投稿 <视频绝对路径> [标题] [--publish]\n"
                "默认只存草稿（暂存离开）；--publish 才会真正发布，且必须已配置 publish_allow_dir。"
            )
            return
        ok, reason = self.policy.check_publish_path(path)
        if not ok:
            yield event.plain_result(f"⛔ 投稿被拒：{reason}")
            return
        decision, refusal = self._action_gate("publish", path)
        if refusal:
            yield event.plain_result(refusal)
            return
        result = await self.browser.publish(
            path, title or f"日常 {time.strftime('%Y-%m-%d')}",
            tags=str(self.config.get("publish_tags") or ""),
            draft=not really, ai_declare=True,
        )
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"投稿失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        ident = result.get("platformId") or result.get("draftId") or result.get("aweme_id") or "?"
        yield event.plain_result(
            f"{'已发布' if really else '已存草稿'}：{ident}\n"
            f"已核对={bool(result.get('verified'))}；取证={result.get('evidence') or '?'}"
            + (f"\n{result.get('url')}" if result.get("url") else "")
            + ("\n" + "；".join(result.get("notes") or []) if result.get("notes") else "")
        )

    @filter.command("抖音登录", alias={"dylogin"})
    async def cmd_login(self, event: AstrMessageEvent):
        """打开浏览器登录页，由本人扫码/短信登录。"""
        blocked = await self._browser_ready()
        if blocked:
            yield event.plain_result(blocked)
            return
        result = await self.browser.open_login()
        if not result.get("ok"):
            yield event.plain_result(f"打开登录页失败：{result.get('error')}")
            return
        yield event.plain_result(
            "已打开抖音登录页。请在弹出的浏览器窗口里完成扫码或短信登录；"
            "登录态保存在插件数据目录的 browser_home 里，之后 /抖音状态 可复查。\n"
            "提示：评论 / 私信需要**带界面**的浏览器（browser_headless=false），无头模式会拿到精简评论区。"
        )

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

    @filter.llm_tool(name="douyin_hot_videos")
    async def tool_hot_videos(self, event: AstrMessageEvent, keyword: str,
                              limit: int = 5) -> MessageEventResult:
        """读取某个热点话题下的视频。注意：本实现是按热词搜索，不是抖音官方热点视频接口。

        Args:
            keyword(string): 热点关键词或热榜标题
            limit(number): 返回条数，1 到 20，默认 5
        """
        if not (self.enabled and self.enable_llm_tools):
            yield event.plain_result("抖音能力未启用。")
            return
        keyword = str(keyword or "").strip()
        if not keyword:
            yield event.plain_result("[invalid_args] 热点关键词不能为空。")
            return
        limit = max(1, min(int(limit or 5), self.gate.config.max_results))
        try:
            result = await self.backend.hot_videos(keyword, limit)
        except DouyinError as error:
            yield event.plain_result(f"[{error.code}] {redact_text(error.message)}")
            return
        lines = [f"热点「{keyword}」相关视频 {result['count']} 条（{result['source']}）"]
        for item in result["items"]:
            author = (item.get("author") or {}).get("nickname") or "未知"
            lines.append(f"- {item.get('title') or '(无文案)'} | {author} | {item.get('aweme_id')}")
        lines.append(result["note"])
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="douyin_remember")
    async def tool_remember(self, event: AstrMessageEvent, content: str,
                            tag: str = "manual") -> MessageEventResult:
        """把一条与抖音有关的共同经历写入记忆，供后续对话和陪伴小屋召回。只写本地，不做任何账号操作。

        Args:
            content(string): 要记住的内容，不能为空
            tag(string): 分类标签，默认 manual
        """
        text = str(content or "").strip()
        if not text:
            yield event.plain_result("[invalid_args] 记忆内容不能为空。")
            return
        try:
            memory_id = self.store.remember(self._event_key(event), text, tag=str(tag or "manual"))
        except ValueError as error:
            yield event.plain_result(f"[invalid_args] {error}")
            return
        yield event.plain_result(f"已记住（{memory_id}）：{text[:120]}")

    @filter.llm_tool(name="douyin_understand")
    async def tool_understand(self, event: AstrMessageEvent, target: str,
                              count: int = 6) -> MessageEventResult:
        """下载抖音视频并生成理解证据：均匀抽帧 + 可选语音转写 + 结构化 JSON。帧路径交给视觉模型解读。

        Args:
            target(string): aweme_id 或抖音视频/分享链接
            count(number): 抽帧数量，4 到 12，默认 6
        """
        if not (self.enabled and self.enable_llm_tools and self.enable_frames):
            yield event.plain_result("视频理解未启用（需打开 enable_frames 并安装 ffmpeg）。")
            return
        expanded = await self.media.expand_short_url(str(target or ""))
        aweme_id = extract_aweme_id(expanded) or extract_aweme_id(target)
        if not aweme_id:
            yield event.plain_result("[invalid_args] 需要合法的 aweme_id 或抖音链接。")
            return
        evidence = await self.understanding.understand(aweme_id, max(4, min(int(count or 6), 12)))
        lines = [
            f"视频 {aweme_id}：{evidence['meta'].get('desc') or '(无文案)'}",
            f"抽帧 {evidence['frame_count']} 张 / ASR {evidence['transcript']['status']}",
            f"证据文件：{evidence['evidence_path']}",
        ]
        if evidence["transcript"].get("text"):
            lines.append("转写：" + evidence["transcript"]["text"][:300])
        if evidence["frames"]:
            lines.append("帧路径：\n" + "\n".join(evidence["frames"][:6]))
        if evidence["notes"]:
            lines.append("提示：" + "；".join(evidence["notes"][:3]))
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="douyin_replay")
    async def tool_replay(self, event: AstrMessageEvent, target: str) -> MessageEventResult:
        """回放之前生成过的视频理解证据（只读本地文件，不联网）。

        Args:
            target(string): aweme_id 或抖音视频/分享链接
        """
        aweme_id = extract_aweme_id(str(target or ""))
        if not aweme_id:
            yield event.plain_result("[invalid_args] 需要合法的 aweme_id 或抖音链接。")
            return
        evidence = self.understanding.replay(aweme_id)
        if evidence is None:
            yield event.plain_result(f"没有视频 {aweme_id} 的证据记录，请先调用 douyin_understand。")
            return
        yield event.plain_result(
            f"证据 {aweme_id}：{evidence['frame_count']} 帧 / "
            f"ASR {evidence['transcript']['status']}\n"
            f"帧：\n" + "\n".join(evidence["frames"][:6])
        )

    # ------------------------------------------------- 账号侧 LLM 工具（读） --
    @filter.llm_tool(name="douyin_feed")
    async def tool_feed(self, event: AstrMessageEvent, num: int = 10,
                        scrolls: int = 6) -> MessageEventResult:
        """刷抖音推荐流（首页推荐，不是关键词搜索），返回露出的视频标题/作者/点赞数。

        Args:
            num(number): 要几条，1 到 30，默认 10
            scrolls(number): 最多滚几屏，1 到 30，默认 6
        """
        blocked = await self._browser_ready()
        if blocked or not self.enable_llm_tools:
            yield event.plain_result(blocked or "LLM 工具未启用。")
            return
        result = await self.browser.feed(
            clamp(num, 1, 30, 10), clamp(scrolls, 1, self.feed_max_scrolls, self.feed_max_scrolls)
        )
        if not result.get("ok"):
            yield event.plain_result(f"推荐流读取失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        items = result.get("items") or []
        if not items:
            yield event.plain_result("推荐流没有抓到视频（可能未登录或被风控），不要编造内容。")
            return
        lines = [f"推荐流 {len(items)} 条："]
        lines += [f"- {i.get('title') or '(无文案)'} | @{i.get('author') or '?'} "
                  f"| 赞 {i.get('digg') or 0} | {i.get('url')}" for i in items]
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="douyin_comments")
    async def tool_comments(self, event: AstrMessageEvent, target: str,
                            num: int = 30) -> MessageEventResult:
        """读取一条抖音视频的评论列表（只读）。

        Args:
            target(string): aweme_id 或抖音视频/分享链接
            num(number): 取几条，1 到 100，默认 30
        """
        blocked = await self._browser_ready()
        if blocked or not self.enable_llm_tools:
            yield event.plain_result(blocked or "LLM 工具未启用。")
            return
        result = await self.browser.comments(target, clamp(num, 1, 100, 30))
        if not result.get("ok"):
            yield event.plain_result(f"评论读取失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        items = result.get("items") or result.get("comments") or []
        if not items:
            yield event.plain_result("没有读到评论（可能要登录，或该作品关闭了评论）。")
            return
        lines = [f"评论 {len(items)} 条："]
        lines += [f"- @{i.get('nickname') or i.get('author') or '?'}："
                  f"{str(i.get('text') or '')[:140]}" for i in items[:20]]
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="douyin_inbox")
    async def tool_inbox(self, event: AstrMessageEvent, nickname: str = "",
                         limit: int = 8) -> MessageEventResult:
        """读取抖音私信收件箱；给 nickname 时只读与这个人的会话。只返回有消息 ID 的入站消息。

        Args:
            nickname(string): 好友完整昵称；留空则读最近会话
            limit(number): 最多几个会话，1 到 12，默认 8
        """
        blocked = await self._browser_ready()
        if blocked or not self.enable_llm_tools:
            yield event.plain_result(blocked or "LLM 工具未启用。")
            return
        if self.dm_private_only and event.get_group_id():
            yield event.plain_result("私信内容不在群里展示（dm_private_only=true）。")
            return
        result = await self.browser.inbox(str(nickname or "").strip(), clamp(limit, 1, 12, 8))
        if not result.get("ok"):
            yield event.plain_result(f"私信读取失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        items = result.get("items") or []
        key = self._event_key(event)
        for item in items:
            self.store.put_dm(key, str(item.get("name") or ""), item)
        if not items:
            yield event.plain_result("没有新的入站私信（预览文字不算消息）。")
            return
        lines = ["私信（入站）："]
        lines += [f"- {i.get('name') or '?'}：{str(i.get('text') or '')[:140]}" for i in items]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------- 账号侧 LLM 工具（写） --
    @filter.llm_tool(name="douyin_like")
    async def tool_like(self, event: AstrMessageEvent, target: str,
                        undo: bool = False, confirm: bool = False) -> MessageEventResult:
        """给抖音视频点赞或取消点赞。默认演练：必须由用户确认后传 confirm=true 才会真执行。

        Args:
            target(string): aweme_id 或抖音视频/分享链接
            undo(boolean): true 表示取消点赞
            confirm(boolean): 用户已明确同意执行，才传 true
        """
        blocked = await self._browser_ready()
        if blocked or not self.enable_llm_tools:
            yield event.plain_result(blocked or "LLM 工具未启用。")
            return
        decision = self.policy.check("like", target, confirm=bool(confirm))
        if decision.mode != "live":
            yield event.plain_result(
                f"{decision.message()}\n把这条告诉用户，等用户明确同意后再以 confirm=true 调用；不要自行重试。"
            )
            return
        result = await self.browser.like(target, bool(undo))
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"点赞失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        yield event.plain_result(
            f"已完成：{'取消点赞' if undo else '点赞'} {result.get('aweme_id') or target}；"
            f"页面状态={result.get('state') or '?'}；已核对={bool(result.get('verified'))}"
        )

    @filter.llm_tool(name="douyin_comment")
    async def tool_comment(self, event: AstrMessageEvent, target: str, text: str,
                           confirm: bool = False) -> MessageEventResult:
        """在抖音视频下发评论。默认演练：必须由用户确认后传 confirm=true 才会真执行。

        Args:
            target(string): aweme_id 或抖音视频/分享链接
            text(string): 评论正文，短、纯文本，不超过 300 字
            confirm(boolean): 用户已明确同意发布，才传 true
        """
        blocked = await self._browser_ready()
        if blocked or not self.enable_llm_tools:
            yield event.plain_result(blocked or "LLM 工具未启用。")
            return
        body = str(text or "").strip()
        if not body:
            yield event.plain_result("[invalid_args] 评论正文不能为空。")
            return
        decision = self.policy.check("comment", target, confirm=bool(confirm))
        if decision.mode != "live":
            yield event.plain_result(
                f"{decision.message()}\n将要发布的内容：{body[:120]}\n"
                "先让用户确认这段文字，再以 confirm=true 调用；不要自行重试或改写。"
            )
            return
        result = await self.browser.comment(target, body, mention=self.comment_mention)
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"评论未确认成功：{result.get('error')}{self._need_login_hint(result)}")
            return
        yield event.plain_result(
            f"评论已发：cid={result.get('cid') or '?'}；取证={result.get('evidence') or '?'}"
        )

    @filter.llm_tool(name="douyin_share")
    async def tool_share(self, event: AstrMessageEvent, target: str, nickname: str,
                         confirm: bool = False) -> MessageEventResult:
        """把抖音视频转发给指定好友。默认演练：必须由用户确认后传 confirm=true。

        Args:
            target(string): aweme_id 或抖音视频/分享链接
            nickname(string): 好友完整昵称（不支持模糊匹配）
            confirm(boolean): 用户已明确同意转发，才传 true
        """
        blocked = await self._browser_ready()
        if blocked or not self.enable_llm_tools:
            yield event.plain_result(blocked or "LLM 工具未启用。")
            return
        decision = self.policy.check("share", nickname, confirm=bool(confirm))
        if decision.mode != "live":
            yield event.plain_result(f"{decision.message()}\n把这条告诉用户，等确认后以 confirm=true 调用。")
            return
        result = await self.browser.share(target, nickname)
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"转发失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        yield event.plain_result(
            f"已转发给 {nickname}；取证={result.get('evidence') or '?'}；已核对={bool(result.get('verified'))}"
        )

    @filter.llm_tool(name="douyin_dm")
    async def tool_dm(self, event: AstrMessageEvent, nickname: str, text: str,
                      confirm: bool = False) -> MessageEventResult:
        """给抖音好友发私信。默认演练：必须由用户确认后传 confirm=true 才会真发。

        Args:
            nickname(string): 好友完整昵称
            text(string): 私信正文，不超过 1000 字
            confirm(boolean): 用户已明确同意发送，才传 true
        """
        blocked = await self._browser_ready()
        if blocked or not self.enable_llm_tools:
            yield event.plain_result(blocked or "LLM 工具未启用。")
            return
        if self.dm_private_only and event.get_group_id():
            yield event.plain_result("私信不在群里发送（dm_private_only=true）。")
            return
        decision = self.policy.check("dm", nickname, confirm=bool(confirm))
        if decision.mode != "live":
            yield event.plain_result(
                f"{decision.message()}\n将要发送：{str(text or '')[:120]}\n"
                "先让用户确认收件人与正文，再以 confirm=true 调用。"
            )
            return
        result = await self.browser.dm_send(nickname, text)
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"私信未确认成功：{result.get('error')}{self._need_login_hint(result)}")
            return
        yield event.plain_result(
            f"私信已发：{nickname}；消息 ID={result.get('messageId') or '?'}；"
            f"取证={result.get('evidence') or '?'}"
        )

    @filter.llm_tool(name="douyin_publish")
    async def tool_publish(self, event: AstrMessageEvent, path: str, title: str = "",
                           draft: bool = True, confirm: bool = False) -> MessageEventResult:
        """投稿视频到抖音创作者中心。默认草稿，必须由用户确认后传 confirm=true。

        Args:
            path(string): 视频文件绝对路径，必须在 publish_allow_dir 内
            title(string): 作品标题，留空用日期
            draft(boolean): true=只存草稿（暂存离开），false=真正发布
            confirm(boolean): 用户已明确同意投稿，才传 true
        """
        blocked = await self._browser_ready()
        if blocked or not self.enable_llm_tools:
            yield event.plain_result(blocked or "LLM 工具未启用。")
            return
        ok, reason = self.policy.check_publish_path(path)
        if not ok:
            yield event.plain_result(f"投稿被拒：{reason}")
            return
        decision = self.policy.check("publish", path, confirm=bool(confirm))
        if decision.mode != "live":
            yield event.plain_result(
                f"{decision.message()}\n文件：{path}；标题：{title or '(日期)'}；"
                f"模式：{'草稿' if draft else '直接发布'}\n等用户确认后以 confirm=true 调用。"
            )
            return
        result = await self.browser.publish(
            path, title or f"日常 {time.strftime('%Y-%m-%d')}",
            tags=str(self.config.get("publish_tags") or ""),
            draft=bool(draft), ai_declare=True,
        )
        self.policy.record(decision, ok=bool(result.get("ok")), detail=str(result.get("error") or ""))
        if not result.get("ok"):
            yield event.plain_result(f"投稿失败：{result.get('error')}{self._need_login_hint(result)}")
            return
        yield event.plain_result(
            f"投稿完成：{'草稿' if draft else '已发布'}；"
            f"ID={result.get('platformId') or result.get('draftId') or '?'}；"
            f"已核对={bool(result.get('verified'))}"
        )
