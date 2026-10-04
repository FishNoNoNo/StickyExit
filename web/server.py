# -*- coding: utf-8 -*-
"""④ Web 管理：FastAPI 页面 + JSON API。

职责边界：页面/接口只编排 storage / auth / converter 三个模块，
自己不实现核心逻辑。所有变更都遵循同一条铁律：

    改数据库 -> 生成配置 -> sing-box check -> 真正重启成功 -> 才返回成功

重启失败必须返回错误并说明代理当前是停着的，绝不能"数据库改了就说成功"。

与旧 Flask 版本的差异：Web 框架换成 FastAPI，/api/* 契约保持不变；
前端为 Vue3 + Tailwind + Element Plus 构建产物（web/static），由本服务直接托管。
"""
from __future__ import annotations

import hmac
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
from converter import Supervisor, SupervisorError, generate_and_validate
from parser import (
    clash_node_to_outbound, fetch_nodes, normalize_outbound,
    parse_clash_yaml, parse_plain_links,
)
from storage import DB

logger = logging.getLogger("distributor.web")

_lock = threading.Lock()


class ConfigApplyError(RuntimeError):
    """配置没能落到正在跑的 sing-box 上。"""


def _apply(db: DB, action: str) -> dict:
    """把数据库当前状态应用到 sing-box：生成 -> 校验 -> 重启。

    成功返回 {"ok": True}，失败抛 ConfigApplyError。调用方应据此回 5xx，
    因为此时"数据库里的改动"与"实际生效状态"可能已经不一致，
    必须让人知道，而不是假装成功。
    """
    try:
        cfg = generate_and_validate(db)
    except Exception as e:
        raise ConfigApplyError(f"配置生成/校验失败: {e}") from e
    try:
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

    state = {"db": db}

    def get_db() -> DB:
        if state["db"] is None:
            state["db"] = DB()
        return state["db"]

    # ---------- 认证：全部 /api/* 统一加 token 校验 ----------
    @app.middleware("http")
    async def _require_admin_token(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            if not _token_ok(request):
                return _unauthorized()
        elif path == "/" and config.WEB_ADMIN_TOKEN and not _token_ok(request):
            return _unauthorized("未授权：请在 URL 后加 ?token=密码 打开管理页")
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

        with _lock:
            try:
                _db = get_db()
                # 增量合并：已有 tag 更新，新 tag 追加，绝不清空已有节点。
                added, updated = _db.add_nodes(nodes)
                # 添加节点不会删除任何旧节点，已有用户的绑定不受影响，
                # 所以这里不需要（更不应该）重新分配用户节点。
                # 生成配置 + 校验 + 重启
                cfg = generate_and_validate(_db)
                Supervisor().reload(cfg)
                return _json({
                    "ok": True, "count": len(nodes), "added": added, "updated": updated,
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
        counts = _db.node_user_counts()
        page_nodes = _db.get_nodes(limit=page_size, offset=(page - 1) * page_size,
                                   search=term)
        # 节点不暴露完整字段，outbound 只给摘要
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
        """全部节点的轻量列表（只有 tag/name/healthy），供「换节点」下拉使用。

        节点列表本身已分页，下拉不能只依赖当前页；这里只回三个字段，
        即便有几千个节点体积也很小。
        """
        _db = get_db()
        options = [{
            "tag": n["tag"], "name": n["name"], "healthy": bool(n.get("healthy")),
        } for n in _db.get_nodes()]
        return _json({"nodes": options})

    @app.post("/api/nodes/{tag:path}/delete")
    async def api_node_delete(tag: str):
        """删除节点：绑在它上面的用户自动迁移到其他节点。

        节点消失后必须重新生成配置并重启成功才算删完，否则页面上看到的
        状态和正在跑的代理会不一致。
        """
        _db = get_db()
        tag = tag.strip()
        with _lock:
            if not _db.get_node(tag):
                return _json({"ok": False, "error": f"节点 {tag} 不存在"}, 404)
            _db.delete_node(tag)
            # 被删节点上可能还绑着用户：从剩余节点里重新分配；
            # 一个都分不出去的进 doomed，由前端明确告知。
            from auth.allocator import ensure_all_users_have_node
            changed, doomed = ensure_all_users_have_node(_db, _db.get_nodes())
            try:
                _apply(_db, f"删除节点 {tag}")
            except ConfigApplyError as e:
                return _json({"ok": False, "error": str(e)}, 500)
            return _json({
                "ok": True, "tag": tag,
                "migrated": [u["username"] for u in changed],
                "doomed": doomed,
            })

    # ---------- 用户 ----------
    @app.get("/api/users")
    async def api_users_get(page: int = 1, page_size: int = 20):
        _db = get_db()
        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        total = _db.count_users(active_only=False)
        users = []
        advertise_ip = await config.get_advertise_ip()
        for u in _db.list_users(active_only=False,
                                limit=page_size, offset=(page - 1) * page_size):
            node = _db.get_node(u["node_tag"]) if u["node_tag"] else None
            users.append({
                "username": u["username"], "status": u["status"],
                "node_tag": u["node_tag"], "node_name": node["name"] if node else "",
                "created_at": u["created_at"], "revoked_at": u.get("revoked_at"),
                # 停用用户的代理地址不展示，因为口令已经作废。
                "proxy": proxy_endpoint(u, advertise_ip, config.LISTEN_PORT)
                         if u["status"] == "active" else "",
            })
        return _json({"users": users, "total": total, "page": page, "page_size": page_size})

    @app.post("/api/users")
    async def api_users_post(request: Request):
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
                user = create_user(_db, username, _db.get_nodes(), prefer=prefer)
            except ValueError as e:
                return _json({"ok": False, "error": str(e)}, 400)
            try:
                _apply(_db, f"创建用户 {username}")
            except ConfigApplyError as e:
                return _json({"ok": False, "error": str(e)}, 500)
            return _json({
                "ok": True,
                "user": {
                    "username": user["username"], "password": user["password"],
                    "node_tag": user["node_tag"],
                    "proxy": proxy_endpoint(user, advertise_ip, config.LISTEN_PORT),
                },
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
            if revoke(_db, username):
                try:
                    _apply(_db, f"停用用户 {username}")
                except ConfigApplyError as e:
                    return _json({"ok": False, "error": str(e)}, 500)
            return _json({
                "ok": True,
                "username": username,
                "status": "revoked",
                "changed": True,
            })

    @app.post("/api/users/{username}/activate")
    async def api_user_activate(username: str):
        """恢复：发放全新口令（旧口令永不恢复）。"""
        _db = get_db()
        advertise_ip = await config.get_advertise_ip()
        with _lock:
            try:
                user = activate(_db, username)
            except ValueError as e:
                return _json({"ok": False, "error": str(e)}, 400)
            try:
                _apply(_db, f"恢复用户 {username}")
            except ConfigApplyError as e:
                return _json({"ok": False, "error": str(e)}, 500)
            return _json({
                "ok": True,
                "user": {
                    "username": user["username"], "password": user["password"],
                    "node_tag": user["node_tag"],
                    "proxy": proxy_endpoint(user, advertise_ip, config.LISTEN_PORT),
                },
            })

    @app.post("/api/users/{username}/delete")
    async def api_user_delete(username: str):
        _db = get_db()
        with _lock:
            if not _db.get_user(username):
                return _json({"ok": False, "error": "用户不存在"}, 404)
            if not delete_user(_db, username):
                return _json({"ok": False, "error": "删除失败"}, 404)
            try:
                _apply(_db, f"删除用户 {username}")
            except ConfigApplyError as e:
                return _json({"ok": False, "error": str(e)}, 500)
            return _json({"ok": True})

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
                pw = reset_password(_db, username, body.get("password"))
            except ValueError as e:
                return _json({"ok": False, "error": str(e)}, 400)
            try:
                _apply(_db, f"重置用户 {username} 口令")
            except ConfigApplyError as e:
                return _json({"ok": False, "error": str(e)}, 500)
            u = _db.get_user(username)
            return _json({
                "ok": True,
                "password": pw,
                "proxy": proxy_endpoint(u, advertise_ip, config.LISTEN_PORT) if u else "",
            })

    @app.post("/api/users/{username}/switch")
    async def api_user_switch(username: str, request: Request):
        _db = get_db()
        try:
            body = await request.json()
        except Exception:
            body = {}
        tag = (body.get("tag") or "").strip()
        with _lock:
            if not _db.get_user(username):
                return _json({"ok": False, "error": "用户不存在"}, 404)
            if not _db.get_node(tag):
                return _json({"ok": False, "error": f"节点 {tag} 不存在"}, 404)
            _db.set_user_node(username, tag)
            try:
                _apply(_db, f"切换用户 {username} 节点")
            except ConfigApplyError as e:
                return _json({"ok": False, "error": str(e)}, 500)
            return _json({"ok": True})

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
                from parser.fetcher import _url_to_outbound
                node = normalize_outbound(_url_to_outbound(u), u)
            except Exception as e:
                print("skip", u[:40], e)
                continue
            if node["tag"] in seen:
                continue
            seen.add(node["tag"])
            nodes.append(node)
    return nodes

