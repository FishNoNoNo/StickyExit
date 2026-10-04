# -*- coding: utf-8 -*-
"""③ 认证模块：谁能用、用哪个节点。

账号(认证) + 分配(一人一节点)。对外提供：
- 建号 / 停用 / 恢复 / 删除 / 改密
- 给用户分配互不相同的节点
"""
from .allocator import (
    assign_node, ensure_all_users_have_node,
    check_node_connectivity, check_and_mark, reassign_dead,
)
from .users import (
    create_user, verify, revoke, activate, delete_user,
    reset_password, proxy_endpoint,
)

__all__ = [
    "assign_node", "ensure_all_users_have_node",
    "check_node_connectivity", "check_and_mark", "reassign_dead",
    "create_user", "verify", "revoke", "activate", "delete_user",
    "reset_password", "proxy_endpoint",
]
