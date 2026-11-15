# Changelog

本文件记录 `astrbot_plugin_douyin_companion` 的版本变化。版本号遵循语义化版本规范。

## 0.3.0

接上**账号侧能力**：推荐流、评论、私信收件箱、点赞、发评论、转发、发私信、投稿。
浏览器层 **vendored** 上游 `kdcadmin/dsh_douyin_tools` 的 Playwright 内核
（MIT / TiAmoSS），只剥离 MCP 脚手架，选择器与取证逻辑一字未改。

### 新增：浏览器内核与适配层

- `douyin_core/browser_driver.py`：vendored 内核（4700+ 行）。
  用 `tools/vendor_browser_driver.py --check` 可与上游比对，防手改。
- `douyin_core/browser_service.py`：配置注入 → 会话生命周期 → 统一返回形状 → 脱敏。
  写不写由策略层决定，适配层不判断。
- `douyin_core/action_policy.py`：写操作策略，四重闸门 + 每日额度 + 收件人白名单 +
  投稿目录白名单 + 默认演练 + 模型必须 `confirm=true`。**取消上游的 `force` 后门**。
- `DouyinStore` 新增 `dm_messages` / `action_ledger` 两张表：
  私信幂等落库（拒绝 DOM 伪造 ID）、写操作记账（只统计 live）。

### 新增：能力（8 个 LLM 工具 / 9 条指令）

| 能力 | 指令 | 工具 |
| --- | --- | --- |
| 推荐流（`/module/feed/` XHR + 滚动） | `/抖音推荐` | `douyin_feed` |
| 评论读取（XHR 优先，DOM 兜底） | `/抖音评论` | `douyin_comments` |
| 私信收件箱（只返回有 ID 的入站消息） | `/抖音私信` | `douyin_inbox` |
| 点赞 / 取消 | `/抖音点赞` | `douyin_like` |
| 发评论（真人 @ 联想面板） | `/抖音发评论` | `douyin_comment` |
| 转发给好友 | `/抖音转发` | `douyin_share` |
| 发私信 | `/抖音发私信` | `douyin_dm` |
| 投稿（默认草稿 + AI 声明） | `/抖音投稿` | `douyin_publish` |
| 打开登录页 | `/抖音登录` | — |

- 写操作会在操作后二次取证（`data-e2e-state` 翻转 / `/comment/publish` 回执 /
  新增出站气泡 / 草稿或作品 ID）；**取不到证据返回 `status=unknown`，不自动重发、不谎报成功**。
- 私信读取默认只在私聊可用（`dm_private_only`），避免群聊泄露。

### 变更

- `metadata.yaml` 版本 `0.3.0`，描述与帮助改为 16 工具 / 19 指令。
- `requirements.txt` 增加 `playwright>=1.45`（只装包，不下载浏览器，驱动本机 Chrome/Edge）。
- `writes_enabled` 语义变化：不再是"保留字段"，而是真实总闸；新增 20 个配置项。
- `allow_send_targets` 标记弃用，改用 `dm_allow_targets`（旧值仍兼容）。
- `tools/upstream_diff.py` 的 34 项映射更新：8 项从 `not_ported` 变为 `implemented`（带闸门说明）。
- `docs/UPSTREAM_MAPPING.md` 重新生成。
- 单测从 26 个增加到 43 个（写策略矩阵 / 私信去重 / vendored 内核完整性）。

### 安全

- 写操作**默认全关**：`enable_browser=false`、`writes_enabled=false`、`write_actions` 空、
  各额度为 0、`write_dry_run=true`。要真跑需逐项显式打开。
- 所有 live 操作落本地账本，`/抖音写权限` 可查今日用量。
- 未新增任何外部端口；登录态只存在插件数据目录 `browser_home/`。

## 0.2.0

以 [`kdcadmin/dsh_douyin_tools`](https://github.com/kdcadmin/dsh_douyin_tools)
（`dsh-douyin-life` v0.2.9，MIT）为**能力参考上游**，新增同步/更新管线并增强只读能力。

### 新增：能力（8 个 LLM 工具 / 10 条指令）

- `douyin_hot_videos`：热点视频（**按热词搜索实现，返回值明确标注非官方接口**）。
- `douyin_understand`：视频理解证据链 —— 下载 → ffmpeg 均匀抽帧 → 可选
  `faster-whisper` 本地 ASR → 结构化证据 JSON 落盘 → `douyin_replay` 可回放。
  借鉴上游 `video-understanding-lab` 的 doctor/prepare/asr/record/replay 五阶段，
  依赖缺失时如实返回 `skipped` + notes，**不编数据**。
- `douyin_remember`：显式记忆写入，落到本插件身份隔离的 SQLite，并参与陪伴小屋
  `read` 自动召回（新增 `DouyinStore.search_all()`）。
- `douyin_selfcheck` / `/抖音自检` / `/抖音能力`：真跑依赖矩阵（ffmpeg / ffprobe /
  aiohttp / faster_whisper / cookie / 数据目录）+ 配额 + 缓存占用 + 能力矩阵。
- `/抖音清理`：有界（≤200 文件）+ **默认 DRY_RUN**，传 `confirm` 才真正删除。
- 新增领域模块 `douyin_core/understanding.py`、`douyin_core/maintenance.py`。

### 新增：同步与自动更新

- `.github/workflows/upstream-watch.yml`：每 6h 检查上游 main HEAD + `dy_*` 工具清单；
  变更时刷新 `docs/UPSTREAM_MAPPING.md`、更新 `UPSTREAM_BASE`、开 Issue 提醒审查。
  **不自动改插件代码**（Node/Cordis ≠ Python/AstrBot Star）。
- `tools/upstream_diff.py`：默认 DRY_RUN，`--check` 有变更返回 1，`--write` 才落盘，
  支持 `--online` 直接抓 raw 文件。
- `.github/workflows/release.yml` 改为 **`v*` tag 触发 + 手动 dispatch**，自动跑
  `compileall` + `unittest` → 白名单构建 zip + sha256 → 发布 Release；
  版本从 `metadata.yaml` 读取。AstrBot 据此检查插件更新。
- `UPSTREAM_BASE` / `docs/UPSTREAM_MAPPING.md` / `docs/UPSTREAM_CHANGE.md` / `UPSTREAM_SYNC.md`。

### 配置

新增 `enable_asr`、`asr_model`、`upstream_repo`；`_conf_schema.json` 现 23 项。

### 明确未移植

`dy_feed` 推荐流、`dy_comments` 评论、`dy_my_posts`、`dy_dm_inbox` 依赖浏览器 CDP/页面 XHR；
`dy_like` / `dy_comment` / `dy_share` / `dy_chat_*` / `dy_publish` / `dy_reply*` 属账号写操作
（上游也默认全关），本插件 `WriteGate` 保留判定器但**随包写工具仍为 0**。逐项理由见能力映射表。

## 0.1.0

首个公开版本。把 `JianHeShui/dsh-douyin`（MIT）的只读抖音能力改编为 AstrBot `Star` 插件。

### 新增

- **AstrBot 插件骨架**：`metadata.yaml`、`_conf_schema.json`（20 个可视化配置项）、`requirements.txt`、纯 `Star` 子类 `main.py`。
- **5 条指令**：`/抖音`、`/抖音热榜`、`/抖音搜索`、`/抖音状态`、`/抖音写权限`。
- **4 个 LLM 工具**：`douyin_hot`、`douyin_search`、`douyin_video`、`douyin_frames`，供 AstrBot 核心回复管线按需调用。
- **领域层 `douyin_core/`**（不 import AstrBot SDK）：
  - 7 个稳定错误码与文本映射、未知码降级；
  - cookie / sessionid / passport_* / storage_state 全链路脱敏；
  - 「缺失即 null」归一化、aweme_id / sec_uid 提取；
  - 单 worker 串行门、滚动 1 小时配额、高斯抖动、触发风控后 1/2/4/8/15 分钟指数退避；
  - `dy-cli` 异步子进程封装、ANSI 混杂输出中的 JSON 信封提取、结构化日志与状态自检；
  - 移动端分享页 `_ROUTER_DATA` → `play_addr` → `playwm→play` 无水印直链、成片缓存裁剪、ffmpeg 均匀抽帧；
  - 按完整身份隔离的 SQLite 存储。
- **陪伴小屋集成**：按 `companion.external-memory.v1` 注册外部记忆 Provider（`read` / `write` / `summary` / `is_active`），带双重发现、120 s 节流重发现与 `terminate()` 注销；未安装陪伴小屋时自动降级为独立模式。
- **旁路链接归档**：`on_llm_request(priority=-900000)` 只观察、不改 `req`、不注入提示词、不 `stop_event()`。
- **安全**：随包写工具 0 个；`writes_enabled` 默认 `false`；`WriteGate` 三道闸（显式确认 / 服务端总闸 / 目标白名单）作为将来扩展的安全原语。
- **20 个离线单测**：不联网、不发消息、不触碰用户数据。
- **许可与归属**：`LICENSE`（MIT）、`LICENSE-UPSTREAM`（上游原文）、`NOTICE.md`（逐行移植清单）。

### 明确未实现

- 不代发抖音私信 / 评论 / 发布，不点赞关注，不做任何账号写操作。
- 不移植 Cordis 外壳、FastAPI `:7709` 本地服务、Playwright 登录状态机与 IM 驱动。
- 不实现浏览器签名 XHR 回退：逆向 `detail` 接口不可用时返回可解释错误，不伪造数据。

### 已知限制

- 未做真机联调（需要 AstrBot 实例与有效 `storage_state`）；已验证编译、20 个离线单测、配置 Schema、`@filter.llm_tool` 参数 schema 与本机 AstrBot 4.29.4 的 API 符号。
- 短链展开依赖网络；复杂分享口令文本可能解析不到 aweme_id。
- 抽帧默认关闭，需 ffmpeg / ffprobe 与磁盘预算。
- 陪伴小屋热重载后，记忆 Provider 最长 120 s 内完成重连（指令与工具不受影响）。
