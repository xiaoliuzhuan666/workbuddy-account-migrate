#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构造测试用临时 fixture

从真实数据目录【只读复制】必要子集到系统临时目录，供 migrate_session.py 测试使用。
严禁直接对真实数据目录执行迁移测试。

平台说明
--------
仅 Windows 实测通过（Windows 11 + Python 3.13）。

脚本本身是跨平台的（路径走 pathlib、临时目录走 tempfile），但复制的是本机
真实 WorkBuddy 数据，其内容（session 的 cwd、projects 目录名等）是 Windows
格式。macOS / Linux 未测试：若本机没有安装并登录过 WorkBuddy，这里造不出 fixture。

复制内容：workbuddy.db（用 sqlite backup API 安全快照，含 WAL）、projects/、memory/、tasks/、
storage/skeleton/account-snapshot.json
不复制：connectors/（内含 mcp.json 的 token / 凭据）、binaries/、cache/、logs/、blobs/ 等大目录

用法:
  python3 tests/prepare_fixture.py            # 重建并打印 fixture 根目录
  python3 tests/prepare_fixture.py --clean    # 删除 fixture
"""

import argparse
import atexit
import io
import os
import platform
import shutil
import stat
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Windows 终端默认可能是 GBK/CP936，打印第一个 emoji 就会 UnicodeEncodeError，
# 整个脚本直接崩 —— 而这是文档里让用户直接敲的入口命令（python tests/run_tests.py）。
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


def _home() -> Path:
    """用户 home；取不到时给一句可操作的提示而不是在 import 期崩

    以前顶层就是一句 `HOME = Path.home()`：在没有 HOME / USERPROFILE 的终端
    （CI、某些 Windows 服务上下文）里，import 本模块直接
    `RuntimeError: Could not determine home directory.` —— 连 --help 都打不出。
    """
    try:
        return Path.home()
    except RuntimeError as e:
        raise SystemExit(
            f"无法定位用户 home 目录（{e}）。\n"
            "请设置 HOME（macOS/Linux）或 USERPROFILE（Windows）后重试；"
            "若只是想看用法，这也说明当前终端缺这两个环境变量。"
        )


_HOME_CACHE = None


def home() -> Path:
    """用户 home（惰性求值）

    ⚠️ 不能搬到模块顶层去算：那是这个模块以前真实的 bug —— `HOME = Path.home()`
    在没有 HOME / USERPROFILE 的终端里让 **import 就崩**，连 `--help` 都打不出来。
    改成惰性之后，只看用法 / 不碰真实目录的路径依然可用。
    """
    global _HOME_CACHE
    if _HOME_CACHE is None:
        _HOME_CACHE = _home()
    return _HOME_CACHE


def editions() -> dict:
    """两个版本的数据目录（同样惰性，理由同 home()）"""
    h = home()
    return {
        "domestic": (h / ".workbuddy", ".workbuddy"),
        "intl": (h / ".workbuddy-ai", ".workbuddy-ai"),
    }


# 刻意不含 "connectors"：那里是 mcp.json / connector-states.json，
# 可能带 token / 凭据，复制到系统临时目录属于凭据泄漏；测试也不需要它。
KEEP_DIRS = ["projects", "memory", "tasks"]

FIXTURE_NAME = "wbmigrate-fixture"
# 交给 run_tests.py 用来给每次运行分配独立目录：固定路径 + 本机常驻客户端，
# 交叠运行会互相删对方的库，客户端 / 索引服务偶尔锁住被复制的 projects 子目录
# → rmtree 半清理 → 下一次运行中途撞上"fixture 缺少数据库"。
FIXTURE_DIR_ENV = "WBMIGRATE_FIXTURE_DIR"
# 独立目录化解决了交叠互删，但也带来自愈的丢失：进程被硬杀（taskkill / 断电 /
# os._exit）时 atexit 不会跑，残留目录里是**真实对话正文与记忆**的副本，
# 而下次运行用的是另一个新目录、永远不会再路过它。所以每次建 fixture 时
# 顺手清掉超过这个天数的同名旧目录。
FIXTURE_MAX_AGE_DAYS = 3


def fixture_root() -> Path:
    env = os.environ.get(FIXTURE_DIR_ENV)
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / FIXTURE_NAME


def sweep_stale_fixtures(tmpdir=None, max_age_days=FIXTURE_MAX_AGE_DAYS,
                         pattern=FIXTURE_NAME + "-*"):
    """清掉硬杀残留的旧 fixture 目录（含真实数据的副本，不该永远躺在 %TEMP%）

    每次运行都用自己的 `<name>-<pid>-<随机>` 目录（避免交叠互删），代价是失去
    自愈：进程被硬杀（taskkill / 断电 / os._exit）时 atexit 不跑，残留目录里的
    是**真实对话正文与记忆**的副本，而下一次运行用的是另一个新目录，永远不会
    再路过它。所以建新 fixture 时顺手扫一遍。

    只删「同命名规则 + 超过 max_age_days」的目录，不动任何别的东西；
    删不掉（被占用）就留着下次再说，不因此中断 build()。
    """
    base = Path(tmpdir) if tmpdir else Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_days * 86400
    removed = []
    try:
        for d in base.glob(pattern):
            if not d.is_dir():
                continue
            try:
                if d.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            if _rmtree(d, f"过期 fixture {d.name}"):
                removed.append(d.name)
    except OSError:
        return removed
    return removed


def copy_db(src: Path, dst: Path):
    """用 sqlite backup API 复制数据库，能安全包含 WAL 中未落盘的数据

    两个连接都要在 finally 里关：以前 `s = connect(...)` 写在 try 之外，
    `d = connect(...)` 一抛异常就泄漏 s —— 残留的库句柄会让下一次 build() 的
    rmtree 报 WinError 32 / 145，之后所有运行都卡死在 fixture 清理上。
    """
    s = None
    d = None
    try:
        s = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
        d = sqlite3.connect(str(dst))
        with d:
            s.backup(d)
    finally:
        if s is not None:
            s.close()
        if d is not None:
            d.close()


def _make_writable(path: Path):
    """抹掉目录树里的只读位

    Windows 上删不掉只读文件（unlink 直接 PermissionError）。fixture 里可能会
    出现只读文件：既有用例故意造的（未清理干净时），也有**备份产物**——副本把源
    的属性一起复制了过来。不清掉只读位，下一次运行会卡在「旧 fixture 删不掉」。
    """
    for p in [path, *([Path(r) / f for r, _d, fs in os.walk(str(path)) for f in fs]
                      if path.is_dir() else [])]:
        try:
            os.chmod(str(p), p.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass


def _rmtree(path: Path, what: str) -> bool:
    """删除目录；被占用时给出可操作提示而不是 traceback

    上一次运行中途崩溃（编码崩、断言异常、被 SIGTERM）会留下还被占用的 fixture，
    这里 rmtree 直接抛 WinError 32 / 145，下一次运行连 fixture 都建不出来。
    半清理状态下重建还会得到「半新半旧」的库（报 no such table: sessions），
    看起来像数据损坏，其实纯粹是清理不健壮的连锁反应。
    """
    try:
        shutil.rmtree(str(path))
        return True
    except OSError as e:
        # 只读文件导致的 PermissionError 可以自愈：抹掉只读位再试一次
        _make_writable(path)
        try:
            shutil.rmtree(str(path))
            return True
        except OSError as e2:
            e = e2
        print(f"⚠️  删除{what}失败：{e}")
        print(f"   通常是上一次运行残留的进程还占着文件。")
        print(f"   请关闭 WorkBuddy 客户端 / 测试进程后手动删除：{path}")
        return False



_BUILD_ROOT = None      # build() 期间正在建的目录（atexit 兜底清理用）
_BUILD_DONE = False     # 建成功后置真：此时目录要留给测试用，不能清


def _cleanup_half_built_fixture():
    """build() 没跑完就退出时的兜底清理（atexit 注册）"""
    if _BUILD_ROOT is None or _BUILD_DONE:
        return
    if _BUILD_ROOT.exists():
        print(f"  🧹 build() 中途失败，清理半截 fixture：{_BUILD_ROOT}")
        _rmtree(_BUILD_ROOT, "半截 fixture")


def build():
    """重建 fixture

    清理失败时不重建（详见上面那段判断）。除此之外的中途失败由
    `_cleanup_half_built_fixture()` 兜底 —— run_tests.py 只在**建成功之后**才
    注册它自己的清理钩子，而 fixture 是从真实数据目录拷出来的，半截留在 %TEMP%
    等于把个人数据摊在临时目录里。
    """
    global _BUILD_ROOT, _BUILD_DONE
    stale = sweep_stale_fixtures()
    if stale:
        print(f"  🧹 已清理 {len(stale)} 个过期 fixture 目录（上一次运行被硬杀留下的）")
    root = fixture_root()
    # 旧 fixture 清不掉就不要重建：半清理状态下建出来的库是「半新半旧」的，
    # 后面的用例会报 no such table: sessions，看起来像数据损坏
    if root.exists() and not _rmtree(root, "旧 fixture"):
        raise SystemExit(f"fixture 清理失败，已停止（不会重建半成品）：{root}")
    root.mkdir(parents=True)
    _BUILD_ROOT, _BUILD_DONE = root, False
    atexit.register(_cleanup_half_built_fixture)

    for name, (src, dirname) in editions().items():
        dst = root / dirname
        if not src.exists():
            print(f"  ⏭️  {name}: 源目录不存在，跳过 ({src})")
            continue
        dst.mkdir(parents=True, exist_ok=True)

        db = src / "workbuddy.db"
        if db.exists():
            copy_db(db, dst / "workbuddy.db")
        else:
            print(f"  ⏭️  {name}: 无 workbuddy.db")

        for sub in KEEP_DIRS:
            s = src / sub
            if s.exists():
                shutil.copytree(str(s), str(dst / sub))

        # 账号快照（用于推断当前 uid）
        snap = src / "storage" / "skeleton" / "account-snapshot.json"
        if snap.exists():
            (dst / "storage" / "skeleton").mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(snap), str(dst / "storage" / "skeleton" / "account-snapshot.json"))

        n_db = "有" if (dst / "workbuddy.db").exists() else "无"
        print(f"  ✅ {name}: db {n_db} → {dst}")

    print()
    print(f"FIXTURE_ROOT={root}")
    print()
    print("使用方式：")
    print(f'  export WORKBUDDY_MIGRATE_HOME="{root}"')
    print("  python3 scripts/migrate_session.py --list --from domestic")
    _BUILD_DONE = True   # 建好了：atexit 兜底不再清理，目录留给测试用
    return root


def clean():
    root = fixture_root()
    if root.exists():
        if _rmtree(root, "fixture"):
            print(f"已删除 {root}")
    else:
        print("fixture 不存在")


def main():
    p = argparse.ArgumentParser(description="构造测试用临时 fixture")
    p.add_argument("--clean", action="store_true", help="删除 fixture")
    args = p.parse_args()
    if args.clean:
        clean()
    else:
        print("=" * 60)
        print("构造测试 fixture（只读复制真实数据的子集）")
        print("=" * 60)
        build()


if __name__ == "__main__":
    main()
