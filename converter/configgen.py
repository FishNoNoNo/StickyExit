# -*- coding: utf-8 -*-
"""生成 sing-box 配置（inbounds + outbounds + route）。

关键点：
- 单个 mixed 入站，users 非空 => 强制认证。
- 每个 active 用户一条 auth_user 路由规则 -> 各自节点。
- 节点 outbound 统一存 DB(outbound_json)，生成时直接回填。
- 只把「被路由规则引用」的节点写进 outbounds：check 是整份配置级别的，
  一个 sing-box 认不出的节点会让整份配置失效，所以没被引用的节点不该
  出现在配置里（导入时另有 drop_invalid_nodes 作为准入门槛）。
- 没有任何 active 用户时走"封闭模式"：清空入站用户列表并把 route.final
  指向 block。绝不留下 "users: [] + final: direct" 这种组合 —— 那等于
  一台对全网开放的匿名直连代理。
- 任何写盘前都交给 core.validate（sing-box check）做权威校验。
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

import config
from converter import core
from storage import DB

logger = logging.getLogger("distributor.configgen")


def build_config(
    users: Iterable[dict],
    nodes: Iterable[dict],
    listen_ip: str = config.LISTEN_IP,
    listen_port: int = config.LISTEN_PORT,
) -> dict:
    users = [u for u in users if u.get("status", "active") == "active"]
    nodes = list(nodes)

    inbound = {
        "type": "mixed",
        "tag": "mixed-in",
        "listen": listen_ip,
        "listen_port": listen_port,
    }

    # ---- 节点池：tag -> outbound ----
    # 这个映射有两个用途：① 判断某个 tag 是不是合法节点（路由规则能不能引用它）；
    # ② 查找被引用的节点该写什么 outbound。
    pool: dict[str, dict] = {}
    for n in nodes:
        tag = n["tag"]
        if tag in pool:
            continue
        pool[tag] = dict(n["outbound"], tag=tag)

    # ---- 只把「被路由规则引用」的节点写进 outbounds ----
    # 不再照抄整个节点池：check 是整份配置级别的，池子里任何一个 sing-box
    # 认不出的节点都会让整份配置失效，从而拖垮每一次建号 / 改密 / 重载。
    # 没人绑定的节点对运行毫无贡献，没有理由让它有这种杀伤力。
    def _outbounds(referenced: list[str]) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for tag in referenced:
            if tag in pool and tag not in seen:
                seen.add(tag)
                out.append(pool[tag])
        # 基础出口兜底（tag 与节点重名时不重复添加）
        for base_tag, base_type in (("direct", "direct"), ("block", "block")):
            if base_tag not in seen:
                seen.add(base_tag)
                out.append({"type": base_type, "tag": base_tag})
        return out

    # ---- 封闭模式：一个 active 用户都没有 ----
    # mixed 入站 users 为空 => sing-box 不做任何认证。若此时 final 仍是 direct，
    # 全世界都能拿这台机器当匿名直连代理用。停用最后一个用户就会踩到这个坑，
    # 所以这里显式锁死：清空入站用户 + 所有流量 block。
    if not users:
        logger.warning("没有任何 active 用户，代理入口进入封闭模式（拒绝所有流量）")
        return {
            "log": {"level": "error"},
            "inbounds": [{**inbound, "users": []}],
            "outbounds": _outbounds([]),
            "route": {"rules": [], "final": "block"},
        }

    route_rules: list[dict] = []
    routable: list[dict] = []
    for u in users:
        tag = u.get("node_tag")
        if not tag:
            logger.warning("用户 %s 没有绑定节点，该用户不会出现在配置里", u["username"])
            continue
        if tag not in pool:
            logger.warning("用户 %s 的节点 %s 不在节点池，忽略该路由", u["username"], tag)
            continue
        routable.append(u)
        route_rules.append({
            "inbound": ["mixed-in"],
            "auth_user": [u["username"]],
            "outbound": tag,
        })

    # 连节点都拿不到的用户同样不能进 users 列表：给他认证却无路由，
    # 会落到 final=direct 直连出去，等于开了个后门。
    inbound_users = [
        {"username": u["username"], "password": u["password"]}
        for u in routable
    ]

    if not inbound_users:
        logger.warning("所有 active 用户都无可用节点，代理入口进入封闭模式（拒绝所有流量）")
        return {
            "log": {"level": "error"},
            "inbounds": [{**inbound, "users": []}],
            "outbounds": _outbounds([]),
            "route": {"rules": [], "final": "block"},
        }

    return {
        "log": {"level": "error"},
        "inbounds": [{**inbound, "users": inbound_users}],
        "outbounds": _outbounds([u["node_tag"] for u in routable]),
        "route": {"rules": route_rules, "final": "direct"},
    }


def drop_invalid_nodes(nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 sing-box check 过滤节点，返回 (可用节点, 被剔除的节点)。

    给导入流程用的准入门槛：坏节点一旦入库，就会让此后每一次生成配置都
    失败（原因见 core.validate_outbounds）。宁可少几个节点，也不能让一个
    节点把整个面板拖成不可用。
    """
    nodes = list(nodes)
    ok = set(core.validate_outbounds([n["outbound"] for n in nodes]))
    good = [n for i, n in enumerate(nodes) if i in ok]
    bad = [n for i, n in enumerate(nodes) if i not in ok]
    if bad:
        logger.warning(
            "剔除 %d 个 sing-box 无法使用的节点: %s",
            len(bad), ", ".join(n.get("name", n["tag"]) for n in bad),
        )
    return good, bad


def write_config(cfg: dict, path=config.CONFIG_PATH) -> None:
    path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def generate_and_validate(db: DB) -> dict:
    """从 DB 组装完整配置 -> sing-box check 校验 -> 写入磁盘。

    任何一步失败都抛异常，调用方负责决定是否回滚到旧配置。
    """
    nodes = db.get_nodes()
    users = db.list_users(active_only=True)
    cfg = build_config(users, nodes)
    core.validate(cfg)
    write_config(cfg)
    return cfg
