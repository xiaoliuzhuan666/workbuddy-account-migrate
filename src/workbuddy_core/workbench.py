# -*- coding: utf-8 -*-
"""账号工作台：把已有 CLI 脚本的能力暴露给网页，不改脚本本身、不重写语义。

只读接口直接调用 migrate.py / migrate_session.py / migrate_project.py 里已有的
函数并做成结构化结果。写操作一律走同一道门：二次确认、确认文案、客户端必须
关闭（禁止 --force / --assume-clients-closed）、写之前由原脚本自己做备份。
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import platform
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .migrate_bridge import (
    ClientsStillRunning,
    SyncRefused,
    _load_migrate,
    _scripts_dir,
    preflight,
)
from .security import mask_uid, redact

CONFIRM_SYNC = "确认同步"
CONFIRM_ROLLBACK = "确认回滚"
CONFIRM_SESSION = "确认迁移"
CONFIRM_EXPORT = "确认导出"
CONFIRM_IMPORT = "确认导入"
CONFIRM_RESTORE = "确认恢复"

_BACKUP_TAG_RE = re.compile(r"备份标签:\s*(\S+)")


def confirm_texts() -> Dict[str, str]:
    return {
        "sync": CONFIRM_SYNC,
        "rollback": CONFIRM_ROLLBACK,
        "session": CONFIRM_SESSION,
        "export": CONFIRM_EXPORT,
        "import": CONFIRM_IMPORT,
        "restore": CONFIRM_RESTORE,
    }


def _capture(fn):
    buf = io.StringIO()
    result = None
    err = None
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            result = fn()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        err = "脚本退出，码 %s" % code
        result = code
    log = redact(buf.getvalue())
    return result, err, log


def _safe_tag(tag: str) -> str:
    tag = (tag or "").strip()
    if (
        not tag
        or "/" in tag
        or "\\" in tag
        or os.path.isabs(tag)
        or re.match(r"^[A-Za-z]:", tag)
        or ".." in Path(tag).parts
    ):
        raise SyncRefused("备份标签不合法，只能是备份目录名，不能包含路径。")
    return tag


def _safe_local_path(raw: str, must_exist: bool = False, kind: str = "路径") -> Path:
    s = (raw or "").strip()
    if not s:
        raise SyncRefused("请指定本机%s。" % kind)
    if s.startswith(("http://", "https://", "ftp://")):
        raise SyncRefused("%s必须是本机路径，不能是网址。" % kind)
    p = Path(s).expanduser()
    try:
        p = p.resolve()
    except OSError as e:
        raise SyncRefused("无法解析%s：%s" % (kind, type(e).__name__)) from e
    if must_exist and not p.exists():
        raise SyncRefused("找不到%s：请确认本机路径。" % kind)
    return p


def _write_gate(
    home: Optional[Path],
    wb_dir: Path,
    confirm: bool,
    confirm_text: str,
    expected: str,
    require_clients: bool = True,
) -> None:
    if not confirm:
        raise SyncRefused("未确认。请勾选并二次确认后再提交。")
    if (confirm_text or "").strip() != expected:
        raise SyncRefused("确认语句不匹配，请输入「%s」。" % expected)
    if not require_clients:
        return
    pf = preflight(home, wb_dir)
    if pf.get("clients_running"):
        raise ClientsStillRunning(pf.get("clients") or [])
    if not pf.get("ok"):
        raise SyncRefused(pf.get("error") or "预检失败")
    mig = _load_migrate(home, wb_dir)
    if not mig.require_clients_closed(force=False, assume_closed=False):
        raise ClientsStillRunning(pf.get("clients") or [])


def _load_script(name: str, home: Optional[Path]):
    if home is not None:
        os.environ["WORKBUDDY_MIGRATE_HOME"] = str(Path(home).expanduser().resolve())
    scripts = str(_scripts_dir())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


def _fmt_mem(n: int) -> str:
    n = int(n or 0)
    if n <= 0:
        return "—"
    if n < 1024:
        return "%d B" % n
    return "%.1f KB" % (n / 1024.0)


def _account_row(mig, uid: str, current: str, session_counts, memory_sizes, connector_info) -> Dict[str, Any]:
    ci = connector_info.get(uid) or {}
    mcp = int(ci.get("mcp_servers") or 0)
    conn = int(ci.get("connector_states") or 0)
    mem = int(memory_sizes.get(uid) or 0)
    nick = ""
    if uid == current:
        try:
            nick = mig.get_client_login_nickname() or ""
        except Exception:
            nick = ""
    return {
        "user_id": uid,
        "display_id": mask_uid(uid),
        "nickname": nick or "",
        "session_count": int(session_counts.get(uid) or 0),
        "memory_bytes": mem,
        "memory_display": _fmt_mem(mem),
        "mcp_servers": mcp,
        "connector_states": conn,
        "is_current": bool(current) and uid == current,
    }


def diagnose(home: Optional[Path], wb_dir: Path) -> Dict[str, Any]:
    """结构化呈现 migrate.py diagnose() 用到的同一批函数，不改脚本。"""
    mig = _load_migrate(home, wb_dir)
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            mig._find_workbuddy_dir()
        except Exception:
            pass
        client_uid = mig.get_client_login_uid() or ""
        storage_uid = mig.get_storage_json_uid() or ""
        panel_uid = mig.get_panel_uid_hint() or ""
        nick = mig.get_client_login_nickname() or ""
        current = mig.get_current_user_id(verbose=False) or ""
        all_uids = list(mig.get_all_user_ids() or [])
        session_counts = mig.get_session_counts() or {}
        memory_sizes = mig.get_memory_sizes() or {}
        connector_info = mig.get_connector_info() or {}
        others = []
        dual = getattr(mig, "_DUAL_EDITION", {}) or {}
        for key, path in dual.items():
            try:
                same = mig._same_dir(path, mig.WORKBUDDY_DIR)
            except Exception:
                same = False
            if same:
                continue
            label = "国际版" if key == "intl" else "国内版"
            try:
                stat = mig._edition_stat_text(path)
            except Exception:
                stat = ""
            others.append({
                "key": key,
                "label": label,
                "stat": stat,
                "path_name": Path(path).name,
            })
    accounts = [_account_row(mig, uid, current, session_counts, memory_sizes, connector_info)
                for uid in all_uids]
    suggestions = []
    other_uids = [u for u in all_uids if u != current]
    other_uids.sort(
        key=lambda u: (session_counts.get(u, 0), memory_sizes.get(u, 0)),
        reverse=True,
    )
    for uid in other_uids:
        sc = session_counts.get(uid, 0)
        ms = memory_sizes.get(uid, 0)
        if sc == 0 and ms == 0:
            continue
        suggestions.append({
            "source_uid": uid,
            "source_display": mask_uid(uid),
            "target_uid": current,
            "target_display": mask_uid(current),
            "session_count": int(sc),
            "memory_bytes": int(ms),
            "memory_display": _fmt_mem(ms),
        })
    login_conflict = bool(client_uid and storage_uid and client_uid != storage_uid)
    return {
        "ok": True,
        "error": "",
        "login": {
            "client_uid": client_uid,
            "client_display": mask_uid(client_uid),
            "client_nickname": nick,
            "storage_uid": storage_uid,
            "storage_display": mask_uid(storage_uid),
            "panel_uid": panel_uid,
            "panel_display": mask_uid(panel_uid),
            "current_uid": current,
            "current_display": mask_uid(current),
            "conflict": login_conflict,
            "conflict_message": (
                "客户端登录态与扩展侧记录不一致。迁移时请把目标设为【客户端登录态】那个账号，否则面板可能仍是空的。"
                if login_conflict else ""
            ),
        },
        "accounts": accounts,
        "account_count": len(accounts),
        "single_account": len(accounts) <= 1,
        "suggestions": suggestions,
        "other_editions": others,
        "message": (
            "只发现一个账号，本版本内无需合并。"
            if len(accounts) <= 1 else
            "发现 %d 个账号。" % len(accounts)
        ),
    }


def preview_sync(home: Optional[Path], wb_dir: Path, source_uid: str, target_uid: str) -> Dict[str, Any]:
    source_uid = (source_uid or "").strip()
    target_uid = (target_uid or "").strip()
    if not source_uid or not target_uid:
        raise SyncRefused("源账号和目标账号都要填。")
    if source_uid == target_uid:
        raise SyncRefused("源账号和目标账号相同，没有可同步的内容。")
    diag = diagnose(home, wb_dir)
    by_id = {a["user_id"]: a for a in diag.get("accounts") or []}
    src = by_id.get(source_uid)
    tgt = by_id.get(target_uid)
    if not src:
        raise SyncRefused("源账号在本目录里找不到。")
    login = diag.get("login") or {}
    current = login.get("current_uid") or login.get("client_uid") or ""
    target_is_current = bool(current) and target_uid == current
    warning = ""
    if current and not target_is_current:
        warning = (
            "目标账号不是当前客户端登录账号。同步完成后，会话会挂在目标账号下，"
            "当前登录看到的列表可能仍是空的或没有这批会话。建议把目标改成当前登录账号。"
        )
    steps = [
        "检查客户端已退出",
        "写入备份（库、记忆、连接器），完成后给出备份标签",
        "把源的 %s 个会话改挂到目标账号（源侧不再单独列出）" % src["session_count"],
        "把源的长期记忆（%s）追加到目标，按内容去重" % src["memory_display"],
        "深度合并源的 %s 个 MCP 连接器 JSON，不覆盖已有项" % src["mcp_servers"],
        "默认重置目标的云端边缘同步映射（可用「保留云端映射」跳过）",
    ]
    return {
        "ok": True,
        "source": src,
        "target": tgt or {
            "user_id": target_uid,
            "display_id": mask_uid(target_uid),
            "session_count": 0,
            "memory_bytes": 0,
            "memory_display": "—",
            "mcp_servers": 0,
            "connector_states": 0,
            "is_current": target_is_current,
        },
        "target_is_current": target_is_current,
        "warning": warning,
        "login_conflict": bool(login.get("conflict")),
        "login_conflict_message": login.get("conflict_message") or "",
        "will_reset_cloud_mapping": True,
        "will_backup": True,
        "steps": steps,
        "rows": [
            {
                "item": "会话",
                "source": src["session_count"],
                "target": (tgt or {}).get("session_count") or 0,
                "action": "改归属到目标账号，源侧不再显示",
            },
            {
                "item": "长期记忆",
                "source": src["memory_display"],
                "target": (tgt or {}).get("memory_display") or "—",
                "action": "追加到目标文件，按块去重",
            },
            {
                "item": "MCP 连接器",
                "source": src["mcp_servers"],
                "target": (tgt or {}).get("mcp_servers") or 0,
                "action": "JSON 深度合并，不覆盖已有",
            },
            {
                "item": "云端通道映射",
                "source": "将清理（默认）",
                "target": "—",
                "action": "让对话按新账号通道重传，换设备才看得到",
            },
        ],
    }


def _backup_contents(path: Path) -> List[str]:
    names = []
    try:
        for p in path.iterdir():
            n = p.name
            if n == "meta.json" or n.startswith("."):
                continue
            if n == "workbuddy.db" or n.endswith(".db"):
                names.append("库")
            elif n.endswith("_memory.md"):
                names.append("记忆")
            elif p.is_dir():
                names.append("连接器")
            elif n.endswith(".wbproj") or "project" in n:
                names.append("项目")
            else:
                names.append(n)
    except OSError:
        pass
    # 去重并保持常见顺序
    order = ["库", "记忆", "连接器", "项目"]
    seen = []
    for x in order:
        if x in names and x not in seen:
            seen.append(x)
    for x in names:
        if x not in seen:
            seen.append(x)
    return seen


def _classify_kind(meta: dict) -> str:
    k = str(meta.get("kind") or "")
    if k.startswith("session"):
        return "session"
    if k.startswith("project") or k == "project_import":
        return "project"
    if k == "account" or meta.get("target_uid") and not k:
        return "account"
    return k or "account"


def list_backups(home: Optional[Path], wb_dir: Path) -> Dict[str, Any]:
    """读取 migrate_backups/*/meta.json，按 kind 分类。只读，不调用会打印的 list 函数。"""
    mig = _load_migrate(home, wb_dir)
    roots = []
    try:
        roots.append(Path(mig.BACKUP_DIR))
    except Exception:
        roots.append(Path(wb_dir) / "migrate_backups")
    # 另一版本的备份目录也列出来，方便跨版本对话回滚
    dual = getattr(mig, "_DUAL_EDITION", {}) or {}
    if not dual:
        try:
            mig._find_workbuddy_dir()
            dual = getattr(mig, "_DUAL_EDITION", {}) or {}
        except Exception:
            dual = {}
    for path in (dual or {}).values():
        roots.append(Path(path) / "migrate_backups")
    seen = set()
    items = []
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            continue
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for d in entries:
            if not d.is_dir():
                continue
            mf = d / "meta.json"
            if not mf.exists():
                continue
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                items.append({
                    "tag": d.name,
                    "kind": "broken",
                    "ok": False,
                    "error": "meta.json 无法解析",
                    "created_at": "",
                    "contents": [],
                })
                continue
            if not isinstance(meta, dict):
                items.append({
                    "tag": d.name, "kind": "broken", "ok": False,
                    "error": "meta.json 不是对象", "created_at": "", "contents": [],
                })
                continue
            kind = _classify_kind(meta)
            src = meta.get("source_uid") or meta.get("from_uid") or ""
            tgt = meta.get("target_uid") or meta.get("to_uid") or ""
            items.append({
                "tag": d.name,
                "kind": kind,
                "ok": True,
                "error": "",
                "created_at": str(meta.get("created_at") or meta.get("timestamp") or ""),
                "timestamp": str(meta.get("timestamp") or ""),
                "source_display": mask_uid(str(src)) if src else "",
                "target_display": mask_uid(str(tgt)) if tgt else "",
                "from_edition": meta.get("from_edition") or "",
                "to_edition": meta.get("to_edition") or "",
                "mode": meta.get("mode") or "",
                "session_display": (str(meta.get("session_id") or "")[:8] + "…")
                if meta.get("session_id") else "",
                "contents": _backup_contents(d),
                "label": _backup_label(kind, meta, src, tgt),
            })
    def _key(it):
        return it.get("created_at") or it.get("timestamp") or it.get("tag") or ""
    items.sort(key=_key, reverse=True)
    return {"ok": True, "backups": items, "count": len(items)}


def _backup_label(kind, meta, src, tgt) -> str:
    if kind == "session":
        fe = meta.get("from_edition") or "?"
        te = meta.get("to_edition") or "?"
        mode = meta.get("mode") or ""
        return "跨版本对话 %s → %s%s" % (
            fe, te, (" · " + mode) if mode else "",
        )
    if kind == "project":
        return "跨设备项目导入"
    if src or tgt:
        a = mask_uid(str(src)) if src else "?"
        b = mask_uid(str(tgt)) if tgt else "?"
        return "%s → %s" % (a, b)
    return "整账号备份"


def run_sync(
    home: Optional[Path],
    wb_dir: Path,
    source_uid: str,
    target_uid: str,
    confirm: bool,
    confirm_text: str,
    keep_cloud_mapping: bool = False,
    restart: bool = False,
    expected_confirm: str = CONFIRM_SYNC,
) -> Dict[str, Any]:
    _write_gate(home, wb_dir, confirm, confirm_text, expected_confirm, require_clients=True)
    source_uid = (source_uid or "").strip()
    target_uid = (target_uid or "").strip()
    if not source_uid or not target_uid:
        raise SyncRefused("源账号和目标账号都要填。")
    if source_uid == target_uid:
        raise SyncRefused("源账号和目标账号相同，没有可同步的内容。")

    mig = _load_migrate(home, wb_dir)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            if not mig.require_clients_closed(force=False, assume_closed=False):
                raise ClientsStillRunning([])
            mig.migrate(
                source_uid,
                target_uid=target_uid,
                skip_confirm=True,
                target_is_manual=True,
                reset_mapping=not bool(keep_cloud_mapping),
            )
    except ClientsStillRunning:
        raise
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        log = redact(buf.getvalue())
        return {
            "ok": False,
            "error": "迁移脚本退出，码 %s" % code,
            "log": log[-8000:],
            "source_display": mask_uid(source_uid),
            "target_display": mask_uid(target_uid),
            "backup_tag": _tag_from_log(log),
        }
    log = redact(buf.getvalue())
    tag = _tag_from_log(log)
    restarted = False
    restart_message = ""
    cancelled = False
    try:
        cancelled = bool((getattr(mig, "_STATE", {}) or {}).get("cancelled"))
    except Exception:
        cancelled = False
    if restart and not cancelled:
        if platform.system() == "Darwin":
            try:
                with redirect_stdout(buf), redirect_stderr(buf):
                    mig.restart_client()
                restarted = True
                restart_message = "已请求在 macOS 上稍后重启 WorkBuddy 客户端。"
            except Exception as e:
                restart_message = "重启客户端失败（%s），请手动重启。" % type(e).__name__
        else:
            restart_message = "自动重启目前仅支持 macOS，请手动重启 WorkBuddy 让变更生效。"
    log = redact(buf.getvalue())
    return {
        "ok": True,
        "error": "",
        "log": log[-8000:],
        "source_display": mask_uid(source_uid),
        "target_display": mask_uid(target_uid),
        "backup_tag": tag,
        "keep_cloud_mapping": bool(keep_cloud_mapping),
        "restarted": restarted,
        "restart_message": restart_message,
        "message": "同步已完成（源 %s → 目标 %s）。%s"
        % (
            mask_uid(source_uid),
            mask_uid(target_uid),
            ("备份标签：%s，可在「备份与回滚」里还原。" % tag) if tag else "",
        ),
    }


def _tag_from_log(log: str) -> str:
    m = _BACKUP_TAG_RE.search(log or "")
    if m:
        return m.group(1).strip()
    m2 = re.search(r"--rollback\s+(\S+)", log or "")
    if m2:
        return m2.group(1).strip()
    return ""


def _kind_of_tag(home, wb_dir, tag) -> str:
    data = list_backups(home, wb_dir)
    for b in data.get("backups") or []:
        if b.get("tag") == tag:
            return b.get("kind") or "account"
    return "account"


def run_rollback(
    home: Optional[Path],
    wb_dir: Path,
    tag: str,
    confirm: bool,
    confirm_text: str,
    kind: str = "",
    expected_confirm: str = CONFIRM_ROLLBACK,
) -> Dict[str, Any]:
    tag = _safe_tag(tag)
    _write_gate(home, wb_dir, confirm, confirm_text, expected_confirm, require_clients=True)
    kind = (kind or _kind_of_tag(home, wb_dir, tag) or "account").strip()
    if kind == "session":
        return _session_rollback(home, wb_dir, tag, already_gated=True)
    if kind == "project":
        return _project_rollback(home, wb_dir, tag, already_gated=True)
    mig = _load_migrate(home, wb_dir)
    result, err, log = _capture(lambda: mig.rollback(tag, skip_confirm=True))
    # rollback 返回 False 表示取消；None/True 成功；SystemExit 失败
    if result is False:
        return {
            "ok": False,
            "error": "已取消",
            "log": log[-8000:],
            "tag": tag,
        }
    if err and result not in (0, True, None):
        return {
            "ok": False,
            "error": err,
            "log": log[-8000:],
            "tag": tag,
        }
    return {
        "ok": True,
        "error": "",
        "log": log[-8000:],
        "tag": tag,
        "kind": "account",
        "message": "已回滚备份 %s。" % tag,
    }


def list_tasks(home: Optional[Path], wb_dir: Path) -> Dict[str, Any]:
    mig = _load_migrate(home, wb_dir)
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        stats = mig.get_task_stats() or {}
        current = ""
        try:
            current = mig._resolve_restore_session() or ""
        except Exception:
            current = ""
        sessions = []
        total = 0
        pending = 0
        completed = 0
        for sid, tasks in stats.items():
            title = ""
            try:
                title = mig.get_session_title(sid) or ""
            except Exception:
                title = ""
            n_done = sum(1 for t in tasks if (t or {}).get("status") == "completed")
            n_pend = sum(1 for t in tasks if (t or {}).get("status") == "pending")
            other = len(tasks) - n_done - n_pend
            total += len(tasks)
            pending += n_pend
            completed += n_done
            pending_subjects = [
                str((t or {}).get("subject") or "(无标题)")
                for t in tasks if (t or {}).get("status") == "pending"
            ][:20]
            sessions.append({
                "session_id": sid,
                "session_display": (sid[:8] + "…") if sid else "",
                "title": title,
                "task_count": len(tasks),
                "completed": n_done,
                "pending": n_pend,
                "other": other,
                "pending_subjects": pending_subjects,
                "is_current": bool(current) and sid == current,
            })
    return {
        "ok": True,
        "sessions": sessions,
        "total_tasks": total,
        "total_pending": pending,
        "total_completed": completed,
        "current_session_display": (current[:8] + "…") if current else "",
        "has_current_session": bool(current),
        "note": "网页恢复任务也会要求客户端先退出（比 CLI 更严），并需要令牌和「确认恢复」。",
    }


def run_restore_tasks(
    home: Optional[Path],
    wb_dir: Path,
    session_id: Optional[str],
    confirm: bool,
    confirm_text: str,
) -> Dict[str, Any]:
    _write_gate(home, wb_dir, confirm, confirm_text, CONFIRM_RESTORE, require_clients=True)
    mig = _load_migrate(home, wb_dir)
    sid = (session_id or "").strip() or None
    result, err, log = _capture(
        lambda: mig.restore_tasks(target_session_id=sid, skip_confirm=True)
    )
    if err:
        return {"ok": False, "error": err, "log": log[-8000:]}
    return {
        "ok": True,
        "error": "",
        "log": log[-8000:],
        "message": "已按既有 migrate.py 恢复历史任务。",
    }


def list_cross_sessions(
    home: Optional[Path],
    src: str,
    query: str = "",
) -> Dict[str, Any]:
    src = (src or "domestic").strip()
    if src not in ("domestic", "intl"):
        raise SyncRefused("来源版本只能是 domestic 或 intl。")
    ms = _load_script("migrate_session", home)
    ep = ms.resolve_edition(src, home=Path(home) if home else None)
    rows = ms.list_sessions(ep, query=query or None) or []
    out = []
    for r in rows:
        uid = str(r.get("user_id") or "")
        sid = str(r.get("id") or "")
        title = (r.get("custom_title") or r.get("title") or "").strip()
        out.append({
            "id": sid,
            "title": title,
            "cwd": r.get("cwd") or "",
            "user_display": mask_uid(uid),
            "status": r.get("status") or "",
            "last_activity_at": r.get("last_activity_at"),
            "model": r.get("model") or "",
        })
    return {
        "ok": True,
        "edition": src,
        "edition_exists": bool(ep.exists()),
        "sessions": out,
        "count": len(out),
    }


def run_session_migrate(
    home: Optional[Path],
    wb_dir: Path,
    src: str,
    dst: str,
    session_id: str,
    mode: str = "move",
    on_conflict: str = "skip",
    dry_run: bool = False,
    target_uid: str = "",
    confirm: bool = False,
    confirm_text: str = "",
) -> Dict[str, Any]:
    src = (src or "").strip()
    dst = (dst or "").strip()
    if src not in ("domestic", "intl") or dst not in ("domestic", "intl"):
        raise SyncRefused("来源和目标版本只能是 domestic 或 intl。")
    mode = (mode or "move").strip()
    if mode not in ("move", "copy"):
        raise SyncRefused("模式只能是 move 或 copy。")
    on_conflict = (on_conflict or "skip").strip()
    if on_conflict not in ("skip", "overwrite", "newer"):
        raise SyncRefused("冲突策略只能是 skip、overwrite 或 newer（网页不允许 ask）。")
    session_id = (session_id or "").strip()
    if not session_id:
        raise SyncRefused("请指定要迁移的对话。")
    if not dry_run:
        _write_gate(home, wb_dir, confirm, confirm_text, CONFIRM_SESSION, require_clients=True)
    ms = _load_script("migrate_session", home)
    src_ep = ms.resolve_edition(src, home=Path(home) if home else None)
    dst_ep = ms.resolve_edition(dst, home=Path(home) if home else None)
    if not src_ep.exists():
        raise SyncRefused("源版本数据目录不存在。")
    def _go():
        return ms.do_migrate(
            src_ep, dst_ep, session_id,
            mode=mode,
            on_conflict=on_conflict,
            dry_run=bool(dry_run),
            assume_yes=True,
            target_uid=(target_uid or "").strip() or None,
        )
    result, err, log = _capture(_go)
    tag = _tag_from_log(log)
    if err:
        return {
            "ok": False,
            "error": err,
            "log": log[-8000:],
            "dry_run": bool(dry_run),
            "backup_tag": tag,
        }
    return {
        "ok": True,
        "error": "",
        "log": log[-8000:],
        "dry_run": bool(dry_run),
        "backup_tag": tag,
        "mode": mode,
        "message": (
            "这是预览，没有写盘。"
            if dry_run else
            "单对话迁移已完成。%s" % (("备份标签：%s。" % tag) if tag else "")
        ),
    }


def _session_rollback(home, wb_dir, tag, already_gated=False, confirm=False, confirm_text=""):
    if not already_gated:
        _write_gate(home, wb_dir, confirm, confirm_text, CONFIRM_ROLLBACK, require_clients=True)
    ms = _load_script("migrate_session", home)
    result, err, log = _capture(lambda: ms.rollback(tag, assume_yes=True, full=False))
    if err:
        return {"ok": False, "error": err, "log": log[-8000:], "tag": tag, "kind": "session"}
    return {
        "ok": True, "error": "", "log": log[-8000:], "tag": tag, "kind": "session",
        "message": "已回滚单对话备份 %s。" % tag,
    }


def list_projects(home: Optional[Path], wb_dir: Path, edition: str = "domestic") -> Dict[str, Any]:
    mp = _load_script("migrate_project", home)
    name = "intl" if edition == "intl" else "domestic"
    if wb_dir:
        ep = mp.EditionPaths(
            name=name,
            root=Path(wb_dir),
            db=Path(wb_dir) / "workbuddy.db",
            projects_dir=Path(wb_dir) / "projects",
            todos_dir=Path(wb_dir) / "todos",
            tasks_dir=Path(wb_dir) / "tasks",
            backup_dir=Path(wb_dir) / "migrate_backups",
            account_snapshot=Path(wb_dir) / "storage" / "skeleton" / "account-snapshot.json",
        )
    else:
        ep = mp.resolve_edition(name, home=Path(home) if home else None)
    rows = mp.list_projects(ep) or []
    projects = []
    for cwd, n, last in rows:
        projects.append({
            "cwd": cwd,
            "session_count": int(n or 0),
            "last_activity_at": last,
        })
    return {
        "ok": True,
        "projects": projects,
        "count": len(projects),
        "edition_exists": bool(ep.exists()),
        "package_ext": getattr(mp, "PKG_EXT", ".wbproj"),
    }


def project_info(home: Optional[Path], package: str) -> Dict[str, Any]:
    pkg = _safe_local_path(package, must_exist=True, kind="迁移包")
    mp = _load_script("migrate_project", home)
    result, err, log = _capture(lambda: mp.do_info(str(pkg)))
    # do_info 主要打印；再读一遍清单做成结构化（用脚本自己的 read_package_members）
    info: Dict[str, Any] = {"path": str(pkg), "name": pkg.name, "size": pkg.stat().st_size}
    try:
        tar, manifest, members = mp.read_package_members(pkg)
        try:
            tar.close()
        except Exception:
            pass
        if isinstance(manifest, dict):
            src = manifest.get("source_uid") or ""
            sessions = manifest.get("sessions") if isinstance(manifest.get("sessions"), list) else []
            titles = []
            for s in sessions:
                if isinstance(s, dict):
                    titles.append({
                        "title": (s.get("title") or "(无标题)"),
                        "last_activity_at": s.get("last_activity_at"),
                    })
            info.update({
                "format": manifest.get("format"),
                "format_version": manifest.get("format_version"),
                "tool_version": manifest.get("tool_version"),
                "created_at": manifest.get("created_at"),
                "source_cwd": manifest.get("source_cwd"),
                "source_display": mask_uid(str(src)) if src else "",
                "session_count": manifest.get("session_count") or len(titles),
                "include_workspace": bool(manifest.get("include_workspace")),
                "sessions": titles[:30],
            })
    except Exception as e:
        if not err:
            err = "%s: 无法读取包内容" % type(e).__name__
    return {
        "ok": not err,
        "error": err or "",
        "log": log[-8000:],
        "info": info,
    }


def run_project_export(
    home: Optional[Path],
    wb_dir: Path,
    cwd: str,
    out: str,
    confirm: bool,
    confirm_text: str,
    no_workspace_memory: bool = False,
    edition: str = "domestic",
) -> Dict[str, Any]:
    # 导出写的是本机一个新文件，不改库；仍要令牌和确认，但不强制关客户端。
    _write_gate(home, wb_dir, confirm, confirm_text, CONFIRM_EXPORT, require_clients=False)
    cwd_p = _safe_local_path(cwd, must_exist=False, kind="项目路径")
    out_p = _safe_local_path(out, must_exist=False, kind="导出路径") if out else None
    mp = _load_script("migrate_project", home)
    args = argparse.Namespace(
        cwd=str(cwd_p),
        out=str(out_p) if out_p else None,
        yes=True,
        no_workspace_memory=bool(no_workspace_memory),
        dir=str(wb_dir) if wb_dir else None,
        intl=(edition == "intl"),
        target_uid=None,
        force=False,
        assume_clients_closed=False,
        cmd="export",
        dry_run=False,
        on_conflict="skip",
        package=None,
    )
    result, err, log = _capture(lambda: mp.do_export(args))
    code = result if isinstance(result, int) else (1 if err else 0)
    ok = code == 0 and not err
    # 从日志里尽量找出写出的路径
    out_found = str(out_p) if out_p and out_p.exists() else ""
    if not out_found:
        m = re.search(r"(\S+\.wbproj)", log or "")
        if m:
            out_found = m.group(1)
    return {
        "ok": ok,
        "error": err or ("" if ok else "导出未成功"),
        "log": log[-8000:],
        "out": out_found,
        "message": "已导出到本机：%s" % out_found if ok and out_found else (err or log[-200:]),
    }


def run_project_import(
    home: Optional[Path],
    wb_dir: Path,
    package: str,
    cwd: str,
    confirm: bool,
    confirm_text: str,
    dry_run: bool = False,
    on_conflict: str = "skip",
    target_uid: str = "",
    edition: str = "domestic",
) -> Dict[str, Any]:
    pkg = _safe_local_path(package, must_exist=True, kind="迁移包")
    cwd_p = _safe_local_path(cwd, must_exist=True, kind="本机项目目录")
    if not cwd_p.is_dir():
        raise SyncRefused("本机项目目录必须已存在且是文件夹。")
    on_conflict = (on_conflict or "skip").strip()
    if on_conflict not in ("skip", "overwrite"):
        raise SyncRefused("冲突策略只能是 skip 或 overwrite（网页不允许 ask）。")
    if not dry_run:
        _write_gate(home, wb_dir, confirm, confirm_text, CONFIRM_IMPORT, require_clients=True)
    mp = _load_script("migrate_project", home)
    args = argparse.Namespace(
        package=str(pkg),
        cwd=str(cwd_p),
        dry_run=bool(dry_run),
        on_conflict=on_conflict,
        yes=True,
        dir=str(wb_dir) if wb_dir else None,
        intl=(edition == "intl"),
        target_uid=(target_uid or "").strip() or None,
        force=False,
        assume_clients_closed=False,
        cmd="import",
        no_workspace_memory=False,
        out=None,
    )
    result, err, log = _capture(lambda: mp.do_import(args))
    code = result if isinstance(result, int) else (1 if err else 0)
    ok = code == 0 and not err
    tag = _tag_from_log(log)
    return {
        "ok": ok,
        "error": err or ("" if ok else "导入未成功"),
        "log": log[-8000:],
        "dry_run": bool(dry_run),
        "backup_tag": tag,
        "message": (
            "这是预览，没有写盘。"
            if dry_run else
            "导入已完成。%s" % (("备份标签：%s。" % tag) if tag else "")
        ),
    }


def _project_rollback(home, wb_dir, tag, already_gated=False, confirm=False, confirm_text="", edition="domestic"):
    if not already_gated:
        _write_gate(home, wb_dir, confirm, confirm_text, CONFIRM_ROLLBACK, require_clients=True)
    mp = _load_script("migrate_project", home)
    args = argparse.Namespace(
        rollback=tag,
        yes=True,
        force=False,
        assume_clients_closed=False,
        dir=str(wb_dir) if wb_dir else None,
        intl=(edition == "intl"),
        target_uid=None,
        cmd=None,
        backups=False,
    )
    result, err, log = _capture(lambda: mp.do_rollback(tag, args))
    code = result if isinstance(result, int) else (1 if err else 0)
    ok = code == 0 and not err
    return {
        "ok": ok,
        "error": err or ("" if ok else "回滚未成功"),
        "log": log[-8000:],
        "tag": tag,
        "kind": "project",
        "message": "已回滚项目导入 %s。" % tag if ok else (err or "回滚未成功"),
    }


def run_session_rollback(home, wb_dir, tag, confirm, confirm_text):
    tag = _safe_tag(tag)
    return _session_rollback(home, wb_dir, tag, already_gated=False, confirm=confirm, confirm_text=confirm_text)


def run_project_rollback(home, wb_dir, tag, confirm, confirm_text, edition="domestic"):
    tag = _safe_tag(tag)
    return _project_rollback(home, wb_dir, tag, already_gated=False, confirm=confirm, confirm_text=confirm_text, edition=edition)
