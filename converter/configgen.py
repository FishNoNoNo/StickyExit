# -*- coding: utf-8 -*-
"""生成 sing-box 配置（inbounds + outbounds + route + experimental）。

关键点：
- 单个 mixed 入站，users 非空 => 强制认证。
- 每个 active 用户一个 `selector` 组 + 一条 auth_user 路由规则。组的
  outbounds 顺序就是池内优先级，default 取优先级最高的那个。
  为什么是 selector 而不是 urltest：池内优先级要能被表达（urltest 按延迟挑，
  无视顺序），而且降级要求「连续失败 N 次才切」（urltest 单次失败就切）。
  自动降级/回切由 converter/health.py 经 Clash API 在运行时做，不在这里。
- 节点 outbound 统一存 DB(outbound_json)，生成时直接回填。
- 只把「被引用的」节点写进 outbounds：check 是整份配置级别的，一个 sing-box
  认不出的节点会让整份配置失效，所以没被引用的节点不该出现在配置里
  （导入时另有 drop_invalid_nodes 作为准入门槛）。
- 没有任何 active 用户、或所有用户都没有可用节点池时走"封闭模式"：清空入站
  用户列表并把 route.final 指向 block。绝不留下 "users: [] + final: direct"
  这种组合 —— 那等于一台对全网开放的匿名直连代理。
- 任何写盘前都交给 core.validate（sing-box check）做权威校验。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

import config
from converter import core
from storage import DB

logger = logging.getLogger("distributor.configgen")

# 组 tag 的保留前缀。节点 tag 由 parser 生成、恒为 node-<8hex>，正常撞不上；
# 这里仍然显式挡一道，避免生成出自我引用 / 相撞的 outbound。
GROUP_PREFIX = "pool-"


def _clash_api_block() -> dict:
    """Clash API 只绑回环 —— 暴露出去等于把节点切换权交给任何人。"""
    return {
        "clash_api": {
            "external_controller": f"{config.CLASH_API_HOST}:{config.CLASH_API_PORT}",
            "secret": config.CLASH_API_SECRET,
        }
    }


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
    pool: dict[str, dict] = {}
    for n in nodes:
        tag = n["tag"]
        if not tag or tag.startswith(GROUP_PREFIX):
            logger.warning("节点 tag %r 占用了保留前缀 %s，跳过", tag, GROUP_PREFIX)
            continue
        if tag in pool:
            continue
        pool[tag] = dict(n["outbound"], tag=tag)

    # ---- 每个用户一个 selector 组 + 一条路由规则 ----
    # 池里已经不存在的成员直接剔除；剔空了就整个用户不进配置（下面封闭模式
    # 的判断会兜住这种情况）。
    groups: list[dict] = []
    route_rules: list[dict] = []
    routable: list[dict] = []
    member_tags: list[str] = []
    for u in users:
        members = [t for t in (u.get("nodes") or []) if t in pool]
        if not members:
            logger.warning("用户 %s 的节点池为空，该用户不会出现在配置里", u["username"])
            continue
        group_tag = GROUP_PREFIX + u["username"]
        routable.append(u)
        groups.append({
            "type": "selector",
            "tag": group_tag,
            "outbounds": members,
            "default": members[0],          # 优先级最高的成员
            "interrupt_exist_connections": False,   # 切换不打断已建立的连接
        })
        route_rules.append({
            "inbound": ["mixed-in"],
            "auth_user": [u["username"]],
            "outbound": group_tag,
        })
        member_tags.extend(members)

    def _outbounds(extra: dict[str, dict]) -> list[dict]:
        """只把「被引用的」出站写进配置，并去重。

        注意入参必须显式带上组本身：组 tag 不在节点 pool 里，只喂节点 tag 的
        话组会被静默丢掉，路由规则就引用了不存在的出站 -> check 失败 ->
        此后每一次建号 / 改密 / 重载都失败。
        """
        out: list[dict] = []
        seen: set[str] = set()
        for tag, ob in extra.items():
            if tag not in seen:
                seen.add(tag)
                out.append(ob)
        for base_tag, base_type in (("direct", "direct"), ("block", "block")):
            if base_tag not in seen:
                seen.add(base_tag)
                out.append({"type": base_type, "tag": base_tag})
        return out

    def _emit() -> list[dict]:
        referenced: dict[str, dict] = {g["tag"]: g for g in groups}
        for tag in member_tags:
            if tag in pool and tag not in referenced:
                referenced[tag] = pool[tag]
        return _outbounds(referenced)

    # ---- 封闭模式 ----
    # mixed 入站 users 为空 => sing-box 不做任何认证。若此时 final 仍是 direct，
    # 全世界都能拿这台机器当匿名直连代理用。停用最后一个用户就会踩到这个坑，
    # 所以这里显式锁死：清空入站用户 + 所有流量 block。
    if not routable:
        if users:
            logger.warning("所有 active 用户都没有可用节点池，代理入口进入封闭模式（拒绝所有流量）")
        else:
            logger.warning("没有任何 active 用户，代理入口进入封闭模式（拒绝所有流量）")
        return {
            "log": {"level": "error"},
            "experimental": _clash_api_block(),
            "inbounds": [{**inbound, "users": []}],
            "outbounds": _outbounds({}),
            "route": {"rules": [], "final": "block"},
        }

    inbound_users = [
        {"username": u["username"], "password": u["password"]}
        for u in routable
    ]

    # 不变式：发了认证的用户集合，必须和路由规则覆盖的用户集合完全一致。
    # 多一个「有认证但没路由」的用户，他就会落到 final=direct 直连出去 ——
    # 那是个后门。两者都由同一个 routable 列表生成，这里只是把不变式钉住。
    assert {u["username"] for u in routable} == {
        name for r in route_rules for name in r["auth_user"]
    }, "入站认证用户与路由规则不一致，会漏出直连后门"

    logger.info("配置生成: %d 个用户 / %d 个出站组 / %d 个节点",
                len(routable), len(groups), len(set(member_tags)))
    return {
        "log": {"level": "error"},
        "experimental": _clash_api_block(),
        "inbounds": [{**inbound, "users": inbound_users}],
        "outbounds": _emit(),
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


def serialize(cfg: dict) -> str:
    """配置的唯一序列化方式。

    写盘和「待同步配置」都走它，两份文件的内容才严格一致 —— 页面的
    「待重载」判定依赖这个一致性。
    """
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def _atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件再原子替换：中断也不会留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_config(cfg: dict, path=config.CONFIG_PATH) -> None:
    _atomic_write_text(path, serialize(cfg))


def build_from_db(db: DB) -> dict:
    """数据库当前状态对应的完整配置（不校验、不落盘）。"""
    return build_config(db.list_users(active_only=True), db.get_nodes())


def write_pending_from_db(db: DB) -> dict:
    """把数据库当前状态渲染成「待同步配置」。

    每个会改变配置的接口改完库后都必须调用；失败要往上抛，否则页面会把
    「库里已改、还没重载」误判成「已生效」。
    """
    cfg = build_from_db(db)
    write_config(cfg, config.CONFIG_PENDING_PATH)
    return cfg


def generate_and_validate(db: DB) -> dict:
    """从 DB 组装完整配置 -> sing-box check 校验 -> 写入磁盘。

    任何一步失败都抛异常，调用方负责决定是否回滚到旧配置。启动路径用它：
    应用用的配置和「待同步配置」一起落盘，两者天然一致。
    """
    cfg = build_from_db(db)
    core.validate(cfg)
    write_config(cfg)
    write_config(cfg, config.CONFIG_PENDING_PATH)
    return cfg
