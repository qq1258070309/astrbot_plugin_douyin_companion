# NOTICE — 归属与许可声明

本插件 `astrbot_plugin_douyin_companion` 是研究与改编产物，**移植了以下开源项目的设计原理与部分代码**。按 AstrBot 官方开发原则第 8、9 条，在此明确说明灵感来源并保留原版权与许可声明。

## 1. dsh-douyin（MIT）

- 项目：<https://github.com/JianHeShui/dsh-douyin>，v1.1.0
- 上游：<https://github.com/SakuraMat0u/dsh-douyin-toolkit>
- 许可：**MIT License**，完整文本（含两位作者版权声明）随包保留于 `LICENSE-UPSTREAM`：
  - `Copyright (c) 2026 SakuraMat0u (original work: dsh-douyin-toolkit)`
  - `Copyright (c) 2026 JianHeShui (modifications and additions)`

**逐行移植的代码**：

| 本插件文件 | 来源文件 | 说明 |
| --- | --- | --- |
| `tools/dy_query_adapter.py` | `python/dy_query.py` | 几乎逐行移植，仅在文件头补充中文归属说明；保留 `--offset` 与 `search_id` 链式翻页逻辑 |

**移植的设计原理（重新以 Python/AstrBot 实现，未复制 JS 代码）**：

| 本插件文件 | 来源 | 保留的原理 |
| --- | --- | --- |
| `douyin_core/errors.py` | `lib/backend.js` | 7 个稳定错误码 + 文本正则映射 + 未知码降级 |
| `douyin_core/redact.py` | `lib/backend.js` | cookie / sessionid / passport_* / storage_state 脱敏与 ANSI 清理 |
| `douyin_core/normalize.py` | `lib/normalize.js` | 「缺失即 null」、字段映射、aweme_id / sec_uid 提取、jsonSafe |
| `douyin_core/gate.py` | `lib/backend.js` + README | 单 worker 串行门、滚动小时配额、高斯抖动、指数退避、写操作三道闸 |
| `douyin_core/backend.py` | `lib/backend.js` | 有界子进程输出、超时/cancel、JSON 信封提取、结构化日志、状态自检 |
| `douyin_core/media.py` | `python/douyin_http.py` | 分享页 `_ROUTER_DATA` → `play_addr` → `playwm→play` 无水印直链、成片缓存裁剪、ffmpeg 均匀抽帧 |

**明确未移植的部分**：Cordis `apply/ctx.effect/ctx.tools.register` 外壳、`cordis.patch.yml`、FastAPI `:7709` 本地服务、Playwright 私信/群聊驱动、登录状态机、评论与发布工具。这些与 AstrBot 场景不匹配，或属于不可逆写操作，已按 `04-改编方案与集成说明.md` 显式裁掉。

## 2. 陪伴小屋（AGPL-3.0-only）— 仅接口引用

- 项目：<https://github.com/menglimi/astrbot_plugin_private_companion>
- 本插件**未复制、未修改其任何源码**，只按它公开发布的
  `docs/extension-development.md` 与 `docs/external-memory-provider.md` 调用
  `extension_api.register_memory_provider()`（`companion.external-memory.v1`）。
- 陪伴小屋缺席、停用或接口版本不匹配时，本插件自动降级为独立模式。
- 若将来要分发本插件，请自行评估 AGPL-3.0 与通过文档化 API 交互的许可边界；本仓库不提供法律意见。

## 3. AstrBot 与模板

- 官方文档：<https://docs.astrbot.app/dev/star/plugin-new.html>（及 `dev/star/guides/*.html`）
- 模板仓库：<https://github.com/Soulter/helloworld>
- 本插件遵循其开发原则：不用 `requests`、持久化数据放 `data/plugin_data/`、良好注释与错误处理、提交前格式化。

## 4. 本改编新增部分的许可

除上表所列移植内容外，本插件新写的 `main.py`、`douyin_core/store.py`、`_conf_schema.json`、测试与文档，按 **MIT License** 提供。完整许可文本（含上游与本改编的版权声明）见仓库根目录 `LICENSE`；上游原始文本另存于 `LICENSE-UPSTREAM`。
