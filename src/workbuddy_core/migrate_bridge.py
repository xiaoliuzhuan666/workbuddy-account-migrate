# -*- coding: utf-8 -*-
"""把已有的 migrate.py 整账号合并逻辑暴露给网页写接口。

默认不导入 migrate（避免扫描真实家目录）。只在预检和真正同步时惰性导入。
网页侧禁止 --force，客户端在跑就必须先关。
"""
from __future__ import annotations

import importlib
import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from typing import Any, Dict, Optional

from .security import redact, mask_uid


class ClientsStillRunning(RuntimeError):
    def __init__(self, clients):
        self.clients = clients or []
        RuntimeError.__init__(self, "WorkBuddy 客户端还在运行，必须先退出再同步")


class SyncRefused(RuntimeError):
    pass


def _scripts_dir() -> Path:
    # src/workbuddy_core/migrate_bridge.py → repo/scripts
    return Path(__file__).resolve().parents[2] / "scripts"


def _load_migrate(home: Optional[Path], wb_dir: Optional[Path]):
    if home is not None:
        os.environ["WORKBUDDY_MIGRATE_HOME"] = str(Path(home).expanduser().resolve())
    scripts = str(_scripts_dir())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import migrate as mig
    importlib.reload(mig)
    if wb_dir is not None:
        mig._set_workbuddy_dir(Path(wb_dir).expanduser().resolve())
    return mig


def preflight(home: Optional[Path], wb_dir: Path) -> Dict[str, Any]:
    """只读预检：客户端是否在跑、源/目标账号是否能判断。不写任何文件。"""
    mig = _load_migrate(home, wb_dir)
    try:
        raw = mig.find_running_clients()
        # v1.7.1：返回 (进程名列表, 检测是否可信)
        if isinstance(raw, tuple) and len(raw) == 2:
            names, trusted = raw
        else:
            names, trusted = (raw or []), True
        clients = [{"name": str(n), "pid": None} for n in (names or [])]
    except Exception as e:
        return {
            "ok": False,
            "error": "检测客户端进程失败：%s" % type(e).__name__,
            "clients": [],
            "clients_running": None,
            "detection_trusted": False,
            "must_close_clients": True,
        }
    if not trusted:
        return {
            "ok": False,
            "error": "进程检测本身失败，无法确认客户端已关闭。网页同步不会在这种情况下继续。请先关掉 WorkBuddy，再从终端跑 CLI。",
            "clients": clients,
            "clients_running": None,
            "detection_trusted": False,
            "must_close_clients": True,
            "message": "进程检测不可信，拒绝同步。",
        }
    return {
        "ok": True,
        "error": "",
        "clients": clients,
        "clients_running": bool(clients),
        "detection_trusted": True,
        "must_close_clients": True,
        "message": (
            "同步前必须退出 WorkBuddy 客户端（和 CLI 一样）。检测到还在运行：%s。"
            % "、".join(c["name"] for c in clients)
            if clients
            else "当前没检测到正在运行的 WorkBuddy 客户端。"
        ),
    }


def run_sync(
    home: Optional[Path],
    wb_dir: Path,
    source_uid: str,
    target_uid: str,
    confirm: bool,
    confirm_text: str,
    expected_confirm: str = "确认同步",
) -> Dict[str, Any]:
    if not confirm:
        raise SyncRefused("未确认。请在网页上二次确认后再提交。")
    if (confirm_text or "").strip() != expected_confirm:
        raise SyncRefused("确认语句不匹配，请输入「%s」。" % expected_confirm)
    source_uid = (source_uid or "").strip()
    target_uid = (target_uid or "").strip()
    if not source_uid or not target_uid:
        raise SyncRefused("源账号和目标账号都要填。")
    if source_uid == target_uid:
        raise SyncRefused("源账号和目标账号相同，没有可同步的内容。")

    pf = preflight(home, wb_dir)
    if pf.get("clients_running"):
        raise ClientsStillRunning(pf.get("clients") or [])
    if not pf.get("ok"):
        raise SyncRefused(pf.get("error") or "预检失败")

    mig = _load_migrate(home, wb_dir)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            # migrate() 本身不检查客户端，检查在 CLI 的 _main() 里。
            # 网页这里必须再拦一次，且不允许 --force / assume_closed。
            if not mig.require_clients_closed(force=False, assume_closed=False):
                raise ClientsStillRunning(pf.get("clients") or [])
            # skip_confirm：HTTP 层已经做了二次确认；不再走交互式 stdin
            # target_is_manual=True：目标是用户在网页上明确选的
            mig.migrate(
                source_uid,
                target_uid=target_uid,
                skip_confirm=True,
                target_is_manual=True,
            )
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        return {
            "ok": False,
            "error": "迁移脚本退出，码 %s" % code,
            "log": redact(buf.getvalue())[-8000:],
            "source_display": mask_uid(source_uid),
            "target_display": mask_uid(target_uid),
        }
    log = redact(buf.getvalue())
    return {
        "ok": True,
        "error": "",
        "log": log[-8000:],
        "source_display": mask_uid(source_uid),
        "target_display": mask_uid(target_uid),
        "message": "同步已交给既有的 migrate.py 执行完毕（源 %s → 目标 %s）。"
        % (mask_uid(source_uid), mask_uid(target_uid)),
    }
