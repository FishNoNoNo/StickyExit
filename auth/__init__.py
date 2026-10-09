# -*- coding: utf-8 -*-
"""③ 认证模块：谁能用、用哪些节点。

账号(认证) + 分配(一池节点)。对外提供：
- 建号 / 停用 / 恢复 / 删除 / 改密
- 给用户分配一个有序节点池（池内顺序 = 优先级）
- 节点健康标注（手动全量探测）

注意：这里不负责运行时的降级 / 回切。那是 converter/health.py 通过
Clash API 做的事 —— 属于运行期行为，不属于分配。
"""
from .allocator import (
    assign_pool, prune_user_pools,
    check_node_connectivity, probe_many, check_all_and_mark,
)
from .users import (
    create_user, verify, revoke, activate, delete_user,
    reset_password, proxy_endpoint,
)

__all__ = [
    "assign_pool", "prune_user_pools",
    "check_node_connectivity", "probe_many", "check_all_and_mark",
    "create_user", "verify", "revoke", "activate", "delete_user",
    "reset_password", "proxy_endpoint",
]
