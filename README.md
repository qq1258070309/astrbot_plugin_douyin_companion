# astrbot_plugin_douyin_companion · 抖音陪伴 · 共同经历

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.27.2-blue.svg)](https://docs.astrbot.app/dev/star/plugin-new.html)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

把抖音的**只读**能力（热榜 / 搜索 / 视频详情 / 无水印直链 / 抽帧）带进 [AstrBot](https://github.com/AstrBotDevs/AstrBot)，并可通过官方稳定接口接入 [陪伴小屋](https://github.com/menglimi/astrbot_plugin_private_companion)，把「一起看过的视频」变成角色可召回的共同经历。

> 本插件改编自 [`JianHeShui/dsh-douyin`](https://github.com/JianHeShui/dsh-douyin)（DeepSeek Harness / Cordis 插件，MIT）。归属与移植清单见 [NOTICE.md](NOTICE.md)。

---

## ✨ 能力边界

| ✅ 提供 | ❌ 不提供 |
| --- | --- |
| 抖音热榜（无需登录） | 发私信、发评论、发布视频 |
| 关键词搜索（`search_id` 链式翻页） | 点赞、关注、养号、批量抓取 |
| 视频详情（文案 / 作者 / 互动数据） | 自动过验证码、打码平台、伪造设备指纹 |
| 短链展开 → 无水印直链 → 下载 → ffmpeg 抽帧 | 任何形式的账号写操作 |
| 单 worker 限频 / 小时配额 / 高斯抖动 / 指数退避 | 一个额外的 HTTP 端口 |
| 凭证全链路脱敏、缺失字段返回 `null` | 把 cookie 写进日志或消息 |

**随包写工具数量：0**。`writes_enabled` 默认 `false`。

---

## 📦 安装（三种方式）

### 方式 A：从 Release 下载 zip（推荐，适合 AstrBot「本地添加插件」）

1. 到本仓库 **Releases** 页下载 `astrbot_plugin_douyin_companion-v0.1.0.zip`。
2. **AstrBot WebUI → 插件 → 本地安装 / 上传插件**，选择该 zip。
3. 安装后在插件列表点「重载插件」，发 `/抖音状态` 自检。

> zip 内已包含一层 `astrbot_plugin_douyin_companion/` 目录，解压后目录名与 `metadata.yaml` 的 `name` 一致。
> 若 WebUI 上传不适配，也可手动解压：把解压出的目录整个放进 `AstrBot/data/plugins/`，重启或重载 AstrBot。

### 方式 B：git clone

```bash
cd AstrBot/data/plugins
git clone https://github.com/qq1258070309/astrbot_plugin_douyin_companion.git
```

### 方式 C：只下载源码压缩包

Release 页的 `Source code (zip)` 亦可，但推荐上面的插件 zip（已排除 `.git` / `__pycache__` / 测试开发产物）。

---

## ⚙️ 首次配置

### 1. 准备 `dy-cli` 环境（读路径的实际实现方）

插件不实现 X-Bogus / a_bogus 签名，热榜 / 搜索 / 详情 / 主页都交给 `dy-cli`。**推荐独立 venv**，避免污染 AstrBot 宿主环境：

```bash
python -m venv /opt/douyin-venv
/opt/douyin-venv/bin/pip install dy-cli==0.2.2 aiohttp
```

然后在 WebUI「插件 → 抖音陪伴」把 `python_path` 填成 `/opt/douyin-venv/bin/python`。
若留空（使用 AstrBot 自带解释器），需在该解释器里安装 `dy-cli==0.2.2`（见 `requirements.txt` 注释）。

### 2. 导出登录 Cookie（可选，但搜索/详情多数需要）

服务器无头环境**无法扫码**。请在有显示器的电脑上用上游 `tools/export_login_state.py`（或等价的 Playwright 脚本）导出 `storage_state` JSON，放到：

```
AstrBot/data/plugin_data/astrbot_plugin_douyin_companion/home/.dy/cookies/default.json
```

也可把绝对路径填到 `cookie_file`。**请把该文件权限设为仅当前用户可读**，它不会、也不应进入版本库（`.gitignore` 已排除）。

### 3. 打开运行时开关

> ⚠️ 代码装好 ≠ 功能开启。请在 WebUI 保存一次插件配置，确认 `enabled` 等开关生效。

### 4. 自检

发 `/抖音状态`，应看到 dy-cli 就绪、cookie 已配置、陪伴小屋记忆接入状态。

---

## 🎯 指令

| 指令 | 说明 |
| --- | --- |
| `/抖音 <aweme_id 或分享链接>` | 视频详情；开启抽帧后附前 3 张关键帧 |
| `/抖音热榜 [条数]` | 热榜，默认 10 条 |
| `/抖音搜索 <关键词> [条数]` | 关键词搜索，默认 5 条 |
| `/抖音状态` | 后端 / 登录态 / 配额 / 陪伴小屋接入 / 写能力自检 |
| `/抖音写权限` | 查看写操作三道闸状态（只读展示） |

启用 `enable_llm_tools` 后，主模型可调用 4 个工具：
`douyin_hot` · `douyin_search` · `douyin_video` · `douyin_frames`。

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

> 本插件不修改陪伴小屋源码、不写它的数据库、不注册它的页面路由。

---

## 🔧 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `enable_llm_tools` | `true` | 注册 4 个 LLM 工具 |
| `enable_memory_provider` | `true` | 接入陪伴小屋记忆 |
| `auto_capture_links` | `true` | 旁路链接归档 |
| `enable_frames` | `false` | 下载 + 抽帧（需 ffmpeg） |
| `python_path` | `""` | 装了 dy-cli 的解释器 |
| `cookie_file` | `""` | storage_state 路径 |
| `ffmpeg_path` / `ffprobe_path` | `ffmpeg` / `ffprobe` | 抽帧工具 |
| `max_results` / `max_search_pages` | `20` / `1` | 硬上限 20 / 3 |
| `hourly_request_budget` | `60` | 滚动 1 小时预算 |
| `call_delay_min/max_ms` | `1800` / `3200` | 调用高斯抖动 |
| `page_delay_min/max_ms` | `3000` / `8000` | 翻页高斯抖动 |
| `media_cache_mb` | `800` | 成片/帧缓存上限 |
| `log_level` | `info` | 结构化日志等级 |
| `writes_enabled` / `allow_send_targets` | `false` / `""` | 保留的写闸门，默认关闭 |

---

## 🧱 项目结构

```
main.py                        唯一 Star：5 指令 + 4 LLM 工具 + 2 钩子 + 集成
douyin_core/                   平台无关领域层（不 import AstrBot SDK）
  errors.py                    7 个稳定错误码 + 文本映射
  redact.py                    凭证脱敏 / ANSI 清理
  normalize.py                 「缺失即 null」归一化 + id 提取
  gate.py                      单 worker 配额门 + 写操作三道闸
  backend.py                   dy-cli 异步子进程 / JSON 信封解析
  media.py                     无水印直链 / 下载 / 抽帧
  store.py                     SQLite：按完整身份隔离
tools/dy_query_adapter.py      上游 MIT 的 --offset / search_id 分页补丁
tests/test_offline.py          20 个离线单测（不联网、不发消息）
metadata.yaml / _conf_schema.json / requirements.txt
```

> Release 的 zip 由 `.github/scripts/build_release.py` 按白名单打包，只含上表
> 21 个文件（不含 CI 脚本与开发文件），解压即为一层完整的插件目录。
> 改编来源与许可归属见 `NOTICE.md` / `LICENSE-UPSTREAM`。

---

## 🧪 开发验证

```bash
python -m compileall -q .
python -m unittest discover -s tests -p "test_*.py" -v
```

离线单测覆盖错误码、脱敏、归一化、JSON 信封、配额与退避、三道闸、身份隔离、幂等写入。全部不联网、不发消息。

---

## ⚠️ 风险与合规

- 使用**非官方接口**（dy-cli 逆向 API + 移动端分享页），不是抖音开放平台；接口与页面结构随时可能变，项目不保证可用性。
- 抖音有风控：高频请求、机房 IP、多实例共用 cookie 会触发限流/验证码，极端情况账号受限。限频只能降低概率，不能消除风险。遇到 `challenge_required` 请**人工在有头浏览器完成验证**，不要写脚本硬刷。
- `detail` 逆向接口不稳定时，上游会回退浏览器签名 XHR；**本插件不含浏览器驱动**，会明确返回可解释错误，不伪造数据。
- 抽帧需 ffmpeg/ffprobe 与有效媒体直链；直链有时效，只缓存成片不缓存 URL。
- 仅供**个人学习与研究**。禁止垃圾营销、骚扰、刷量、养号、多账号矩阵，或任何违反抖音用户协议与当地法律的行为。若抖音开放平台提供同能力，优先使用官方 API。
- 因使用本插件产生的任何后果，作者不承担责任。

---

## 📄 许可与致谢

- **MIT**，完整文本见 [LICENSE](LICENSE)，上游原文见 [LICENSE-UPSTREAM](LICENSE-UPSTREAM)。
- 逐行移植与设计借鉴清单见 [NOTICE.md](NOTICE.md)。上游项目：[`JianHeShui/dsh-douyin`](https://github.com/JianHeShui/dsh-douyin)、[`SakuraMat0u/dsh-douyin-toolkit`](https://github.com/SakuraMat0u/dsh-douyin-toolkit)。
- 集成契约来自 [`menglimi/astrbot_plugin_private_companion`](https://github.com/menglimi/astrbot_plugin_private_companion)（AGPL-3.0-only）公开发布的扩展开发文档；本插件**未复制其源码**。
- AstrBot 插件规范来自 [官方文档](https://docs.astrbot.app/dev/star/plugin-new.html) 与 [`Soulter/helloworld`](https://github.com/Soulter/helloworld) 模板。
