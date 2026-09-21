#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 单对话跨版本迁移工具（国内版 ⇄ 国际版）

与 migrate.py 的区别：
  - migrate.py        : 整个账号的数据合并（Session/Memory/Connector），同一版本内
  - migrate_session.py: 只迁移「一个对话」，且支持跨版本（国内 ⇄ 国际）

设计要点：
  1. 不修改 migrate.py，通过 import 复用它的函数与路径常量
  2. 默认 move（迁移后删除源），可选 copy
  3. 目标已存在时分级询问：
     - 硬冲突（ID 相同）   → 覆盖 / 不操作
     - 软冲突（标题相同）  → 覆盖 / 不覆盖（跳过）/ 不操作
     询问时展示最后活动时间、消息数、对话大小等差异，并给出建议
  4. 迁移前要求两个版本的客户端都已关闭（否则 WAL 未落盘 + 内存缓存会覆盖写入）

用法:
  python3 scripts/migrate_session.py                                  # 交互式向导
  python3 scripts/migrate_session.py --list                           # 列出国内版对话
  python3 scripts/migrate_session.py --list --from intl               # 列出国际版对话
  python3 scripts/migrate_session.py --from domestic --to intl \\
          --session-id <SESSION_ID>                                   # 迁移指定对话（默认 move，支持前缀）
  python3 scripts/migrate_session.py --from domestic --to intl --session-id <ID> --mode copy
  python3 scripts/migrate_session.py --from domestic --to intl --session-id <ID> --dry-run
  python3 scripts/migrate_session.py --backups                        # 列出备份
  python3 scripts/migrate_session.py --rollback <TAG>                 # 回滚

环境变量:
  WORKBUDDY_MIGRATE_HOME   覆盖 home 目录（测试用，指向临时 fixture）

平台兼容性:
  仅 Windows 实测通过（Windows 11 + Python 3.13）。
  macOS / Linux 的路径逻辑沿用 migrate.py 的跨平台实现（路径走 pathlib、
  进程检测在 Windows 用 tasklist、其他平台用 ps），但未经实测，欢迎反馈。
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
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# 复用原脚本：把 scripts/ 目录加入模块搜索路径
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    import migrate as legacy  # 原迁移脚本，复用其函数避免重复实现
except ImportError:
    legacy = None

# Windows 终端可能使用 GBK/CP936 编码，强制 UTF-8 避免 emoji 崩溃。
# 注意：migrate.py 顶层已经做过同样的包装，这里必须先判断编码再包装，
# 否则二次包装会让上一个 wrapper 被 GC 时关闭共享 buffer，导致输出全部失效。
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

EDITIONS = {"domestic": "国内版", "intl": "国际版"}
EDITION_DIR = {"domestic": ".workbuddy", "intl": ".workbuddy-ai"}

# 客户端进程名关键字（用于"必须关闭客户端"检测）
PROC_KEYWORDS = ("workbuddy", "codebuddy")

# 原脚本中依赖全局路径的变量，绑定 edition 时需要临时替换
# 说明：新版 migrate.py（国际版适配后）新增了 STORAGE_JSON / ACCOUNT_SNAPSHOT 两个
# 登录态相关的全局变量，它们同样指向"当前生效的数据目录"，必须一起绑定，
# 否则复用 legacy.get_current_user_id() 时会读到真实机器的登录态。
_LEGACY_PATH_VARS = (
    "WORKBUDDY_DIR",
    "DB_PATH",
    "MEMORY_DIR",
    "CONNECTORS_DIR",
    "TASKS_DIR",
    "BACKUP_DIR",
    "ACCOUNT_SNAPSHOT",
    "STORAGE_JSON",
)

# ---------------------------------------------------------------- 通用工具


def resolve_home() -> Path:
    """home 目录，支持环境变量覆盖（测试时指向临时 fixture）"""
    env = os.environ.get("WORKBUDDY_MIGRATE_HOME")
    return Path(env) if env else Path.home()


def fmt_size(n) -> str:
    """字节数格式化"""
    n = int(n or 0)
    if n <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(ms) -> str:
    """毫秒时间戳 → MM-DD HH:MM"""
    if not ms:
        return "-"
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


def fmt_delta(ms) -> str:
    """毫秒差 → 人类可读"""
    ms = abs(int(ms or 0))
    sec = ms // 1000
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分钟"
    if sec < 86400:
        h, m = divmod(sec // 60, 60)
        return f"{h} 小时 {m} 分钟"
    d, h = divmod(sec // 3600, 24)
    return f"{d} 天 {h} 小时"


def ask_yes_no(prompt: str) -> bool:
    """统一的 y/N 确认，管道输入耗尽时视为取消而非抛异常"""
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans == "y"


def dw(s) -> int:
    """字符串的显示宽度（中文等宽字符占 2 列）"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def pad(s, width, align="left") -> str:
    """按显示宽度补齐，保证中文表格对齐"""
    s = str(s)
    gap = max(0, width - dw(s))
    return s + " " * gap if align == "left" else " " * gap + s


def clip(s, width) -> str:
    """按显示宽度截断，超出部分用 … 表示"""
    s = str(s)
    if dw(s) <= width:
        return s
    out, acc = "", 0
    for ch in s:
        w = dw(ch)
        if acc + w > width - 1:
            break
        out += ch
        acc += w
    return out + "…"


def text_of(content) -> str:
    """从消息 content 中取出纯文本（content 可能是字符串或块数组）"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                # 正文块类型可能是 text / input_text / output_text，只要带 text 字段就取
                if "text" in c:
                    parts.append(str(c.get("text", "")))
                elif "content" in c:
                    parts.append(text_of(c.get("content")))
        return " ".join(p for p in parts if p)
    return ""


# ---------------------------------------------------------------- 进程检测


def find_running_clients():
    """检测正在运行的 WorkBuddy / CodeBuddy 客户端进程

    返回 (进程名列表, 检测是否可信)。检测命令失败（ps/tasklist 不存在或报错）时
    不能静默当成"没有客户端在跑"——那会让迁移在客户端持锁的情况下继续。
    """
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
            # comm= 只有进程名，macOS/Linux 上 Electron 应用的进程名常是包名
            # 或 "Electron"；args= 带完整命令行，能匹配到安装路径里的关键字。
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
        # 检测失败要如实上报，由调用方决定如何处理
        print(f"⚠️  客户端进程检测失败（{e}），无法确认客户端是否已关闭")
        return [], False
    return sorted(found), True


def _clients_closed_or_force(force=False) -> bool:
    """确认客户端已关闭；检测不可信时要求显式 --force 或手动确认"""
    running, trustworthy = find_running_clients()
    if not trustworthy and not force:
        print("   若已确认客户端全部退出，可用 --force 继续。")
        return False
    if running:
        return require_clients_closed(running, force)
    return True


def require_clients_closed(running=None, force=False) -> bool:
    """迁移前必须关闭两个版本客户端，否则返回 False

    running 可直接传入进程名列表（由 _clients_closed_or_force 检测好后传进来）；
    传 None 时自行检测（仅用于兼容直接调用的场景）。
    """
    if running is None:
        running, _ok = find_running_clients()
    if not running:
        return True

    print("=" * 70)
    print("❌ 检测到 WorkBuddy 客户端正在运行")
    print("=" * 70)
    for n in running:
        print(f"   • {n}")
    print()
    print("  迁移前必须关闭【两个版本】的 WorkBuddy 窗口，原因：")
    print("   1. 数据还在 WAL 日志里没落盘，会读到旧数据")
    print("   2. 客户端内存缓存会在退出时把你的修改覆盖回去")
    print("   3. 两个客户端同时持有数据库锁，写入可能失败")
    print()
    if not force:
        print("  请关闭所有 WorkBuddy 窗口后重新运行本命令。")
        print("  （确知风险可用 --force 跳过，但不建议）")
        return False

    print("  ⚠️  已用 --force 跳过检测，后果自负。")
    print()
    return True


# ---------------------------------------------------------------- 版本路径


@dataclass
class EditionPaths:
    """一个 WorkBuddy 版本的数据目录布局"""

    name: str
    root: Path
    db: Path
    memory_dir: Path
    connectors_dir: Path
    tasks_dir: Path
    projects_dir: Path
    sessions_dir: Path
    backup_dir: Path
    account_snapshot: Path

    @property
    def label(self) -> str:
        return EDITIONS.get(self.name, self.name)

    def exists(self) -> bool:
        return self.db.exists()

    @contextmanager
    def bind_legacy(self):
        """把本 edition 的路径写入原脚本的全局变量，从而复用它的函数

        原脚本（migrate.py）用模块级全局变量保存路径，这里临时替换后调用
        legacy.get_session_counts() 等函数即可作用于指定版本，退出时恢复。
        """
        if legacy is None:
            yield
            return
        saved = {k: getattr(legacy, k, None) for k in _LEGACY_PATH_VARS}
        try:
            legacy.WORKBUDDY_DIR = self.root
            legacy.DB_PATH = self.db
            legacy.MEMORY_DIR = self.memory_dir
            legacy.CONNECTORS_DIR = self.connectors_dir
            legacy.TASKS_DIR = self.tasks_dir
            legacy.BACKUP_DIR = self.backup_dir
            legacy.ACCOUNT_SNAPSHOT = self.account_snapshot
            # 平台 storage.json 不在数据目录内，fixture 里也不存在；
            # 一旦用 WORKBUDDY_MIGRATE_HOME 指向 fixture，就必须切断它，
            # 否则会把真实机器的登录态读进测试/迁移流程。
            if os.environ.get("WORKBUDDY_MIGRATE_HOME"):
                legacy.STORAGE_JSON = None
            yield
        finally:
            for k, v in saved.items():
                setattr(legacy, k, v)


def resolve_edition(name: str, home: Optional[Path] = None) -> EditionPaths:
    """解析某个版本的数据目录"""
    home = home or resolve_home()
    root = home / EDITION_DIR[name]
    return EditionPaths(
        name=name,
        root=root,
        db=root / "workbuddy.db",
        memory_dir=root / "memory",
        connectors_dir=root / "connectors",
        tasks_dir=root / "tasks",
        projects_dir=root / "projects",
        sessions_dir=root / "sessions",
        backup_dir=root / "migrate_backups",
        account_snapshot=root / "storage" / "skeleton" / "account-snapshot.json",
    )


def cwd_to_slug(cwd: str) -> str:
    """工作目录 → projects 子目录名

    C:\\Users\\alice\\WorkBuddy\\2026-09-10-14-49-02
      → c-Users-alice-WorkBuddy-2026-09-10-14-49-02
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
    """读写连接"""
    return sqlite3.connect(str(db))


def table_columns(conn, table):
    """读取表列名"""
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def fetch_session_row(ep: EditionPaths, session_id: str):
    """按 id（或 id 前缀）取一行 session，返回 dict；找不到返回 None"""
    conn = connect_ro(ep.db)
    if conn is None:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None and len(session_id) >= 4:
            # 支持前缀匹配
            rows = conn.execute(
                "SELECT * FROM sessions WHERE id LIKE ? ORDER BY last_activity_at DESC",
                (session_id + "%",),
            ).fetchall()
            if len(rows) == 1:
                row = rows[0]
            elif len(rows) > 1:
                print(f"❌ 前缀 {session_id} 匹配到多个对话：")
                for r in rows:
                    print(f"   {r['id']}  {str(r['title'])[:40]}")
                return None
        return dict(row) if row else None
    finally:
        conn.close()


def list_sessions(ep: EditionPaths, query=None):
    """列出某版本的全部对话（按最后活动时间倒序）"""
    conn = connect_ro(ep.db)
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY last_activity_at DESC")]
    finally:
        conn.close()

    if query:
        q = query.lower()
        rows = [
            r for r in rows
            if q in str(r.get("title") or "").lower()
            or q in str(r.get("cwd") or "").lower()
            or q in str(r.get("id") or "").lower()
        ]
    return rows


def get_current_uid(ep: EditionPaths):
    """推断某版本当前登录的 user_id

    优先级：
      1. {root}/storage/skeleton/account-snapshot.json → primary.uid（数据目录内，天然区分版本）
      2. 数据库中 session 数最多的 user_id（复用原脚本的统计函数）
      3. 原脚本的 get_current_user_id()（读平台 storage.json，兜底）
    """
    # 1. account-snapshot
    try:
        if ep.account_snapshot.exists():
            data = json.loads(ep.account_snapshot.read_text(encoding="utf-8"))
            uid = (data.get("primary") or {}).get("uid", "")
            if uid:
                return uid, "account-snapshot"
    except Exception:
        pass

    # 2. DB 中 session 数最多的 user_id（复用原脚本实现）
    if legacy is not None:
        try:
            with ep.bind_legacy():
                counts = legacy.get_session_counts()
            if counts:
                uid = max(counts.items(), key=lambda kv: kv[1])[0]
                if uid:
                    return uid, "db-majority"
        except Exception:
            pass

    # 3. 兜底：原脚本逻辑（platform storage.json）
    if legacy is not None:
        try:
            with ep.bind_legacy():
                uid = legacy.get_current_user_id()
            if uid:
                return uid, "legacy-storage-json"
        except Exception:
            pass

    return "", "unknown"


# ---------------------------------------------------------------- 对话统计


@dataclass
class SessionInfo:
    """一个对话的完整画像（用于展示与对比）"""

    session_id: str = ""
    edition: str = ""
    title: str = ""
    status: str = ""
    model: str = ""
    mode: str = ""
    cwd: str = ""
    user_id: str = ""
    created_at: int = 0
    updated_at: int = 0
    last_activity_at: int = 0
    # 文件相关
    files: list = field(default_factory=list)      # Path 列表
    file_size: int = 0
    line_count: int = 0
    # jsonl 内容统计（deep 模式）
    messages: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    first_ts: int = 0
    last_ts: int = 0
    last_user_msg: str = ""
    # usage
    usage_used: int = 0
    usage_size: int = 0
    has_tasks: bool = False

    @property
    def effective_last_ts(self) -> int:
        """最后活动时间：DB 字段与 jsonl 末条时间取较大者"""
        return max(int(self.last_activity_at or 0), int(self.last_ts or 0))

    @property
    def display_title(self) -> str:
        return self.title or "(无标题)"


def find_project_files(ep: EditionPaths, session_id: str):
    """定位对话正文文件：projects/{slug}/{sid}.jsonl 及其附属文件

    注意：这里的“文件”可能包含【目录】。WorkBuddy 会把体积较大的工具调用结果
    外溢到 projects/{slug}/{sid}/tool-results/*.txt，即与会话同名的目录。
    因此所有对返回值的拷贝 / 删除都必须走 copy_path() / remove_path()，
    不能直接用 shutil.copy2() 或 Path.unlink()，否则在 Windows 上会抛
    PermissionError / IsADirectoryError。
    """
    if not ep.projects_dir.exists():
        return []
    return sorted(ep.projects_dir.glob(f"*/{session_id}*"))


def path_size(p: Path) -> int:
    """统计占用空间：目录需递归累加内部文件，否则 tool-results 会被算成 0"""
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except OSError:
        return 0


def copy_path(src: Path, dst: Path):
    """复制文件或目录（目录走 copytree，目标已存在则先整体删除）"""
    if src.is_dir():
        if dst.exists():
            remove_path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src), str(dst))
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))


def remove_path(p: Path):
    """删除文件或目录；不存在则静默返回"""
    try:
        if p.is_dir():
            shutil.rmtree(str(p))
        elif p.exists():
            p.unlink()
    except FileNotFoundError:
        pass


def _abort_backup(bp: Path, msg: str):
    """备份中途失败：删掉半成品目录后再中止

    半截备份没有 meta.json，既不能回滚又会在 --backups 里留下一条找不到的记录。
    """
    shutil.rmtree(str(bp), ignore_errors=True)
    raise RuntimeError(msg)


def safe_copy(src: Path, dst: Path) -> bool:
    """复制文件或目录，成功返回 True；失败只打印可操作的提示，不抛原始 traceback"""
    try:
        copy_path(src, dst)
        return True
    except OSError as e:
        kind = "目录" if src.is_dir() else "文件"
        print(f"  ⚠️  无法复制{kind} {src.name}：{e}")
        print("     常见原因：WorkBuddy 客户端未完全退出、文件被其他进程占用，或权限不足。")
        return False


def scan_jsonl(path: Path):
    """扫描对话正文，统计消息数 / 工具调用数 / 首末时间 / 最后一条用户消息"""
    stats = {
        "lines": 0, "messages": 0, "user_messages": 0,
        "assistant_messages": 0, "tool_calls": 0,
        "first_ts": 0, "last_ts": 0, "last_user_msg": "",
    }
    if not path or not path.exists():
        return stats
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                stats["lines"] += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = obj.get("timestamp")
                if isinstance(ts, (int, float)):
                    ts = int(ts)
                    if not stats["first_ts"] or ts < stats["first_ts"]:
                        stats["first_ts"] = ts
                    if ts > stats["last_ts"]:
                        stats["last_ts"] = ts
                t = obj.get("type")
                if t == "message":
                    stats["messages"] += 1
                    role = obj.get("role")
                    if role == "user":
                        stats["user_messages"] += 1
                        txt = text_of(obj.get("content")).strip()
                        if txt:
                            stats["last_user_msg"] = txt
                    elif role == "assistant":
                        stats["assistant_messages"] += 1
                elif t == "function_call":
                    stats["tool_calls"] += 1
    except Exception:
        pass
    return stats


def count_lines_fast(path: Path) -> int:
    """只数行数，不解析 JSON（列表展示用，快）"""
    if not path or not path.exists():
        return 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def collect_info(ep: EditionPaths, row: dict, deep=True) -> SessionInfo:
    """把 DB 行 + 文件统计组装成 SessionInfo"""
    sid = row.get("id", "")
    files = find_project_files(ep, sid)
    jsonl = next((f for f in files if f.suffix == ".jsonl"), None)

    info = SessionInfo(
        session_id=sid,
        edition=ep.name,
        title=(row.get("custom_title") or row.get("title") or "").strip(),
        status=row.get("status") or "",
        model=row.get("model") or "",
        mode=row.get("mode") or "",
        cwd=row.get("cwd") or "",
        user_id=row.get("user_id") or "",
        created_at=int(row.get("created_at") or 0),
        updated_at=int(row.get("updated_at") or 0),
        last_activity_at=int(row.get("last_activity_at") or 0),
        files=files,
        file_size=sum(path_size(f) for f in files if f.exists()),
        has_tasks=(ep.tasks_dir / sid).exists() if ep.tasks_dir else False,
    )

    # usage
    conn = connect_ro(ep.db)
    if conn is not None:
        try:
            r = conn.execute(
                "SELECT used, size FROM session_usage WHERE session_id = ?", (sid,)
            ).fetchone()
            if r:
                info.usage_used = int(r[0] or 0)
                info.usage_size = int(r[1] or 0)
        except Exception:
            pass
        finally:
            conn.close()

    if deep and jsonl:
        st = scan_jsonl(jsonl)
        info.messages = st["messages"]
        info.user_messages = st["user_messages"]
        info.assistant_messages = st["assistant_messages"]
        info.tool_calls = st["tool_calls"]
        info.first_ts = st["first_ts"]
        info.last_ts = st["last_ts"]
        info.last_user_msg = st["last_user_msg"]
        info.line_count = st["lines"]
    elif jsonl:
        info.line_count = count_lines_fast(jsonl)

    return info


# ---------------------------------------------------------------- 差异对比


def build_diff_rows(src: SessionInfo, dst: SessionInfo):
    """构造对比表的行数据：(标签, 目标值, 源值, 是否重点)"""
    rows = [
        ("标题", clip(dst.display_title, 30), clip(src.display_title, 30), False),
        ("状态", dst.status or "-", src.status or "-", False),
        ("模型", dst.model or "-", src.model or "-", False),
        ("创建时间", fmt_time(dst.created_at), fmt_time(src.created_at), False),
        ("★ 最后活动", fmt_time(dst.effective_last_ts), fmt_time(src.effective_last_ts), True),
        (
            "★ 消息数",
            f"{dst.messages} 条（我 {dst.user_messages} / AI {dst.assistant_messages}）"
            if dst.messages else "-",
            f"{src.messages} 条（我 {src.user_messages} / AI {src.assistant_messages}）"
            if src.messages else "-",
            True,
        ),
        (
            "★ 对话大小",
            f"{fmt_size(dst.file_size)} · {dst.line_count} 行" if dst.file_size else "-",
            f"{fmt_size(src.file_size)} · {src.line_count} 行" if src.file_size else "-",
            True,
        ),
        ("工具调用", f"{dst.tool_calls} 次" if dst.tool_calls else "-",
                    f"{src.tool_calls} 次" if src.tool_calls else "-", False),
        ("token 用量", f"{dst.usage_used:,}" if dst.usage_used else "-",
                     f"{src.usage_used:,}" if src.usage_used else "-", False),
        ("工作目录", clip(dst.cwd, 30) or "-", clip(src.cwd, 30) or "-", False),
    ]

    # 最后一条用户消息（帮助识别对话内容）
    if src.last_user_msg or dst.last_user_msg:
        rows.append((
            "最后提问",
            clip(dst.last_user_msg.replace("\n", " "), 40) or "-",
            clip(src.last_user_msg.replace("\n", " "), 40) or "-",
            False,
        ))
    return rows


def build_verdict(src: SessionInfo, dst: SessionInfo):
    """根据差异生成结论与建议"""
    parts = []
    newer_src = None

    dts, sts = dst.effective_last_ts, src.effective_last_ts
    if dts and sts:
        delta = sts - dts
        if abs(delta) < 60_000:
            parts.append("两者最后活动时间几乎相同")
        elif delta > 0:
            parts.append(f"源比目标新 {fmt_delta(delta)}")
            newer_src = True
        else:
            parts.append(f"目标比源新 {fmt_delta(delta)}")
            newer_src = False

    md = src.messages - dst.messages
    if md:
        parts.append(f"消息{'多' if md > 0 else '少'} {abs(md)} 条")

    if dst.file_size and src.file_size:
        ratio = src.file_size / dst.file_size
        if ratio >= 100:
            parts.append(f"内容远超目标（约 {ratio:.0f} 倍）")
        elif ratio >= 1.2 or ratio <= 0.8:
            parts.append(f"内容约为目标的 {ratio:.1f} 倍")

    if newer_src is True and md > 0:
        advice = "建议：覆盖（源更新且更完整）"
    elif newer_src is False:
        advice = "⚠️ 目标比源新，覆盖会丢失目标中更新的内容"
    elif newer_src is None:
        advice = "两者接近，请自行判断"
    else:
        advice = "请自行判断"

    return ("，".join(parts) if parts else "无显著差异"), advice


def render_diff(src: SessionInfo, dst: SessionInfo, kind: str) -> str:
    """渲染对比表"""
    rows = build_diff_rows(src, dst)
    verdict, advice = build_verdict(src, dst)

    w1, w2, w3 = 14, 34, 34
    lines = []
    lines.append(f"  {pad('指标', w1)}{pad('目标现有（将被覆盖）', w2)}{pad('源（将写入）', w3)}")
    lines.append("  " + "─" * (w1 + w2 + w3))
    for label, d, s, _hot in rows:
        lines.append(f"  {pad(label, w1)}{pad(d, w2)}{pad(s, w3)}")
    lines.append("  " + "─" * (w1 + w2 + w3))
    if verdict != "无显著差异":
        lines.append(f"  → {verdict}")
    lines.append(f"  → {advice}")
    return "\n".join(lines)


def _files_desc(files) -> str:
    """正文文件描述：区分普通文件与 tool-results 目录，避免用户误以为多出异常项"""
    if not files:
        return "-"
    dirs = [f for f in files if f.is_dir()]
    n = len(files) - len(dirs)
    parts = []
    if n:
        parts.append(f"{n} 个文件")
    if dirs:
        parts.append(f"{len(dirs)} 个目录（tool-results）")
    return " + ".join(parts)


def render_single(info: SessionInfo) -> str:
    """渲染单个对话的摘要（无冲突时使用）"""
    w1 = 14
    lines = [
        f"  {pad('标题', w1)}{clip(info.display_title, 50)}",
        f"  {pad('状态', w1)}{info.status or '-'}",
        f"  {pad('创建时间', w1)}{fmt_time(info.created_at)}",
        f"  {pad('最后活动', w1)}{fmt_time(info.effective_last_ts)}",
        f"  {pad('消息数', w1)}{info.messages} 条（我 {info.user_messages} / AI {info.assistant_messages}）"
        if info.messages else f"  {pad('消息数', w1)}-",
        f"  {pad('对话大小', w1)}{fmt_size(info.file_size)} · {info.line_count} 行",
        f"  {pad('工具调用', w1)}{info.tool_calls} 次",
        f"  {pad('工作目录', w1)}{clip(info.cwd, 50)}",
        f"  {pad('正文文件', w1)}{_files_desc(info.files)}",
    ]
    if info.last_user_msg:
        lines.append(f"  {pad('最后提问', w1)}{clip(info.last_user_msg.replace(chr(10), ' '), 50)}")
    if info.has_tasks:
        lines.append(f"  {pad('任务数据', w1)}有（tasks/{info.session_id[:8]}…）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 备份 / 回滚


def snapshot_db(src: Path, dst: Path):
    """用 sqlite3 backup API 复制数据库（能正确包含 WAL 中未落盘的数据）"""
    try:
        s = sqlite3.connect(str(src))
        d = sqlite3.connect(str(dst))
        with d:
            s.backup(d)
        s.close()
        d.close()
        return True
    except Exception as e:
        print(f"  ⚠️  数据库快照失败（不影响精确回滚）: {e}")
        return False


def create_backup(src_ep, dst_ep, src_row, dst_row, session_id, mode,
                  backup_root=None, extra_row=None) -> Path:
    """创建单对话迁移的备份（精确到行 + 文件，另附整库快照兜底）

    extra_row: 软冲突时被覆盖掉的那条目标记录（id 与源不同），单独备份以便回滚
    """
    root = Path(backup_root) if backup_root else dst_ep.backup_dir
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    tag = f"{ts}_{src_ep.name}2{dst_ep.name}_{session_id[:8]}"
    bp = root / tag
    bp.mkdir(parents=True, exist_ok=True)

    meta = {
        "version": 2,
        "kind": "session",
        "created_at": datetime.now().isoformat(),
        "tag": tag,
        "mode": mode,
        "from_edition": src_ep.name,
        "to_edition": dst_ep.name,
        "session_id": session_id,
        "source_user_id": src_row.get("user_id", "") if src_row else "",
        "target_user_id": "",
        "source_deleted": False,
        "target_existed": dst_row is not None,
        "target_overwritten": False,
        "src_files": [],
        "dst_files": [],
        "copied_to": [],
        "tasks_copied": False,
    }

    # 源行 + 源文件
    (bp / "src_session.json").write_text(
        json.dumps(src_row, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    src_files = find_project_files(src_ep, session_id)
    if src_files:
        (bp / "files").mkdir(exist_ok=True)
        for f in src_files:
            if not f.exists():
                continue
            if not safe_copy(f, bp / "files" / f.name):
                _abort_backup(bp, "备份不完整，已中止迁移（未改动任何数据）")
            meta["src_files"].append({"name": f.name, "path": str(f), "is_dir": f.is_dir()})

    # 目标原有行 + 原文件（覆盖场景）
    if dst_row:
        (bp / "dst_session.json").write_text(
            json.dumps(dst_row, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        dst_files = find_project_files(dst_ep, session_id)
        if dst_files:
            (bp / "dst_files").mkdir(exist_ok=True)
            for f in dst_files:
                if not f.exists():
                    continue
                if not safe_copy(f, bp / "dst_files" / f.name):
                    _abort_backup(bp, "备份不完整，已中止迁移（未改动任何数据）")
                meta["dst_files"].append({"name": f.name, "path": str(f), "is_dir": f.is_dir()})

    # 软冲突：被覆盖的那条目标记录（id 与源不同）
    if extra_row:
        (bp / "override_session.json").write_text(
            json.dumps(extra_row, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        ov_files = find_project_files(dst_ep, extra_row.get("id", ""))
        if ov_files:
            (bp / "override_files").mkdir(exist_ok=True)
            for f in ov_files:
                if not f.exists():
                    continue
                if not safe_copy(f, bp / "override_files" / f.name):
                    _abort_backup(bp, "备份不完整，已中止迁移（未改动任何数据）")
                meta.setdefault("override_files", []).append(
                    {"name": f.name, "path": str(f), "is_dir": f.is_dir()}
                )
        meta["override_id"] = extra_row.get("id", "")

    # usage 行
    # 软冲突覆盖时还会删掉目标旧对话的 usage，必须一并备份，否则回滚后丢失
    usage_targets = [("src_usage.json", src_ep, session_id), ("dst_usage.json", dst_ep, session_id)]
    if extra_row and extra_row.get("id"):
        usage_targets.append(("override_usage.json", dst_ep, extra_row["id"]))
    for tag_, ep, uid_sid in usage_targets:
        conn = connect_ro(ep.db)
        if conn is None:
            continue
        try:
            r = conn.execute("SELECT * FROM session_usage WHERE session_id = ?", (uid_sid,)).fetchone()
            if r:
                cols = table_columns(conn, "session_usage")
                (bp / tag_).write_text(
                    json.dumps(dict(zip(cols, r)), ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
        except Exception:
            pass
        finally:
            conn.close()

    # 整库快照（兜底）；同版本迁移时两库是同一个，只快照一次
    if src_ep.db.exists():
        snapshot_db(src_ep.db, bp / "snapshot_src.db")
    if dst_ep.db.exists() and dst_ep.db != src_ep.db:
        snapshot_db(dst_ep.db, bp / "snapshot_dst.db")

    (bp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return bp


def load_backup(tag, backup_root=None):
    """按 tag 查找备份目录"""
    roots = []
    if backup_root:
        roots.append(Path(backup_root))
    for name in ("domestic", "intl"):
        roots.append(resolve_edition(name).backup_dir)
    for r in roots:
        p = r / tag
        if (p / "meta.json").exists():
            return p
    # 前缀模糊匹配
    for r in roots:
        if not r.exists():
            continue
        hits = [d for d in r.iterdir() if d.is_dir() and d.name.startswith(tag)]
        if len(hits) == 1 and (hits[0] / "meta.json").exists():
            return hits[0]
    return None


def rollback(tag, backup_root=None, full=False, assume_yes=False):
    """回滚单对话迁移"""
    bp = load_backup(tag, backup_root)
    if bp is None:
        print(f"❌ 找不到备份: {tag}")
        print("   可用 --backups 查看")
        sys.exit(1)

    meta = json.loads((bp / "meta.json").read_text(encoding="utf-8"))
    src_ep = resolve_edition(meta["from_edition"])
    dst_ep = resolve_edition(meta["to_edition"])
    sid = meta["session_id"]

    print("=" * 70)
    print("单对话迁移回滚")
    print("=" * 70)
    print(f"\n  备份标签:   {meta.get('tag', tag)}")
    print(f"  对话:       {sid}")
    print(f"  方向:       {src_ep.label} → {dst_ep.label}")
    print(f"  模式:       {meta.get('mode')}")
    print(f"  源已删除:   {'是' if meta.get('source_deleted') else '否'}")
    print(f"  目标被覆盖: {'是' if meta.get('target_overwritten') else '否'}")
    print()

    if full:
        print("  ⚠️  整库恢复模式：会把两个数据库整体还原到迁移前，")
        print("      迁移之后产生的新对话/新数据会一并丢失！")
    if not assume_yes and not ask_yes_no("  确认回滚？(y/N): "):
        print("已取消")
        return

    if full:
        for snapshot, ep in (("snapshot_src.db", src_ep), ("snapshot_dst.db", dst_ep)):
            f = bp / snapshot
            if f.exists():
                shutil.copy2(str(f), str(ep.db))
                print(f"  ✅ 已整库恢复 {ep.label}")
        print("\n  ✅ 整库回滚完成")
        return

    # 同版本迁移：回滚就是把 user_id 改回去
    if meta.get("kind") == "session_intra":
        conn = connect_rw(dst_ep.db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute(
                "UPDATE sessions SET user_id = ? WHERE id = ?",
                (meta.get("source_user_id", ""), sid),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            n = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ? AND user_id = ?",
                (sid, meta.get("source_user_id", "")),
            ).fetchone()[0]
            print(f"  ✅ 已把 user_id 改回 {str(meta.get('source_user_id', ''))[:12]}…（验证 {n} 行）")
        finally:
            conn.close()
        print("\n  ✅ 回滚完成")
        return

    # 同版本克隆：只删掉克隆出来的那条新对话，绝对不能碰原始对话
    # （通用回滚会按 session_id 删行，而这里的 session_id 就是原始对话的 id，
    #   走通用分支会把原对话一起删掉，所以必须单独处理）
    if meta.get("kind") == "session_clone":
        new_sid = meta.get("new_session_id", "")
        if not new_sid:
            print("  ⚠️  备份缺少 new_session_id，无法定位克隆产物，跳过")
            return
        conn = connect_rw(dst_ep.db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("DELETE FROM sessions WHERE id = ?", (new_sid,))
            conn.execute("DELETE FROM session_usage WHERE session_id = ?", (new_sid,))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            print(f"  ✅ 已删除克隆出的对话 {new_sid[:8]}…")
        finally:
            conn.close()
        for rel in meta.get("copied_to", []):
            p = Path(rel)
            if p.exists():
                remove_path(p)
                print(f"  ✅ 已删除 {p.name}")
        if meta.get("cloned_tasks"):
            td = dst_ep.tasks_dir / new_sid
            if td.exists():
                shutil.rmtree(str(td))
                print("  ✅ 已删除克隆的任务数据")
        print("\n  ✅ 回滚完成（原始对话未受影响）")
        return

    # 精确回滚
    # 1) 目标侧：删除写入的行与文件，恢复被覆盖的原行/原文件
    conn = connect_rw(dst_ep.db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if meta.get("target_existed") and (bp / "dst_session.json").exists():
            row = json.loads((bp / "dst_session.json").read_text(encoding="utf-8"))
            cols = [c for c in row.keys() if c in table_columns(conn, "sessions")]
            conn.execute(
                f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row[c] for c in cols],
            )
            print("  ✅ 已恢复目标原有 session 行")
        else:
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            print("  ✅ 已删除目标侧的 session 行")

        conn.execute("DELETE FROM session_usage WHERE session_id = ?", (sid,))
        if (bp / "dst_usage.json").exists():
            u = json.loads((bp / "dst_usage.json").read_text(encoding="utf-8"))
            cols = [c for c in u.keys() if c in table_columns(conn, "session_usage")]
            if cols:
                conn.execute(
                    f"INSERT OR REPLACE INTO session_usage ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [u[c] for c in cols],
                )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    # 软冲突中被删掉的旧对话：整条恢复（行 + 文件）
    if meta.get("override_deleted") and (bp / "override_session.json").exists():
        conn = connect_rw(dst_ep.db)
        try:
            row = json.loads((bp / "override_session.json").read_text(encoding="utf-8"))
            cols = [c for c in row.keys() if c in table_columns(conn, "sessions")]
            conn.execute(
                f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row[c] for c in cols],
            )
            conn.commit()
            print(f"  ✅ 已恢复目标中被覆盖的旧对话 {str(row.get('id'))[:8]}…")
            # usage 行同样要恢复：迁移时 DELETE 过，不还原的话用量统计会凭空消失
            uf = bp / "override_usage.json"
            if uf.exists():
                u = json.loads(uf.read_text(encoding="utf-8"))
                ucols = [c for c in u.keys() if c in table_columns(conn, "session_usage")]
                if ucols:
                    conn.execute(
                        f"INSERT OR REPLACE INTO session_usage ({','.join(ucols)}) "
                        f"VALUES ({','.join('?' * len(ucols))})",
                        [u[c] for c in ucols],
                    )
                    conn.commit()
                    print(f"  ✅ 已恢复其 session_usage")
        finally:
            conn.close()
        for f in meta.get("override_files", []):
            srcf = bp / "override_files" / f["name"]
            if srcf.exists():
                if safe_copy(srcf, Path(f["path"])):
                    print(f"  ✅ 已还原 {f['name']}")

    # 删除复制过去的文件；若之前覆盖了目标文件则还原
    for rel in meta.get("copied_to", []):
        p = Path(rel)
        if p.exists():
            remove_path(p)  # 可能是复制过去的 tool-results 目录
            print(f"  ✅ 已删除 {p.name}")
    for f in meta.get("dst_files", []):
        srcf = bp / "dst_files" / f["name"]
        if srcf.exists():
            if safe_copy(srcf, Path(f["path"])):
                print(f"  ✅ 已还原 {f['name']}")

    # 2) 源侧：若迁移时删除了源，则插回
    if meta.get("source_deleted"):
        conn = connect_rw(src_ep.db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = json.loads((bp / "src_session.json").read_text(encoding="utf-8"))
            cols = [c for c in row.keys() if c in table_columns(conn, "sessions")]
            conn.execute(
                f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row[c] for c in cols],
            )
            if (bp / "src_usage.json").exists():
                u = json.loads((bp / "src_usage.json").read_text(encoding="utf-8"))
                ucols = [c for c in u.keys() if c in table_columns(conn, "session_usage")]
                if ucols:
                    conn.execute(
                        f"INSERT OR REPLACE INTO session_usage ({','.join(ucols)}) VALUES ({','.join('?' * len(ucols))})",
                        [u[c] for c in ucols],
                    )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            print("  ✅ 已把 session 行插回源版本")
        finally:
            conn.close()

        for f in meta.get("src_files", []):
            srcf = bp / "files" / f["name"]
            if srcf.exists():
                if safe_copy(srcf, Path(f["path"])):
                    print(f"  ✅ 已还原源文件 {f['name']}")

    print("\n  ✅ 精确回滚完成（未影响其他对话）")


def list_backups(backup_root=None):
    """列出所有单对话迁移备份"""
    found = []
    roots = [Path(backup_root)] if backup_root else [resolve_edition(n).backup_dir for n in ("domestic", "intl")]
    for r in roots:
        if not r.exists():
            continue
        for d in sorted(r.iterdir(), reverse=True):
            mf = d / "meta.json"
            if not (d.is_dir() and mf.exists()):
                continue
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
                if not str(meta.get("kind", "")).startswith("session"):
                    continue
                found.append((d.name, meta))
            except Exception:
                continue

    if not found:
        print("（暂无单对话迁移备份）")
        return
    print("=" * 70)
    print("单对话迁移备份")
    print("=" * 70)
    for tag, meta in found:
        print(f"\n  📦 {tag}")
        print(f"     {EDITIONS.get(meta.get('from_edition'), '?')} → "
              f"{EDITIONS.get(meta.get('to_edition'), '?')}  "
              f"{meta.get('mode', '?')}  session={str(meta.get('session_id'))[:8]}…")
        print(f"     回滚: python3 scripts/migrate_session.py --rollback {tag}")


# ---------------------------------------------------------------- 冲突询问


def ask_conflict(src: SessionInfo, dst: SessionInfo, kind: str, assume_yes=False, default="ask"):
    """冲突询问

    kind = "id"    硬冲突：ID 相同 → 覆盖 / 不操作
    kind = "title" 软冲突：标题相同、ID 不同 → 覆盖 / 不覆盖 / 不操作

    返回 "overwrite" | "skip" | "cancel"
    """
    if kind == "id":
        title = "⚠️  目标版本已存在【相同 ID】的对话"
        reason = "原因：两个版本中存在完全相同的 session id（通常是之前迁移过一次）。"
    else:
        title = "⚠️  目标版本已存在【标题相同】但 ID 不同的对话"
        reason = ("原因：标题一致但 id 不同，很可能是同一段对话被迁移过一次，\n"
                  "       再次迁移会在客户端里出现两条看起来一样的对话。")

    print()
    print("=" * 70)
    print(title)
    print("=" * 70)
    print(f"\n  {reason}\n")
    print(render_diff(src, dst, kind))
    print()

    if assume_yes:
        # 非交互模式：无法向用户提问，一律保守处理
        if default == "overwrite":
            print("  → --on-conflict=overwrite，直接覆盖")
            return "overwrite"
        if default == "newer":
            return "overwrite" if src.effective_last_ts >= dst.effective_last_ts else "skip"
        # ask / skip：没有 TTY 可供询问，降级为跳过（不覆盖用户已有数据）
        print("  → 非交互模式无法询问（--on-conflict=ask 已降级为 skip），跳过该对话")
        return "skip"

    if kind == "id":
        prompt = "  请确认 [y] 覆盖 / [n] 不操作（取消）: "
        opts = {"y": "overwrite", "n": "cancel", "": "cancel"}
    else:
        prompt = "  请确认 [y] 覆盖 / [s] 不覆盖（跳过该对话） / [n] 不操作（取消）: "
        opts = {"y": "overwrite", "s": "skip", "n": "cancel", "": "skip"}

    while True:
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "cancel"
        if ans in opts:
            return opts[ans]
        print("  请输入 y / s / n")


# ---------------------------------------------------------------- 迁移执行


def do_migrate(src_ep, dst_ep, sid, mode="move", on_conflict="ask",
               dry_run=False, assume_yes=False, target_uid=None, backup_root=None):
    """执行单对话迁移"""
    src_row = fetch_session_row(src_ep, sid)
    if not src_row:
        print(f"❌ 源版本（{src_ep.label}）中找不到对话: {sid}")
        sys.exit(1)
    sid = src_row["id"]

    if src_ep.name == dst_ep.name and src_ep.db == dst_ep.db:
        return _migrate_intra(src_ep, src_row, target_uid, dry_run, assume_yes, mode)

    return _migrate_cross(src_ep, dst_ep, src_row, mode, on_conflict,
                          dry_run, assume_yes, target_uid, backup_root)


def _migrate_intra(ep, src_row, target_uid, dry_run, assume_yes, mode="move"):
    """同版本迁移，三种语义：

    1) copy（--mode copy）：无论源属于哪个账号，都克隆出一条新对话，源保持不动
    2) move + 源属其他账号：改 sessions.user_id 完成归属转移（源账号看不到该对话）
    3) move + 源已属目标账号：没有归属可改，退化为同版本克隆

    以前 mode 参数收下却没用：跨账号 + copy 时走的是 UPDATE（归属转移），
    源账号会失去这条对话，与帮助文本「copy=保留源」直接矛盾。
    """
    if not target_uid:
        target_uid, _src = get_current_uid(ep)
    if not target_uid:
        print("❌ 无法确定目标 user_id，请加 --target-uid 指定")
        sys.exit(1)

    sid = src_row["id"]
    old_uid = src_row.get("user_id", "")
    print("=" * 70)
    print(f"同版本迁移（{ep.label}）")
    print("=" * 70)
    print(f"\n  对话: {str(src_row.get('title'))[:50]}")
    print(f"  {old_uid[:12]}… → {target_uid[:12]}…")
    if mode == "copy":
        # copy 的核心承诺是"源不动"：跨账号时也一样，克隆到目标账号而不是改归属
        print("\n  ℹ️  --mode copy：保留源对话，改为克隆一份归属到目标账号")
        return _clone_intra(ep, src_row, target_uid, dry_run, assume_yes)
    if old_uid == target_uid:
        # 归属无需变更：真正的诉求是复制一份，交给克隆逻辑处理
        print("\n  ℹ️  该对话已属于当前账号，无归属可改 → 按「同版本复制」处理")
        return _clone_intra(ep, src_row, target_uid, dry_run, assume_yes)
    if dry_run:
        print("\n  [dry-run] UPDATE sessions SET user_id=? WHERE id=?")
        return
    if not assume_yes and not ask_yes_no("\n确认迁移？(y/N): "):
        print("已取消")
        return

    # 同版本迁移同样必须备份，否则改完 user_id 无法回滚
    bp = create_backup(ep, ep, src_row, None, sid, "intra", None)
    meta = json.loads((bp / "meta.json").read_text(encoding="utf-8"))
    meta["kind"] = "session_intra"
    meta["source_user_id"] = old_uid
    meta["target_user_id"] = target_uid
    (bp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  📦 备份: {bp.name}")

    conn = connect_rw(ep.db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("UPDATE sessions SET user_id = ? WHERE id = ?", (target_uid, sid))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        n = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ? AND user_id = ?", (sid, target_uid)
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"\n  ✅ 完成（验证命中 {n} 行）")
    print(f"  回滚: python3 scripts/migrate_session.py --rollback {bp.name}")


def _rewrite_session_id(path: Path, old_sid: str, new_sid: str):
    """把正文 jsonl 里内嵌的 sessionId 换成新 id

    只替换 "sessionId":"<old>" 这种字段值：会话正文（text）里同样会出现这个 id 串
    （日志、路径、引用等），整行 replace 会把用户可见的消息内容一起改坏。

    逐行流式处理，避免把几 MB 的正文整个读进内存。
    """
    pat = re.compile(r'("sessionId"\s*:\s*")' + re.escape(old_sid) + r'(")')
    tmp = path.with_name(path.name + ".tmp")
    # newline="" 保证 \r\n 原样保留，不被通用换行模式改写
    with open(path, encoding="utf-8", errors="replace", newline="") as fin, \
            open(tmp, "w", encoding="utf-8", newline="") as fout:
        for line in fin:
            fout.write(pat.sub(lambda m: m.group(1) + new_sid + m.group(2), line))
    tmp.replace(path)


def _clone_intra(ep, src_row, target_uid, dry_run, assume_yes):
    """同版本内克隆一条对话：新 id + 复制正文（改写 sessionId）+ 任务数据 + 工具结果"""
    sid = src_row["id"]
    new_sid = str(uuid.uuid4())
    old_title = (src_row.get("custom_title") or src_row.get("title") or "").strip()
    new_title = old_title if "（副本）" in old_title else f"{old_title}（副本）"

    src_info = collect_info(ep, src_row, deep=True)

    print(f"\n  模式:      复制（同版本内克隆出新对话）")
    print(f"  新对话 id: {new_sid}")
    print(f"  新标题:    {new_title or '(无标题)'}")
    print()
    print("  【源对话】")
    print(render_single(src_info))

    if dry_run:
        print("\n  [dry-run] 将执行：")
        print(f"   1. INSERT sessions / session_usage（新 id {new_sid[:8]}…）")
        print(f"   2. 复制 {len(src_info.files)} 项正文，"
              f"共 {fmt_size(src_info.file_size)}（文件名换成新 id，正文内 sessionId 一并改写）")
        if src_info.has_tasks:
            print("   3. 复制任务数据")
        return

    if not assume_yes and not ask_yes_no(f"\n确认在{ep.label}内复制出一份？(y/N): "):
        print("已取消")
        return

    # 备份仍以源 id 为准；kind=session_clone 让回滚只删克隆产物、不动原始对话
    bp = create_backup(ep, ep, src_row, None, sid, "copy", None)
    meta = json.loads((bp / "meta.json").read_text(encoding="utf-8"))
    meta["kind"] = "session_clone"
    meta["new_session_id"] = new_sid
    meta["source_user_id"] = src_row.get("user_id", "")
    meta["target_user_id"] = target_uid
    meta["cloned_tasks"] = False
    meta["copied_to"] = []   # 回滚按此列表删除克隆产物
    (bp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  📦 备份: {bp.name}")

    conn = connect_rw(ep.db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cols = table_columns(conn, "sessions")
        row = dict(src_row)
        row["id"] = new_sid
        row["user_id"] = target_uid
        # 标题打上副本标记，避免两条同名对话分不清
        if "custom_title" in cols:
            row["custom_title"] = new_title
        elif "title" in cols:
            row["title"] = new_title
        ks = [k for k in row if k in cols]
        conn.execute(
            f"INSERT INTO sessions ({','.join(ks)}) VALUES ({','.join('?' * len(ks))})",
            [row[k] for k in ks],
        )

        cur = conn.execute("SELECT * FROM session_usage WHERE session_id = ?", (sid,))
        u = cur.fetchone()
        if u:
            ucols = [d[0] for d in cur.description]
            urow = dict(zip(ucols, u))
            urow["session_id"] = new_sid
            ks2 = [k for k in urow if k in ucols]
            conn.execute(
                f"INSERT OR REPLACE INTO session_usage ({','.join(ks2)}) "
                f"VALUES ({','.join('?' * len(ks2))})",
                [urow[k] for k in ks2],
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        n = conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (new_sid,)).fetchone()[0]
    finally:
        conn.close()
    print(f"\n  ✅ 新 session 行已写入（验证命中 {n} 行）")

    # 正文文件：文件名换成新 id；jsonl 内部的 sessionId 也要跟着换
    print("\n📄 复制对话正文...")
    for f in src_info.files:
        if not f.exists():
            continue
        target = f.parent / f.name.replace(sid, new_sid)
        if not safe_copy(f, target):
            raise RuntimeError("复制对话正文失败，已中止（请执行回滚）")
        # .jsonl 是正文；.meta.json / .file-rollback.ndjson 也可能内嵌 sessionId，
        # 一并走同一套（只改 "sessionId":"..." 字段值）的替换
        if target.is_file() and target.suffix in (".jsonl", ".json", ".ndjson"):
            _rewrite_session_id(target, sid, new_sid)
        meta["copied_to"].append(str(target))
        mark = "目录" if target.is_dir() else "文件"
        print(f"  ✅ {target.name} [{mark}] ({fmt_size(path_size(target))})")

    # 校验：副本里不应再出现旧的 session id（残留说明有字段没被改写，客户端可能串台）
    leftovers = []
    for rel in meta.get("copied_to", []):
        p = Path(rel)
        if p.is_file() and p.suffix in (".jsonl", ".json", ".ndjson"):
            try:
                if sid in p.read_text(encoding="utf-8", errors="replace"):
                    leftovers.append(p.name)
            except Exception:
                pass
    if leftovers:
        print(f"  ⚠️  以下文件内仍残留旧 session id，请人工确认：{', '.join(leftovers)}")

    src_tasks = ep.tasks_dir / sid
    if src_tasks.exists():
        dst_tasks = ep.tasks_dir / new_sid
        if dst_tasks.exists():
            shutil.rmtree(str(dst_tasks))
        shutil.copytree(str(src_tasks), str(dst_tasks))
        meta["cloned_tasks"] = True
        print("  ✅ 任务数据已复制")

    (bp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print("✅ 同版本复制完成")
    print("=" * 70)
    print(f"\n  原标题: {clip(old_title or '(无标题)', 40)}")
    print(f"  新对话: {clip(new_title or '(无标题)', 40)}")
    print(f"  新 id:  {new_sid}")
    print(f"  备份:   {bp}")
    print(f"  回滚:   python3 scripts/migrate_session.py --rollback {bp.name}")
    print("\n  现在可以在客户端里看到这条副本（原对话保持不变）。")


def _migrate_cross(src_ep, dst_ep, src_row, mode, on_conflict,
                   dry_run, assume_yes, target_uid, backup_root):
    """跨版本迁移：源库读出 → 目标库插入 → 复制正文 → (move) 删除源"""
    sid = src_row["id"]

    # 目标 user_id
    if not target_uid:
        target_uid, how = get_current_uid(dst_ep)
        if target_uid:
            print(f"  目标账号: {target_uid[:12]}…（来源: {how}）")
    if not target_uid:
        print("❌ 无法确定目标版本的 user_id，请用 --target-uid 指定")
        sys.exit(1)

    src_info = collect_info(src_ep, src_row, deep=True)
    dst_row = fetch_session_row(dst_ep, sid)
    dst_info = collect_info(dst_ep, dst_row, deep=True) if dst_row else None
    override_row = None   # 软冲突时被覆盖掉的目标记录（id 与源不同）

    print()
    print("=" * 70)
    print("单对话跨版本迁移")
    print("=" * 70)
    print(f"\n  方向: {src_ep.label} → {dst_ep.label}   模式: {mode}")
    print()
    print("  【源对话】")
    print(render_single(src_info))

    # 硬冲突：ID 相同
    # 注意：这里必须初始化为 None，否则「无冲突」时会被误判成"已确认覆盖"，
    # 从而跳过下面的执行前确认（默认 move 会删源，不加确认很危险）。
    decision = None
    if dst_info:
        decision = ask_conflict(src_info, dst_info, "id", assume_yes, on_conflict)
        if decision == "cancel":
            print("\n已取消，未做任何改动")
            return
        if decision == "skip":
            print("\n已跳过该对话")
            return
    else:
        # 软冲突：标题相同但 id 不同
        same_title = [
            r for r in list_sessions(dst_ep)
            if (r.get("custom_title") or r.get("title") or "").strip() == src_info.title
            and r.get("id") != sid and src_info.title
        ]
        if same_title:
            d_info = collect_info(dst_ep, same_title[0], deep=True)
            decision = ask_conflict(src_info, d_info, "title", assume_yes, on_conflict)
            if decision == "cancel":
                print("\n已取消，未做任何改动")
                return
            if decision == "skip":
                print("\n已跳过该对话（源与目标均未改动）")
                return
            # 覆盖：删掉目标那条旧记录，仍用「源的 id」写入。
            # 不能改用目标 id——否则 db 里的 id 与正文文件名对不上，客户端会读到空对话。
            override_row = same_title[0]
            print(f"\n  → 将删除目标中的旧对话 {d_info.session_id[:8]}…，"
                  f"并以源 id {sid[:8]}… 写入（保证正文文件与 id 一致）")

    # 计划摘要
    files = src_info.files
    steps = [
        f"目标库插入 session 行（user_id 改写为 {target_uid[:12]}…）",
        f"复制 {len(files)} 个正文文件，共 {fmt_size(src_info.file_size)}",
    ]
    if override_row:
        steps.append(f"删除目标中的旧对话 {str(override_row['id'])[:8]}…（标题重复）")
    if src_info.has_tasks:
        steps.append(f"复制任务数据 tasks/{sid[:8]}…")
    if mode == "move":
        steps.append("删除源版本中的该对话（行 + 文件）")
    steps.append(f"备份到 {dst_ep.backup_dir.name}/")

    print()
    print("  【将执行】")
    for i, s in enumerate(steps, 1):
        print(f"   {i}. {s}")

    if dry_run:
        print("\n  [dry-run] 未做任何改动")
        return

    # 冲突询问里已经确认过覆盖时，不再重复确认一次
    if not assume_yes and decision != "overwrite":
        if not ask_yes_no("\n确认执行？(y/N): "):
            print("已取消")
            return

    # 备份
    print("\n📦 备份中...")
    bp = create_backup(src_ep, dst_ep, src_row, dst_row, sid, mode, backup_root,
                       extra_row=override_row)
    meta = json.loads((bp / "meta.json").read_text(encoding="utf-8"))
    meta["target_user_id"] = target_uid
    print(f"  ✅ {bp.name}")

    # 写入目标库
    print("\n🔄 写入目标版本...")
    conn = connect_rw(dst_ep.db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst_cols = table_columns(conn, "sessions")
        cols = [c for c in src_row.keys() if c in dst_cols]
        values = []
        for c in cols:
            v = src_row[c]
            if c == "user_id":
                v = target_uid          # 关键：跨版本必须改写 user_id
            elif c == "id":
                v = sid
            values.append(v)
        conn.execute(
            f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            values,
        )

        # usage
        sconn = connect_ro(src_ep.db)
        if sconn is not None:
            try:
                r = sconn.execute(
                    "SELECT * FROM session_usage WHERE session_id = ?", (src_row["id"],)
                ).fetchone()
                if r:
                    ucols = table_columns(sconn, "session_usage")
                    urow = dict(zip(ucols, r))
                    urow["session_id"] = sid
                    dcols = [c for c in urow.keys() if c in table_columns(conn, "session_usage")]
                    conn.execute(
                        f"INSERT OR REPLACE INTO session_usage ({','.join(dcols)}) VALUES ({','.join('?' * len(dcols))})",
                        [urow[c] for c in dcols],
                    )
            finally:
                sconn.close()

        # workspaces（登记工作目录，否则客户端可能找不到路径）
        if src_row.get("cwd"):
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO workspaces (path, last_opened_at) VALUES (?, ?)",
                    (src_row["cwd"], int(src_row.get("last_activity_at") or datetime.now().timestamp() * 1000)),
                )
            except Exception:
                pass

        # 软冲突覆盖：删掉目标里那条标题重复的旧记录
        if override_row:
            ov_id = override_row["id"]
            conn.execute("DELETE FROM session_usage WHERE session_id = ?", (ov_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (ov_id,))
            for f in find_project_files(dst_ep, ov_id):
                try:
                    remove_path(f)  # 同样可能是 tool-results 目录
                except OSError as e:
                    print(f"  ⚠️  残留目标旧文件 {f.name} 未能删除：{e}")
            print(f"  ✅ 已删除目标中的旧对话 {ov_id[:8]}…")
            meta["override_deleted"] = True

        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        n = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ? AND user_id = ?", (sid, target_uid)
        ).fetchone()[0]
        print(f"  ✅ session 行已写入（验证命中 {n} 行）")
        meta["target_overwritten"] = dst_row is not None
    finally:
        conn.close()

    # 复制正文文件
    print("\n📄 复制对话正文...")
    # 目标目录：优先沿用目标侧已存在的同名目录，否则按 cwd 推导
    existing = find_project_files(dst_ep, sid)
    if existing:
        dst_dir = existing[0].parent
    else:
        # 目录名由 cwd 推导；cwd 为空时（老会话/异常数据）退回用源侧正文所在目录名，
        # 否则 projects_dir / "" 会把正文直接扔在 projects 根目录，
        # 客户端按 projects/<slug>/<sid>.jsonl 找，等于迁移成功却打不开。
        slug = cwd_to_slug(src_row.get("cwd", "")) or cwd_to_slug(src_info.cwd)
        if not slug and src_info.files:
            slug = src_info.files[0].parent.name
        if not slug:
            raise RuntimeError(
                "无法确定目标 projects 子目录（会话记录里 cwd 为空），"
                "为避免正文落到 projects 根目录导致客户端打不开，已中止；请回滚本次迁移"
            )
        dst_dir = dst_ep.projects_dir / slug
    dst_dir.mkdir(parents=True, exist_ok=True)
    if dst_dir == dst_ep.projects_dir:
        raise RuntimeError("目标目录退化成 projects 根目录，已中止（请回滚本次迁移）")

    copied = []
    for f in src_info.files:
        if not f.exists():
            continue
        target = dst_dir / f.name
        if not safe_copy(f, target):
            raise RuntimeError("复制对话正文失败，已中止（请先回滚或将源数据手动恢复）")
        copied.append(str(target))
        mark = "目录" if f.is_dir() else "文件"
        print(f"  ✅ {f.name} [{mark}] ({fmt_size(path_size(f))})")
    meta["copied_to"] = copied

    # 任务数据
    src_tasks = src_ep.tasks_dir / src_row["id"]
    if src_tasks.exists():
        dst_tasks = dst_ep.tasks_dir / sid
        if dst_tasks.exists():
            shutil.rmtree(str(dst_tasks))
        shutil.copytree(str(src_tasks), str(dst_tasks))
        meta["tasks_copied"] = True
        print(f"  ✅ 任务数据已复制")

    # move：删除源
    if mode == "move":
        print("\n🧹 删除源版本数据（move 模式）...")
        conn = connect_rw(src_ep.db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("DELETE FROM session_usage WHERE session_id = ?", (src_row["id"],))
            conn.execute("DELETE FROM sessions WHERE id = ?", (src_row["id"],))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            left = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (src_row["id"],)
            ).fetchone()[0]
            print(f"  ✅ 源 session 行已删除（剩余 {left} 行）")
            meta["source_deleted"] = True
        finally:
            conn.close()
        for f in src_info.files:
            if f.exists():
                kind = "目录" if f.is_dir() else "文件"  # 需在删除前判断
                remove_path(f)  # 可能是文件，也可能是 tool-results 目录
                print(f"  ✅ 已删除源{kind} {f.name}")
        if src_tasks.exists():
            shutil.rmtree(str(src_tasks))
        # 文件删完后源目录可能空了，顺手清理，避免残留空目录
        for d in {f.parent for f in src_info.files}:
            try:
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
                    print(f"  ✅ 已清理空目录 {d.name}")
            except Exception:
                pass

    (bp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print("✅ 迁移完成")
    print("=" * 70)
    print(f"\n  对话:   {clip(src_info.display_title, 40)}")
    print(f"  方向:   {src_ep.label} → {dst_ep.label}（{mode}）")
    print(f"  备份:   {bp}")
    print(f"  回滚:   python3 scripts/migrate_session.py --rollback {bp.name}")
    print(f"\n  现在可以重新打开 {dst_ep.label} 客户端，即可看到该对话。")


# ---------------------------------------------------------------- 交互式


def choose_edition(prompt, default="domestic"):
    """选择版本"""
    print(f"\n{prompt}")
    print("  1. 国内版（~/.workbuddy）")
    print("  2. 国际版（~/.workbuddy-ai）")
    while True:
        try:
            c = input(f"请输入序号（默认 {'1' if default == 'domestic' else '2'}）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(0)
        if not c:
            return default
        if c == "1":
            return "domestic"
        if c == "2":
            return "intl"
        print("  请输入 1 或 2")


def print_session_table(ep: EditionPaths, rows, quick=True):
    """打印对话列表"""
    print()
    print("=" * 70)
    print(f"{ep.label} 对话列表（{len(rows)} 个）")
    print("=" * 70)
    if not rows:
        print("\n  （无对话）\n")
        return
    print(f"\n  {pad('序号', 6)}{pad('ID', 11)}{pad('最后活动', 14)}"
          f"{pad('状态', 12)}{pad('大小', 12)}标题")
    print("  " + "─" * 86)
    for i, r in enumerate(rows, 1):
        sid = r["id"]
        files = find_project_files(ep, sid)
        # 必须递归：tool-results 是目录，stat().st_size 只会返回 ~4KB
        size = sum(path_size(f) for f in files if f.exists())
        title = (r.get("custom_title") or r.get("title") or "").strip() or "(无标题)"
        print(
            f"  {pad(i, 6)}{pad(sid[:8], 11)}{pad(fmt_time(r.get('last_activity_at')), 14)}"
            f"{pad(r.get('status') or '-', 12)}{pad(fmt_size(size), 12)}{clip(title, 40)}"
        )
    print()


def pick_session(ep: EditionPaths, query=None):
    """交互式选择一个对话"""
    rows = list_sessions(ep, query)
    if not rows:
        print(f"❌ {ep.label} 没有匹配的对话")
        sys.exit(1)
    print_session_table(ep, rows)
    while True:
        try:
            c = input(f"请选择要迁移的对话（1-{len(rows)}，q 退出）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(0)
        if c.lower() == "q":
            sys.exit(0)
        try:
            idx = int(c) - 1
            if 0 <= idx < len(rows):
                return rows[idx]["id"]
        except ValueError:
            pass
        print(f"  请输入 1-{len(rows)} 的数字")


def interactive():
    """交互式向导"""
    print("=" * 70)
    print("WorkBuddy 单对话跨版本迁移")
    print("=" * 70)

    if not _clients_closed_or_force():
        sys.exit(2)

    src_name = choose_edition("\n【第 1 步】选择【源版本】（对话当前所在的版本）")
    dst_name = choose_edition("\n【第 2 步】选择【目标版本】（要迁移到的版本）")

    src_ep = resolve_edition(src_name)
    dst_ep = resolve_edition(dst_name)

    if not src_ep.exists():
        print(f"\n❌ {src_ep.label} 没有数据：{src_ep.db}")
        sys.exit(1)
    if src_name == dst_name:
        print("\n⚠️  源版本与目标版本相同，这是同版本内的迁移。")
        print("   如果是要在账号之间迁移整个账号的数据，请用 scripts/migrate.py")
        print("   · 源对话属于别的账号 → 只把该对话的 user_id 改到当前账号")
        print("   · 源对话已属于当前账号 → 复制出一份新对话（新 id，标题加「（副本）」）")
        if not ask_yes_no("   是否继续？(y/N): "):
            sys.exit(0)
    elif not dst_ep.exists():
        print(f"\n❌ {dst_ep.label} 没有数据：{dst_ep.db}")
        print("   请至少登录并使用一次目标版本，再执行迁移。")
        sys.exit(1)

    print("\n【第 3 步】选择要迁移的对话")
    sid = pick_session(src_ep)

    print("\n【第 4 步】迁移模式")
    print("  1. move  —— 迁移后删除源版本中的该对话（默认）")
    print("  2. copy  —— 迁移后保留源版本中的该对话")
    while True:
        try:
            c = input("请输入序号（默认 1）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return
        mode = "copy" if c == "2" else "move"
        break

    do_migrate(src_ep, dst_ep, sid, mode=mode, on_conflict="ask",
               dry_run=False, assume_yes=False)


# ---------------------------------------------------------------- CLI


def main():
    parser = argparse.ArgumentParser(
        description="WorkBuddy 单对话跨版本迁移（国内版 ⇄ 国际版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from", dest="src", choices=["domestic", "intl"], default="domestic",
                        help="源版本（默认 domestic）")
    parser.add_argument("--to", dest="dst", choices=["domestic", "intl"], default="domestic",
                        help="目标版本（默认 domestic）")
    parser.add_argument("--session-id", "-i", help="要迁移的对话 id（支持前缀）")
    parser.add_argument("--list", "-l", action="store_true", help="列出源版本的对话")
    parser.add_argument("--query", "-q", help="按标题/路径/id 过滤")
    parser.add_argument("--mode", "-m", choices=["move", "copy"], default="move",
                        help="迁移语义：move=迁移后删除源（默认），copy=保留源")
    parser.add_argument("--on-conflict", choices=["ask", "skip", "overwrite", "newer"],
                        default="ask", help="冲突策略（默认 ask）")
    parser.add_argument("--target-uid", help="手动指定目标版本的 user_id")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    parser.add_argument("--yes", "-y", action="store_true", help="非交互模式")
    parser.add_argument("--force", action="store_true", help="跳过「客户端必须关闭」检测")
    parser.add_argument("--backups", action="store_true", help="列出可回滚的备份")
    parser.add_argument("--rollback", help="回滚指定备份（标签或前缀）")
    parser.add_argument("--full", action="store_true", help="与 --rollback 配合：整库恢复")
    parser.add_argument("--backup-dir", help="备份目录（默认目标版本的 migrate_backups/）")

    args = parser.parse_args()

    if legacy is None:
        print("⚠️  未找到 scripts/migrate.py，部分功能（当前账号推断）将降级")

    if args.backups:
        list_backups(args.backup_dir)
        return

    if args.rollback:
        rollback(args.rollback, args.backup_dir, full=args.full, assume_yes=args.yes)
        return

    src_ep = resolve_edition(args.src)

    if args.list:
        rows = list_sessions(src_ep, args.query)
        print_session_table(src_ep, rows)
        return

    if not args.session_id:
        interactive()
        return

    # 显式指定对话 id
    if not args.dry_run and not _clients_closed_or_force(args.force):
        sys.exit(2)

    dst_ep = resolve_edition(args.dst)
    if not src_ep.exists():
        print(f"❌ 源版本没有数据：{src_ep.db}")
        sys.exit(1)
    if args.src != args.dst and not dst_ep.exists():
        print(f"❌ 目标版本没有数据：{dst_ep.db}")
        sys.exit(1)

    do_migrate(
        src_ep, dst_ep, args.session_id,
        mode=args.mode, on_conflict=args.on_conflict,
        dry_run=args.dry_run, assume_yes=args.yes,
        target_uid=args.target_uid, backup_root=args.backup_dir,
    )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        # 备份/复制阶段的业务性中止：给出人话提示，不打 traceback
        print(f"\n❌ {e}")
        sys.exit(1)
    except sqlite3.Error as e:
        # 跨库 INSERT 撞上目标库新增的 NOT NULL 无默认值列等情况：
        # 给可操作提示而不是 traceback
        print(f"\n❌ 数据库写入失败：{e}")
        print("   常见原因：目标版本库结构比源版本新（多了 NOT NULL 且无默认值的列）。")
        print("   处理：用 --rollback <备份标签> 回滚，或先用 --backups 查看备份。")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
