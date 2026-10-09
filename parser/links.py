"""节点链接（proxy:// URL）→ sing-box outbound。

只用标准库自实现：历史上这里走第三方 singbox2proxy，但它不在 requirements
里，缺了就会让所有裸链接（vmess:// / socks5:// …）静默导入 0 个节点。改成
自实现后，认不出的类型显式抛 ValueError，由上层过滤并回报，而不是静默吞掉。

链接 scheme → sing-box outbound:
    socks/socks5/socks4/socks4a  -> socks（靠 version 区分 5 / 4 / 4a）
    http/https                   -> http（https 补 tls）
    ss                           -> shadowsocks
    vmess/vless/trojan           -> 同名 outbound
    hy2/hysteria2                -> hysteria2
    tuic                         -> tuic
"""
from __future__ import annotations

import base64
import ipaddress
import json
import urllib.parse
from typing import Any

# parse_plain_links 用它从长文本里挑出节点链接行。
SUPPORTED_PREFIXES = (
    "vmess://", "vless://", "ss://", "trojan://",
    "hy2://", "hysteria2://", "tuic://",
    "socks://", "socks5://", "socks4://", "socks4a://",
    "http://", "https://",
)


def _b64_try_decode(s: str) -> str:
    """宽松 base64 解码：补 padding，并把 URL-safe 的 -_ 还原成 +/。"""
    cand = s.strip().replace("-", "+").replace("_", "/")
    cand += "=" * (-len(cand) % 4)
    return base64.b64decode(cand).decode("utf-8", "replace")


def _qdict(query: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items():
        if v:
            out[k] = v[0]
    return out


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _split_host_port(s: str) -> tuple[str, int]:
    s = s.strip().rstrip("/")  # SIP002 常写成 host:port/?plugin=...
    if s.startswith("["):  # IPv6: [::1]:1080
        host, _, rest = s[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = s.rpartition(":")
    return host, int(port or 0)


def _tls_from_query(q: dict[str, str], host: str) -> dict | None:
    security = (q.get("security") or "").lower()
    if security not in ("tls", "reality"):
        return None
    tls: dict[str, Any] = {"enabled": True}
    sni = q.get("sni") or q.get("peer")
    if sni:
        tls["server_name"] = sni
    elif host and not _is_ip(host):
        tls["server_name"] = host
    if q.get("alpn"):
        tls["alpn"] = [a for a in q["alpn"].split(",") if a]
    if q.get("allowInsecure") in ("1", "true") or q.get("insecure") in ("1", "true"):
        tls["insecure"] = True
    if q.get("fp"):
        tls["utls"] = {"enabled": True, "fingerprint": q["fp"]}
    if security == "reality" and q.get("pbk"):
        tls["reality"] = {
            "enabled": True,
            "public_key": q["pbk"],
            "short_id": q.get("sid", ""),
        }
    return tls


def _transport_from_query(q: dict[str, str]) -> dict | None:
    net = (q.get("type") or q.get("net") or "").lower()
    if net in ("", "tcp", "raw"):
        return None
    if net == "ws":
        tr: dict[str, Any] = {"type": "ws"}
        if q.get("path"):
            tr["path"] = urllib.parse.unquote(q["path"])
        if q.get("host"):
            tr["headers"] = {"Host": q["host"]}
        return tr
    if net == "grpc":
        tr = {"type": "grpc"}
        svc = q.get("serviceName") or q.get("path")
        if svc:
            tr["service_name"] = urllib.parse.unquote(svc)
        return tr
    if net == "httpupgrade":
        tr = {"type": "httpupgrade"}
        if q.get("path"):
            tr["path"] = urllib.parse.unquote(q["path"])
        if q.get("host"):
            tr["host"] = q["host"]
        return tr
    if net in ("http", "h2"):
        tr = {"type": "http"}
        if q.get("path"):
            tr["path"] = urllib.parse.unquote(q["path"])
        if q.get("host"):
            tr["host"] = [h for h in q["host"].split(",") if h]
        return tr
    # xhttp / kcp / quic / splithttp 等没有对应 transport，宁可不收
    raise ValueError(f"链接的 transport={net} 当前 sing-box 构建不支持，已跳过")


def _parse_socks_link(url: str, scheme: str) -> dict:
    p = urllib.parse.urlsplit(url)
    if not p.hostname or not p.port:
        raise ValueError("socks 链接缺少服务器或端口")
    out: dict[str, Any] = {
        "type": "socks",
        "server": p.hostname,
        "server_port": int(p.port),
        "version": {"socks4": "4", "socks4a": "4a"}.get(scheme, "5"),
    }
    if p.username:
        out["username"] = urllib.parse.unquote(p.username)
    if p.password:
        out["password"] = urllib.parse.unquote(p.password)
    return out


def _parse_http_link(url: str) -> dict:
    p = urllib.parse.urlsplit(url)
    if not p.hostname or not p.port:
        raise ValueError("http 链接缺少服务器或端口")
    out: dict[str, Any] = {"type": "http", "server": p.hostname, "server_port": int(p.port)}
    if p.username:
        out["username"] = urllib.parse.unquote(p.username)
    if p.password:
        out["password"] = urllib.parse.unquote(p.password)
    if p.scheme.lower() == "https":
        tls: dict[str, Any] = {"enabled": True}
        if not _is_ip(p.hostname):
            tls["server_name"] = p.hostname
        out["tls"] = tls
    return out


def _ss_legacy(body: str) -> tuple[str, str, str, int]:
    userinfo, _, hostpart = _b64_try_decode(body).rpartition("@")
    method, _, password = userinfo.partition(":")
    host, port = _split_host_port(hostpart)
    return method, password, host, port


def _parse_ss_link(url: str) -> dict:
    body = url[len("ss://"):]
    if "#" in body:
        body = body.split("#", 1)[0]
    plugin = ""
    if "@" in body:  # SIP002：base64(method:password) 或明文 userinfo
        userinfo, hostpart = body.rsplit("@", 1)
        if "?" in hostpart:
            hostpart, query = hostpart.split("?", 1)
            plugin = _qdict(query).get("plugin", "")
        if ":" in userinfo:
            method, _, password = userinfo.partition(":")
            method = urllib.parse.unquote(method)
            password = urllib.parse.unquote(password)
        else:
            method, _, password = _b64_try_decode(userinfo).partition(":")
        host, port = _split_host_port(hostpart)
    else:  # 旧式：base64(method:password@host:port)
        method, password, host, port = _ss_legacy(body)
    out: dict[str, Any] = {
        "type": "shadowsocks",
        "server": host,
        "server_port": int(port),
        "method": method.lower(),
        "password": password,
    }
    if plugin:
        parts = urllib.parse.unquote(plugin).split(";")
        out["plugin"] = parts[0]
        if len(parts) > 1:
            out["plugin_opts"] = ";".join(parts[1:])
    return out


def _parse_vmess_link(url: str) -> dict:
    payload = url[len("vmess://"):].split("#", 1)[0]
    data = json.loads(_b64_try_decode(payload))
    host = str(data.get("add", ""))
    out: dict[str, Any] = {
        "type": "vmess",
        "server": host,
        "server_port": int(data.get("port") or 0),
        "uuid": str(data.get("id", "")),
        "security": str(data.get("scy") or data.get("security") or "auto"),
        "alter_id": int(data.get("aid") or 0),
    }
    if str(data.get("tls", "")).lower() in ("tls", "1", "true"):
        tls: dict[str, Any] = {"enabled": True}
        sni = data.get("sni")
        if sni:
            tls["server_name"] = str(sni)
        elif host and not _is_ip(host):
            tls["server_name"] = host
        alpn = data.get("alpn")
        if alpn:
            tls["alpn"] = [a for a in str(alpn).split(",") if a]
        if str(data.get("verify_cert", "")).lower() in ("false", "0"):
            tls["insecure"] = True
        out["tls"] = tls
    path = str(data.get("path", ""))
    tr = _transport_from_query({
        "type": str(data.get("net", "")),
        "path": path,
        "host": str(data.get("host", "")),
        "serviceName": path,
    })
    if tr:
        out["transport"] = tr
    return out


def _parse_vless_link(url: str) -> dict:
    p = urllib.parse.urlsplit(url)
    if not p.hostname or not p.port:
        raise ValueError("vless 链接缺少服务器或端口")
    out: dict[str, Any] = {
        "type": "vless",
        "server": p.hostname,
        "server_port": int(p.port),
        "uuid": urllib.parse.unquote(p.username or ""),
        "packet_encoding": "xudp",
    }
    q = _qdict(p.query)
    if q.get("flow"):
        out["flow"] = q["flow"]
    tls = _tls_from_query(q, p.hostname)
    if tls:
        out["tls"] = tls
    tr = _transport_from_query(q)
    if tr:
        out["transport"] = tr
    return out


def _parse_trojan_link(url: str) -> dict:
    p = urllib.parse.urlsplit(url)
    if not p.hostname or not p.port:
        raise ValueError("trojan 链接缺少服务器或端口")
    out: dict[str, Any] = {
        "type": "trojan",
        "server": p.hostname,
        "server_port": int(p.port),
        "password": urllib.parse.unquote(p.username or ""),
    }
    q = _qdict(p.query)
    out["tls"] = _tls_from_query(q, p.hostname) or {"enabled": True}
    tr = _transport_from_query(q)
    if tr:
        out["transport"] = tr
    return out


def _parse_hysteria2_link(url: str) -> dict:
    p = urllib.parse.urlsplit(url)
    if not p.hostname or not p.port:
        raise ValueError("hysteria2 链接缺少服务器或端口")
    out: dict[str, Any] = {
        "type": "hysteria2",
        "server": p.hostname,
        "server_port": int(p.port),
        "password": urllib.parse.unquote(p.username or ""),
    }
    q = _qdict(p.query)
    tls: dict[str, Any] = {"enabled": True}
    sni = q.get("sni") or q.get("peer")
    if sni:
        tls["server_name"] = sni
    elif not _is_ip(p.hostname):
        tls["server_name"] = p.hostname
    if q.get("insecure") in ("1", "true"):
        tls["insecure"] = True
    if q.get("alpn"):
        tls["alpn"] = [a for a in q["alpn"].split(",") if a]
    out["tls"] = tls
    if q.get("obfs"):
        out["obfs"] = {
            "type": q["obfs"],
            "password": q.get("obfs-password") or q.get("obfs_password") or "",
        }
    for src, dst in (("up", "up_mbps"), ("down", "down_mbps")):
        if q.get(src):
            try:
                out[dst] = int(float(q[src]))
            except ValueError:
                pass
    return out


def _parse_tuic_link(url: str) -> dict:
    p = urllib.parse.urlsplit(url)
    if not p.hostname or not p.port:
        raise ValueError("tuic 链接缺少服务器或端口")
    out: dict[str, Any] = {
        "type": "tuic",
        "server": p.hostname,
        "server_port": int(p.port),
        "uuid": urllib.parse.unquote(p.username or ""),
        "password": urllib.parse.unquote(p.password or ""),
    }
    q = _qdict(p.query)
    tls: dict[str, Any] = {"enabled": True}
    sni = q.get("sni") or q.get("peer")
    if sni:
        tls["server_name"] = sni
    elif not _is_ip(p.hostname):
        tls["server_name"] = p.hostname
    if q.get("allow_insecure") in ("1", "true") or q.get("insecure") in ("1", "true"):
        tls["insecure"] = True
    if q.get("alpn"):
        tls["alpn"] = [a for a in q["alpn"].split(",") if a]
    out["tls"] = tls
    if q.get("congestion_control"):
        out["congestion_control"] = q["congestion_control"]
    if q.get("udp_relay_mode"):
        out["udp_relay_mode"] = q["udp_relay_mode"]
    return out


_PARSERS = {
    "socks": _parse_socks_link,
    "socks5": _parse_socks_link,
    "socks4": _parse_socks_link,
    "socks4a": _parse_socks_link,
    "http": _parse_http_link,
    "https": _parse_http_link,
    "ss": _parse_ss_link,
    "vmess": _parse_vmess_link,
    "vless": _parse_vless_link,
    "trojan": _parse_trojan_link,
    "hy2": _parse_hysteria2_link,
    "hysteria2": _parse_hysteria2_link,
    "tuic": _parse_tuic_link,
}


def parse_plain_links(text: str) -> list[str]:
    """从普通文本（每行一个 proxy:// 链接）中提取 URL。"""
    links = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(SUPPORTED_PREFIXES):
            links.append(line)
        elif "://" in line:
            # trap: 偶发链接带参数换行
            links.extend(
                part.strip() for part in line.split()
                if part.strip().startswith(SUPPORTED_PREFIXES)
            )
    return links


def url_to_outbound(url: str) -> dict:
    """把一条节点链接解析成 sing-box outbound（不含 tag）。"""
    scheme, sep, _ = url.partition("://")
    if not sep:
        raise ValueError(f"不是合法的节点链接: {url[:50]}")
    scheme = scheme.strip().lower()
    parser = _PARSERS.get(scheme)
    if parser is None:
        raise ValueError(f"暂不支持解析的链接类型: {scheme}://")
    if scheme.startswith("socks"):
        return parser(url, scheme)
    return parser(url)
