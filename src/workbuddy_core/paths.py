# -*- coding: utf-8 -*-
"""解析 WorkBuddy 数据目录：--dir / --intl / 双版本提示。

口径与 scripts/migrate.py 在 v1.7.1 的约定对齐：两个目录都有数据时默认国内版，
并把两边都列出来。本模块在导入时不扫描家目录。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def home_dir(home: Optional[Path] = None) -> Path:
    """测试可用 WORKBUDDY_MIGRATE_HOME 或显式 home 覆盖，避免碰到真实数据。"""
    if home is not None:
        return Path(home).expanduser().resolve()
    env = (os.environ.get("WORKBUDDY_MIGRATE_HOME") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home().resolve()


def _dir_in_use(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _scan_edition(wb_dir: Path) -> Tuple[Optional[int], Optional[int]]:
    """返回 (session 总数, 账号数)。库不存在是 (0, 0)；打不开是 (None, None)。"""
    db = wb_dir / "workbuddy.db"
    if not db.exists():
        return 0, 0
    try:
        uri = db.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except Exception:
        try:
            conn = sqlite3.connect(str(db))
        except Exception:
            return None, None
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        uids = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM sessions"
        ).fetchone()[0]
        return int(total), int(uids)
    except Exception:
        return None, None
    finally:
        conn.close()


@dataclass
class EditionSummary:
    key: str  # domestic / intl
    label: str
    path: Path
    in_use: bool
    session_count: Optional[int]
    account_count: Optional[int]
    readable: bool

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "path": str(self.path),
            "in_use": self.in_use,
            "session_count": self.session_count,
            "account_count": self.account_count,
            "readable": self.readable,
        }


@dataclass
class ResolvedDir:
    home: Path
    path: Path
    edition: str  # domestic / intl / custom
    label: str
    db_path: Path
    projects_dir: Path
    snapshot_path: Path
    memory_dir: Path
    dual: bool
    editions: List[EditionSummary] = field(default_factory=list)
    notice: str = ""

    def as_dict(self) -> dict:
        return {
            "home": str(self.home),
            "path": str(self.path),
            "edition": self.edition,
            "label": self.label,
            "db_path": str(self.db_path),
            "projects_dir": str(self.projects_dir),
            "dual": self.dual,
            "notice": self.notice,
            "editions": [e.as_dict() for e in self.editions],
            "db_exists": self.db_path.exists(),
            "projects_exists": self.projects_dir.is_dir(),
        }


def _summarize(key: str, label: str, path: Path) -> EditionSummary:
    in_use = _dir_in_use(path)
    total, uids = _scan_edition(path) if in_use else (0, 0)
    readable = not (in_use and total is None)
    return EditionSummary(
        key=key,
        label=label,
        path=path,
        in_use=in_use,
        session_count=total,
        account_count=uids,
        readable=readable,
    )


def resolve_workbuddy(
    home: Optional[Path] = None,
    explicit_dir: Optional[Path] = None,
    intl: bool = False,
    edition: Optional[str] = None,
) -> ResolvedDir:
    """解析当前要读的数据目录。

    优先级：explicit_dir > edition/intl > 自动探测。
    edition 取值：domestic / intl / auto（默认）。
    """
    h = home_dir(home)
    domestic = h / ".workbuddy"
    intl_dir = h / ".workbuddy-ai"
    editions = [
        _summarize("domestic", "国内版", domestic),
        _summarize("intl", "国际版", intl_dir),
    ]
    has_d = editions[0].in_use
    has_i = editions[1].in_use
    dual = has_d and has_i
    notice = ""

    if explicit_dir is not None:
        path = Path(explicit_dir).expanduser().resolve()
        name = path.name
        if name == ".workbuddy-ai":
            ed, label = "intl", "国际版"
        elif name == ".workbuddy":
            ed, label = "domestic", "国内版"
        else:
            ed, label = "custom", "指定目录"
    else:
        want = (edition or ("intl" if intl else "auto") or "auto").lower()
        if want == "intl":
            path, ed, label = intl_dir, "intl", "国际版"
        elif want == "domestic":
            path, ed, label = domestic, "domestic", "国内版"
        else:
            if has_d and has_i:
                path, ed, label = domestic, "domestic", "国内版"
                notice = (
                    "本机同时存在国内版和国际版数据目录。"
                    "默认读取国内版；要看国际版请在页面切换，或启动时加 --intl。"
                )
            elif has_i and not has_d:
                path, ed, label = intl_dir, "intl", "国际版"
            else:
                path, ed, label = domestic, "domestic", "国内版"

    return ResolvedDir(
        home=h,
        path=path,
        edition=ed,
        label=label,
        db_path=path / "workbuddy.db",
        projects_dir=path / "projects",
        snapshot_path=path / "storage" / "skeleton" / "account-snapshot.json",
        memory_dir=path / "memory",
        dual=dual,
        editions=editions,
        notice=notice,
    )


def open_db_readonly(db_path: Path) -> sqlite3.Connection:
    """只读打开 workbuddy.db。优先 URI mode=ro，失败再退普通连接（只跑 SELECT）。"""
    if not db_path.exists():
        raise FileNotFoundError("找不到 workbuddy.db：%s" % db_path)
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass
    return conn
