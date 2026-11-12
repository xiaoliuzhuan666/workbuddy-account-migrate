# -*- coding: utf-8 -*-
"""账号视图：只读列出 user_id、会话数、当前登录态。

读取 account-snapshot.json 时只取 primary.uid 和 nickname，其它字段一律丢掉，
避免把凭证带进内存或接口。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import ResolvedDir, open_db_readonly
from .security import mask_uid


def _read_login(snapshot_path: Path) -> Dict[str, str]:
    """只取 uid 和 nickname。文件缺失或损坏时返回空。"""
    out = {"uid": "", "nickname": "", "source": ""}
    if not snapshot_path.exists():
        return out
    try:
        with open(snapshot_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    primary = data.get("primary") if isinstance(data.get("primary"), dict) else {}
    # 只用这两个键，绝不把整个 JSON 留下来
    uid = primary.get("uid") or ""
    nick = primary.get("nickname") or ""
    out["uid"] = str(uid) if uid else ""
    out["nickname"] = str(nick) if nick else ""
    out["source"] = "account-snapshot.primary"
    # 明确丢掉原对象
    del data
    return out


@dataclass
class AccountView:
    user_id: str
    display_id: str
    nickname: str
    session_count: int
    is_current: bool
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_id": self.display_id,
            "nickname": self.nickname,
            "session_count": self.session_count,
            "is_current": self.is_current,
            "source": self.source,
        }


def list_accounts(resolved: ResolvedDir) -> Dict[str, Any]:
    login = _read_login(resolved.snapshot_path)
    current = login.get("uid") or ""
    accounts: List[AccountView] = []
    db_ok = False
    db_error = ""
    if resolved.db_path.exists():
        conn = None
        try:
            conn = open_db_readonly(resolved.db_path)
            rows = conn.execute(
                "SELECT user_id, COUNT(*) AS n FROM sessions "
                "WHERE user_id IS NOT NULL AND user_id != '' "
                "GROUP BY user_id ORDER BY n DESC"
            ).fetchall()
            db_ok = True
            seen = set()
            for r in rows:
                uid = r["user_id"]
                seen.add(uid)
                nick = login.get("nickname") if uid == current else ""
                accounts.append(
                    AccountView(
                        user_id=uid,
                        display_id=mask_uid(uid),
                        nickname=nick or "",
                        session_count=int(r["n"]),
                        is_current=bool(current) and uid == current,
                        source="sessions",
                    )
                )
            if current and current not in seen:
                accounts.append(
                    AccountView(
                        user_id=current,
                        display_id=mask_uid(current),
                        nickname=login.get("nickname") or "",
                        session_count=0,
                        is_current=True,
                        source="account-snapshot.primary",
                    )
                )
        except sqlite3.Error as e:
            db_error = type(e).__name__
        finally:
            if conn is not None:
                conn.close()
    elif current:
        accounts.append(
            AccountView(
                user_id=current,
                display_id=mask_uid(current),
                nickname=login.get("nickname") or "",
                session_count=0,
                is_current=True,
                source="account-snapshot.primary",
            )
        )
    return {
        "current_user_id": current,
        "current_display_id": mask_uid(current),
        "current_nickname": login.get("nickname") or "",
        "login_source": login.get("source") or "",
        "db_ok": db_ok,
        "db_error": db_error,
        "accounts": [a.as_dict() for a in accounts],
    }


def list_sessions(
    resolved: ResolvedDir,
    user_id: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    include_deleted: bool = False,
) -> Dict[str, Any]:
    """会话列表，不含正文。标题来自 sessions 表。"""
    if not resolved.db_path.exists():
        return {"ok": False, "error": "找不到数据库", "sessions": [], "total": 0}
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    conn = None
    try:
        conn = open_db_readonly(resolved.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        # 按实际列拼 SELECT，客户端改过表也能跑
        wanted = [
            "id", "title", "custom_title", "user_id", "cwd", "status",
            "created_at", "updated_at", "last_activity_at", "model",
            "deleted_at",
        ]
        have = [c for c in wanted if c in cols]
        if "id" not in have:
            return {"ok": False, "error": "sessions 表没有 id 列", "sessions": [], "total": 0}
        where = []
        args: List[Any] = []
        if user_id:
            where.append("user_id = ?")
            args.append(user_id)
        if not include_deleted and "deleted_at" in cols:
            where.append("(deleted_at IS NULL OR deleted_at = 0)")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            "SELECT COUNT(*) FROM sessions" + where_sql, args
        ).fetchone()[0]
        order = "updated_at" if "updated_at" in cols else "id"
        sql = (
            "SELECT %s FROM sessions%s ORDER BY %s DESC LIMIT ? OFFSET ?"
            % (", ".join(have), where_sql, order)
        )
        rows = conn.execute(sql, args + [limit, offset]).fetchall()
        sessions = []
        for r in rows:
            item = {k: r[k] for k in have}
            title = (item.get("custom_title") or item.get("title") or "").strip()
            sessions.append(
                {
                    "id": item.get("id"),
                    "title": title,
                    "user_id": item.get("user_id") or "",
                    "display_id": mask_uid(item.get("user_id") or ""),
                    "cwd": item.get("cwd") or "",
                    "status": item.get("status") or "",
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "last_activity_at": item.get("last_activity_at"),
                    "model": item.get("model") or "",
                    "deleted": bool(item.get("deleted_at")),
                }
            )
        return {"ok": True, "error": "", "sessions": sessions, "total": int(total),
                "limit": limit, "offset": offset}
    except sqlite3.Error as e:
        return {"ok": False, "error": type(e).__name__, "sessions": [], "total": 0}
    finally:
        if conn is not None:
            conn.close()
