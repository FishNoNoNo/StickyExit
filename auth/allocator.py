"""节点分配器：user ↔ node 的映射与健康标注。

策略：
1. 已分配且节点存在的用户 → 保留原节点（避免刷新时频繁跳动）。
2. 新用户 → 从未被占用的健康节点里轮换挑选。
3. 用户数 > 节点数 → 抛出明确错误，由 CLI 提示限号/扩容。
4. 每次应用配置前，先探测「被绑定节点」的健康并回写 DB 标注。
   探测只标注，不做自动迁移 —— 死节点等做节点操作时再手动处理。
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Iterable

import config
from storage import DB
import storage

logger = logging.getLogger("distributor.allocator")


def _node_identity(node: dict) -> tuple:
    out = node["outbound"]
    return (out.get("server"), out.get("server_port"))


def assign_node(db: storage.DB, username: str, nodes: list[dict], prefer: str | None = None) -> str | None:
    """为用户名分配一个节点 tag。返回 None 表示无可供分配的健康节点。"""
    if prefer:
        for n in nodes:
            if n["tag"] == prefer:
                return prefer

    used = db.used_node_tags()
    # 只从健康节点里挑。调用方应传"健康的那批"（比如 nodes_filtered by
    # healthy），但这里也兜底过滤一遍：死节点不该被当成可分配资源。
    healthy = [n for n in nodes if n.get("healthy", True)]
    free = [n for n in healthy if n["tag"] not in used]
    if not free:
        free = healthy  # 退化：健康那批没人可独占时允许复用，交给调用方决定是否拒绝
    if not free:
        return None
    # 稳定轮换：按 tag 排序取 hash，让不同新用户尽量错开
    index = sum(ord(c) for c in username) % len(free)
    return free[index]["tag"]


def ensure_all_users_have_node(db: storage.DB, nodes: list[dict]) -> tuple[list[dict], list[str]]:
    """保证所有 active 用户都有节点；返回 (需要更新的用户, 无可分配的用户名)。"""
    node_by_tag = {n["tag"]: n for n in nodes}
    users = db.list_users(active_only=True)
    changed: list[dict] = []
    doomed: list[str] = []

    for u in users:
        current = u["node_tag"]
        if current and current in node_by_tag:
            continue
        # 原节点失效/被删 → 重新分配
        if current:
            logger.warning("用户 %s 的原节点 %s 已失效，重新分配", u["username"], current)
        free_tags = [t for t in node_by_tag if t not in db.used_node_tags() or t == current]
        candidates = [n for n in nodes if n["tag"] in free_tags]
        new_tag = assign_node(db, u["username"], candidates or nodes)
        if not new_tag:
            doomed.append(u["username"])
            continue
        db.set_user_node(u["username"], new_tag)
        u["node_tag"] = new_tag
        changed.append(u)

    return changed, doomed


# ---------- 健康检查 ----------

_health_cache: dict[str, tuple[float, bool]] = {}
_health_lock = threading.Lock()


def check_node_connectivity(node: dict, timeout: float = config.NODE_CHECK_TIMEOUT) -> bool:
    """TCP 拨测节点服务器：通 = 服务器活着（不代表机场线路可用）。"""
    out = node["outbound"]
    host = str(out.get("server", ""))
    port = int(out.get("server_port") or 0)
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as e:
        logger.debug("节点 %s 不可达: %s", node["tag"], e)
        return False


def check_and_mark(db: storage.DB, nodes: list[dict]) -> None:
    """探测节点健康并回写标注；结果缓存 300 秒避免重复探测。

    只做标注，不做任何迁移：节点挂掉不等于立刻要动用户（用户可能等
    手动操作时一起处理），这里只负责让 healthy 列反映真实可达性。
    """
    import time as _time
    now = _time.monotonic()
    with _health_lock:
        for n in nodes:
            cached = _health_cache.get(n["tag"])
            ok = False
            if cached and now - cached[0] < 300:
                ok = cached[1]
            else:
                ok = check_node_connectivity(n)
                _health_cache[n["tag"]] = (now, ok)
            db.set_node_health(n["tag"], ok)


def reassign_dead(db: storage.DB, nodes: list[dict]) -> list[dict]:
    """把绑定在不可达节点上的用户迁移到其他活跃节点。返回被迁移的用户。"""
    healthy = [n for n in nodes if n.get("healthy", True)]
    healthy_tags = {n["tag"] for n in healthy}
    migrated: list[dict] = []
    for u in db.list_users(active_only=True):
        tag = u["node_tag"]
        if tag and tag in healthy_tags:
            continue
        # prefer 指回原节点。candidates 已排除原节点，所以 prefer 只在
        # "原节点仍健康、但据 DB 状态该迁移"时不命中；不会导致
        # 把用户留在死节点上。
        candidates = [n for n in healthy if n["tag"] != tag]
        new_tag = assign_node(db, u["username"], candidates or healthy,
                              prefer=tag if tag not in healthy_tags else None)
        if not new_tag and tag:
            continue  # 无健康节点可切，保持原样等恢复
        if new_tag and new_tag != tag:
            db.set_user_node(u["username"], new_tag)
            u["node_tag"] = new_tag
            u["_migrated"] = True
            logger.warning("用户 %s 从 %s 迁移到 %s", u["username"], tag, new_tag)
            migrated.append(u)
    return migrated


def check_bound_and_mark(db: storage.DB, nodes: list[dict]) -> list[str]:
    """只探测「有 active 用户绑定」的节点并回写健康标注，返回探测过的 tag。

    只标注、不迁移。配合"点击检测"的周期内高频调用：绑定节点少，
    且 300 秒缓存让重复调用几乎零成本 —— 保留它给未来接入后台定时器用。
    """
    used = db.used_node_tags()
    probed: list[str] = []
    for n in nodes:
        if n["tag"] in used:
            check_and_mark(db, [n])
            probed.append(n["tag"])
    return probed


def _probe_many(db: storage.DB, nodes: list[dict]) -> list[str]:
    """并发探测节点健康并回写标注，返回探测过的 tag（不再受 300s 缓存约束）。

    这是"点击检测"的完整语义：不管缓存，每个节点都真的来一遍，这样
    列表页的健康列反映的是这次点击时的真实可达性。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    probed: list[str] = []
    if not nodes:
        return probed
    with ThreadPoolExecutor(max_workers=min(20, len(nodes))) as pool:
        futures = {
            pool.submit(check_node_connectivity, n): n["tag"] for n in nodes
        }
        for fut in as_completed(futures):
            tag = futures[fut]
            try:
                ok = fut.result()
            except Exception:
                ok = False
            db.set_node_health(tag, ok)
            probed.append(tag)
    return probed


def check_all_and_mark(db: storage.DB) -> dict:
    """全量健康检测：并发探测所有节点并回写标注（供手动"检测节点"按钮用）。

    返回 {"checked": n, "healthy": m, "unhealthy": [name,...]}。
    不加缓存：每次点击都是独立一次真实探测；标注写进 nodes.healthy，
    前端列表据此显示红绿。
    """
    nodes = db.get_nodes()
    if not nodes:
        return {"checked": 0, "healthy": 0, "unhealthy": []}
    names = {n["tag"]: n["name"] for n in nodes}
    probed = _probe_many(db, nodes)   # 探测结果已写回 DB
    dead = [tag for tag in probed if not db.get_node(tag)["healthy"]]
    return {"checked": len(probed), "healthy": len(probed) - len(dead),
            "unhealthy": [names[tag] for tag in dead]}