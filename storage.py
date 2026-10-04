"""存储层：SQLite。用户、节点、分配记录，全部持久化。

设计要点：
- 所有写操作走单例连接 + 事务，进程内线程安全。
- 节点表存 outbound_json，导入时按 tag 增量合并：已有节点更新，缺失节点新增，
  不会因为一次导入就把之前拉到的节点删掉。
- 分配表 users.node_name 是权威归属：user → 一个节点。
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
                """
            )
            self._migrate_users()

    def _migrate_users(self):
        """给旧库补列（ALTER TABLE ADD COLUMN 幂等，重复启动不会报错）。"""
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, ddl in (("updated_at", "TEXT"), ("revoked_at", "TEXT")):
            if col not in have:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

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

    def delete_node(self, tag: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM nodes WHERE tag = ?", (tag,))
        return cur.rowcount > 0

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

    def add_user(self, username: str, password: str, node_tag: str | None) -> dict:
        with self._lock, self._conn:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            cur = self._conn.execute(
                "INSERT INTO users(username, password, node_tag, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?)",
                (username, password, node_tag, now, now),
            )
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        u = self._user_from_row(row)
        logger.info("新增用户 %s -> 节点 %s", u["username"], u["node_tag"])
        return u

    def get_user(self, username: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return self._user_from_row(row) if row else None

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
        return [self._user_from_row(r) for r in rows]

    def count_users(self, active_only: bool = True) -> int:
        q = "SELECT COUNT(*) FROM users"
        if active_only:
            q += " WHERE status = 'active'"
        with self._lock:
            return int(self._conn.execute(q).fetchone()[0])

    def set_user_node(self, username: str, node_tag: str):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE users SET node_tag = ?, updated_at = ? WHERE username = ?",
                (node_tag, time.strftime("%Y-%m-%d %H:%M:%S"), username),
            )

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
        """彻底删除用户（含口令与历史）。返回是否真的删掉了。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
        return cur.rowcount > 0

    def used_node_tags(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_tag FROM users WHERE status = 'active' AND node_tag IS NOT NULL"
            ).fetchall()
        return {r[0] for r in rows}

    def node_user_counts(self) -> dict[str, int]:
        """每个节点上绑定了几个 active 用户（给「删除节点」前提示用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_tag, COUNT(*) FROM users "
                "WHERE status = 'active' AND node_tag IS NOT NULL GROUP BY node_tag"
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    @staticmethod
    def _user_from_row(row) -> dict:
        return dict(zip(
            ("id", "username", "password", "node_tag", "status", "created_at",
             "updated_at", "revoked_at"), row
        ))