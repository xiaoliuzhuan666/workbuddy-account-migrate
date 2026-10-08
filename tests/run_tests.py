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

import atexit
import hashlib
import io
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

# Windows 终端默认可能是 GBK/CP936，打印第一个 emoji 就会 UnicodeEncodeError，
# 整个套件直接崩 —— 与文件里反复声明的「仅 Windows 实测通过」自相矛盾。
# 与 scripts/migrate.py / migrate_session.py 同一套包装；先判断编码再包，
# 避免二次包装把上一个 wrapper 里块缓冲的输出丢掉。
if platform.system() == "Windows":
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name)
        _enc = (getattr(_stream, "encoding", "") or "").lower().replace("-", "")
        if _enc != "utf8":
            try:
                _stream.flush()
                setattr(
                    sys, _name,
                    io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace", line_buffering=True),
                )
            except Exception:
                pass


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SCRIPT = ROOT / "scripts" / "migrate_session.py"

sys.path.insert(0, str(TESTS_DIR))
import prepare_fixture  # noqa: E402

PY = sys.executable
PASS, FAIL = [], []

# 测试 helper 打开的 sqlite 连接统一登记，进程退出时兜底关闭。
# 以前每个 helper 都是 `c = sqlite3.connect(...)` + 末尾 `c.close()`，中间任何
# 异常 / sys.exit 都会泄漏 fd（生产代码早已改成 finally，这里是漏网的一批）。
_OPEN_CONNS = []


def _conn(db):
    """测试用 sqlite 连接：登记到 _OPEN_CONNS，由 atexit 兜底关闭

    连接前必须校验库文件存在：sqlite3.connect 对不存在的路径会**凭空创建空库**，
    半清理的 fixture 上一跑就多出几个空 workbuddy.db，之后报的
    "no such table: sessions" 看起来像数据损坏（pick_test_session 的注释里
    点名过这个坑，这里是把它落到 helper 层）。
    """
    p = Path(str(db))
    if not p.exists():
        raise AssertionError(f"fixture 缺少数据库：{p}（不会自动创建空库）")
    c = sqlite3.connect(str(p))
    _OPEN_CONNS.append(c)
    return c


def _new_conn(db):
    """与 _conn() 相对：目标库文件本来就该由用例自己新建，所以不做存在性校验

    同样登记到 _OPEN_CONNS —— 单元测试里用例失败会 raise 出 KeyboardInterrupt
    之外的任意异常，裸 `sqlite3.connect` + 末尾 close() 的写法一律泄漏句柄。
    """
    c = sqlite3.connect(str(db))
    _OPEN_CONNS.append(c)
    return c


def _close_open_conns():
    while _OPEN_CONNS:
        try:
            _OPEN_CONNS.pop().close()
        except Exception:
            pass


atexit.register(_close_open_conns)


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
    c = _conn(str(db))
    # 与生产侧对齐：异常路径也要关（_OPEN_CONNS 的 atexit 只是第二道保险）
    try:
        if sid:
            return c.execute(
                "SELECT id, user_id, title FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
        return c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        c.close()


def project_files(home, edition, sid):
    p = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "projects"
    return sorted(p.glob(f"*/{sid}*"))


def db_row_full(home, edition, sid):
    """读取整行（含 custom_title）"""
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    try:
        return c.execute(
            "SELECT id, user_id, title, custom_title FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
    finally:
        c.close()


def _find_clone_sid(home, edition, base_sid):
    """找出同版本克隆出来的那条对话

    不能只取 custom_title LIKE '%副本%' 的第一条：fixture 里本来可能就有别的
    副本（甚至是上一个用例留下的），断言会锁到错的那条、时好时坏。
    先按「原标题 + （副本）」精确匹配（_clone_title 的规则），都匹配不上才退回
    最近插入的那条。
    """
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    try:
        row = c.execute(
            "SELECT COALESCE(custom_title, title, '') FROM sessions WHERE id = ?", (base_sid,)
        ).fetchone()
        want = ((row[0] or "").strip() + "（副本）") if row else ""
        if want:
            hit = c.execute(
                "SELECT id FROM sessions WHERE id != ? "
                "AND COALESCE(custom_title, title, '') = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (base_sid, want),
            ).fetchone()
            if hit:
                return hit[0]
        r = c.execute(
            "SELECT id FROM sessions WHERE id != ? AND custom_title LIKE '%副本%' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (base_sid,),
        ).fetchone()
        return r[0] if r else None
    finally:
        c.close()


def _jsonl_path(home, edition, sid):
    p = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "projects"
    hits = sorted(p.glob(f"*/{sid}.jsonl"))
    return hits[0] if hits else None


def first_session(home, edition):
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    try:
        r = c.execute("SELECT id, title FROM sessions ORDER BY last_activity_at DESC LIMIT 1").fetchone()
    finally:
        c.close()
    return r


def pick_test_session(home) -> tuple:
    """挑一条【国内版有、国际版没有】的最新对话作为测试用例

    fixture 取自真实数据，用户可能已经把某些对话迁到国际版了。若刚好选到两边
    都存在的对话，后面「无冲突迁移」的用例会全部撞上硬冲突而失败——那是数据
    状态问题，不是脚本缺陷，所以这里主动避开。
    """
    dom_db = home / ".workbuddy" / "workbuddy.db"
    intl_db = home / ".workbuddy-ai" / "workbuddy.db"
    for db in (dom_db, intl_db):
        if not db.exists():
            # 没装对应版本时 fixture 里根本没有库；sqlite3.connect 会凭空建一个空文件，
            # 后面 "no such table: sessions" 直接 traceback，整个套件没有友好退路
            print(f"❌ fixture 缺少数据库：{db}")
            print("   本机需要安装并登录过 WorkBuddy（国内版/国际版）才能造出 fixture。")
            sys.exit(1)
    # 这两连接不能靠"末尾 c.close()"兜底：中间任何 sys.exit / 异常都会跳过它，
    # 残留句柄会让下一次 build() 清理 fixture 时报 WinError 32/145
    c = sqlite3.connect(str(dom_db))
    try:
        if not c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone():
            print(f"❌ fixture 的库里没有 sessions 表：{dom_db}")
            sys.exit(1)
        rows = c.execute(
            "SELECT id, title FROM sessions ORDER BY last_activity_at DESC"
        ).fetchall()
    finally:
        c.close()
    d = sqlite3.connect(str(intl_db))
    # 国际版的库同样要先确认有 sessions 表：只查国内版的话，国际版缺表会直接
    # OperationalError，整个套件崩掉而不是给一句可操作的提示
    try:
        if not d.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone():
            print(f"❌ fixture 的库里没有 sessions 表：{intl_db}")
            print("   本机需要安装并登录过 WorkBuddy（国际版）才能造出 fixture。")
            sys.exit(1)
        have = {r[0] for r in d.execute("SELECT id FROM sessions")}
    finally:
        d.close()
    for sid, title in rows:
        if sid not in have:
            return str(sid), str(title or "")
    if rows:
        print("  ⚠️  国内版所有对话在国际版都已存在，只能选到会冲突的对话")
        return str(rows[0][0]), str(rows[0][1] or "")
    # fixture 里一条对话都没有，后续用例无从运行
    print("❌ fixture 里没有任何对话，无法运行测试")
    sys.exit(1)


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

    # fixture 目录默认**每次运行独立**：固定路径 + 本机常驻的 WorkBuddy 客户端，
    # 交叠运行会互相删掉对方的库；客户端 / 索引服务偶尔锁住被复制过来的
    # projects 子目录，rmtree 半清理后留下的残缺 fixture 会传染给下一次运行。
    if not os.environ.get(prepare_fixture.FIXTURE_DIR_ENV):
        unique = f"{prepare_fixture.FIXTURE_NAME}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        os.environ[prepare_fixture.FIXTURE_DIR_ENV] = str(
            Path(tempfile.gettempdir()) / unique)

    home = prepare_fixture.build()

    # 本机只有国内版数据时，跨版本端到端用例（domestic ⇄ intl）根本无从运行。
    # 过去这里会直接抛 sqlite3 "unable to open database file" traceback，看着像脚本坏了，
    # 实际是环境缺数据（README「项目结构」已注明）。改成显式跳过，退出码 0。
    if not (home / ".workbuddy-ai" / "workbuddy.db").exists():
        print()
        print("⏭️  跳过端到端测试：本机没有国际版数据（~/.workbuddy-ai/workbuddy.db）。")
        print("   本套用例全部是「国内版 ⇄ 国际版」跨版本场景，fixture 只能造出国内版一半。")
        print("   这不是脚本缺陷，属于环境限制。要跑全量请先在另一版本里创建过对话。")
        return 0

    print(f"\nfixture: {home}\n")
    # ⚠️ 用例编号 = 执行序（打印出来就是 [1] [2] … 依次递增）。
    # 历史上多次被破坏，成因都一样：
    # 编号与「文件里的先后」对不上，读源码的人按文件顺序核对就得出错误结论。
    # 现在两者**刻意做成一致**：main() 里只留 [1]~[21] 的内联用例，[22]~[27]
    # 全是函数，且**定义顺序 = 调用顺序**（unit_tests → migrate_py_e2e →
    # cli_entrypoints_e2e → task_restore_e2e → _case_full_copy_keeps_source →
    # _case_full_requires_rollback）。
    # 加用例时：新的一段写成函数放在对应位置，取「它在执行序列里的下一个号」。
    # 改完用 `python tests/run_tests.py | grep '^\['` 核对是否严格递增。
    # 跑完（含中途异常）自动清理：fixture 里是真实对话正文与记忆，长期留在系统
    # 临时目录等于持续泄漏个人数据。调试时设 WB_KEEP_FIXTURE=1 可保留。
    if not os.environ.get("WB_KEEP_FIXTURE"):
        # ⚠️ 顺序要紧：atexit 是 LIFO，两个清理分别注册的话，后注册的
        # prepare_fixture.clean 会**先**跑 —— 而此时测试用的 sqlite 连接还开着
        # （Windows 上打开的句柄会让 rmtree 报 WinError 145），fixture 就删不掉，
        # 里面是真实对话正文与记忆的副本。所以合成一个：先关连接，再删目录。
        def _cleanup_all():
            _close_open_conns()
            prepare_fixture.clean()

        atexit.register(_cleanup_all)

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
    # 计划里的备份落点必须和实际一致（单对话备份在 migrate_backups/session/，
    # 以前只打印 migrate_backups/）
    check("dry-run 计划的备份落点含 session/", "session/" in r.stdout, r.stdout[-300:])

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
        r = run(["--rollback", tag, "--yes", "--force"], home)
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
    # 注：无法在测试里真的拉起客户端，所以不再断言"被拦截"（那样会因
    # `blocked or returncode == 0` 恒真而失去意义）。改为断言检测分支确实执行了，
    # 真正的"排除自身进程 / 检测失败不上报"放到单元用例里验证。
    print("\n[8] 客户端运行检测分支被执行")
    # 关键回归点：仓库目录名含 "workbuddy"，脚本自己的命令行会命中进程关键字。
    # 若没有排除自身进程，这里会被"检测到客户端正在运行"无条件拦住。
    _remove_from_intl(home, sid)   # 清掉目标侧，避免撞上硬冲突而不是在测进程检测
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
             "--dry-run", "--force"], home)
    check("--force 时 dry-run 正常执行", r.returncode == 0 and "dry-run" in r.stdout,
          r.stdout[-300:])
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid, "--dry-run"], home)
    check("不带 --force 时不会被脚本自身误拦",
          r.returncode == 0 and "正在运行" not in r.stdout, r.stdout[-300:])

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
            r = run(["--rollback", tag, "--yes", "--force"], home)
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
        if cj is not None and cj.exists() and new_sid:
            txt = cj.read_text(encoding="utf-8", errors="replace")
            # 只应改写 "sessionId" 字段值；消息正文里引用到的旧 id 必须原样保留。
            # 与改写逻辑共用同一套正则（容忍 "sessionId": "<sid>" 这种带空格写法），
            # 否则会出现「改得到、查不出」的漏报。
            field_re = re.compile(r'("sessionId"\s*:\s*")' + re.escape(sid) + r'(")')

            def _text_refs(s):
                """正文里非字段形态的旧 id 出现次数"""
                return len(re.findall(re.escape(sid), s)) - len(field_re.findall(s))

            check("副本正文内 sessionId 字段已改写", f'"sessionId":"{new_sid}"' in txt,
                  f'未找到 "sessionId":"{new_sid}"')
            check("副本正文不再残留旧的 sessionId 字段", not field_re.search(txt))
            # 「消息正文里引用的旧 id 未被改写」这条语义要靠**计数对拍**验证：
            # 以前写成 `字段不在 txt 且 new_sid 在 txt`，与它声称的语义无关。
            src_jsonl = _jsonl_path(home, "domestic", sid)
            src_txt = (src_jsonl.read_text(encoding="utf-8", errors="replace")
                       if src_jsonl is not None else "")
            check("消息正文里引用的旧 id 未被改写",
                  _text_refs(txt) == _text_refs(src_txt),
                  f"副本 {_text_refs(txt)} 处 vs 源 {_text_refs(src_txt)} 处")

        orig = db_row_full(home, "domestic", sid)
        oshown = str((orig[3] or orig[2] or "") if orig else "")
        check("原始对话保留", orig is not None)
        check("原始对话未被加副本标记", orig is not None and "副本" not in oshown, f"got={oshown}")

        tag = _last_backup_tag(home, "domestic")
        if tag:
            r = run(["--rollback", tag, "--yes", "--force"], home)
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

    # ---------- 14. 跨账号 + copy：源必须保留 ----------
    print("\n[14] 跨账号 + --mode copy：源对话必须保留（不能退化成归属转移）")
    csid, _ctitle = pick_test_session(home)
    other_uid = "other-uid-0000-1111-2222-333344445555"
    dom_uid = _current_uid(home, "domestic")
    if csid and dom_uid:
        _remove_from_intl(home, csid)
        _set_uid(home, "domestic", csid, other_uid)
        before_n = db_rows(home, "domestic")
        r = run(["--from", "domestic", "--to", "domestic", "--session-id", csid,
                 "--mode", "copy", "--target-uid", dom_uid, "--yes", "--force"], home)
        check("跨账号 copy 可执行", r.returncode == 0, r.stderr[-300:])
        src_row = db_rows(home, "domestic", csid)
        check("copy 后源对话仍在", src_row is not None)
        check("copy 后源归属没被改走", src_row is not None and src_row[1] == other_uid,
              f"got={src_row[1] if src_row else None}")
        check("copy 后对话数 +1", db_rows(home, "domestic") == before_n + 1,
              f"{before_n} → {db_rows(home, 'domestic')}")
        clone_sid = _find_clone_sid(home, "domestic", csid)
        if clone_sid:
            crow = db_rows(home, "domestic", clone_sid)
            check("副本归属目标账号", crow and crow[1] == dom_uid,
                  f"got={crow[1] if crow else None}")
        tag = _last_backup_tag(home, "domestic")
        if tag:
            run(["--rollback", tag, "--yes", "--force"], home)
        _set_uid(home, "domestic", csid, dom_uid)   # 还原现场

    # ---------- 15. 软冲突覆盖：回滚要还原被删对话的 usage ----------
    print("\n[15] 软冲突覆盖后回滚：被删对话的 session_usage 要还原")
    fake_id = _make_same_title_session(home, sid, title)
    if fake_id:
        _ensure_usage(home, "intl", fake_id)
        check("构造：被覆盖对话有 usage", _usage_exists(home, "intl", fake_id))
        r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
                 "--mode", "move", "--force"], home, stdin="y\ny\n")
        check("覆盖执行成功", r.returncode == 0, r.stderr[-300:])
        check("覆盖后旧对话已删除", db_rows(home, "intl", fake_id) is None)
        tag = _last_backup_tag(home, "intl")
        if tag:
            rr = run(["--rollback", tag, "--yes", "--force"], home)
            check("覆盖场景回滚成功", rr.returncode == 0, rr.stderr[-300:])
            check("回滚后旧对话恢复", db_rows(home, "intl", fake_id) is not None)
            check("回滚后旧对话 usage 恢复", _usage_exists(home, "intl", fake_id))
        _remove_from_intl(home, fake_id)

    # ---------- 16. cwd 为空：正文不得落到 projects 根目录 ----------
    print("\n[16] 会话 cwd 为空时，正文不得落到 projects 根目录")
    esid, _etitle = first_session(home, "domestic")
    if esid:
        origin_cwd = _get_cwd(home, "domestic", esid)
        _remove_from_intl(home, esid)
        _set_cwd(home, "domestic", esid, "")
        r = run(["--from", "domestic", "--to", "intl", "--session-id", esid,
                 "--mode", "copy", "--yes", "--force"], home)
        root_hits = sorted((home / ".workbuddy-ai" / "projects").glob(f"{esid}.*"))
        check("正文没有落到 projects 根目录", not root_hits, str(root_hits))
        # 严格断言：源侧有正文时，cwd 为空也必须靠「源侧目录名」放进正确子目录。
        # 以前写成 if/else 两分支都判通过，等于无论脚本怎么走都会绿（弱断言）。
        if sorted((home / ".workbuddy" / "projects").glob(f"*/{esid}*")):
            check("cwd 为空时仍能放进正确子目录",
                  r.returncode == 0 and len(project_files(home, "intl", esid)) >= 1,
                  f"rc={r.returncode} files={len(project_files(home, 'intl', esid))} "
                  f"stdout={r.stdout[-200:]}")
        else:
            print("  ⏭️  该对话在源侧没有正文文件，跳过「放进子目录」断言")
        _set_cwd(home, "domestic", esid, origin_cwd)   # 还原

    # ---------- 17. 中途失败时 meta 必须已落盘 ----------
    print("\n[17] 迁移中途失败时，备份 meta.json 必须已包含阶段标记")
    fsid, _ftitle = first_session(home, "domestic")
    src_hits = sorted((home / ".workbuddy" / "projects").glob(f"*/{fsid}*")) if fsid else []
    if fsid and src_hits:
        _remove_from_intl(home, fsid)
        slug = src_hits[0].parent.name
        d = home / ".workbuddy-ai" / "projects" / slug
        d.mkdir(parents=True, exist_ok=True)
        # 让【最后一个文件】复制失败（不是第一个）：它之前的文件会先成功复制，
        # 这样才验证得到"已复制的文件是否登记进 meta"。
        # 以前只挡住第一个文件，覆盖不到"部分成功后失败"这条路径。
        ordered = sorted(src_hits, key=lambda p: str(p))
        files_only = [p for p in ordered if not p.is_dir()]
        victim = files_only[-1] if files_only else None
        ro = d / (victim.name if victim else f"{fsid}.jsonl")
        ro.write_text("占位，制造复制失败", encoding="utf-8")
        os.chmod(str(ro), 0o444)   # 只读 → 复制正文必然失败
        prior = [p for p in ordered if p.name != ro.name]
        before_src_rows = db_rows(home, "domestic", fsid)
        pre_dst = {p.name for p in project_files(home, "intl", fsid)}
        r = run(["--from", "domestic", "--to", "intl", "--session-id", fsid,
                 "--mode", "move", "--yes", "--force"], home)
        check("正文复制失败时迁移中止", r.returncode != 0, r.stdout[-300:])
        tag = _last_backup_tag(home, "intl")
        if tag:
            mp = _backup_root(home, "intl") / tag / "meta.json"
            mt = mp.read_text(encoding="utf-8") if mp.exists() else ""
            check("失败后 meta 已落盘阶段标记", '"target_overwritten"' in mt, mt[:200])
            if prior:
                # 关键回归：失败前已复制的文件必须已登记进 copied_to，
                # 否则回滚不会删它们 → 目标侧残留孤儿正文
                compact = mt.replace(" ", "").replace("\n", "")
                check("失败前已复制的文件已登记进 meta",
                      '"copied_to":[]' not in compact, mt[:300])
            rr = run(["--rollback", tag, "--yes", "--force"], home)
            check("失败后可用备份回滚", rr.returncode == 0, rr.stderr[-300:])
            check("回滚后源对话仍在", db_rows(home, "domestic", fsid) == before_src_rows)
            left = {p.name for p in project_files(home, "intl", fsid)}
            check("回滚后目标侧没有孤儿正文", left == pre_dst,
                  f"left={left} expect={pre_dst}")
        os.chmod(str(ro), 0o644)
        _rm(ro)
        _remove_from_intl(home, fsid)

    # ---------- 18. 跨版本目标目录名沿用源侧真实目录名 ----------
    print("\n[18] 跨版本迁移的 projects 子目录沿用源侧真实目录名")
    ssid, _stitle = first_session(home, "domestic")
    s_hits = sorted((home / ".workbuddy" / "projects").glob(f"*/{ssid}*")) if ssid else []
    if ssid and s_hits:
        _remove_from_intl(home, ssid)
        r = run(["--from", "domestic", "--to", "intl", "--session-id", ssid,
                 "--mode", "copy", "--yes", "--force"], home)
        check("跨版本复制成功", r.returncode == 0, r.stderr[-300:])
        d_hits = sorted((home / ".workbuddy-ai" / "projects").glob(f"*/{ssid}*"))
        check("目标目录名与源侧一致",
              bool(d_hits) and d_hits[0].parent.name == s_hits[0].parent.name,
              f"src={s_hits[0].parent.name if s_hits else None} "
              f"dst={d_hits[0].parent.name if d_hits else None}")
        _remove_from_intl(home, ssid)

    # ---------- 19. 任务数据的备份与回滚 ----------
    print("\n[19] 跨版本 move：tasks/ 任务数据必须能备份与回滚")
    # 注意：本用例会动 tasks/，必须放在 [20] --full 整库回滚【之前】，
    # 否则整库恢复会把本用例造的数据冲掉（原来两段顺序是反的，注释与代码相反）
    tsid2, _t2 = first_session(home, "domestic")
    if tsid2:
        _remove_from_intl(home, tsid2)
        src_tasks = home / ".workbuddy" / "tasks" / tsid2
        src_tasks.mkdir(parents=True, exist_ok=True)
        (src_tasks / "1.json").write_text(
            '{"subject":"任务A","status":"pending"}', encoding="utf-8"
        )
        r = run(["--from", "domestic", "--to", "intl", "--session-id", tsid2,
                 "--mode", "move", "--yes", "--force"], home)
        check("move 迁移成功", r.returncode == 0, r.stderr[-300:])
        dst_tasks = home / ".workbuddy-ai" / "tasks" / tsid2
        check("任务数据已复制到目标", dst_tasks.exists())
        check("move 后源任务目录已删除", not src_tasks.exists())
        tag = _last_backup_tag(home, "intl")
        if tag:
            rr = run(["--rollback", tag, "--yes", "--force"], home)
            check("回滚可执行", rr.returncode == 0, rr.stderr[-300:])
            check("回滚后源任务数据恢复",
                  src_tasks.exists() and (src_tasks / "1.json").exists())
            check("回滚后目标任务数据已清理", not dst_tasks.exists())
        _remove_from_intl(home, tsid2)

    # ---------- 20. 整库回滚清理 -wal / -shm ----------
    print("\n[20] --full 整库回滚会清掉残留的 -wal / -shm")
    tag = _last_backup_tag(home, "intl")
    if tag:
        wal = home / ".workbuddy-ai" / "workbuddy.db-wal"
        shm = home / ".workbuddy-ai" / "workbuddy.db-shm"
        wal.write_bytes(b"stale-wal")
        shm.write_bytes(b"stale-shm")
        r = run(["--rollback", tag, "--full", "--yes", "--force"], home)
        check("整库回滚可执行", r.returncode == 0, r.stderr[-300:])
        check("回滚后 -wal 已清理", not wal.exists())
        check("回滚后 -shm 已清理", not shm.exists())

    # ---------- 21. 覆盖后 --full 回滚要还原目标原有正文 ----------
    print("\n[21] 硬冲突覆盖后 --full 回滚：目标被覆盖前的正文必须还原")
    _remove_from_intl(home, sid)
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
             "--mode", "copy", "--yes", "--force"], home)
    check("先做一次 copy 让目标有正文", r.returncode == 0, r.stderr[-300:])
    if len(project_files(home, "intl", sid)) >= 1:
        r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
                 "--mode", "move", "--on-conflict", "overwrite", "--yes", "--force"], home)
        check("覆盖执行成功", r.returncode == 0, r.stderr[-300:])
        tag = _last_backup_tag(home, "intl")
        if tag:
            rr = run(["--rollback", tag, "--full", "--yes", "--force"], home)
            check("--full 回滚可执行", rr.returncode == 0, rr.stderr[-300:])
            check("--full 回滚后目标正文仍在",
                  len(project_files(home, "intl", sid)) >= 1,
                  f"{len(project_files(home, 'intl', sid))} 个文件")
            check("--full 回滚后目标 session 行仍在",
                  db_rows(home, "intl", sid) is not None)
    _remove_from_intl(home, sid)

    # ---------- 22. migrate.py / migrate_session.py 单元测试（不依赖 fixture 数据） ----------
    unit_tests(home)

    # ---------- 23. migrate.py 主流程端到端（fixture） ----------
    migrate_py_e2e(home)

    # ---------- 24. CLI 入口：--backups 列表 + 交互式向导 ----------
    cli_entrypoints_e2e(home)

    # ---------- 25. migrate.py 任务恢复（此前零覆盖的写路径） ----------
    task_restore_e2e(home)

    # ---------- 26. copy 模式的 --full 回滚不动源版本 ----------
    _case_full_copy_keeps_source(home)

    # ---------- 27. --full 必须配 --rollback ----------
    _case_full_requires_rollback(home)

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


def _backup_root(home, edition="intl"):
    """单对话迁移备份所在目录（新版在 migrate_backups/session/）"""
    base = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "migrate_backups"
    sess = base / "session"
    if sess.exists():
        return sess
    return base


def _last_backup_tag(home, edition="intl"):
    bd = _backup_root(home, edition)
    if not bd.exists():
        return None
    tags = sorted([d.name for d in bd.iterdir() if d.is_dir()], reverse=True)
    return tags[0] if tags else None


def _set_uid(home, edition, sid, uid):
    """直接改某版本 fixture 里某个 session 的 user_id（测试用）"""
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    c.execute("UPDATE sessions SET user_id = ? WHERE id = ?", (uid, sid))
    c.commit()
    c.close()


def _remove_from_intl(home, sid):
    """清掉国际版 fixture 中的某个对话（行 + 正文文件 + 任务数据）

    一次运行内多个用例会复用同一份 fixture，任务/正文数据若不清干净，后面的用例
    会把前面用例的残留当成"迁移前就有的数据"，导致断言互相污染。
    （注意：fixture 目录本身**每次 build() 都重建**、跑完还会被清理，
    不存在"跨运行复用"，这里的清理只为同一次运行内的用例隔离。）
    """
    db = home / ".workbuddy-ai" / "workbuddy.db"
    c = _conn(str(db))
    c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    c.execute("DELETE FROM session_usage WHERE session_id = ?", (sid,))
    c.commit()
    c.close()
    for f in project_files(home, "intl", sid):
        _rm(f)
    _rm(home / ".workbuddy-ai" / "tasks" / sid)


def _rm(p):
    """删除文件或目录（tool-results 是目录）"""
    import shutil
    if p.is_dir():
        shutil.rmtree(str(p))
    elif p.exists():
        p.unlink()


def _usage_exists(home, edition, sid):
    """某版本 fixture 里该对话是否还有 session_usage 记录"""
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    try:
        n = c.execute(
            "SELECT COUNT(*) FROM session_usage WHERE session_id = ?", (sid,)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    c.close()
    return n > 0


def _ensure_usage(home, edition, sid):
    """给某条对话补一条 session_usage（用于验证覆盖/回滚是否保住它）

    表里有 used / size / updated_at 等 NOT NULL 列，所以直接照抄一条已有记录
    再改 session_id，比自己拼空值更容易满足约束。
    """
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    try:
        cols = [d[0] for d in c.execute("SELECT * FROM session_usage LIMIT 1").description]
        exist = c.execute("SELECT * FROM session_usage LIMIT 1").fetchone()
        row: dict = dict(zip(cols, exist)) if exist else {}
        row["session_id"] = sid
        for k, v in (("used", 1), ("size", 1), ("updated_at", 1789000000000)):
            if k in cols and not row.get(k):
                row[k] = v
        ks = [k for k in row if k in cols]
        c.execute(
            f"INSERT OR REPLACE INTO session_usage ({','.join(ks)}) "
            f"VALUES ({','.join('?' * len(ks))})",
            [row[k] for k in ks],
        )
        c.commit()
    except sqlite3.Error as e:
        print(f"  ⚠️  构造 usage 失败: {e}")
    finally:
        c.close()


def _get_cwd(home, edition, sid):
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    r = c.execute("SELECT cwd FROM sessions WHERE id = ?", (sid,)).fetchone()
    c.close()
    return r[0] if r else None


def _set_cwd(home, edition, sid, cwd):
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    c.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (cwd, sid))
    c.commit()
    c.close()


def _make_same_title_session(home, sid, title):
    """在目标库造一条同标题、不同 id 的旧对话，返回其 id"""
    import json
    db = home / ".workbuddy-ai" / "workbuddy.db"
    c = _conn(str(db))
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


def _json_ok(p):
    """文件存在且是合法 JSON"""
    import json
    try:
        json.loads(p.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def _account_backup_tags(home, edition="domestic"):
    """整账号备份的标签（migrate.py 放在 migrate_backups/ 根下，不是 session/ 子目录）"""
    base = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "migrate_backups"
    if not base.exists():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir() and d.name != "session")
def unit_tests(home):
    """migrate.py / migrate_session.py 的纯函数级用例（不依赖 fixture 里的真实会话）"""
    import io as _io
    import json as _json
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf
    from contextlib import redirect_stdout as _redirect_stdout

    print("\n[22] 单元级用例（migrate.py / migrate_session.py）")
    tmp = Path(_tf.mkdtemp(prefix="wb-unit-"))
    old_env = os.environ.get("WORKBUDDY_MIGRATE_HOME")
    os.environ["WORKBUDDY_MIGRATE_HOME"] = str(tmp)
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        for m in ("migrate", "migrate_session"):
            sys.modules.pop(m, None)
        # ⚠️ import migrate 之前必须先 flush：migrate.py 顶层在 Windows 会把
        # sys.stdout 换成新的 TextIOWrapper，旧 wrapper 被 GC 时里面**块缓冲**的
        # 输出会一起丢失。stdout 重定向到文件/管道时（CI、`> log`）就会看不到
        # 前面所有 e2e 用例的结果，只剩单元用例，排查失败非常误导。
        sys.stdout.flush()
        sys.stderr.flush()
        import migrate  # type: ignore[import-not-found]  # scripts/ 已加入 sys.path
        import migrate_session as ms  # type: ignore[import-not-found]

        # ⚠️ 进程内一律禁止「向用户提问」。
        # require_clients_closed() 在真检测到客户端时会用 input() 问一次，判据是
        # sys.stdin.isatty() —— 那是**真实终端**的属性，跟 stdout 重定向到哪无关。
        # 直接 `python tests/run_tests.py`（开发者机器上最常见的跑法）会卡在一个
        # 没人看的提问上；而 `... 2>&1`（管道 stdin，立刻 EOF）又是好的。
        # 可运行性不能绑在 stdin 类型上，所以这里统一钉死成 False。
        # 需要覆盖提问行为的用例（2.12a）在自己那个块里临时改成 True。
        _orig_can_prompt = migrate._stdin_can_prompt
        migrate._stdin_can_prompt = lambda: False

        # --- 1) user_id 判定不能只看"含连字符" ---
        check("UUID 形态目录算账号",
              migrate._looks_like_uid("12345678-1234-1234-1234-123456789abc"))
        check("普通带连字符目录不算账号",
              not migrate._looks_like_uid("projects-backup"))
        check("非 UUID 短串不算账号", not migrate._looks_like_uid("abc-def"))

        # --- 2) Memory 重复迁移不得重复追加 ---
        mem = tmp / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        migrate.MEMORY_DIR = mem
        suid, duid = "aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444"

        def _blk(t):
            return f"<!-- RAW_JSON_START {{'memoryBlock':'{t}'}} RAW_JSON_END -->".replace("'", '"')

        (mem / f"{suid}_memory.md").write_text("# 源\n" + _blk("源账号记忆"), encoding="utf-8")
        (mem / f"{duid}_memory.md").write_text("# 目标\n" + _blk("目标自己的记忆"), encoding="utf-8")
        migrate.migrate_memory(suid, duid)
        migrate.migrate_memory(suid, duid)   # 故意再跑一次
        txt = (mem / f"{duid}_memory.md").read_text(encoding="utf-8")
        check("Memory 迁移后含源块", "源账号记忆" in txt)
        check("重复迁移不重复追加", txt.count("源账号记忆") == 1,
              f"出现 {txt.count('源账号记忆')} 次")

        # --- 2.5) 国际版不读平台 storage.json（否则 --intl 会拿到国内版 uid）---
        check("国际版目录不读平台 storage.json",
              migrate._storage_json_for(tmp / ".workbuddy-ai") is None)
        ai = tmp / ".workbuddy-ai"
        (ai / "storage" / "skeleton").mkdir(parents=True, exist_ok=True)
        (ai / "storage" / "skeleton" / "account-snapshot.json").write_text(
            _json.dumps({"primary": {"uid": "INTL-UID-0000"}}), encoding="utf-8"
        )
        migrate.WORKBUDDY_DIR = ai
        migrate.STORAGE_JSON = migrate._storage_json_for(ai)
        migrate.ACCOUNT_SNAPSHOT = ai / "storage" / "skeleton" / "account-snapshot.json"
        migrate.DB_PATH = tmp / "nonexistent.db"
        check("国际版 uid 取 account-snapshot",
              migrate.get_current_user_id() == "INTL-UID-0000")

        # --- 2.6) mcp.json 深度合并必须真的生效 ---
        c2 = tmp / "connectors2"
        (c2 / "src").mkdir(parents=True)
        (c2 / "tgt").mkdir(parents=True)
        (c2 / "src" / "mcp.json").write_text(
            _json.dumps({"mcpServers": {"A": {"command": "a"}, "B": {"command": "b"}}}),
            encoding="utf-8",
        )
        (c2 / "tgt" / "mcp.json").write_text(
            _json.dumps({"mcpServers": {"A": {"command": "a"}}}), encoding="utf-8"
        )
        migrate.CONNECTORS_DIR = c2
        migrate.migrate_connectors("src", "tgt")
        merged = _json.loads((c2 / "tgt" / "mcp.json").read_text(encoding="utf-8"))
        check("mcp.json 深度合并生效（顶层 key 已存在也要合并）",
              "B" in merged.get("mcpServers", {}), str(merged))
        migrate.migrate_connectors("src", "tgt")   # 幂等
        merged2 = _json.loads((c2 / "tgt" / "mcp.json").read_text(encoding="utf-8"))
        check("重复合并结果一致", merged2 == merged)

        # --- 2.7) 目标 connector 目录不存在时能被创建出来 ---
        c3 = tmp / "connectors3"
        (c3 / "src").mkdir(parents=True)
        (c3 / "src" / "mcp.json").write_text(_json.dumps({"mcpServers": {}}), encoding="utf-8")
        migrate.CONNECTORS_DIR = c3
        migrate.migrate_connectors("src", "brand-new")
        check("目标 connector 目录不存在时自动创建", (c3 / "brand-new").exists())

        # --- 2.8) memory 文件名也要做 UUID 校验 ---
        mem2 = tmp / "memory2"
        mem2.mkdir()
        (mem2 / "12345678-1234-1234-1234-123456789abc_memory.md").write_text("x", encoding="utf-8")
        (mem2 / "随便一个笔记_memory.md").write_text("x", encoding="utf-8")
        migrate.MEMORY_DIR = mem2
        migrate.CONNECTORS_DIR = tmp / "no-such-connectors"
        uids = migrate.get_all_user_ids()
        check("memory 里的 UUID 文件名算账号",
              "12345678-1234-1234-1234-123456789abc" in uids, str(uids))
        check("memory 里的非 UUID 文件名不算账号",
              "随便一个笔记" not in uids, str(uids))

        # --- 2.9) 进程检测要排除脚本自身 ---
        # 进程检测只有 migrate.py 一份实现（migrate_session.py 走薄委托），所以这里
        # 直接测 migrate 的那份；两处引用同一函数，行为一致。
        check("自身进程被排除",
              migrate._is_self_process(str(os.getpid()), "python migrate_session.py"))
        check("真实客户端不被误排除",
              not migrate._is_self_process("4321", "/Applications/WorkBuddy.app/WorkBuddy"))
        check("路径里恰好含 migrate.py 的第三方进程不被当成自己",
              not migrate._is_self_process("4321", "/opt/backup/migrate.py.bak/run"))
        check("非 Windows 的完整命令行能取出可读进程名",
              migrate._client_display_name(
                  "/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy --type=gpu",
                  "workbuddy") == "WorkBuddy")

        # --- 2.10) 非 WAL 库 checkpoint 不误报 ---
        nw = tmp / "nw.db"
        cn = _new_conn(nw)
        cn.execute("PRAGMA journal_mode=DELETE")
        cn.execute("CREATE TABLE t (a)")
        cn.commit()
        ok_nw = migrate._wal_checkpoint(cn.cursor(), "测试 ")
        cn.close()
        check("非 WAL 库 checkpoint 不误报占锁", ok_nw)

        # --- 2.10b) cwd_to_slug 要处理 UNC 路径（B）---
        check("普通盘符路径 slug 不变",
              ms.cwd_to_slug(r"C:\Users\alice\WorkBuddy") == "c-Users-alice-WorkBuddy",
              ms.cwd_to_slug(r"C:\Users\alice\WorkBuddy"))
        check("UNC 路径能算出 slug",
              ms.cwd_to_slug(r"\\server\share\proj") == "unc-server-share-proj",
              ms.cwd_to_slug(r"\\server\share\proj"))

        # --- 2.10c) 克隆标题每次都追加「（副本）」（E）---
        check("首次克隆加一次副本标记", ms._clone_title("标题") == "标题（副本）")
        check("克隆副本时再加一次", ms._clone_title("标题（副本）") == "标题（副本）（副本）")

        # --- 2.10d) find_project_files 不误带别的会话（G）---
        pj = tmp / "projects2"
        (pj / "slug-a").mkdir(parents=True)
        (pj / "slug-b").mkdir(parents=True)
        (pj / "slug-a" / "aaaa.jsonl").write_text("x", encoding="utf-8")
        (pj / "slug-b" / "aaaa1111.jsonl").write_text("x", encoding="utf-8")
        (pj / "slug-b" / "aaaa").mkdir()
        fake_ep = ms.EditionPaths(
            name="domestic", root=tmp, db=tmp / "x.db", memory_dir=tmp / "memory",
            connectors_dir=tmp / "conn", tasks_dir=tmp / "tasks", projects_dir=pj,
            sessions_dir=tmp / "sessions", backup_dir=tmp / "backups",
            account_snapshot=tmp / "snapshot.json",
        )
        hits = sorted(f.name for f in ms.find_project_files(fake_ep, "aaaa"))
        check("短 id 不会误带别的会话文件", hits == ["aaaa", "aaaa.jsonl"], str(hits))

        # --- 2.10e) get_connector_info 与 get_all_user_ids 判定一致（A）---
        cdir4 = tmp / "connectors4"
        (cdir4 / "12345678-1234-1234-1234-123456789abc").mkdir(parents=True)
        (cdir4 / ".12345678-1234-1234-1234-123456789abc").mkdir(parents=True)
        migrate.CONNECTORS_DIR = cdir4
        migrate.MEMORY_DIR = tmp / "no-such-memory"
        migrate.DB_PATH = tmp / "no-such.db"
        ci = set(migrate.get_connector_info())
        au = set(migrate.get_all_user_ids())
        check("隐藏目录不算 connector 账号",
              ".12345678-1234-1234-1234-123456789abc" not in ci, str(ci))
        check("两个函数的账号集合一致", ci == au, f"connector={ci} all={au}")

        # --- 2.11) 绑国际版时不得沿用国内版 storage.json（C2/C10）---
        intl_ep = ms.resolve_edition("intl")
        if intl_ep.root.exists():
            with intl_ep.bind_legacy():
                check("绑国际版后不再使用平台 storage.json",
                      ms.legacy.STORAGE_JSON is None, str(ms.legacy.STORAGE_JSON))
        else:
            # 空转必须说出来：fixture 缺国际版目录时这条断言什么也没测到，
            # 却会混进「通过」的计数里，掩盖真实缺口
            print("  ⏭️  fixture 里没有国际版目录，2.11 未执行（不算通过）")

        # --- 2.12) 检测不可信时：--force 放行、不 --force 拒绝 ---
        # 进程检测只有 migrate.py 一份实现，打桩点也在它那边；
        # migrate_session.require_clients_closed 是薄委托，走的是同一份逻辑。
        orig_find = migrate.find_running_clients
        orig_can_prompt = migrate._stdin_can_prompt
        try:
            # ⚠️ 必须一起桩掉「能不能问」：真检测到客户端时 require_clients_closed()
            # 会在 TTY 里 input() 等用户回答。这里打的是 find_running_clients 的桩，
            # 而 sys.stdin.isatty() 看的是**真实终端** —— 在开发者机器上直接
            # `python tests/run_tests.py`（管道 stdout 也没用，判的是 stdin）会让
            # 整套测试卡在这一行等一个没人看的回答。非 TTY 环境跑不出来的回归，
            # 所以显式桩成 False 把它钉死。
            migrate._stdin_can_prompt = lambda: False
            migrate.find_running_clients = lambda: ([], False)   # 模拟检测失败
            check("检测不可信 + --force 放行", ms.require_clients_closed(force=True))
            check("检测不可信且无 --force 时拒绝", not ms.require_clients_closed(force=False))
            check("migrate.py：检测不可信 + --force 放行",
                  migrate.require_clients_closed(force=True))
            check("migrate.py：检测不可信且无 --force 时拒绝",
                  not migrate.require_clients_closed(force=False))
            # --assume-clients-closed 只在【检测失败】这一档兜底，比 --force 温和：
            # 现在 migrate_session.py 也支持它了，两个脚本必须同口径
            check("检测不可信 + --assume-clients-closed 放行",
                  ms.require_clients_closed(force=False, assume_closed=True))
            check("migrate.py：检测不可信 + --assume-clients-closed 放行",
                  migrate.require_clients_closed(force=False, assume_closed=True))

            # 检测可信且确实发现了客户端：两种情况都该拒绝（除非 --force）
            migrate.find_running_clients = lambda: (["WorkBuddy.exe"], True)
            check("检测到客户端时拒绝（两个脚本一致）",
                  not ms.require_clients_closed(force=False)
                  and not migrate.require_clients_closed(force=False))
            # assume_closed 不是第二个 --force：真检测到客户端时必须照拦
            check("检测到客户端时 --assume-clients-closed 不放行",
                  not ms.require_clients_closed(force=False, assume_closed=True)
                  and not migrate.require_clients_closed(force=False, assume_closed=True))
            check("检测到客户端 + --force 才放行",
                  ms.require_clients_closed(force=True)
                  and migrate.require_clients_closed(force=True))

            # 检测可信且干净：应放行
            migrate.find_running_clients = lambda: ([], True)
            check("检测可信且无客户端时放行",
                  ms.require_clients_closed(force=False)
                  and migrate.require_clients_closed(force=False))

            # --- 2.12a) TTY 下会询问而不是直接拒绝（新增行为，必须罩住） ---
            # 上面把 _stdin_can_prompt 桩成 False 测的是「非终端 / 自动化」这条老路径；
            # 这里反过来桩成 True，验证真终端里确实会问、且回答决定放行与否。
            migrate._stdin_can_prompt = lambda: True
            migrate.find_running_clients = lambda: (["WorkBuddy.exe"], True)
            orig_ask = migrate._ask_yes_no
            try:
                migrate._ask_yes_no = lambda p: False
                check("TTY 下回答否 = 取消（等同旧行为的拒绝）",
                      not migrate.require_clients_closed(force=False))

                migrate._ask_yes_no = lambda p: True
                check("TTY 下回答 y = 强制继续（--force 之外的第二条出路）",
                      migrate.require_clients_closed(force=False))

                # --force 不该再打扰用户：明明已经说了"别问我"
                _asked = []
                migrate._ask_yes_no = lambda p: _asked.append(p) or True
                migrate.require_clients_closed(force=True)
                check("--force 时不再询问（已经显式授权过）", not _asked)
            finally:
                migrate._ask_yes_no = orig_ask
        finally:
            migrate.find_running_clients = orig_find
            migrate._stdin_can_prompt = orig_can_prompt

        # --- 2.12b) 回滚路径同样受「客户端必须关闭」守卫约束---
        # 迁移路径早有守卫，--rollback 却直接改库（UPDATE/DELETE/覆盖）+ 删文件。
        # 这里直接驱动 main()，断言检测不可信且未给 --force 时退出 2 且 rollback 未被调用。
        orig_argv = sys.argv
        orig_find = migrate.find_running_clients
        orig_rollback = ms.rollback
        calls = []
        try:
            migrate.find_running_clients = lambda: ([], False)      # 模拟检测不可信
            ms.rollback = lambda *a, **k: calls.append(a)      # 通过守卫后才该被调用

            sys.argv = ["migrate_session.py", "--rollback", "x", "--yes"]
            code = None
            try:
                ms.main()
            except SystemExit as e:
                code = e.code
            check("回滚路径：检测不可信且无 --force 时退出 2", code == 2, f"got={code}")
            check("回滚路径：未过守卫时不会执行 rollback", not calls, str(calls))

            sys.argv = ["migrate_session.py", "--rollback", "x", "--yes", "--force"]
            try:
                ms.main()
            except SystemExit:
                pass
            check("回滚路径：--force 时才走到 rollback", bool(calls), str(calls))
        finally:
            sys.argv = orig_argv
            migrate.find_running_clients = orig_find
            ms.rollback = orig_rollback

        # --- 3) home 被覆盖时不得读真实机器 storage.json ---
        sp_ = migrate._get_storage_json_path()
        check("fixture home 下不会读到真实机器 storage.json",
              sp_ is None or str(tmp) in str(sp_), f"got={sp_}")

        # --- 4) 数据库备份走 sqlite backup API ---
        dbp = tmp / "workbuddy.db"
        c = _new_conn(dbp)
        c.execute("CREATE TABLE t (a TEXT)")
        c.execute("INSERT INTO t VALUES ('x')")
        c.commit()
        c.close()
        bak = tmp / "bak.db"
        ok = migrate._backup_db(dbp, bak)
        c2 = _new_conn(bak)
        n = c2.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        c2.close()
        check("数据库备份用 backup API 且内容完整", ok and n == 1)
        # migrate_session 的整库快照同样可用（源库只读打开）
        check("snapshot_db 可用", ms.snapshot_db(dbp, tmp / "snap.db"))

        # --- 4.5) 回滚要撤销"迁移新建"的目标 memory / connectors（C7）---
        mem3 = tmp / "memory3"
        mem3.mkdir()
        conn3 = tmp / "connectors5"
        migrate.MEMORY_DIR = mem3
        migrate.CONNECTORS_DIR = conn3
        migrate.BACKUP_DIR = tmp / "backups3"
        migrate.DB_PATH = tmp / "nonexistent3.db"
        btag = migrate.create_backup("tgt-uid-0000", "20260921000000")
        # 模拟"迁移前目标账号没有 memory/connectors，迁移后才出现"
        (mem3 / "tgt-uid-0000_memory.md").write_text("迁移新建", encoding="utf-8")
        (conn3 / "tgt-uid-0000").mkdir(parents=True)
        migrate.rollback(btag, skip_confirm=True)
        check("回滚撤销了迁移新建的 Memory",
              not (mem3 / "tgt-uid-0000_memory.md").exists())
        check("回滚撤销了迁移新建的 Connectors",
              not (conn3 / "tgt-uid-0000").exists())

        # --- 4.6) 回滚的两个守卫（meta 兜底 / 边车清理顺序）---
        # 备份目录里既没有 meta.json 也没有 workbuddy.db 时：
        #   ① 不能因 meta 未绑定而 NameError 崩栈
        #   ② 不能先删活库的 -wal / -shm —— 数据一点没恢复却把未 checkpoint 的
        #      WAL 删掉 = 永久丢失
        live = tmp / "live.db"
        lc = _new_conn(live)
        lc.execute("CREATE TABLE t (a)")
        lc.commit()
        lc.close()
        live_wal = Path(str(live) + "-wal")
        live_shm = Path(str(live) + "-shm")
        live_wal.write_bytes(b"pending-wal")
        live_shm.write_bytes(b"pending-shm")
        migrate.DB_PATH = live
        migrate.BACKUP_DIR = tmp / "backups4"
        weird = migrate.BACKUP_DIR / "20260921000000_deadbeef"
        weird.mkdir(parents=True)
        (weird / "deadbeef").mkdir()   # 只有目标账号 connector 目录，没有 meta / db
        crashed = False
        try:
            migrate.rollback("20260921000000_deadbeef", skip_confirm=True)
        except SystemExit:
            crashed = True
        except Exception as e:      # noqa: BLE001 - 单元用例要抓住任何崩栈
            crashed = True
            print(f"    异常: {e!r}")
        check("无 meta.json 的备份回滚不崩栈", not crashed)
        check("无 workbuddy.db 的备份不会删活库 -wal", live_wal.exists())
        check("无 workbuddy.db 的备份不会删活库 -shm", live_shm.exists())

        # --- 4.7) 回滚遇到损坏的 meta.json：给可操作提示，而不是 traceback ---
        # meta.json 是半截损坏时（写到一半断电），migrate_session.rollback() 以前是
        # 裸 json.loads，且发生在"要不要回滚"的确认询问之前 → 用户连提示都看不到。
        badroot = tmp / "badbackups"
        badbp = badroot / "20260922000000_domestic2intl_12345678"
        badbp.mkdir(parents=True)
        (badbp / "meta.json").write_text('{"from_edition": "domest', encoding="utf-8")
        code = None
        try:
            ms.rollback(badbp.name, backup_root=str(badroot), assume_yes=True)
        except SystemExit as e:
            code = e.code
        except Exception as e:      # noqa: BLE001 - 单元用例要抓住任何崩栈
            code = f"EXC {e!r}"
        check("损坏的 meta.json 不会 traceback（退出码 1）", code == 1, f"got={code}")

        WTGT = "aaaaaaaa-1111-2222-3333-444444444444"      # 目标账号
        WEMPTY = "bbbbbbbb-1111-2222-3333-444444444444"    # 只有一条重复 memory → 合并无新增
        WDATA = "cccccccc-1111-2222-3333-444444444444"     # 有 session + 新 memory
        # --- 4.8) 向导：源账号"合并后无新增"(exit 3) 之后可以换源重试 ---
        # 以前 SystemExit 直接穿透向导：挑了个没有新内容的源账号就被踢出进程，
        # 而向导本来正好能让他换一个源再试一次。
        # ⚠️ 必须用 _set_workbuddy_dir() 整体重绑：只改 migrate.WORKBUDDY_DIR
        # 的话 DB_PATH 等仍然指向真实数据目录（2026-09-23 事故就是这么发生的）。
        wroot = tmp / "wizard" / ".workbuddy"
        wroot.mkdir(parents=True)
        orig_dir = migrate.WORKBUDDY_DIR
        wc = _new_conn(wroot / "workbuddy.db")
        wc.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, "
            "status TEXT, created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER, "
            "cwd TEXT, model TEXT, mode TEXT, custom_title TEXT)")
        wc.execute("INSERT INTO sessions VALUES ('t1',?,?,'',1,1,1,'','','','')",
                   (WTGT, "target"))
        wc.execute("INSERT INTO sessions VALUES ('d1',?,?,'',1,1,1,'','','','')",
                   (WDATA, "with data"))
        wc.commit()
        wc.close()
        wmem = wroot / "memory"
        wmem.mkdir()
        dup = '<!-- RAW_JSON_START {"memoryBlock":"dup"} RAW_JSON_END -->'
        for uid in (WTGT, WEMPTY):
            (wmem / f"{uid}_memory.md").write_text(dup, encoding="utf-8")
        migrate._set_workbuddy_dir(wroot)
        try:
            assert str(migrate.DB_PATH).startswith(str(wroot)), migrate.DB_PATH
            all_uids = sorted(migrate.get_all_user_ids())
            check("向导用例：三个账号都被扫到", len(all_uids) == 3, str(all_uids))
            # ⚠️ input 平时是 builtins 的名字、不是 migrate 的模块属性：
            # 打完桩若把它设回 None，后续用例里 migrate 内的 input(...) 会变成
            # 「NoneType is not callable」，Ctrl-C 那条用例就跟着假失败。
            _had_input = hasattr(migrate, "input")
            _orig_input = getattr(migrate, "input", None)
            seq = iter([str(all_uids.index(WTGT) + 1),
                        str(all_uids.index(WEMPTY) + 1),
                        str(all_uids.index(WDATA) + 1)])
            migrate.input = lambda p="": next(seq)
            # ⚠️ 这两个桩用完必须还原：不还原的话后面「Ctrl-C 返回否」那条用例
            # 拿到的是这里的 lambda（恒 True），会假失败。
            _orig_yes_no = migrate._ask_yes_no
            migrate._ask_yes_no = lambda p: True
            escaped = None
            try:
                migrate.interactive_migrate(skip_edition_prompt=True, skip_confirm=True)
            except SystemExit as e:
                escaped = e.code
            finally:
                migrate._ask_yes_no = _orig_yes_no
                if _had_input:
                    migrate.input = _orig_input
                else:
                    del migrate.input
            check("无新增内容后不再直接退出进程", escaped is None, f"SystemExit={escaped}")
            con = None
            try:
                con = _new_conn(wroot / "workbuddy.db")
                moved = con.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (WTGT,)).fetchone()[0]
            finally:
                if con is not None:
                    con.close()
            check("换源后第二轮真的迁移了", moved == 2, f"target sessions={moved}")
        finally:
            migrate._set_workbuddy_dir(orig_dir)

        # --- 4.9) 两份登录态不一致时以客户端登录态为准（v1.6.3 口径）---
        # 选错目标账号 = 整个账号的 session 被改到面板看不到的 uid 下（对话"消失"）。
        # v1.6.3 起口径固定为 account-snapshot → 平台 storage.json → DB 反推，
        # 不再把两份登录态的冲突交给用户仲裁；但每一次回落都必须留下可见痕迹，
        # 且"只剩 DB 反推"时必须能被调用方识别出来（整账号迁移会拦一次）。
        arb = tmp / "arb" / ".workbuddy"
        (arb / "storage" / "skeleton").mkdir(parents=True)
        A, B = "aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444"
        snap = arb / "storage" / "skeleton" / "account-snapshot.json"
        snap.write_text(_json.dumps({"primary": {"uid": A}}), encoding="utf-8")
        plat = arb / "platform-storage.json"
        plat.write_text(_json.dumps({"genie.userId": B}), encoding="utf-8")
        arb_dir, arb_storage = migrate.WORKBUDDY_DIR, migrate.STORAGE_JSON
        migrate._set_workbuddy_dir(arb)
        try:
            # 本机没有平台 storage.json，直接把候选路径指到临时文件上
            migrate.STORAGE_JSON = plat
            picked = migrate.get_current_user_id(verbose=False)
            check("两份登录态不一致时以客户端登录态为准", picked == A, f"got={picked}")
            # 客户端登录态读不到时才回落到扩展侧记录，且回落路径必须真的能走到
            snap.write_text(_json.dumps({}), encoding="utf-8")
            picked2 = migrate.get_current_user_id(verbose=False)
            check("读不到 account-snapshot 时回落到 storage.json", picked2 == B,
                  f"got={picked2}")
            # 两份都读不到（且临时目录里没有 DB）时不能假装有结果
            migrate.STORAGE_JSON = None
            picked3 = migrate.get_current_user_id(verbose=False)
            check("两份登录态都没有时不假装有结果", picked3 == "", f"got={picked3}")
        finally:
            migrate.STORAGE_JSON = arb_storage
            migrate._set_workbuddy_dir(arb_dir)

        # --- 5) 含中文的 mcp.json 必须按 utf-8 解析 ---
        cdir = tmp / "connectors" / "12345678-1234-1234-1234-123456789abc"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "mcp.json").write_text(
            _json.dumps({"mcpServers": {"中文服务": {"command": "x"}}}, ensure_ascii=False),
            encoding="utf-8",
        )
        migrate.CONNECTORS_DIR = tmp / "connectors"
        info = migrate.get_connector_info()
        check("中文 mcp.json 能解析出 server",
              any(v.get("mcp_servers") == 1 for v in info.values()), str(info))

        # --- 6) 正文 id 改写只动 sessionId 字段，不动正文文本 ---
        old_sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        new_sid = "11111111-2222-3333-4444-555555555555"
        jf = tmp / "s.jsonl"
        jf.write_text(
            '{"sessionId":"' + old_sid + '","text":"引用旧 id ' + old_sid + '"}\n',
            encoding="utf-8",
        )
        ms._rewrite_session_id(jf, old_sid, new_sid)
        t = jf.read_text(encoding="utf-8")
        check("sessionId 字段已改写", f'"sessionId":"{new_sid}"' in t, t)
        check("正文里同串 id 不被误改", t.count(old_sid) == 1, t)

        # --- 6.5) .meta.json / .file-rollback.ndjson / 目录内文件也要改写---
        # 这两种附属文件此前没有专门用例，只靠后缀 .json / .ndjson "侥幸"命中
        tree = tmp / "tree"
        (tree / "inner").mkdir(parents=True)
        (tree / "s.meta.json").write_text('{"sessionId":"' + old_sid + '"}', encoding="utf-8")
        (tree / "s.file-rollback.ndjson").write_text(
            '{"sessionId":"' + old_sid + '"}\n', encoding="utf-8"
        )
        (tree / "inner" / "t.txt").write_text(
            '{"sessionId":"' + old_sid + '"}', encoding="utf-8"
        )
        ms._rewrite_tree_ids(tree, old_sid, new_sid)
        check("_rewrite_tree_ids 改写 .meta.json",
              f'"sessionId":"{new_sid}"' in (tree / "s.meta.json").read_text(encoding="utf-8"))
        check("_rewrite_tree_ids 改写 .file-rollback.ndjson",
              f'"sessionId":"{new_sid}"' in (tree / "s.file-rollback.ndjson").read_text(encoding="utf-8"))
        check("_rewrite_tree_ids 递归改写目录内文件",
              f'"sessionId":"{new_sid}"' in (tree / "inner" / "t.txt").read_text(encoding="utf-8"))

        chk = tmp / "chk.jsonl"
        chk.write_text('{"sessionId":"' + new_sid + '","text":"' + old_sid + '"}\n',
                       encoding="utf-8")
        check("_contains_old_session_id 不把正文里的旧 id 当残留",
              not ms._contains_old_session_id(chk, old_sid))
        chk.write_text('{"sessionId":"' + old_sid + '"}\n', encoding="utf-8")
        check("_contains_old_session_id 能识别残留字段",
              ms._contains_old_session_id(chk, old_sid))

        # 带空格的写法 `"sessionId": "<sid>"`：改写与残留校验必须用同一套正则。
        # 以前改写用 \s* 容忍空格、校验用字面量针 → 改写得到但校验查不出（漏报）。
        sp = tmp / "spaced.jsonl"
        sp.write_text('{"sessionId": "' + old_sid + '"}\n', encoding="utf-8")
        check("带空格写法能被识别为残留", ms._contains_old_session_id(sp, old_sid))
        ms._rewrite_session_id(sp, old_sid, new_sid)
        sp_txt = sp.read_text(encoding="utf-8")
        check("带空格写法同样会被改写",
              new_sid in sp_txt and old_sid not in sp_txt, sp_txt)
        check("改写后不再报残留", not ms._contains_old_session_id(sp, old_sid))

        # --- 7) --rollback 与 --source 互斥；--rollback 支持 --yes ---
        # 注：migrate.py 现在也有「客户端必须关闭」检测，
        # 单元用例一律带 --force，否则在客户端运行时会先被拦下，测不到真正的断言点。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        r = _sp.run(
            [sys.executable, str(ROOT / "scripts" / "migrate.py"),
             "--rollback", "x", "--source", "y", "--force"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        out = r.stdout + r.stderr
        check("--rollback 与 --source 同给时报错", r.returncode != 0 and "不能与" in out, out[-200:])

        r = _sp.run(
            [sys.executable, str(ROOT / "scripts" / "migrate.py"),
             "--rollback", "no-such-tag", "--yes", "--force"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        out = r.stdout + r.stderr
        check("--rollback --yes 不再等待人工确认", "备份不存在" in out, out[-200:])

        # 标签本身是路径时必须拒绝（否则会被拼进备份目录做 rmtree/copytree）
        r = _sp.run(
            [sys.executable, str(ROOT / "scripts" / "migrate.py"),
             "--rollback", "../../etc", "--yes", "--force"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        out = r.stdout + r.stderr
        check("--rollback 拒绝含路径分隔符的标签",
              r.returncode != 0 and "不合法" in out, out[-200:])

        # migrate_session.py 的 --rollback 同样要拒绝路径形态的标签：
        # 它以前不校验，load_backup() 会把标签直接拼进候选备份根目录
        r = _sp.run(
            [sys.executable, str(ROOT / "scripts" / "migrate_session.py"),
             "--rollback", "../../etc", "--yes", "--force"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        out = r.stdout + r.stderr
        check("migrate_session --rollback 也拒绝路径形态标签",
              r.returncode != 0 and "不合法" in out, out[-200:])
        # 「不合法」和「找不到」必须是两个不同的事实：混成一句会先打
        # "❌ 标签不合法"紧接着又打"❌ 找不到备份: ../../etc"
        _bp_inv, _why_inv = ms.find_backup("../../etc")
        _bp_nf, _why_nf = ms.find_backup("20990101000000_no_such_backup")
        check("非法标签的理由是 invalid", _bp_inv is None and _why_inv == "invalid",
              f"got={_why_inv}")
        check("合法但不存在的理由是 notfound", _bp_nf is None and _why_nf == "notfound",
              f"got={_why_nf}")
        _buf = _io.StringIO()
        try:
            with _redirect_stdout(_buf):
                ms.rollback("../../etc", assume_yes=True)
        except SystemExit:
            pass
        out_inv = _buf.getvalue()
        check("非法标签只报一次『不合法』，不再接着报『找不到』",
              "不合法" in out_inv and "找不到备份" not in out_inv, out_inv[-300:])

        # --- 8) 依赖参数缺失时明确报错（以前会被静默忽略）---
        r = _sp.run(
            [sys.executable, str(ROOT / "scripts" / "migrate.py"), "--target", "some-uid", "--force"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        out = r.stdout + r.stderr
        check("--target 单独给会报错", r.returncode != 0 and "必须与 --source" in out, out[-200:])

        r = _sp.run(
            [sys.executable, str(ROOT / "scripts" / "migrate.py"), "--generate-commands", "--force"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        out = r.stdout + r.stderr
        check("--generate-commands 单独给会报错",
              r.returncode != 0 and "必须与 --restore-tasks" in out, out[-200:])

        # --- 9) 目标账号裁决与静默失败修复的回归点 ---
        # 9.1) 路径在别处的同名脚本不能被当成自己（误报会静默关掉客户端检测）
        check("第三方目录下的 migrate.py 不被当成自己",
              not migrate._is_self_process("4321", "python /opt/other/project/migrate.py"))
        check("裸脚本名仍被当成自己（已知局限，误拦可用 --force）",
              migrate._is_self_process("4321", "python migrate_session.py"))

        # 9.2) 目标侧的空壳不算「已有」，要从源补齐；同名不同值要报冲突而不是静默
        c6 = tmp / "connectors6"
        (c6 / "src").mkdir(parents=True)
        (c6 / "tgt").mkdir(parents=True)
        (c6 / "src" / "mcp.json").write_text(
            _json.dumps({"mcpServers": {"A": {"command": "src-cmd", "args": ["--x"]}}}),
            encoding="utf-8",
        )
        (c6 / "tgt" / "mcp.json").write_text(
            _json.dumps({"mcpServers": {"A": {"command": "", "args": []}}}),
            encoding="utf-8",
        )
        migrate.CONNECTORS_DIR = c6
        migrate.migrate_connectors("src", "tgt")
        merged3 = _json.loads((c6 / "tgt" / "mcp.json").read_text(encoding="utf-8"))
        check("目标侧空壳（空串 / 空列表）从源补齐",
              merged3.get("mcpServers", {}).get("A")
              == {"command": "src-cmd", "args": ["--x"]}, str(merged3))
        st = migrate.deep_merge_dict(
            {"mcpServers": {"A": {"command": "src-cmd", "args": ["--x"]}}},
            {"mcpServers": {"A": {"command": "tgt-cmd", "args": ["--y"]}}},
        )
        check("同名不同值报为冲突而不是静默",
              st["conflicts"] == ["mcpServers.A.command", "mcpServers.A.args"], str(st))

        # 9.3) connect_rw 不得凭空创建空库
        before_names = set(p.name for p in tmp.iterdir())
        _rw_raised = False
        try:
            ms.connect_rw(tmp / "no-such-db.db")
        except RuntimeError:
            _rw_raised = True
        check("connect_rw 对不存在的库报错", _rw_raised)
        check("connect_rw 没有凭空建出空库",
              set(p.name for p in tmp.iterdir()) == before_names)

        # 9.4) 确认询问遇到 Ctrl-C 返回「否」而不是抛 traceback
        import builtins as _builtins
        _orig_input = _builtins.input

        def _kb(*a, **k):
            raise KeyboardInterrupt

        try:
            _builtins.input = _kb
            check("确认询问遇到 Ctrl-C 返回否而不是抛异常",
                  migrate._ask_yes_no("test?") is False)
        finally:
            _builtins.input = _orig_input

        # 9.5) memory 文件名的账号判定与 get_all_user_ids 一致
        mem4 = tmp / "memory4"
        mem4.mkdir()
        (mem4 / "随便一个笔记_memory.md").write_text("x", encoding="utf-8")
        migrate.MEMORY_DIR = mem4
        migrate.DB_PATH = tmp / "no-such4.db"
        check("get_memory_sizes 不把非 UUID 文件名当账号",
              "随便一个笔记" not in migrate.get_memory_sizes(),
              str(migrate.get_memory_sizes()))

        # 9.6) _to_int：脏时间戳的容错（此前只被 e2e 间接扫到，fixture 里
        # 根本没有脏数据 —— 等于没测。这里直接打表 + 走一遍真实调用点）
        _ti = ms._to_int
        check("_to_int 空值回退默认值",
              _ti(None) == 0 and _ti("") == 0 and _ti(None, -1) == -1)
        check("_to_int 正常数字与负数",
              _ti(0) == 0 and _ti("123") == 123 and _ti(-7) == -7)
        # 真正的回归场景：某次导出把时间戳写成了 ISO 字符串，int() 会 ValueError
        check("_to_int ISO 串不炸（这就是它存在的原因）",
              _ti("2026-09-21T10:30:00.000Z") == 0
              and _ti("2026-09-21T10:30:00.000Z", 1) == 1)
        check("_to_int 其它脏输入不炸",
              _ti("abc") == 0 and _ti([]) == 0 and _ti({}) == 0
              and _ti("12.5") == 0)
        # REAL 列会给出真正的 float（不是 "12.5" 这种字符串），能 truncate 就别丢
        check("_to_int 真 float 按 int() 截断", _ti(12.7) == 12 and _ti(-0.5) == 0)

        # 走一遍真实调用点：collect_info() 在 --list 的循环里被调用，
        # 一条脏数据以前会让整个命令崩。fixture 里造不出这种数据，只能手搓。
        _ti_sid = "aaaaaaaa-0000-4000-8000-000000000001"
        # 用 resolve_edition 而不是手搓 EditionPaths：这个 dataclass 字段多
        # （connectors_dir / sessions_dir / account_snapshot…），缺一个就炸，
        # 而这里只想验证时间戳容错
        _ti_ep = ms.resolve_edition("domestic")
        _ti_row = {
            "id": _ti_sid,
            "title": "脏时间戳会话",
            "created_at": "2026-09-21T10:30:00.000Z",   # ISO 串，不是数字
            "updated_at": None,                          # 空
            "last_activity_at": "not-a-number",          # 完全不是数字
        }
        _ti_info = None
        try:
            _ti_info = ms.collect_info(_ti_ep, _ti_row, deep=False)
        except Exception as _e:
            check("collect_info 遇脏时间戳不抛异常", False,
                  f"{type(_e).__name__}: {_e}")
        if _ti_info is not None:
            check("collect_info 遇脏时间戳不抛异常", True)
            check("collect_info 脏时间戳被容错成 0",
                  _ti_info.created_at == 0 and _ti_info.updated_at == 0
                  and _ti_info.last_activity_at == 0,
                  f"{_ti_info.created_at}/{_ti_info.updated_at}/"
                  f"{_ti_info.last_activity_at}")

        # 9.6) 原子写失败要向上抛，且不留 .tmp
        d_target = tmp / "atomic-target"
        d_target.mkdir()
        _aw_raised = False
        try:
            migrate._atomic_write_text(d_target, "{}")
        except OSError:
            _aw_raised = True
        check("原子写目标不可写时报错", _aw_raised)
        check("原子写失败后没有残留 .tmp", not (tmp / "atomic-target.tmp").exists())
    finally:
        if old_env is None:
            os.environ.pop("WORKBUDDY_MIGRATE_HOME", None)
        else:
            os.environ["WORKBUDDY_MIGRATE_HOME"] = old_env
        try:
            migrate._stdin_can_prompt = _orig_can_prompt
        except Exception:
            pass    # import 就失败时这个桩也不存在
        _sh.rmtree(str(tmp), ignore_errors=True)




def migrate_py_e2e(home):
    """migrate.py 整账号迁移的端到端用例

    此前 migrate.py 只有纯函数级单测（create_backup / rollback / _backup_db / …），
    **主流程 migrate() 一次都没真正跑过** —— 这个缺口
    而「回滚 meta 兜底、边车清理顺序」两项正好都落在未覆盖区。
    这里在 fixture 上真跑一遍：造源账号 → 改 user_id → 校验备份 → 整库回滚。
    """
    print("\n[23] migrate.py 整账号迁移端到端（改 user_id + 备份 + 整库回滚）")
    mig = ROOT / "scripts" / "migrate.py"
    # ⚠️ 必须用 --dir 钉住版本：migrate.py 的自动探测在"两个版本目录都存在"时
    # 优先选国际版（~/.workbuddy-ai），而本用例造的数据在国内版里 ——
    # 不钉住的话它会在 intl 库上跑，判定"源账号无任何数据"直接返回（不报错、也不备份）。
    dom_dir = home / ".workbuddy"

    def run_mig(args, stdin=None):
        env = dict(os.environ)
        env["WORKBUDDY_MIGRATE_HOME"] = str(home)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [PY, str(mig), "--dir", str(dom_dir)] + args, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", input=stdin, timeout=180,
        )

    # --- 只读命令 ---
    r = run_mig(["--diagnose"])
    check("migrate.py --diagnose 可执行", r.returncode == 0, r.stderr[-300:])
    check("诊断输出含账号分布", "账号数据诊断" in r.stdout and "当前登录" in r.stdout,
          r.stdout[-300:])
    r = run_mig(["--list-tasks"])
    check("migrate.py --list-tasks 可执行", r.returncode == 0, r.stderr[-300:])

    sid, _title = pick_test_session(home)
    dst_uid = _current_uid(home, "domestic")
    src_uid = "11111111-1111-1111-1111-111111111111"
    if not (sid and dst_uid):
        print("  ⏭️  缺少可用的 session 或当前 uid，跳过整账号迁移用例")
        return

    # --- 造一个"旧账号"名下的对话，然后整账号迁到当前账号 ---
    _set_uid(home, "domestic", sid, src_uid)
    before_tags = set(_account_backup_tags(home))
    r = run_mig(["--source", src_uid, "--target", dst_uid, "--yes", "--force"])
    check("整账号迁移可执行", r.returncode == 0, r.stderr[-400:])
    row = db_rows(home, "domestic", sid)
    check("该对话的 user_id 已迁到目标账号",
          row is not None and row[1] == dst_uid,
          f"got={(row[1] if row else None) or ''}… want={dst_uid[:8]}…  out={r.stdout[-200:]}")

    new_tags = [t for t in _account_backup_tags(home) if t not in before_tags]
    check("已创建整账号备份", len(new_tags) == 1, str(new_tags))
    tag = new_tags[0] if new_tags else None
    if tag:
        mp = home / ".workbuddy" / "migrate_backups" / tag / "meta.json"
        check("备份 meta.json 已写出且可解析（原子写）", mp.exists() and _json_ok(mp), str(mp))
        if _json_ok(mp):
            import json
            meta = json.loads(mp.read_text(encoding="utf-8"))
            check("meta 记录了 target_uid", meta.get("target_uid") == dst_uid,
                  str(meta)[:200])
        rr = run_mig(["--rollback", tag, "--yes", "--force"])
        check("整账号回滚可执行", rr.returncode == 0, rr.stderr[-400:])
        row2 = db_rows(home, "domestic", sid)
        check("回滚后 user_id 复原", row2 is not None and row2[1] == src_uid,
              f"got={(row2[1] if row2 else None) or ''} want={src_uid}")

    # --- 源账号在所选目录下确实没数据时，必须说清"用的是哪个目录" ---
    # 两个版本都装时自动探测会选国际版，用户在国内版里有数据却只看到
    # "无任何数据"（退出码还是 0），很容易误判成"真没东西可迁"。
    r = run_mig(["--source", "22222222-2222-2222-2222-222222222222",
                 "--target", dst_uid, "--yes", "--force"])
    # 退出码 3 = 无数据可迁（见 README「退出码」）：不能与「迁移成功」的 0 混用
    check("源账号无数据时给出目录与换版本提示",
          r.returncode == 3 and "本次使用的数据目录" in r.stdout and "--dir" in r.stdout,
          f"rc={r.returncode} {r.stdout[-300:]}")

    _set_uid(home, "domestic", sid, dst_uid)   # 还原现场


def cli_entrypoints_e2e(home):
    """两个此前零覆盖的用户入口：`--backups` 列表、交互式向导

    这两个都是用户直接打交道的入口，却从未被任何用例跑过。交互式向导尤其值得补：
    近期改过它的参数透传（`--mode` / `--dry-run` / `--yes` / `--target-uid` /
    `--backup-dir` 以前在"未给 --session-id"时被静默丢弃），改完没有任何回归网。
    """
    print("\n[24] CLI 入口：--backups 列表 + 交互式向导（--dry-run 不写盘）")
    sid, _title = pick_test_session(home)
    if not sid:
        print("  ⏭️  fixture 里没有可用对话，跳过")
        return

    # --- 1) --backups：先造一个备份，再列出来 ---
    _remove_from_intl(home, sid)
    r = run(["--from", "domestic", "--to", "intl", "--session-id", sid,
             "--mode", "copy", "--yes", "--force"], home)
    check("造备份：copy 迁移成功", r.returncode == 0, r.stderr[-300:])
    r = run(["--backups"], home)
    check("--backups 可执行", r.returncode == 0, r.stderr[-300:])
    check("--backups 列出备份与回滚命令",
          "单对话迁移备份" in r.stdout and "--rollback" in r.stdout, r.stdout[-400:])
    tag = _last_backup_tag(home, "intl")
    if tag:
        check("--backups 列出了刚建的备份", tag[:20] in r.stdout, r.stdout[-400:])

    # --- 2) 交互式向导 + --dry-run：走完全流程但不写盘 ---
    before_src = db_rows(home, "domestic")
    before_dst = db_rows(home, "intl")
    # 输入顺序：源版本(1=国内版) → 目标版本(2=国际版) → 选对话(1) → 模式(1=move)
    r = run(["--dry-run", "--force"], home, stdin="1\n2\n1\n1\n")
    check("向导 --dry-run 可执行", r.returncode == 0, r.stderr[-400:])
    check("向导走到第 4 步并打印计划",
          "第 4 步" in r.stdout and "dry-run" in r.stdout, r.stdout[-400:])
    check("向导 --dry-run 未改动任何数据",
          db_rows(home, "domestic") == before_src and db_rows(home, "intl") == before_dst,
          f"src {before_src}→{db_rows(home, 'domestic')} "
          f"dst {before_dst}→{db_rows(home, 'intl')}")

    # --- 3) 向导透传 --mode：不应再问模式 ---
    r = run(["--dry-run", "--mode", "copy", "--force"], home, stdin="1\n2\n1\n")
    check("向导透传 --mode（不再重复询问）",
          r.returncode == 0 and "来自命令行 --mode" in r.stdout, r.stdout[-400:])

    _remove_from_intl(home, sid)


def _set_status(home, edition, sid, status, created_at=None):
    """直接改某版本 fixture 里某个 session 的 status（测试用）

    restore_tasks 会用 `WHERE status='working' ORDER BY created_at DESC LIMIT 1`
    猜"当前 session"，测试里把它置成 working 并给一个极大的 created_at 才能稳定命中。
    """
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    if created_at is None:
        c.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, sid))
    else:
        c.execute("UPDATE sessions SET status = ?, created_at = ? WHERE id = ?",
                  (status, created_at, sid))
    c.commit()
    c.close()


def _other_session_id(home, edition, exclude):
    """取一条不等于 exclude 的 session id（测试用）"""
    db = home / (".workbuddy-ai" if edition == "intl" else ".workbuddy") / "workbuddy.db"
    c = _conn(str(db))
    r = c.execute(
        "SELECT id FROM sessions WHERE id != ? ORDER BY last_activity_at DESC LIMIT 1",
        (exclude,),
    ).fetchone()
    c.close()
    return r[0] if r else None


def task_restore_e2e(home):
    """`--restore-tasks` / `--generate-commands`：此前零覆盖的任务恢复写路径

    `restore_tasks()` 会把历史任务重新写成 `tasks/<当前 session>/<n>.json`，
    是仓库里最后一个没被任何用例跑过的写路径。
    """
    print("\n[25] migrate.py 任务恢复：--restore-tasks / --generate-commands")
    dom = home / ".workbuddy"
    mig = ROOT / "scripts" / "migrate.py"

    def run_mig(args):
        env = dict(os.environ)
        env["WORKBUDDY_MIGRATE_HOME"] = str(home)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [PY, str(mig), "--dir", str(dom)] + args, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=180,
        )

    sid, _title = pick_test_session(home)
    # ⚠️ "当前 session"必须与源 session 不同：否则 restore_tasks 读写的都是同一个
    # tasks/<sid>/ 目录，断言会读到源文件而不是恢复出的文件（第一版就栽在这）
    cur_sid = _other_session_id(home, "domestic", sid)
    if not (sid and cur_sid):
        print("  ⏭️  没有可用对话，跳过")
        return

    # 造数据：源 session 下放一个 pending 任务
    src_tasks = dom / "tasks" / sid
    src_tasks.mkdir(parents=True, exist_ok=True)
    (src_tasks / "1.json").write_text(
        json.dumps({"subject": "端到端任务A", "description": "d", "status": "pending",
                    "id": "1"}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 造"当前 session"：置 working + 极大 created_at，保证被稳定选中
    _set_status(home, "domestic", cur_sid, "working", created_at=9999999999999)

    r = run_mig(["--restore-tasks", "--session", sid, "--yes"])
    check("--restore-tasks 可执行", r.returncode == 0, r.stderr[-400:])
    check("任务恢复流程跑完", "任务恢复完成" in r.stdout, r.stdout[-400:])
    m = re.search(r"当前 session: (\S+)", r.stdout)
    got = m.group(1) if m else None
    check("解析出恢复目标 session", got == cur_sid, f"got={got} want={cur_sid}")
    if got:
        dst_dir = dom / "tasks" / got
        files = sorted(dst_dir.glob("*.json")) if dst_dir.exists() else []
        check("任务文件已写入该 session 的 tasks 目录", len(files) >= 1, str(dst_dir))
        # 目标目录里可能本来就有文件（next_id 会往后排），所以要看【有没有一份】
        # 带 restored_from_session 元数据的恢复产物，而不是盯第一个文件
        texts = [f.read_text(encoding="utf-8") for f in files]
        check("恢复出的任务含 subject 与 restored_from 元数据",
              any("端到端任务A" in t and "restored_from_session" in t for t in texts),
              f"{files} {texts[:1]}")

    # --generate-commands：只读打印，不写盘
    before = len(list((dom / "tasks").rglob("*.json")))
    r = run_mig(["--restore-tasks", "--generate-commands", "--session", sid])
    check("--generate-commands 可执行", r.returncode == 0, r.stderr[-400:])
    check("--generate-commands 打印 TaskCreate 参数",
          "端到端任务A" in r.stdout and "activeForm" in r.stdout, r.stdout[-400:])
    after = len(list((dom / "tasks").rglob("*.json")))
    check("--generate-commands 不写盘", before == after, f"{before} → {after}")

    _rm(src_tasks)


def _case_full_copy_keeps_source(home):
    """[26] copy 模式的 --full 回滚不还原源侧（源是用户特意保留的）"""
    print("\n[26] copy 模式的 --full 回滚不还原源侧（源是用户特意保留的）")
    c_sid, _c_title = first_session(home, "domestic")
    if c_sid:
        _remove_from_intl(home, c_sid)
        r = run(["--from", "domestic", "--to", "intl", "--session-id", c_sid,
                 "--mode", "copy", "--yes", "--force"], home)
        check("copy 迁移成功", r.returncode == 0, r.stderr[-300:])
        tag = _last_backup_tag(home, "intl")
        if tag:
            src_db = home / ".workbuddy" / "workbuddy.db"
            # 迁移之后再取指纹：这里要比的是"回滚有没有动源"，不是"迁移有没有动源"
            before = hashlib.md5(src_db.read_bytes()).hexdigest()
            rr = run(["--rollback", tag, "--full", "--yes", "--force"], home)
            check("--full 回滚可执行", rr.returncode == 0, rr.stderr[-300:])
            check("copy 模式输出跳过源库的提示", "跳过" in rr.stdout, rr.stdout[-300:])
            after = hashlib.md5(src_db.read_bytes()).hexdigest()
            # copy 模式迁移没碰过源侧数据，还原整库快照会把源版本迁移之后新增的
            # 对话 / 消息一起抹掉（精确回滚一向是用 source_deleted 守卫的）
            check("copy 模式 --full 回滚后源库逐字节未变", before == after,
                  f"{before[:8]} → {after[:8]}")
            # 反证：目标侧必须真的滚回去了 —— 否则"源库没变"可能只是压根没执行
            check("--full 回滚后目标侧的对话已撤掉",
                  db_rows(home, "intl", c_sid) is None)
        _remove_from_intl(home, c_sid)



def _case_full_requires_rollback(home):
    """[27] --full 不带 --rollback 要显式报错"""
    print("\n[27] --full 不带 --rollback 要显式报错，不能被静默忽略")
    r = run(["--full", "--yes", "--force"], home)
    check("--full 缺少 --rollback 时退出非 0", r.returncode != 0, r.stdout[-300:])
    check("--full 缺少 --rollback 时给出用法说明",
          "--full" in r.stdout and "--rollback" in r.stdout, r.stdout[-200:])




if __name__ == "__main__":
    sys.exit(main())
