"""存储层：SQLite。用户、节点、节点池记录，全部持久化。

设计要点：
- 所有写操作走单例连接 + 事务，进程内线程安全。
- 节点表存 outbound_json，导入时按 tag 增量合并：已有节点更新，缺失节点新增，
  不会因为一次导入就把之前拉到的节点删掉。
- 用户 → 节点池的归属存在 user_nodes 表，它是唯一权威：一个用户可以有多个
  节点，position 就是池内优先级（越小越优先）。
- users.status 只有 'active' / 'revoked' 两种；停用 = 移出 sing-box 配置 + 立刻作废口令。
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from typing import Any

import config

logger = logging.getLogger("distributor.storage")


def _invalid_password() -> str:
    """停用时用来顶替原口令的随机串。

    必须每次不同且不可预测：既让旧口令立刻作废，又保证即使有人后来
    看到 sing-box 日志/配置残留，也无法反推出原口令。
    """
    return "revoked-" + secrets.token_urlsafe(24)


class DB:
    def __init__(self, path=config.DB_PATH):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    tag       TEXT PRIMARY KEY,
                    name      TEXT NOT NULL,
                    outbound  TEXT NOT NULL,
                    healthy   INTEGER NOT NULL DEFAULT 1,
                    last_check TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    username   TEXT UNIQUE NOT NULL,
                    password   TEXT NOT NULL,
                    node_tag   TEXT,
                    status     TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_users_node ON users(node_tag);
                CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

                CREATE TABLE IF NOT EXISTS user_nodes (
                    username TEXT NOT NULL,
                    tag      TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (username, tag)
                );
                CREATE INDEX IF NOT EXISTS idx_user_nodes_tag ON user_nodes(tag);
                """
            )
            self._migrate_users()
            self._migrate_user_nodes()

    def _migrate_users(self):
        """给旧库补列（ALTER TABLE ADD COLUMN 幂等，重复启动不会报错）。"""
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, ddl in (("updated_at", "TEXT"), ("revoked_at", "TEXT")):
            if col not in have:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    def _migrate_user_nodes(self):
        """把 users.node_tag 灌进 user_nodes（幂等）。

        只在「该用户在 user_nodes 里一行都没有」时才灌，所以重复启动不会
        覆盖后来在前端改过的池。
        """
        rows = self._conn.execute(
            "SELECT username, node_tag FROM users "
            "WHERE node_tag IS NOT NULL AND node_tag != '' "
            "AND username NOT IN (SELECT username FROM user_nodes)"
        ).fetchall()
        if not rows:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO user_nodes(username, tag, position) VALUES (?, ?, 0)",
            [(r[0], r[1]) for r in rows],
        )
        logger.info("已把 %d 个用户的单节点分配迁移为节点池", len(rows))

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------- nodes ----------
    def add_nodes(self, nodes: list[dict]) -> tuple[int, int]:
        """增量合并节点：按 tag upsert，返回 (新增数, 更新数)。

        绝不 DELETE 全表：一次订阅源拉取失败/半截，不会把已有节点抹掉。
        用 ON CONFLICT DO UPDATE 而不是 INSERT OR REPLACE，避免把 healthy /
        last_check 这两列冲回默认值。
        """
        added = 0
        updated = 0
        rows = [
            (n["tag"], n.get("name", n["tag"]),
             json.dumps(n["outbound"], ensure_ascii=False))
            for n in nodes
        ]
        with self._lock, self._conn:
            for tag, name, outbound in rows:
                exists = self._conn.execute(
                    "SELECT 1 FROM nodes WHERE tag = ?", (tag,)
                ).fetchone()
                self._conn.execute(
                    "INSERT INTO nodes(tag, name, outbound) VALUES (?, ?, ?) "
                    "ON CONFLICT(tag) DO UPDATE SET name = excluded.name, "
                    "outbound = excluded.outbound",
                    (tag, name, outbound),
                )
                if exists:
                    updated += 1
                else:
                    added += 1
        return added, updated

    @staticmethod
    def _node_where(healthy_only: bool, search: str | None) -> tuple[str, list]:
        """节点列表的过滤条件：健康 + 关键词（名称 / tag / 服务器等）。

        关键词直接 LIKE 原始的 outbound JSON，这样能按服务器地址、端口搜，
        又不必依赖 SQLite 的 JSON1 扩展。% 和 _ 做转义，避免被当通配符。
        """
        conds: list[str] = []
        args: list = []
        if healthy_only:
            conds.append("healthy = 1")
        term = (search or "").strip()
        if term:
            like = "%" + (
                term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            ) + "%"
            conds.append(
                "(tag LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
                "OR outbound LIKE ? ESCAPE '\\')"
            )
            args += [like, like, like]
        return (" WHERE " + " AND ".join(conds)) if conds else "", args

    def get_nodes(self, healthy_only: bool = False, limit: int | None = None,
                  offset: int = 0, search: str | None = None) -> list[dict]:
        where, args = self._node_where(healthy_only, search)
        q = "SELECT * FROM nodes" + where + " ORDER BY tag"
        if limit is not None:
            q += " LIMIT ? OFFSET ?"
            args = args + [limit, max(0, offset)]
        with self._lock:
            rows = self._conn.execute(q, tuple(args)).fetchall()
        return [self._node_from_row(r) for r in rows]

    def count_nodes(self, healthy_only: bool = False, search: str | None = None) -> int:
        where, args = self._node_where(healthy_only, search)
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM nodes" + where, tuple(args)).fetchone()[0]
            )

    def get_node(self, tag: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE tag = ?", (tag,)
            ).fetchone()
        return self._node_from_row(row) if row else None

    def set_node_health(self, tag: str, healthy: bool, detail: str = ""):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE nodes SET healthy = ?, last_check = ? WHERE tag = ?",
                (1 if healthy else 0, time.strftime("%Y-%m-%d %H:%M:%S") + " " + detail, tag),
            )

    def delete_node(self, tag: str) -> tuple[bool, list[str]]:
        """删除节点，并把它从所有用户的节点池里摘掉。

        返回 (是否真的删掉了, 受影响用户名)。被摘空的池**不会**自动补节点 ——
        因删节点导致的空池不做处理，该用户就是暂时不进配置。
        """
        with self._lock, self._conn:
            affected = [r[0] for r in self._conn.execute(
                "SELECT username FROM user_nodes WHERE tag = ? ORDER BY username", (tag,)
            ).fetchall()]
            cur = self._conn.execute("DELETE FROM nodes WHERE tag = ?", (tag,))
            self._conn.execute("DELETE FROM user_nodes WHERE tag = ?", (tag,))
        return cur.rowcount > 0, affected

    @staticmethod
    def _node_from_row(row) -> dict:
        r = dict(zip(("tag", "name", "outbound", "healthy", "last_check"), row))
        r["outbound"] = json.loads(r["outbound"])
        return r

    # ---------- users ----------
    #
    # 停用（revoke）的语义：一个用户在系统里"能用"的唯一凭据就是
    # (username, password) 这对 sing-box mixed 入站认证的凭据。因此停用必须
    # ① 把 status 改成 revoked（不再出现在生成的配置里）
    # ② 立刻作废 password（即使热重载失败，旧进程里的口令也失去意义）
    # ③ 打上时间戳，便于审计
    # 三者缺一，停用就只是"数据库里的一行字"。

    def add_user(self, username: str, password: str,
                 node_tags: list[str] | None = None) -> dict:
        """建号 + 写节点池，在同一个事务里完成。

        分两次写的话，第二次失败就会留下一个「active 但没有池」的用户，
        而他会静默地从生成的配置里消失。
        """
        tags = _dedup(node_tags or [])
        with self._lock, self._conn:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            cur = self._conn.execute(
                "INSERT INTO users(username, password, status, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?)",
                (username, password, now, now),
            )
            uid = cur.lastrowid
            if tags:
                self._conn.executemany(
                    "INSERT INTO user_nodes(username, tag, position) VALUES (?, ?, ?)",
                    [(username, t, i) for i, t in enumerate(tags)],
                )
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (uid,)
            ).fetchone()
        u = self._user_from_row(row)
        u["nodes"] = tags
        logger.info("新增用户 %s -> 节点池 %s", u["username"], tags or "(空)")
        return u

    def get_user(self, username: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            if not row:
                return None
            u = self._user_from_row(row)
            u["nodes"] = self._read_pool(username)
        return u

    def list_users(self, active_only: bool = True, limit: int | None = None,
                   offset: int = 0) -> list[dict]:
        q = "SELECT * FROM users"
        args: tuple = ()
        if active_only:
            q += " WHERE status = 'active'"
        q += " ORDER BY id"
        if limit is not None:
            q += " LIMIT ? OFFSET ?"
            args = (limit, max(0, offset))
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
            users = [self._user_from_row(r) for r in rows]
            self._attach_pools(users)
        return users

    def _read_pool(self, username: str) -> list[str]:
        """读一个用户的节点池，按优先级（position 升序）。调用方需持锁。"""
        return [r[0] for r in self._conn.execute(
            "SELECT tag FROM user_nodes WHERE username = ? ORDER BY position, tag",
            (username,),
        ).fetchall()]

    def _attach_pools(self, users: list[dict]) -> None:
        """给一页用户批量挂上节点池，避免 N+1。调用方需持锁。"""
        if not users:
            return
        by_user: dict[str, list[str]] = {u["username"]: [] for u in users}
        names = list(by_user)
        marks = ",".join("?" * len(names))
        rows = self._conn.execute(
            f"SELECT username, tag FROM user_nodes WHERE username IN ({marks}) "
            "ORDER BY position, tag",
            names,
        ).fetchall()
        for username, tag in rows:
            by_user[username].append(tag)
        for u in users:
            u["nodes"] = by_user[u["username"]]

    def count_users(self, active_only: bool = True) -> int:
        q = "SELECT COUNT(*) FROM users"
        if active_only:
            q += " WHERE status = 'active'"
        with self._lock:
            return int(self._conn.execute(q).fetchone()[0])

    def set_user_nodes(self, username: str, tags: list[str]) -> list[str]:
        """整体替换一个用户的节点池。列表顺序就是优先级（越靠前越优先）。

        先 dedup：池里的重复 tag 会让生成的 selector 出现重复成员。
        """
        tags = _dedup(tags)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM user_nodes WHERE username = ?", (username,))
            if tags:
                self._conn.executemany(
                    "INSERT INTO user_nodes(username, tag, position) VALUES (?, ?, ?)",
                    [(username, t, i) for i, t in enumerate(tags)],
                )
            self._conn.execute(
                "UPDATE users SET updated_at = ? WHERE username = ?",
                (time.strftime("%Y-%m-%d %H:%M:%S"), username),
            )
        return tags

    def set_user_password(self, username: str, password: str):
        """改口令。要求调用方随后重载配置，否则旧口令在跑的进程里仍然有效。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE users SET password = ?, updated_at = ? WHERE username = ?",
                (password, time.strftime("%Y-%m-%d %H:%M:%S"), username),
            )

    def revoke_user(self, username: str):
        """停用：移出 active 集合 + 随机作废口令（幂等）。"""
        with self._lock, self._conn:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            self._conn.execute(
                "UPDATE users SET status = 'revoked', password = ?, revoked_at = ?, "
                "updated_at = ? WHERE username = ?",
                (_invalid_password(), now, now, username),
            )

    def activate_user(self, username: str, password: str):
        """恢复一个停用用户：重新发一个全新口令并放回 active。"""
        with self._lock, self._conn:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            self._conn.execute(
                "UPDATE users SET status = 'active', password = ?, revoked_at = NULL, "
                "updated_at = ? WHERE username = ?",
                (password, now, username),
            )

    def delete_user(self, username: str) -> bool:
        """彻底删除用户（含口令、节点池与历史）。返回是否真的删掉了。

        不删 user_nodes 会留下孤儿行：既污染 users_by_node() 的计数，又会
        在同名用户重建时把旧池捞回来。
        """
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
            self._conn.execute("DELETE FROM user_nodes WHERE username = ?", (username,))
        return cur.rowcount > 0

    def pool_node_tags(self) -> set[str]:
        """所有 active 用户的池里出现过的节点 tag。

        健康探测的范围就是它 —— 只探测真的有人在用的节点，池外的一百多个
        节点不碰。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT un.tag FROM user_nodes un "
                "JOIN users u ON u.username = un.username "
                "WHERE u.status = 'active'"
            ).fetchall()
        return {r[0] for r in rows}

    def users_by_node(self) -> dict[str, int]:
        """每个节点被多少个 active 用户的池包含（节点列表的「用户数」列）。

        必须过滤 status = 'active'：停用的用户仍留着池的行，不过滤会把这一
        列撑大。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT un.tag, COUNT(*) FROM user_nodes un "
                "JOIN users u ON u.username = un.username "
                "WHERE u.status = 'active' GROUP BY un.tag"
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    @staticmethod
    def _user_from_row(row) -> dict:
        u = dict(zip(
            ("id", "username", "password", "node_tag", "status", "created_at",
             "updated_at", "revoked_at"), row
        ))
        u.pop("node_tag", None)
        return u


def _dedup(tags: list[str]) -> list[str]:
    """去重但保持顺序，并丢掉空串。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out