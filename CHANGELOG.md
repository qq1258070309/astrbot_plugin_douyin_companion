# Changelog

本文件记录 `astrbot_plugin_douyin_companion` 的版本变化。版本号遵循语义化版本规范。

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
