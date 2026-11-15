"""dy-cli 子进程适配层（Python 版 ``lib/backend.js``）。

设计与上游一致：**插件不实现 X-Bogus / a_bogus 签名、不实现登录协议、不重写爬虫**。
读路径一律通过 ``python -m dy_cli.main``（或配置指定的解释器）取数，这里只负责：
有界 stdout/stderr、超时与取消、JSON 信封提取、错误码映射、全链路脱敏、串行配额门。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .errors import DouyinError, normalize_code
from .gate import AsyncRateGate
from .redact import redact, redact_text, strip_ansi

MAX_STDOUT = 4_000_000
MAX_STDERR = 2_000_000
_ENVELOPE_OK_KEY = "ok"


def extract_envelope(stdout: str) -> dict[str, Any] | None:
    """dy-cli 的 stdout 混有 Rich 进度条和 ANSI 色码；从后往前找最后一个合法信封。"""
    clean = strip_ansi(stdout).replace("\r\n", "\n")
    lines = clean.split("\n")
    starts = [i for i, line in enumerate(lines)
              if (t := line.strip()) == "{" or t.startswith('{"') or t.startswith('{ "')]
    for start in reversed(starts):
        candidate = "\n".join(lines[start:]).strip()
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and _ENVELOPE_OK_KEY in parsed:
            return parsed
    return None


class DyCliBackend:
    def __init__(
        self,
        *,
        gate: AsyncRateGate,
        data_dir: Path,
        python_path: str = "",
        dy_query_script: Path | None = None,
        cookie_file: Path | None = None,
        log_dir: Path | None = None,
        log_level: str = "info",
    ):
        self.gate = gate
        self.data_dir = Path(data_dir)
        self.python_path = python_path or sys.executable
        self.dy_query_script = Path(dy_query_script) if dy_query_script else None
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self.log_dir = Path(log_dir) if log_dir else self.data_dir / "logs"
        self.log_level = log_level if log_level in ("silent", "error", "info", "debug") else "info"
        self.last_error: dict[str, Any] | None = None
        self.dy_module: dict[str, Any] | None = None
        self.detail_api_broken = False
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ env
    def child_env(self) -> dict[str, str]:
        """把子进程的主目录 / 临时目录全部收进插件数据区，避免污染用户目录。"""
        env = dict(os.environ)
        env.update({
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "TERM": "dumb",
            "DOUYIN_DATA_DIR": str(self.data_dir),
        })
        if self.cookie_file:
            env["DOUYIN_COOKIE_FILE"] = str(self.cookie_file)
        return env

    # --------------------------------------------------------------- process
    async def _run_process(self, args: list[str], timeout: float) -> dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.data_dir),
                env=self.child_env(),
            )
        except (OSError, ValueError) as error:
            raise DouyinError("backend_unavailable", redact_text(f"无法启动子进程：{error}")) from error
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as error:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise DouyinError(
                "backend_unavailable",
                f"子进程超时（{int(timeout)}s）：{args[0]} {(args[1] or '') if len(args) > 1 else ''}",
            ) from error
        return {
            "code": proc.returncode or 0,
            "stdout": stdout_b.decode("utf-8", "ignore")[:MAX_STDOUT],
            "stderr": stderr_b.decode("utf-8", "ignore")[:MAX_STDERR],
        }

    def _parse(self, out: dict[str, Any], command: str, tool: str) -> Any:
        envelope = extract_envelope(out["stdout"])
        raw = f"{out['stderr']}\n{out['stdout']}".strip()
        if out["code"] != 0 or not envelope or envelope.get("ok") is not True:
            err = envelope.get("error") if isinstance(envelope, dict) else None
            err = err if isinstance(err, dict) else {}
            code = normalize_code(err.get("code"), f"{err.get('message', '')} {raw}")
            message = redact_text(err.get("message")) or redact_text(
                " ".join(strip_ansi(raw).split("\n")[-3:])
            ) or f"{command} 失败（exit {out['code']}）"
            raise DouyinError(code, message, {"command": command})
        return envelope.get("data")

    # ------------------------------------------------------------- public API
    async def run_dy(self, args: list[str], *, tool: str, timeout: float = 90.0, units: int = 1) -> Any:
        command = args[0] if args else "dy"

        async def task() -> Any:
            out = await self._run_process([self.python_path, "-m", "dy_cli.main", *args], timeout)
            return self._parse(out, command, tool)

        try:
            return await self.gate.guarded(tool, task, units=units)
        except DouyinError as error:
            self._record_error(tool, error)
            raise

    async def run_dy_query(self, args: list[str], *, tool: str, timeout: float = 90.0) -> Any:
        """搜索分页：优先用随包的 dy_query.py（补 --offset / --logid 链式翻页）。"""
        if self.dy_query_script and self.dy_query_script.exists():
            argv = [self.python_path, str(self.dy_query_script), *args]
        else:
            argv = [self.python_path, "-m", "dy_cli.main", *args]

        async def task() -> Any:
            out = await self._run_process(argv, timeout)
            return self._parse(out, "search", tool)

        try:
            return await self.gate.guarded(tool, task, units=1)
        except DouyinError as error:
            self._record_error(tool, error)
            raise

    async def hot(self, count: int) -> dict[str, Any]:
        data = await self.run_dy(["trending", "--count", str(count), "--json-output"], tool="douyin_hot")
        from .normalize import normalize_hot_items
        items = normalize_hot_items(data)[:count]
        return {"items": items, "count": len(items), "partial": False, "failures": []}

    async def search(self, keyword: str, limit: int, pages: int, sort: str = "综合") -> dict[str, Any]:
        from .normalize import normalize_search_payload
        items: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        offset, logid = 0, ""
        for page in range(pages):
            if page > 0:
                await self.gate.page_delay()
            argv = ["search", keyword, "--count", str(limit), "--offset", str(offset), "--sort", sort]
            if logid:
                argv += ["--logid", logid]
            try:
                data = await self.run_dy_query(argv, tool="douyin_search")
                normalized = normalize_search_payload(data)
                items.extend(normalized["items"])
                trace.append({
                    "page": page + 1,
                    "offset": offset,
                    "raw_count": normalized["raw_count"],
                    "items": len(normalized["items"]),
                    "has_more": normalized["has_more"],
                    "chained_with_logid": bool(logid),
                })
                next_offset = normalized["next_offset"] or (offset + limit)
                if normalized["has_more"] and normalized["logid"]:
                    offset, logid = int(next_offset), normalized["logid"]
                else:
                    offset, logid = int(next_offset), ""
                    break
                if not normalized["items"]:
                    break
            except DouyinError as error:
                if page == 0:
                    raise
                failures.append({"page": page + 1, "code": error.code, "message": error.message})
                break
        capped = items[: limit * pages]
        return {
            "keyword": keyword,
            "sort": sort,
            "items": capped,
            "count": len(capped),
            "partial": bool(failures),
            "failures": failures,
            "coverage": {"pages_requested": pages, "pages_completed": len(trace), "offsets": trace},
        }

    async def video(self, aweme_id: str) -> dict[str, Any]:
        from .normalize import normalize_aweme
        try:
            data = await self.run_dy(["detail", aweme_id, "--json-output"], tool="douyin_video")
        except DouyinError as error:
            if error.code not in ("not_found", "parse_failed", "backend_unavailable"):
                raise
            self.detail_api_broken = True
            # 逆向 detail 接口对该账号不稳定时，上游会回退浏览器签名 XHR。
            # 本改编版不附带浏览器驱动，明确返回可解释的失败，不静默伪造数据。
            raise DouyinError(
                error.code,
                f"视频详情接口不可用（{error.code}）；本插件未启用浏览器回退，请稍后重试或改用分享链接解析。",
            ) from error
        video = normalize_aweme(data)
        if not video:
            raise DouyinError("parse_failed", f"视频 {aweme_id} 详情为空或无法解析。")
        return {"video": video, "partial": False, "failures": []}

    async def user(self, sec_uid: str) -> dict[str, Any]:
        from .normalize import normalize_user
        data = await self.run_dy(["profile", sec_uid, "--json-output"], tool="douyin_user", timeout=60.0)
        user = normalize_user(data)
        if not user:
            raise DouyinError("parse_failed", f"用户 {sec_uid} 资料无法解析。")
        return {"user": user, "posts": [], "partial": False, "failures": []}

    async def check_dy_module(self) -> dict[str, Any]:
        try:
            out = await self._run_process(
                [self.python_path, "-c", "import dy_cli; print(getattr(dy_cli,'__version__','unknown'))"],
                20.0,
            )
            self.dy_module = ({"ok": True, "version": out["stdout"].strip()} if out["code"] == 0
                              else {"ok": False, "error": redact_text(out["stderr"][-500:])})
        except Exception as error:  # 依赖探测失败不能影响插件启动
            self.dy_module = {"ok": False, "error": redact_text(error)}
        return self.dy_module

    async def status(self) -> dict[str, Any]:
        dy = self.dy_module or await self.check_dy_module()
        cookie_present = bool(self.cookie_file and self.cookie_file.exists())
        quota = self.gate.quota()
        problems: list[str] = []
        if not dy.get("ok"):
            problems.append("dy-cli 未就绪（需在配置的 Python 环境安装 dy-cli==0.2.2）")
        if not cookie_present:
            problems.append("未发现 storage_state cookie 文件（热榜可用；搜索/详情多半需要登录）")
        if self.gate.backoff_seconds() > 0:
            problems.append(f"限流退避中，剩余 {int(self.gate.backoff_seconds())} 秒")
        if self.last_error and self.last_error["code"] == "challenge_required":
            problems.append("触发人机验证：请在有头浏览器里人工完成，不要写脚本硬刷")
        return {
            "summary": "可用：后端就绪。" if not problems else "可用但有提示：" + "；".join(problems),
            "backend": {"python": self.python_path, "dy_cli": dy, "cookie_present": cookie_present},
            "quota": quota,
            "detail_api_broken": self.detail_api_broken,
            "last_error": self.last_error,
            "problems": problems,
        }

    # ---------------------------------------------------------------- logging
    def _record_error(self, tool: str, error: DouyinError) -> None:
        self.last_error = {
            "code": error.code,
            "message": redact_text(error.message),
            "tool": tool,
        }
        self.log({"tool": tool, "ok": False, "code": error.code, "message": error.message})

    def log(self, entry: dict[str, Any]) -> None:
        if self.log_level == "silent":
            return
        import datetime
        record = redact({"ts": datetime.datetime.now().isoformat(timespec="seconds"), **entry})
        try:
            day = datetime.date.today().isoformat()
            with (self.log_dir / f"douyin-{day}.log").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 日志绝不能打断工具调用

    async def close(self) -> None:
        self.gate.dispose()
