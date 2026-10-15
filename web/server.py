# -*- coding: utf-8 -*-
"""④ Web 管理：FastAPI 页面 + JSON API。

职责边界：页面/接口只编排 storage / auth / converter 三个模块，
自己不实现核心逻辑。所有变更都遵循同一条铁律：

    改数据库 -> 刷新「待同步配置」-> 手动重载 -> sing-box check -> 真正重启成功

重启失败必须返回错误并说明代理当前是停着的，绝不能"数据库改了就说成功"。

前端为 Vue3 + Tailwind + Element Plus 构建产物（web/static），由本服务直接托管。
"""
from __future__ import annotations

import hmac
import json
import logging
import threading

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from auth import (
    activate, create_user, delete_user, proxy_endpoint, reset_password,
    revoke,
)
from auth.allocator import check_all_and_mark
from converter import (
    Supervisor, SupervisorError, clashapi, health,
    drop_invalid_nodes,
)
from converter.configgen import GROUP_PREFIX, write_pending_from_db
from parser import (
    clash_node_to_outbound, fetch_nodes, link_name, normalize_outbound,
    parse_clash_yaml, parse_plain_links, url_to_outbound,
)
from storage import DB

logger = logging.getLogger("distributor.web")

_lock = threading.Lock()


class ConfigApplyError(RuntimeError):
    """配置没能落到正在跑的 sing-box 上。"""


class PendingSyncError(RuntimeError):
    """数据库改动没能同步到「待同步配置」，外层事务会回滚数据库。"""


def _pending_config(db: DB) -> dict:
    """取「待同步配置」：正常直接读 pending 文件，缺失/损坏才按 DB 重建。

    常态下每个会改配置的接口都已经把 pending 写好了；这里的重建只是兜底，
    避免升级 / 误删文件之后重载直接失败。
    """
    try:
        return json.loads(config.CONFIG_PENDING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("待同步配置缺失或损坏，按数据库重建")
        return write_pending_from_db(db)


def _apply(db: DB, action: str) -> dict:
    """把「待同步配置」应用到 sing-box：校验 -> 写 sing-box.json -> 重启。

    成功返回 {"ok": True}，失败抛 ConfigApplyError。调用方应据此回 5xx，
    因为此时"数据库里的改动"与"实际生效状态"可能已经不一致，
    必须让人知道，而不是假装成功。

    健康探测不在这里触发：手动点"检测节点"时才探测（全量，并发）。
    """
    try:
        cfg = _pending_config(db)
    except Exception as e:
        raise ConfigApplyError(f"待同步配置不可用: {e}") from e
    try:
        # 和健康探测互斥：别让一次切换请求打到正在重启的 sing-box 上。
        with health.cycle_lock():
            Supervisor().reload(cfg)
    except SupervisorError as e:
        # 旧实例已被停掉，代理现在处于停止状态：如实上报。
        logger.error("%s 后重载 sing-box 失败: %s", action, e)
        raise ConfigApplyError(
            f"{action}已写入数据库，但 sing-box 重载失败（代理当前未运行）：{e}"
        ) from e
    return {"ok": True}


def _token_ok(request: Request) -> bool:
    """管理 token 校验：URL ?token= 或请求头 X-Admin-Token。"""
    if not config.WEB_ADMIN_TOKEN:
        return True  # 未配置 = 不开启认证
    supplied = (
        ((request.query_params.get("token") or ""))
        or request.headers.get("X-Admin-Token", "")
    ).strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, config.WEB_ADMIN_TOKEN)


def _json(data, status=200):
    return JSONResponse(content=data, status_code=status)


def _unauthorized(message=None) -> JSONResponse:
    return _json({
        "ok": False,
        "error": message or "未授权：缺少或错误的 token（需 ?token=密码 或 X-Admin-Token 头）",
    }, 401)


def _error(message: str, status: int = 500) -> JSONResponse:
    return _json({"ok": False, "error": message}, status)


def create_app(db: DB | None = None) -> FastAPI:
    app = FastAPI(title="StickyExit", docs_url=None, redoc_url=None, openapi_url=None)

    def _config_dirty() -> bool:
        """「待同步配置」与已应用的配置是否不一致（页面提示「待重载」）。

        只比这两份文件：运行中的 sing-box 到底跑的是哪一份不重要 —— 点重载
        一定会重启它；这里要回答的是"库里改过、还没点重载"。
        """
        try:
            pending = json.loads(config.CONFIG_PENDING_PATH.read_text(encoding="utf-8"))
            applied = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True  # 读不到 / 坏文件：保守地认为需要重载
        return pending != applied

    state = {"db": db}

    def get_db() -> DB:
        if state["db"] is None:
            state["db"] = DB()
        return state["db"]

    def _sync_pending(db: DB) -> None:
        """数据库改完后立刻刷新「待同步配置」；失败必须让外层事务回滚。"""
        try:
            write_pending_from_db(db)
        except Exception as e:
            logger.exception("待同步配置写入失败")
            raise PendingSyncError(
                f"待同步配置写入失败，数据库改动已回滚：{e}"
            ) from e

    @app.exception_handler(PendingSyncError)
    async def _pending_sync_error(request: Request, exc: PendingSyncError):
        return _error(str(exc), 500)

    # ---------- 认证：全部 /api/* 统一加 token 校验 ----------
    @app.middleware("http")
    async def _require_admin_token(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            if not _token_ok(request):
                return _unauthorized()
        elif path == "/" and config.WEB_ADMIN_TOKEN and not _token_ok(request):
            return _unauthorized("401 Unauthorized")
        return await call_next(request)

    # ---------- 页面 ----------
    INDEX_HTML = config.BASE_DIR / "web" / "static" / "index.html"

    if not INDEX_HTML.exists():
        logger.warning(
            "前端产物不存在：%s（先在 web/frontend 执行 npm install && npm run build；"
            "Docker 部署还需把宿主机的 web/static 挂载到容器内这个路径）",
            INDEX_HTML,
        )

    def _frontend_missing():
        """报错时把「实际找的路径」带出来，方便区分没构建 / 没挂载 / 读不到。"""
        return _json({
            "ok": False,
            "error": f"前端产物不存在：{INDEX_HTML}",
            "hint": "先在 web/frontend 执行 npm install && npm run build；"
                    "Docker 部署请把宿主机的 web/static 挂到容器内这个路径，"
                    "并确认运行用户对该文件有读权限",
        }, 500)

    @app.get("/")
    async def index():
        if INDEX_HTML.exists():
            return FileResponse(INDEX_HTML)
        return _frontend_missing()

    # ---------- 状态 ----------
    @app.get("/api/status")
    async def api_status():
        _db = get_db()
        # 只取计数，不把整张表拉进内存（节点/用户可能很多）。
        node_total = _db.count_nodes()
        healthy_total = _db.count_nodes(healthy_only=True)
        active_total = _db.count_users(active_only=True)
        revoked_total = _db.count_users(active_only=False) - active_total
        supervisor = Supervisor()
        alive = supervisor._is_alive()
        out = {
            "listen": f"{config.LISTEN_IP}:{config.LISTEN_PORT}",
            "proxy": f"http://<user>:<pass>@{await config.get_advertise_ip()}:{config.LISTEN_PORT}",
            "nodes": node_total,
            "healthy_nodes": healthy_total,
            "users": active_total,
            "revoked_users": revoked_total,
            "sing_box_alive": alive,
            # 手动重载模式：「待同步配置」与已应用配置不一致时提示「待重载」。
            "config_dirty": not alive or _config_dirty(),
        }
        if alive:
            # 代理在跑但配置里一个 active 用户都没有 = 封闭模式，
            # 端口是开的但所有流量都会被拒。页面要能一眼看出这个状态。
            out["locked"] = active_total == 0
        return _json(out)

    # ---------- 节点：导入 ----------
    @app.post("/api/nodes/import")
    async def api_nodes_import(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        source_type = body.get("source_type", "url")  # url | text
        source = (body.get("source") or "").strip()
        if not source:
            return _json({"ok": False, "error": "source 为空"}, 400)

        # 抓取 / 解析订阅是网络 + CPU 操作，放在锁外，别让全局锁和事件循环干等。
        try:
            if source_type == "url":
                nodes = await fetch_nodes(source)
            else:
                nodes = _nodes_from_text(source)
        except Exception as e:
            logger.exception("导入节点失败")
            return _json({"ok": False, "error": str(e)}, 500)
        if not nodes:
            return _json({"ok": False, "error": "没有解析到节点"}, 400)

        # 准入门槛：先过一遍 sing-box check，把认不出的节点挡在库外。
        # check 是整份配置级别的，坏节点一旦入库会让此后每一次生成配置都失败。
        nodes, invalid = drop_invalid_nodes(nodes)
        if not nodes:
            return _json({
                "ok": False,
                "error": f"解析到 {len(invalid)} 个节点，但 sing-box 都无法使用，未导入任何节点",
            }, 400)

        with _lock:
            try:
                _db = get_db()
                # 增量合并：已有 tag 更新，新 tag 追加，绝不清空已有节点。
                # 添加节点不会删除任何旧节点，已有用户的节点池不受影响，
                # 所以这里不需要（更不应该）重写用户节点池。
                # 注意：只入库，不重载。运行中的 sing-box 还是旧配置，
                # 手动点"重载"后才生效。
                with _db.transaction():
                    added, updated = _db.add_nodes(nodes)
                    _sync_pending(_db)
                return _json({
                    "ok": True, "count": len(nodes), "added": added, "updated": updated,
                    "skipped": [n.get("name", n["tag"]) for n in invalid],
                    "pending_reload": True,
                })
            except Exception as e:
                logger.exception("导入节点失败")
                return _json({"ok": False, "error": str(e)}, 500)

    @app.get("/api/nodes")
    async def api_nodes(page: int = 1, page_size: int = 20, search: str = ""):
        _db = get_db()
        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        term = search.strip()
        total = _db.count_nodes(search=term)
        counts = _db.users_by_node()
        page_nodes = _db.get_nodes(limit=page_size, offset=(page - 1) * page_size,
                                   search=term)
        # 节点不暴露完整字段，outbound 只给摘要；users = 有多少个 active 用户的
        # 节点池包含这个节点（池之间允许重叠，所以这个数可能大于用户总数）。
        rows = [{
            "tag": n["tag"], "name": n["name"], "type": n["outbound"].get("type"),
            "server": n["outbound"].get("server"), "port": n["outbound"].get("server_port"),
            "healthy": bool(n.get("healthy")), "users": counts.get(n["tag"], 0),
        } for n in page_nodes]
        return _json({
            "nodes": rows, "total": total, "page": page, "page_size": page_size,
            "search": term,
        })

    @app.get("/api/nodes/options")
    async def api_node_options():
        """全部节点的轻量列表（只有 tag/name/healthy），供「节点池」编辑器使用。

        节点列表本身已分页，下拉不能只依赖当前页；这里只回三个字段，
        即便有几千个节点体积也很小。
        """
        _db = get_db()
        options = [{
            "tag": n["tag"], "name": n["name"], "healthy": bool(n.get("healthy")),
        } for n in _db.get_nodes()]
        return _json({"nodes": options})

    @app.post("/api/nodes/check")
    async def api_nodes_check():
        """手动全量健康检测：并发探测所有节点 -> 写回 healthy 标注。

        探测在锁外跑（纯网络读 + DB 写），不阻塞其它请求太久；
        触发锁唯一可能卡一下的是写同一行时的行锁，量级可忽略。
        """
        _db = get_db()
        result = check_all_and_mark(_db)
        # 结果里的 unhealthy 是节点名（列表页好懂）；数量上 healthy=总-坏
        return _json(result)

    @app.post("/api/nodes/{tag:path}/delete")
    async def api_node_delete(tag: str):
        """删除节点：把它从所有用户的节点池里摘掉。

        被摘空的池**不做处理** —— 池空着的用户就是不进配置，等人工决定要不要
        补节点。摘除和删除在同一个事务里完成（见 storage.delete_node）。
        运行中的 sing-box 在手动"重载"后才会更新。
        """
        _db = get_db()
        tag = tag.strip()
        with _lock:
            if not _db.get_node(tag):
                return _json({"ok": False, "error": f"节点 {tag} 不存在"}, 404)
            with _db.transaction():
                _, affected = _db.delete_node(tag)
                _sync_pending(_db)
            return _json({
                "ok": True, "tag": tag,
                "removed_from": affected,
                "pending_reload": True,
            })

    @app.post("/api/nodes/{tag:path}/update")
    async def api_node_update(tag: str, request: Request):
        """给节点改显示名。

        name 与 tag 解耦（tag 只由连接参数决定），所以改名不进 sing-box
        配置、不需要重载：节点列表 / 用户节点池读的都是库里的 name。
        """
        _db = get_db()
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str(body.get("name") or "").strip()[:200]
        if not name:
            return _json({"ok": False, "error": "名称不能为空"}, 400)
        tag = tag.strip()
        with _lock:
            if not _db.set_node_name(tag, name):
                return _json({"ok": False, "error": f"节点 {tag} 不存在"}, 404)
            # 改名不改变配置内容（name 不进 sing-box 配置），无需刷新待同步配置。
        return _json({"ok": True, "tag": tag, "name": name})

    # ---------- 用户 ----------
    @app.get("/api/users")
    async def api_users_get(page: int = 1, page_size: int = 20):
        _db = get_db()
        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        total = _db.count_users(active_only=False)
        users = []
        advertise_ip = await config.get_advertise_ip()
        all_nodes = _db.get_nodes()
        names = {n["tag"]: n["name"] for n in all_nodes}
        healthy = {n["tag"]: bool(n.get("healthy")) for n in all_nodes}
        # 每个用户当前实际走的是哪个成员（selector 的 now）。取不到就空着，
        # 「检测节点」之外它只用于展示。
        selection = clashapi.current_selection()
        for u in _db.list_users(active_only=False,
                                limit=page_size, offset=(page - 1) * page_size):
            users.append({
                "username": u["username"], "status": u["status"],
                # 按优先级排序的节点池；池可以为空。
                "nodes": [{"tag": t, "name": names.get(t, ""),
                           "healthy": healthy.get(t, False)}
                          for t in (u.get("nodes") or [])],
                "active_node": selection.get(GROUP_PREFIX + u["username"], ""),
                "created_at": u["created_at"], "revoked_at": u.get("revoked_at"),
                # 停用用户的代理地址不展示，因为口令已经作废。
                "proxy": proxy_endpoint(u, advertise_ip, config.LISTEN_PORT)
                         if u["status"] == "active" else "",
            })
        return _json({"users": users, "total": total, "page": page, "page_size": page_size})

    def _pool_detail(db: DB, tags: list[str]) -> list[dict]:
        """把一池 tag 补成 {tag, name, healthy}（供建号/恢复回包用，跟列表接口一致）。"""
        names = {n["tag"]: n["name"] for n in db.get_nodes()}
        healthy = {n["tag"]: bool(n.get("healthy")) for n in db.get_nodes()}
        return [{"tag": t, "name": names.get(t, ""), "healthy": healthy.get(t, False)}
                for t in tags]

    @app.post("/api/users")
    async def api_users_post(request: Request):
        """建号。两种入口两种初始池：

        - 不带 prefer：**空池**起步（节点池由用户自己在池编辑器里配）。
        - 带 prefer=<tag>（节点行「加用户」）：**单成员池 [tag]** —— 快捷把该
          节点放入其节点池首位。
        """
        _db = get_db()
        try:
            body = await request.json()
        except Exception:
            body = {}
        username = (body.get("username") or "").strip()
        prefer = body.get("prefer") or None
        advertise_ip = await config.get_advertise_ip()
        with _lock:
            try:
                with _db.transaction():
                    user = create_user(_db, username, prefer=prefer)
                    _sync_pending(_db)
            except ValueError as e:
                return _json({"ok": False, "error": str(e)}, 400)
            return _json({
                "ok": True,
                "user": {
                    "username": user["username"], "password": user["password"],
                    "nodes": _pool_detail(_db, user["nodes"]),
                    "proxy": proxy_endpoint(user, advertise_ip, config.LISTEN_PORT),
                },
                "pending_reload": True,
            })

    @app.post("/api/users/{username}/revoke")
    async def api_user_revoke(username: str):
        """停用：移出配置 + 立刻作废口令。

        成功意味着：数据库里没有此人 + 已落到正在跑的 sing-box 实例上。
        """
        _db = get_db()
        with _lock:
            if not _db.get_user(username):
                return _json({"ok": False, "error": "用户不存在"}, 404)
            # 只入库，不重载：停用也会立刻作废口令（revoke 已做）。
            # 运行中的 sing-box 还是旧配置，手动点"重载"后旧进程消失。
            with _db.transaction():
                revoke(_db, username)
                _sync_pending(_db)
            return _json({
                "ok": True,
                "username": username,
                "status": "revoked",
                "changed": True,
                "pending_reload": True,
            })

    @app.post("/api/users/{username}/activate")
    async def api_user_activate(username: str):
        """恢复：发放全新口令（旧口令永不恢复）。"""
        _db = get_db()
        advertise_ip = await config.get_advertise_ip()
        with _lock:
            try:
                with _db.transaction():
                    user = activate(_db, username)
                    _sync_pending(_db)
            except ValueError as e:
                return _json({"ok": False, "error": str(e)}, 400)
            return _json({
                "ok": True,
                "user": {
                    "username": user["username"], "password": user["password"],
                    # 和 GET /api/users 的 nodes 保持同形状：{tag, name, healthy}。
                    "nodes": _pool_detail(_db, user["nodes"]),
                    "proxy": proxy_endpoint(user, advertise_ip, config.LISTEN_PORT),
                },
                "pending_reload": True,
            })

    @app.post("/api/users/{username}/delete")
    async def api_user_delete(username: str):
        _db = get_db()
        with _lock:
            if not _db.get_user(username):
                return _json({"ok": False, "error": "用户不存在"}, 404)
            # 只入库，不重载：手动点"重载"后新配置（不含该用户）才生效。
            with _db.transaction():
                delete_user(_db, username)
                _sync_pending(_db)
            return _json({"ok": True, "pending_reload": True})

    @app.post("/api/users/{username}/password")
    async def api_user_password(username: str, request: Request):
        """重置口令：旧口令立即失效。"""
        _db = get_db()
        try:
            body = await request.json()
        except Exception:
            body = {}
        advertise_ip = await config.get_advertise_ip()
        with _lock:
            try:
                with _db.transaction():
                    pw = reset_password(_db, username, body.get("password"))
                    _sync_pending(_db)
            except ValueError as e:
                return _json({"ok": False, "error": str(e)}, 400)
            u = _db.get_user(username)
            return _json({
                "ok": True,
                "password": pw,
                "proxy": proxy_endpoint(u, advertise_ip, config.LISTEN_PORT) if u else "",
                "pending_reload": True,
            })

    @app.post("/api/users/{username}/pool")
    async def api_user_pool(username: str, request: Request):
        """整体替换用户的节点池。

        body: {"tags": [...]} —— **列表顺序就是优先级**（越靠前越优先）。
        允许空池（该用户会暂时不进配置）。池大小不设上限。
        """
        _db = get_db()
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = body.get("tags")
        if not isinstance(raw, list):
            return _json({"ok": False, "error": "tags 必须是数组"}, 400)
        with _lock:
            if not _db.get_user(username):
                return _json({"ok": False, "error": "用户不存在"}, 404)
            # 去重但保序：池里重复的 tag 会让生成的 selector 出现重复成员。
            ordered, seen = [], set()
            for t in raw:
                t = str(t or "").strip()
                if not t or t in seen:
                    continue
                seen.add(t)
                ordered.append(t)
            for t in ordered:
                if not _db.get_node(t):
                    return _json({"ok": False, "error": f"节点 {t} 不存在"}, 404)
            with _db.transaction():
                _db.set_user_nodes(username, ordered)
                _sync_pending(_db)
            # 只入库，不重载：手动点"重载"后新的优先级才生效。
            return _json({"ok": True, "tags": ordered, "pending_reload": True})

    # ---------- 重载 ----------
    @app.post("/api/reload")
    async def api_reload():
        _db = get_db()
        with _lock:
            try:
                _apply(_db, "重载")
            except ConfigApplyError as e:
                return _json({"ok": False, "error": str(e)}, 500)
            return _json({"ok": True})

    # ---------- 前端静态文件（Vue 构建产物） ----------
    static_dir = config.BASE_DIR / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/{full_path:path}")
    async def frontend_fallback(full_path: str):
        # 非 /api 路径全部回退到前端入口（支持 Vue 路由刷新直开）
        if full_path.startswith("api"):
            return _json({"ok": False, "error": "接口不存在"}, 404)
        if INDEX_HTML.exists():
            return FileResponse(INDEX_HTML)
        return _frontend_missing()

    return app


def _nodes_from_text(text: str) -> list:
    """从粘贴的文本解析节点：Clash YAML / 每行一个 proxy:// 链接 / base64。"""
    nodes = []
    seen = set()
    proxies = parse_clash_yaml(text)
    if proxies:
        for p in proxies:
            name = str(p.get("name", "")).strip()
            if any(k in name for k in ("流量", "重置", "到期", "套餐", "剩余", "时间")):
                continue
            try:
                node = normalize_outbound(clash_node_to_outbound(p), name)
            except Exception as e:
                print("skip", name, e)
                continue
            if node["tag"] in seen:
                continue
            seen.add(node["tag"])
            nodes.append(node)
    else:
        for u in parse_plain_links(text):
            try:
                out = url_to_outbound(u)
                node = normalize_outbound(out, link_name(u, out))
            except Exception as e:
                print("skip", u[:40], e)
                continue
            if node["tag"] in seen:
                continue
            seen.add(node["tag"])
            nodes.append(node)
    return nodes

