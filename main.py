# -*- coding: utf-8 -*-
"""入口：python main.py 直接启动整个服务（FastAPI Web 管理 + sing-box 代理）。

不需要任何命令行参数：
1. 若节点池非空，生成 sing-box 配置并确保代理在跑（已有实例则复用）。
2. 用 uvicorn 启动 FastAPI Web 管理页（默认 0.0.0.0:5003），浏览器打开即可导入节点 / 建用户。

注意：启动时如果数据库里一个 active 用户都没有，生成的配置是"封闭模式"
（入站不认证 + 所有流量 block）。端口会开着但拒绝一切代理流量，
不会出现"裸奔的匿名直连代理"。
"""
from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

import config
from storage import DB


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    logger = logging.getLogger("distributor")
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(h)
    logger.propagate = False
    return logger


LOG = setup_logging()


def ensure_proxy(db: DB) -> bool:
    """节点池非空时：生成配置 → 校验 → 拉起/复用 sing-box。

    返回是否成功确保代理在运行；节点池为空时返回 False，
    等用户在 Web 页导入节点后会自动拉起。
    """
    from converter import Supervisor, generate_and_validate

    nodes = db.get_nodes()
    if not nodes:
        print("[启动] 节点池为空：请打开 Web 管理页导入节点（订阅链接 / Clash YAML / 节点链接）")
        return False

    active = db.list_users(active_only=True)
    try:
        cfg = generate_and_validate(db)
    except Exception as e:
        LOG.error("配置生成/校验失败: %s", e)
        print("[启动] 配置校验失败，先不启动代理；可在 Web 页重载或检查节点数据。")
        return False

    if not active:
        print("[启动] 当前没有启用的用户：代理以封闭模式启动（拒绝所有代理流量）")

    sv = Supervisor()
    try:
        sv.ensure_running()
    except Exception as e:
        LOG.error("启动 sing-box 失败: %s", e)
        print(f"[启动] sing-box 启动失败: {e}")
        return False

    alive = sv._is_alive()
    print(f"[启动] sing-box 代理: {'运行中' if alive else '未运行'}  {config.LISTEN_IP}:{config.LISTEN_PORT}")
    return alive


def main() -> int:
    db = DB()
    try:
        ensure_proxy(db)
    except Exception as e:
        LOG.error("启动代理失败: %s", e, exc_info=True)

    from web.server import create_app
    app = create_app(db)
    print(f"[启动] Web 管理页: http://{config.WEB_HOST}:{config.WEB_PORT}")
    print(f"[启动] 代理端口:   {config.LISTEN_IP}:{config.LISTEN_PORT}")
    advertise_ip = asyncio.run(config.get_advertise_ip())
    print(f"[启动] 用户连接:   http://用户:密码@{advertise_ip}:{config.LISTEN_PORT}")
    try:
        uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
