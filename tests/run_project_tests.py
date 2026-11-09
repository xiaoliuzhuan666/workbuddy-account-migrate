#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_project.py 的合成 fixture 测试

与 run_tests.py 的区别：不依赖真实 WorkBuddy 数据——用两个假 home 模拟两台设备
（不同 uid、不同项目路径、不同的 sessions 表列集合/顺序），全链路跑
export → （模拟传输）→ import → 重复导入防重 → 回滚。

用法:
    python3 tests/run_project_tests.py

全程在临时目录运行，不触碰真实数据目录。
"""

import csv
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

if platform.system() == "Windows":
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name)
        _enc = (getattr(_stream, "encoding", "") or "").lower().replace("-", "")
        if _enc != "utf8":
            try:
                _stream.flush()
                setattr(sys, _name, io.TextIOWrapper(
                    _stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))
            except Exception:
                pass



SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_project.py"

PASS = 0
FAIL = 0
FAILURES = []


def _client_running() -> bool:
    """本机是否真有 WorkBuddy 在跑（决定能否断言守卫确实拦截）"""
    try:
        if platform.system() == "Windows":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
            ).stdout
            # 只查第一列（映像名），与产品侧 find_running_clients 同口径：
            # 整段输出还含窗口标题，终端开在 workbuddy-* 目录下会假阳性
            names = [next(csv.reader([ln]), [""])[0] for ln in out.splitlines() if ln.strip()]
        else:
            out = subprocess.run(
                ["ps", "-eo", "comm="], capture_output=True, text=True,
                errors="replace", timeout=15,
            ).stdout
            names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return any("workbuddy" in n.lower() for n in names)
    except Exception:
        return False


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILURES.append((name, detail))
        print(f"  ❌ {name}  {detail}")


# ---------------------------------------------------------------- fixture


SCHEMA_A = (
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, user_id TEXT, title TEXT, "
    "custom_title TEXT, status TEXT, created_at INTEGER, updated_at INTEGER, "
    "deleted_at INTEGER, last_activity_at INTEGER, model TEXT, source_mode TEXT, "
    # BLOB 列：验证导出/导入走 base64 无损往返（v1 的 default=str 会变成 repr 字符串）
    "payload BLOB)"
)
SCHEMA_B = (
    # 故意与 A 不同：列顺序不同 + permission_mode 是 B 独有 + model 缺失
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, status TEXT, "
    "cwd TEXT, created_at INTEGER, last_activity_at INTEGER, updated_at INTEGER, "
    "deleted_at INTEGER, permission_mode TEXT, payload BLOB)"   # payload 两边都有，用于验证 BLOB 往返
)
USAGE_SCHEMA = (
    "CREATE TABLE session_usage (session_id TEXT PRIMARY KEY, used INTEGER, "
    "size INTEGER, updated_at INTEGER, credit_json TEXT)"
)
WORKSPACES_SCHEMA = "CREATE TABLE workspaces (path TEXT, last_opened_at INTEGER)"


def make_session_row(sid, uid, cwd, title, last):
    return {
        "id": sid, "cwd": cwd, "user_id": uid, "title": title,
        "custom_title": None, "status": "completed", "created_at": last - 60000,
        "updated_at": last, "deleted_at": None, "last_activity_at": last,
        "model": "glm-5", "source_mode": "cli",
        "payload": b"\x00\x01\xff\x00BLOB",
    }


def write_jsonl(path: Path, sid: str, cwd: str):
    """四行正文：带 cwd / 消息文本里引用旧路径（不应被改）/ 无 cwd / 嵌套同名 cwd"""
    lines = [
        json.dumps({"type": "message", "sessionId": sid, "timestamp": 1760000000000,
                    "cwd": cwd, "meta": {}}, ensure_ascii=False),
        json.dumps({"type": "message", "sessionId": sid, "timestamp": 1760000001000,
                    "content": [{"type": "input_text", "text": f"请看 {cwd}/logs 下的日志"}]},
                   ensure_ascii=False),
        json.dumps({"type": "summary", "sessionId": sid, "title": "无 cwd 字段的行"}, ensure_ascii=False),
        # 嵌套对象里【先】出现同名 cwd 且值相同：只有顶层那个该被改写。
        # 以前用正则替换行内第一个 "cwd"，会把嵌套里的值改掉（可见内容被篡改）。
        json.dumps({"type": "message", "sessionId": sid, "timestamp": 1760000002000,
                    "meta": {"cwd": cwd}, "cwd": cwd}, ensure_ascii=False),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_home_a(home: Path):
    """源机：项目目录真实存在（工作区记忆才收得到），2 个会话，uid-A"""
    root = home / ".workbuddy"
    root.mkdir(parents=True)
    proj_dir = home / "alice-projects" / "proj-demo"
    proj = str(proj_dir)
    db = root / "workbuddy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA_A)
    conn.execute(USAGE_SCHEMA)
    conn.execute(WORKSPACES_SCHEMA)
    uid = "aaaaaaaa-1111-2222-3333-444444444444"
    sids = []
    for i, (title, last) in enumerate([
        ("修复区间票退款 bug", 1760000100000),
        ("地图密钥安全优化", 1760000200000),
    ]):
        sid = str(uuid.uuid4())
        sids.append(sid)
        row = make_session_row(sid, uid, proj, title, last)
        cols = list(row.keys())
        conn.execute(
            f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [row[c] for c in cols],
        )
        conn.execute("INSERT INTO session_usage VALUES (?, ?, ?, ?, ?)",
                     (sid, 1234 + i, 5678 + i, last, "{}"))
        slug = "Users-alice-WorkBuddy-proj-demo"
        write_jsonl(root / "projects" / slug / f"{sid}.jsonl", sid, proj)
        (root / "projects" / slug / f"{sid}.meta.json").write_text(
            json.dumps({"codebuddy.ai/hostKind": "unopted"}), encoding="utf-8")
        tr = root / "projects" / slug / sid / "tool-results"
        tr.mkdir(parents=True, exist_ok=True)
        (tr / "cmd-1.txt").write_text("工具输出内容" * 10, encoding="utf-8")
        (root / "todos" / f"{sid}.json").parent.mkdir(parents=True, exist_ok=True)
        (root / "todos" / f"{sid}.json").write_text(json.dumps([{"subject": "任务", "status": "pending"}]),
                                                     encoding="utf-8")
        tdir = root / "tasks" / sid
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "t1.json").write_text(json.dumps({"subject": "子任务", "status": "pending"}), encoding="utf-8")
    conn.execute("INSERT INTO workspaces VALUES (?, ?)", (proj, 1760000200000))
    conn.commit()
    conn.close()
    # 登录态
    snap = root / "storage" / "skeleton" / "account-snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"primary": {"uid": uid}}), encoding="utf-8")
    # 项目目录本身 + 工作区记忆（目录必须真实存在且与 DB cwd 一致，export 才收得到）
    proj_dir.mkdir(parents=True, exist_ok=True)
    mem = proj_dir / ".workbuddy" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# 项目记忆\n- 源机写下的记忆\n", encoding="utf-8")
    (mem / "2026-10-01.md").write_text("- 旧日志\n", encoding="utf-8")
    return uid, proj, sids


def build_home_b(home: Path):
    """目标机：登录 uid-B，已有 1 个别的项目的会话，项目目录已就位"""
    root = home / ".workbuddy"
    root.mkdir(parents=True)
    db = root / "workbuddy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA_B)
    conn.execute(USAGE_SCHEMA)
    conn.execute(WORKSPACES_SCHEMA)
    uid = "bbbbbbbb-1111-2222-3333-444444444444"
    other_sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, status, cwd, created_at, last_activity_at, "
        "updated_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (other_sid, uid, "本地已有对话", "completed", "/Users/bob/other", 1, 2, 2, None),
    )
    conn.commit()
    conn.close()
    snap = root / "storage" / "skeleton" / "account-snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"primary": {"uid": uid}}), encoding="utf-8")
    proj_dir = home / "bob-projects" / "demo-repo"
    proj_dir.mkdir(parents=True)
    return uid, str(proj_dir), [other_sid]


def run_script(args, home: Path, expect_ok=True):
    env = dict(os.environ)
    env["WORKBUDDY_MIGRATE_HOME"] = str(home)
    r = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True, env=env, timeout=120,
        # 子进程输出里有 emoji / 中文，GBK 控制台上不显式指定会 UnicodeDecodeError
        encoding="utf-8", errors="replace",
        input="",
    )
    if expect_ok and r.returncode != 0:
        print(f"  ⚠️  子进程退出码 {r.returncode}\n--- stdout ---\n{r.stdout[-2000:]}\n--- stderr ---\n{r.stderr[-2000:]}")
    return r


def db_query(home: Path, sql, params=()):
    conn = sqlite3.connect(str(home / ".workbuddy" / "workbuddy.db"))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def slug_of(cwd: str) -> str:
    """镜像 cwd_to_slug 规则（测试保持独立实现，不 import 被测脚本）"""
    s = str(cwd)
    unc = s.startswith("\\\\") or s.startswith("//")
    # 与产品 cwd_to_slug 完全一致：盘符小写去冒号 → 分隔符转 - → 折叠 -{2,} → strip
    s = re.sub(r"^([A-Za-z]):", lambda m: m.group(1).lower(), s)
    s = s.replace("\\", "-").replace("/", "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if unc and s:
        s = "unc-" + s
    return s


# ---------------------------------------------------------------- 测试


def _print_summary(tmp):
    print("\n" + "=" * 60)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    if FAILURES:
        print("失败项：")
        for name, detail in FAILURES:
            print(f"  - {name}: {detail}")
        # 失败时保留 fixture 便于排查
        print(f"fixture 保留在：{tmp}")
    else:
        # 成功时清掉（本套件全是合成数据，纯粹是别在 %TEMP% 留垃圾）
        shutil.rmtree(str(tmp), ignore_errors=True)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="wb_project_test_"))
    home_a = tmp / "homeA"
    home_b = tmp / "homeB"
    home_a.mkdir()
    home_b.mkdir()
    print(f"fixture：{tmp}\n")

    uid_a, proj_a, sids_a = build_home_a(home_a)
    uid_b, proj_b, sids_b = build_home_b(home_b)
    pkg = tmp / "proj-demo.wbproj"

    # ---- 0. 裸运行 / 新手路径 ----
    print("== 裸运行冒烟 ==")
    r0 = run_script([], home_a)
    check("无参数运行不崩（无 TTY 打印帮助）", r0.returncode == 1 and "Traceback" not in r0.stderr)

    # ---- 1. export ----
    print("\n== export：源机打包 ==")
    r = run_script(["export", "--cwd", proj_a, "--out", str(pkg)], home_a)
    check("export 退出码 0", r.returncode == 0, r.stdout[-500:] + r.stderr[-500:])
    check("包文件生成", pkg.exists())
    # 默认输出：fixture 里有桌面目录时应落到桌面
    (home_a / "Desktop").mkdir(exist_ok=True)
    rd = run_script(["export", "--cwd", proj_a], home_a)
    desk_pkgs = list((home_a / "Desktop").glob("*.wbproj"))
    check("export 退出码 0（默认输出）", rd.returncode == 0, rd.stdout[-500:] + rd.stderr[-500:])
    check("不指定 --out 时包默认落在桌面", len(desk_pkgs) == 1, str(desk_pkgs))
    if pkg.exists():
        r_info = run_script(["info", str(pkg)], home_a)
        check("info 能读包", r_info.returncode == 0 and "proj-demo" in r_info.stdout)

    # ---- 2. import（路径重映射 + 列名对齐 + uid 改写）----
    print("\n== import：目标机导入 ==")
    # 客户端守卫的真实拦截分支：本机确实开着 WorkBuddy 时，不带 --force 必须 exit 2。
    # 以前所有用例都带 --force，这条分支零覆盖。
    if _client_running():
        r_guard = run_script(["import", str(pkg), "--cwd", proj_b], home_b)
        check("客户端在跑时 import 被守卫拦下（exit 2）",
              r_guard.returncode == 2, f"rc={r_guard.returncode}")
    else:
        print("  ⏭️  本机没有运行中的客户端，跳过守卫拦截断言")

    r = run_script(["import", str(pkg), "--cwd", proj_b, "--on-conflict", "overwrite", "--force"],
                   home_b)
    check("import 退出码 0", r.returncode == 0, r.stdout[-800:] + r.stderr[-800:])

    rows = db_query(home_b, "SELECT * FROM sessions WHERE id IN (?, ?)", tuple(sids_a))
    check("两个会话都已写入", len(rows) == 2, f"实际 {len(rows)}")
    check("user_id 全部改写为本机登录 uid", all(r["user_id"] == uid_b for r in rows))
    check("cwd 全部改写为新路径", all(r["cwd"] == proj_b for r in rows))
    check("model 列（B 独有缺失列）被安全跳过", all("model" not in r or r.get("model") is None for r in rows))
    # BLOB 往返：导出走 {"__b64__": ...}，导入要还原成 bytes（不是 repr 字符串）
    blob_ok = [r for r in rows if r.get("payload") == b"\x00\x01\xff\x00BLOB"]
    check("BLOB 列无损往返（仍是 bytes）", len(blob_ok) == 2,
          f"payload={[type(r.get('payload')).__name__ for r in rows]}")
    local = db_query(home_b, "SELECT * FROM sessions WHERE id = ?", (sids_b[0],))
    check("本地原有会话不受影响", len(local) == 1)

    slug_b = slug_of(proj_b)
    sid1 = sids_a[0]
    jsonl_b = home_b / ".workbuddy" / "projects" / slug_b / f"{sid1}.jsonl"
    check("正文落到按新路径推导的 slug 目录", jsonl_b.exists(), str(jsonl_b))
    if jsonl_b.exists():
        content = jsonl_b.read_text(encoding="utf-8")
        lines = [json.loads(l) for l in content.strip().split("\n")]
        check("jsonl 顶层 cwd 已改写为新路径", lines[0].get("cwd") == proj_b)
        check("消息文本里引用的旧路径未被改动",
              f"{proj_a}/logs" in lines[1]["content"][0]["text"])
        check("sessionId 保持稳定（防重导关键）", lines[0]["sessionId"] == sid1)
        # 嵌套对象里的同名 cwd 必须保持旧值（只改顶层那个）
        check("嵌套对象里的同名 cwd 未被改写",
              lines[3]["meta"]["cwd"] == proj_a and lines[3]["cwd"] == proj_b,
              f"meta={lines[3].get('meta')} top={lines[3].get('cwd')}")
    tr_b = home_b / ".workbuddy" / "projects" / slug_b / sid1 / "tool-results" / "cmd-1.txt"
    check("tool-results 目录已复制", tr_b.exists())
    meta_b = home_b / ".workbuddy" / "projects" / slug_b / f"{sid1}.meta.json"
    check("meta.json 已复制", meta_b.exists())
    check("todos 已复制", (home_b / ".workbuddy" / "todos" / f"{sid1}.json").exists())
    check("tasks 已复制", (home_b / ".workbuddy" / "tasks" / sid1 / "t1.json").exists())
    usage = db_query(home_b, "SELECT * FROM session_usage WHERE session_id = ?", (sid1,))
    check("usage 行已写入", len(usage) == 1)
    ws = db_query(home_b, "SELECT * FROM workspaces WHERE path = ?", (proj_b,))
    check("workspaces 登记了新路径", len(ws) == 1, f"实际 {len(ws)}")
    mem_b = Path(proj_b) / ".workbuddy" / "memory" / "MEMORY.md"
    check("工作区记忆已合并到新项目目录", mem_b.exists() and "源机写下的记忆" in mem_b.read_text(encoding="utf-8"))

    # ---- 3. 重复导入防重（同 id 覆盖语义）----
    print("\n== 重复导入：同 id 覆盖，不出双份 ==")
    r2 = run_script(["import", str(pkg), "--cwd", proj_b, "--on-conflict", "overwrite", "--force"],
                    home_b)
    check("重复导入退出码 0", r2.returncode == 0)
    rows2 = db_query(home_b, "SELECT id FROM sessions WHERE id IN (?, ?)", tuple(sids_a))
    check("会话数量仍为 2（无重复）", len(rows2) == 2, f"实际 {len(rows2)}")

    # 非交互默认跳过：全部跳过 = 未做任何改动 → 退出码 3（与 migrate.py 同口径）
    r3 = run_script(["import", str(pkg), "--cwd", proj_b, "--force"], home_b)
    rows3 = db_query(home_b, "SELECT id FROM sessions WHERE id IN (?, ?)", tuple(sids_a))
    check("非交互无 --on-conflict 默认跳过（退出码 3）",
          r3.returncode == 3 and len(rows3) == 2 and "跳过" in r3.stdout,
          f"退出码 {r3.returncode}")

    # ---- 4. dry-run 不写盘 ----
    print("\n== dry-run ==")
    n_before = len(db_query(home_b, "SELECT id FROM sessions"))
    r4 = run_script(["import", str(pkg), "--cwd", proj_b, "--dry-run", "--force"], home_b)
    n_after = len(db_query(home_b, "SELECT id FROM sessions"))
    check("dry-run 退出码 0", r4.returncode == 0)
    check("dry-run 未写盘", n_before == n_after)

    # ---- 5. 软冲突（同标题不同 id）默认跳过：用全新的 homeD 验证 ----
    print("\n== 软冲突 ==")
    home_d = tmp / "homeD"
    uid_d, proj_d, _sids_d = build_home_b(home_d)
    conn = sqlite3.connect(str(home_d / ".workbuddy" / "workbuddy.db"))
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, status, cwd, created_at, last_activity_at, "
        "updated_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), uid_d, "修复区间票退款 bug", "completed", proj_d, 1, 3, 3, None),
    )
    conn.commit()
    conn.close()
    # 默认（ask，非交互降级 skip）：软冲突跳过，本地同标题保持 1 条
    r5 = run_script(["import", str(pkg), "--cwd", proj_d, "--force"], home_d)
    n_soft = db_query(home_d, "SELECT COUNT(*) c FROM sessions WHERE title = '修复区间票退款 bug'")
    check("软冲突默认跳过（本地同标题保持 1 条）", n_soft[0]["c"] == 1, f"实际 {n_soft[0]['c']}")
    n_other = db_query(home_d, "SELECT COUNT(*) c FROM sessions WHERE title = '地图密钥安全优化'")
    check("无冲突的会话正常导入", n_other[0]["c"] == 1, f"实际 {n_other[0]['c']}")

    # --on-conflict overwrite：软冲突按「仍导入」处理（id 不同，不删本地那条）
    r5b = run_script(["import", str(pkg), "--cwd", proj_d,
                      "--on-conflict", "overwrite", "--force"], home_d)
    n_soft2 = db_query(home_d, "SELECT COUNT(*) c FROM sessions WHERE title = '修复区间票退款 bug'")
    check("--on-conflict overwrite 时软冲突也导入（2 条同标题）",
          r5b.returncode == 0 and n_soft2[0]["c"] == 2, f"实际 {n_soft2[0]['c']}")

    # ---- 6. 回滚：全新 home（不含历史导入）导入后回滚，验证彻底还原 ----
    print("\n== 回滚 ==")
    home_c = tmp / "homeC"
    _uid_c, proj_c, _sids_c = build_home_b(home_c)
    # 必须用 home_c 自己的项目路径：以前这里写的是 proj_b，
    # 工作区记忆会被追加进 homeB 的项目目录（跨 fixture 污染）
    r6 = run_script(["import", str(pkg), "--cwd", proj_c, "--on-conflict", "overwrite", "--force"],
                    home_c)
    check("homeC 导入成功", r6.returncode == 0, r6.stdout[-400:] + r6.stderr[-400:])
    import re as _re
    m_tag = _re.search(r"--rollback (\S+)", r6.stdout)
    check("能从输出解析回滚 tag", bool(m_tag))
    if not m_tag:
        print("跳过后续回滚断言")
        _print_summary(tmp)
        return 1 if FAIL else 0
    tag = m_tag.group(1)
    slug_c = slug_of(proj_c)
    # 前置条件：回滚前 {sid}/ 目录必须真的存在，否则下面的「没有残留空目录」恒真
    pre_dirs = [p for p in (home_c / ".workbuddy" / "projects" / slug_c).rglob(sids_a[0])
                if p.is_dir()]
    check("回滚前 {sid}/ 目录存在（空目录断言的前置条件）", bool(pre_dirs),
          f"projects/{slug_c} 下没有 {sids_a[0][:8]}… 目录")
    r7 = run_script(["--rollback", tag, "--yes", "--force"], home_c)
    check("rollback 退出码 0", r7.returncode == 0, r7.stdout[-500:] + r7.stderr[-500:])
    after = db_query(home_c, "SELECT id FROM sessions WHERE id IN (?, ?)", tuple(sids_a))
    check("回滚后导入的会话行已移除", len(after) == 0, f"实际 {len(after)}")
    jsonl_c = home_c / ".workbuddy" / "projects" / slug_c / f"{sid1}.jsonl"
    check("回滚后正文文件已删除", not jsonl_c.exists(), str(jsonl_c))
    # {sid}/ 与 {sid}/tool-results/ 是 stream_member_to_file 的 mkdir(parents=True)
    # 隐式建的，不在 created_files 里，必须靠 _prune_empty_dirs 收掉。
    # 判据：{sid}/ 目录本身必须不存在 —— 不能用「目录是否为空」，
    # 因为残留的往往正是「只含空子目录」的 {sid}/（它非空，但里面什么都没有）。
    sid_dir = home_c / ".workbuddy" / "projects" / slug_c / sids_a[0]
    check("回滚后 {sid}/ 目录已清理", not sid_dir.exists(), str(sid_dir))
    usage_c = db_query(home_c, "SELECT COUNT(*) c FROM session_usage WHERE session_id = ?", (sid1,))
    check("回滚后 usage 已清理", usage_c[0]["c"] == 0)

    # ---- 7. 路径不存在时拒绝导入 ----
    print("\n== 路径校验 ==")
    r8 = run_script(["import", str(pkg), "--cwd", str(tmp / "不存在的路径"), "--force"], home_b)
    check("目标路径不存在时拒绝导入", r8.returncode != 0 and "不存在" in (r8.stdout + r8.stderr))

    # ---- 8. --target-uid 的入口语义 ----
    print("\n== --target-uid 入口 ==")
    # 无子命令 = 向导路径，必须放行（第十四轮修过、第十六轮被我方守卫误拒过一次）
    r_tu = run_script(["--target-uid", "12345678-1234-1234-1234-123456789abc"],
                      home_b, expect_ok=False)
    # 非 TTY 下 cmd is None 走 print_help（不进 wizard），所以断言「没被守卫误拒」：
    # 回归时这里会打印「只用于导入」并 return 1
    check("--target-uid 无子命令不被误拒",
          "只用于导入" not in (r_tu.stdout + r_tu.stderr),
          (r_tu.stdout + r_tu.stderr)[-300:])
    # 子命令后写也给人话，而不是 argparse 的 unrecognized
    r_tu2 = run_script(["info", str(pkg), "--target-uid", "x"], home_b, expect_ok=False)
    check("info 后置 --target-uid 给明确提示",
          "只用于导入" in (r_tu2.stdout + r_tu2.stderr),
          (r_tu2.stdout + r_tu2.stderr)[-200:])

    # ---- 9. 大包导入的两层 O(n²) 优化（纯函数级）----
    print("\n== 按 sid 分组 + 按包内偏移单趟读取 ==")
    sys.path.insert(0, str(SCRIPT.parent))
    import migrate_project as mp  # type: ignore[import-not-found]
    from types import SimpleNamespace as _NS

    def _m(off):
        return _NS(offset_data=off)

    # 故意让字典序与 offset 顺序相反：旧实现按名字访问会反复回退 seek
    _members = {
        "projects/slugA/s1.meta.json": _m(500),
        "projects/slugA/s1.jsonl": _m(100),
        "projects/slugA/s1/tool-results/x": _m(300),
        "projects/slugB/s2.jsonl": _m(200),
        "todos/s1.json": _m(400),
        "tasks/s1/a.json": _m(600),
        "projects/slugA/s3.jsonl": _m(700),
    }
    _pj, _ax = mp.group_package_members(_members)
    # 分组按 . / / 边界逐级注册前缀，所以 key 会比 sid 多；关键是每个 sid 的组内成员正确
    check("分组：projects 按 sid 归位",
          all(k in _pj for k in ("s1", "s2", "s3"))
          and len(_pj["s1"]) == 3 and len(_pj["s2"]) == 1,
          str({k: len(v) for k, v in _pj.items()}))
    check("分组：todos / tasks 按 sid 归位",
          sorted(_ax) == ["s1"] and len(_ax["s1"]) == 2,
          str({k: len(v) for k, v in _ax.items()}))
    check("组内按 offset_data 单调递增",
          all([m.offset_data for _a, m in pairs]
              == sorted(m.offset_data for _a, m in pairs)
              for pairs in list(_pj.values()) + list(_ax.values())),
          str({k: [m.offset_data for _a, m in v] for k, v in _pj.items()}))
    _plan = [({"id": "s2", "title": "B"}, "insert", ""),
             ({"id": "s9", "title": "none"}, "insert", ""),
             ({"id": "s1", "title": "A"}, "overwrite", "")]
    _ordered = [it[0]["id"] for it in mp.plan_in_tar_order(_plan, _pj, _ax)]
    check("plan 按包内首偏移排序，无成员会话排最后",
          _ordered == ["s1", "s2", "s9"], str(_ordered))

    # 语义不变：用分组子列表算出的 created_files 必须与全量扫描完全一致
    class _EP:
        projects_dir = Path(tmp) / "homeB" / "projects"
        todos_dir = Path(tmp) / "homeB" / "todos"
        tasks_dir = Path(tmp) / "homeB" / "tasks"

    _all_pj = [(a, m) for a, m in _members.items() if a.startswith("projects/")]
    _all_ax = [(a, m) for a, m in _members.items()
               if a.startswith("todos/") or a.startswith("tasks/")]
    for _sid in ("s1", "s2"):
        _full = mp._planned_files(_all_pj, _all_ax, _sid, "slugB", _EP)
        _grp = mp._planned_files(_pj.get(_sid, ()), _ax.get(_sid, ()),
                                 _sid, "slugB", _EP)
        check(f"分组不改变 created_files 语义（{_sid}）",
              sorted(_full) == sorted(_grp), f"{_full} vs {_grp}")

    # 含点 session id：分组不能只按第一个点切，否则 abc.def 会漏收
    _dot_sid = "abc.def"
    _dot_members = {
        "projects/slugA/abc.def.jsonl": _m(100),
        "projects/slugA/abc.jsonl": _m(200),
    }
    _dp, _da = mp.group_package_members(_dot_members)
    check("含点 sid：分组仍能命中完整 id",
          _dot_sid in _dp
          and [a for a, _ in _dp[_dot_sid]] == ["projects/slugA/abc.def.jsonl"],
          str({k: [a for a, _ in v] for k, v in _dp.items()}))
    _dall = list(_dot_members.items())
    _dfull = mp._planned_files(_dall, [], _dot_sid, "slugB", _EP)
    _dgrp = mp._planned_files(_dp.get(_dot_sid, ()), [], _dot_sid, "slugB", _EP)
    check("含点 sid：created_files 语义与全量扫描一致",
          _dfull == _dgrp, f"{_dfull} vs {_dgrp}")

    # ---- 汇总 ----
    _print_summary(tmp)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
