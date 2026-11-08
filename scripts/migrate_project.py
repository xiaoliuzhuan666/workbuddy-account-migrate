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
  - 硬冲突（同 id）  ：默认 ask（交互模式逐个询问）；无 TTY 时降级为跳过。
    要「反复导入不出双份」请显式 --on-conflict overwrite
  - 软冲突（同标题不同 id）：默认跳过并警告，交互模式可询问；
    --on-conflict overwrite 时按「仍导入」处理（id 不同，不删本地那条）
  - 无 TTY 时一律降级为跳过

环境变量:
  WORKBUDDY_MIGRATE_HOME   覆盖 home 目录（测试用，指向临时 fixture）

平台兼容性:
  脚本本身跨平台（pathlib + tasklist/ps 进程检测），逻辑复用 v1.6 系列已验证机制；
  跨设备全流程需两台真机实测，欢迎反馈。
"""

import argparse
import base64
import csv
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
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

TOOL_VERSION = "1.7.0"
PKG_FORMAT = "workbuddy-project-export"


def _looks_like_uid(name: str) -> bool:
    """与 migrate.py / migrate_session.py 同口径的 UUID 形态判断"""
    return bool(re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", name or ""))


def _safe_sid(sid) -> bool:
    """包内/备份里的 session id 能否安全地拼进路径与 glob。

    Windows 上 `Path(root) / "C:evil"` 会**丢掉基路径**变成盘符相对路径，
    所以冒号必须禁；首尾点/空格会被 Win32 剥掉，导致 `abc` 与 `abc.`
    落到同一物理目录。`.`/`..` 会让 stash 目录退化成备份根。
    计划循环与回滚共用这一个判据，避免两处各写一套。
    """
    if not isinstance(sid, str) or not (1 <= len(sid) <= 128):
        return False
    if sid in (".", "..") or re.match(r"^[A-Za-z]:", sid):
        return False
    if re.search(r"[\x00-\x1f\x7f/\\:*?\"\[\]<>|]", sid):
        return False
    # CON / NUL / COM1 等在 Windows 上是设备名，建不出普通文件
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", sid):
        return False
    return sid[0] not in ". " and sid[-1] not in ". "


PKG_FORMAT_VERSION = 2   # v2: BLOB 用 {"__b64__": ...} 无损编码（v1 是 repr 字符串）
PKG_EXT = ".wbproj"

EDITIONS = {"domestic": "国内版", "intl": "国际版"}
EDITION_DIR = {"domestic": ".workbuddy", "intl": ".workbuddy-ai"}

PROC_KEYWORDS = ("workbuddy", "codebuddy")


def resolve_home() -> Path:
    """home 目录；WORKBUDDY_MIGRATE_HOME 优先（测试 fixture 隔离）"""
    env = os.environ.get("WORKBUDDY_MIGRATE_HOME")
    if env:
        return Path(env).expanduser().resolve()
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
        # stdin 关闭 ≠ 用户同意。写入前确认传的是 default=True，
        # 沿用旧写法（return bool(default)）会让 Ctrl-D / 管道关闭直接写库。
        return False
    except KeyboardInterrupt:
        # Ctrl-C 是明确的中断意图，一律按「不同意」处理。
        # 以前和 EOF 共用一条分支、返回 bool(default)，default=True 的
        # 「确认导入」会在用户按 Ctrl-C 时继续导入。
        print()
        return False
    if not r and default is not None:
        return default
    return r in ("y", "yes")


def is_interactive() -> bool:
    # sys.stdin 可能为 None（pythonw / 完全脱离终端），直接 .isatty() 会 AttributeError
    return sys.stdin is not None and sys.stdin.isatty()


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


def _edition_from_args(args) -> EditionPaths:
    """按 --intl / --dir 解析数据目录

    所有子命令共用：以前 --backups 只认 --intl、忽略 --dir，列出的备份来自默认
    home，用户照着 tag 去 --rollback 会找不到（或回滚错版本）。
    """
    name = "intl" if getattr(args, "intl", False) else "domestic"
    if getattr(args, "dir", None):
        root = Path(args.dir).expanduser().resolve()
        return EditionPaths(
            name=name, root=root, db=root / "workbuddy.db",
            projects_dir=root / "projects", todos_dir=root / "todos",
            tasks_dir=root / "tasks", backup_dir=root / "migrate_backups",
            account_snapshot=root / "storage" / "skeleton" / "account-snapshot.json",
        )
    return resolve_edition(name, resolve_home())


def _kind_bases(ep: EditionPaths) -> dict:
    return {"projects": ep.projects_dir, "todos": ep.todos_dir, "tasks": ep.tasks_dir}


def cwd_to_slug(cwd: str) -> str:
    """工作目录 → projects 子目录名

    C:\\Users\\alice\\WorkBuddy\\2026-09-10-14-49-02
      → c-Users-alice-WorkBuddy-2026-09-10-14-49-02
    \\\\server\\share\\proj
      → unc-server-share-proj        （UNC 网络盘，前缀 unc- 以免与本地目录混淆）

    盘符转小写、去掉冒号、\\ 和 / 转 -。import 侧必须用【目标机新路径】推导，
    沿用源 slug 而 cwd 改了新路径 = 客户端按 projects/<slug>/<sid>.jsonl 找不到。

    ⚠️ 必须与 migrate_session.cwd_to_slug 完全一致：同一 UNC 项目经两个工具
    落到不同子目录，就会出现「迁移成功却打不开」。
    """
    if not cwd:
        return ""
    s = str(cwd)
    unc = s.startswith("\\\\") or s.startswith("//")
    s = re.sub(r"^([A-Za-z]):", lambda m: m.group(1).lower(), s)
    s = s.replace("\\", "-").replace("/", "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if unc and s:
        s = "unc-" + s
    return s


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
        if busy == -1:
            # 非 WAL 模式（journal_mode=DELETE）的库返回 -1，不代表"有进程占锁"。
            # 以前 if busy: 把 -1 当成失败，每次导入/回滚都误报。
            return True
        if busy:
            print("  ⚠️  WAL checkpoint 未完成（busy），结果可能未落盘；请确认客户端已关闭后重试")
            return False
        return True
    except Exception as e:
        print(f"  ⚠️  WAL checkpoint 失败：{e}")
        return False


def snapshot_db(src: Path, dst: Path):
    """用 sqlite backup API 备份数据库（v1.6.1 教训：文件复制拿到的是陈旧快照）"""
    # mode=ro：快照不该对源库产生任何写入（也不会顺手创建 -wal）。
    # export 号称「只读操作」，这里读写打开就自相矛盾了。
    src_conn = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
        # 与 migrate.py / migrate_session.py 同口径：仅当源库本来就是 WAL 才把
        # 备份产物设回 WAL；无条件设会在源库为 DELETE 时静默改变 journal mode。
        try:
            _jm = (src_conn.execute("PRAGMA journal_mode").fetchone() or [""])[0]
            if str(_jm).lower() == "wal":
                dst_conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
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


def _ensure_writable(p: Path):
    """给文件或目录树补上写权限；失败不影响主流程

    `copy2` / `copytree` 会把源的只读属性一起带到产物上。只读文件在 Windows 上
    删不掉：导入产物清不掉、回滚覆盖不了、临时 fixture 也删不掉。
    """
    try:
        if p.is_dir():
            for inner in p.rglob("*"):
                try:
                    os.chmod(str(inner), inner.stat().st_mode | stat.S_IWRITE)
                except OSError:
                    pass
            os.chmod(str(p), p.stat().st_mode | stat.S_IWRITE)
        elif p.exists():
            os.chmod(str(p), p.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass


def copy_path(src: Path, dst: Path) -> bool:
    """复制文件或目录；返回是否成功（只读位一并处理）

    会话附属物里有【目录】（tool-results/），不能直接 shutil.copy2（v1.6.0 教训）。
    """
    try:
        if src.is_dir():
            if dst.exists():
                remove_path(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(src), str(dst))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                # 覆盖只读文件前必须先补写权限：copy2 写只读目标会 PermissionError
                # （回滚"还原被覆盖的文件"正好会撞上这种目标）
                _ensure_writable(dst)
            shutil.copy2(str(src), str(dst))
        _ensure_writable(dst)
        return True
    except (OSError, shutil.Error) as e:
        print(f"  ⚠️  复制 {src.name} 失败：{e}")
        return False


def remove_path(p: Path) -> bool:
    """删除文件/目录；失败先抹掉只读位重试，仍失败返回 False（不抛 OSError）

    与 migrate_session.remove_path 同一口径：回滚路径上「删不掉」必须能让调用方
    知道并汇总，而不是抛成裸 traceback 停在半途。
    """
    def _do():
        if p.is_dir():
            shutil.rmtree(str(p))
        elif p.exists():
            p.unlink()

    try:
        _do()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        _ensure_writable(p)
        try:
            _do()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False


def safe_copy(src: Path, dst: Path) -> bool:
    if copy_path(src, dst):
        return True
    print("     常见原因：客户端未完全退出、文件被占用或权限不足。")
    return False


def _validate_backup_tag(tag: str) -> bool:
    """备份标签只能是单层目录名（防路径遍历）

    另两个脚本都有同等检查：标签来自命令行，直接拼进 backup_dir 会被用于
    rmtree / copytree 的目标推导。
    """
    if not tag or tag in (".", ".."):
        return False
    if "/" in tag or "\\" in tag or ".." in Path(tag).parts:
        return False
    if os.path.isabs(tag) or re.match(r"^[A-Za-z]:", tag):
        return False
    return True


def _safe_rel(rel: str) -> bool:
    """meta / 包内的相对路径必须是「往下走」的单层相对路径"""
    if not rel or os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel):
        return False
    parts = Path(rel).parts
    return bool(parts) and ".." not in parts


def find_session_files(ep: EditionPaths, session_id: str):
    """定位某会话的正文文件（可能含 tool-results 目录），返回相对 projects_dir 的路径列表

    ⚠️ 必须做前缀过滤：`glob("*/{sid}*")` 在 id 互为前缀时会把别的会话文件也算进来
    （migrate_session.py 的 find_project_files 有同样的过滤 + 专门的回归用例）。
    """
    if not ep.projects_dir.exists():
        return []
    return sorted(
        p.relative_to(ep.projects_dir).as_posix()
        for p in ep.projects_dir.glob(f"*/{session_id}*")
        if p.name == session_id or p.name.startswith(session_id + ".")
    )


# ---------------------------------------------------------------- 进程检测


_SELF_SCRIPT_NAMES = ("migrate.py", "migrate_session.py", "migrate_project.py",
                      "run_tests.py", "run_project_tests.py", "prepare_fixture.py")


def _is_self_process(cmdline: str) -> bool:
    """判断某条进程记录是不是本工具自己

    仓库目录名含 "workbuddy"，`ps -eo args=` 会输出完整命令行 →
    `python3 .../workbuddy-account-migrate/scripts/migrate_project.py import ...`
    自己就命中 PROC_KEYWORDS。不排除的话 import / --rollback 会被自己的守卫
    无条件拦下（exit 2），用户只能长期加 --force —— 反而把最危险的开关变成习惯。
    """
    norm = (cmdline or "").lower().replace("\\", "/")
    if not norm:
        return False
    script_dir = str(Path(__file__).resolve().parent).lower().replace("\\", "/")
    if script_dir and script_dir in norm:
        return True
    repo_root = str(Path(__file__).resolve().parent.parent).lower().replace("\\", "/")
    for token in re.split(r"[\s\"']+", norm):
        cleaned = token.rstrip(":")
        if cleaned.rsplit("/", 1)[-1] not in _SELF_SCRIPT_NAMES:
            continue
        if "/" not in cleaned or cleaned.startswith(repo_root + "/"):
            return True
    return False


def find_running_clients():
    """检测正在运行的客户端进程，返回 (进程名列表, 检测是否可信)"""
    found = set()
    system = platform.system()
    try:
        if system == "Windows":
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
            )
            if proc.returncode != 0:
                # 命令失败时 stdout 是空的 —— 不能当成「确实没有客户端」，
                # 否则会在客户端持锁时继续写库
                return [], False
            for line in proc.stdout.splitlines():
                row = next(csv.reader([line]), [])
                if row and any(k in row[0].lower() for k in PROC_KEYWORDS):
                    found.add(row[0])
        else:
            for ps_args in (["ps", "-eo", "comm="], ["ps", "-eo", "args="]):
                proc = subprocess.run(
                    ps_args, capture_output=True, text=True,
                    errors="replace", timeout=15,
                )
                if proc.returncode != 0:
                    return [], False
                for line in proc.stdout.splitlines():
                    name = line.strip()
                    if not name:
                        continue
                    if _is_self_process(name):
                        continue
                    if any(k in name.lower() for k in PROC_KEYWORDS):
                        found.add(Path(name.split()[0]).name or name)
    except Exception as e:
        print(f"⚠️  客户端进程检测失败（{e}），无法确认客户端是否已关闭")
        return [], False
    return sorted(found), True


def require_clients_closed(force=False, assume_closed=False) -> bool:
    """import 是写操作，客户端必须关闭

    assume_closed：只在**检测本身失败**时生效（真检测到客户端仍在运行照样拦截），
    与 migrate.py / migrate_session.py 的 `--assume-clients-closed` 同义。
    """
    running, trustworthy = find_running_clients()
    if not running and trustworthy:
        return True
    if not trustworthy and not (force or assume_closed):
        print("❌ 无法确认客户端是否已关闭。若已确认全部退出，可用 "
              "--assume-clients-closed（或风险更大的 --force）继续。")
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


def _json_string_end(text: str, start: int):
    """从 start（指向开头的 `"`）开始，返回字符串结束后的位置；未闭合返回 None"""
    n = len(text)
    i = start + 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2          # 跳过转义字符（含 \" 与 \\）
            continue
        if c == '"':
            return i + 1
        i += 1
    return None


def _json_value_end(text: str, start: int):
    """从 start（指向值的第一个字符）开始，返回值结束后的位置"""
    n = len(text)
    if start >= n:
        return None
    c = text[start]
    if c == '"':
        return _json_string_end(text, start)
    if c in "{[":
        depth = 0
        i = start
        while i < n:
            ch = text[i]
            if ch == '"':
                end = _json_string_end(text, i)
                if end is None:
                    return None
                i = end
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return None
    # 数字 / true / false / null：读到分隔符为止
    i = start
    while i < n and text[i] not in ",}] \t\r\n":
        i += 1
    return i


def _top_level_value_span(text: str, key: str):
    """在 JSON 对象文本里定位【顶层】key 的值区间 → (start, end)，找不到返回 None

    为什么不用正则：`"cwd"` 在嵌套对象里先出现时
    （`{"message":{"cwd":"…"},"cwd":"…"}`），`re.sub(count=1)` 改的是嵌套那个值。
    这里按 JSON 词法扫描（跳过字符串内容与转义），只在深度 1 的键上匹配。
    """
    n = len(text)
    i = text.find("{")
    if i < 0:
        return None
    i += 1
    depth = 1
    expect_key = True      # 深度 1 上，下一个字符串可能是键
    while i < n:
        c = text[i]
        if c == '"':
            end = _json_string_end(text, i)
            if end is None:
                return None
            if depth == 1 and expect_key:
                k = end
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and text[k] == ":":
                    try:
                        name = json.loads(text[i:end])
                    except ValueError:
                        name = None
                    if name == key:
                        v = k + 1
                        while v < n and text[v] in " \t\r\n":
                            v += 1
                        vend = _json_value_end(text, v)
                        if vend is None:
                            return None
                        return v, vend
                    expect_key = False
                    i = k + 1
                    continue
                expect_key = False
            i = end
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return None
        elif c == "," and depth == 1:
            expect_key = True
        i += 1
    return None


def rewrite_jsonl_cwd(src_fh, dst_fh, old_cwd: str, new_cwd: str) -> int:
    """流式改写 jsonl 每行顶层 "cwd" 字段值（实测每条消息顶层都带 cwd）。

    只动结构化字段值，消息文本里出现的路径字符串一律不碰（沿用 v1.6.1
    "不碰用户可见内容" 原则）。先用 `json.loads` 确认顶层 cwd 就是旧路径，
    再按 JSON 词法扫描定位**顶层** cwd 值的字符区间做定点替换 ——
    不用正则找第一个 `"cwd"`：嵌套对象里先出现同名键时会改错字段。
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
            span = _top_level_value_span(stripped, "cwd")
            offset = line.find(stripped) if span else -1
            if span and offset >= 0:
                s, e = span
                dst_fh.write(line[:offset + s] + new_val + line[offset + e:])
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
            json.dump(data, f, ensure_ascii=False, indent=1, default=_json_default)
        self._tempfiles.append(p)
        return p

    def write(self, manifest: dict, session_rows: list, usage_rows: list,
              file_sources: list, workspace_sources: list):
        """file_sources: [(绝对路径, arcname)]；workspace_sources: [(绝对路径, arcname)]"""
        ok = False
        created_new = not self.out_path.exists()
        # do_export 入口已提前拦过一次；这里是 write() 的最后一道保险
        if not created_new:
            raise RuntimeError(f"输出包已存在，拒绝覆盖：{self.out_path}（请换一个 --out 路径）")
        try:
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
            ok = True
        finally:
            # 打包中途失败也要清理：临时目录里是含用户对话正文的 JSON，
            # 不能留在 %TEMP%（与 fixture 数据泄漏同一类问题）
            self.cleanup()
            if not ok and created_new:
                # 只删本次新建的半成品；绝不删用户原有的包
                try:
                    if self.out_path.exists():
                        self.out_path.unlink()
                except OSError:
                    pass

    def cleanup(self):
        for p in self._tempfiles:
            try:
                os.unlink(p)
            except OSError:
                pass
        self._tempfiles = []


# 迁移包的体积上限：包可能来自不可信来源（网盘 / U 盘 / 别人发的），
# 不设限的话一个特制压缩包就能把磁盘吃满
MAX_MEMBER_BYTES = 512 * 1024 * 1024          # 单成员 512MB
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024      # 展开总量 4GB
MAX_MEMBERS = 200_000                          # 成员数量上限（几十万个 0 字节成员能绕过大小限制）


def read_package_members(pkg: Path):
    """读包，返回 (tar, manifest, members_by_name)；不落盘，全部流式

    失败一律转成 RuntimeError（带人话说明）并关闭 tar：
    `tarfile.ReadError` 不是 RuntimeError 子类，以前会穿透成裸 traceback。
    """
    try:
        tar = tarfile.open(str(pkg), "r:gz")
    except (tarfile.TarError, OSError) as e:
        raise RuntimeError(f"无法打开迁移包（{e}）：{pkg}") from e

    try:
        manifest = None
        members = {}
        total = 0
        count = 0
        # 用 next() 逐个读而不是 getmembers()：可以边读边计数、尽早撞上 MAX_MEMBERS
        # 退出。注意 TarFile 仍会把每个 TarInfo 缓存进 self.members，所以这条只防
        # "一次性构造列表 + 无上限"，不是"完全不占内存"。
        while True:
            m = tar.next()
            if m is None:
                break
            count += 1
            if count > MAX_MEMBERS:
                raise RuntimeError(
                    f"包内成员数过多（超过上限 {MAX_MEMBERS}），已中止")
            if not m.isfile():
                continue
            name = m.name
            if name.startswith("./"):
                name = name[2:]
            # 路径校验：拒绝绝对路径 / Windows 盘符 / 上跳，否则成员名可以把文件
            # 写到数据目录之外（tasks/{sid}/ 的剩余路径是直接拼上去的）
            if (name.startswith("/") or name.startswith("\\")
                    or re.match(r"^[A-Za-z]:", name)):
                raise RuntimeError(f"包内出现非法路径成员：{m.name}")
            if ".." in Path(name).parts:
                raise RuntimeError(f"包内出现非法路径成员：{m.name}")
            if m.size > MAX_MEMBER_BYTES:
                raise RuntimeError(
                    f"包内成员过大（{m.size} 字节，上限 {MAX_MEMBER_BYTES}）：{m.name}")
            total += m.size
            if total > MAX_TOTAL_BYTES:
                raise RuntimeError(
                    f"包展开后总大小超过上限（{MAX_TOTAL_BYTES} 字节），已中止")
            members[name] = m
            if name == "manifest.json":
                fh = tar.extractfile(m)
                if fh is None:
                    raise RuntimeError("包内 manifest.json 无法读取")
                try:
                    with fh:
                        manifest = json.loads(fh.read().decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as e:
                    raise RuntimeError(f"包内 manifest.json 无法解析：{e}") from e
        if manifest is None:
            raise RuntimeError("包内缺少 manifest.json，不是本工具导出的项目包")
        if not isinstance(manifest, dict):
            raise RuntimeError("包内 manifest.json 顶层不是对象，无法导入")
        if manifest.get("format") != PKG_FORMAT:
            raise RuntimeError(f"包格式不符：{manifest.get('format')}")
        _ver = manifest.get("format_version")
        if _ver is not None and not isinstance(_ver, int):
            # 字符串 "3" / 3.0 之类不能悄悄按 v2 语义解包
            raise RuntimeError(f"包内 format_version 无法识别（{_ver!r}），已中止导入")
        if _ver is None or _ver < PKG_FORMAT_VERSION:
            # v1 的 BLOB 列是 default=str 的 repr 文本，_revive_blobs 认不出来
            print("  ⚠️  这是 v1 格式的包（或缺少 format_version）：若 sessions/usage 含 BLOB 列，"
                  "会以文本形式写回（类型不可逆）；建议用当前版本重新导出")
        if isinstance(_ver, int) and _ver > PKG_FORMAT_VERSION:
            # 新版本工具写的包，旧工具按 v1 语义解包会静默出错
            raise RuntimeError(
                f"包格式版本 v{_ver} 比本工具支持的 v{PKG_FORMAT_VERSION} 新，"
                "请升级工具后再导入")
        return tar, manifest, members
    except BaseException:
        tar.close()
        raise


def _to_int(v, default=0) -> int:
    """脏时间戳/脏 id 容错：包可能来自旧版本或手工编辑"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _json_default(o):
    """BLOB 无损序列化：bytes 转 {"__b64__": ...}

    以前用 default=str，bytes 会变成 "b'\\x01\\x02'" 这种 repr 字符串，
    导入时原样写回目标库 —— 类型从 BLOB 变 TEXT、内容不可逆。改成 base64 +
    标记，导入侧 _revive_blobs() 再解码回 bytes。
    """
    if isinstance(o, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(o)).decode("ascii")}
    return str(o)


def _revive_blobs(row: dict) -> dict:
    """把 {"__b64__": ...} 还原成 bytes（与 _json_default 配对）"""
    for k, v in list(row.items()):
        if isinstance(v, dict) and set(v) == {"__b64__"}:
            try:
                row[k] = base64.b64decode(v["__b64__"], validate=True)
            except (ValueError, TypeError):
                # 坏包里的 base64：退化成空 bytes，不能留 dict 给 sqlite 绑定；
                # 但不能静默 —— 否则用户只看到「导入成功」，实际这列被清空了
                print(f"  ⚠️  {k} 的 base64 无法解码，已置空")
                row[k] = b""
    return row


def read_json_member(tar, members, name, default=None):
    m = members.get(name)
    if m is None:
        return default
    # 包可能来自网盘 / U 盘：坏 JSON 要转成受控错误，不能甩 traceback
    try:
        fh = tar.extractfile(m)
        if fh is None:
            raise RuntimeError(f"包内 {name} 无法读取")
        with fh:
            return json.loads(fh.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError, OSError, tarfile.TarError) as e:
        raise RuntimeError(f"包内 {name} 损坏，无法解析：{e}") from e


def stream_member_to_file(tar, member, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    fh = tar.extractfile(member)
    if fh is None:
        raise RuntimeError(f"包内成员无法读取：{member.name}")
    with fh, open(dst, "wb") as out:
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
    # 统一走 _edition_from_args（内部会 resolve）：export/import 以前各自内联
    # Path(args.dir).expanduser()，相对路径下 connect_ro 的 as_uri() 会抛
    # ValueError 被吞成「数据库不可读」，报错文案完全是误导。
    ep = _edition_from_args(args)

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
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return 1
        try:
            n = int(idx)
        except ValueError:
            print("❌ 无效选择")
            return 1
        # 显式范围检查：输入 0 时 projects[-1] 会静默选中最后一个项目
        if not 1 <= n <= len(projects):
            print(f"❌ 序号超出范围（1-{len(projects)}）")
            return 1
        sel_cwd = projects[n - 1][0]

    # 收集会话与文件。sessions 与 usage 用同一次连接读：
    # 两个连接会看到不同快照（客户端在跑时可能不一致），第二次连接失败还会
    # 静默生成一份不含 usage 的包。
    usage_rows = []
    usage_ids = set()
    conn = connect_ro(ep.db)
    if conn is None:
        print("❌ 数据库无法读取")
        return 1
    try:
        conn.row_factory = sqlite3.Row
        # BEGIN：同一连接也要显式开读事务，否则两次查询之间客户端提交的新数据
        # 会进来，sessions 与 usage 就不是同一份快照
        conn.execute("BEGIN")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions WHERE cwd = ? AND deleted_at IS NULL "
            "ORDER BY last_activity_at ASC", (sel_cwd,))]
        for row in rows:
            u = conn.execute(
                "SELECT * FROM session_usage WHERE session_id = ?", (row["id"],)
            ).fetchone()
            if u:
                usage_rows.append(dict(u))
                usage_ids.add(row["id"])
        conn.execute("COMMIT")
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
    # usage 行先读出来：per_session["has_usage"] 要按实际结果写。以前恒写 True，
    # 而 manifest 是导入侧与二期增量同步的依据，字段失真会一路传导。
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
            "last_activity_at": _to_int(row.get("last_activity_at")),
            "files": arcs,
            "has_usage": sid in usage_ids,
        })

    workspace_sources = []
    if not args.no_workspace_memory:
        workspace_sources = collect_workspace_files(Path(sel_cwd))

    # 一个项目目录下可能混有多个账号的会话（换过账号 / 两地交替）。
    # 以前 source_uid 直接取 rows[0]，其余账号的会话会被记成错的来源。
    src_uids = sorted({str(r.get("user_id") or "") for r in rows if r.get("user_id")})
    if len(src_uids) > 1:
        print(f"  ⚠️  该项目下混有 {len(src_uids)} 个账号的会话："
              f"{', '.join(u[:8] + '…' for u in src_uids)}")
        print("     manifest.source_uid 取第一个，完整列表见 source_uids；"
              "导入时会统一改写为本机登录账号")

    manifest = {
        "format": PKG_FORMAT,
        "format_version": PKG_FORMAT_VERSION,
        "tool_version": TOOL_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "edition": ep.name,
        "source_cwd": sel_cwd,
        "source_uid": src_uids[0] if src_uids else "",
        "source_uids": src_uids,
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

    # out_path 一算出来就拦，别等读完整库、打完导出摘要才在 write() 里报错
    if out_path.exists():
        print(f"❌ 输出包已存在，拒绝覆盖：{out_path}")
        print("   请换一个 --out 路径，或先移走旧包。")
        return 1

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
            for f in sorted((ep.tasks_dir / sid).rglob("*")) if f.is_file()]
           if (ep.tasks_dir / sid).is_dir() else [])
    ):
        src = ep.projects_dir / rel if kind == "projects" else (ep.todos_dir / rel if kind == "todos" else ep.tasks_dir / rel)
        if src.exists():
            dst = stash_dir / kind / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 先复制到临时路径、成功后再替换：copy_path 中途失败会在 dst 留半截
            # 文件，回滚时那个半截文件会被当成有效备份覆盖原文件（等于用坏数据
            # 换掉好数据）。要么完整备份，要么完全没有。
            # 临时件放 stash 树之外：回滚的 rglob 会把 stash 树里的**任何**文件
            # 当成合法备份还原出去，所以中转文件不能留在树内
            tmp_dir = backup_dir / ".tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp = tmp_dir / f"{kind}-{rel.replace('/', '_')}"
            remove_path(tmp)
            if not copy_path(src, tmp):
                remove_path(tmp)
                # 备份失败就不能覆盖：覆盖后原文件没了、备份里也没有 = 不可恢复
                raise RuntimeError(f"备份目标侧原有文件失败：{rel}（该会话未改动）")
            if dst.exists():
                remove_path(dst)
            try:
                os.replace(str(tmp), str(dst))
            except OSError as e:
                remove_path(tmp)
                raise RuntimeError(f"备份目标侧原有文件失败：{rel}（{e}）") from e
            saved.append({"kind": kind, "rel": rel})
    return saved


def group_package_members(members: dict):
    """按 sid 预分组包内成员，并按 `offset_data` 排序（消除导入侧两层 O(n²)）。

    旧实现有两层退化：
      1. 每个会话都把全部 members 扫一遍 → O(会话数 × 成员数)；
      2. `tar.extractfile(m)` 会 seek 到成员的数据偏移，而 gzip 流向后 seek
         需要从头解压；成员按字典序访问时反复回退，累计 O(n²)。
    按 `offset_data` 单调递增读取，gzip 只需单趟解压。

    返回 `(projects_by_sid, aux_by_sid)`，值为 `[(arc, TarInfo), ...]`。
    归属规则与 `_planned_files()` / 主循环一致（`sid` / `sid.xxx` / `sid/xxx`）。
    """
    projects = {}
    aux = {}
    for arc, m in members.items():
        if arc.startswith("projects/"):
            parts = arc[len("projects/"):].split("/", 1)
            if len(parts) < 2:
                continue
            base = parts[1]
            # session id 允许含点（合法 id 字符集收 `.`），只按第一个点切分
            # 会把 `abc.def.jsonl` 归到 `abc`，漏掉 sid=`abc.def` 的会话。
            # 按 `.` / `/` 边界逐级注册前缀；同一成员会进多个组，但
            # `_planned_files()` 的精确过滤保证每个 sid 只写自己的文件。
            prefixes = {base[:i] for i, ch in enumerate(base) if ch in "./"}
            prefixes.add(base)
            for key in prefixes:
                if key:
                    projects.setdefault(key, []).append((arc, m))
        elif arc.startswith("todos/"):
            name = arc[len("todos/"):]
            if name.endswith(".json"):
                aux.setdefault(name[:-5], []).append((arc, m))
        elif arc.startswith("tasks/"):
            key = arc[len("tasks/"):].split("/", 1)[0]
            if key:
                aux.setdefault(key, []).append((arc, m))
    for group in (projects, aux):
        for pairs in group.values():
            pairs.sort(key=lambda kv: kv[1].offset_data)
    return projects, aux


def plan_in_tar_order(plan, projects_by_sid, aux_by_sid):
    """把会话按「首个成员在包内的偏移」排序，避免会话之间来回 seek。

    没有正文成员的会话排最后（本来就不读 tar）。只改变处理顺序，
    不改变每条会话的 action / created_files 语义。
    """
    def first_offset(item):
        row = item[0] if isinstance(item[0], dict) else {}
        sid = row.get("id") or ""
        vals = [m.offset_data for _a, m in projects_by_sid.get(sid, ())]
        vals += [m.offset_data for _a, m in aux_by_sid.get(sid, ())]
        return min(vals) if vals else float("inf")
    return sorted(plan, key=first_offset)


def _planned_files(projects_members, aux_members, sid: str, dst_slug: str,
                   ep: EditionPaths) -> list:
    """预先算出本次会话会写哪些相对路径 → [(kind, rel, existed_before)]

    回滚按 `meta["applied"][].created_files` 删文件，所以必须在落盘【之前】登记：
    中途失败时，已写盘的文件才不会被漏掉（C8 教训：先登记再继续）。

    existed_before 是安全阀：覆盖前就存在的文件**不能直接删** —— 如果 stash 备份
    失败，它就是唯一副本，删掉等于永久丢失。这类文件只靠 stash 还原。
    """
    bases = _kind_bases(ep)
    out = []
    for arc, _m in projects_members:
        rel = arc[len("projects/"):]
        parts = rel.split("/", 1)
        if len(parts) < 2:
            continue
        base = parts[1]
        if not (base == sid or base.startswith(sid + ".")
                or base.startswith(sid + "/")):
            continue
        dst_rel = f"{dst_slug}/{base}"
        out.append(("projects", dst_rel, (ep.projects_dir / dst_rel).exists()))
    for arc, _m in aux_members:
        if arc == f"todos/{sid}.json":   # 精确匹配：<sid>.json.bak 不算
            rel = f"{sid}.json"
            out.append(("todos", rel, (bases["todos"] / rel).exists()))
        elif arc.startswith(f"tasks/{sid}/"):
            rel = arc[len("tasks/"):]
            out.append(("tasks", rel, (bases["tasks"] / rel).exists()))
    return out


def _prune_empty_dirs(d: Path, stop: Path):
    """删完文件后自底向上清空目录（tool-results 这类目录是隐式创建的）"""
    while d != stop and str(d).startswith(str(stop)):
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent


def _cleanup_failed_session(ep: EditionPaths, item: dict, backup_dir: Path = None):
    """会话导入失败后清掉它已写入的文件

    与回滚同一口径：**覆盖前就存在的文件不能直接删** —— stash 失败时它就是唯一
    副本，删掉等于永久丢失。这类文件只从 overwritten/ 还原，没有备份就保持现状。
    """
    bases = _kind_bases(ep)
    preexisting = {(k, r) for k, r in (item.get("preexisting") or [])}
    failed = 0
    for kind, rel in item.get("created_files", []):
        base = bases.get(kind)
        if base is None or not _safe_rel(rel):
            continue
        dst = base / rel
        if (kind, rel) in preexisting:
            src = (backup_dir / "overwritten" / item["id"] / kind / rel
                   if backup_dir is not None else None)
            if src is not None and src.exists():
                if not copy_path(src, dst):
                    print(f"  ⚠️  还原 {rel} 失败（备份在 {src}，可手动复制回去）")
                    failed += 1
            continue
        if not remove_path(dst):
            print(f"  ⚠️  无法删除 {rel}（可能被占用）")
            failed += 1
        else:
            _prune_empty_dirs(dst.parent, base)
    # 覆盖/导入时删掉的旧 slug 文件也要还原（它们在 created_files 之外）
    for kind, rel in (item.get("deleted_old_slug") or []):
        base = bases.get(kind)
        src = (backup_dir / "overwritten" / item["id"] / kind / rel
               if backup_dir is not None else None)
        if base is not None and src is not None and src.exists():
            if not copy_path(src, base / rel):
                print(f"  ⚠️  还原旧 slug 文件 {rel} 失败（备份在 {src}）")
                failed += 1
    return failed


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
            try:
                size = fmt_size(p.stat().st_size)
            except OSError:
                size = "?"
            print(f"  {i}. {p.name}  （{size}，{p.parent}）")
        print(f"  m. 手动输入路径（可把包文件从访达拖进本窗口）")
        try:
            choice = input("请选择要导入的包（输入序号）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if choice.lower() in ("m", "手动"):
            try:
                raw = input("包文件路径: ").strip().strip('"').strip("'")
            except (EOFError, KeyboardInterrupt):
                return None
            return Path(raw).expanduser() if raw else None
        try:
            n = int(choice)
        except ValueError:
            return None
        # 显式范围检查：输入 0 时 found[-1] 会静默选中最后一个包
        if not 1 <= n <= len(found):
            return None
        return found[n - 1]
    print(f"\n未在 当前目录 / 桌面 / 下载 里找到 {PKG_EXT} 迁移包。")
    try:
        raw = input("请输入包文件路径（可把文件从访达拖进本窗口）: ").strip().strip('"').strip("'")
    except (EOFError, KeyboardInterrupt):
        return None
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

    # 同 do_export：统一解析（含 resolve），否则 --dir ./data 会被误报为「库不可读」
    ep = _edition_from_args(args)

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
    # 损坏/手工构造的包：顶层不是数组、数组里有 null/字符串时，
    # 后面的 .get(...) / int(...) 会在逐会话 try 之外抛 AttributeError/TypeError
    if not isinstance(sessions_data, list):
        sessions_data = []
    if not isinstance(usage_data, list):
        usage_data = []
    sessions_data = [r for r in sessions_data if isinstance(r, dict)]
    usage_data = [u for u in usage_data if isinstance(u, dict)]
    if not sessions_data:
        print("❌ 包内没有会话数据")
        return 1
    src_cwd = manifest.get("source_cwd") or ""
    if not src_cwd:
        print("❌ manifest 缺少 source_cwd")
        return 1

    if getattr(args, "target_uid", None):
        # 登录态快照缺失时，用户可以显式指定账号（不必用 --yes 跳过所有确认）。
        # 但显式值拼错会把整包会话挂到不存在的账号下，必须做形态校验 + 确认。
        _t = str(args.target_uid)
        if not _looks_like_uid(_t):
            print(f"⚠️  --target-uid 不像一个 WorkBuddy user_id（应为 UUID 形态）：{_t}")
            print("   拼错会把整批会话挂到不存在的账号下，表现是「导入成功但对话消失」。")
            if not getattr(args, "yes", False):
                if not is_interactive() or not ask_yes_no("   仍要继续导入？(y/N)", default=False):
                    print("已取消")
                    return 1
        target_uid, uid_src = _t, "显式指定（--target-uid）"
    else:
        target_uid, uid_src = get_current_uid(ep)
    if not target_uid:
        print("❌ 无法确定目标机当前登录账号（account-snapshot.json 缺失且 DB 无会话）")
        print("   请先在 WorkBuddy 里登录账号并至少开始过一个对话，再运行导入。")
        return 1
    if uid_src == "db-majority":
        # 与 migrate.py / migrate_session.py 的弱推断拦截对齐：典型机器上 session
        # 最多的恰恰是旧账号，拿它当目标 = 导入后对话在当前账号下看不见
        print("  ⚠️  目标账号是**从 DB 里 session 数最多反推**的（登录态文件没读到）。")
        print("     这种推断最容易落到旧账号上：导入后对话挂在它名下，客户端里就看不见了。")
        print("     建议先确认该版本已登录过，或在客户端里切换一次账号后重试。")
        if args.dry_run:
            # dry-run 只预览不写盘，不该被账号可靠性拦住（交互模式下也一样）
            print("   （dry-run 只预览、不写盘，继续）")
        elif is_interactive() and not getattr(args, "yes", False):
            if not ask_yes_no("   仍然继续导入？(y/N)", default=False):
                print("已取消")
                return 1
        elif not getattr(args, "yes", False):
            # 非交互 + 账号是猜的 + 没有显式 --yes：不能默默按猜测账号写库
            print("❌ 目标账号是从 DB 反推的（不可靠），非交互模式必须显式加 --yes 确认；")
            print("   更稳的做法是先在客户端里登录一次，让 account-snapshot.json 存在。")
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
        raw = ""
        try:
            raw = input("请输入项目在【本机】的路径（目录必须已存在；可把项目文件夹从访达拖进本窗口）: ").strip()
        except EOFError:
            # stdin 被重定向 / 关闭时不能抛 traceback
            print("\n❌ 非交互环境无法输入路径，请用 --cwd 指定")
            return 1
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
            # 已软删除的行不参与软冲突判定：本地删掉的同名对话不该把包里的
            # 会话整条跳过（导出侧也过滤了 deleted_at，两边口径要对齐）
            if r.get("deleted_at"):
                continue
            # 客户端显示的是 custom_title（用户改过的标题），软冲突必须按它比对：
            # 只比 title 会出现「显示同名却判不出软冲突」→ 重复导入两条同标题对话
            key = str(r.get("custom_title") or r.get("title") or "")
            if not key:
                # 空标题不进索引：否则所有无标题会话互为「同名」，成批误判软冲突
                continue
            local_titles.setdefault(key, []).append(r)
    finally:
        conn.close()

    on_conflict = args.on_conflict
    plan = []  # (row, action, note)
    skip_n = 0
    bad_rows = 0   # 包内结构不合法的行（缺 id / 含非法字符 / 非对象）
    seen_ids = set()   # 包内重复 id：第二条会覆盖 stash 里的「导入前原件」
    for row in sessions_data:
        if not isinstance(row, dict):
            bad_rows += 1
            continue
        sid = row.get("id")
        if not _safe_sid(sid):
            # 畸形/恶意包：缺 id 会在旧代码里 KeyError 裸抛；id 含 ../ 或 * 会被
            # 拼进 rglob/glob 越出数据目录，必须在进入计划前挡掉
            print(f"  ⚠️  包内有一条会话的 id 缺失或含非法字符，已跳过：{str(sid)[:40]!r}")
            bad_rows += 1
            continue
        if sid in seen_ids:
            print(f"  ⚠️  包内有重复的 session id {sid[:8]}…，已跳过（保留第一条）")
            skip_n += 1
            continue
        seen_ids.add(sid)
        if sid in local_rows:
            local = local_rows[sid]
            pkg_newer = _to_int(row.get("last_activity_at")) > _to_int(local.get("last_activity_at"))
            action = None
            if on_conflict == "overwrite":
                action = "overwrite"
            elif on_conflict == "skip":
                action = "skip"
            elif is_interactive() and not getattr(args, "yes", False):
                print(f"\n⚠️  目标已存在相同 id 的对话：【{clip(local.get('title') or '(无标题)', 40)}】")
                print(f"    包内最后活动 {fmt_time(row.get('last_activity_at'))} / "
                      f"本地最后活动 {fmt_time(local.get('last_activity_at'))}")
                rec = "覆盖（包内更新）" if pkg_newer else "跳过（本地更新）"
                print(f"    建议：{rec}")
                try:
                    a = input("    [o] 覆盖 / [s] 跳过: ").strip().lower()
                except EOFError:
                    a = ""
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
            # 空标题不参与软冲突判定：否则本地只要有一条无标题会话，包内所有
            # 无标题会话都会被判成「同名」而静默跳过（客户端自动生成标题失败时很常见）
            title_key = str(row.get("custom_title") or row.get("title") or "")
            same_title = local_titles.get(title_key, []) if title_key else []
            if same_title:
                note = f"软冲突：本地已有同标题对话 x{len(same_title)}"
                if on_conflict == "overwrite":
                    # --on-conflict overwrite 的语义是「遇到冲突就导入」。软冲突 id 不同，
                    # 不存在覆盖问题，直接 insert（不删本地那条同名对话）。
                    plan.append((row, "insert", note + "，按 --on-conflict overwrite 导入"))
                    continue
                if (is_interactive() and on_conflict == "ask"
                        and not getattr(args, "yes", False)):
                    print(f"\n⚠️  本地已有【同标题不同 id】的对话：{clip(row.get('title') or '(无标题)', 40)}")
                    try:
                        a = input("    [i] 仍导入（会出现两条同标题对话） / [s] 跳过: ").strip().lower()
                    except EOFError:
                        a = ""
                    if a in ("i", "import"):
                        plan.append((row, "insert", note + "，用户选择仍导入"))
                    else:
                        skip_n += 1
                    continue
                # 非交互 / 显式 skip：一律跳过（重复迁移会在客户端出现两条同标题对话，必须拦）
                print(f"ℹ️  {sid[:8]}… {note}，跳过（交互模式可询问处理）")
                skip_n += 1
            else:
                plan.append((row, "insert", ""))

    if not plan and args.dry_run:
        # 畸形包是错误，不是「没东西可导」
        if bad_rows:
            print(f"\n❌ 包内 {bad_rows} 条会话数据无效，预览中止")
            return 1
        print("\n没有需要导入的会话（全部跳过）。")
        return 0

    # 预分组 + 按包内偏移排序：主循环不再扫全量成员，tar 读取顺序单调，
    # gzip 单趟解压。放在计划摘要打印之前，保证日志顺序与实际处理顺序一致。
    projects_by_sid, aux_by_sid = group_package_members(members)
    plan = plan_in_tar_order(plan, projects_by_sid, aux_by_sid)

    print(f"\n导入计划：{len(plan)} 个会话（覆盖 {sum(1 for _, a, _ in plan if a == 'overwrite')} / "
          f"新增 {sum(1 for _, a, _ in plan if a == 'insert')}），跳过 {skip_n}")
    for row, action, note in plan[:10]:
        mark = "🔄" if action == "overwrite" else "➕"
        extra = f"  {note}" if note else ""
        print(f"  {mark} {clip(row.get('title') or '(无标题)', 38)}  {fmt_time(row.get('last_activity_at'))}{extra}")
    if len(plan) > 10:
        print(f"  …等 {len(plan)} 个")

    if args.dry_run:
        if bad_rows:
            print(f"\n❌ 包内 {bad_rows} 条会话数据无效，预览中止")
            return 1
        print("\n✅ dry-run 结束，未写入任何数据")
        return 0

    # 客户端必须关闭（写操作）。assume_closed 必须一起透传：
    # 漏传时 --assume-clients-closed 在 import 路径完全无效，而错误提示恰恰让用户加它
    if not require_clients_closed(
            force=args.force,
            assume_closed=getattr(args, "assume_clients_closed", False)):
        return 2

    if not plan:
        # 放在守卫之后：客户端开着时「全部跳过」应报 2（被守卫拦），而不是 3（未做改动）
        if bad_rows:
            print(f"\n❌ 包内 {bad_rows} 条会话数据无效，导入中止")
            return 1
        print("\n没有需要导入的会话（全部跳过）。")
        return 3

    # 交互模式：写入前最后确认（这就是小白的 dry-run 替代品，计划已在上面展示）
    if is_interactive() and not getattr(args, "yes", False):
        if not ask_yes_no("\n确认按上述计划导入？", default=True):
            print("已取消，未做任何修改")
            # 非零退出：自动化要能区分「用户取消」与「导入成功」
            return 1

    # 备份。tag 精确到秒，同一秒内两次导入会撞名 —— 必须加序号避让，
    # 否则后一次会覆盖前一次的整库快照与 meta.json（前一个回滚点就没了）
    base_tag = datetime.now().strftime("%Y%m%d%H%M%S") + "_project-import"
    tag = base_tag
    seq = 2
    while (ep.backup_dir / tag).exists():
        tag = f"{base_tag}-{seq}"
        seq += 1
    backup_dir = ep.backup_dir / tag
    try:
        backup_dir.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        print(f"❌ 无法创建备份目录（{e}），中止导入")
        return 1
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
        "applied": [],   # 每个已执行会话一条：{id, action, created_files: [rel], status}
        "workspace_merged": [],
    }

    dst_slug = cwd_to_slug(new_cwd)
    if not dst_slug:
        print("❌ 无法从新路径推导 projects 子目录名")
        shutil.rmtree(str(backup_dir), ignore_errors=True)
        return 1

    def save_meta():
        # 原子写：meta.json 是回滚的唯一依据，写到一半中断就成了损坏文件
        tmp = backup_dir / "meta.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(backup_dir / "meta.json"))

    def save_meta_safe() -> bool:
        """循环内用：返回是否写成功

        成功路径上失败只告警（库已提交，删文件反而更糟）；但**会话开始时的预登记**
        必须成功——meta 里没有这条，回滚就删不到它写的文件。
        """
        try:
            save_meta()
            return True
        except OSError as e:
            print(f"  ⚠️  meta.json 更新失败（{e}）：回滚记录可能不完整")
            return False

    try:
        save_meta()
    except OSError as e:
        # 备份目录里已有整库快照但没有 meta.json —— 这种备份对 --backups /
        # --rollback 双向不可见，等于白占空间，直接清掉并中止
        print(f"❌ 无法写入备份清单（{e}）：{backup_dir}")
        print("   导入已中止，未改动任何数据。")
        shutil.rmtree(str(backup_dir), ignore_errors=True)
        return 1

    ok_n, fail_n = 0, bad_rows   # 计划阶段就判为无效的行也算失败
    conn = connect_rw(ep.db)
    try:
        cur = conn.cursor()
        s_cols = table_columns(conn, "sessions")
        u_cols = table_columns(conn, "session_usage")
        if not u_cols:
            print("⚠️  目标库没有 session_usage 表：usage 统计不会导入（会话本身不受影响）")
        if not s_cols:
            print("❌ 目标库 sessions 表无法读取")
            # 备份目录和 meta.json 已经建好，直接 return 会在 --backups 里留一条
            # applied: [] 的空备份，误导用户以为那次动过数据
            shutil.rmtree(str(backup_dir), ignore_errors=True)
            return 1

        # 成员已在计划阶段按 sid 分组并按 offset_data 排序（见 group_package_members），
        # 这里直接取本会话的子列表：不再扫全量成员，tar 读取顺序单调递增。
        for row, action, _note in plan:
            sid = row.get("id")
            if not sid:
                # 畸形包可能缺 id：以前这行在 try 外，KeyError 会直接甩 traceback
                print("  ❌ 包内有一条会话缺少 id，已跳过")
                fail_n += 1
                continue
            # 先登记再落盘：中途失败时回滚才知道要删哪些文件。
            # 以前 created_files 只在该会话全部成功后才写进 meta —— 第 2 个成员
            # 失败时，第 1 个已写盘的文件既不在 meta 里也没删，回滚删不掉（孤儿正文）。
            sid_projects = projects_by_sid.get(sid, ())
            sid_aux = aux_by_sid.get(sid, ())
            created_files = _planned_files(sid_projects, sid_aux,
                                           sid, dst_slug, ep)
            item = {
                "id": sid, "action": action,
                "created_files": [[k, r] for k, r, _ in created_files],
                # 覆盖前就存在的路径：回滚时只靠 stash 还原，绝不直接删
                "preexisting": [[k, r] for k, r, existed in created_files if existed],
                "status": "in_progress",
            }
            meta["applied"].append(item)
            if not save_meta_safe():
                # 回滚清单写不进去 = 这个会话写下的文件将来删不掉，不能开始写
                meta["applied"].pop()
                fail_n += 1
                print(f"  ❌ {sid[:8]}… 无法写入回滚清单，跳过该会话（未改动任何文件）")
                continue
            try:
                # insert 也可能撞上目标侧残留的同名文件（库被重建 / 清空过，
                # 库里没有对应行但磁盘上还有正文）。一律先备份再写：
                # 否则下面的 remove_path 会无备份删掉它，回滚也还原不回来。
                pending_del = []
                for saved in stash_session_files(ep, sid, backup_dir):
                    # 旧 slug 下的同名文件要删掉：只写新 slug 的话，同一 session 的
                    # jsonl 会同时存在于两个 projects/*/ 目录，客户端可能读到陈旧副本。
                    # insert 同样要删（库里没行、磁盘上还留着旧 slug 正文的场景）。
                    if (saved["kind"] == "projects"
                            and not saved["rel"].startswith(dst_slug + "/")):
                        pending_del.append(["projects", saved["rel"]])
                if pending_del:
                    # 先登记再删：登记失败时该会话失败后就没法即时还原，
                    # 至少要让用户知道（数据仍在 overwritten/ 里，整库回滚能救）
                    item["deleted_old_slug"] = pending_del
                    if not save_meta_safe():
                        print("  ⚠️  无法登记旧 slug 清理记录："
                              "该会话失败时只能靠 --rollback 整库还原")
                    for kind, rel in pending_del:
                        if not remove_path(ep.projects_dir / rel):
                            print(f"  ⚠️  无法删除旧 slug 文件 {rel}"
                                  "（可能被占用；新旧正文会同时存在，回滚不受影响）")

                # 1. sessions 行：user_id / cwd 改写，列名对齐插入（版本漂移是现实：43 列 vs 30 列）
                new_row = _revive_blobs(dict(row))
                new_row["user_id"] = target_uid
                new_row["cwd"] = new_cwd
                if action == "overwrite":
                    # 目标表比包里多出来的列，用目标原值填回去。
                    # INSERT OR REPLACE 是「先删后插」，不填就会被重置成 NULL/默认值
                    # （跨版本导入时静默丢字段，事后极难发现）。
                    old = local_rows.get(sid) or {}
                    for c in s_cols:
                        if c not in new_row and c in old:
                            new_row[c] = old[c]
                cols = [c for c in new_row.keys() if c in s_cols]
                cur.execute(
                    f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [new_row[c] for c in cols],
                )

                # 2. usage 行
                urow = next((u for u in usage_data if u.get("session_id") == sid), None)
                if urow and u_cols:
                    urow = _revive_blobs(dict(urow))
                    ucols = [c for c in urow.keys() if c in u_cols]
                    cur.execute(
                        f"INSERT OR REPLACE INTO session_usage ({','.join(ucols)}) "
                        f"VALUES ({','.join('?' * len(ucols))})",
                        [urow[c] for c in ucols],
                    )

                # 3. workspaces 登记（新路径）。注意：真实库的 workspaces 表
                #    没有 UNIQUE 约束，INSERT OR IGNORE 拦不住重复导入，
                #    先删后插保证一个路径只有一行；两者放同一 SAVEPOINT，
                #    INSERT 失败时不能把已删掉的原记录也丢掉。
                cur.execute("SAVEPOINT ws_upsert")
                try:
                    # 先读原值：直接覆盖会把目标侧真实打开时间改成源会话的最后活动时间，
                    # 工作区列表排序就失真了（取两者较晚的那个）
                    # 表允许同一 path 多行，必须取 MAX 而不是第一行，
                    # 否则会把时间更新的其他记录一起删掉
                    old_ws = cur.execute(
                        "SELECT MAX(last_opened_at) FROM workspaces WHERE path = ?", (new_cwd,)
                    ).fetchone()
                    ts = max(_to_int(row.get("last_activity_at")),
                             _to_int(old_ws[0] if old_ws else 0)) or int(time.time() * 1000)
                    cur.execute("DELETE FROM workspaces WHERE path = ?", (new_cwd,))
                    cur.execute(
                        "INSERT INTO workspaces (path, last_opened_at) VALUES (?, ?)",
                        (new_cwd, ts),
                    )
                    cur.execute("RELEASE ws_upsert")
                except Exception as e:
                    try:
                        cur.execute("ROLLBACK TO ws_upsert")
                        cur.execute("RELEASE ws_upsert")
                    except sqlite3.Error:
                        pass
                    # 登记失败 → 客户端可能找不到该工作目录，不能静默
                    print(f"  ⚠️  workspaces 登记失败（{e}）：客户端可能找不到 {new_cwd}")

                # 4. 正文与附属文件：projects/{src_slug}/… → projects/{dst_slug}/…
                for arc, m in sid_projects:
                    rel = arc[len("projects/"):]
                    parts = rel.split("/", 1)
                    if len(parts) < 2:
                        continue
                    # 只搬运本会话名下的成员。必须按「等于 sid / sid.xxx / sid/xxx」过滤：
                    # 只用 startswith 时，id 互为前缀的另一个会话会被一起搬走；
                    # 而漏掉 sid/ 又会把 tool-results 等附属目录整体丢掉。
                    base = parts[1]
                    if not (base == sid or base.startswith(sid + ".")
                            or base.startswith(sid + "/")):
                        continue
                    dst_rel = f"{dst_slug}/{base}"
                    dst = ep.projects_dir / dst_rel
                    if dst.exists():
                        remove_path(dst)
                    stream_member_to_file(tar, m, dst)
                    # jsonl 顶层 cwd 字段改写（流式，二次写回）
                    if base == f"{sid}.jsonl":
                        tmp = dst.with_suffix(".jsonl.tmp")
                        # surrogateescape：非 UTF-8 字节被 replace 成 U+FFFD 写回就是
                        # 静默篡改用户正文（不可逆）；newline="" 防止 Windows 把 LF 全转 CRLF
                        with open(dst, "r", encoding="utf-8", errors="surrogateescape",
                                  newline="") as fin, \
                             open(tmp, "w", encoding="utf-8", errors="surrogateescape",
                                  newline="") as fout:
                            rewrite_jsonl_cwd(fin, fout, row.get("cwd") or src_cwd, new_cwd)
                        os.replace(str(tmp), str(dst))

                # 5. todos / tasks
                for arc, m in sid_aux:
                    if arc == f"todos/{sid}.json":
                        dst = ep.todos_dir / f"{sid}.json"
                        if dst.exists():
                            remove_path(dst)
                        stream_member_to_file(tar, m, dst)
                    elif arc.startswith(f"tasks/{sid}/"):
                        rel = arc[len("tasks/"):]
                        dst = ep.tasks_dir / rel
                        if dst.exists():
                            remove_path(dst)
                        stream_member_to_file(tar, m, dst)

                conn.commit()
                item["status"] = "done"
                save_meta_safe()
                ok_n += 1
                print(f"  ✅ {clip(row.get('title') or sid[:8], 36)}")
            except sqlite3.Error as e:
                conn.rollback()
                bad = _cleanup_failed_session(ep, item, backup_dir)
                item["status"] = "failed"
                save_meta_safe()
                fail_n += 1
                print(f"  ❌ {sid[:8]}… 写库失败：{e}")
                if bad:
                    print(f"     已清理本次写入的文件，{bad} 项未完成（见上）；"
                          "可用 --rollback 恢复到导入前状态")
                else:
                    print("     已清理本次写入的文件（覆盖前已有的已从备份还原）；"
                          "可用 --rollback 恢复到导入前状态")
            except (OSError, ValueError, KeyError, tarfile.TarError, RuntimeError) as e:
                conn.rollback()
                # stash 失败（RuntimeError）只应跳过这一条：其余会话仍可继续。
                # 不捕获的话整批导入会中止，且 item 停在 in_progress。
                bad = _cleanup_failed_session(ep, item, backup_dir)
                item["status"] = "failed"
                save_meta_safe()
                fail_n += 1
                print(f"  ❌ {sid[:8]}… 文件/数据错误：{e}")
                if bad:
                    print(f"     已清理本次写入的文件，{bad} 项未完成（见上）；"
                          "可用 --rollback 恢复到导入前状态")
                else:
                    print("     已清理本次写入的文件（覆盖前已有的已从备份还原）；"
                          "可用 --rollback 恢复到导入前状态")

        checkpoint_failed = not wal_checkpoint(conn)
    finally:
        conn.close()
        # .tmp 只是 stash 的中转站：留着的话，硬杀时残留的正文副本
        # 既不在回滚扫描范围、也不在 --backups 展示范围
        shutil.rmtree(str(backup_dir / ".tmp"), ignore_errors=True)

    # 6. 工作区记忆合并（追加式文本，不回滚——与账号级 Memory 同一哲学）
    if manifest.get("include_workspace") and ok_n > 0:
        # 一条会话都没成功时不要写工作区记忆：它不参与回滚，写了就撤不掉
        merged = 0
        for arc, m in sorted(members.items(), key=lambda kv: kv[1].offset_data):
            if not arc.startswith("workspace/.workbuddy/memory/"):
                continue
            rel = arc[len("workspace/"):]
            dst = Path(new_cwd) / rel
            if dst.exists():
                try:
                    existing = dst.read_text(encoding="utf-8", errors="surrogateescape")
                    _fh = tar.extractfile(m)
                    if _fh is None:
                        raise OSError(f"包内成员无法读取：{m.name}")
                    with _fh:
                        incoming = _fh.read().decode("utf-8", errors="surrogateescape")
                    # set 移出推导式：写在里面会逐行重建，文件大时退化成 O(n²)
                    existing_lines = set(existing.split("\n"))
                    new_lines = [l for l in incoming.split("\n") if l not in existing_lines]
                    if new_lines:
                        with open(dst, "a", encoding="utf-8", errors="surrogateescape") as f:
                            f.write("\n" + "\n".join(new_lines))
                        merged += 1
                except OSError as e:
                    # 以前是 except OSError: pass —— 合并失败完全无提示
                    print(f"  ⚠️  合并工作区记忆 {dst.name} 失败：{e}")
            else:
                try:
                    stream_member_to_file(tar, m, dst)
                    merged += 1
                except OSError as e:
                    print(f"  ⚠️  写入工作区记忆 {dst.name} 失败：{e}")
        for arc, m in sorted(members.items(), key=lambda kv: kv[1].offset_data):
            if arc.startswith("workspace/.workbuddy/skills/"):
                rel = arc[len("workspace/"):]
                dst = Path(new_cwd) / rel
                if not dst.exists():
                    try:
                        stream_member_to_file(tar, m, dst)
                    except OSError as e:
                        print(f"  ⚠️  复制项目技能 {dst.name} 失败：{e}")
        if merged:
            meta["workspace_merged"] = [new_cwd]
            save_meta_safe()
            print(f"\n📎 工作区记忆已合并到 {Path(new_cwd) / '.workbuddy'}（{merged} 个文件，不参与回滚）")

    # 验证：只统计本次确实写入成功的会话（以前把失败 / 跳过的也算进「验证命中」）
    hit = 0
    conn = connect_ro(ep.db)
    verified = conn is not None
    if conn is None:
        print("  ⚠️  无法以只读方式打开目标库，跳过导入结果验证")
    else:
        try:
            for item in meta.get("applied", []):
                if item.get("status") != "done":
                    continue
                n = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE id = ? AND user_id = ?",
                    (item["id"], target_uid),
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
    if verified and hit != ok_n:
        # 没验证成（verified=False）时 hit 恒为 0，不能把一次成功的导入
        # 报成「验证不一致」并让用户去 --rollback
        print(f"  ⚠️  事后验证命中 {hit} 条，与成功写入的 {ok_n} 条不一致；"
              "请用 --rollback 检查并恢复")
        return 1
    if checkpoint_failed:
        print("  ⚠️  WAL checkpoint 未完成（写入已提交，但未截断 WAL）；"
              "建议关闭客户端后重跑，或用上面的 --rollback 标签核对")
    if fail_n:
        # 部分会话写库失败属于业务性错误 → 1。2 已被「客户端未关闭」占用，
        # 混用会让自动化把导入失败误判成「客户端没关」。
        return 1
    # 走到这里说明至少有一个会话成功；checkpoint 未落盘要如实反映在退出码里
    return 1 if checkpoint_failed else 0


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
            if not isinstance(meta, dict):
                continue
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
    # 标签来自命令行，直接拼进 backup_dir 会被用于删除/写回的目标推导 ——
    # 必须限制在备份目录内（另两个脚本都有同等检查）
    if not _validate_backup_tag(tag):
        print(f"❌ 备份标签不合法：{tag!r}")
        print("   标签只能是备份目录名，不能包含路径分隔符 / .. / 盘符")
        return 1
    ep = _edition_from_args(args)
    backup_dir = ep.backup_dir / tag
    meta_f = backup_dir / "meta.json"
    if not meta_f.exists():
        print(f"❌ 备份不存在或缺少 meta.json：{tag}")
        return 1
    try:
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"❌ 备份 meta.json 无法解析（{e}），无法回滚：{meta_f}")
        return 1
    if not isinstance(meta, dict):
        print(f"❌ 备份 meta.json 顶层不是对象，无法回滚：{meta_f}")
        return 1
    if meta.get("kind") != "project_import":
        print("❌ 不是本工具创建的备份，请用对应工具回滚")
        return 1

    if not require_clients_closed(force=args.force,
                                  assume_closed=getattr(args, "assume_clients_closed", False)):
        return 2

    print(f"回滚目标：{tag}")
    print(f"  导入路径：{meta.get('new_cwd')}")
    print(f"  已执行会话：{len(meta.get('applied') or [])}")
    print("  ⚠️  回滚会把【整个数据库】还原到导入前的快照：")
    print("     导入之后新增或修改的其他会话、任务关联也会一并丢失。")
    if not args.yes and not ask_yes_no(
            "确认回滚？（本次导入的会话会被移除/还原）"):
        print("已取消")
        # 非零退出：自动化要能区分「用户取消」与「回滚成功」
        return 1

    db_backup = backup_dir / "workbuddy.db"
    if not db_backup.exists():
        print("❌ 备份缺少数据库快照，无法回滚")
        return 1

    # 1. 还原数据库快照。
    #    目标库可能残留 -wal/-shm，陈旧 WAL 会在新连接上被重放，把刚还原的快照
    #    又覆盖回导入后的状态（等于白滚）。但**不能直接删**：snapshot_db 失败时
    #    「WAL 已删、库没还原」= 永久丢失未落盘数据。先移走，失败再放回去。
    sidecars = []
    for suffix in ("-wal", "-shm"):
        p = Path(str(ep.db) + suffix)
        if not p.exists():
            continue
        tmp = backup_dir / f"rollback-sidecar{suffix}"
        try:
            shutil.move(str(p), str(tmp))
            sidecars.append((tmp, p))
        except OSError as e:
            # 已经移走的必须放回去：只移走 -wal 就中止的话，主库看不到 WAL 里
            # 已提交但未 checkpoint 的数据，等于丢数据
            failed_back = []
            for tmp, prev in sidecars:
                try:
                    shutil.move(str(tmp), str(prev))
                except OSError as be:
                    failed_back.append(f"{prev.name}：{be}")
            print(f"❌ 无法移走 {p.name}（{e}），中止回滚。")
            if failed_back:
                print("   ⚠️  以下边车未能放回，数据库可能缺少未落盘的数据：")
                for f in failed_back:
                    print(f"      · {f}")
            else:
                print("   已把移走的 -wal/-shm 放回原位。")
            return 1
    try:
        snapshot_db(db_backup, ep.db)
    except Exception as e:
        failed_back = []
        for tmp, p in sidecars:
            try:
                shutil.move(str(tmp), str(p))
            except OSError as be:
                failed_back.append(f"{p.name}：{be}")
        print(f"❌ 还原数据库快照失败（{e}）。")
        if failed_back:
            print("   ⚠️  以下边车未能放回，数据库可能缺少未落盘的数据：")
            for f in failed_back:
                print(f"      · {f}")
        else:
            print("   已把移走的 -wal/-shm 放回原位。")
        return 1
    for tmp, _ in sidecars:
        remove_path(tmp)

    # 2. 还原被覆盖的文件、删除新创建的文件
    restored = removed = 0
    problems = []
    bases = _kind_bases(ep)
    _applied = meta.get("applied")
    if not isinstance(_applied, list):
        problems.append("备份 meta.applied 不是列表")
        _applied = []
    for item in _applied:
        if not isinstance(item, dict):
            problems.append("跳过 meta.applied 中非对象的条目")
            continue
        sid = item.get("id")
        if not _safe_sid(sid):
            problems.append(f"跳过备份里的非法会话 id：{sid!r}")
            continue
        preexisting = {(k, r) for k, r in (item.get("preexisting") or [])}
        # 删除导入产生的文件（含覆盖后写入的）
        for kind, rel in item.get("created_files", []):
            base = bases.get(kind)
            if base is None or not _safe_rel(rel):
                problems.append(f"跳过非法路径 {kind}/{rel}")
                continue
            if (kind, rel) in preexisting:
                # 覆盖前就存在的文件：直接删会把唯一副本删掉（stash 失败时）。
                # 交给下面的 overwritten/ 还原逻辑处理。
                continue
            if remove_path(base / rel):
                removed += 1
                # 顺手清掉变空的父目录（否则 {sid}/tool-results 会残留）
                _prune_empty_dirs((base / rel).parent, base)
            else:
                problems.append(f"删除 {kind}/{rel}")
        # 收尾：{sid}/ 与 {sid}/tool-results/ 是 stream_member_to_file 的
        # mkdir(parents=True) 隐式建的，不在 created_files 里；按 sid 单独清一次
        for d in bases["projects"].glob(f"*/{sid}"):
            _prune_empty_dirs(d, bases["projects"])
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
                base = bases.get(kind)
                if base is None or not _safe_rel(frel):
                    continue
                if copy_path(f, base / frel):
                    restored += 1
                else:
                    problems.append(f"还原 {kind}/{frel}")

    conn = connect_rw(ep.db)
    try:
        if not wal_checkpoint(conn):
            problems.append("WAL checkpoint 未完成（回滚结果可能尚未截断 WAL）")
    finally:
        conn.close()

    print(f"\n回滚完成：数据库已还原快照；删除导入文件 {removed} 项，还原覆盖前文件 {restored} 项")
    print("   工作区记忆的合并不参与回滚（追加式文本）。")
    print("   请重启 WorkBuddy 客户端。")
    if problems:
        print(f"\n  ⚠️  有 {len(problems)} 项没做到：")
        for p in problems:
            print(f"     · {p}")
        print("     关闭客户端后重跑同一条 --rollback 即可续做（已完成的部分是幂等的）")
        return 1
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
    _raw_sessions = manifest.get("sessions")
    _sess = [x for x in (_raw_sessions if isinstance(_raw_sessions, list) else [])
             if isinstance(x, dict)]
    ss = sorted(_sess, key=lambda s: _to_int(s.get("last_activity_at")), reverse=True)
    for s in ss[:15]:
        print(f"    {fmt_time(s.get('last_activity_at'))}  {clip(s.get('title') or '(无标题)', 46)}")
    if len(ss) > 15:
        print(f"    …等 {len(ss)} 个")
    return 0


# ---------------------------------------------------------------- 向导


def wizard(args=None) -> int:
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

    # 透传命令行已解析的 --dir / --intl / --force / --assume-clients-closed：
    # 以前这里写死 dir=None、intl=False，`migrate_project.py --intl` 进向导会
    # 静默按国内版默认目录跑（与 migrate.py / migrate_session.py 修掉的同类问题）
    ns = argparse.Namespace(
        dir=getattr(args, "dir", None),
        intl=getattr(args, "intl", False),
        target_uid=getattr(args, "target_uid", None),
        yes=True, cwd=None, out=None,
        no_workspace_memory=False, dry_run=False, on_conflict="ask",
        force=getattr(args, "force", False),
        assume_clients_closed=getattr(args, "assume_clients_closed", False),
        package=None, cmd="wizard",
    )
    if choice == "1":
        print()
        return do_export(ns)
    if choice == "2":
        # 导入是写操作：必须保留「写入前最后确认」（小白的 dry-run 替代品）。
        # 以前 ns 里写死 yes=True，这道闸门在向导路径上等于不存在。
        ns.yes = False
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
    # docstring 里写的是「用法（推荐小白路线…）:」，不含子串「用法:」，
    # 原来的 split("用法:") 恒不命中 → --help 永远不显示用法示例
    _usage = re.search(r"^用法[^\n]*:\s*\n(.*?)(?:\n\n|\Z)", __doc__ or "", re.S | re.M)
    ap = argparse.ArgumentParser(
        description="WorkBuddy 跨设备项目迁移（export / import）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_usage.group(1).strip() if _usage else None,
    )
    ap.add_argument("--dir", help="显式指定数据目录（优先级高于 --intl）")
    # 父解析器必须也注册：无子命令进 wizard() 时 args 由父解析器产生，
    # 只挂在 p_imp 会让 --target-uid 在向导路径恒为 None（且无子命令直接报 unrecognized）
    ap.add_argument("--target-uid", default=None,
                    help="显式指定导入目标账号 user_id（向导路径同样可用）")
    ap.add_argument("--intl", action="store_true", help="操作国际版（数据目录 ~/.workbuddy-ai）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")

    sub = ap.add_subparsers(dest="cmd")

    p_exp = sub.add_parser("export", help="导出一个项目的会话数据为迁移包")
    p_exp.add_argument("--cwd", help="项目路径（不传则交互式选择）")
    p_exp.add_argument("--out", help=f"输出包路径（默认：桌面/项目名-日期{PKG_EXT}）")
    p_exp.add_argument("--no-workspace-memory", action="store_true",
                       help="不打包 {项目}/.workbuddy/ 下的记忆与技能")
    # default=SUPPRESS：子解析器解析时会用默认值覆盖父解析器已解析的同名参数，
    # `--yes export` 会被重置成 False（--force 同理）。SUPPRESS 让它「不碰」已有值。
    p_exp.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                       help="非交互确认")
    p_exp.set_defaults(fn=cmd_export)

    p_imp = sub.add_parser("import", help="把迁移包导入本机（必须先关闭客户端）")
    p_imp.add_argument("package", nargs="?", default=None,
                       help=f"迁移包路径（{PKG_EXT}）；不传则自动发现桌面/下载/当前目录里的包")
    p_imp.add_argument("--cwd", help="项目在【本机】的新路径（目录必须已存在）")
    p_imp.add_argument("--dry-run", action="store_true", help="只预览导入计划，不写盘")
    p_imp.add_argument("--on-conflict", choices=["ask", "overwrite", "skip"], default="ask",
                       help="冲突策略：ask 询问（默认）/ overwrite 覆盖同 id 并按「仍导入」处理软冲突 / skip 跳过；无 TTY 时 ask 降级为 skip")
    p_imp.add_argument("--target-uid", default=argparse.SUPPRESS,
                       help="显式指定目标账号 user_id（登录态快照缺失时避免用 --yes 跳过所有确认）")
    p_imp.add_argument("--force", action="store_true", default=argparse.SUPPRESS,
                       help="跳过客户端关闭检测")
    p_imp.add_argument("--assume-clients-closed", action="store_true",
                       default=argparse.SUPPRESS,
                       help="检测失败时按「客户端已退出」继续（真检测到仍拦截）")
    p_imp.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                       help="非交互确认")
    p_imp.set_defaults(fn=cmd_import)

    # 父解析器的 --dir / --intl 写在子命令后会报 unrecognized arguments。
    # 两个子解析器用 SUPPRESS 接住同名参数：写在子命令后也能生效，
    # 且不会覆盖父解析器已解析的值（与 --yes / --force 同一手法）。
    # p_info 在下面才定义，且 do_info 不读 --dir/--intl，不纳入这个循环
    for _p in (p_exp, p_imp):
        _p.add_argument("--dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        _p.add_argument("--intl", action="store_true", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)

    p_info = sub.add_parser("info", help="查看迁移包内容")
    p_info.add_argument("package")
    # 子命令后的 --target-uid 也接住，交给 main() 统一给「只用于导入」的人话
    p_info.add_argument("--target-uid", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_info.set_defaults(fn=cmd_info)

    ap.add_argument("--backups", action="store_true", help="列出本项目工具的备份")
    ap.add_argument("--rollback", metavar="TAG", help="回滚一次导入")
    ap.add_argument("--yes", action="store_true", help="跳过回滚确认")
    ap.add_argument("--force", action="store_true", help="回滚时跳过客户端关闭检测")
    ap.add_argument("--assume-clients-closed", action="store_true",
                    help="检测失败时按「客户端已退出」继续（真检测到仍拦截）")

    args = ap.parse_args()

    # --target-uid 只对 import / 向导有意义；其余用法明确拒绝，不静默吞。
    # 注意：向导路径 cmd 是 None，不能写成 cmd != "import"（那会把向导一起拒掉）
    if getattr(args, "target_uid", None) and (
            args.rollback or args.backups or args.cmd in ("export", "info")):
        print("❌ --target-uid 只用于导入（import / 向导），"
              "对 export / info / --rollback / --backups 无效")
        return 1

    # 互斥检查：这些组合以前会被静默忽略（--rollback 直接把子命令吃掉）
    if args.rollback and args.cmd:
        print("❌ --rollback 不能与子命令（export / import / info）同时使用")
        return 1
    if args.backups and (args.rollback or args.cmd):
        print("❌ --backups 不能与其它命令同时使用")
        return 1

    if args.rollback:
        return do_rollback(args.rollback, args)
    if args.backups:
        ep = _edition_from_args(args)
        print(f"{ep.label} 备份（{ep.backup_dir}）：")
        list_backups(ep)
        return 0
    if args.cmd is None:
        if is_interactive():
            return wizard(args)
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
    except OSError as e:
        # 磁盘满 / 权限不足：同样给人话，不要甩 traceback
        print(f"\n❌ 文件操作失败：{e}")
        sys.exit(1)
    except (tarfile.TarError, ValueError, KeyError, AttributeError, TypeError) as e:
        # ValueError/KeyError 也可能来自工具自身，带上类型便于区分归因，
        # 别把编程错误一律说成「包损坏」
        print(f"\n❌ 操作失败（{type(e).__name__}）：{e}")
        print("   若正在导入，请检查迁移包是否完整；否则请把上面的类型贴到 Issue。")
        sys.exit(1)
