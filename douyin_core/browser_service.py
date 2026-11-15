"""Playwright 浏览器会话适配层。

职责边界（很重要）：

* **不做**抖音页面操作。真正点按钮、打 @、核对回执、判私信方向，全部在
  :mod:`douyin_core.browser_driver`（vendored 上游内核）里。
* 只做四件事：配置注入 → 会话生命周期 → 统一返回形状 → 脱敏。
* 写不写由 :class:`douyin_core.action_policy.ActionPolicy` 决定，本层不判断策略。

为什么用浏览器而不是纯 HTTP
--------------------------
点赞 / 评论 / 私信 / 投稿都是**账号行为**，走的是签名后的 XHR；而抖音 Web 的 DOM
class 是哈希的、经常改版。上游内核的做法是「操作 + 同一页面内的二次取证」
（``data-e2e-state`` 翻转、``/comment/publish`` 回执、新增出站气泡），
本层原样复用，失败就如实返回 ``status=unknown``，不伪造成功。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .errors import DouyinError
from .normalize import extract_aweme_id
from .redact import redact_text

# 上游内核里每个函数都可能抛，统一封顶，避免 AstrBot 的指令处理被挂死
FEED_TIMEOUT = 240
READ_TIMEOUT = 120
WRITE_TIMEOUT = 180
PUBLISH_TIMEOUT = 420
LOGIN_TIMEOUT = 90


class BrowserUnavailable(DouyinError):
    def __init__(self, message: str, detail: str = ""):
        super().__init__("backend_unavailable", message)
        self.detail = detail


def _flatten(result: Any) -> dict[str, Any]:
    """把上游 ``_ok/_err/needLogin`` 统一成 ``{ok, error, ...}`` 并脱敏。"""
    if not isinstance(result, dict):
        return {"ok": True, "result": result}
    out = {k: v for k, v in result.items() if k != "runtime"}
    if not out.get("ok"):
        out["error"] = redact_text(
            str(out.get("error") or out.get("hint") or "抖音操作失败")
        )
    return out


class BrowserService:
    """一个进程一份 Chrome profile；同一时刻只允许一个页面任务。"""

    def __init__(
        self,
        *,
        data_dir: Path,
        headless: bool = False,
        channel: str = "auto",
        cdp_url: str = "",
        chrome_path: str = "",
        profile_port: int = 9333,
        default_timeout: int = 90,
    ):
        self.data_dir = Path(data_dir)
        self.headless = bool(headless)
        self.channel = str(channel or "auto").strip().lower()
        self.cdp_url = str(cdp_url or "").strip()
        self.chrome_path = str(chrome_path or "").strip()
        self.profile_port = int(profile_port)
        self.default_timeout = max(30, min(int(default_timeout), 600))
        self._driver: Any = None
        self._import_error = ""
        self._env_applied = False

    # ------------------------------------------------------------- 生命周期 --
    def _apply_env(self) -> None:
        """上游内核在 import 时读这些变量，所以必须在导入前写好。"""
        if self._env_applied:
            return
        home = self.data_dir / "browser_home"
        home.mkdir(parents=True, exist_ok=True)
        os.environ["DSH_DOUYIN_HOME"] = str(home)
        os.environ["DOUYIN_HEADLESS"] = "1" if self.headless else "0"
        os.environ["DOUYIN_PROFILE_PORT"] = str(self.profile_port)
        # 装饰器层的写开关永远是 0：策略只在本插件的 ActionPolicy 里判，
        # 这样即使有人直接 import 内核也拿不到「自动写」的后门。
        os.environ["DOUYIN_ALLOW_WRITES"] = "0"
        os.environ.setdefault("PYTHONUTF8", "1")
        if self.channel in ("chrome", "msedge"):
            os.environ["DOUYIN_BROWSER_CHANNEL"] = self.channel
        if self.chrome_path:
            os.environ["DOUYIN_CHROME_PATH"] = self.chrome_path
        if self.cdp_url:
            os.environ["DOUYIN_CDP_URL"] = self.cdp_url
        self._env_applied = True

    def driver(self) -> Any:
        if self._driver is not None:
            return self._driver
        if self._import_error:
            raise BrowserUnavailable(self._import_error)
        self._apply_env()
        try:
            module = importlib.import_module(f"{__package__}.browser_driver")
        except Exception as exc:  # pragma: no cover - 依赖缺失时的兜底
            self._import_error = f"浏览器内核导入失败：{type(exc).__name__}: {exc}"
            raise BrowserUnavailable(self._import_error) from exc
        self._driver = module
        return module

    def probe(self) -> dict[str, Any]:
        """不启动浏览器，只回答「能不能用、缺什么」。"""
        info: dict[str, Any] = {
            "ok": True,
            "headless": self.headless,
            "channel": self.channel or "auto",
            "cdp": bool(self.cdp_url),
            "profile_port": self.profile_port,
        }
        try:
            self.driver()
        except BrowserUnavailable as exc:
            return {**info, "ok": False, "error": exc.message, "detail": exc.detail}
        try:
            importlib.import_module("playwright.async_api")
            info["playwright"] = True
        except Exception as exc:
            info["playwright"] = False
            info["ok"] = False
            info["error"] = f"未安装 playwright：{type(exc).__name__}"
            info["detail"] = "在 AstrBot 的 Python 环境里执行 pip install playwright>=1.45"
        drv = self._driver
        try:
            info["browser_channel"] = drv.BROWSER_CHANNEL or "bundled"
            info["browser_exe"] = Path(drv.BROWSER_EXE).name if drv.BROWSER_EXE else ""
            info["profile_dir"] = str(drv.PROFILE_DIR)
            info["mode"] = drv._browser.mode
        except Exception:
            pass
        return info

    async def close(self) -> None:
        if self._driver is None:
            return
        try:
            await self._driver._browser.close()
        except Exception:
            pass

    # --------------------------------------------------------------- 执行器 --
    async def _run(self, fn, *, timeout: int | None = None, chat: bool = False) -> dict[str, Any]:
        try:
            drv = self.driver()
        except BrowserUnavailable as exc:
            return {"ok": False, "error": exc.message, "detail": exc.detail}

        async def impl():
            ctx = await drv._browser.context()
            page = await (drv._browser.page_chat() if chat else drv._browser.page())
            return await fn(drv, page, ctx)

        try:
            result = await drv._guard(impl, timeout or self.default_timeout)
        except Exception as exc:  # 内核理论上不会抛，但绝不能让异常穿到指令层
            return {"ok": False, "error": redact_text(f"{type(exc).__name__}: {exc}")}
        return _flatten(result)

    async def _run_json(self, factory, *, timeout: int | None = None) -> dict[str, Any]:
        """上游有几个 MCP 包装函数返回 JSON 字符串，直接复用。"""
        try:
            drv = self.driver()
        except BrowserUnavailable as exc:
            return {"ok": False, "error": exc.message, "detail": exc.detail}
        try:
            raw = await asyncio.wait_for(factory(drv), timeout=timeout or self.default_timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"操作超时（{timeout or self.default_timeout}s）", "status": "unknown"}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": redact_text(f"{type(exc).__name__}: {exc}")}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "error": redact_text(raw[:300])}
        return _flatten(raw)

    # ------------------------------------------------------------- 只读能力 --
    async def login_state(self) -> dict[str, Any]:
        async def impl(drv, page, ctx):
            state = await drv._login_state(page, ctx)
            return {"ok": True, **state}

        return await self._run(impl, timeout=LOGIN_TIMEOUT)

    async def feed(self, num: int = 10, scrolls: int = 6) -> dict[str, Any]:
        num = max(1, min(int(num), 50))
        scrolls = max(1, min(int(scrolls), 30))
        return await self._run(
            lambda drv, page, ctx: drv._feed_core(page, ctx, num, scrolls, True),
            timeout=FEED_TIMEOUT,
        )

    async def comments(self, target: str, num: int = 30) -> dict[str, Any]:
        aweme_id = extract_aweme_id(str(target or ""))
        if not aweme_id:
            return {"ok": False, "error": "aweme_id 不合法"}
        num = max(1, min(int(num), 100))
        return await self._run(
            lambda d, page, ctx: d._comments_core(page, aweme_id, num),
            timeout=READ_TIMEOUT,
        )

    async def inbox(self, nickname: str = "", limit: int = 12) -> dict[str, Any]:
        limit = max(1, min(int(limit), 12))
        return await self._run_json(
            lambda drv: drv.douyin_inbox(limit=limit, nickname=str(nickname or "").strip()),
            timeout=READ_TIMEOUT,
        )

    # ------------------------------------------------------------- 写操作 ----
    async def like(self, target: str, undo: bool = False) -> dict[str, Any]:
        aweme_id = extract_aweme_id(str(target or ""))
        if not aweme_id:
            return {"ok": False, "error": "aweme_id 不合法"}
        return await self._run(
            lambda d, page, ctx: d._like_core(page, ctx, aweme_id, bool(undo)),
            timeout=WRITE_TIMEOUT,
        )

    async def comment(
        self,
        target: str,
        text: str,
        *,
        reply_cid: str = "",
        mention: str = "",
    ) -> dict[str, Any]:
        aweme_id = extract_aweme_id(str(target or ""))
        if not aweme_id:
            return {"ok": False, "error": "aweme_id 不合法"}
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "error": "评论正文不能为空"}
        if len(body) > 300:
            body = body[:300]
        return await self._run(
            lambda d, page, ctx: d._comment_core(
                page, ctx, aweme_id, body,
                str(reply_cid or ""), str(mention or ""), -1,
            ),
            timeout=WRITE_TIMEOUT,
        )

    async def share(self, target: str, nickname: str) -> dict[str, Any]:
        aweme_id = extract_aweme_id(str(target or ""))
        nick = str(nickname or "").strip()
        if not aweme_id:
            return {"ok": False, "error": "aweme_id 不合法"}
        if not nick:
            return {"ok": False, "error": "转发必须给完整昵称（不支持模糊匹配）"}
        return await self._run(
            lambda d, page, ctx: d._share_core(page, ctx, aweme_id, nick),
            timeout=WRITE_TIMEOUT,
            chat=True,
        )

    async def dm_send(self, nickname: str, text: str) -> dict[str, Any]:
        nick = str(nickname or "").strip()
        body = str(text or "").strip()
        if not nick:
            return {"ok": False, "error": "私信必须给完整昵称"}
        if not body:
            return {"ok": False, "error": "私信正文不能为空"}
        if len(body) > 1000:
            return {"ok": False, "error": "私信正文过长（≤1000 字）"}
        return await self._run_json(
            lambda drv: drv.douyin_chat_send(nick, body),
            timeout=WRITE_TIMEOUT,
        )

    async def publish(
        self,
        video_path: str,
        title: str,
        *,
        tags: str = "",
        draft: bool = True,
        ai_declare: bool = True,
        cover_path: str = "",
    ) -> dict[str, Any]:
        path = Path(str(video_path or "")).expanduser()
        if not path.is_file():
            return {"ok": False, "error": f"视频文件不存在：{path}"}
        name = str(title or "").strip() or f"日常 {time.strftime('%Y-%m-%d')}"
        return await self._run(
            lambda drv, page, ctx: drv._publish_core(
                page, ctx, str(path), name, str(tags or ""),
                bool(draft), bool(ai_declare), str(cover_path or ""),
            ),
            timeout=PUBLISH_TIMEOUT,
        )

    # ------------------------------------------------------------- 登录引导 --
    async def open_login(self) -> dict[str, Any]:
        async def impl(drv, page, ctx):
            await drv._goto(page, "https://www.douyin.com/jingxuan", settle=3.0)
            clicked = await drv._click_text(page, "登录", exact=True)
            if not clicked:
                clicked = await drv._click_text(page, "立即登录", exact=False)
            return {"ok": True, "clickedLogin": bool(clicked), "url": page.url,
                    "note": "浏览器窗口已打开，请在窗口里扫码或短信登录；完成后用 /抖音状态 复查"}

        return await self._run(impl, timeout=LOGIN_TIMEOUT)
