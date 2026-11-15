# 上游能力映射（自动生成，勿手改）

- 上游：`kdcadmin/dsh_douyin_tools`（dsh-douyin-life `0.2.9`）
- 基线 commit：`5c3a644d3586d81e9473b14ad76b7776d7137b93`
- 识别到 34 个 `dy_*` 工具
- 本插件是 Python / AstrBot Star 插件，**不与上游做代码 merge**；下表是能力对齐状态。

| 上游工具 | 本插件 | 状态 | 说明 |
| --- | --- | --- | --- |
| `dy_chat_send` | WriteGate（不暴露工具） | not_ported | 账号写操作，随包写工具恒为 0 |
| `dy_chat_sticker` | WriteGate（不暴露工具） | not_ported | 账号写操作，随包写工具恒为 0 |
| `dy_chrome_open` | — | not_ported | 本插件不启动浏览器 |
| `dy_cleanup` | douyin_cleanup | implemented | 有界 + 默认 dry-run |
| `dy_comment` | WriteGate（不暴露工具） | not_ported | 账号写操作，随包写工具恒为 0 |
| `dy_comments` | — | not_ported | 上游靠页面 XHR/DOM，本插件无浏览器驱动 |
| `dy_config` | _conf_schema.json | partial | AstrBot 可视化配置，无运行时 patch 接口 |
| `dy_dm_inbox` | — | not_ported | 只读私信需浏览器 CDP |
| `dy_dm_poll` | — | not_ported | 秒回属于写操作 |
| `dy_dm_round` | — | not_ported | 写操作，需单独授权 |
| `dy_feed` | — | not_ported | 推荐流需拦截 /module/feed/ 的 CDP 浏览器 |
| `dy_hot` | douyin_hot | implemented | 热榜，无需登录 |
| `dy_hot_videos` | douyin_hot_videos | partial | 按热词搜索实现，非官方热点视频接口 |
| `dy_like` | WriteGate（不暴露工具） | not_ported | 账号写操作，随包写工具恒为 0 |
| `dy_login_poll` | （外部前置步骤） | partial | 同上 |
| `dy_login_sms` | （外部前置步骤） | partial | 同上 |
| `dy_login_sms_submit` | （外部前置步骤） | partial | 同上 |
| `dy_login_start` | （外部前置步骤） | partial | 无头不能扫码，改用 storage_state 导出 |
| `dy_my_posts` | — | not_ported | 需登录态浏览器与创作者页 |
| `dy_my_videos` | — | not_ported | 需登录态浏览器与创作者页 |
| `dy_publish` | WriteGate（不暴露工具） | not_ported | 账号写操作，随包写工具恒为 0 |
| `dy_reconcile_pending` | — | not_ported | 依赖上游待回执账本 |
| `dy_remember` | douyin_remember | implemented | 写入身份隔离的 SQLite |
| `dy_reply` | — | not_ported | 写操作，需单独授权 |
| `dy_reply_round` | — | not_ported | 写操作，需单独授权 |
| `dy_run` | — | not_ported | 上游定时巡检依赖 DSH 调度器 |
| `dy_search` | douyin_search | implemented | search_id 链式翻页 |
| `dy_selfcheck` | douyin_selfcheck | implemented | 真跑依赖探测 |
| `dy_share` | WriteGate（不暴露工具） | not_ported | 账号写操作，随包写工具恒为 0 |
| `dy_status` | douyin_selfcheck / /抖音状态 | implemented | 含依赖矩阵与配额 |
| `dy_thumb` | douyin_frames / douyin_understand | implemented | ffmpeg 均匀抽帧 |
| `dy_video` | douyin_video | implemented | 文案/作者/互动数据 |
| `dy_watch` | douyin_understand | implemented | 抽帧 + 可选 faster-whisper 转写 + 证据 JSON |
| `dy_whoami` | — | not_ported | 需登录态浏览器 |

## 本次变更

- 新增工具：（无）
- 移除工具：（无）

## 移植原则

1. 只读、纯 Python 可落地的能力优先移植；
2. 依赖浏览器 CDP / 页面 XHR 的能力标注 `not_ported`，不伪造实现；
3. 账号写操作（点赞/评论/私信/投稿）一律不暴露工具，`WriteGate` 保持默认 dry-run；
4. 上游变更只触发「报告 + Issue」，**不自动修改插件代码**。
