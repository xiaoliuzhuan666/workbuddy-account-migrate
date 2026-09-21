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

复制内容：workbuddy.db（用 sqlite backup API 安全快照，含 WAL）、projects/、memory/、connectors/、tasks/
不复制：binaries/、cache/、logs/、blobs/ 等大目录

用法:
  python3 tests/prepare_fixture.py            # 重建并打印 fixture 根目录
  python3 tests/prepare_fixture.py --clean    # 删除 fixture
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
EDITIONS = {
    "domestic": (HOME / ".workbuddy", ".workbuddy"),
    "intl": (HOME / ".workbuddy-ai", ".workbuddy-ai"),
}
KEEP_DIRS = ["projects", "memory", "connectors", "tasks"]

FIXTURE_NAME = "wbmigrate-fixture"


def fixture_root() -> Path:
    return Path(tempfile.gettempdir()) / FIXTURE_NAME


def copy_db(src: Path, dst: Path):
    """用 sqlite backup API 复制数据库，能安全包含 WAL 中未落盘的数据"""
    s = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        with d:
            s.backup(d)
    finally:
        s.close()
        d.close()


def build():
    root = fixture_root()
    if root.exists():
        shutil.rmtree(str(root))
    root.mkdir(parents=True)

    for name, (src, dirname) in EDITIONS.items():
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
    return root


def clean():
    root = fixture_root()
    if root.exists():
        shutil.rmtree(str(root))
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
