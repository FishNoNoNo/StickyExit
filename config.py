"""全局配置与常量。

所有路径基于本文件所在目录（项目根），不与 cwd 绑定。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """加载 .env 文件（极简实现，不依赖 python-dotenv）。

    - 支持注释行（# 开头）与空行
    - 值支持单/双引号包裹，自动去除
    - 已有环境变量优先，.env 不覆盖
    """
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")

LIB_DIR = BASE_DIR / "lib"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "users.db"
CONFIG_PATH = DATA_DIR / "sing-box.json"
RUN_LOG_PATH = DATA_DIR / "sing-box.log"
PIDFILE_PATH = DATA_DIR / "sing-box.pid"
# 记录"当前在跑的 sing-box 实例用的是哪份配置"（内容摘要）。
# 启动时靠它判断在跑的实例是不是陈旧的：sing-box 是 python 的子进程，
# python 被强杀后它会被孤儿化并继续用旧配置服务，光看"端口在听"看不出来。
CONFIG_HASH_PATH = DATA_DIR / "sing-box.sha"

# 「待同步配置」：数据库每变一次就重新渲染到这里；点「重载」时把它写成
# CONFIG_PATH 并重启 sing-box。两者内容不一致 => 页面提示「待重载」。
CONFIG_PENDING_PATH = DATA_DIR / "sing-box.pending.json"

# 对外代理监听：0.0.0.0 = 所有网卡；只有一台机器就填本机内网/公网 IP 更稳
LISTEN_IP = os.environ.get("LISTEN_IP", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "1080"))

# Web 管理页监听
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "5003"))

# Web 管理页访问口令。留空 = 不开启认证（内网/反代后使用）。
# 开启后：打开页面需 http://host:5003/?token=密码，所有 /api/* 接口也需携带 token
# （方式 1：URL 参数 ?token=密码；方式 2：请求头 X-Admin-Token: 密码）。
WEB_ADMIN_TOKEN = os.environ.get("WEB_ADMIN_TOKEN", "").strip()

# 展示给用户的对外 IP（代理地址里的 host），留空则自动探测。
# 探测优先级：ADVERTISE_IP > LISTEN_IP(具体IP) > 公网IP > 本机内网IP > 127.0.0.1
ADVERTISE_IP = os.environ.get("ADVERTISE_IP", "").strip()

# 订阅往往是自签名证书（机场面板常见），默认不校验证书。
# 若你的订阅是正规 CA 证书，可设 True 开启校验更安全。
VERIFY_SSL = os.environ.get("VERIFY_SSL", "0") == "1"

# 手动节点健康检查的 TCP 拨测超时
NODE_CHECK_TIMEOUT = float(os.environ.get("NODE_CHECK_TIMEOUT", "5.0"))

# ---------- Clash API（sing-box 的运行时控制接口）----------
#
# selector 是手动组，没人动它就永远停在 default，哪怕那个节点已经死了。
# Clash API 是唯一能在不重启 sing-box 的前提下改变 selector 选中项的手段，
# 故障转移就靠它。只绑回环，不要改成 0.0.0.0。
CLASH_API_HOST = os.environ.get("CLASH_API_HOST", "127.0.0.1").strip()
CLASH_API_PORT = int(os.environ.get("CLASH_API_PORT", "9090"))
CLASH_API_SECRET = os.environ.get("CLASH_API_SECRET", "").strip()

# ---------- 健康探测与故障转移 ----------
#
# 探测周期。要数「连续 N 次失败」就必须反复探测，范围只限「进了某个用户池的
# 节点」，不碰池外的节点。
HEALTH_INTERVAL = max(1, int(os.environ.get("HEALTH_INTERVAL", "30")))
# 每轮探测的最大并发数（协程）。协程很轻，池节点很多时可放心调大。
HEALTH_MAX_WORKERS = max(1, int(os.environ.get("HEALTH_MAX_WORKERS", "20")))
# 连续失败多少次才降级到下一个成员。压住「一次网络波动就切」。
FAIL_THRESHOLD = max(1, int(os.environ.get("FAIL_THRESHOLD", "3")))
# 连续成功多少次才回切到优先级更高的成员。压住「回切时来回抖」。
RECOVER_THRESHOLD = max(1, int(os.environ.get("RECOVER_THRESHOLD", "2")))
# 探测 URL：**从节点那一侧访问**。国内不通 gstatic 的话必须换掉。
HEALTH_PROBE_URL = os.environ.get(
    "HEALTH_PROBE_URL", "http://www.gstatic.com/generate_204"
).strip()
HEALTH_PROBE_TIMEOUT = float(os.environ.get("HEALTH_PROBE_TIMEOUT", "5.0"))


# ---------- 对外 IP 探测 ----------

_advertise_cache: str | None = None


def _configured_advertise_ip() -> str | None:
    """不联网就能确定的对外 IP；无法确定时返回 None。"""
    if ADVERTISE_IP:
        return ADVERTISE_IP
    if LISTEN_IP not in ("0.0.0.0", "::", ""):
        return LISTEN_IP
    return None


def _lan_ip() -> str:
    """本机内网 IP（纯 socket，不发网络请求）。"""
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def get_cached_advertise_ip() -> str:
    """同步读取对外 IP，绝不发起网络请求。

    只在缓存命中或能从配置/本机网卡立即确定时返回真实对外 IP，
    否则用本机内网 IP 兜底。需要公网探测请 await get_advertise_ip()。
    """
    return _advertise_cache or _configured_advertise_ip() or _lan_ip()


async def get_advertise_ip() -> str:
    """异步返回展示给用户的对外 IP（首次探测公网 IP 并缓存）。"""
    global _advertise_cache
    if _advertise_cache:
        return _advertise_cache
    fixed = _configured_advertise_ip()
    if fixed:
        _advertise_cache = fixed
        return fixed
    # 公网 IP 探测（短超时，失败不阻塞太久）
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://api.ipify.org")
        val = r.text.strip()
        if val and "{" not in val and ":" not in val:
            _advertise_cache = val
            return val
    except Exception:
        pass
    _advertise_cache = _lan_ip()
    return _advertise_cache