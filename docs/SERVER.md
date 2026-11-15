# 在无显示器的服务器上使用（Linux / aarch64 / proot）

本插件的账号侧能力（推荐流、评论、私信、点赞、转发、投稿）依赖**带界面的**浏览器。
服务器通常没有显示器、也装不了官方 Playwright Chromium（aarch64 无对应构建），
所以采用：

```
Xvfb（虚拟显示器） + 系统 Chromium（--no-sandbox） + CDP 9222
                                  ▲
                    AstrBot 插件通过 browser_cdp_url 连接
```

这样 AstrBot 进程本身不需要 `DISPLAY`，重启插件也不影响浏览器里的登录态。

---

## 1. 装系统依赖

```bash
apt-get install -y --no-install-recommends chromium fonts-noto-cjk xvfb
apt-get install -y --no-install-recommends x11vnc novnc websockify   # 可选：网页远程登录
```

> **proot 容器注意**：`dpkg` 可能报
> `error creating new backup file '/var/lib/dpkg/status-old': Permission denied`。
> 包文件通常已经解包、二进制可用（`chromium --version` 能跑），只是 dpkg 记账状态停在 `iU`。
> 后续 apt 仍会报同样错，但不影响本插件运行。

装 AstrBot 侧依赖（只装客户端，不下载浏览器）：

```bash
/root/.local/share/uv/tools/astrbot/bin/python -m pip install 'playwright>=1.45'
```

## 2. 启动浏览器

把 `scripts/server-browser-start.sh` 放到服务器（如 `/root/douyin-browser/start.sh`），执行：

```bash
sh /root/douyin-browser/start.sh
# CDP ready: { "Browser": "Chrome/150...", "User-Agent": "... Windows NT 10.0 ..." }
```

脚本里三个必需参数的原因：

| 参数 | 原因 |
| --- | --- |
| `Xvfb :99 -nolock` | proot 不允许 Xvfb 创建 `/tmp/.X99-lock`（link 被拒），`xvfb-run` 会直接失败 |
| `--no-sandbox` | proot 无 user namespace，沙箱起不来 |
| `--user-agent=<Windows Chrome>` | 抖音按 UA 下发不同前端；Linux UA 没有发送按钮、@ 也不出联想面板 |

另外带了 `--disable-background-timer-throttling` 等反节流参数：窗口被判「被遮挡」时
Chrome 会暂停计时器，登录轮询会停，表现就是「扫码了但一直没登上」。

## 3. 在服务器上登录一次

浏览器在服务器上没有可见窗口，推荐用 noVNC 远程操作一次：

```bash
# 一次性临时开启（仅内网；用完关掉）
x11vnc -storepasswd '<临时密码>' /root/.vnc/passwd
x11vnc -display :99 -auth /dev/null -noshm -forever -shared \
       -rfbauth /root/.vnc/passwd -rfbport 5900 -noxdamage -repeat -bg
websockify --web /usr/share/novnc 6080 127.0.0.1:5900 &
```

浏览器打开 `http://<服务器IP>:6080/vnc.html?autoconnect=1&resize=scale`，
输入临时密码，在远程桌面里完成扫码或短信登录。登录态写进
`<DY_BROWSER_HOME>/profile`，之后插件直接复用。

> 安全：noVNC 没有 TLS，只适合内网临时使用；登录完请 `pkill websockify`。
> 需要长期远程维护时，改用 SSH 隧道：`ssh -L 6080:127.0.0.1:6080 ...`，不要直接暴露端口。

## 4. 配置插件

在 AstrBot 的插件配置里：

```json
{
  "enable_browser": true,
  "browser_headless": false,
  "browser_cdp_url": "http://127.0.0.1:9222"
}
```

写操作仍然默认关闭（`writes_enabled=false`、`write_dry_run=true`）。

可选：把登录态导出给读路径（`dy-cli`）用：

```python
# 在能连 CDP 的机器上执行
ctx.storage_state(path="data/plugin_data/astrbot_plugin_douyin_companion/home/.dy/cookies/default.json")
```

## 5. 重启后自启

无 systemd 的机器没有开机服务。若存在 `/usr/local/bin/ensure-services.sh`
（被 `/etc/ssh/sshrc` 调用），在末尾追加：

```sh
if ! curl -sf --max-time 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    if mkdir /tmp/dy-browser-start.lock 2>/dev/null; then
        (
            nohup sh -c 'sh /root/douyin-browser/start.sh >>/root/douyin-browser/autostart.log 2>&1; rmdir /tmp/dy-browser-start.lock' >/dev/null 2>&1 &
        )
    fi
fi
```

这样每次 SSH 会话都会检查一次（端口通就不动），登录到机器后浏览器自动就绪。

---

## 已知限制

- **登录态绑定这份 profile**：删掉 `profile/` 就要重新登录。
- **单实例**：同一 profile 只允许一个 Chrome。插件与手工调试都要连同一个 CDP，不要各自 launch。
- **内存**：抖音首页带视频，Chromium 峰值可能 1–2 GB；小内存机器建议不要长时间停在推荐流页。
- **选择器会变**：DOM 取证逻辑来自 vendored 上游（`douyin_core/browser_driver.py`）；
  改版后先跑 `python tools/vendor_browser_driver.py --check` 看是否落后于上游。
- **写操作不可撤回**：服务器上同样默认全关，开启前先读 README 的「写操作安全模型」。
