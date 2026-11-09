#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双版本同装探测测试（纯合成 fixture，跨平台，不碰真实数据）

背景
----
WorkBuddy 国内版数据目录是 ~/.workbuddy，国际版是 ~/.workbuddy-ai，
两者可以同时装在一台机器上。旧版 _find_workbuddy_dir() 只要 ~/.workbuddy-ai
非空就无条件返回国际版目录，且不打任何提示，导致：

  · `--diagnose` 诊断的是几乎空的国际版库 → 报「只发现一个账号，无需迁移」
  · 不带 --intl / --dir 的 `--source` 迁移也会打在国际版库上

实测一台双装机器上，国内版 266 条会话被完全跳过，诊断却说无事可做。

本套测试用 WORKBUDDY_MIGRATE_HOME 把 home 指向临时目录，
把四种目录组合都跑一遍，断言探测结果与提示语。

与 run_tests.py 的区别
----------------------
run_tests.py 的 fixture 从本机真实数据复制（含 Windows 路径），只能在 Windows 跑；
这里全部现场合成，macOS / Linux / Windows 都能跑。

用法:
  python3 tests/run_dual_edition_tests.py
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SCRIPT = ROOT / "scripts" / "migrate.py"
SCRIPTS = ROOT / "scripts"

PY = sys.executable
PASS, FAIL = [], []

DOMESTIC_UID = "11111111-1111-1111-1111-111111111111"
INTL_UID = "22222222-2222-2222-2222-222222222222"
OTHER_UID = "33333333-3333-3333-3333-333333333333"


# ---------------------------------------------------------------- helpers

def _check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(f"{name} :: {detail}" if detail else name)
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


def _env(home):
    """构造干净的子进程环境：HOME 指向 fixture，剥掉沙箱 PYTHONPATH"""
    env = dict(os.environ)
    env["WORKBUDDY_MIGRATE_HOME"] = str(home)
    env.pop("PYTHONPATH", None)
    return env


def make_wb_dir(base, name, uids_sessions, with_db=True):
    """造一个类似 <home>/.workbuddy 的目录

    uids_sessions: [(uid, session_count)]；为空列表时只造目录不造库。
    """
    d = Path(base) / name
    d.mkdir(parents=True, exist_ok=True)
    # 放一个占位文件，保证目录"非空"（_dir_in_use 靠这个判断）
    (d / "placeholder.txt").write_text("x", encoding="utf-8")

    if not with_db:
        return d

    db = d / "workbuddy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            " id TEXT PRIMARY KEY, user_id TEXT, title TEXT, cwd TEXT,"
            " created_at INTEGER, updated_at INTEGER, deleted_at INTEGER)"
        )
        for uid, count in uids_sessions:
            for i in range(count):
                conn.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?,NULL)",
                    (f"{uid[:8]}-s{i}", uid, f"t{i}", "/tmp", 0, 0),
                )
        conn.commit()
    finally:
        conn.close()

    # account-snapshot.json：让"客户端登录态"有值，贴近真实场景
    if uids_sessions:
        snap_dir = d / "storage" / "skeleton"
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "account-snapshot.json").write_text(
            json.dumps({"primary": {"uid": uids_sessions[0][0], "nickname": "tester"}}),
            encoding="utf-8",
        )
    return d


def probe(home):
    """子进程里导入 migrate，回传探测到的目录与双版本标记"""
    code = (
        "import sys, json;"
        f"sys.path.insert(0, {str(SCRIPTS)!r});"
        "import migrate;"
        "print(json.dumps({"
        "  'dir': str(migrate.WORKBUDDY_DIR),"
        "  'dual': sorted(migrate._DUAL_EDITION.keys()),"
        "}))"
    )
    out = subprocess.run(
        [PY, "-c", code], capture_output=True, text=True,
        env=_env(home), timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"probe 失败: {out.stderr[:400]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def run_diagnose(home, extra=()):
    """跑 migrate.py --diagnose，返回 (exit_code, stdout)"""
    out = subprocess.run(
        [PY, str(SCRIPT), "--diagnose", *extra],
        capture_output=True, text=True, env=_env(home), timeout=120,
    )
    return out.returncode, out.stdout + out.stderr


# ---------------------------------------------------------------- cases

def case_domestic_only(tmp):
    print("\n[1] 只有国内版 → 选中国内版，且不报双版本")
    home = Path(tmp) / "c1"
    home.mkdir()
    make_wb_dir(home, ".workbuddy", [(DOMESTIC_UID, 3)])

    r = probe(home)
    _check("选中 ~/.workbuddy", r["dir"].endswith(".workbuddy"), r["dir"])
    _check("未标记双版本", r["dual"] == [], str(r["dual"]))

    code, out = run_diagnose(home)
    _check("--diagnose 不输出双版本提示", "同时存在" not in out, out[:300])


def case_intl_only(tmp):
    print("\n[2] 只有国际版 → 选中国际版（保住原有行为）")
    home = Path(tmp) / "c2"
    home.mkdir()
    make_wb_dir(home, ".workbuddy-ai", [(INTL_UID, 2)])

    r = probe(home)
    _check("选中 ~/.workbuddy-ai", r["dir"].endswith(".workbuddy-ai"), r["dir"])
    _check("未标记双版本", r["dual"] == [], str(r["dual"]))


def case_both(tmp):
    print("\n[3] 两个版本都有数据 → 默认国内版 + 标记双版本（本次修复的核心）")
    home = Path(tmp) / "c3"
    home.mkdir()
    make_wb_dir(home, ".workbuddy", [(DOMESTIC_UID, 7)])
    make_wb_dir(home, ".workbuddy-ai", [(INTL_UID, 1)])

    r = probe(home)
    _check("不再无条件选国际版", r["dir"].endswith(".workbuddy"), r["dir"])
    _check("标记出双版本", r["dual"] == ["domestic", "intl"], str(r["dual"]))

    code, out = run_diagnose(home)
    _check("--diagnose 提示两个目录同时存在", "同时存在" in out, out[:300])
    _check("--diagnose 列出未诊断的另一版本", "另一版本的数据目录" in out, out[:400])
    _check("--diagnose 给出换版本指引", "--intl" in out, out[:400])
    _check("本次诊断覆盖国内版 7 条", "7" in out, out[:400])
    _check("列出了国际版的 1 条", "1 sessions" in out, out[:600])


def case_both_single_account_warning(tmp):
    print("\n[4] 双版本但当前版本只有一个账号 → 必须补一句「别据此下结论」")
    home = Path(tmp) / "c4"
    home.mkdir()
    make_wb_dir(home, ".workbuddy", [(DOMESTIC_UID, 2)])
    make_wb_dir(home, ".workbuddy-ai", [(INTL_UID, 9)])

    code, out = run_diagnose(home)
    _check("仍然提示只发现一个账号", "只发现一个账号" in out, out[:400])
    _check("紧跟一句范围限定", "别据此下结论" in out, out[:600])


def case_intl_flag_on_dual(tmp):
    print("\n[5] 双版本 + 显式 --intl → 诊断国际版，另一个指向国内版")
    home = Path(tmp) / "c5"
    home.mkdir()
    make_wb_dir(home, ".workbuddy", [(DOMESTIC_UID, 5)])
    make_wb_dir(home, ".workbuddy-ai", [(INTL_UID, 4)])

    code, out = run_diagnose(home, extra=("--intl",))
    _check("--intl 诊断到国际版账号", INTL_UID in out, out[:600])
    _check("另一个版本显示为国内版", "国内版" in out, out[:600])
    _check("提醒国内版有 5 条未纳入", "5 sessions" in out, out[:800])


def case_empty_intl_dir(tmp):
    print("\n[6] 国际版目录存在但为空 → 不该抢走国内版")
    home = Path(tmp) / "c6"
    home.mkdir()
    make_wb_dir(home, ".workbuddy", [(DOMESTIC_UID, 2)])
    (home / ".workbuddy-ai").mkdir()          # 空目录

    r = probe(home)
    _check("空目录不算已安装", r["dir"].endswith(".workbuddy"), r["dir"])
    _check("未误报双版本", r["dual"] == [], str(r["dual"]))


def case_no_dir_at_all(tmp):
    print("\n[7] 两个目录都不存在 → 回落国内版（不崩）")
    home = Path(tmp) / "c7"
    home.mkdir()

    r = probe(home)
    _check("回落 ~/.workbuddy", r["dir"].endswith(".workbuddy"), r["dir"])
    _check("未标记双版本", r["dual"] == [], str(r["dual"]))


def case_third_party_dir(tmp):
    print("\n[8] 双版本 + --dir 指到第三方目录 → 两个标准目录都算「另一个」")
    home = Path(tmp) / "c8"
    home.mkdir()
    make_wb_dir(home, ".workbuddy", [(DOMESTIC_UID, 3)])
    make_wb_dir(home, ".workbuddy-ai", [(INTL_UID, 2)])
    custom = make_wb_dir(home, "custom-profile", [(OTHER_UID, 6)])

    code, out = run_diagnose(home, extra=("--dir", str(custom)))
    _check("诊断的是自定义目录里的账号", OTHER_UID in out, out[:600])
    _check("国内版被列为另一版本", "国内版" in out, out[:800])
    _check("国际版也被列为另一版本", "国际版" in out, out[:800])


def main():
    print("=" * 70)
    print("双版本同装探测测试")
    print("=" * 70)
    tmp = tempfile.mkdtemp(prefix="wb-dual-")
    try:
        for fn in (
            case_domestic_only, case_intl_only, case_both,
            case_both_single_account_warning, case_intl_flag_on_dual,
            case_empty_intl_dir, case_no_dir_at_all, case_third_party_dir,
        ):
            fn(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
