# 上游能力映射（自动生成，勿手改）

- 上游：`kdcadmin/dsh_douyin_tools`（dsh-douyin-life `0.2.9`）
- 基线 commit：`5c3a644d3586d81e9473b14ad76b7776d7137b93`
- 识别到 34 个 `dy_*` 工具
- 本插件是 Python / AstrBot Star 插件，**不与上游做代码 merge**；下表是能力对齐状态。

| 上游工具 | 本插件 | 状态 | 说明 |
| --- | --- | --- | --- |
| `dy_chat_send` | douyin_dm / /抖音发私信 | implemented | 受收件人白名单 + 每日额度约束 |
| `dy_chat_sticker` | — | not_ported | 表情包发送未接 |
| `dy_chrome_open` | /抖音登录 | partial | 浏览器由 Playwright 拉起，没有独立开窗指令 |
| `dy_cleanup` | douyin_cleanup | implemented | 有界 + 默认 dry-run |
| `dy_comment` | douyin_comment / /抖音发评论 | implemented | 受 ActionPolicy 约束，支持真人 @ 联想 |
| `dy_comments` | douyin_comments / /抖音评论 | implemented | 页面 XHR 优先，DOM 兜底 |
| `dy_config` | _conf_schema.json | partial | AstrBot 可视化配置，无运行时 patch 接口 |
| `dy_dm_inbox` | douyin_inbox / /抖音私信 | implemented | 只返回有消息 ID 的入站消息 |
| `dy_dm_poll` | — | not_ported | 常驻轮询属于自动回复，未接 |
| `dy_dm_round` | — | not_ported | 自动回复回合，未接 |
| `dy_feed` | douyin_feed / /抖音推荐 | implemented | 拦截 /module/feed/ XHR + 滚动收集 |
| `dy_hot` | douyin_hot | implemented | 热榜，无需登录 |
| `dy_hot_videos` | douyin_hot_videos | partial | 按热词搜索实现，非官方热点视频接口 |
| `dy_like` | douyin_like / /抖音点赞 | implemented | 受 ActionPolicy 四重闸门约束 |
| `dy_login_poll` | （外部前置步骤） | partial | 同上 |
| `dy_login_sms` | （外部前置步骤） | partial | 同上 |
| `dy_login_sms_submit` | （外部前置步骤） | partial | 同上 |
| `dy_login_start` | （外部前置步骤） | partial | 无头不能扫码，改用 storage_state 导出 |
| `dy_my_posts` | — | not_ported | 需创作者中心页面，尚未接 |
| `dy_my_videos` | — | not_ported | 需创作者中心页面，尚未接 |
| `dy_publish` | douyin_publish / /抖音投稿 | implemented | 默认草稿，投稿目录白名单 |
| `dy_reconcile_pending` | store.recent_actions | partial | 本地账本可查，未做平台对账 |
| `dy_remember` | douyin_remember | implemented | 写入身份隔离的 SQLite |
| `dy_reply` | douyin_comment（reply_cid 接口已备） | partial | 内核支持按 cid/序号回复，CLI 暂未暴露 |
| `dy_reply_round` | — | not_ported | 自动回复回合，未接 |
| `dy_run` | — | not_ported | 上游定时巡检依赖 DSH 调度器 |
| `dy_search` | douyin_search | implemented | search_id 链式翻页 |
| `dy_selfcheck` | douyin_selfcheck | implemented | 真跑依赖探测 |
| `dy_share` | douyin_share / /抖音转发 | implemented | 受收件人白名单约束 |
| `dy_status` | douyin_selfcheck / /抖音状态 | implemented | 含依赖矩阵与配额 |
| `dy_thumb` | douyin_frames / douyin_understand | implemented | ffmpeg 均匀抽帧 |
| `dy_video` | douyin_video | implemented | 文案/作者/互动数据 |
| `dy_watch` | douyin_understand | implemented | 抽帧 + 可选 faster-whisper 转写 + 证据 JSON |
| `dy_whoami` | /抖音状态 登录 | partial | 登录态由 vendored 内核探测（cookie + 头像双证据） |

## 本次变更

- 新增工具：（无）
- 移除工具：（无）

## 移植原则

1. 只读、纯 Python 可落地的能力优先移植；浏览器能力复用 vendored 内核（MIT），不重写选择器；
2. 依赖浏览器 CDP / 页面 XHR 的能力在没有内核支持时标注 `not_ported`，不伪造实现；
3. 账号写操作默认关闭，必须经 `ActionPolicy` 四重闸门（总闸 / 动作白名单 / 每日额度 / 收件人白名单）；
4. 上游变更只触发「报告 + Issue」，**不自动修改插件代码**。
