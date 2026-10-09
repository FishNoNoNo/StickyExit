"""订阅拉取 + Clash YAML / URL 解析 → 统一的节点列表。

- 输入：Clash 订阅（YAML，proxies 列表）或 base64 编码的 URL 列表（各类 proxy:// 链接）。
- 输出：与 sing-box outbound 结构一致的 dict 列表，每个节点一个稳定 tag。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re

import httpx
import yaml

import config
from parser.links import parse_plain_links, url_to_outbound

logger = logging.getLogger("distributor.fetcher")

USER_AGENT = (
    "clash-verge/v2.0.0 (https://github.com/clash-verge-rev/clash-verge-rev)"
)
TEXT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
}


async def _fetch_text(url: str, timeout: int = 45) -> str:
    """下载订阅原始内容（异步，自动跟随重定向）。"""
    verify = config.VERIFY_SSL
    async with httpx.AsyncClient(
        verify=verify, timeout=timeout, follow_redirects=True
    ) as client:
        resp = await client.get(url, headers=TEXT_HEADERS)
    resp.raise_for_status()
    # 机场常返回 gzip，httpx 已解压；偶尔给出 GBK 等编码，做兜底
    try:
        return resp.text
    except UnicodeDecodeError:
        return resp.content.decode("utf-8", "replace")


def _maybe_decode_b64(text: str) -> str:
    """订阅内容若是 base64 则解码成明文链接列表。

    机场面板常按 76 列折行输出 base64，所以"含 \\n"不再等于"不是 base64"：
    只有看到 Clash YAML 的明确特征（proxies:）才原样返回，其余情况仍然
    尝试按 base64 解码；解不出来再退回原文本（普通链接列表 / 错误内容）。
    """
    t = text.strip()
    if not t:
        return t
    if "proxies:" in t[:4000]:
        return t  # 明显的 Clash YAML（解码尝试会失败，且会破坏缩进）
    # 去掉全部空白（含折行的换行）后按 base64 试解；plain URL 列表含 "://"，
    # 去掉空白后必然带着冒号，过不了下面的字符集检查 -> 原样返回，行为不变。
    candidate = re.sub(r"\s+", "", t)
    if len(candidate) < 16 or not re.fullmatch(r"[A-Za-z0-9+/=\-_]*", candidate):
        return t
    padded = candidate + "=" * (-len(candidate) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
        # 解码后应是一堆 URL 行
        if b"://" in raw:
            return raw.decode("utf-8", "replace")
    except Exception:
        pass
    return t


def _strip_comments(text: str) -> str:
    """去掉 YAML 顶层注释行（Clash 常带 # 说明，不影响解析但去噪）。"""
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def parse_clash_yaml(text: str) -> list[dict]:
    """解析 Clash YAML，返回每个 proxy 的源字段 dict（保留原样）。

    非 YAML 文本（每行一个 proxy:// 链接的纯文本）返回空列表，交给
    links.parse_plain_links 兜底；不能让 yaml.safe_load 的 ParserError
    冒出去 —— 例如链接列表里夹一行 # 注释就会触发，导致导入整体 500。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict) or "proxies" not in data:
        logger.warning("YAML 中没有 proxies 字段")
        return []
    proxies = data["proxies"]
    if not isinstance(proxies, list):
        return []
    return [p for p in proxies if isinstance(p, dict)]


# --- node → sing-box outbound 映射（核心转换） --------------------------------

def _clash_server_fields(proxy: dict) -> dict:
    return {
        "server": str(proxy.get("server", "")),
        "server_port": int(proxy.get("port") or 0),
    }


def _clash_tls(proxy: dict) -> dict | None:
    if proxy.get("tls", False):
        out = {"enabled": True}
        if proxy.get("servername"):
            out["server_name"] = str(proxy["servername"])
        if proxy.get("skip-cert-verify"):
            out["insecure"] = True
        if proxy.get("alpn"):
            out["alpn"] = proxy["alpn"] if isinstance(proxy["alpn"], list) else [str(proxy["alpn"])]
        if proxy.get("client-fingerprint"):
            out["utls"] = {"enabled": True, "fingerprint": str(proxy["client-fingerprint"])}
        return out
    return None


def _clash_transport(proxy: dict) -> dict | None:
    """Clash 的 network/*-opts → sing-box transport 字段。

    返回 None 表示走默认 TCP（sing-box 不需要 transport 字段）。**认不出的
    network 一律抛 ValueError 而不是当 TCP 处理**：那样会让一个 h2/kcp 节点
    静默退化成裸 TCP —— check 能过，但连不上，属于"发得出去、用不了"的哑
    节点。宁可显式跳过并在导入结果里报出来。
    """
    network = str(proxy.get("network") or "").strip().lower()
    if network == "ws":
        opts = proxy.get("ws-opts") or {}
        headers = opts.get("headers") or {}
        transport = {
            "type": "ws",
            "headers": {str(k): str(v) for k, v in headers.items()},
        }
        if opts.get("path"):
            transport["path"] = str(opts["path"])
        if opts.get("max-early-data"):
            transport["max_early_data"] = int(opts["max-early-data"])
        if opts.get("early-data-header-name"):
            transport["early_data_header_name"] = str(opts["early-data-header-name"])
        return transport
    if network == "grpc":
        opts = proxy.get("grpc-opts") or {}
        transport = {"type": "grpc"}
        if opts.get("grpc-service-name"):
            transport["service_name"] = str(opts["grpc-service-name"])
        return transport
    if network == "httpupgrade":
        opts = proxy.get("httpupgrade-opts") or {}
        transport = {"type": "httpupgrade"}
        if opts.get("path"):
            transport["path"] = str(opts["path"])
        if opts.get("host"):
            transport["host"] = str(opts["host"])
        return transport
    if network in ("h2", "http"):
        # HTTP/2。Clash 新老版本把参数放在 h2-opts 或 http-opts，两个都认。
        opts = proxy.get("h2-opts") or proxy.get("http-opts") or {}
        transport = {"type": "http"}
        host = opts.get("host")
        if host:
            transport["host"] = host if isinstance(host, list) else [str(host)]
        path = opts.get("path")
        if path:
            # sing-box 的 path 是单个字符串，给数组会 unmarshal 失败
            transport["path"] = str(path[0]) if isinstance(path, list) else str(path)
        return transport

    if network in (None, "", "tcp"):
        return None

    # xhttp 未编入当前构建；kcp / splithttp 等同样没有对应 transport。
    raise ValueError(f"network={network} 当前 sing-box 构建不支持，已跳过")


def clash_node_to_outbound(proxy: dict) -> dict:
    """Clash proxy → sing-box outbound（无 tag 部分）。

    转换不了的协议直接抛 ValueError，由上层过滤。
    """
    ptype = str(proxy.get("type", "")).lower()
    base = {"server": str(proxy.get("server", "")), "server_port": int(proxy.get("port") or 0)}

    if ptype == "vmess":
        out = {"type": "vmess", **base}
        out["uuid"] = str(proxy.get("uuid", ""))
        out["alter_id"] = int(proxy.get("alterId", 0))
        # Clash 叫 cipher，sing-box 的 vmess 字段叫 security
        out["security"] = str(proxy.get("cipher", "auto"))
        if proxy.get("packet-encoding"):
            out["packet_encoding"] = str(proxy["packet-encoding"])
        tls = _clash_tls(proxy)
        if tls:
            out["tls"] = tls
        tr = _clash_transport(proxy)
        if tr:
            out["transport"] = tr
        return out

    if ptype == "vless":
        out = {"type": "vless", **base}
        out["uuid"] = str(proxy.get("uuid", ""))
        out["packet_encoding"] = str(proxy.get("packet-encoding", "xudp"))
        if proxy.get("flow"):
            out["flow"] = str(proxy["flow"])
        tls = _clash_tls(proxy)
        if tls:
            out["tls"] = tls
        if proxy.get("reality-opts"):
            ro = proxy["reality-opts"] or {}
            tls = out.setdefault("tls", {"enabled": True})
            tls["reality"] = {
                "enabled": True,
                "public_key": str(ro.get("public-key", "")),
                "short_id": str(ro.get("short-id", "")),
            }
            if ro.get("fingerprint"):
                tls["utls"] = {"enabled": True, "fingerprint": str(ro["fingerprint"])}
        tr = _clash_transport(proxy)
        if tr:
            out["transport"] = tr
        return out

    if ptype == "trojan":
        out = {"type": "trojan", **base}
        out["password"] = str(proxy.get("password", ""))
        tls = _clash_tls(proxy)
        if tls:
            out["tls"] = tls
        tr = _clash_transport(proxy)
        if tr:
            out["transport"] = tr
        # 注意：trojan 没有 udp_over_tcp 字段（shadowsocks 才有），
        # 写了会被 check 判为 unknown field 并让整份配置失效。
        return out

    if ptype == "ss":
        out = {"type": "shadowsocks", **base}
        out["method"] = str(proxy.get("cipher", "")).lower()
        out["password"] = str(proxy.get("password", ""))
        if proxy.get("udp"):
            out["udp_over_tcp"] = True
        plugin = str(proxy.get("plugin") or "").strip()
        if plugin:
            # sing-box 用 SIP003 的 plugin / plugin_opts 字符串字段，没有
            # Clash 那种 obfs 对象；写错会被 check 判为 unknown field "obfs"
            # 并让整份配置失效。
            opts = proxy.get("plugin-opts") or {}
            if plugin.lower().startswith("obfs"):
                # Clash 里叫 obfs，SIP003 里叫 obfs-local（sing-box 内建实现）
                parts = ["obfs=" + str(opts.get("mode") or "http")]
                if opts.get("host"):
                    parts.append("obfs-host=" + str(opts["host"]))
                out["plugin"] = "obfs-local"
                out["plugin_opts"] = ";".join(parts)
            else:
                # 其余插件名按 SIP003 原样透传（v2ray-plugin 也是 sing-box
                # 内建的，不需要额外装二进制），选项由 Clash 的 dict 拍平成
                # "k=v;k2=v2"。认不认得出交给 sing-box check 判定：像
                # shadow-tls 这种它没有内建支持的会报 plugin not found，
                # 导入时即被剔除并回报，而不是静默变成连不上的哑节点。
                parts = []
                for k, v in opts.items():
                    if isinstance(v, bool):
                        if v:
                            parts.append(str(k))  # tls: true -> "tls"
                    elif isinstance(v, (str, int, float)) and str(v) != "":
                        parts.append(f"{k}={v}")
                # quic 模式 sing-box 强制要求 TLS，Clash 会隐式补上，这里跟上
                if str(opts.get("mode", "")).lower() == "quic" and not opts.get("tls"):
                    parts.append("tls")
                out["plugin"] = plugin
                if parts:
                    out["plugin_opts"] = ";".join(parts)
        return out

    if ptype == "hysteria2":
        out = {"type": "hysteria2", **base}
        out["password"] = str(proxy.get("password", ""))
        if not proxy.get("tls"):
            out["tls"] = {"enabled": True}  # hy2 强制 TLS
        tls = _clash_tls(proxy)
        if tls:
            out["tls"] = tls
        if proxy.get("obfs"):
            out["obfs"] = {"type": "salamander", "password": str(proxy["obfs"])}
        if proxy.get("up"):
            out["up_mbps"] = int(proxy["up"])
        if proxy.get("down"):
            out["down_mbps"] = int(proxy["down"])
        return out

    if ptype == "tuic":
        out = {"type": "tuic", **base}
        out["uuid"] = str(proxy.get("uuid", ""))
        out["password"] = str(proxy.get("password", ""))
        if proxy.get("congestion-controller"):
            out["congestion_control"] = str(proxy["congestion-controller"])
        if proxy.get("udp-relay-mode"):
            out["udp_relay_mode"] = str(proxy["udp-relay-mode"])
        if proxy.get("zero-rtt-handshake"):
            out["zero_rtt_handshake"] = bool(proxy["zero-rtt-handshake"])
        # tuic 一定需要 tls
        tls = {"enabled": True}
        if proxy.get("servername"):
            tls["server_name"] = str(proxy["servername"])
        if proxy.get("skip-cert-verify"):
            tls["insecure"] = True
        if proxy.get("alpn"):
            tls["alpn"] = proxy["alpn"] if isinstance(proxy["alpn"], list) else [str(proxy["alpn"])]
        out["tls"] = tls
        return out

    if ptype in ("socks5", "socks"):
        out = {"type": "socks", **base}
        out["version"] = "5"
        if proxy.get("username"):
            out["username"] = str(proxy["username"])
        if proxy.get("password"):
            out["password"] = str(proxy["password"])
        return out

    if ptype == "http":
        out = {"type": "http", **base}
        if proxy.get("username"):
            out["username"] = str(proxy["username"])
        if proxy.get("password"):
            out["password"] = str(proxy["password"])
        return out

    if ptype in ("ssr",):
        raise ValueError("SSR 协议 sing-box 原生不支持，已跳过")

    raise ValueError(f"不支持的节点类型: {ptype}")


def _stable_tag(name: str, server: str, port: int, idx: int = 0) -> str:
    """节点唯一 tag：原始名 + 服务器 + 端口 hash。"""
    raw = f"{name}|{server}|{port}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"node-{h}"


def normalize_outbound(out: dict, name: str) -> dict:
    """给转换后的 outbound 补上稳定 tag；name 去重后缀。"""
    out = dict(out)
    out.pop("tag", None)
    server = out.get("server", "")
    port = int(out.get("server_port") or 0)
    tag = _stable_tag(str(name), server, port, out.get("uuid") or out.get("password") or "")
    out["tag"] = tag
    return {"tag": tag, "name": str(name), "outbound": out}


async def fetch_nodes(url: str) -> list[dict]:
    """主入口：异步下载订阅 → 解析 → 转换 → 返回 sing-box outbound 列表（含 tag）。"""
    text = await _fetch_text(url)
    text = _maybe_decode_b64(text)
    text = _strip_comments(text)

    proxies = parse_clash_yaml(text)
    outbounds: list[dict] = []
    seen = set()
    # 面板会把“剩余流量/重置时间/到期”等信息伪装成节点，过滤掉
    META_KEYWORDS = ("流量", "重置", "到期", "套餐", "剩余", "时间", "traffic", "expire", "reset")

    if proxies:
        for p in proxies:
            name = str(p.get("name", "")).strip() or f"node-{len(outbounds)}"
            if any(k in name for k in META_KEYWORDS):
                logger.info("跳过面板信息节点: %s", name)
                continue
            try:
                out = clash_node_to_outbound(p)
            except ValueError as e:
                logger.warning("跳过节点 %s: %s", name, e)
                continue
            node = normalize_outbound(out, name)
            if node["tag"] in seen:
                continue
            seen.add(node["tag"])
            outbounds.append(node)
    else:
        links = parse_plain_links(text)
        for u in links:
            try:
                out = url_to_outbound(u)
            except Exception as e:
                logger.warning("跳过链接: %s", e)
                continue
            node = normalize_outbound(out, u)
            if node["tag"] in seen:
                continue
            seen.add(node["tag"])
            outbounds.append(node)

    logger.info("拉取到 %d 个可用节点", len(outbounds))
    return outbounds