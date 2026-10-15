# -*- coding: utf-8 -*-
"""节点池的健康探测与故障转移。

selector 是**手动组**，自己不会故障转移（见 clashapi.py）。所以这里用
APScheduler 定时跑一个周期：

1. 并发探测「所有进了用户池的节点」，维护每个节点的连续成功 / 连续失败计数，
   顺带回写 nodes.healthy 供列表页显示红绿。
2. 对每个用户：
   - 当前选中的节点**连续失败到 FAIL_THRESHOLD** 才降级到池里优先级最高且
     可用的其它成员 —— 一次波动不切。
   - 当前节点恢复后，优先级比它高、且**连续成功到 RECOVER_THRESHOLD** 的
     成员把使用权抢回去 —— 回切也不来回抖。

切换走 Clash API，**不重写配置文件**。一旦重写，Supervisor 的 _running_digest
和磁盘摘要就不一致，页面会一直显示「配置未生效」。

要数「连续 N 次失败」就必须反复探测；探测范围严格限定在**池成员**，池外那一
百多个节点一个都不碰。
"""
from __future__ import annotations

import asyncio
import logging
import threading

import httpx

import config
from auth.allocator import check_node_connectivity_async
from converter import clashapi
from converter.configgen import GROUP_PREFIX
from storage import DB

logger = logging.getLogger("distributor.health")

# 每轮探测（含切换）持它；重载也持它，避免切换请求打到正在重启的 sing-box 上。
CYCLE_LOCK = threading.RLock()

_scheduler = None
_state_lock = threading.Lock()
_consecutive_fail: dict[str, int] = {}
_consecutive_ok: dict[str, int] = {}


def cycle_lock() -> threading.RLock:
    """给重载路径用：保证不和一轮探测/切换交叠。"""
    return CYCLE_LOCK


async def _probe(node: dict, use_clash: bool, client: httpx.AsyncClient,
                 sem: asyncio.Semaphore) -> bool:
    """探测单个节点。信号量限流，避免一轮里同时打开过多连接。"""
    async with sem:
        if use_clash:
            ok, ms = await clashapi.adelay(node["tag"], client=client)
            if ok:
                return ms is not None
            # 探测中途才发现接口不可用：本次退回 TCP，下一轮会自动改方式。
        return await check_node_connectivity_async(node)


async def _probe_all(nodes: dict[str, dict], watch: list[str],
                     use_clash: bool, limit: int) -> dict[str, bool]:
    """并发探测池内所有节点，返回 {tag: 是否健康}。

    用协程 + 信号量代替线程池：一个在途探测只占几 KB，比线程小两三个数量级，
    所以并发上限（config.HEALTH_MAX_WORKERS）可以开得比线程池大得多。
    """
    limit = max(1, limit)
    sem = asyncio.Semaphore(limit)
    limits = httpx.Limits(max_connections=limit,
                         max_keepalive_connections=limit)
    async with httpx.AsyncClient(limits=limits) as client:
        results = await asyncio.gather(
            *(_probe(nodes[t], use_clash, client, sem) for t in watch),
            return_exceptions=True,
        )
    out: dict[str, bool] = {}
    for tag, res in zip(watch, results):
        out[tag] = False if isinstance(res, BaseException) else bool(res)
    return out


def _switch(group: str, member: str, username: str, why: str) -> None:
    if clashapi.select(group, member):
        logger.warning("用户 %s %s -> %s", username, why, member)


def _rebalance(db: DB, nodes: dict[str, dict],
               fails: dict[str, int], oks: dict[str, int]) -> None:
    """按计数决定每个用户是否需要降级 / 回切。"""
    selection = clashapi.current_selection()
    if not selection:
        return
    for u in db.list_users(active_only=True):
        members = [t for t in (u.get("nodes") or []) if t in nodes]
        if not members:
            continue
        group = GROUP_PREFIX + u["username"]
        if group not in selection:
            # 运行中的配置里还没有这个组（多半是改了池但还没重载）。别硬切，
            # 否则每轮都会往一个不存在的组上打一次失败的请求。
            continue
        cur = selection.get(group) or members[0]
        if cur not in members:
            # 重载后 default 可能已经变了，以配置为准，下一轮再评估。
            continue

        if fails.get(cur, 0) >= config.FAIL_THRESHOLD:
            for t in members:
                if t != cur and oks.get(t, 0) > 0:
                    _switch(group, t, u["username"], "连续失败降级")
                    break
            continue

        # 自动回切：优先级比 cur 更高、且连续成功达标的成员。
        for t in members[:members.index(cur)]:
            if oks.get(t, 0) >= config.RECOVER_THRESHOLD:
                _switch(group, t, u["username"], "原节点恢复回切")
                break


def run_once(db: DB) -> None:
    """跑一轮：探测池成员 -> 更新计数与标注 -> 决定降级/回切。

    任何异常都在这里咽掉：这是定时作业，一次失败不该让调度器把作业丢掉。
    """
    with CYCLE_LOCK:
        try:
            _run_once(db)
        except Exception:
            logger.exception("健康探测这一轮失败（下轮继续）")


def _run_once(db: DB) -> None:
    if not clashapi.available():
        # sing-box 没在跑，或配置里没开 clash_api。此时探测没有意义，
        # 更不能把所有节点都记成失败。
        logger.debug("Clash API 不可达，本轮跳过")
        return

    nodes = {n["tag"]: n for n in db.get_nodes()}
    watch = sorted(db.pool_node_tags() & set(nodes))
    if not watch:
        return

    # 先探一个样本，决定这一轮用 Clash API 还是退回 TCP 拨测。
    use_clash, _ = clashapi.delay(watch[0])
    if not use_clash:
        logger.warning("Clash API 的 delay 接口不可用，改用 TCP 拨测（只能证明端口开着）")

    # 调度器跑在后台线程里（没有运行中的事件循环），所以这里可以安全地
    # 用 asyncio.run 起一个新 loop 跑这一轮的并发探测。
    healthy = asyncio.run(
        _probe_all(nodes, watch, use_clash, config.HEALTH_MAX_WORKERS)
    )

    with _state_lock:
        for tag, ok in healthy.items():
            if ok:
                _consecutive_ok[tag] = _consecutive_ok.get(tag, 0) + 1
                _consecutive_fail[tag] = 0
            else:
                _consecutive_fail[tag] = _consecutive_fail.get(tag, 0) + 1
                _consecutive_ok[tag] = 0
        fails = dict(_consecutive_fail)
        oks = dict(_consecutive_ok)

    for tag, ok in healthy.items():
        db.set_node_health(tag, ok)

    _rebalance(db, nodes, fails, oks)


def start(db: DB) -> None:
    """启动定时探测。重复调用无副作用。"""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler()
    # coalesce + max_instances=1：上一轮没跑完就不要叠下一轮。
    sched.add_job(
        run_once, "interval", seconds=config.HEALTH_INTERVAL, args=[db],
        id="node-health", max_instances=1, coalesce=True,
    )
    sched.start()
    _scheduler = sched
    logger.info(
        "健康探测已启动：每 %ds 一轮，最大并发 %d（协程），连续失败 %d 次降级 / 连续成功 %d 次回切",
        config.HEALTH_INTERVAL, config.HEALTH_MAX_WORKERS,
        config.FAIL_THRESHOLD, config.RECOVER_THRESHOLD,
    )


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
