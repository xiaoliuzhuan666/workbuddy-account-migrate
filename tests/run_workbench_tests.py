# -*- coding: utf-8 -*-
"""账号工作台：新只读接口与写接口的集成测试。只用临时 fixture，不碰真实用户数据。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from run_webui_tests import (  # noqa: E402
    FAILED,
    FAILURES,
    PASSED,
    _req,
    _start_server,
    _uid,
    check,
)


def _uid2(n="1"):
    return "11111111-1111-1111-1111-11111111111" + n


def _make_db(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, user_id TEXT, title TEXT, status TEXT, "
        "created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER, "
        "cwd TEXT, model TEXT, mode TEXT, custom_title TEXT, deleted_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE session_usage ("
        "session_id TEXT PRIMARY KEY, used INTEGER, size INTEGER, "
        "updated_at INTEGER, credit_json TEXT)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            r,
        )
        conn.execute(
            "INSERT INTO session_usage VALUES (?,?,?,?,?)",
            (r[0], 0, 0, r[5], "{}"),
        )
    conn.commit()
    conn.close()


def make_workbench_home():
    """两个国内版账号 + 一个国际版对话 + 记忆/MCP/任务 + 可导出的项目目录。"""
    home = Path(tempfile.mkdtemp(prefix="wb-workbench-"))
    u1, u2 = _uid2("1"), _uid2("2")
    wb = home / ".workbuddy"
    intl = home / ".workbuddy-ai"
    proj_cwd = home / "projA"
    proj_cwd.mkdir(parents=True)
    (proj_cwd / "README.md").write_text("demo project\n", encoding="utf-8")

    snap = wb / "storage" / "skeleton"
    snap.mkdir(parents=True)
    (snap / "account-snapshot.json").write_text(json.dumps({
        "primary": {
            "uid": u1,
            "nickname": "当前账号",
            "access_token": "SUPER_SECRET_TOKEN_DO_NOT_LEAK",
        },
        "tokens": {"refresh": "ANOTHER_SECRET"},
    }, ensure_ascii=False), encoding="utf-8")

    sid1, sid2, sid3 = "sess-cur-0001", "sess-old-0002", "sess-old-0003"
    _make_db(wb / "workbuddy.db", [
        (sid1, u1, "当前会话", "active", 1, 10, 10, str(proj_cwd), "m", "", "", None),
        (sid2, u2, "旧会话甲", "active", 2, 20, 20, str(proj_cwd), "m", "", "", None),
        (sid3, u2, "旧会话乙", "active", 3, 30, 30, "/tmp/other", "m", "", "", None),
    ])

    mem = wb / "memory"
    mem.mkdir()
    (mem / f"{u1}_memory.md").write_text(
        '<!-- RAW_JSON_START {"memoryBlock":"keep-me"} RAW_JSON_END -->\n当前记忆\n',
        encoding="utf-8",
    )
    (mem / f"{u2}_memory.md").write_text(
        '<!-- RAW_JSON_START {"memoryBlock":"from-old"} RAW_JSON_END -->\n旧记忆\n',
        encoding="utf-8",
    )

    for uid, servers in ((u1, {"keep": {"url": "http://127.0.0.1"}}),
                         (u2, {"oldmcp": {"url": "http://127.0.0.1"}})):
        d = wb / "connectors" / uid
        d.mkdir(parents=True)
        (d / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        (d / "connector-states.json").write_text(json.dumps({"s": 1}), encoding="utf-8")

    # 历史任务（挂在旧会话下）
    tdir = wb / "tasks" / sid2
    tdir.mkdir(parents=True)
    (tdir / "task-1.json").write_text(json.dumps({
        "id": "t1", "subject": "待办示例", "status": "pending",
    }, ensure_ascii=False), encoding="utf-8")
    (tdir / "task-2.json").write_text(json.dumps({
        "id": "t2", "subject": "已完成示例", "status": "completed",
    }, ensure_ascii=False), encoding="utf-8")

    # 国际版：同一会话 id 的拷贝，供跨版本列表 / dry-run
    isnap = intl / "storage" / "skeleton"
    isnap.mkdir(parents=True)
    (isnap / "account-snapshot.json").write_text(json.dumps({
        "primary": {"uid": u1, "nickname": "国际当前", "access_token": "INTL_SECRET_DO_NOT_LEAK"},
    }, ensure_ascii=False), encoding="utf-8")
    sid_intl = "sess-intl-0099"
    _make_db(intl / "workbuddy.db", [
        (sid_intl, u1, "国际版对话", "active", 5, 50, 50, str(proj_cwd), "m", "", "", None),
    ])
    (intl / "memory").mkdir(parents=True, exist_ok=True)
    (intl / "projects").mkdir(parents=True, exist_ok=True)

    # 国内版会话正文（给项目导出一点内容）
    pdir = wb / "projects" / "demo"
    pdir.mkdir(parents=True)
    (pdir / (sid1 + ".jsonl")).write_text(
        json.dumps({"timestamp": 1, "cwd": str(proj_cwd), "content": "不应出现在接口里的正文"}) + "\n",
        encoding="utf-8",
    )

    token_file = home / ".config" / "workbuddy-account-migrate" / "webui-token"
    token_file.parent.mkdir(parents=True)
    return home, token_file, u1, u2, sid1, sid2, sid_intl, proj_cwd


def _auth(port, token):
    return {
        "Authorization": "Bearer " + token,
        "Origin": "http://127.0.0.1:%d" % port,
    }


def _sessions_by_user(db: Path):
    conn = sqlite3.connect(str(db))
    rows = list(conn.execute("SELECT id, user_id FROM sessions ORDER BY id"))
    conn.close()
    return rows


def test_readonly_endpoints():
    print("\n[工作台只读接口]")
    home, token_file, u1, u2, sid1, sid2, sid_intl, proj = make_workbench_home()
    httpd, port, token = _start_server(home, token_file)
    try:
        st, obj, raw = _req(port, "GET", "/api/meta")
        blob = raw.decode("utf-8", "replace")
        check("meta 200 且版本 1.9.0", st == 200 and obj.get("app_version") == "1.9.0", str(obj))
        check("meta 有令牌路径没有令牌本身", obj.get("token_path") and token not in blob)
        check("meta 有各写操作确认文案", (obj.get("confirm_texts") or {}).get("rollback") == "确认回滚")
        check("meta 不含快照机密", "SUPER_SECRET" not in blob and "ANOTHER_SECRET" not in blob)

        st, obj, raw = _req(port, "GET", "/api/diagnose", host="evil.example")
        check("diagnose 坏 Host 403", st == 403, str((st, obj)))

        st, obj, raw = _req(port, "GET", "/api/diagnose")
        blob = raw.decode("utf-8", "replace")
        check("diagnose 200", st == 200 and obj.get("ok") is True, str(obj))
        check("diagnose 不泄露凭证", "SUPER_SECRET" not in blob and "ANOTHER_SECRET" not in blob and "INTL_SECRET" not in blob)
        acc = obj.get("accounts") or []
        check("diagnose 两个账号", len(acc) == 2, str(acc))
        by = {a["user_id"]: a for a in acc}
        check("diagnose 有记忆和 MCP", by[u2]["memory_bytes"] > 0 and by[u2]["mcp_servers"] == 1, str(by[u2]))
        check("diagnose 当前登录标记", by[u1]["is_current"] is True)
        check("diagnose 显示掩码 id", by[u1]["display_id"] and by[u1]["display_id"] != u1)
        check("登录态有三来源字段", "client_display" in (obj.get("login") or {}), str(obj.get("login")))

        st, obj, raw = _req(port, "GET", "/api/accounts")
        blob = raw.decode("utf-8", "replace")
        check("accounts 也带记忆 MCP", any(a.get("mcp_servers") == 1 for a in (obj.get("accounts") or [])), str(obj))
        check("accounts 仍不泄露凭证", "SUPER_SECRET" not in blob)

        st, obj, _ = _req(port, "GET", "/api/sync/preview?source_uid=%s&target_uid=%s" % (u2, u1))
        check("preview 200", st == 200 and obj.get("ok") is True, str(obj))
        items = [r["item"] for r in (obj.get("rows") or [])]
        check("preview 含记忆连接器云端", items == ["会话", "长期记忆", "MCP 连接器", "云端通道映射"], str(items))
        check("preview 目标是当前登录", obj.get("target_is_current") is True)
        check("preview 有执行顺序", len(obj.get("steps") or []) >= 4)

        st, obj, _ = _req(port, "GET", "/api/sync/preview?source_uid=%s&target_uid=%s" % (u1, u1))
        check("preview 同源目标 400", st == 400, str(obj))

        st, obj, _ = _req(port, "GET", "/api/sync/preview?source_uid=%s&target_uid=%s" % (u1, u2))
        check("preview 目标不是当前登录有警告", st == 200 and "当前" in (obj.get("warning") or ""), str(obj.get("warning")))

        st, obj, _ = _req(port, "GET", "/api/backups")
        check("backups 200 空列表", st == 200 and obj.get("ok") is True and obj.get("count") == 0, str(obj))

        st, obj, _ = _req(port, "GET", "/api/tasks")
        check("tasks 200 能列出待办", st == 200 and (obj.get("total_pending") or 0) >= 1, str(obj))
        check("tasks 有说明网页更严", "客户端" in (obj.get("note") or ""))

        st, obj, raw = _req(port, "GET", "/api/session-migrate/list?from=intl")
        blob = raw.decode("utf-8", "replace")
        check("跨版本列表 200", st == 200 and obj.get("count") == 1, str(obj))
        check("跨版本列表不含国际版凭证", "INTL_SECRET" not in blob)
        check("跨版本对话标题在", (obj.get("sessions") or [{}])[0].get("title") == "国际版对话")

        st, obj, _ = _req(port, "GET", "/api/projects")
        check("项目列表 200 且含 projA", st == 200 and any("projA" in (p.get("cwd") or "") for p in (obj.get("projects") or [])), str(obj))
    finally:
        httpd.shutdown()
        shutil.rmtree(str(home), ignore_errors=True)


def test_write_gates_and_sync_rollback():
    print("\n[工作台写接口：门卫、同步、备份、回滚]")
    home, token_file, u1, u2, sid1, sid2, sid_intl, proj = make_workbench_home()
    db = home / ".workbuddy" / "workbuddy.db"
    httpd, port, token = _start_server(home, token_file)
    auth = _auth(port, token)
    before = _sessions_by_user(db)
    try:
        # 无令牌
        st, obj, _ = _req(port, "POST", "/api/rollback", body={
            "tag": "x", "confirm": True, "confirm_text": "确认回滚",
        })
        check("回滚无令牌 401", st == 401, str((st, obj)))
        check("无令牌时库未改", before == _sessions_by_user(db))

        st, obj, _ = _req(port, "POST", "/api/rollback", headers=auth, body={
            "tag": "x", "confirm": False, "confirm_text": "确认回滚",
        })
        check("回滚未勾选 400", st == 400, str(obj))

        st, obj, _ = _req(port, "POST", "/api/rollback", headers=auth, body={
            "tag": "x", "confirm": True, "confirm_text": "随便",
        })
        check("回滚确认文案不对 400", st == 400 and "确认" in (obj.get("message") or ""), str(obj))

        st, obj, _ = _req(port, "POST", "/api/rollback", headers=auth, body={
            "tag": "../../etc", "confirm": True, "confirm_text": "确认回滚",
        })
        check("回滚路径穿越被拒", st == 400, str(obj))
        check("路径穿越后库未改", before == _sessions_by_user(db))

        # 客户端在跑
        import workbuddy_core.workbench as wb
        orig_pf = wb.preflight

        def fake_pf(*a, **k):
            return {
                "ok": True, "clients": [{"name": "WorkBuddy", "pid": 1}],
                "clients_running": True, "detection_trusted": True,
                "must_close_clients": True, "error": "",
            }

        wb.preflight = fake_pf
        try:
            st, obj, _ = _req(port, "POST", "/api/sync", headers=auth, body={
                "source_uid": u2, "target_uid": u1,
                "confirm": True, "confirm_text": "确认同步",
            })
            check("客户端在跑时同步 409", st == 409 and obj.get("error") == "clients_running", str((st, obj)))
            check("409 时库未改", before == _sessions_by_user(db))

            st, obj, _ = _req(port, "POST", "/api/rollback", headers=auth, body={
                "tag": "nope", "confirm": True, "confirm_text": "确认回滚",
            })
            check("客户端在跑时回滚 409", st == 409, str((st, obj)))
        finally:
            wb.preflight = orig_pf

        # 真正同步（临时 fixture）
        st, obj, raw = _req(port, "POST", "/api/sync", headers=auth, body={
            "source_uid": u2, "target_uid": u1,
            "confirm": True, "confirm_text": "确认同步",
        })
        check("同步 200", st == 200 and obj.get("ok") is True, str(obj))
        check("同步返回备份标签", bool(obj.get("backup_tag")), str(obj))
        tag = obj.get("backup_tag")
        check("同步响应脱敏", "SUPER_SECRET" not in raw.decode("utf-8", "replace"))
        after = _sessions_by_user(db)
        owners = {sid: uid for sid, uid in after}
        check("源会话已改挂到目标", owners.get(sid2) == u1, str(after))
        check("目标原会话还在", owners.get(sid1) == u1)

        st, obj, _ = _req(port, "GET", "/api/backups")
        tags = [b.get("tag") for b in (obj.get("backups") or [])]
        check("备份列表含本次标签", tag in tags, str(obj))
        kind = next((b.get("kind") for b in obj["backups"] if b.get("tag") == tag), "")
        check("备份 kind 是 account", kind == "account", str(obj))

        # 回滚还原
        st, obj, _ = _req(port, "POST", "/api/rollback", headers=auth, body={
            "tag": tag, "confirm": True, "confirm_text": "确认回滚",
        })
        check("回滚 200", st == 200 and obj.get("ok") is True, str(obj))
        restored = _sessions_by_user(db)
        owners2 = {sid: uid for sid, uid in restored}
        check("回滚后旧会话回到源账号", owners2.get(sid2) == u2, str(restored))
        check("回滚后当前会话仍在", owners2.get(sid1) == u1)
    finally:
        httpd.shutdown()
        shutil.rmtree(str(home), ignore_errors=True)


def test_session_dry_run_and_project_export():
    print("\n[工作台跨版本 dry-run 与项目导出导入]")
    home, token_file, u1, u2, sid1, sid2, sid_intl, proj = make_workbench_home()
    intl_db = home / ".workbuddy-ai" / "workbuddy.db"
    httpd, port, token = _start_server(home, token_file)
    auth = _auth(port, token)
    before = _sessions_by_user(intl_db)
    domestic_before = _sessions_by_user(home / ".workbuddy" / "workbuddy.db")
    try:
        # dry-run 也要令牌（走 POST 写门）
        st, obj, _ = _req(port, "POST", "/api/session-migrate", body={
            "from": "intl", "to": "domestic", "session_id": sid_intl,
            "mode": "copy", "on_conflict": "skip", "dry_run": True,
        })
        check("dry-run 无令牌 401", st == 401, str(obj))

        st, obj, _ = _req(port, "POST", "/api/session-migrate", headers=auth, body={
            "from": "intl", "to": "domestic", "session_id": sid_intl,
            "mode": "copy", "on_conflict": "skip", "dry_run": True,
        })
        check("dry-run 200", st == 200 and obj.get("ok") is True and obj.get("dry_run") is True, str(obj))
        check("dry-run 不改国际版库", before == _sessions_by_user(intl_db))
        check("dry-run 不改国内版库", domestic_before == _sessions_by_user(home / ".workbuddy" / "workbuddy.db"))

        # ask 冲突策略拒绝
        st, obj, _ = _req(port, "POST", "/api/session-migrate", headers=auth, body={
            "from": "intl", "to": "domestic", "session_id": sid_intl,
            "mode": "copy", "on_conflict": "ask", "dry_run": True,
        })
        check("不允许 ask 冲突策略", st == 400, str(obj))

        # 项目导出到临时目录（本机路径，不上传）
        out = home / "out" / "demo.wbproj"
        out.parent.mkdir()
        st, obj, _ = _req(port, "POST", "/api/projects/export", headers={
            **auth,
        }, body={
            "cwd": str(proj),
            "out": str(out),
            "confirm": False,
            "confirm_text": "确认导出",
        })
        check("导出未确认 400 且未写文件", st == 400 and not out.exists(), str(obj))

        st, obj, _ = _req(port, "POST", "/api/projects/export", headers=auth, body={
            "cwd": str(proj),
            "out": str(out),
            "confirm": True,
            "confirm_text": "确认导出",
        })
        check("导出 200", st == 200 and obj.get("ok") is True, str(obj))
        # 工具可能把路径写到默认位置，检查 log / out
        out_path = Path(obj.get("out") or str(out))
        check("导出包在本机", out_path.exists() or out.exists() or ".wbproj" in (obj.get("log") or ""), str(obj))
        pkg = out_path if out_path.exists() else (out if out.exists() else None)

        if pkg is not None:
            st, obj, raw = _req(port, "GET", "/api/projects/info?package=" + str(pkg))
            check("info 200", st == 200 and obj.get("ok") is True, str(obj))
            check("info 不含快照机密", "SUPER_SECRET" not in raw.decode("utf-8", "replace"))

            # dry-run 导入
            dest = home / "projA-imported"
            dest.mkdir()
            before_dom = _sessions_by_user(home / ".workbuddy" / "workbuddy.db")
            st, obj, _ = _req(port, "POST", "/api/projects/import", headers=auth, body={
                "package": str(pkg),
                "cwd": str(dest),
                "dry_run": True,
                "on_conflict": "skip",
                "confirm": True,
                "confirm_text": "确认导入",
            })
            check("导入 dry-run 不写库", st == 200 and obj.get("dry_run") is True
                  and before_dom == _sessions_by_user(home / ".workbuddy" / "workbuddy.db"), str(obj))
        else:
            check("导出包必须存在以便后续测试", False, "no package")

        # 任务恢复：无确认不写（这里主要测门卫；真正恢复依赖「当前 session」）
        st, obj, _ = _req(port, "POST", "/api/tasks/restore", headers=auth, body={
            "session_id": sid2, "confirm": False, "confirm_text": "确认恢复",
        })
        check("任务恢复未确认 400", st == 400, str(obj))
        st, obj, _ = _req(port, "POST", "/api/tasks/restore", body={
            "session_id": sid2, "confirm": True, "confirm_text": "确认恢复",
        })
        check("任务恢复无令牌 401", st == 401, str(obj))
    finally:
        httpd.shutdown()
        shutil.rmtree(str(home), ignore_errors=True)


def run_all():
    for fn in (
        test_readonly_endpoints,
        test_write_gates_and_sync_rollback,
        test_session_dry_run_and_project_export,
    ):
        try:
            fn()
        except Exception:
            import traceback
            global FAILED
            FAILED += 1
            FAILURES.append((fn.__name__, traceback.format_exc()))
            print("  [EXC] %s\n%s" % (fn.__name__, traceback.format_exc()))


if __name__ == "__main__":
    run_all()
    print()
    print("结果：通过 %d，失败 %d" % (PASSED, FAILED))
    for name, detail in FAILURES:
        print("--- %s ---\n%s" % (name, detail))
    sys.exit(0 if FAILED == 0 else 1)
