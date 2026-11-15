# 上游同步 / 自动更新 / 能力移植

本插件有**两个上游概念**，不要混淆：

| 概念 | 仓库 | 关系 |
| --- | --- | --- |
| 代码来源 | [`JianHeShui/dsh-douyin`](https://github.com/JianHeShui/dsh-douyin)（MIT） | 0.1.0 的设计原理与 `dy_query_adapter.py` 从这里移植 |
| **能力参考上游** | [`kdcadmin/dsh_douyin_tools`](https://github.com/kdcadmin/dsh_douyin_tools) = `dsh-douyin-life`（MIT / TiAmoSS） | 0.2.0 起的能力对齐对象，由哨兵持续跟踪 |

## 1. 同步：上游能力哨兵

`.github/workflows/upstream-watch.yml` 每 6 小时（及手动触发）执行：

```
git clone --depth 1 kdcadmin/dsh_douyin_tools /tmp/upstream
  → tools/upstream_diff.py --check（DRY_RUN，比对 UPSTREAM_BASE）
  → 有变更才 --write：刷新 docs/UPSTREAM_MAPPING.md + docs/UPSTREAM_CHANGE.md + UPSTREAM_BASE
  → 提交报告 + 开一个 Issue「上游能力变更待移植」
```

**关键：不自动改插件代码。** 上游是 Node 22 / Cordis / DSH 插件（267 KB `index.js` + 108 KB `client.js` + Playwright CDP），本仓库是 Python / AstrBot `Star` 插件，目录结构与运行时完全不同，自动 merge 必然破坏插件。哨兵只做「感知 → 差异报告 → 人工/AI 审查移植」。

本地手动跑：

```bash
git clone --depth 1 https://github.com/kdcadmin/dsh_douyin_tools.git /tmp/upstream
python tools/upstream_diff.py --upstream-dir /tmp/upstream          # DRY_RUN，只打印
python tools/upstream_diff.py --upstream-dir /tmp/upstream --check  # 有变更返回 1
python tools/upstream_diff.py --upstream-dir /tmp/upstream --write  # 落盘报告
python tools/upstream_diff.py --online                             # 不 clone，直接抓 raw
```

`docs/UPSTREAM_MAPPING.md` 是自动生成的**能力映射表**，逐项标注 `implemented` / `partial` / `not_ported` / `need_review`。上游新增的工具会落到 `need_review`，等评估可移植性。

## 2. 更新：自动发版 + AstrBot 检查更新

- `.github/workflows/release.yml`：打 `v*` tag 自动跑（编译 + 单测 → 白名单打包 zip + sha256 → 发布 Release）；也可在 Actions 手动触发，版本默认从 `metadata.yaml` 读。
- `metadata.yaml` 的 `repo` 指向本仓库，AstrBot 依据「默认分支 `metadata.yaml` 的 `version` + GitHub Release」检查插件更新。因此**每次升级只需改版本号并打 tag**。
- `UPSTREAM_BASE` 记录上游 commit / version / 工具哈希，作为「同步到哪儿了」的凭据。

发布一个新版本：

```bash
# 1. 改 metadata.yaml 的 version 与 CHANGELOG.md
# 2. 提交
git add -A && git commit -m "release: 0.2.0"
# 3. 打 tag（触发自动发版）
git tag v0.2.0 && git push origin main && git push origin v0.2.0
```

## 3. 功能增强：0.2.0 移植了什么

对照上游 34 个 `dy_*` 工具，**只移植只读、纯 Python 可落地的能力**：

| 上游 | 本插件 | 说明 |
| --- | --- | --- |
| `dy_watch` / `dy_thumb` | `douyin_understand` / `douyin_frames` | 抽帧 + 可选 faster-whisper 转写 + 结构化证据 JSON + 回放 |
| `dy_hot_videos` | `douyin_hot_videos` | 按热词搜索（**明确标注非官方热点接口**） |
| `dy_remember` | `douyin_remember` | 写入身份隔离的 SQLite，参与陪伴小屋自动召回 |
| `dy_status` / `dy_selfcheck` | `douyin_selfcheck` / `/抖音自检` / `/抖音能力` | 真跑依赖矩阵 + 配额 + 能力矩阵 |
| `dy_cleanup` | `douyin_cleanup` / `/抖音清理` | 有界（≤200 文件）+ **默认 dry-run** |

**明确不移植**（详见 `docs/UPSTREAM_MAPPING.md`）：

- `dy_feed` 推荐流、`dy_comments` 评论、`dy_my_posts`、`dy_dm_inbox`：依赖浏览器 CDP / 页面 XHR，本插件不含浏览器驱动；
- `dy_like` / `dy_comment` / `dy_share` / `dy_chat_*` / `dy_publish` / `dy_reply*`：**账号写操作**，上游自己也默认全关；本插件 `WriteGate` 保留判定器但随包写工具恒为 0；
- `dy_run` / 日报 / 定时巡检：依赖 DSH 调度器。

## 4. 安全与回滚

- 哨兵默认 DRY_RUN；`--write` 只改 3 个报告文件，不碰 `main.py` / `douyin_core/`。
- 发版工作流先跑 `compileall` + `unittest`，失败不发 Release。
- 回滚：`v0.1.0` Release 与 zip 永久保留， AstrBot 侧重新上传旧 zip 即回退；插件不写陪伴小屋任何数据，卸载无残留（除自己的 `data/plugin_data/` 目录）。
- 上游仓库无 tag/release，因此哨兵按 **commit SHA + 工具清单哈希** 判定变更，不依赖版本号。
