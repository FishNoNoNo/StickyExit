"""节点分配器：user ↔ 节点池 的映射与健康标注。

策略：
1. 一个用户一个池，**池内顺序就是优先级**（越靠前越优先）。
2. 池与池之间允许重叠 —— 单节点时代的「一个节点只能给一个人」独占语义已经
   整个去掉了（`used_node_tags` 随之删除）。
3. 新用户 → 按用户名做稳定轮转，从全量节点里取 k 个入池。
4. 删节点只摘不补：池里的失效成员被摘掉，摘空了就空着，不自动补人。
5. 这里只负责「标注」和「建池」，不做任何迁移。运行时的降级 / 回切由
   converter/health.py 通过 Clash API 完成 —— 那是运行期行为，不是分配行为。
"""
from __future__ import annotations

import logging
import socket
from typing import Iterable

import config
from storage import DB
import storage

logger = logging.getLogger("distributor.allocator")


def assign_pool(username: str, nodes: list[dict],
                k: int = config.POOL_SIZE,
                prefer: str | None = None) -> list[str]:
    """按用户名挑一个有序节点池。返回 [] 表示一个可用节点都没有。

    注意：建号 / 恢复现在都走「空池起步」（用户自己配池），这个函数目前并
    没有被调用 —— 留着以备将来「一键随机配池」之类的功能使用，逻辑经过
    验证可复用。

    轮转输入是**全量节点按 tag 排序**，不是健康子集：健康状态会被手动的
    「检测节点」按钮整批翻转，拿它当轮转输入，一次健康检查就能把所有人的
    池重排一遍，配置摘要跟着变，Supervisor 会平白无故重启 sing-box。

    哈希必须**稳定**：这里用 sum(ord(c))，不能用内置 hash() —— 后者每个
    进程加盐，重启一次池顺序就全变。

    prefer 命中的节点会被提到池首。
    """
    tags = sorted({n["tag"] for n in nodes})
    if not tags:
        return []
    k = max(1, min(k, len(tags)))
    start = sum(ord(c) for c in username) % len(tags)
    picked = [tags[(start + i) % len(tags)] for i in range(k)]
    if prefer and prefer in tags:
        picked = ([prefer] + [t for t in picked if t != prefer])[:k]
    return picked


def prune_user_pools(db: storage.DB, nodes: list[dict]) -> list[str]:
    """把池里已不存在于节点表的 tag 摘掉，返回被改动过的用户名。

    **只摘不补**：池被摘空就空着，那个用户暂时不进配置，等人工决定要不要补。

    正常删节点走的是 storage.delete_node（它在同一个事务里就把池摘干净了），
    这个函数是给「节点表被别的方式改动过」兜底。
    """
    live = {n["tag"] for n in nodes}
    changed: list[str] = []
    for u in db.list_users(active_only=True):
        pool = u.get("nodes") or []
        kept = [t for t in pool if t in live]
        if len(kept) != len(pool):
            db.set_user_nodes(u["username"], kept)
            logger.warning("用户 %s 的节点池摘掉了 %d 个已失效节点",
                           u["username"], len(pool) - len(kept))
            changed.append(u["username"])
    return changed


# ---------- 健康检查（手动全量探测）----------

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


def probe_many(db: storage.DB, nodes: list[dict]) -> list[str]:
    """并发探测节点并回写 healthy 标注，返回探测过的 tag（不受缓存约束）。

    不加缓存：这是"点击检测"的完整语义 —— 每个节点都真的来一遍，这样列表页
    的健康列反映的是这次点击时的真实可达性。
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
    """全量健康检测：并发探测所有节点并回写标注（供手动「检测节点」按钮用）。

    返回 {"checked": n, "healthy": m, "unhealthy": [name,...]}。
    """
    nodes = db.get_nodes()
    if not nodes:
        return {"checked": 0, "healthy": 0, "unhealthy": []}
    names = {n["tag"]: n["name"] for n in nodes}
    probed = probe_many(db, nodes)   # 探测结果已写回 DB
    dead = [tag for tag in probed if not db.get_node(tag)["healthy"]]
    return {"checked": len(probed), "healthy": len(probed) - len(dead),
            "unhealthy": [names[tag] for tag in dead]}
