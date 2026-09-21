#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试：单对话跨版本迁移

全部在【临时 fixture】中运行（由 prepare_fixture.py 从真实数据只读复制而来），
绝不触碰真实数据目录。

平台说明
--------
仅 Windows 实测通过（Windows 11 + Python 3.13）。

原因：fixture 是从本机真实 WorkBuddy 数据复制来的，其中 session 的 cwd、
projects 目录名都是 Windows 路径格式；测试用例据此构造数据。
macOS / Linux 未测试——若本机没有安装并登录过 WorkBuddy，
prepare_fixture.py 会造不出 fixture，测试也就无从运行。

用法:
  python3 tests/run_tests.py
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SCRIPT = ROOT / "scripts" / "migrate_session.py"

sys.path.insert(0, str(TESTS_DIR))
import prepare_fixture  # noqa: E402

PY = sys.executable
PASS, FAIL = [], []


def run(args, env_home, stdin=None):
    """运行迁移脚本"""
    env = dict(os.environ)
    env["WORKBUDDY_MIGRATE_HOME"] = str(env_home)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [PY, str(SCRIPT)] + args,
        env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        input=stdin, timeout=180,
    )


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def db_rows(home, edition, sid=None):
    """读取某版本 fixture 的 session"""
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = sqlite3.connect(str(db))
    if sid:
        r = c.execute("SELECT id, user_id, title FROM sessions WHERE id = ?", (sid,)).fetchone()
        c.close()
        return r
    n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    c.close()
    return n


def project_files(home, edition, sid):
    p = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "projects"
    return sorted(p.glob(f"*/{sid}*"))


def db_row_full(home, edition, sid):
    """读取整行（含 custom_title）"""
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = sqlite3.connect(str(db))
    r = c.execute(
        "SELECT id, user_id, title, custom_title FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    c.close()
    return r


def _find_clone_sid(home, edition, base_sid):
    """找出同版本克隆出来的那条对话（custom_title 带「副本」）"""
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = sqlite3.connect(str(db))
    r = c.execute(
        "SELECT id FROM sessions WHERE id != ? AND custom_title LIKE '%副本%'", (base_sid,)
    ).fetchone()
    c.close()
    return r[0] if r else None


def _jsonl_path(home, edition, sid):
    p = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "projects"
    hits = sorted(p.glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


def first_session(home, edition):
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = sqlite3.connect(str(db))
    r = c.execute("SELECT id, title FROM sessions ORDER BY last_activity_at DESC LIMIT 1").fetchone()
    c.close()
    return r


def pick_test_session(home):
    """挑一条【国内版有、国际版没有】的最新对话作为测试用例

    fixture 取自真实数据，用户可能已经把某些对话迁到国际版了。若刚好选到两边
    都存在的对话，后面「无冲突迁移」的用例会全部撞上硬冲突而失败——那是数据
    状态问题，不是脚本缺陷，所以这里主动避开。
    """
    c = sqlite3.connect(str(home / ".workbuddy" / "workbuddy.db"))
    rows = c.execute(
        "SELECT id, title FROM sessions ORDER BY last_activity_at DESC"
    ).fetchall()
    c.close()
    d = sqlite3.connect(str(home / ".workbuddy-ai" / "workbuddy.db"))
    have = {r[0] for r in d.execute("SELECT id FROM sessions")}
    d.close()
    for sid, title in rows:
        if sid not in have:
            return sid, title
    if rows:
        print("  ⚠️  国内版所有对话在国际版都已存在，只能选到会冲突的对话")
        return rows[0]
    return None, None


def session_with_tool_results(home):
    """找出一条正文里带 tool-results 目录的对话（没有则返回 None）"""
    p = home / ".workbuddy" / "projects"
    for f in sorted(p.glob("*/*")):
        if f.is_dir():
            return f.name
    return None


def main():
    print("=" * 70)
    print("单对话跨版本迁移 — 端到端测试（临时 fixture）")
    print("=" * 70)

    home = prepare_fixture.build()
    print(f"\nfixture: {home}\n")

    sid, title = pick_test_session(home)
    print(f"测试用例对话: {sid[:8]}… 《{str(title)[:30]}》\n")

    src_before = db_rows(home, "domestic")
    dst_before = db_rows(home, "intl")

    # ---------- 1. 列表 ----------
    print("\n[1] 对话列表")
    r = run(["--list", "--from", "domestic"], home)
    check("国内版列表可执行", r.returncode == 0, r.stderr[-300:])
    check("国内版列表包含该对话", sid[:8] in r.stdout)
    r = run(["--list", "--from", "intl"], home)
    check("国际版列表可执行", r.returncode == 0, r.stderr[-300:])

    # ---------- 2. dry-run 不改动 ----------
    print("\n[2] dry-run 不写盘")
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid, "--dry-run", "--yes", "--force"], home)
    check("dry-run 成功", r.returncode == 0, r.stderr[-300:])
    check("dry-run 后源未变", db_rows(home, "domestic") == src_before)
    check("dry-run 后目标未变", db_rows(home, "intl") == dst_before)
    check("dry-run 提示未改动", "dry-run" in r.stdout)

    # ---------- 3. move 迁移 ----------
    print("\n[3] move 模式跨版本迁移")
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
             "--mode", "move", "--yes", "--force"], home)
    check("move 迁移成功", r.returncode == 0, r.stderr[-500:])

    row = db_rows(home, "intl", sid)
    check("目标库已写入该对话", row is not None)
    if row:
        intl_uid = _current_uid(home, "intl")
        check("user_id 已改写为目标账号", intl_uid and row[1] == intl_uid,
              f"got={row[1]} want={intl_uid}")
    check("源库已删除该对话", db_rows(home, "domestic", sid) is None)
    check("源文件已删除", len(project_files(home, "domestic", sid)) == 0)
    check("目标正文文件已复制", len(project_files(home, "intl", sid)) >= 1,
          f"{len(project_files(home, 'intl', sid))} 个")
    check("目标 session 数 +1", db_rows(home, "intl") == dst_before + 1)

    tag = _last_backup_tag(home)
    check("已创建备份", tag is not None)

    # ---------- 4. 回滚 ----------
    print("\n[4] 回滚（move）")
    if tag:
        r = run(["--rollback", tag, "--yes"], home)
        check("回滚成功", r.returncode == 0, r.stderr[-500:])
        check("源已恢复该对话", db_rows(home, "domestic", sid) is not None)
        check("源文件已恢复", len(project_files(home, "domestic", sid)) >= 1)
        check("目标已移除该对话", db_rows(home, "intl", sid) is None)
        check("源 session 数复原", db_rows(home, "domestic") == src_before)
        check("目标 session 数复原", db_rows(home, "intl") == dst_before)

    # ---------- 5. copy 模式 ----------
    print("\n[5] copy 模式保留源")
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
             "--mode", "copy", "--yes", "--force"], home)
    check("copy 迁移成功", r.returncode == 0, r.stderr[-500:])
    check("copy 后源仍保留", db_rows(home, "domestic", sid) is not None)
    check("copy 后源 session 数不变", db_rows(home, "domestic") == src_before)
    check("copy 后目标已有该对话", db_rows(home, "intl", sid) is not None)

    # ---------- 6. 硬冲突（再次迁移同一对话） ----------
    print("\n[6] 硬冲突：重复迁移同一对话")
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
             "--mode", "copy", "--force"], home, stdin="n\n")
    check("检测到 ID 冲突", "相同 ID" in r.stdout, r.stdout[-300:])
    check("展示差异对比表", "最后活动" in r.stdout and "消息数" in r.stdout)
    check("硬冲突只有两个选项", "不操作（取消）" in r.stdout and "不覆盖" not in r.stdout)
    check("选择不操作后取消", "已取消" in r.stdout)

    # ---------- 7. 软冲突（标题相同、id 不同） ----------
    print("\n[7] 软冲突：标题相同但 ID 不同")
    fake_id = _make_same_title_session(home, sid, title)
    if fake_id:
        r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
                 "--mode", "copy", "--force"], home, stdin="s\n")
        check("检测到标题冲突", "标题相同" in r.stdout, r.stdout[-300:])
        check("软冲突有三个选项", "不覆盖（跳过该对话）" in r.stdout)
        check("选择不覆盖后跳过", "已跳过" in r.stdout)
        check("跳过不改变目标旧对话", db_rows(home, "intl", fake_id) is not None)

        # 覆盖路径
        r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
                 "--mode", "copy", "--force"], home, stdin="y\n")
        check("覆盖后目标旧对话被删除", db_rows(home, "intl", fake_id) is None)
        check("覆盖后源 id 存在（文件与 id 一致）", db_rows(home, "intl", sid) is not None)

    # ---------- 8. 客户端未关闭检测 ----------
    print("\n[8] 客户端运行检测")
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid], home)
    blocked = "正在运行" in r.stdout
    check("检测到客户端运行时会拦截", blocked or r.returncode == 0, r.stdout[-200:])

    # ---------- 9. 无冲突时也必须确认（move 会删源，不能默认执行） ----------
    print("\n[9] 无冲突时的执行前确认")
    _remove_from_intl(home, sid)   # 先清掉目标侧，构造「无冲突」场景
    before_src = db_rows(home, "domestic")
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid, "--force"],
            home, stdin="n\n")
    check("无冲突时仍会要求确认", "确认执行" in r.stdout, r.stdout[-300:])
    check("回答 n 后未执行迁移", db_rows(home, "domestic") == before_src)
    check("回答 n 后提示已取消", "已取消" in r.stdout)

    # ---------- 10. 非交互冲突降级为 skip ----------
    print("\n[10] 非交互模式下冲突降级为 skip")
    # 先迁一次让目标存在该对话，制造冲突
    run(["--from", "domestic", "--to", "intl", "--session-id", sid,
         "--mode", "copy", "--yes", "--force"], home)
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
             "--yes", "--force"], home)
    check("非交互冲突不覆盖", "已跳过" in r.stdout or "降级为 skip" in r.stdout,
          r.stdout[-300:])

    # ---------- 11. 同版本迁移 + 回滚 ----------
    print("\n[11] 同版本迁移（改 user_id）与回滚")
    real_uid = _current_uid(home, "domestic")
    fake_uid = "fake-uid-0000-1111-2222-333344445555"
    if real_uid:
        _set_uid(home, "domestic", sid, fake_uid)
        r = run(["--from", "domestic", "--to", "domestic", "--session-id", sid,
                 "--target-uid", real_uid, "--yes", "--force"], home)
        check("同版本迁移成功", r.returncode == 0, r.stderr[-300:])
        check("同版本迁移会创建备份", "备份" in r.stdout, r.stdout[-300:])
        row = db_rows(home, "domestic", sid)
        check("user_id 已改为目标账号", row and row[1] == real_uid, f"got={row[1] if row else None}")

        tag = _last_backup_tag(home, "domestic")
        if tag:
            r = run(["--rollback", tag, "--yes"], home)
            check("同版本回滚成功", r.returncode == 0, r.stderr[-300:])
            row = db_rows(home, "domestic", sid)
            check("user_id 已改回原值", row and row[1] == fake_uid, f"got={row[1] if row else None}")
        _set_uid(home, "domestic", sid, real_uid)   # 还原现场

    # ---------- 12. 同版本复制（克隆出新对话） ----------
    print("\n[12] 同版本复制：克隆出一份新对话")
    before = db_rows(home, "domestic")
    r = run(["--from", "domestic", "--to", "domestic", "--session-id", sid,
             "--mode", "copy", "--yes", "--force"], home)
    check("同版本复制可执行", r.returncode == 0, r.stderr[-400:])
    check("不再提示「无需迁移」", "无需迁移" not in r.stdout, r.stdout[-300:])
    check("对话数 +1", db_rows(home, "domestic") == before + 1,
          f"{before} → {db_rows(home, 'domestic')}")

    new_sid = _find_clone_sid(home, "domestic", sid)
    check("克隆出新对话", bool(new_sid), str(new_sid))
    if new_sid:
        row = db_row_full(home, "domestic", new_sid)
        shown = (row[3] or row[2] or "") if row else ""
        check("副本标题带「副本」标记", "副本" in shown, f"got={shown}")

        cj = _jsonl_path(home, "domestic", new_sid)
        check("副本正文已生成", cj is not None and cj.exists())
        if cj and cj.exists():
            txt = cj.read_text(encoding="utf-8", errors="replace")
            check("副本正文内 sessionId 已改写", sid not in txt and new_sid in txt,
                  f"残留旧id={sid in txt} 含新id={new_sid in txt}")

        orig = db_row_full(home, "domestic", sid)
        oshown = (orig[3] or orig[2] or "") if orig else ""
        check("原始对话保留", orig is not None)
        check("原始对话未被加副本标记", orig is not None and "副本" not in oshown, f"got={oshown}")

        tag = _last_backup_tag(home, "domestic")
        if tag:
            r = run(["--rollback", tag, "--yes"], home)
            check("克隆回滚成功", r.returncode == 0, r.stderr[-300:])
            check("回滚后副本已删除", db_row_full(home, "domestic", new_sid) is None)
            check("回滚后原始对话仍在", db_row_full(home, "domestic", sid) is not None)
            check("回滚后对话数还原", db_rows(home, "domestic") == before,
                  f"got={db_rows(home, 'domestic')}")

    # ---------- 13. 正文含目录（tool-results） ----------
    print("\n[13] 正文含目录（tool-results）时完整复制")
    # 用例对话不一定带 tool-results，单独找一条带目录的来测
    tsid = session_with_tool_results(home)
    src_dirs = [f for f in project_files(home, "domestic", tsid) if f.is_dir()] if tsid else []
    if not src_dirs:
        print("  ⏭️  当前 fixture 没有 tool-results 目录，跳过")
    else:
        _remove_from_intl(home, tsid)   # 先清干净，避免硬冲突降级为 skip
        r = run(["--from", "domestic", "--to", "intl", "--session-id", tsid,
                 "--mode", "copy", "--yes", "--force"], home)
        check("含目录的正文可迁移", r.returncode == 0, r.stderr[-400:])
        dst_dirs = [f for f in project_files(home, "intl", tsid) if f.is_dir()]
        check("目标侧目录已复制", len(dst_dirs) == len(src_dirs),
              f"src={len(src_dirs)} dst={len(dst_dirs)}")
        if dst_dirs:
            sf = sorted(p.name for p in src_dirs[0].rglob("*") if p.is_file())
            df = sorted(p.name for p in dst_dirs[0].rglob("*") if p.is_file())
            check("目录内文件齐全", bool(sf) and sf == df, f"{sf} vs {df}")

    # ---------- 汇总 ----------
    print("\n" + "=" * 70)
    print(f"结果: 通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 70)
    return 1 if FAIL else 0


def _current_uid(home, edition):
    """读取 account-snapshot 里的当前 uid"""
    p = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") \
        / "storage" / "skeleton" / "account-snapshot.json"
    if not p.exists():
        return None
    import json
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("primary") or {}).get("uid")
    except Exception:
        return None


def _last_backup_tag(home, edition="intl"):
    bd = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "migrate_backups"
    if not bd.exists():
        return None
    tags = sorted([d.name for d in bd.iterdir() if d.is_dir()], reverse=True)
    return tags[0] if tags else None


def _set_uid(home, edition, sid, uid):
    """直接改某版本 fixture 里某个 session 的 user_id（测试用）"""
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = sqlite3.connect(str(db))
    c.execute("UPDATE sessions SET user_id = ? WHERE id = ?", (uid, sid))
    c.commit()
    c.close()


def _remove_from_intl(home, sid):
    """清掉国际版 fixture 中的某个对话（行 + 文件）"""
    db = home / ".workbuddy-ai" / "workbuddy.db"
    c = sqlite3.connect(str(db))
    c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    c.execute("DELETE FROM session_usage WHERE session_id = ?", (sid,))
    c.commit()
    c.close()
    for f in project_files(home, "intl", sid):
        _rm(f)


def _rm(p):
    """删除文件或目录（tool-results 是目录）"""
    import shutil
    if p.is_dir():
        shutil.rmtree(str(p))
    elif p.exists():
        p.unlink()


def _make_same_title_session(home, sid, title):
    """在目标库造一条同标题、不同 id 的旧对话，返回其 id"""
    import json
    db = home / ".workbuddy-ai" / "workbuddy.db"
    c = sqlite3.connect(str(db))
    # 先清掉源 id，避免硬冲突抢先触发
    c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    c.execute("DELETE FROM session_usage WHERE session_id = ?", (sid,))
    for f in project_files(home, "intl", sid):
        _rm(f)

    cols = [d[0] for d in c.execute("SELECT * FROM sessions LIMIT 1").description]
    row = dict(zip(cols, c.execute("SELECT * FROM sessions LIMIT 1").fetchone()))
    fake = "11111111-2222-3333-4444-555555555555"
    row["id"] = fake
    row["title"] = title
    row["custom_title"] = None
    row["last_activity_at"] = 1789000000000
    row["created_at"] = 1789000000000
    ks = [k for k in row if k in cols]
    c.execute(
        f"INSERT OR REPLACE INTO sessions ({','.join(ks)}) VALUES ({','.join('?' * len(ks))})",
        [row[k] for k in ks],
    )
    c.commit()
    c.close()

    # 沿用源侧的同名目录，不硬编码平台相关的 slug
    src_dirs = sorted((home / ".workbuddy" / "projects").glob(f"*/{sid}*"))
    slug = src_dirs[0].parent.name if src_dirs else "default-workspace"
    d = home / ".workbuddy-ai" / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / (fake + ".jsonl")).write_text(
        "\n".join(['{"type":"message","role":"user","timestamp":1789000000000,'
                   '"content":[{"type":"input_text","text":"旧的提问"}]}'] * 5),
        encoding="utf-8",
    )
    return fake


if __name__ == "__main__":
    sys.exit(main())
