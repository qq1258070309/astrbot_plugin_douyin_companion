#!/bin/sh
# 服务器侧启动抖音专用浏览器（Xvfb + Chromium + CDP）
#
# 适用场景：无显示器的 Linux（含 proot 容器）。插件通过
#   browser_cdp_url = http://127.0.0.1:9222
# 连接这个浏览器，复用它的登录态。
#
# 环境变量（都可不填）：
#   DY_BROWSER_HOME   工作目录，默认 /root/douyin-browser
#   DY_BROWSER_PORT   CDP 端口，默认 9222
#   DY_DISPLAY_NUM    Xvfb 显示器编号，默认 99
#   DY_CHROMIUM       浏览器可执行文件，默认 /usr/bin/chromium
set -u

HOME_DIR="${DY_BROWSER_HOME:-/root/douyin-browser}"
PORT="${DY_BROWSER_PORT:-9222}"
DISPLAY_NUM="${DY_DISPLAY_NUM:-99}"
CHROMIUM="${DY_CHROMIUM:-/usr/bin/chromium}"
PROFILE_DIR="$HOME_DIR/profile"
LOG="$HOME_DIR/chrome.log"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

mkdir -p "$PROFILE_DIR"

# 只杀本脚本拉起的进程，别误伤 SSH 会话（pattern 用 [] 防止匹配到自己）
pkill -f '[d]ouyin-chromium' 2>/dev/null || true
pkill -f "[X]vfb :$DISPLAY_NUM" 2>/dev/null || true
rm -f "/tmp/.X${DISPLAY_NUM}-lock"
sleep 1

# proot 里 Xvfb 建不了 /tmp/.X99-lock（link 被拒），必须 -nolock
Xvfb ":$DISPLAY_NUM" -nolock -screen 0 1440x900x24 </dev/null >/tmp/xvfb.log 2>&1 &
sleep 2

# --no-sandbox：proot 无 user namespace
# Windows UA：抖音按 UA 下发前端，Linux UA 会拿到精简互动栏（无发送按钮/@ 联想）
# 反节流：窗口被判“被遮挡”时暂停计时器，登录轮询会停
DISPLAY=":$DISPLAY_NUM" setsid "$CHROMIUM" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --remote-debugging-port="$PORT" \
  --remote-allow-origins='*' \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  --lang=zh-CN \
  --window-size=1440,900 \
  --user-agent="$UA" \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-features=CalculateNativeWinOcclusion \
  --mute-audio \
  about:blank </dev/null >"$LOG" 2>&1 &

i=0
while [ "$i" -lt 20 ]; do
  if curl -sS --max-time 2 "http://127.0.0.1:$PORT/json/version" >/tmp/dy_cdp.json 2>/dev/null; then
    echo "CDP ready:"
    head -c 260 /tmp/dy_cdp.json
    echo
    exit 0
  fi
  i=$((i + 1))
  sleep 1
done

echo "CDP not ready; log tail:"
tail -20 "$LOG" 2>/dev/null
exit 1
