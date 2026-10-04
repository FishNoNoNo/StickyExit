# -*- coding: utf-8 -*-
"""③-2 认证模块：账号管理（建号 / 停用 / 恢复 / 改密 / 删除）。

与 auth/allocator.py（分配）互补：allocator 决定"用哪个节点"，
这里决定"谁能用"。

停用的完整语义（改这一块是本次的重点）：
1. 从 active 集合移出 -> 生成的 sing-box 配置里不再有这个人；
2. 立即作废口令 -> 就算热重载失败，旧进程里的旧口令也不再匹配任何东西；
3. 改配置后必须真的重启成功 -> 由 Supervisor.reload 保证，重启失败要报错。
"""
from __future__ import annotations

import logging
import secrets

from auth.allocator import assign_node
from storage import DB

logger = logging.getLogger("distributor.auth")


def create_user(db: DB, username: str, nodes: list[dict],
                prefer: str | None = None,
                password: str | None = None) -> dict:
    """建号：自动分配一个节点（与其他用户不同）+ 随机口令。

    username 已存在或节点不足会抛 ValueError。
    """
    username = username.strip()
    if not username or any(c in username for c in ("@", ":", "/")):
        raise ValueError("用户名不能包含 @ : / 等字符")
    if db.get_user(username):
        raise ValueError(f"用户 {username} 已存在")

    nodes = nodes or db.get_nodes(healthy_only=True) or db.get_nodes()
    if not nodes:
        raise ValueError("节点池为空，请先添加/拉取节点")

    tag = assign_node(db, username, nodes, prefer=prefer)
    if not tag:
        raise ValueError("节点不足：所有节点已被占用或不可用")
    pw = password or secrets.token_urlsafe(16)
    return db.add_user(username, pw, tag)


def verify(db: DB, username: str, password: str) -> dict | None:
    """认证：校验账号密码是否有效。通过返回用户 dict，否则 None。"""
    u = db.get_user(username)
    if not u:
        return None
    if u["status"] != "active":
        return None
    if u["password"] != password:
        return None
    return u


def revoke(db: DB, username: str) -> bool:
    """停用账号：移出配置 + 作废口令。返回是否发生了状态变化。"""
    u = db.get_user(username)
    if not u:
        return False
    if u["status"] == "revoked":
        # 已经是停用态：仍强制作废一次口令（补做历史遗留的口令清理）
        db.revoke_user(username)
        return False
    db.revoke_user(username)
    logger.info("用户 %s 已停用，口令已作废", username)
    return True


def activate(db: DB, username: str) -> dict:
    """恢复一个被停用的用户，返回带新口令的用户记录。

    恢复时重新分配节点（原分配可能已失效），并发放全新口令 ——
    被停用期间泄露过的旧口令绝不复用。
    """
    u = db.get_user(username)
    if not u:
        raise ValueError(f"用户 {username} 不存在")
    if u["status"] == "active":
        raise ValueError(f"用户 {username} 已经是启用状态")

    nodes = db.get_nodes(healthy_only=True) or db.get_nodes()
    if not nodes:
        raise ValueError("节点池为空，无法恢复用户")

    tag = u["node_tag"] if u["node_tag"] and any(n["tag"] == u["node_tag"] for n in nodes) else None
    if not tag:
        tag = assign_node(db, username, nodes)
    if not tag:
        raise ValueError("节点不足：所有节点已被占用，无法恢复该用户")

    pw = secrets.token_urlsafe(16)
    db.activate_user(username, pw)
    db.set_user_node(username, tag)
    logger.info("用户 %s 已恢复，新节点 %s，新口令已发放", username, tag)
    u = db.get_user(username)
    assert u is not None
    return u


def delete_user(db: DB, username: str) -> bool:
    """彻底删除用户。返回是否真的删掉了。"""
    ok = db.delete_user(username)
    if ok:
        logger.info("用户 %s 已删除", username)
    return ok


def reset_password(db: DB, username: str, password: str | None = None) -> str:
    """重置密码，返回新密码。

    旧口令立刻失效（配置里就是明文比对），调用方需随后 reload。
    """
    u = db.get_user(username)
    if not u:
        raise ValueError(f"用户 {username} 不存在")
    pw = password or secrets.token_urlsafe(16)
    db.set_user_password(username, pw)
    logger.info("用户 %s 口令已重置", username)
    return pw


def proxy_endpoint(u: dict, host: str, port: int) -> str:
    """给用户看的可直接使用的代理地址。"""
    return f"http://{u['username']}:{u['password']}@{host}:{port}"
