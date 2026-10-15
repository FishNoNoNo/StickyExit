# -*- coding: utf-8 -*-
"""Clash API 客户端。

sing-box 的 `selector` 是**手动组** —— 没人动它就永远停在 default，哪怕那个
节点已经死了。Clash API 是唯一能在**不重启 sing-box** 的前提下改变 selector
选中项的手段，故障转移全靠它。（换用 urltest 倒是会自动切，但它按延迟挑、
无视池内优先级，而且单次探测失败就切，两个要求都不满足。）

只打回环地址：不要改 CLASH_API_HOST。
"""
from __future__ import annotations

import logging

import httpx

import config
from auth.allocator import check_node_connectivity

logger = logging.getLogger("distributor.clashapi")

# 实测（sing-box 1.15.0-alpha.10）：GET /proxies/{tag}/delay 走的是节点自己的
# 出口适配器，对纯直连节点不产生额外报文；对真实机场节点则真连一次、失败报
# 503。也就是说它比 TCP 拨测更能反映机场线路可用性。但它有两个坑，必须避开：
# 1. 返回 200 {delay} 时 delay 可能远低于真实（握手路径的因素），只当布尔用；
# 2. 请求本身各种原因可能不触发失败（观测下来不稳定），所以判定失败要保守。
#    宁可不切（多等一轮），也不能把所有节点误判成死的。

# 直连 / block 这类不出去的节点会靠这个下限兜底：接口能不能用是「有没有管线」，
# 与具体节点无关，只用第一个被探测的节点试一次。
_MODE_UNKNOWN = -1
_MODE_CLASH = 1
_MODE_TCP = 2


def _probe_params(url: str) -> dict:
    """delay 接口的查询参数；url 为空时用配置里的探测 URL。"""
    params: dict = {"timeout": int(config.HEALTH_PROBE_TIMEOUT * 1000)}
    url = url or config.HEALTH_PROBE_URL
    if url:
        params["url"] = url
    return params


def _parse_delay(r) -> tuple[bool, int | None]:
    """把 delay 接口的响应翻译成 (接口是否可用, 延迟ms)。"""
    if r.status_code in (404, 405, 501):
        return False, None            # 接口本身没有（tag 不存在也算）
    if r.status_code // 100 != 2:
        return True, None             # 接口在，是节点连不通（503 之类）
    try:
        ms = (r.json() or {}).get("delay")
    except Exception:
        return True, None
    return True, (int(ms) if ms else None)


def delay(tag: str, url: str = "") -> tuple[bool, int | None]:
    """让 sing-box 真连一次这个出站。返回 (接口是否可用, 延迟ms)。

    延迟只在接口可用且有值时用；失败的理解见模块注释。接口不可用 → False,
    None，调用方退回 TCP 拨测。
    """
    try:
        r = httpx.get(
            f"{_base()}/proxies/{tag}/delay",
            params=_probe_params(url),
            headers=_headers(),
            timeout=config.HEALTH_PROBE_TIMEOUT + 3.0,
        )
    except Exception:
        return False, None
    return _parse_delay(r)


async def adelay(tag: str, url: str = "",
                 client: httpx.AsyncClient | None = None) -> tuple[bool, int | None]:
    """delay() 的协程版：语义完全一致，只是走 async HTTP。

    传入共享 client 可复用连接池 —— 探测循环用它在一轮里并发打几百个节点，
    每个协程只占几 KB，不占线程栈。
    """
    path = f"{_base()}/proxies/{tag}/delay"
    kwargs = dict(params=_probe_params(url), headers=_headers(),
                  timeout=config.HEALTH_PROBE_TIMEOUT + 3.0)
    try:
        if client is not None:
            r = await client.get(path, **kwargs)
        else:
            async with httpx.AsyncClient() as c:
                r = await c.get(path, **kwargs)
    except Exception:
        return False, None
    return _parse_delay(r)


def _base() -> str:
    return f"http://{config.CLASH_API_HOST}:{config.CLASH_API_PORT}"


def _headers() -> dict:
    if config.CLASH_API_SECRET:
        return {"Authorization": f"Bearer {config.CLASH_API_SECRET}"}
    return {}


def select(group: str, member: str, timeout: float = 5.0) -> bool:
    """把 selector 组的选中项换成 member。成功返回 True。

    这是运行时切换：不重写配置文件、不重启进程，已建立的连接不受影响
    （selector 的 interrupt_exist_connections 是 false）。
    """
    try:
        r = httpx.put(
            f"{_base()}/proxies/{group}",
            json={"name": member},
            headers=_headers(),
            timeout=timeout,
        )
        if r.status_code // 100 != 2:
            logger.warning("切换 %s -> %s 失败: HTTP %s %s",
                           group, member, r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("切换 %s -> %s 失败: %s", group, member, e)
        return False


def available(timeout: float = 3.0) -> bool:
    """Clash API 是否可达（sing-box 在跑且开了 clash_api）。"""
    try:
        r = httpx.get(f"{_base()}/version", headers=_headers(), timeout=timeout)
        return r.status_code // 100 == 2
    except Exception:
        return False


def _base() -> str:
    return f"http://{config.CLASH_API_HOST}:{config.CLASH_API_PORT}"


def _headers() -> dict:
    if config.CLASH_API_SECRET:
        return {"Authorization": f"Bearer {config.CLASH_API_SECRET}"}
    return {}


def current_selection() -> dict[str, str]:
    """所有组的当前选中项 {组名: 成员名}；取不到返回 {}。

    这是「当前用的是哪个节点」的唯一真相 —— 探测循环每轮都重新读它，而不是
    自己在内存里记一份，省掉一整类「内存状态和 sing-box 不一致」的 bug
    （比如重载之后 selector 被重置回 default）。
    """
    try:
        r = httpx.get(f"{_base()}/proxies", headers=_headers(), timeout=5.0)
        if r.status_code // 100 != 2:
            return {}
        proxies = (r.json() or {}).get("proxies") or {}
        out: dict[str, str] = {}
        for name, info in proxies.items():
            now = (info or {}).get("now")
            if now:
                out[name] = now
        return out
    except Exception:
        return {}
