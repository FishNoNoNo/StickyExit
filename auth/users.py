# -*- coding: utf-8 -*-
"""③-2 认证模块：账号管理（建号 / 停用 / 恢复 / 改密 / 删除）。

与 auth/allocator.py（分配）互补：allocator 决定「用哪些节点」，
这里决定「谁能用」。

停用的完整语义：
1. 从 active 集合移出 -> 生成的 sing-box 配置里不再有这个人；
2. 立即作废口令 -> 就算热重载失败，旧进程里的旧口令也不再匹配任何东西；
3. 改配置后必须真的重启成功 -> 由 Supervisor.reload 保证，重启失败要报错。

节点池：一个用户一个有序池，池内顺序就是优先级。池与池之间允许重叠。
"""
from __future__ import annotations

import logging
import secrets

from storage import DB

logger = logging.getLogger("distributor.auth")


def create_user(db: DB, username: str,
                password: str | None = None,
                prefer: str | None = None) -> dict:
    """建号。

    两个入口，两种初始池：
    - 「新建用户」（不带 prefer）：**空池**起步 —— 节点池由用户自己配（方案 B）。
    - 节点行的「加用户」（prefer=<tag>）：**单成员池 [tag]** —— 快捷把这个节点
      与用户绑定（想加更多，之后在池编辑器里加）。

    prefer 指向的节点必须存在，否则抛 ValueError。一个节点都没有时建号没有
    意义（配池时无节点可选），同样抛错提示先导入。
    """
    username = username.strip()
    if not username or any(c in username for c in ("@", ":", "/")):
        raise ValueError("用户名不能包含 @ : / 等字符")
    if db.get_user(username):
        raise ValueError(f"用户 {username} 已存在")
    if not db.get_nodes():
        raise ValueError("节点池为空，请先添加/拉取节点")

    tags: list[str] = []
    if prefer:
        if not db.get_node(prefer):
            raise ValueError(f"节点 {prefer} 不存在")
        tags = [prefer]

    pw = password or secrets.token_urlsafe(16)
    return db.add_user(username, pw, tags)


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

    和建号一样：**不自动分配节点**（方案 B）。池还在就原样保留（停用期间
    user_nodes 的行没被删）；池已空就保持空池 —— 恢复只发新口令，配池是
    用户自己的事。恢复时发放全新口令：被停用期间泄露过的旧口令绝不复用。
    """
    u = db.get_user(username)
    if not u:
        raise ValueError(f"用户 {username} 不存在")
    if u["status"] == "active":
        raise ValueError(f"用户 {username} 已经是启用状态")

    pw = secrets.token_urlsafe(16)
    db.activate_user(username, pw)
    logger.info("用户 %s 已恢复，新口令已发放", username)
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
