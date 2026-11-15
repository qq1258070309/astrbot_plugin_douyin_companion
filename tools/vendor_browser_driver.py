#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把上游 ``kdcadmin/dsh_douyin_tools`` 的浏览器内核 vendor 成本插件模块。

为什么 vendor 而不是重写
------------------------
上游 ``vendor/douyin-mcp/mcp_server.py``（MIT, Copyright (c) 2026 TiAmoSS）是**在真实账号上
逐条实测**出来的：抖音的 DOM class 是哈希的，选择器、@ 联想面板尺寸、评论回执校验、
私信气泡方向判定这些细节无法凭推测复现。重写等于把已经踩过的坑再踩一遍。

本脚本只做三件**机械**改动，不碰任何业务逻辑：

1. 去掉 MCP 脚手架（``from mcp.server.fastmcp import FastMCP``、``mcp = FastMCP(...)``、
   ``@restricted_tool()``、``main()``）——AstrBot 不是 MCP 宿主，装 ``mcp`` 包纯属浪费。
2. 顶部插入来源与许可声明。
3. 其余函数体、选择器、注释一字不改，便于用 ``git diff`` 对照上游。

用法::

    python tools/vendor_browser_driver.py --upstream /path/to/dsh_douyin_tools
    python tools/vendor_browser_driver.py --upstream ... --check   # 只比对是否已同步

``--check`` 在内容不一致时返回 1，供 CI 的上游哨兵使用。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "douyin_core" / "browser_driver.py"
RELATIVE_UPSTREAM = Path("vendor") / "douyin-mcp" / "mcp_server.py"

HEADER = '''# -*- coding: utf-8 -*-
# 抖音浏览器内核（vendored，勿手改）。
#
# 来源：https://github.com/kdcadmin/dsh_douyin_tools
#       vendor/douyin-mcp/mcp_server.py
# 许可：MIT License, Copyright (c) 2026 TiAmoSS（全文见 LICENSE-UPSTREAM）
#
# 本地改动**只有**脚手架剥离，业务逻辑与选择器保持上游原样：
#   * 删除 `from mcp.server.fastmcp import FastMCP` / `mcp = FastMCP(APP_NAME)`
#   * 删除 `@restricted_tool()` 装饰器与 `main()` 入口
#   * 顶部加这段声明
# 因此可以用 git diff 对照上游确认没有偷偷改逻辑。
# 重新生成：python tools/vendor_browser_driver.py --upstream <repo>
#
# 上游文件 sha256: {sha}
# ---------------------------------------------------------------------------

'''

STUB_NOTE = '''# --- vendored: MCP 注册层已移除（策略改由 AstrBot 侧 ActionPolicy 执行）---


'''


def transform(source: str) -> str:
    text = source

    # 1) MCP 导入与实例
    text = text.replace("from mcp.server.fastmcp import FastMCP\n", "")
    text = text.replace("mcp = FastMCP(APP_NAME)\n", "")

    # 2) restricted_tool 整块（含其装饰的 mcp.tool() 调用）→ 空
    start = text.find("def restricted_tool():")
    if start < 0:
        raise SystemExit("上游结构变了：找不到 restricted_tool()")
    first_use = text.find("@restricted_tool()", start)
    if first_use < 0:
        raise SystemExit("上游结构变了：找不到 @restricted_tool() 使用点")
    text = text[:start] + STUB_NOTE + text[first_use:]

    # 3) 其余装饰器
    text = text.replace("@restricted_tool()\n", "")

    # 4) CLI 入口
    main_at = text.find("def main() -> int:")
    if main_at < 0:
        raise SystemExit("上游结构变了：找不到 main()")
    text = text[:main_at].rstrip() + "\n"

    # 只检查**可执行符号**；注释与 docstring 里提 MCP 是历史说明，不必删
    for symbol in ("from mcp.server", "FastMCP(", "mcp.tool(", "mcp.run("):
        if symbol in text:
            raise SystemExit(f"剥离不干净：仍残留 {symbol}")

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return HEADER.format(sha=digest) + text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="dsh_douyin_tools 仓库根目录或 mcp_server.py")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件；不一致返回 1")
    args = parser.parse_args()

    upstream = Path(args.upstream)
    if upstream.is_dir():
        upstream = upstream / RELATIVE_UPSTREAM
    if not upstream.is_file():
        print(f"找不到上游文件：{upstream}", file=sys.stderr)
        return 2

    source = upstream.read_text(encoding="utf-8")
    rendered = transform(source)

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current == rendered:
            print("browser_driver.py 与上游一致")
            return 0
        print("browser_driver.py 落后于上游，需要重新 vendor", file=sys.stderr)
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # 固定 LF：vendored 文件必须与 CI checkout 后一致，避免 Windows 上生成 CRLF
    with TARGET.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    print(f"已生成 {TARGET}（{len(rendered.splitlines())} 行，"
          f"sha256={hashlib.sha256(rendered.encode('utf-8')).hexdigest()[:16]}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
