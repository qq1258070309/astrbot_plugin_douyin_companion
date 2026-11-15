# astrbot_plugin_douyin_companion · 抖音陪伴 · 共同经历

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.27.2-blue.svg)](https://docs.astrbot.app/dev/star/plugin-new.html)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

把抖音带进 [AstrBot](https://github.com/AstrBotDevs/AstrBot)：热榜 / 搜索 / 视频详情 / 无水印直链 / 抽帧 / 视频理解，
以及**推荐流、评论、私信收件箱、点赞、发评论、转发、发私信、投稿**。
可通过官方稳定接口接入 [陪伴小屋](https://github.com/menglimi/astrbot_plugin_private_companion)，把「一起看过的视频」变成角色可召回的共同经历。

> 改编自 [`JianHeShui/dsh-douyin`](https://github.com/JianHeShui/dsh-douyin)（MIT）与
> [`kdcadmin/dsh_douyin_tools`](https://github.com/kdcadmin/dsh_douyin_tools)（`dsh-douyin-life`，MIT / TiAmoSS）。
> 0.3.0 起**逐字 vendor** 后者的 Playwright 浏览器内核（只剥离 MCP 脚手架），归属见 [NOTICE.md](NOTICE.md)。

---

## ✨ 能力矩阵

| 能力 | 状态 | 依赖 |
| --- | --- | --- |
| 热榜 / 关键词搜索 / 视频详情 / 热点视频 | ✅ 只读 | `dy-cli` + cookie |
| 无水印直链 / 下载 / ffmpeg 抽帧 / 视频理解证据链（可选 ASR） | ✅ 只读 | ffmpeg；ASR 需 faster-whisper |
| **推荐流**（首页 `/module/feed/` XHR + 滚动收集） | ✅ 只读 | 浏览器登录态 |
| **评论读取**（页面 XHR 优先，DOM 兜底） | ✅ 只读 | 浏览器登录态 |
| **私信收件箱**（只返回有消息 ID 的入站消息） | ✅ 只读 | 浏览器登录态 |
| **点赞 / 取消点赞** | ⚠️ 写，默认关 | 浏览器登录态 + 写闸门 |
| **发评论 / 回复评论**（支持真人 @ 联想面板） | ⚠️ 写，默认关 | 同上 |
| **转发视频给好友** | ⚠️ 写，默认关 | 同上 + 收件人白名单 |
| **发私信** | ⚠️ 写，默认关 | 同上 + 收件人白名单 |
| **投稿**（默认只存草稿） | ⚠️ 写，默认关 | 同上 + 投稿目录白名单 |
| 关注 / 养号 / 批量抓取 / 过验证码 | ❌ 不做 | — |
| 常驻自动回复 / 自动点赞轮询 | ❌ 不做 | 只提供按次调用 |
| 额外 HTTP 端口 / 把 cookie 写进日志 | ❌ 不做 | — |

> **写操作默认全部关闭，且默认演练（dry-run）**。这不是"忘记打开"，是设计。
> 详见下一节。

---

## 🔐 写操作安全模型

写操作会**以你本人的账号**留下公开痕迹，撤不回。所以判定集中在一层
（`douyin_core/action_policy.py`），任何一条不满足都直接拒绝，不做静默降级：

| 闸门 | 配置 | 默认 |
| --- | --- | --- |
| 1. 浏览器能力 | `enable_browser` | `false` |
| 2. 写总闸 | `writes_enabled` | `false` |
| 3. 动作白名单 | `write_actions`（`like,comment,share,dm,publish`） | 空 |
| 4. 每日额度 | `like_per_day` / `comment_per_day` / `share_per_day` / `dm_per_day` / `publish_per_day` | 全 `0` |
| 5. 收件人白名单 | `dm_allow_targets`（dm / share 生效） | 空（不限制） |
| 6. 投稿目录 | `publish_allow_dir` | 空（禁止投稿） |
| 7. 演练 | `write_dry_run` | `true` |
| 8. 模型确认 | 工具参数 `confirm=true` | 必须由用户同意 |

- **指令**由人直接输入 → 视为已确认；**模型工具**必须 `confirm=true`，否则只返回"将要做什么"。
- 额度按**自然日**计，只统计真正执行过的动作（dry-run 不记账）。
- 每次 live 操作写本地账本 `action_ledger`，`/抖音写权限` 可查今日用量。
- 没有 `force` 后门：上游内核允许模型用 `force` 越过闸门，本插件不提供这个参数。

推荐启用顺序：先 `write_dry_run=true` 演练几次 → 关演练、只开 `like` + 小额额度 →
确认行为符合预期后再逐项放开。

---

## 📦 安装

### 方式 A：Release zip（推荐）

1. 到 **Releases** 下载最新的 `astrbot_plugin_douyin_companion-v0.3.1.zip`。
2. **AstrBot WebUI → 插件 → 本地安装 / 上传插件**，选择该 zip。
3. 重载插件，发 `/抖音状态` 自检。

> zip 内已包含一层 `astrbot_plugin_douyin_companion/` 目录，与 `metadata.yaml` 的 `name` 一致。
> 也可手动解压到 `AstrBot/data/plugins/`。

### 方式 B：git clone

```bash
cd AstrBot/data/plugins
git clone https://github.com/qq1258070309/astrbot_plugin_douyin_companion.git
```

### 依赖

- **读路径**：`dy-cli==0.2.2`（推荐独立 venv，见下）。
- **浏览器能力**：`playwright>=1.45`。**不需要** `playwright install` —— 我们连的是你本机已装的 Chrome / Edge。

> 服务器 / 无显示器 / aarch64 / proot 容器？见 **[docs/SERVER.md](docs/SERVER.md)**：
> Xvfb + 系统 Chromium + CDP 的方案、`--no-sandbox` / `-nolock` / Windows UA 的原因，
> 以及用 noVNC 在网页里手动登录、重启后自启的做法。

---

## ⚙️ 首次配置

### 1. 读路径：准备 `dy-cli`

插件不实现 X-Bogus / a_bogus 签名，热榜 / 搜索 / 详情 / 主页都交给 `dy-cli`：

```bash
python -m venv /opt/douyin-venv
/opt/douyin-venv/bin/pip install dy-cli==0.2.2 aiohttp
```

WebUI「插件 → 抖音陪伴」把 `python_path` 填成 `/opt/douyin-venv/bin/python`。

### 2. 读路径 Cookie（可选，但搜索/详情多数需要）

服务器无头环境无法扫码。可在有显示器的电脑上用 Playwright 导出 `storage_state` JSON，放到：

```
AstrBot/data/plugin_data/astrbot_plugin_douyin_companion/home/.dy/cookies/default.json
```

也可把绝对路径填到 `cookie_file`。该文件权限设为仅当前用户可读，`.gitignore` 已排除。

### 3. 账号侧：打开浏览器并登录

1. 在 AstrBot 的 Python 环境安装 `playwright`（`pip install playwright>=1.45`）。
2. 配置里打开 `enable_browser`。
3. 保持 `browser_headless=false`（带界面）。**无头模式下抖音给的是精简评论区**：没有发送按钮、打 @ 不出联想面板，评论和私信会失败。
4. 发 `/抖音登录`，在弹出的浏览器窗口里扫码或短信登录（登录态存在插件数据目录 `browser_home/`）。
5. 发 `/抖音状态 登录` 复查：应显示"已登录 / cookie=True / 头像=True"。

> 想复用你日常用的 Chrome：关掉它，用 `--remote-debugging-port=9222` 启动，再把
> `browser_cdp_url` 填 `http://127.0.0.1:9222`。这样不需要重新登录。

### 4. 打开运行时开关

> ⚠️ **代码装好 ≠ 功能开启**。请在 WebUI 保存一次插件配置。

### 5. 自检

```
/抖音状态          后端 / 浏览器 / 写闸门摘要
/抖音状态 登录      真实探测登录态（会启动浏览器）
/抖音自检          依赖矩阵 + 配额 + 缓存占用
/抖音能力          对照上游 34 个工具的能力矩阵
```

---

## 🎯 指令

| 指令 | 说明 |
| --- | --- |
| `/抖音 <aweme_id 或分享链接>` | 视频详情；开启抽帧后附前 3 张关键帧 |
| `/抖音热榜 [条数]` | 热榜，默认 10 条 |
| `/抖音搜索 <关键词> [条数]` | 关键词搜索，默认 5 条 |
| `/抖音推荐 [条数]` | 刷首页推荐流（不搜索） |
| `/抖音评论 <链接> [条数]` | 读取评论 |
| `/抖音私信 [昵称] [条数]` | 读取私信收件箱（默认仅私聊可见） |
| `/抖音点赞 <链接> [取消]` | 点赞 / 取消点赞 |
| `/抖音发评论 <链接> <正文>` | 发评论；配 `comment_mention` 走真人 @ |
| `/抖音转发 <链接> <好友昵称>` | 转发视频给好友（需白名单） |
| `/抖音发私信 <好友昵称> <正文>` | 发私信（需白名单；默认仅私聊） |
| `/抖音投稿 <绝对路径> [标题] [--publish]` | 默认只存草稿；`--publish` 才发布 |
| `/抖音登录` | 打开浏览器登录页（扫码） |
| `/抖音理解 <链接> [帧数]` | 视频理解证据链（抽帧 + 可选 ASR + 证据 JSON） |
| `/抖音记忆 <内容> [标签]` | 显式记一条共同经历；不带参数查看最近 5 条 |
| `/抖音状态 [登录]` | 后端 / 浏览器 / 配额 / 陪伴小屋接入 |
| `/抖音写权限` | 写闸门矩阵与今日用量 |
| `/抖音自检` / `/抖音能力` / `/抖音清理 [confirm]` | 依赖矩阵 / 上游能力矩阵 / 有界清理（默认 DRY_RUN） |

### 模型可调用工具（`enable_llm_tools`，共 16 个）

| 只读 | 写（需 `confirm=true`） |
| --- | --- |
| `douyin_hot` `douyin_search` `douyin_video` | `douyin_like` |
| `douyin_frames` `douyin_understand` `douyin_replay` | `douyin_comment` |
| `douyin_hot_videos` `douyin_remember` | `douyin_share` |
| `douyin_feed` `douyin_comments` `douyin_inbox` | `douyin_dm` `douyin_publish` |

---

## 🤝 与陪伴小屋结合（可选）

**陪伴小屋没装也能用**：所有指令与工具在独立模式下正常工作。

装了陪伴小屋时，本插件按它公开发布的 `docs/extension-development.md` / `docs/external-memory-provider.md`，通过 `companion.external-memory.v1` 注册外部记忆 Provider：

```python
context.get_all_stars()
  └─ root_dir_name / name == "astrbot_plugin_private_companion" 且 activated
      └─ extension_api.memory_provider_api_version == "companion.external-memory.v1"
          └─ register_memory_provider(
                 "douyin_companion.shared_videos",
                 read / write / summary / is_active,
                 handles_model_memory=False, priority=0, timeout_seconds=5.0)
```

- `read`：只返回 `request["context"]` 完整身份下、预算内的抖音共同经历；群聊不会被宿主调用。
- `write`：接收日记/梦境/主动消息副本，以 `source_plugin + memory_id` 幂等归档。
- `summary`：返回固定六类计数，不含正文。
- 生命周期：`initialize()` + `on_astrbot_loaded()` 双重发现，回调里 120 s 节流重发现（应对热重载），`terminate()` 主动注销。

**旁路归档**：`on_llm_request(priority=-900000)` 只观察消息里的抖音链接并写入本插件自己的 SQLite；它**不修改 `req`、不注入提示词、不 `stop_event()`**，因此不会与陪伴小屋的主注入点冲突，也不会击穿它的 Prompt Cache。

**私信存储**：`/抖音私信` 读到的入站消息按会话身份写入本插件的 `dm_messages`（幂等、拒绝 DOM 伪造 ID），
**只在本插件内可查，不进入陪伴小屋的召回**——私信的身份键（会话/群/发送者哈希）与陪伴小屋的
`context` 身份键不是同一套，混用会把「聊天对象」误当成「共同经历」。私信默认只在私聊展示（`dm_private_only=true`）。

**这个联合是单向的**：陪伴小屋**来问**，本插件回答；本插件不会往陪伴小屋的数据库或提示词里写东西。
`write` 回调只把陪伴小屋推来的日记/梦境副本归档留底，不参与任何召回。

> 本插件不修改陪伴小屋源码、不写它的数据库、不注册它的页面路由。

---

## 🔄 上游同步与自动更新

完整机制见 [UPSTREAM_SYNC.md](UPSTREAM_SYNC.md)。

| 能力 | 落地 |
| --- | --- |
| **同步** | `.github/workflows/upstream-watch.yml` 每 6h 检查 `kdcadmin/dsh_douyin_tools` 的 main HEAD + `dy_*` 工具清单；变更 → 刷新 `docs/UPSTREAM_MAPPING.md` + 开 Issue 提醒审查。**不自动改插件代码** |
| **浏览器内核同步** | `python tools/vendor_browser_driver.py --upstream <repo> --check` 比对 vendored 内核与上游是否一致；CI 会跑 |
| **更新** | `.github/workflows/release.yml`：打 `v*` tag 自动跑编译 + 单测 → 构建 zip + sha256 → 发 Release |
| **AstrBot 检查更新** | `metadata.yaml` 的 `repo` 指向本仓库；升级只需改版本并打 tag |

本地手动跑哨兵（默认 DRY_RUN，不写文件）：

```bash
git clone --depth 1 https://github.com/kdcadmin/dsh_douyin_tools.git /tmp/upstream
python tools/upstream_diff.py --upstream-dir /tmp/upstream          # 只看
python tools/upstream_diff.py --upstream-dir /tmp/upstream --write  # 落盘报告
python tools/vendor_browser_driver.py --upstream /tmp/upstream --check
```

> 上游仓库没有 tag/release，所以哨兵按 **commit SHA + 工具清单哈希** 判变更。

## 🔧 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `enable_llm_tools` | `true` | 注册 16 个 LLM 工具 |
| `enable_memory_provider` | `true` | 接入陪伴小屋记忆 |
| `auto_capture_links` | `true` | 旁路链接归档 |
| `enable_frames` | `false` | 下载 + 抽帧（需 ffmpeg） |
| `enable_asr` / `asr_model` | `false` / `base` | 视频理解时本地转写（需 faster-whisper） |
| `enable_browser` | `false` | 账号侧能力总开关（需 playwright） |
| `browser_headless` | `false` | **评论/私信必须 false（带界面）** |
| `browser_channel` / `browser_chrome_path` | `auto` / `""` | 浏览器选择 |
| `browser_cdp_url` | `""` | 连接已登录 Chrome（如 `http://127.0.0.1:9222`） |
| `browser_profile_port` | `9333` | 持久 profile 调试端口（多实例共享） |
| `browser_timeout` | `90` | 单次操作基础超时（秒） |
| `feed_max_scrolls` | `6` | 推荐流最多滚几屏 |
| `dm_private_only` | `true` | 私信只在私聊可用 |
| `comment_mention` | `""` | 发评论时真人 @ 的昵称 |
| `publish_tags` | `""` | 投稿追加话题 |
| `publish_allow_dir` | `""` | 允许投稿的目录；空 = 禁止 |
| `write_dry_run` | `true` | 写操作演练 |
| `write_actions` | `""` | 允许的写动作，逗号分隔 |
| `like/comment/share/dm/publish_per_day` | `0` | 每日额度 |
| `dm_allow_targets` | `""` | 私信/转发收件人白名单 |
| `writes_enabled` | `false` | 写总闸 |
| `python_path` | `""` | 装了 dy-cli 的解释器 |
| `cookie_file` | `""` | storage_state 路径（读路径） |
| `ffmpeg_path` / `ffprobe_path` | `ffmpeg` / `ffprobe` | 抽帧工具 |
| `max_results` / `max_search_pages` | `20` / `1` | 硬上限 20 / 3 |
| `hourly_request_budget` | `60` | 滚动 1 小时预算（读路径） |
| `call_delay_min/max_ms` | `1800` / `3200` | 调用高斯抖动 |
| `page_delay_min/max_ms` | `3000` / `8000` | 翻页高斯抖动 |
| `media_cache_mb` | `800` | 成片/帧缓存上限 |
| `log_level` | `info` | 结构化日志等级 |

---

## 🧱 项目结构

```
main.py                        唯一 Star：19 指令 + 16 LLM 工具 + 2 钩子 + 集成
douyin_core/                   平台无关领域层（不 import AstrBot SDK）
  errors.py                    7 个稳定错误码 + 文本映射
  redact.py                    凭证脱敏 / ANSI 清理
  normalize.py                 「缺失即 null」归一化 + id 提取
  gate.py                      单 worker 配额门 + 写操作三道闸
  backend.py                   dy-cli 异步子进程 / JSON 信封解析 / 热点视频
  media.py                     无水印直链 / 下载 / 抽帧
  store.py                     SQLite：身份隔离 + 记忆 + 私信 + 写操作账本
  understanding.py             视频理解证据链（抽帧 + ASR + 证据 JSON + 回放）
  maintenance.py               自检 / 能力矩阵 / 有界 dry-run 清理
  action_policy.py             写操作策略（四重闸门 + 每日额度 + 目录/收件人白名单）
  browser_service.py           Playwright 会话适配层（配置注入 / 统一错误 / 脱敏）
  browser_driver.py            vendored 上游浏览器内核（4700+ 行，只剥 MCP 脚手架）
tools/dy_query_adapter.py      上游 MIT 的 --offset / search_id 分页补丁
tools/upstream_diff.py         上游能力哨兵（默认 DRY_RUN）
tools/vendor_browser_driver.py vendored 内核生成/比对（--check 供 CI 用）
.github/workflows/             release.yml（自动发版）+ upstream-watch.yml（上游哨兵）
docs/UPSTREAM_MAPPING.md       自动生成的上游能力映射表
UPSTREAM_BASE                  上游基线（commit / version / 工具哈希）
UPSTREAM_SYNC.md               同步 / 更新 / 移植说明
tests/test_offline.py          离线单测（43 个，不联网、不发消息）
metadata.yaml / _conf_schema.json / requirements.txt
```

> Release 的 zip 由 `.github/scripts/build_release.py` 按白名单打包，只含插件运行时文件，
> 解压即为一层完整的插件目录。改编来源与许可归属见 `NOTICE.md` / `LICENSE-UPSTREAM`。

---

## 🧪 开发验证

```bash
python -m compileall -q .
python -m unittest discover -s tests -p "test_*.py" -v
python tools/vendor_browser_driver.py --upstream /tmp/upstream --check
```

离线单测覆盖：错误码、脱敏、归一化、JSON 信封、配额与退避、身份隔离、幂等写入、
写策略矩阵（未知动作/白名单/额度/演练/确认/收件人/投稿目录）、私信去重、
vendored 内核可导入且无 MCP 脚手架。全部不联网、不发消息、不启动浏览器。

---

## ⚠️ 风险与合规

- 使用**非官方接口与页面自动化**，不是抖音开放平台；接口与 DOM 随时可能变，项目不保证可用性。
- **写操作不可撤回**：评论/私信公开可见，转发会留下会话记录，投稿可能被限流或下架。
  请只操作你自己的账号，并对内容负责。
- 抖音有风控：高频操作、机房 IP、多实例共用同一 profile 会触发限流/验证码，极端情况账号受限。
  遇到 `challenge_required` 请**人工在有头浏览器完成验证**，不要写脚本硬刷。
- 浏览器内核在操作后会二次取证（`data-e2e-state` 翻转 / 评论回执 / 新增出站气泡）。
  **取不到证据时返回 `status=unknown`，不会自动重发**，也不会谎报成功。
- `detail` 逆向接口不稳定时，会明确返回可解释错误，不伪造数据；抽帧需 ffmpeg 与有效媒体直链。
- 仅供**个人学习与研究**。禁止垃圾营销、骚扰、刷量、养号、多账号矩阵，或任何违反抖音用户协议与当地法律的行为。
- 因使用本插件产生的任何后果，作者不承担责任。

---

## 📄 许可与致谢

- **MIT**，完整文本见 [LICENSE](LICENSE)，上游原文见 [LICENSE-UPSTREAM](LICENSE-UPSTREAM)。
- 逐行移植、vendored 与设计借鉴清单见 [NOTICE.md](NOTICE.md)。
  上游项目：[`JianHeShui/dsh-douyin`](https://github.com/JianHeShui/dsh-douyin)、
  [`SakuraMat0u/dsh-douyin-toolkit`](https://github.com/SakuraMat0u/dsh-douyin-toolkit)、
  [`kdcadmin/dsh_douyin_tools`](https://github.com/kdcadmin/dsh_douyin_tools)。
- 集成契约来自 [`menglimi/astrbot_plugin_private_companion`](https://github.com/menglimi/astrbot_plugin_private_companion)（AGPL-3.0-only）公开发布的扩展开发文档；本插件**未复制其源码**。
- AstrBot 插件规范来自 [官方文档](https://docs.astrbot.app/dev/star/plugin-new.html) 与 [`Soulter/helloworld`](https://github.com/Soulter/helloworld) 模板。
