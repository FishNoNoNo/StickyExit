#!/usr/bin/env bash
# 容器入口：修正挂载进来的 sing-box 执行位，校验后启动。
#
# 为什么要在运行时 chmod：lib/ 是 bind mount，镜像构建期对
# /app/lib/sing-box-linux/sing-box 做的 chmod 会被挂载覆盖掉。文件权限
# 来自宿主机（在 Windows 上 checkout 出来通常是 0644），sing-box 会
# "permission denied"。这里以 root 在容器内补上执行位。
set -euo pipefail

SB="/app/lib/sing-box-linux/sing-box"

if [[ ! -f "$SB" ]]; then
  echo "[entrypoint] 错误：找不到 $SB" >&2
  echo "[entrypoint] lib/ 必须挂载进容器，例如：-v ./lib:/app/lib" >&2
  exit 1
fi

chmod +x "$SB" 2>/dev/null || true

if [[ ! -x "$SB" ]]; then
  echo "[entrypoint] 错误：$SB 不可执行且无法 chmod。" >&2
  echo "[entrypoint] 请在宿主机执行: chmod +x lib/sing-box-linux/sing-box" >&2
  exit 1
fi

# 提前跑一次 version：二进制损坏/架构不匹配（arm64 机器用了 amd64 的包）
# 在这里就炸掉，而不是等到建用户时才莫名其妙失败。
if ! "$SB" version >/dev/null 2>&1; then
  echo "[entrypoint] 错误：sing-box 无法运行（架构不匹配或文件损坏）。" >&2
  "$SB" version || true
  exit 1
fi

echo "[entrypoint] sing-box: $("$SB" version | head -1)"

mkdir -p /app/data

# exec：让 python 成为 PID 1，收到 SIGTERM 能直接退出，
# 否则 compose stop 会等 10 秒再 SIGKILL。
exec python main.py
