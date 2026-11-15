#!/usr/bin/env python3
"""上游能力哨兵：检测 kdcadmin/dsh_douyin_tools 的工具清单变化并生成能力映射报告。

⚠️ 默认 DRY_RUN：只打印，不写任何文件。加 ``--write`` 才落盘。
本项目是 Python/AstrBot 插件，上游是 Node/Cordis 插件，**不做代码 merge**；
哨兵只负责「感知变更 + 生成差异报告」，移植由人/AI 审查后完成。

用法：
    python tools/upstream_diff.py --upstream-dir /tmp/dsh_douyin_tools           # DRY_RUN
    python tools/upstream_diff.py --upstream-dir /tmp/dsh_douyin_tools --check   # 有变更返回 1
    python tools/upstream_diff.py --upstream-dir /tmp/dsh_douyin_tools --write   # 落盘
    python tools/upstream_diff.py --online                                        # 直接抓 raw 文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_FILE = ROOT / "UPSTREAM_BASE"
MAPPING_FILE = ROOT / "docs" / "UPSTREAM_MAPPING.md"
CHANGE_FILE = ROOT / "docs" / "UPSTREAM_CHANGE.md"
RAW_BASE = "https://raw.githubusercontent.com/kdcadmin/dsh_douyin_tools/main/"

TOOL_RE = re.compile(r"\bdy_[a-z][a-z0-9_]{1,40}\b")
VERSION_RE = re.compile(r'"version"\s*:\s*"([0-9][^"]*)"')

# 静态能力映射：上游工具 → 本插件状态。新增工具会标 need_review。
STATUS: dict[str, tuple[str, str, str]] = {
    "dy_hot": ("douyin_hot", "implemented", "热榜，无需登录"),
    "dy_hot_videos": ("douyin_hot_videos", "partial", "按热词搜索实现，非官方热点视频接口"),
    "dy_search": ("douyin_search", "implemented", "search_id 链式翻页"),
    "dy_video": ("douyin_video", "implemented", "文案/作者/互动数据"),
    "dy_thumb": ("douyin_frames / douyin_understand", "implemented", "ffmpeg 均匀抽帧"),
    "dy_watch": ("douyin_understand", "implemented", "抽帧 + 可选 faster-whisper 转写 + 证据 JSON"),
    "dy_remember": ("douyin_remember", "implemented", "写入身份隔离的 SQLite"),
    "dy_status": ("douyin_selfcheck / /抖音状态", "implemented", "含依赖矩阵与配额"),
    "dy_selfcheck": ("douyin_selfcheck", "implemented", "真跑依赖探测"),
    "dy_cleanup": ("douyin_cleanup", "implemented", "有界 + 默认 dry-run"),
    "dy_config": ("_conf_schema.json", "partial", "AstrBot 可视化配置，无运行时 patch 接口"),
    "dy_whoami": ("/抖音状态 登录", "partial", "登录态由 vendored 内核探测（cookie + 头像双证据）"),
    "dy_feed": ("douyin_feed / /抖音推荐", "implemented", "拦截 /module/feed/ XHR + 滚动收集"),
    "dy_comments": ("douyin_comments / /抖音评论", "implemented", "页面 XHR 优先，DOM 兜底"),
    "dy_my_posts": ("—", "not_ported", "需创作者中心页面，尚未接"),
    "dy_my_videos": ("—", "not_ported", "需创作者中心页面，尚未接"),
    "dy_dm_inbox": ("douyin_inbox / /抖音私信", "implemented", "只返回有消息 ID 的入站消息"),
    "dy_dm_poll": ("—", "not_ported", "常驻轮询属于自动回复，未接"),
    "dy_dm_round": ("—", "not_ported", "自动回复回合，未接"),
    "dy_reply_round": ("—", "not_ported", "自动回复回合，未接"),
    "dy_reply": ("douyin_comment（reply_cid 接口已备）", "partial", "内核支持按 cid/序号回复，CLI 暂未暴露"),
    "dy_like": ("douyin_like / /抖音点赞", "implemented", "受 ActionPolicy 四重闸门约束"),
    "dy_comment": ("douyin_comment / /抖音发评论", "implemented", "受 ActionPolicy 约束，支持真人 @ 联想"),
    "dy_share": ("douyin_share / /抖音转发", "implemented", "受收件人白名单约束"),
    "dy_chat_send": ("douyin_dm / /抖音发私信", "implemented", "受收件人白名单 + 每日额度约束"),
    "dy_chat_sticker": ("—", "not_ported", "表情包发送未接"),
    "dy_publish": ("douyin_publish / /抖音投稿", "implemented", "默认草稿，投稿目录白名单"),
    "dy_run": ("—", "not_ported", "上游定时巡检依赖 DSH 调度器"),
    "dy_reconcile_pending": ("store.recent_actions", "partial", "本地账本可查，未做平台对账"),
    "dy_chrome_open": ("/抖音登录", "partial", "浏览器由 Playwright 拉起，没有独立开窗指令"),
    "dy_login_start": ("（外部前置步骤）", "partial", "无头不能扫码，改用 storage_state 导出"),
    "dy_login_poll": ("（外部前置步骤）", "partial", "同上"),
    "dy_login_sms": ("（外部前置步骤）", "partial", "同上"),
    "dy_login_sms_submit": ("（外部前置步骤）", "partial", "同上"),
}


def read_upstream(directory: Path | None, online: bool) -> tuple[str, str, str]:
    """返回 (README 文本, SPEC/package 文本, commit SHA)。"""
    if online:
        readme = fetch(RAW_BASE + "README.md")
        spec = fetch(RAW_BASE + "SPEC.md") + "\n" + fetch(RAW_BASE + "package.json")
        return readme, spec, "online"
    if not directory or not directory.exists():
        raise SystemExit("需要 --upstream-dir <上游仓库目录> 或 --online")
    readme = (directory / "README.md").read_text(encoding="utf-8", errors="ignore")
    spec = (directory / "SPEC.md").read_text(encoding="utf-8", errors="ignore")
    spec += "\n" + (directory / "package.json").read_text(encoding="utf-8", errors="ignore")
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(directory), "rev-parse", "HEAD"], text=True, timeout=20
        ).strip()
    except Exception:
        commit = "unknown"
    return readme, spec, commit


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "upstream-sentinel"}, method="GET")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "ignore")


def parse(text: str) -> tuple[list[str], str]:
    tools = sorted(set(TOOL_RE.findall(text)))
    match = VERSION_RE.search(text)
    return tools, match.group(1) if match else "unknown"


def render(tools: list[str], added: list[str], removed: list[str],
           version: str, commit: str) -> str:
    lines = [
        "# 上游能力映射（自动生成，勿手改）",
        "",
        f"- 上游：`kdcadmin/dsh_douyin_tools`（dsh-douyin-life `{version}`）",
        f"- 基线 commit：`{commit}`",
        f"- 识别到 {len(tools)} 个 `dy_*` 工具",
        "- 本插件是 Python / AstrBot Star 插件，**不与上游做代码 merge**；下表是能力对齐状态。",
        "",
        "| 上游工具 | 本插件 | 状态 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for tool in tools:
        ours, status, note = STATUS.get(tool, ("—", "need_review", "上游新增能力，待评估可移植性"))
        lines.append(f"| `{tool}` | {ours} | {status} | {note} |")
    lines += [
        "",
        "## 本次变更",
        "",
        f"- 新增工具：{', '.join('`' + t + '`' for t in added) or '（无）'}",
        f"- 移除工具：{', '.join('`' + t + '`' for t in removed) or '（无）'}",
        "",
        "## 移植原则",
        "",
        "1. 只读、纯 Python 可落地的能力优先移植；浏览器能力复用 vendored 内核（MIT），不重写选择器；",
        "2. 依赖浏览器 CDP / 页面 XHR 的能力在没有内核支持时标注 `not_ported`，不伪造实现；",
        "3. 账号写操作默认关闭，必须经 `ActionPolicy` 四重闸门（总闸 / 动作白名单 / 每日额度 / 收件人白名单）；",
        "4. 上游变更只触发「报告 + Issue」，**不自动修改插件代码**。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream-dir", type=Path)
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--write", action="store_true", help="落盘（默认 DRY_RUN）")
    ap.add_argument("--check", action="store_true", help="有变更时返回退出码 1")
    args = ap.parse_args()

    readme, spec, commit = read_upstream(args.upstream_dir, args.online)
    tools, version = parse(readme + "\n" + spec)
    tool_hash = hashlib.sha256("\n".join(tools).encode()).hexdigest()[:16]
    base = json.loads(BASE_FILE.read_text(encoding="utf-8")) if BASE_FILE.exists() else {}
    old_tools = base.get("tools", [])
    added = [t for t in tools if t not in old_tools]
    removed = [t for t in old_tools if t not in tools]
    changed = bool(added or removed or base.get("version") != version
                   or (commit != "online" and base.get("commit") != commit))

    print(f"upstream_commit={commit} version={version} tools={len(tools)} hash={tool_hash}")
    print(f"changed={changed} added={added} removed={removed}")
    report = render(tools, added, removed, version, commit)
    if not args.write:
        print("DRY_RUN：未写入任何文件。加 --write 落盘。")
        print(report[:1200])
    else:
        MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)
        MAPPING_FILE.write_text(report, encoding="utf-8")
        CHANGE_FILE.write_text(
            f"# 上游变更待移植\n\n"
            f"- commit: `{commit}`\n- version: `{version}`\n"
            f"- 新增: {', '.join(added) or '（无）'}\n"
            f"- 移除: {', '.join(removed) or '（无）'}\n\n"
            f"请审查 `docs/UPSTREAM_MAPPING.md`，按移植原则决定是否实现。\n",
            encoding="utf-8",
        )
        BASE_FILE.write_text(json.dumps({
            "repo": "kdcadmin/dsh_douyin_tools", "commit": commit,
            "version": version, "tool_hash": tool_hash, "tools": tools,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"written: {MAPPING_FILE.name} / {CHANGE_FILE.name} / {BASE_FILE.name}")
    if args.check and changed:
        sys.exit(1)


if __name__ == "__main__":
    main()
