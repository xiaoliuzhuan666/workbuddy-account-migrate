#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 跨设备项目迁移工具（export / import）

与另两个脚本的区别：
  - migrate.py         : 整个账号的数据合并（Session/Memory/Connector），同一台机器
  - migrate_session.py : 只迁移「一个对话」，支持跨版本（国内 ⇄ 国际），同一台机器
  - migrate_project.py : 把「一个项目」的全部会话打包，跨设备搬运（电脑A账号A → 电脑B账号B）

设计边界（issue #8）：
  - 工具只做两件纯本地的事：export 打包、import 解包落库
  - 包怎么传（AirDrop/U盘/scp/网盘）、目标机项目放哪、用哪个账号 —— 全部由用户决定
  - 源机只读不动；不做网络传输；不做账号级数据（memory/connectors）；不做自动双向同步

一个项目的会话数据包含（缺一样客户端就显示异常）：
  1. sessions 表行（user_id / cwd 在 import 时改写）
  2. session_usage 表行
  3. projects/{slug}/ 下的正文：{sid}.jsonl + .meta.json + .file-rollback.ndjson
  4. projects/{slug}/{sid}/tool-results/ 目录（大工具输出外溢处）
  5. todos/{sid}.json + tasks/{sid}/
  6. 可选：{项目}/.workbuddy/（工作区记忆 memory/ + 项目技能 skills/）

用法（推荐小白路线：无参数进向导，全程输序号 + 拖文件）:
  python3 scripts/migrate_project.py          # 向导：选「打包带走」或「导入进来」

  # 源机：打包一个项目的会话（只读，客户端开着也能跑；包默认放桌面）
  python3 scripts/migrate_project.py export                       # 交互式：列项目 → 选 → 打包
  python3 scripts/migrate_project.py export --cwd /path/项目A     # 指定项目

  # 查看包内容
  python3 scripts/migrate_project.py info 项目A.wbproj

  # 目标机：导入（必须关闭客户端；需指定项目在本机的新路径）
  python3 scripts/migrate_project.py import 项目A.wbproj                      # 交互式确认新路径
  python3 scripts/migrate_project.py import 项目A.wbproj --cwd /新路径 --dry-run

  # 高级：冲突策略 / 回滚
  python3 scripts/migrate_project.py import 项目A.wbproj --cwd /新路径 --on-conflict overwrite
  python3 scripts/migrate_project.py --backups
  python3 scripts/migrate_project.py --rollback <TAG>

冲突语义（"两地交替"场景的命根子）：
  - 硬冲突（同 id）  ：覆盖 —— 同一个包反复导入不会产生双份对话，id 稳定
  - 软冲突（同标题不同 id）：默认跳过并警告，交互模式可询问
  - 无 TTY 时一律降级为跳过，需显式 --on-conflict overwrite 才批量覆盖

环境变量:
  WORKBUDDY_MIGRATE_HOME   覆盖 home 目录（测试用，指向临时 fixture）

平台兼容性:
  脚本本身跨平台（pathlib + tasklist/ps 进程检测），逻辑复用 v1.6 系列已验证机制；
  跨设备全流程需两台真机实测，欢迎反馈。
"""

import argparse
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
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# 沙箱 shim 剥离（v1.6.2 教训）：WorkBuddy 会话内运行时，注入的 PYTHONPATH 指向
# sitecustomize.py 会劫持 Path.mkdir 等文件操作，备份阶段就崩。剥离后 re-exec 自身。
if os.environ.get("PYTHONPATH") and not os.environ.get("_WB_PROJECT_MIGRATE_REEXEC"):
    os.environ["_WB_PROJECT_MIGRATE_REEXEC"] = "1"
    os.environ.pop("PYTHONPATH")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# Windows 终端 GBK/CP936 编码下 emoji 崩溃（v1.1 教训）；先判编码再包装，避免二次包装
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

# ---------------------------------------------------------------- 常量

TOOL_VERSION = "1.0.0"
PKG_FORMAT = "workbuddy-project-export"
PKG_FORMAT_VERSION = 1
PKG_EXT = ".wbproj"

EDITIONS = {"domestic": "国内版", "intl": "国际版"}
EDITION_DIR = {"domestic": ".workbuddy", "intl": ".workbuddy-ai"}

PROC_KEYWORDS = ("workbuddy", "codebuddy")


def resolve_home() -> Path:
    """home 目录；WORKBUDDY_MIGRATE_HOME 优先（测试 fixture 隔离）"""
    env = os.environ.get("WORKBUDDY_MIGRATE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home()


# ---------------------------------------------------------------- 展示辅助


def fmt_size(n) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}B"


def fmt_time(ms) -> str:
    try:
        ms = int(ms or 0)
        if ms <= 0:
            return "-"
        return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


def dw(s) -> int:
    """显示宽度（中文算 2）"""
    try:
        import unicodedata
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))
    except Exception:
        return len(str(s))


def pad(s, width, align="left") -> str:
    s = str(s)
    gap = width - dw(s)
    if gap <= 0:
        return s
    return s + " " * gap if align == "left" else " " * gap + s


def clip(s, width) -> str:
    s = str(s)
    if dw(s) <= width:
        return s
    out = ""
    used = 0
    for c in s:
        w = 2 if dw(c) > 1 else 1
        if used + w > width - 2:
            return out + "…"
        out += c
        used += w
    return out


def ask_yes_no(prompt: str, default=None) -> bool:
    """y/n 询问；default 非 None 时空回车取默认（交互导入确认用）"""
    suffix = ""
    if default is True:
        suffix = " [回车=是]"
    elif default is False:
        suffix = " [回车=否]"
    try:
        r = input(f"{prompt}{suffix}: ").strip().lower()
    except EOFError:
        return bool(default) if default is not None else False
    if not r and default is not None:
        return default
    return r in ("y", "yes")


def is_interactive() -> bool:
    return sys.stdin.isatty()


# ---------------------------------------------------------------- 版本路径


@dataclass
class EditionPaths:
    """一个 WorkBuddy 版本的数据目录布局"""

    name: str
    root: Path
    db: Path
    projects_dir: Path
    todos_dir: Path
    tasks_dir: Path
    backup_dir: Path
    account_snapshot: Path

    @property
    def label(self) -> str:
        return EDITIONS.get(self.name, self.name)

    def exists(self) -> bool:
        return self.db.exists()


def resolve_edition(name: str, home: Optional[Path] = None) -> EditionPaths:
    home = home or resolve_home()
    root = home / EDITION_DIR[name]
    return EditionPaths(
        name=name,
        root=root,
        db=root / "workbuddy.db",
        projects_dir=root / "projects",
        todos_dir=root / "todos",
        tasks_dir=root / "tasks",
        backup_dir=root / "migrate_backups",
        account_snapshot=root / "storage" / "skeleton" / "account-snapshot.json",
    )


def cwd_to_slug(cwd: str) -> str:
    """工作目录 → projects 子目录名

    C:\\Users\\alice\\WorkBuddy\\2026-09-10-14-49-02
      → c-Users-alice-WorkBuddy-2026-09-10-14-49-02
    盘符转小写、去掉冒号、\\ 和 / 转 -。import 侧必须用【目标机新路径】推导，
    沿用源 slug 而 cwd 改了新路径 = 客户端按 projects/<slug>/<sid>.jsonl 找不到。
    """
    if not cwd:
        return ""
    s = re.sub(r"^([A-Za-z]):", lambda m: m.group(1).lower(), str(cwd))
    s = s.replace("\\", "-").replace("/", "-")
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


# ---------------------------------------------------------------- 数据库访问


def connect_ro(db: Path):
    """只读连接（URI 模式，能正确读到尚未 checkpoint 的 WAL 内容）"""
    if not db or not db.exists():
        return None
    try:
        return sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    except Exception:
        return None


def connect_rw(db: Path):
    return sqlite3.connect(str(db))


def table_columns(conn, table):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def wal_checkpoint(conn) -> bool:
    """WAL checkpoint，busy≠0 说明有进程占锁、结果未落盘（v1.6.1 教训：不能只打印）"""
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy = row[0] if row else 1
        if busy:
            print("  ⚠️  WAL checkpoint 未完成（busy），结果可能未落盘；请确认客户端已关闭后重试")
            return False
        return True
    except Exception as e:
        print(f"  ⚠️  WAL checkpoint 失败：{e}")
        return False


def snapshot_db(src: Path, dst: Path):
    """用 sqlite backup API 备份数据库（v1.6.1 教训：文件复制拿到的是陈旧快照）"""
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def get_current_uid(ep: EditionPaths):
    """推断某版本当前登录的 user_id

    优先级：account-snapshot.json 的 primary.uid（客户端真实登录态，v1.6.3 定下的权威来源）
    → DB 中 session 数最多的 user_id（兜底）。
    不读平台 storage.json —— 它是国内版的登录态文件，与客户端真实登录态可能长期不一致。
    """
    try:
        if ep.account_snapshot.exists():
            data = json.loads(ep.account_snapshot.read_text(encoding="utf-8"))
            uid = (data.get("primary") or {}).get("uid", "")
            if uid:
                return uid, "account-snapshot"
    except Exception:
        pass
    conn = connect_ro(ep.db)
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id ORDER BY 2 DESC LIMIT 1"
            ).fetchall()
            if rows and rows[0][0]:
                return rows[0][0], "db-majority"
        finally:
            conn.close()
    return "", "unknown"


# ---------------------------------------------------------------- 文件辅助


def path_size(p: Path) -> int:
    """目录需递归累加，否则 tool-results 会被算成 0（v1.6.0 教训）"""
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except OSError:
        return 0


def copy_path(src: Path, dst: Path):
    """复制文件或目录；目录目标已存在则先整体删除。

    会话附属物里有【目录】（tool-results/），不能直接 shutil.copy2（v1.6.0 教训）。
    """
    if src.is_dir():
        if dst.exists():
            remove_path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src), str(dst))
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))


def remove_path(p: Path):
    try:
        if p.is_dir():
            shutil.rmtree(str(p))
        elif p.exists():
            p.unlink()
    except FileNotFoundError:
        pass


def safe_copy(src: Path, dst: Path) -> bool:
    try:
        copy_path(src, dst)
        return True
    except OSError as e:
        print(f"  ⚠️  无法复制 {src}：{e}")
        print("     常见原因：客户端未完全退出、文件被占用或权限不足。")
        return False


def find_session_files(ep: EditionPaths, session_id: str):
    """定位某会话的正文文件（可能含 tool-results 目录），返回相对 projects_dir 的路径列表"""
    if not ep.projects_dir.exists():
        return []
    return sorted(
        p.relative_to(ep.projects_dir).as_posix()
        for p in ep.projects_dir.glob(f"*/{session_id}*")
    )


# ---------------------------------------------------------------- 进程检测


def find_running_clients():
    """检测正在运行的客户端进程，返回 (进程名列表, 检测是否可信)"""
    found = set()
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
            ).stdout
            for line in out.splitlines():
                row = next(csv.reader([line]), [])
                if row and any(k in row[0].lower() for k in PROC_KEYWORDS):
                    found.add(row[0])
        else:
            for ps_args in (["ps", "-eo", "comm="], ["ps", "-eo", "args="]):
                out = subprocess.run(
                    ps_args, capture_output=True, text=True,
                    errors="replace", timeout=15,
                ).stdout
                for line in out.splitlines():
                    name = line.strip()
                    if not name:
                        continue
                    if any(k in name.lower() for k in PROC_KEYWORDS):
                        found.add(Path(name.split()[0]).name or name)
    except Exception as e:
        print(f"⚠️  客户端进程检测失败（{e}），无法确认客户端是否已关闭")
        return [], False
    return sorted(found), True


def require_clients_closed(force=False) -> bool:
    """import 是写操作，客户端必须关闭"""
    running, trustworthy = find_running_clients()
    if not running and trustworthy:
        return True
    if not trustworthy and not force:
        print("❌ 无法确认客户端是否已关闭。若已确认全部退出，可用 --force 继续。")
        return False
    if running:
        print("=" * 70)
        print("❌ 检测到 WorkBuddy 客户端正在运行：")
        for n in running:
            print(f"   • {n}")
        print()
        print("  导入前必须关闭 WorkBuddy 窗口，原因：")
        print("   1. 数据还在 WAL 日志里没落盘，会读到旧数据")
        print("   2. 客户端内存缓存会在退出时把你的修改覆盖回去")
        print("   3. 两个进程同时持有数据库锁，写入可能失败")
        print()
        if not force:
            print("  请关闭所有 WorkBuddy 窗口后重新运行本命令（确知风险可用 --force）。")
            return False
        print("  ⚠️  已用 --force 跳过检测，后果自负。")
    return True


# ---------------------------------------------------------------- 包读写

_CWD_KEY_RE = re.compile(r'("cwd"\s*:\s*)("(?:[^"\\]|\\.)*")')


def rewrite_jsonl_cwd(src_fh, dst_fh, old_cwd: str, new_cwd: str) -> int:
    """流式改写 jsonl 每行顶层 "cwd" 字段值（实测每条消息顶层都带 cwd）。

    只动结构化字段值，消息文本里出现的路径字符串一律不碰（沿用 v1.6.1
    "不碰用户可见内容" 原则）。匹配时先 json 解码转义再比较，兼容 \\uXXXX 与原文两种存储。
    返回改写行数。
    """
    rewritten = 0
    new_val = json.dumps(new_cwd, ensure_ascii=False)
    for line in src_fh:
        if '"cwd"' not in line:
            dst_fh.write(line)
            continue
        try:
            stripped = line.strip()
            if not stripped.startswith("{"):
                dst_fh.write(line)
                continue
            obj = json.loads(stripped)
        except Exception:
            dst_fh.write(line)
            continue
        if isinstance(obj, dict) and obj.get("cwd") == old_cwd:

            def _sub(m, _new=new_val):
                return m.group(1) + _new

            new_line, n = _CWD_KEY_RE.subn(_sub, line, count=1)
            if n:
                dst_fh.write(new_line)
                rewritten += 1
                continue
        dst_fh.write(line)
    return rewritten


class ProjectPackageWriter:
    """把一个项目的数据写进 tar.gz 包（arcname 布局见 _pack 函数）"""

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self._tempfiles = []

    def _temp_json(self, data) -> str:
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        self._tempfiles.append(p)
        return p

    def write(self, manifest: dict, session_rows: list, usage_rows: list,
              file_sources: list, workspace_sources: list):
        """file_sources: [(绝对路径, arcname)]；workspace_sources: [(绝对路径, arcname)]"""
        with tarfile.open(str(self.out_path), "w:gz") as tar:
            for src, arc in file_sources:
                tar.add(str(src), arcname=arc)
            for src, arc in workspace_sources:
                tar.add(str(src), arcname=arc)
            for name, data in (
                ("sessions.json", session_rows),
                ("usage.json", usage_rows),
                ("manifest.json", manifest),
            ):
                p = self._temp_json(data)
                tar.add(p, arcname=name)
        for p in self._tempfiles:
            try:
                os.unlink(p)
            except OSError:
                pass
        self._tempfiles = []


def read_package_members(pkg: Path):
    """读包，返回 (manifest, members_by_name)；不落盘，全部流式"""
    tar = tarfile.open(str(pkg), "r:gz")
    manifest = None
    members = {}
    for m in tar.getmembers():
        if not m.isfile():
            continue
        name = m.name
        if name.startswith("./"):
            name = name[2:]
        if ".." in name or name.startswith("/"):
            raise RuntimeError(f"包内出现非法路径成员：{m.name}")
        members[name] = m
        if name == "manifest.json":
            fh = tar.extractfile(m)
            manifest = json.loads(fh.read().decode("utf-8"))
    if manifest is None:
        tar.close()
        raise RuntimeError("包内缺少 manifest.json，不是本工具导出的项目包")
    if manifest.get("format") != PKG_FORMAT:
        tar.close()
        raise RuntimeError(f"包格式不符：{manifest.get('format')}")
    return tar, manifest, members


def read_json_member(tar, members, name, default=None):
    m = members.get(name)
    if m is None:
        return default
    fh = tar.extractfile(m)
    return json.loads(fh.read().decode("utf-8"))


def stream_member_to_file(tar, member, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    fh = tar.extractfile(member)
    with open(dst, "wb") as out:
        shutil.copyfileobj(fh, out)


# ---------------------------------------------------------------- export


def list_projects(ep: EditionPaths):
    """按 cwd 分组列出项目：cwd / 会话数 / 最后活动"""
    conn = connect_ro(ep.db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT cwd, COUNT(*) AS n, MAX(last_activity_at) AS last "
            "FROM sessions WHERE deleted_at IS NULL AND cwd IS NOT NULL AND cwd != '' "
            "GROUP BY cwd ORDER BY last DESC"
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]
    finally:
        conn.close()


def normalize_cwd(p: str) -> str:
    s = os.path.expanduser(str(p).strip())
    s = os.path.abspath(s)
    if len(s) > 1:
        s = s.rstrip("/") or "/"
    return s


def gather_session_files(ep: EditionPaths, sid: str):
    """收集一个会话在数据目录里的全部文件（projects 正文 + todos + tasks）"""
    rels = []
    for rel in find_session_files(ep, sid):
        rels.append(("projects", rel))
    if ep.todos_dir.exists():
        for p in ep.todos_dir.glob(f"{sid}.json"):
            rels.append(("todos", p.name))
    if ep.tasks_dir.exists():
        for p in ep.tasks_dir.glob(sid):
            if p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file():
                        rels.append(("tasks", f.relative_to(ep.tasks_dir).as_posix()))
    return rels


def collect_workspace_files(project_cwd: Path) -> list:
    """收集 {项目}/.workbuddy/ 下的 memory/ 与 skills/（其他子目录不动）"""
    out = []
    wb = project_cwd / ".workbuddy"
    if not wb.exists():
        return out
    for sub in ("memory", "skills"):
        base = wb / sub
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file():
                out.append((f, f"workspace/.workbuddy/{sub}/{f.relative_to(base).as_posix()}"))
    return out


def do_export(args) -> int:
    home = resolve_home()
    name = "intl" if args.intl else "domestic"
    if args.dir:
        root = Path(args.dir).expanduser()
        ep = EditionPaths(
            name=name, root=root, db=root / "workbuddy.db",
            projects_dir=root / "projects", todos_dir=root / "todos",
            tasks_dir=root / "tasks", backup_dir=root / "migrate_backups",
            account_snapshot=root / "storage" / "skeleton" / "account-snapshot.json",
        )
    else:
        ep = resolve_edition(name, home)

    if not ep.exists():
        print(f"❌ 未找到 {ep.label} 数据目录：{ep.db}")
        return 1

    projects = list_projects(ep)
    if not projects:
        print("❌ 数据库里没有任何带工作目录的会话，无可导出项目")
        return 1

    # 选项目
    if args.cwd:
        target_cwd = normalize_cwd(args.cwd)
        matched = [p for p in projects if normalize_cwd(p[0]) == target_cwd]
        if not matched:
            # 大小写不敏感兜底 + 近似提示
            lower_map = {normalize_cwd(p[0]).lower(): p for p in projects}
            hit = lower_map.get(target_cwd.lower())
            if hit:
                matched = [hit]
                print(f"ℹ️  按大小写不敏感匹配到：{hit[0]}")
        if not matched:
            print(f"❌ 数据库中没有 cwd 为以下路径的会话：\n   {target_cwd}")
            close = [p[0] for p in projects if target_cwd.lower() in p[0].lower()]
            if close:
                print("   相似路径：")
                for c in close[:5]:
                    print(f"     {c}")
            return 1
        sel_cwd = matched[0][0]
    else:
        print("=" * 70)
        print(f"{ep.label} 项目列表（按最后活动排序）")
        print("=" * 70)
        w = 52
        print(f"  {'序号':<4}{pad('工作目录', w)}{'会话':>6}{'最后活动':>12}")
        for i, (cwd, n, last) in enumerate(projects, 1):
            print(f"  {i:<4}{pad(clip(cwd, w), w)}{n:>5} {fmt_time(last):>11}")
        try:
            idx = input("\n请选择要导出的项目（输入序号）: ").strip()
            sel = projects[int(idx) - 1]
            sel_cwd = sel[0]
        except (ValueError, IndexError, EOFError):
            print("❌ 无效选择")
            return 1

    # 收集会话与文件
    conn = connect_ro(ep.db)
    if conn is None:
        print("❌ 数据库无法读取")
        return 1
    try:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions WHERE cwd = ? AND deleted_at IS NULL "
            "ORDER BY last_activity_at ASC", (sel_cwd,))]
    finally:
        conn.close()
    if not rows:
        print("❌ 该项目下没有会话")
        return 1

    uid, uid_src = get_current_uid(ep)
    running, _t = find_running_clients()
    if running:
        print(f"ℹ️  客户端正在运行（{', '.join(running)}）。导出是只读操作可继续，")
        print("   但正在进行中的对话可能不完整；追求完整请关闭客户端后重新导出。")

    print(f"\n📦 项目：{sel_cwd}")
    print(f"   会话数：{len(rows)}")
    file_sources = []      # (abs_path, arcname)
    per_session = []
    missing_files = []
    for row in rows:
        sid = row["id"]
        rels = gather_session_files(ep, sid)
        has_jsonl = any(k == "projects" and rel.endswith(f"/{sid}.jsonl") for k, rel in rels)
        if not has_jsonl:
            missing_files.append(sid)
        arcs = []
        for kind, rel in rels:
            arc = f"{kind}/{rel}" if kind != "projects" else f"projects/{rel}"
            arcs.append(arc)
            file_sources.append((ep.root / kind / rel if kind != "projects" else ep.projects_dir / rel, arc))
        per_session.append({
            "id": sid,
            "title": row.get("title") or "",
            "cwd": row.get("cwd") or "",
            "user_id": row.get("user_id") or "",
            "last_activity_at": int(row.get("last_activity_at") or 0),
            "files": arcs,
            "has_usage": True,
        })

    usage_rows = []
    conn = connect_ro(ep.db)
    try:
        conn.row_factory = sqlite3.Row
        for row in rows:
            u = conn.execute("SELECT * FROM session_usage WHERE session_id = ?", (row["id"],)).fetchone()
            if u:
                usage_rows.append(dict(u))
    finally:
        conn.close()

    workspace_sources = []
    if not args.no_workspace_memory:
        workspace_sources = collect_workspace_files(Path(sel_cwd))

    manifest = {
        "format": PKG_FORMAT,
        "format_version": PKG_FORMAT_VERSION,
        "tool_version": TOOL_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "edition": ep.name,
        "source_cwd": sel_cwd,
        "source_uid": (rows[0].get("user_id") or ""),
        "client_login_uid": uid,
        "uid_source": uid_src,
        "session_count": len(rows),
        "include_workspace": bool(workspace_sources) and not args.no_workspace_memory,
        "sessions": per_session,
    }

    # 输出文件名：项目目录名 + 日期，默认放桌面（小白好找、好拖去传输）
    default_name = f"{Path(sel_cwd).name or 'project'}-workbuddy-{datetime.now().strftime('%Y%m%d-%H%M%S')}{PKG_EXT}"
    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        desktop = resolve_home() / "Desktop"
        out_path = (desktop if desktop.is_dir() else Path.cwd()) / default_name

    total_size = sum(path_size(Path(s)) for s, _ in file_sources)
    print(f"   正文+附属：{len(file_sources)} 项（{fmt_size(total_size)}）")
    print(f"   usage 行：{len(usage_rows)}")
    print(f"   工作区记忆/技能：{len(workspace_sources)} 个文件" if workspace_sources else "   工作区记忆/技能：无")
    if missing_files:
        print(f"   ⚠️  {len(missing_files)} 个会话没有正文文件（对话打开将为空）：")
        for sid in missing_files[:5]:
            print(f"      {sid}")
        if len(missing_files) > 5:
            print(f"      …等 {len(missing_files)} 个")
    if not is_interactive() and not args.yes:
        print("\n（非交互模式，默认继续导出）")

    writer = ProjectPackageWriter(out_path)
    writer.write(manifest, rows, usage_rows, file_sources, workspace_sources)
    print(f"\n✅ 导出完成：{out_path}（{fmt_size(out_path.stat().st_size)}）")
    print("   接下来：把这个文件传到目标电脑（AirDrop / U 盘 / scp / 网盘均可），")
    print("   在目标机上运行： python3 scripts/migrate_project.py import "
          f"{out_path.name} --cwd <项目在目标机的新路径>")
    return 0


# ---------------------------------------------------------------- import


def stash_session_files(ep: EditionPaths, sid: str, backup_dir: Path) -> list:
    """把目标侧已存在的会话文件存进备份（覆盖前调用），返回备份内相对路径列表"""
    stash_dir = backup_dir / "overwritten" / sid
    saved = []
    for kind, rel in (
        [("projects", r) for r in find_session_files(ep, sid)]
        + [("todos", r.name) for r in (ep.todos_dir.glob(f"{sid}.json") if ep.todos_dir.exists() else [])]
        + ([("tasks", f.relative_to(ep.tasks_dir).as_posix())
            for f in sorted(ep.tasks_dir.rglob(f"{sid}/*")) if f.is_file()]
           if ep.tasks_dir.exists() else [])
    ):
        src = ep.projects_dir / rel if kind == "projects" else (ep.todos_dir / rel if kind == "todos" else ep.tasks_dir / rel)
        if src.exists():
            dst = stash_dir / kind / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_path(src, dst)
            saved.append({"kind": kind, "rel": rel})
    return saved


def find_packages() -> list:
    """在常见位置（当前目录 / 桌面 / 下载）发现迁移包，小白不用敲路径"""
    bases = [Path.cwd(), resolve_home() / "Desktop", resolve_home() / "Downloads"]
    out, seen = [], set()
    for base in bases:
        if base.is_dir():
            for p in sorted(base.glob(f"*{PKG_EXT}")):
                try:
                    key = p.resolve()
                except OSError:
                    key = p
                if key not in seen:
                    seen.add(key)
                    out.append(p)
    return out


def pick_package() -> Optional[Path]:
    """交互式挑一个迁移包；找不到时引导手动输入（支持拖拽）"""
    found = find_packages()
    if found:
        print("\n发现以下迁移包：")
        for i, p in enumerate(found, 1):
            print(f"  {i}. {p.name}  （{fmt_size(p.stat().st_size)}，{p.parent}）")
        print(f"  m. 手动输入路径（可把包文件从访达拖进本窗口）")
        try:
            choice = input("请选择要导入的包（输入序号）: ").strip()
        except EOFError:
            return None
        if choice.lower() in ("m", "手动"):
            raw = input("包文件路径: ").strip().strip('"').strip("'")
            return Path(raw).expanduser() if raw else None
        try:
            return found[int(choice) - 1]
        except (ValueError, IndexError):
            return None
    print(f"\n未在 当前目录 / 桌面 / 下载 里找到 {PKG_EXT} 迁移包。")
    raw = input("请输入包文件路径（可把文件从访达拖进本窗口）: ").strip().strip('"').strip("'")
    return Path(raw).expanduser() if raw else None


def do_import(args) -> int:
    if getattr(args, "package", None):
        pkg = Path(args.package).expanduser()
    elif is_interactive():
        picked = pick_package()
        if picked is None:
            print("❌ 未选择迁移包")
            return 1
        pkg = picked
    else:
        print(f"❌ 非交互模式必须指定迁移包路径：migrate_project.py import xxx{PKG_EXT}")
        return 1
    if not pkg.exists():
        print(f"❌ 包不存在：{pkg}")
        return 1

    name = "intl" if args.intl else "domestic"
    if args.dir:
        root = Path(args.dir).expanduser()
        ep = EditionPaths(
            name=name, root=root, db=root / "workbuddy.db",
            projects_dir=root / "projects", todos_dir=root / "todos",
            tasks_dir=root / "tasks", backup_dir=root / "migrate_backups",
            account_snapshot=root / "storage" / "skeleton" / "account-snapshot.json",
        )
    else:
        ep = resolve_edition(name, resolve_home())

    if not ep.exists():
        print(f"❌ 未找到 {ep.label} 数据目录：{ep.db}")
        return 1

    tar, manifest, members = read_package_members(pkg)
    try:
        return _import_inner(args, ep, tar, manifest, members, pkg)
    finally:
        tar.close()


def _import_inner(args, ep: EditionPaths, tar, manifest, members, pkg) -> int:
    sessions_data = read_json_member(tar, members, "sessions.json", [])
    usage_data = read_json_member(tar, members, "usage.json", [])
    if not sessions_data:
        print("❌ 包内没有会话数据")
        return 1
    src_cwd = manifest.get("source_cwd") or ""
    if not src_cwd:
        print("❌ manifest 缺少 source_cwd")
        return 1

    target_uid, uid_src = get_current_uid(ep)
    if not target_uid:
        print("❌ 无法确定目标机当前登录账号（account-snapshot.json 缺失且 DB 无会话）")
        print("   请先在 WorkBuddy 里登录账号并至少开始过一个对话，再运行导入。")
        return 1

    # 新路径：参数或交互式输入；必须真实存在（防"迁移成功却打不开"）
    new_cwd = normalize_cwd(args.cwd) if args.cwd else ""
    if not new_cwd:
        if not is_interactive():
            print("❌ 非交互模式必须用 --cwd 指定项目在目标机的新路径")
            return 1
        print("=" * 70)
        print("项目导入")
        print("=" * 70)
        print(f"  包：{pkg.name}")
        print(f"  源机路径：{src_cwd}")
        print(f"  会话数：{manifest.get('session_count')}（导出于 {manifest.get('created_at')}）")
        print()
        raw = input("请输入项目在【本机】的路径（目录必须已存在；可把项目文件夹从访达拖进本窗口）: ").strip()
        if not raw:
            print("❌ 未输入路径")
            return 1
        new_cwd = normalize_cwd(raw)
    if not Path(new_cwd).is_dir():
        print(f"❌ 路径在本机不存在（或不是目录）：{new_cwd}")
        print("   请先把项目代码放到本机（或用 git 拉下来），再运行导入。")
        return 1

    print(f"\n📥 目标：{ep.label}  登录账号 {target_uid[:8]}…（{uid_src}）")
    print(f"   新路径：{new_cwd}")

    if args.dry_run:
        print("\n（dry-run 模式，只预览不写盘）")

    # 冲突判定
    conn = connect_ro(ep.db)
    if conn is None:
        print("❌ 目标数据库无法读取")
        return 1
    try:
        conn.row_factory = sqlite3.Row
        local_rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM sessions")}
        local_titles = {}
        for r in local_rows.values():
            local_titles.setdefault(str(r.get("title") or ""), []).append(r)
    finally:
        conn.close()

    on_conflict = args.on_conflict
    plan = []  # (row, action, note)
    skip_n = 0
    for row in sessions_data:
        sid = row["id"]
        if sid in local_rows:
            local = local_rows[sid]
            pkg_newer = int(row.get("last_activity_at") or 0) > int(local.get("last_activity_at") or 0)
            action = None
            if on_conflict == "overwrite":
                action = "overwrite"
            elif on_conflict == "skip":
                action = "skip"
            elif is_interactive():
                print(f"\n⚠️  目标已存在相同 id 的对话：【{clip(local.get('title') or '(无标题)', 40)}】")
                print(f"    包内最后活动 {fmt_time(row.get('last_activity_at'))} / "
                      f"本地最后活动 {fmt_time(local.get('last_activity_at'))}")
                rec = "覆盖（包内更新）" if pkg_newer else "跳过（本地更新）"
                print(f"    建议：{rec}")
                a = input("    [o] 覆盖 / [s] 跳过: ").strip().lower()
                action = "overwrite" if a in ("o", "overwrite") else "skip"
            else:
                action = "skip"
                print(f"ℹ️  {sid[:8]}… 与本地会话 id 相同，非交互模式默认跳过"
                      f"（要覆盖请加 --on-conflict overwrite）")
            if action == "overwrite":
                note = "覆盖本地（本地较新）" if not pkg_newer else "覆盖本地"
                plan.append((row, "overwrite", note))
            else:
                skip_n += 1
        else:
            same_title = local_titles.get(str(row.get("title") or ""), [])
            if same_title:
                note = f"软冲突：本地已有同标题对话 x{len(same_title)}"
                if is_interactive() and on_conflict == "ask":
                    print(f"\n⚠️  本地已有【同标题不同 id】的对话：{clip(row.get('title') or '(无标题)', 40)}")
                    a = input("    [i] 仍导入（会出现两条同标题对话） / [s] 跳过: ").strip().lower()
                    if a in ("i", "import"):
                        plan.append((row, "insert", note + "，用户选择仍导入"))
                    else:
                        skip_n += 1
                    continue
                # 非交互 / 显式策略：一律跳过（重复迁移会在客户端出现两条同标题对话，必须拦）
                print(f"ℹ️  {sid[:8]}… {note}，跳过（交互模式可询问处理）")
                skip_n += 1
            else:
                plan.append((row, "insert", ""))

    if not plan:
        print("\n没有需要导入的会话（全部跳过）。")
        return 0

    print(f"\n导入计划：{len(plan)} 个会话（覆盖 {sum(1 for _, a, _ in plan if a == 'overwrite')} / "
          f"新增 {sum(1 for _, a, _ in plan if a == 'insert')}），跳过 {skip_n}")
    for row, action, note in plan[:10]:
        mark = "🔄" if action == "overwrite" else "➕"
        extra = f"  {note}" if note else ""
        print(f"  {mark} {clip(row.get('title') or '(无标题)', 38)}  {fmt_time(row.get('last_activity_at'))}{extra}")
    if len(plan) > 10:
        print(f"  …等 {len(plan)} 个")

    if args.dry_run:
        print("\n✅ dry-run 结束，未写入任何数据")
        return 0

    # 客户端必须关闭（写操作）
    if not require_clients_closed(force=args.force):
        return 2

    # 交互模式：写入前最后确认（这就是小白的 dry-run 替代品，计划已在上面展示）
    if is_interactive() and not getattr(args, "yes", False):
        if not ask_yes_no("\n确认按上述计划导入？", default=True):
            print("已取消，未做任何修改")
            return 0

    # 备份
    tag = datetime.now().strftime("%Y%m%d%H%M%S") + "_project-import"
    backup_dir = ep.backup_dir / tag
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_db(ep.db, backup_dir / "workbuddy.db")
    except Exception as e:
        print(f"❌ 备份数据库失败（{e}），中止导入")
        shutil.rmtree(str(backup_dir), ignore_errors=True)
        return 1
    meta = {
        "kind": "project_import",
        "tag": tag,
        "package": str(pkg),
        "source_cwd": src_cwd,
        "new_cwd": new_cwd,
        "target_uid": target_uid,
        "tool_version": TOOL_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "applied": [],   # 每个已执行会话一条：{id, action, created_files: [rel], stashed: n}
        "workspace_merged": [],
    }

    dst_slug = cwd_to_slug(new_cwd)
    if not dst_slug:
        print("❌ 无法从新路径推导 projects 子目录名")
        shutil.rmtree(str(backup_dir), ignore_errors=True)
        return 1

    def save_meta():
        with open(backup_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)

    save_meta()

    ok_n = fail_n = 0
    conn = connect_rw(ep.db)
    try:
        cur = conn.cursor()
        s_cols = table_columns(conn, "sessions")
        u_cols = table_columns(conn, "session_usage")
        if not s_cols:
            print("❌ 目标库 sessions 表无法读取")
            return 1

        for row, action, _note in plan:
            sid = row["id"]
            try:
                if action == "overwrite":
                    stash_session_files(ep, sid, backup_dir)

                # 1. sessions 行：user_id / cwd 改写，列名对齐插入（版本漂移是现实：43 列 vs 30 列）
                new_row = dict(row)
                new_row["user_id"] = target_uid
                new_row["cwd"] = new_cwd
                cols = [c for c in new_row.keys() if c in s_cols]
                cur.execute(
                    f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [new_row[c] for c in cols],
                )

                # 2. usage 行
                urow = next((u for u in usage_data if u.get("session_id") == sid), None)
                if urow and u_cols:
                    ucols = [c for c in urow.keys() if c in u_cols]
                    cur.execute(
                        f"INSERT OR REPLACE INTO session_usage ({','.join(ucols)}) "
                        f"VALUES ({','.join('?' * len(ucols))})",
                        [urow[c] for c in ucols],
                    )

                # 3. workspaces 登记（新路径）。注意：真实库的 workspaces 表
                #    没有 UNIQUE 约束，INSERT OR IGNORE 拦不住重复导入，
                #    先删后插保证一个路径只有一行。
                try:
                    cur.execute("DELETE FROM workspaces WHERE path = ?", (new_cwd,))
                    cur.execute(
                        "INSERT INTO workspaces (path, last_opened_at) VALUES (?, ?)",
                        (new_cwd, int(row.get("last_activity_at") or time.time() * 1000)),
                    )
                except Exception:
                    pass

                created_files = []
                # 4. 正文与附属文件：projects/{src_slug}/… → projects/{dst_slug}/…
                for arc, m in sorted(members.items()):
                    if not arc.startswith("projects/"):
                        continue
                    rel = arc[len("projects/"):]
                    parts = rel.split("/", 1)
                    if len(parts) < 2:
                        continue
                    # 只搬运本会话名下的成员
                    base = parts[1]
                    if not base.startswith(sid):
                        continue
                    dst_rel = f"{dst_slug}/{base}"
                    dst = ep.projects_dir / dst_rel
                    if dst.exists():
                        remove_path(dst)
                    stream_member_to_file(tar, m, dst)
                    created_files.append(("projects", dst_rel))
                    # jsonl 顶层 cwd 字段改写（流式，二次写回）
                    if base == f"{sid}.jsonl":
                        tmp = dst.with_suffix(".jsonl.tmp")
                        with open(dst, "r", encoding="utf-8", errors="replace") as fin, \
                             open(tmp, "w", encoding="utf-8") as fout:
                            rewrite_jsonl_cwd(fin, fout, row.get("cwd") or src_cwd, new_cwd)
                        os.replace(str(tmp), str(dst))

                # 5. todos / tasks
                for arc, m in sorted(members.items()):
                    if arc.startswith(f"todos/{sid}.json"):
                        dst = ep.todos_dir / f"{sid}.json"
                        if dst.exists():
                            remove_path(dst)
                        stream_member_to_file(tar, m, dst)
                        created_files.append(("todos", f"{sid}.json"))
                    elif arc.startswith(f"tasks/{sid}/"):
                        rel = arc[len("tasks/"):]
                        dst = ep.tasks_dir / rel
                        if dst.exists():
                            remove_path(dst)
                        stream_member_to_file(tar, m, dst)
                        created_files.append(("tasks", rel))

                conn.commit()
                meta["applied"].append({
                    "id": sid, "action": action,
                    "created_files": created_files,
                })
                save_meta()
                ok_n += 1
                print(f"  ✅ {clip(row.get('title') or sid[:8], 36)}")
            except sqlite3.Error as e:
                conn.rollback()
                fail_n += 1
                print(f"  ❌ {sid[:8]}… 写库失败：{e}")
                print("     可用 --rollback 恢复到导入前状态")
            except OSError as e:
                conn.rollback()
                fail_n += 1
                print(f"  ❌ {sid[:8]}… 文件操作失败：{e}")
                print("     可用 --rollback 恢复到导入前状态")

        wal_checkpoint(conn)
    finally:
        conn.close()

    # 6. 工作区记忆合并（追加式文本，不回滚——与账号级 Memory 同一哲学）
    if manifest.get("include_workspace"):
        merged = 0
        for arc, m in sorted(members.items()):
            if not arc.startswith("workspace/.workbuddy/memory/"):
                continue
            rel = arc[len("workspace/"):]
            dst = Path(new_cwd) / rel
            if dst.exists():
                try:
                    existing = dst.read_text(encoding="utf-8", errors="replace")
                    incoming = tar.extractfile(m).read().decode("utf-8", errors="replace")
                    new_lines = [l for l in incoming.split("\n") if l not in set(existing.split("\n"))]
                    if new_lines:
                        with open(dst, "a", encoding="utf-8") as f:
                            f.write("\n" + "\n".join(new_lines))
                        merged += 1
                except OSError:
                    pass
            else:
                stream_member_to_file(tar, m, dst)
                merged += 1
        for arc, m in sorted(members.items()):
            if arc.startswith("workspace/.workbuddy/skills/"):
                rel = arc[len("workspace/"):]
                dst = Path(new_cwd) / rel
                if not dst.exists():
                    stream_member_to_file(tar, m, dst)
        if merged:
            meta["workspace_merged"] = [new_cwd]
            save_meta()
            print(f"\n📎 工作区记忆已合并到 {Path(new_cwd) / '.workbuddy'}（{merged} 个文件，不参与回滚）")

    # 验证
    conn = connect_ro(ep.db)
    try:
        hit = 0
        for row, action, _ in plan:
            n = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ? AND user_id = ?", (row["id"], target_uid)
            ).fetchone()[0]
            hit += 1 if n else 0
    finally:
        conn.close()

    print("\n" + "=" * 70)
    print(f"导入完成：成功 {ok_n}（验证命中 {hit}），失败 {fail_n}，跳过 {skip_n}")
    print("=" * 70)
    if ok_n:
        print(f"  回滚： python3 scripts/migrate_project.py --rollback {tag}")
        print("  请重启 WorkBuddy 客户端让左侧列表刷新。")
    return 0 if fail_n == 0 else 2


# ---------------------------------------------------------------- rollback / backups


def list_backups(ep: EditionPaths):
    if not ep.backup_dir.exists():
        print("（还没有任何备份）")
        return
    found = False
    for d in sorted(ep.backup_dir.iterdir()):
        meta_f = d / "meta.json"
        if not meta_f.exists():
            continue
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("kind") != "project_import":
            continue
        found = True
        print(f"  {meta.get('tag')}  {meta.get('created_at')}  "
              f"包={Path(meta.get('package') or '').name}  "
              f"路径={meta.get('new_cwd')}")
    if not found:
        print("（本项目工具尚无备份）")


def do_rollback(tag: str, args) -> int:
    name = "intl" if args.intl else "domestic"
    ep = resolve_edition(name, resolve_home())
    if args.dir:
        root = Path(args.dir).expanduser()
        ep = EditionPaths(
            name=name, root=root, db=root / "workbuddy.db",
            projects_dir=root / "projects", todos_dir=root / "todos",
            tasks_dir=root / "tasks", backup_dir=root / "migrate_backups",
            account_snapshot=root / "storage" / "skeleton" / "account-snapshot.json",
        )
    backup_dir = ep.backup_dir / tag
    meta_f = backup_dir / "meta.json"
    if not meta_f.exists():
        print(f"❌ 备份不存在或缺少 meta.json：{tag}")
        return 1
    meta = json.loads(meta_f.read_text(encoding="utf-8"))
    if meta.get("kind") != "project_import":
        print("❌ 不是本工具创建的备份，请用对应工具回滚")
        return 1

    if not require_clients_closed(force=args.force):
        return 2

    print(f"回滚目标：{tag}")
    print(f"  导入路径：{meta.get('new_cwd')}")
    print(f"  已执行会话：{len(meta.get('applied') or [])}")
    if not args.yes and not ask_yes_no("确认回滚？（导入的会话将被移除/还原）"):
        print("已取消")
        return 0

    db_backup = backup_dir / "workbuddy.db"
    if not db_backup.exists():
        print("❌ 备份缺少数据库快照，无法回滚")
        return 1

    # 1. 还原数据库快照。
    #    先清掉目标库可能残留的 -wal/-shm：陈旧 WAL 会在新连接上被重放，
    #    把刚还原的快照又覆盖回导入后的状态（等于白滚）。
    for suffix in ("-wal", "-shm"):
        remove_path(Path(str(ep.db) + suffix))
    snapshot_db(db_backup, ep.db)

    # 2. 还原被覆盖的文件、删除新创建的文件
    restored = removed = 0
    for item in meta.get("applied", []):
        sid = item["id"]
        # 删除导入产生的文件（含覆盖后写入的）
        for kind, rel in item.get("created_files", []):
            base = {"projects": ep.projects_dir, "todos": ep.todos_dir, "tasks": ep.tasks_dir}[kind]
            remove_path(base / rel)
            removed += 1
        # 从备份还原覆盖前的文件
        stash_dir = backup_dir / "overwritten" / sid
        if stash_dir.exists():
            for f in stash_dir.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(stash_dir).as_posix()  # kind/rel
                parts = rel.split("/", 1)
                if len(parts) != 2:
                    continue
                kind, frel = parts
                base = {"projects": ep.projects_dir, "todos": ep.todos_dir, "tasks": ep.tasks_dir}.get(kind)
                if base is None:
                    continue
                dst = base / frel
                copy_path(f, dst)
                restored += 1

    conn = connect_rw(ep.db)
    try:
        wal_checkpoint(conn)
    finally:
        conn.close()

    print(f"\n✅ 回滚完成：数据库已还原快照；删除导入文件 {removed} 项，还原覆盖前文件 {restored} 项")
    print("   工作区记忆的合并不参与回滚（追加式文本）。")
    print("   请重启 WorkBuddy 客户端。")
    return 0


# ---------------------------------------------------------------- info


def do_info(pkg_path: str) -> int:
    pkg = Path(pkg_path).expanduser()
    if not pkg.exists():
        print(f"❌ 包不存在：{pkg}")
        return 1
    tar, manifest, members = read_package_members(pkg)
    tar.close()
    print("=" * 70)
    print(f"包：{pkg.name}（{fmt_size(pkg.stat().st_size)}）")
    print("=" * 70)
    print(f"  格式：{manifest.get('format')} v{manifest.get('format_version')}（工具 v{manifest.get('tool_version')}）")
    print(f"  导出时间：{manifest.get('created_at')}")
    print(f"  源机路径：{manifest.get('source_cwd')}")
    print(f"  源账号：{str(manifest.get('source_uid'))[:8]}…  目标机导入时会改写为本机登录账号")
    print(f"  会话数：{manifest.get('session_count')}")
    print(f"  工作区记忆：{'包含' if manifest.get('include_workspace') else '不包含'}")
    print()
    print("  会话列表（按最后活动倒序前 15 条）：")
    ss = sorted(manifest.get("sessions") or [], key=lambda s: int(s.get("last_activity_at") or 0), reverse=True)
    for s in ss[:15]:
        print(f"    {fmt_time(s.get('last_activity_at'))}  {clip(s.get('title') or '(无标题)', 46)}")
    if len(ss) > 15:
        print(f"    …等 {len(ss)} 个")
    return 0


# ---------------------------------------------------------------- 向导


def wizard() -> int:
    """无参数运行时的小白向导：全程输序号 + 拖文件，不打一个 flag"""
    print("=" * 70)
    print("WorkBuddy 跨设备迁移向导")
    print("=" * 70)
    print()
    print("  你现在在哪台电脑上？")
    print("    1. 要离开这台电脑 —— 把项目打包带走（导出）")
    print("    2. 到了新电脑 —— 把迁移包导入进来（导入）")
    print("    3. 查看一个迁移包里有什么")
    print("    0. 退出")
    try:
        choice = input("\n请选择（输入序号）: ").strip()
    except EOFError:
        print()
        print("非交互环境无法运行向导，请直接使用子命令：")
        print("  python3 scripts/migrate_project.py export   # 打包")
        print("  python3 scripts/migrate_project.py import 包文件.wbproj --cwd /新路径")
        return 1

    ns = argparse.Namespace(
        dir=None, intl=False, yes=True, cwd=None, out=None,
        no_workspace_memory=False, dry_run=False, on_conflict="ask",
        force=False, package=None, cmd="wizard",
    )
    if choice == "1":
        print()
        return do_export(ns)
    if choice == "2":
        print()
        return do_import(ns)
    if choice == "3":
        picked = pick_package()
        if picked is None:
            print("❌ 未选择迁移包")
            return 1
        return do_info(str(picked))
    print("已退出")
    return 0


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(
        description="WorkBuddy 跨设备项目迁移（export / import）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法:")[1] if "用法:" in __doc__ else None,
    )
    ap.add_argument("--dir", help="显式指定数据目录（优先级高于 --intl）")
    ap.add_argument("--intl", action="store_true", help="操作国际版（数据目录 ~/.workbuddy-ai）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")

    sub = ap.add_subparsers(dest="cmd")

    p_exp = sub.add_parser("export", help="导出一个项目的会话数据为迁移包")
    p_exp.add_argument("--cwd", help="项目路径（不传则交互式选择）")
    p_exp.add_argument("--out", help=f"输出包路径（默认：桌面/项目名-日期{PKG_EXT}）")
    p_exp.add_argument("--no-workspace-memory", action="store_true",
                       help="不打包 {项目}/.workbuddy/ 下的记忆与技能")
    p_exp.add_argument("--yes", action="store_true", help="非交互确认")
    p_exp.set_defaults(fn=cmd_export)

    p_imp = sub.add_parser("import", help="把迁移包导入本机（必须先关闭客户端）")
    p_imp.add_argument("package", nargs="?", default=None,
                       help=f"迁移包路径（{PKG_EXT}）；不传则自动发现桌面/下载/当前目录里的包")
    p_imp.add_argument("--cwd", help="项目在【本机】的新路径（目录必须已存在）")
    p_imp.add_argument("--dry-run", action="store_true", help="只预览导入计划，不写盘")
    p_imp.add_argument("--on-conflict", choices=["ask", "overwrite", "skip"], default="ask",
                       help="同 id 冲突策略：ask 询问（默认）/ overwrite 批量覆盖 / skip 跳过；无 TTY 时 ask 降级为 skip")
    p_imp.add_argument("--force", action="store_true", help="跳过客户端关闭检测")
    p_imp.add_argument("--yes", action="store_true", help="非交互确认")
    p_imp.set_defaults(fn=cmd_import)

    p_info = sub.add_parser("info", help="查看迁移包内容")
    p_info.add_argument("package")
    p_info.set_defaults(fn=cmd_info)

    ap.add_argument("--backups", action="store_true", help="列出本项目工具的备份")
    ap.add_argument("--rollback", metavar="TAG", help="回滚一次导入")
    ap.add_argument("--yes", action="store_true", help="跳过回滚确认")
    ap.add_argument("--force", action="store_true", help="回滚时跳过客户端关闭检测")

    args = ap.parse_args()

    if args.rollback:
        return do_rollback(args.rollback, args)
    if args.backups:
        ep = resolve_edition("intl" if args.intl else "domestic", resolve_home())
        print(f"{ep.label} 备份（{ep.backup_dir}）：")
        list_backups(ep)
        return 0
    if args.cmd is None:
        if is_interactive():
            return wizard()
        ap.print_help()
        return 1
    return args.fn(args)


def cmd_export(args):
    return do_export(args)


def cmd_import(args):
    return do_import(args)


def cmd_info(args):
    return do_info(args.package)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except sqlite3.Error as e:
        print(f"\n❌ 数据库错误：{e}")
        print("   若是导入过程出错，可用备份回滚： python3 scripts/migrate_project.py --backups")
        sys.exit(1)
    except RuntimeError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
