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
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
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

# 客户端进程检测（PROC_KEYWORDS / _is_self_process / _client_display_name /
# find_running_clients / require_clients_closed）只有 migrate.py 一份实现，
# 本脚本通过 import 复用 —— 见下方「进程检测」段落。

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


def _to_int(v, default=0) -> int:
    """宽松转 int：DB 里的时间/用量字段偶尔不是数字（写成了 ISO 串或空串），
    直接 int() 会 ValueError，在 --list 的循环里等于一条脏数据让整个命令崩掉。"""
    if v in (None, ""):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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
                elif c.get("type") in ("tool_use", "server_tool_use", "tool_result"):
                    # 工具调用块没有 text：取工具名 + 关键入参，否则"最后提问"会空白
                    name = c.get("name") or c.get("tool_name") or "工具调用"
                    detail = c.get("input")
                    if isinstance(detail, dict):
                        keys = ("command", "path", "file_path", "query", "url", "pattern")
                        hint = next(
                            (str(detail[k]) for k in keys if detail.get(k)), ""
                        )
                        detail = hint
                    tail = f"（{detail}）" if detail else ""
                    parts.append(f"{name}{tail}")
        return " ".join(p for p in parts if p)
    return ""


# ---------------------------------------------------------------- 进程检测
#
# 复用 migrate.py 的实现（见文件头「不修改 migrate.py，通过 import 复用它的函数」）。
# 这里以前抄了一份几乎逐行相同的 _is_self_process / _client_display_name /
# find_running_clients / require_clients_closed —— 两份实现各自漂移
# （连 require_clients_closed 的签名都长得不一样），而且后抄的那份没有测试覆盖。
# 现在只保留一层薄委托，进程检测逻辑只有 migrate.py 一份。


def require_clients_closed(force=False, assume_closed=False) -> bool:
    """确认客户端已关闭；检测不可信、或检测到客户端时返回 False

    与 migrate.py 的签名保持一致（多一个 assume_closed）：
    - `assume_closed=True`：进程检测**失败**（拿不到结果）时按「客户端已全部退出」
      谨慎继续；**真检测到**客户端仍在运行照样拦截
    - `force=True`：一把关掉整项检查（连真检测到也放行），风险更大
    """
    if legacy is None or not hasattr(legacy, "require_clients_closed"):
        # 拿不到 migrate.py =「完全没有检测手段」，比「检测失败」更严重：
        # 不能因为给了 --force 就直接改库删文件，至少要一次显式确认。
        print("⚠️  未找到 scripts/migrate.py，无法确认客户端是否已关闭")
        print("   这种情况下继续 = 在客户端可能持锁、WAL 未落盘的状态下改库删文件。")
        if not (force or assume_closed):
            print("   若已确认两个版本的客户端都已退出，"
                  "可用 --assume-clients-closed（或风险更大的 --force）继续。")
            return False
        # 以前这里只认 --force，却提示了 --assume-clients-closed：照提示做了照样被拦。
        # 两者语义在这个分支里其实一样（都表示"我已确认客户端退出"），
        # 但连检测手段都没有，所以仍要再确认一次。
        print(f"   已给 {'--force' if force else '--assume-clients-closed'}，"
              f"但这里连检测手段都没有，需要你再确认一次。")
        return ask_yes_no("   确认两个版本的 WorkBuddy 都已完全退出？(y/N): ")
    return legacy.require_clients_closed(force=force, assume_closed=assume_closed)


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
            # 登录态来源必须跟着本次绑定的版本走，不能沿用 import 时算好的平台路径：
            # 国际版若仍读国内版 storage.json，兜底分支会把国内 uid 当国际版当前账号写进
            # intl 库 → "迁移成功但对话消失"（正是 migrate.py 里 _storage_json_for 修的那个坑）。
            try:
                legacy.STORAGE_JSON = legacy._storage_json_for(self.root)
            except AttributeError:
                # 老版本 migrate.py 没有这个 helper：至少按目录名切断国际版
                legacy.STORAGE_JSON = (
                    None if self.root.name == ".workbuddy-ai" else legacy.STORAGE_JSON
                )
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
    \\\\server\\share\\proj
      → unc-server-share-proj        （UNC 网络盘，前缀 unc- 以免与本地目录混淆）
    """
    if not cwd:
        return ""
    s = str(cwd)
    unc = s.startswith("\\\\") or s.startswith("//")
    s = re.sub(r"^([A-Za-z]):", lambda m: m.group(1).lower(), s)
    s = s.replace("\\", "-").replace("/", "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if unc and s:
        # 去掉 UNC 前导分隔符后得到的片段，加前缀标识来源
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
    """读写连接

    ⚠️  sqlite3.connect() 对不存在的路径会**凭空创建一个空库**：目标库缺失时
    以前会建出一个空 workbuddy.db，接着报 "no such table: sessions"，被顶层
    包装成「目标库结构比源版本新」——提示完全误导，还留下一个垃圾库文件。
    精确回滚 / --full / 源侧插回都可能踩到。
    """
    if not db or not Path(db).exists():
        raise RuntimeError(
            f"数据库不存在：{db}（不会自动创建空库，请确认版本 / 数据目录选对了）"
        )
    return sqlite3.connect(str(db))



def table_columns(conn, table):
    """读取表列名

    表名会被拼进 `PRAGMA table_info(...)`（标识符没法参数化），所以这里用
    **白名单**兜住：将来若有人误把外部输入传进来，宁可报错也不要把它拼进 SQL。

    ⚠️ 用 `raise` 而不是 `assert`：`python -O` 会把 assert 整段剥掉，防护静默
    消失；而且 AssertionError 顶层不捕（只捕 RuntimeError / sqlite3.Error），
    触发时是一段 traceback 而不是可操作提示。
    """
    if table not in ("sessions", "session_usage"):
        raise ValueError(f"非预期的表名：{table!r}（只允许 sessions / session_usage）")
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _report_verify(ok_text: str, n: int, expect: int = 1) -> None:
    """打印写入验证结果，命中行数不符时明确告警

    以前这几处只把 COUNT(*) 打印出来（"验证命中 n 行"），不做任何断言：
    写入实际没生效时照样显示 ✅，用户会被误导。
    """
    if n == expect:
        print(f"  ✅ {ok_text}（验证命中 {n} 行）")
    else:
        print(f"  ⚠️  验证异常：{ok_text} —— 预期 {expect} 行，实际 {n} 行，请人工确认")


def _warn_db_unreadable(ep: "EditionPaths"):
    """只读连接打不开时，区分「库不存在」与「库存在但读不到」

    以前两种情况都返回空/None，调用方无法区分"没有对话"和"读不到库"，
    会把「客户端还开着、库被独占」误报成「这个版本没有对话」。
    """
    if ep.db.exists():
        print(f"⚠️  无法以只读方式打开 {ep.label} 数据库：{ep.db}")
        print("     可能被其他进程独占（客户端未关闭？）。下面的结果可能不完整。")
    else:
        print(f"⚠️  {ep.label} 数据库不存在：{ep.db}")


def fetch_session_row(ep: EditionPaths, session_id: str):
    """按 id（或 id 前缀）取一行 session，返回 dict；找不到返回 None"""
    conn = connect_ro(ep.db)
    if conn is None:
        _warn_db_unreadable(ep)
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None and len(session_id) >= 4:
            # 支持前缀匹配。ESCAPE 是必须的：用户粘贴的 id 里若含 % 或 _ ，
            # 不加转义会被当成通配符（不是注入风险，但会误匹配到别的对话）
            esc = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute(
                "SELECT * FROM sessions WHERE id LIKE ? ESCAPE '\\' "
                "ORDER BY last_activity_at DESC",
                (esc + "%",),
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
        _warn_db_unreadable(ep)
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


def _uid_src_text(how: str) -> str:
    """uid 来源的**用户可见**说明

    内部标签（`account-snapshot` / `db-majority` 之类）不该直接打印给用户看；
    拿得到 migrate.py 时优先用它的映射表，保证两个脚本说法一致。
    """
    if legacy is not None and hasattr(legacy, "uid_source_label"):
        return legacy.uid_source_label(how)
    # 拿不到 migrate.py 时**不要**在本地再抄一份映射表：那份表和
    # migrate.uid_source_label() 必然漂移（改一处忘一处），宁可少说。
    # 走到这里意味着 migrate.py 都找不到了，此时也本该提示用户先修环境。
    return "未知来源（未找到 scripts/migrate.py，无法确认）"


def _warn_weak_target_uid(how: str):
    """目标账号是「猜」出来的时候说清楚风险

    登录态文件（account-snapshot / 平台 storage.json）都读不到时只剩 DB 推断，
    而典型机器恰恰是**旧账号的 session 更多** —— 拿它当目标账号，对话会被挂到
    旧账号名下，UI 里就"消失"了（H1 描述的正是这条路径）。

    ⚠️ 判据必须跟 `get_current_uid()` 返回的标签一致：以前 `how` 恒为
    "legacy(...)"（DB 推断在 migrate.py 内部就返回了），这个告警一次都没触发过。
    """
    if how != "db-majority":
        return
    print("  ⚠️  目标账号是**从 DB 里 session 数最多反推**的（登录态文件都没读到）。")
    print("     这种推断最容易落到旧账号上：迁移后对话挂在它名下，客户端里就看不见了。")
    print("     建议加 --target-uid <USER_ID> 显式钉住目标账号；或先确认该版本已登录过。")


def get_current_uid(ep: EditionPaths):
    """推断某版本当前登录的 user_id

    优先级（与 migrate.py:get_current_user_id() **同一套顺序**，别再各自一套）：
      1. {root}/storage/skeleton/account-snapshot.json → primary.uid（数据目录内，天然区分版本）
      2. 平台 storage.json → genie.userId（仅国内版兜底；国际版目录不读它）
      3. 数据库中 session 数最多的 user_id（最后手段，会打印告警提示用 --target-uid 钉住）

    ⚠️ 以前这里把「DB session 数最多」排在平台 storage.json **前面**，而典型机器
    恰恰是旧账号的 session 更多 —— 跨版本迁移会直接拿它当 --target-uid，
    把对话迁到旧账号下（表现就是"迁移成功但对话消失"，即 H1 那条路径）。

    命名备注：migrate.py 里的同义函数叫 `get_current_user_id()`（无参，
    作用于模块全局路径）。两者现在共用同一套优先级，函数形态不同是按 edition
    绑定路径导致的（本脚本要同时处理两个版本）。
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

    # 2. 复用 migrate.py 那套（snapshot → 平台 storage.json（仅国内版）→ DB），
    #    并**连来源一起取回**。
    #    ⚠️ 以前这里调的是只返回 uid 的 get_current_user_id()：登录态文件都读不到时，
    #    它内部的 DB 兜底会静默返回一个 uid，来源被标成 "legacy(...)"，
    #    于是下面第 3 步（真正打 db-majority 标签的那一路）永远到不了 ——
    #    "只剩 DB 反推"这种最危险的情况反而是唯一没有告警的。
    if legacy is not None and hasattr(legacy, "_resolve_current_uid"):
        try:
            with ep.bind_legacy():
                uid, src = legacy._resolve_current_uid()
            if uid:
                return uid, src
        except Exception:
            pass

    # 3. 最后手段：拿不到 migrate.py（或它没这套解析）时自己按 DB 反推
    #    这是最容易踩坑的一路（旧账号的 session 往往更多），必须如实打上来源标签，
    #    让调用方提示用户用 --target-uid 钉住目标账号。
    conn = connect_ro(ep.db)
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT user_id, COUNT(*) c FROM sessions WHERE user_id IS NOT NULL "
                "GROUP BY user_id ORDER BY c DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return row[0], "db-majority"
        except Exception:
            pass
        finally:
            conn.close()

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
    # 不能只 glob "*/{sid}*"：id 较短或互相包含时会把别的会话文件一起带进来。
    # 会话产物只有两种形态：<sid>（tool-results 目录）与 <sid>.xxx（正文/元数据）
    hits = []
    for p in ep.projects_dir.glob(f"*/{session_id}*"):
        if p.name == session_id or p.name.startswith(session_id + "."):
            hits.append(p)
    return sorted(hits)


def path_size(p: Path) -> int:
    """统计占用空间：目录需递归累加内部文件，否则 tool-results 会被算成 0"""
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except OSError:
        return 0


def copy_path(src: Path, dst: Path):
    """复制文件或目录（目录走 copytree，目标已存在则先整体删除）

    复制完会把目标的写权限补上：`copy2` / `copytree` 会连属性一起复制，源若只读，
    产物就也是只读。备份里的只读文件在 Windows 上**删不掉**（unlink 直接
    PermissionError）——既让整个临时 fixture 无法清理（下一次跑测试卡在"旧 fixture
    删不掉"），也会让后续回滚覆盖失败。我们生成的东西从来不该是只读的。
    """
    if src.is_dir():
        if dst.exists():
            remove_path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src), str(dst))
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
    _ensure_writable(dst)


def copy_tree(src: Path, dst: Path) -> bool:
    """复制目录树（copytree + 补齐写权限）；返回是否成功

    copytree 会把源的只读属性一起复制到产物上。只读的任务文件会一路传染：
    备份副本删不掉、回滚覆盖失败、整个临时 fixture 清不掉（Windows 不能删除
    只读文件）。与 copy_path 同一处理。

    返回 bool 而不是抛异常：调用方是回滚 / 备份这类「已经做了一半」的路径，
    崩栈只会让状态更乱，能不能续做要由调用方决定。
    """
    try:
        shutil.copytree(str(src), str(dst))
        _ensure_writable(dst)
        return True
    except (OSError, shutil.Error) as e:
        print(f"  ⚠️  复制目录 {src.name} 失败：{e}")
        return False


def _ensure_writable(p: Path):
    """给文件或目录树补上写权限；失败不影响主流程"""
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


def remove_path(p: Path, quiet: bool = False) -> bool:
    """删除文件或目录；不存在返回 True；被占用等 OSError 返回 False

    quiet：不打印自带的失败提示 —— 调用方要打印**更贴合上下文**的说明时用，
    否则失败信息会重复两遍（这里一段、调用方一段）。

    以前只吞 FileNotFoundError：Windows 上文件被 WorkBuddy / 索引服务占用时会抛
    PermissionError 直接冒到顶层，回滚进行到一半就断了（半回滚态），
    而调用方还照样打印 ✅。现在失败会如实上报，由调用方决定是否只提示。
    """
    def _unlink():
        if p.is_dir():
            shutil.rmtree(str(p))
        elif p.exists():
            p.unlink()

    try:
        _unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as first:
        # 只读文件在 Windows 上删不掉（unlink / rmtree 直接 PermissionError）。
        # 只读位可能来自我们自己的复制产物（见 copy_path / copy_tree），
        # 也可能是客户端写出来的 —— 抹掉再试一次，能自愈就别让用户手动处理。
        err = first
        try:
            _ensure_writable(p)
            _unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as second:
            err = second
        if not quiet:
            print(f"  ⚠️  删除 {p.name} 失败：{err}")
            print("     通常是文件仍被 WorkBuddy 客户端 / 索引服务占用，"
                  "关闭后重跑同一条命令即可续做")
        return False



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
        # 直接用 int() 会在值不是数字（例如某次导出把时间戳写成了 ISO 字符串）
        # 时抛 ValueError，而 collect_info() 在 `--list` 的循环里被调用 ——
        # 一条脏数据会让整个列表命令崩掉。这里统一容错成 0。
        created_at=_to_int(row.get("created_at")),
        updated_at=_to_int(row.get("updated_at")),
        last_activity_at=_to_int(row.get("last_activity_at")),
        files=files,
        file_size=sum(path_size(f) for f in files if f.exists()),
        # tasks_dir 是必填字段、恒为真，原来的 `if ep.tasks_dir else False`
        # 里的 else 分支永远走不到（纯噪音），直接判目录在不在即可
        has_tasks=(ep.tasks_dir / sid).is_dir(),
    )

    # usage
    conn = connect_ro(ep.db)
    if conn is not None:
        try:
            r = conn.execute(
                "SELECT used, size FROM session_usage WHERE session_id = ?", (sid,)
            ).fetchone()
            if r:
                info.usage_used = _to_int(r[0])
                info.usage_size = _to_int(r[1])
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
    """渲染对比表

    kind: "id"（硬冲突：ID 相同）/ "title"（软冲突：标题相同、ID 不同），
    不同冲突类型的处理后果不同，表头要写清楚。
    """
    rows = build_diff_rows(src, dst)
    verdict, advice = build_verdict(src, dst)

    w1, w2, w3 = 14, 34, 34
    lines = []
    if kind == "title":
        # 软冲突：目标是另一条 id，覆盖等于把那条删掉
        header_dst = "目标现有（将被删除）"
    else:
        header_dst = "目标现有（将被覆盖）"
    lines.append(f"  {pad('指标', w1)}{pad(header_dst, w2)}{pad('源（将写入）', w3)}")
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
    """用 sqlite3 backup API 复制数据库（能正确包含 WAL 中未落盘的数据）

    源库以只读方式打开：快照不该对源库产生任何写入（也不会顺手创建 -wal）。
    """
    s = None
    d = None
    try:
        s = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
        d = sqlite3.connect(str(dst))
        with d:
            s.backup(d)
        # 保持与源库一致：源库是 WAL 而备份产物默认 DELETE 模式，
        # 恢复/直接用别的工具打开时行为可能不一致（与 migrate.py:_backup_db 对齐）
        try:
            mode = s.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() == "wal":
                d.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"  ⚠️  数据库快照失败（不影响精确回滚）: {e}")
        return False
    finally:
        # 异常时也要关，否则连接泄漏会一直占着源库
        if s is not None:
            s.close()
        if d is not None:
            d.close()


def _remove_db_sidecars(db_path: Path) -> bool:
    """删除数据库的 -wal / -shm 边车文件（整库恢复/回滚前必须清理）

    返回是否全部清理成功：删不掉（客户端仍持有句柄）时必须让调用方知道，
    否则"清理失败但照样覆盖主库"等于没清理 —— SQLite 下次打开会把与新主库
    不匹配的旧 WAL 重放上去，整库回滚可能无效甚至数据错乱。

    复用 migrate.py 的实现：这段逻辑以前在这里抄了一份，migrate.py 修了
    「返回 bool + 失败中止」而这边没同步（典型的双份漂移）。
    拿不到 migrate.py 时返回 False → 调用方中止，绝不"清理失败照样覆盖主库"。
    """
    if legacy is None or not hasattr(legacy, "_remove_db_sidecars"):
        print("  ⚠️  未找到 scripts/migrate.py，无法清理 -wal / -shm")
        return False
    return legacy._remove_db_sidecars(db_path)


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8"):
    """原子写：先写同名 .tmp 再 os.replace

    meta.json 是回滚判断的唯一依据，写到一半断电就成了损坏文件（没有 meta 的备份
    等于不可用）。直接 write_text 覆盖不满足这一点。

    这里**刻意保留本地实现**、不复用 migrate.py 的同名函数（两处逐行相同）：
    meta.json 的原子写是回滚的最后一道保险，不该依赖可选的 migrate.py ——
    本脚本拿不到 migrate.py 时仍要能写出完好的备份。进程检测与 -wal/-shm
    边车清理那两处已改为委托（逻辑更复杂，且已经出现过双份漂移）。

    与 migrate.py 的同名函数一样：失败要清理 .tmp 并原样向上抛。os.replace 抛
    OSError 时以前是静默的，meta.json 会停在旧内容上、回滚失去唯一依据。
    """
    tmp = Path(str(path) + ".tmp")
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _write_meta(bp: Path, meta: dict):
    """把备份 meta 立即落盘

    每个破坏性步骤（软冲突覆盖删旧对话、move 删源行、删源文件、复制正文）
    之后都要增量写一次。只在最后写一次的话，中途失败（RuntimeError / Ctrl-C）
    会让磁盘上的 meta 缺 override_deleted / source_deleted / copied_to，
    精确回滚正是靠这些字段判断，会静默漏项。
    """
    _atomic_write_text(
        bp / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2)
    )


def create_backup(src_ep, dst_ep, src_row, dst_row, session_id, mode,
                  backup_root=None, extra_row=None, kind="session",
                  extra_meta=None) -> Path:
    """创建单对话迁移的备份（精确到行 + 文件，另附整库快照兜底）

    extra_row: 软冲突时被覆盖掉的那条目标记录（id 与源不同），单独备份以便回滚
    kind: 备份种类（session / session_intra / session_clone），**必须在创建时一次写定**。
          以前是建完再读回来改 kind，两次写之间中断的话回滚会走通用分支，
          同库场景下反而把没动过的原对话行删掉。
    extra_meta: 需要一并写入 meta 的额外字段
    """
    # 单对话备份统一放 migrate_backups/session/，与 migrate.py 的整账号备份分开
    # （两者 meta.json 格式不同，混在一起会让 --backups 列出、--rollback 撞 KeyError）。
    # ⚠️ 给了 --backup-dir 也要套一层 session/：以前自定义目录是平铺写入的，
    # 若用户把它指向 migrate_backups，单对话备份就和整账号备份挤在同一个目录下，
    # 前缀模糊匹配还可能同时命中两边。查找侧同时扫「根」和「根/session」。
    root = (Path(backup_root) if backup_root else dst_ep.backup_dir) / "session"
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    base_tag = f"{ts}_{src_ep.name}2{dst_ep.name}_{session_id[:8]}"
    # 标签精度只到秒，同一秒内迁两条 session_id 前缀相同的对话会撞名。
    # 复用同一目录会让 meta.json 被后一次覆盖，前一份备份失去回滚依据。
    bp = root / base_tag
    seq = 2
    while bp.exists() and any(bp.iterdir()):
        bp = root / f"{base_tag}_{seq}"
        seq += 1
    tag = bp.name
    try:
        bp.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _abort_backup(bp, f"无法创建备份目录：{e}")

    meta = {
        "version": 2,
        "kind": kind,
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
        try:
            (bp / "files").mkdir(parents=True, exist_ok=True)
        except Exception as e:
            _abort_backup(bp, f"无法创建备份子目录 files/：{e}")
        for f in src_files:
            if not f.exists():
                continue
            if not safe_copy(f, bp / "files" / f.name):
                _abort_backup(bp, "备份不完整，已中止迁移（未改动任何数据）")
            meta["src_files"].append({"name": f.name, "path": str(f), "is_dir": f.is_dir()})

    # 目标原有行 + 原文件（覆盖场景）
    # 注意：文件备份必须在 dst_row 判据【之外】——目标侧的附属文件可能根本没有
    # 对应的库行（上次迁移半途而废留下的孤儿），那种文件本次也要清掉，
    # 挂在 if dst_row 里就等于"想删又没备份"，只能不删 → 孤儿永远留着。
    if dst_row:
        (bp / "dst_session.json").write_text(
            json.dumps(dst_row, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    dst_files = find_project_files(dst_ep, session_id)
    if dst_files:
        # 没有目标行却有一堆附属文件 = 上一次迁移留下的孤儿。
        # 这个标记刻意只写不读：清理由上面的 always 分支负责，读它反而会引入
        # 第二个事实来源。留在 meta.json 里是给用户求助时看的现场证据
        # （备份 meta.json 是排查迁移问题时唯一能拿到的完整快照）。
        meta["orphan_dst_files"] = dst_row is None
        try:
            (bp / "dst_files").mkdir(parents=True, exist_ok=True)
        except Exception as e:
            _abort_backup(bp, f"无法创建备份子目录 dst_files/：{e}")
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
            try:
                (bp / "override_files").mkdir(parents=True, exist_ok=True)
            except Exception as e:
                _abort_backup(bp, f"无法创建备份子目录 override_files/：{e}")
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

    # 任务数据：源侧（move 会删）、目标侧（硬冲突覆盖会删，同版本时与源同目录）都要备份
    # 以前完全不备份 tasks/，move 后回滚 = 任务数据永久丢失
    seen_task_dirs = []
    for tag_, ep in (("src_tasks", src_ep), ("dst_tasks", dst_ep)):
        tdir = ep.tasks_dir / session_id
        if not tdir.is_dir():
            continue
        if any(tdir == d for d in seen_task_dirs):   # 同版本：源目标同一个目录，只备一次
            continue
        seen_task_dirs.append(tdir)
        if copy_tree(tdir, bp / tag_):
            meta[tag_] = True
        else:
            _abort_backup(bp, f"备份任务数据 {tdir.name} 失败（未改动任何数据）")

    # 整库快照（兜底）；同版本迁移时两库是同一个，只快照一次
    if src_ep.db.exists():
        snapshot_db(src_ep.db, bp / "snapshot_src.db")
    if dst_ep.db.exists() and dst_ep.db != src_ep.db:
        snapshot_db(dst_ep.db, bp / "snapshot_dst.db")

    if extra_meta:
        meta.update(extra_meta)
    _write_meta(bp, meta)
    return bp


def _backup_roots(backup_root=None):
    """单对话迁移备份的候选根目录

    新备份放在 `migrate_backups/session/`，与 migrate.py 的整账号备份分开
    （两者格式不同，混在一个目录里会让 --backups 列出、--rollback 撞上 KeyError）。
    旧位置的备份仍要能找到，所以两个目录都扫。

    backup_root（--backup-dir）的语义是「**额外**加入一个搜索根」，不是「限定只搜它」：
    显式指定后仍会回退扫描两个版本的标准目录，否则在自定义目录里找标准位置的
    备份会全部落空。新建备份时则**只用** backup_root（见 create_backup）。
    """
    roots = []
    if backup_root:
        # 自定义根也按 session/ 命名空间找（create_backup 现在一律写进 session/），
        # 同时保留"平铺"的旧布局作为兼容，两种都能找到
        roots.append(Path(backup_root) / "session")
        roots.append(Path(backup_root))
    for name in ("domestic", "intl"):
        ep = resolve_edition(name)
        roots.append(ep.backup_dir / "session")
        roots.append(ep.backup_dir)
    uniq = []
    for r in roots:
        if r not in uniq:
            uniq.append(r)
    return uniq


def _validate_backup_tag(tag):
    """备份标签合法性检查：只能是备份目录名，不能有路径成分

    标签来自命令行，会被直接拼进候选备份根目录；命中后还用于删文件 / 覆盖库的
    目标推导。`../../xxx` 这类标签能让操作落到备份目录之外。
    与 migrate.py 的 rollback() 同一套校验。

    只做判断、不打印：细节由调用方决定怎么说（"不合法"和"找不到"是两件事）。
    """
    return not (not tag
                or "/" in tag or "\\" in tag
                or os.path.isabs(tag)
                or ".." in Path(tag).parts)


def find_backup(tag, backup_root=None):
    """按 tag 查找备份；返回 `(路径或 None, 失败原因)`

    reason 是 `None` / `"invalid"` / `"notfound"`：
    「标签不合法」和「合法但没找到」是两个不同的事实，混成一句会既重复又误导
    （曾经先打「❌ 标签不合法」紧接着又打「❌ 找不到备份: ../../etc」）。

    给了 --backup-dir 时它只是**额外**搜索根（见 _backup_roots）：写错或给了个空
    目录时仍会回退扫标准目录，可能匹配到同前缀的旧备份而去回滚完全不相干的东西。
    所以命中来源不是 backup_root 时必须显式警告。
    """
    # 校验下沉到这里：以前只写在 rollback() 里，任何绕过 rollback 直接调
    # load_backup() 的路径（以及将来新增的调用点）都能拼出备份目录之外。
    # ⚠️ 但一个叫 "load"/"find" 的函数不该直接终止进程（未来若有「查不到就
    # 返回 None」的调用者会踩坑），所以这里只返回结果，由调用方决定怎么退出。
    if not _validate_backup_tag(tag):
        return None, "invalid"
    roots = _backup_roots(backup_root)
    for r in roots:
        p = r / tag
        if (p / "meta.json").exists():
            _warn_backup_root_mismatch(backup_root, r)
            return p, None
    # 前缀模糊匹配
    hits = []
    hit_root = {}
    for r in roots:
        if not r.exists():
            continue
        for d in r.iterdir():
            if d.is_dir() and d.name.startswith(tag) and (d / "meta.json").exists():
                if d.name not in [h.name for h in hits]:
                    hits.append(d)
                    hit_root[d.name] = r
    if len(hits) == 1:
        _warn_backup_root_mismatch(backup_root, hit_root[hits[0].name])
        return hits[0], None
    if len(hits) > 1:
        # 以前是直接 return None，用户只看到"找不到备份"，实际是前缀歧义
        print(f"❌ 前缀 {tag} 匹配到多个备份，请写得更具体：")
        for h in hits:
            print(f"   {h.name}")
        sys.exit(1)
    return None, "notfound"


def load_backup(tag, backup_root=None):
    """按 tag 查找备份目录，找不到返回 None（不终止进程）

    需要区分「标签不合法」和「合法但没找到」的调用方请用 `find_backup()`
    —— 它额外返回 reason。
    """
    bp, _reason = find_backup(tag, backup_root)
    return bp


def _warn_backup_root_mismatch(backup_root, matched_root):
    """--backup-dir 指定的目录里没找到、却在标准目录里命中时必须说清楚"""
    if not backup_root:
        return
    want = Path(backup_root).resolve()
    try:
        got = Path(matched_root).resolve()
    except OSError:
        got = Path(matched_root)
    if want == got or want in got.parents:
        return
    print(f"⚠️  --backup-dir 指定的是 {want}，但这份备份实际来自 {got}")
    print("   如果这不是你想回滚的那份，请中止并用 --backups 确认标签。")
    print("   （--backup-dir 是【额外】搜索根，找不到时仍会回退扫描两个版本的标准目录）")



def _report_rollback(problems, ok_text: str):
    """回滚收尾：没做到的步骤如实列出，不要无条件报成功

    半回滚是最难排查的状态 —— 用户以为已经干净了，然后在这个基础上继续操作。
    """
    if not problems:
        print(f"\n  ✅ {ok_text}")
        return
    print(f"\n  ⚠️  回滚未完全完成，以下步骤没做到：")
    for i, p in enumerate(problems, 1):
        print(f"     {i}. {p}")
    print("     常见原因是文件仍被 WorkBuddy 客户端 / 索引服务占用，")
    print("     或目标路径被设为只读。关掉客户端后重跑同一条 --rollback 命令即可续做（幂等）。")


def _load_backup_json(path: Path, problems, what: str):
    """读备份里的 json 行；损坏时记一笔并跳过，不要抛 traceback

    回滚路径里这些 JSON 以前都是裸 `json.loads(...read_text())`：备份写到一半
    断电就会损坏，回滚进行到一半直接崩栈，用户既看不到"哪一步没做"，也拿不到
    "还能怎么办"。损坏时把这一步记为未完成，由收尾汇总统一告知。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  ⚠️  备份文件 {path.name} 无法读取（{e}）")
        problems.append(f"{what}（{path.name} 无法读取）")
        return None


def rollback(tag, backup_root=None, full=False, assume_yes=False):
    """回滚单对话迁移"""
    # 标签来自命令行，可能是相对/绝对路径（如 ../../etc）——会被拼进候选备份根目录，
    # 命中后还用于删文件 / 覆盖库的目标推导。校验已在 load_backup() 内部完成
    # （下沉是为了让任何直接调用 load_backup() 的路径都受同一约束）；
    # 标签不合法时它返回 None，退出码由这里决定。
    bp, reason = find_backup(tag, backup_root)
    if bp is None:
        if reason == "invalid":
            print(f"❌ 备份标签不合法：{tag!r}")
            print("   标签只能是备份目录名（如 20260921153000_domestic2intl_12345678），"
                  "不能包含路径分隔符")
        else:
            print(f"❌ 找不到备份: {tag}")
            print("   可用 --backups 查看")
        sys.exit(1)

    # meta.json 是回滚的唯一依据：from_edition / to_edition / session_id / kind
    # 全部从它取。它可能是半截损坏的（写到一半断电），必须给可操作提示而不是
    # traceback —— 而且这段发生在"要不要回滚"的确认询问之前，崩在这里用户连
    # 提示都看不到。（migrate.py 的 rollback() 已补同类兜底，这里是另一半。）
    meta_file = bp / "meta.json"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"❌ 备份 {bp.name} 的 meta.json 无法解析：{e}")
        print("   回滚必须知道「从哪个版本迁到哪个版本、迁的是哪条对话」，这些都存在 meta.json 里，")
        print("   损坏后无法精确回滚。可选处理：")
        print("     · 用 --backups 挑一个 meta.json 完好的备份")
        print(f"     · 手动检查 {meta_file}")
        print("     · 备份里的 snapshot_src.db / snapshot_dst.db 是迁移前的整库快照，")
        print("       可手动复制回对应版本的 workbuddy.db 兜底（先关闭客户端并删掉 -wal/-shm）")
        sys.exit(1)
    # 两个工具的备份格式不同：migrate.py 的整账号备份没有 from_edition / kind，
    # 拿来跑单对话回滚会直接 KeyError traceback
    if not meta.get("from_edition") or not str(meta.get("kind", "")).startswith("session"):
        print(f"❌ {bp.name} 不是单对话迁移的备份（很可能是 migrate.py 的整账号备份）")
        print("   整账号回滚请用：python3 scripts/migrate.py --rollback <TAG>")
        print("   单对话备份请用：python3 scripts/migrate_session.py --backups 查看")
        sys.exit(1)
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
    # 「目标被覆盖」只反映同 id 的那条；软冲突时被删掉的是另一条同名旧对话，
    # 只打「否」会让用户以为目标什么都没动过
    print(f"  目标被覆盖: {'是' if meta.get('target_overwritten') else '否'}"
          f"{'，另有同名旧对话被删除（回滚会整条还原）' if meta.get('override_deleted') else ''}")
    print()

    # copy 模式（源未被删除）时迁移根本没动过源侧的数据 —— 精确回滚也是按
    # meta["source_deleted"] 来判断要不要把源插回去的。整库快照若不加这个判断就把
    # 源库一并覆盖，会把源版本迁移之后新增的对话 / 消息一起抹掉，而用户选 copy
    # 恰恰是为了保住源；正文文件同样会被还原成备份里的旧内容。
    # 例外：同版本迁移（同属一个库，备份里只会有 snapshot_src.db），那一份必须照还原。
    _same_root_db = src_ep.db == dst_ep.db
    _restore_src_side = bool(meta.get("source_deleted")) or _same_root_db

    if full:
        print("  ⚠️  整库恢复模式：会把迁移波及到的数据库整体还原到迁移前，")
        print("      迁移之后产生的新对话/新数据会一并丢失！")
        if not _restore_src_side:
            print(f"      本次是 copy 模式（{src_ep.label} 源数据未被删除），"
                  f"{src_ep.label} 不在还原范围内。")
    if not assume_yes and not ask_yes_no("  确认回滚？(y/N): "):
        print("已取消")
        return

    # 回滚里凡是「可能失败但不能崩栈」的步骤都往这里记一笔，由收尾汇总统一告知。
    # 以前不管有没有做到都打「✅ 回滚完成」，半回滚态最难排查，假话代价太高。
    rollback_problems = []

    if full:
        restored = False
        for snapshot, ep in (("snapshot_src.db", src_ep), ("snapshot_dst.db", dst_ep)):
            if ep is src_ep and not _restore_src_side:
                print(f"  ⏭️  copy 模式：{src_ep.label} 未被本次迁移改动，跳过 {snapshot}")
                continue
            f = bp / snapshot
            if not f.exists():
                continue
            # 覆盖前先把"当前库"另存一份，回滚后发现问题还有得救
            if ep.db.exists():
                snapshot_db(ep.db, bp / ("prefull_" + ep.name + ".db"))
            # 必须先清 -wal / -shm：只覆盖主库的话，SQLite 下次打开会把
            # 与新主库不匹配的旧 WAL 重放上去，整库恢复等于白做。
            # 清理失败（客户端仍持句柄）时不能硬着头皮覆盖主库。
            if not _remove_db_sidecars(ep.db):
                print(f"  ❌ 未清理 {ep.db.name} 的 -wal / -shm，继续覆盖会让整库恢复失效，已中止。")
                print("     请关闭 WorkBuddy 客户端后重新执行 --rollback --full")
                sys.exit(1)
            shutil.copy2(str(f), str(ep.db))
            print(f"  ✅ 已整库恢复 {ep.label}")
            restored = True

        if not restored:
            # copy 模式下源侧快照是主动跳过的，说清楚「为什么没得恢复」，
            # 否则用户会以为是备份损坏
            if _same_root_db:
                how = "同版本迁移只产生一个整库快照，它也不见了"
            elif not _restore_src_side:
                how = f"源侧未被本次迁移改动（copy 模式）而跳过，{dst_ep.label} 侧又没有快照"
            else:
                how = "备份里两个整库快照都不在"
            print(f"  ❌ 无法 --full 恢复：{how}。")
            print("     请改用不带 --full 的精确回滚，或先 --backups 确认备份内容。")
            sys.exit(1)

        # 文件层面也要回到迁移前：否则"整库回滚"名不副实（库回来了、正文还在目标侧）
        for rel in meta.get("copied_to", []):
            p = Path(rel)
            if p.exists() and remove_path(p):
                print(f"  ✅ 已删除 {p.name}")
        if _restore_src_side:
            for f in meta.get("src_files", []):
                srcf = bp / "files" / f["name"]
                if srcf.exists() and safe_copy(srcf, Path(f["path"])):
                    print(f"  ✅ 已还原源文件 {f['name']}")
        elif meta.get("src_files"):
            print(f"  ⏭️  copy 模式：{len(meta['src_files'])} 个源侧正文文件未被删除，跳过还原")
        # 覆盖路径必须还原目标侧的原有文件：硬冲突/软冲突覆盖后只删 copied_to 的话，
        # 库行从快照回来了，目标 jsonl 却被删且没还原 → "对话行在、正文空"
        for f in meta.get("dst_files", []):
            srcf = bp / "dst_files" / f["name"]
            if srcf.exists() and safe_copy(srcf, Path(f["path"])):
                print(f"  ✅ 已还原目标原有文件 {f['name']}")
        for f in meta.get("override_files", []):
            srcf = bp / "override_files" / f["name"]
            if srcf.exists() and safe_copy(srcf, Path(f["path"])):
                print(f"  ✅ 已还原被覆盖的旧对话文件 {f['name']}")
        if meta.get("tasks_copied"):
            dt = dst_ep.tasks_dir / sid
            if dt.exists():
                if remove_path(dt):
                    print("  ✅ 已删除目标侧任务数据")
                else:
                    rollback_problems.append("删除目标侧任务数据")
        # 目标侧迁移前就有的任务数据也要回来
        if (bp / "dst_tasks").exists():
            dt = dst_ep.tasks_dir / sid
            if not dt.exists():
                dt.parent.mkdir(parents=True, exist_ok=True)
                if copy_tree(bp / "dst_tasks", dt):
                    print("  ✅ 已还原目标侧原有任务数据")
                else:
                    rollback_problems.append("还原目标侧原有任务数据")
        if _restore_src_side and (bp / "src_tasks").exists():
            st = src_ep.tasks_dir / sid
            if not st.exists():
                st.parent.mkdir(parents=True, exist_ok=True)
                if copy_tree(bp / "src_tasks", st):
                    print("  ✅ 已还原源侧任务数据")
                else:
                    rollback_problems.append("还原源侧任务数据")
        _report_rollback(rollback_problems, "整库回滚完成（库 + 正文 + 任务数据）")
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
        finally:
            conn.close()
        _report_verify(
            f"已把 user_id 改回 {str(meta.get('source_user_id', ''))[:12]}…", n
        )
        # 校验失败也得进汇总：这一支以前是裸 print("✅ 回滚完成")，
        # 而 _report_verify 只打印不记账 —— 命中 0 行时照样报成功
        if n != 1:
            rollback_problems.append(
                f"把 {sid[:8]}… 的 user_id 改回 {str(meta.get('source_user_id', ''))[:12]}…")
        _report_rollback(rollback_problems, "回滚完成")
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
                if remove_path(p):
                    print(f"  ✅ 已删除 {p.name}")
                else:
                    rollback_problems.append(f"删除克隆出的文件 {p.name}")
        if meta.get("cloned_tasks"):
            td = dst_ep.tasks_dir / new_sid
            if td.exists():
                if remove_path(td):
                    print("  ✅ 已删除克隆的任务数据")
                else:
                    rollback_problems.append("删除克隆的任务数据")
        _report_rollback(rollback_problems, "回滚完成（原始对话未受影响）")
        return

    # 精确回滚
    # 1) 目标侧：删除写入的行与文件，恢复被覆盖的原行/原文件
    conn = connect_rw(dst_ep.db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if meta.get("target_existed") and (bp / "dst_session.json").exists():
            row = _load_backup_json(bp / "dst_session.json", rollback_problems,
                                    "恢复目标原有 session 行")
            if row:
                cols = [c for c in row.keys() if c in table_columns(conn, "sessions")]
                conn.execute(
                    f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [row[c] for c in cols],
                )
                print("  ✅ 已恢复目标原有 session 行")
            else:
                # 备份损坏，恢复不了；目标行已经迁过来了，删掉等于连原件一起丢，
                # 所以保持现状并由收尾汇总告知
                print("  ⚠️  目标原有 session 行未能恢复（备份不可用，已保留迁移后的行）")
        else:
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            print("  ✅ 已删除目标侧的 session 行")

        conn.execute("DELETE FROM session_usage WHERE session_id = ?", (sid,))
        if (bp / "dst_usage.json").exists():
            u = _load_backup_json(bp / "dst_usage.json", rollback_problems,
                                  "恢复目标原有 session_usage")
            cols = ([c for c in u.keys() if c in table_columns(conn, "session_usage")]
                    if u else [])
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
            row = _load_backup_json(bp / "override_session.json", rollback_problems,
                                    "恢复被覆盖的旧对话")
            if row:
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
                u = _load_backup_json(uf, rollback_problems,
                                      "恢复其 session_usage")
                ucols = ([c for c in u.keys() if c in table_columns(conn, "session_usage")]
                         if u else [])
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
                else:
                    rollback_problems.append(f"还原被覆盖的旧文件 {f['name']}")

    # 删除复制过去的文件；若之前覆盖了目标文件则还原
    for rel in meta.get("copied_to", []):
        p = Path(rel)
        if p.exists():
            # 可能是复制过去的 tool-results 目录
            if remove_path(p):
                print(f"  ✅ 已删除 {p.name}")
            else:
                rollback_problems.append(f"删除目标侧残留文件 {p.name}")
    for f in meta.get("dst_files", []):
        srcf = bp / "dst_files" / f["name"]
        if srcf.exists():
            if safe_copy(srcf, Path(f["path"])):
                print(f"  ✅ 已还原 {f['name']}")
            else:
                rollback_problems.append(f"还原目标原有文件 {f['name']}")

    # 2) 源侧：若迁移时删除了源，则插回
    if meta.get("source_deleted"):
        conn = connect_rw(src_ep.db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = _load_backup_json(bp / "src_session.json", rollback_problems,
                                    "把 session 行插回源版本")
            if row:
                cols = [c for c in row.keys() if c in table_columns(conn, "sessions")]
                conn.execute(
                    f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [row[c] for c in cols],
                )
            if (bp / "src_usage.json").exists():
                u = _load_backup_json(bp / "src_usage.json", rollback_problems,
                                      "恢复源侧 session_usage")
                ucols = ([c for c in u.keys() if c in table_columns(conn, "session_usage")]
                         if u else [])
                if ucols:
                    conn.execute(
                        f"INSERT OR REPLACE INTO session_usage ({','.join(ucols)}) VALUES ({','.join('?' * len(ucols))})",
                        [u[c] for c in ucols],
                    )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if row:
                print("  ✅ 已把 session 行插回源版本")
        finally:
            conn.close()

        for f in meta.get("src_files", []):
            srcf = bp / "files" / f["name"]
            if srcf.exists():
                if safe_copy(srcf, Path(f["path"])):
                    print(f"  ✅ 已还原源文件 {f['name']}")
                else:
                    rollback_problems.append(f"还原源文件 {f['name']}")

        # move 模式删掉的源 tasks 目录必须还原，否则任务数据永久丢失
        if (bp / "src_tasks").exists():
            src_tasks = src_ep.tasks_dir / sid
            if not src_tasks.exists():
                src_tasks.parent.mkdir(parents=True, exist_ok=True)
                if copy_tree(bp / "src_tasks", src_tasks):
                    print("  ✅ 已还原源侧任务数据")
                else:
                    rollback_problems.append("还原源侧任务数据")

    # 目标侧 tasks：迁移复制过去的要删掉，被覆盖前的要还原
    dst_tasks = dst_ep.tasks_dir / sid
    if meta.get("tasks_copied") and dst_tasks.exists():
        if remove_path(dst_tasks):
            print(f"  ✅ 已删除目标侧任务数据 {sid[:8]}…")
        else:
            rollback_problems.append(f"删除目标侧任务数据 {sid[:8]}…")
    if (bp / "dst_tasks").exists() and not dst_tasks.exists():
        dst_tasks.parent.mkdir(parents=True, exist_ok=True)
        if copy_tree(bp / "dst_tasks", dst_tasks):
            print("  ✅ 已还原目标侧原有任务数据")
        else:
            rollback_problems.append("还原目标侧原有任务数据")

    _report_rollback(rollback_problems, "精确回滚完成（未影响其他对话）")


def list_backups(backup_root=None):
    """列出所有单对话迁移备份"""
    found = []
    broken = []
    foreign = []
    roots = _backup_roots(backup_root)
    for r in roots:
        if not r.exists():
            continue
        for d in sorted(r.iterdir(), reverse=True):
            mf = d / "meta.json"
            if not (d.is_dir() and mf.exists()):
                continue
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except Exception as e:
                # 以前 except: continue 静默跳过坏 meta，--backups 里根本看不到它，
                # 而 --rollback 又会提示「meta.json 无法解析」，两边说法对不上
                broken.append((d.name, f"{type(e).__name__}: {e}"))
                continue
            if not str(meta.get("kind", "")).startswith("session"):
                # migrate.py 的整账号备份：格式不同，回滚要走另一个脚本
                foreign.append(d.name)
                continue
            found.append((d.name, meta))

    for _name, _err in broken:
        print(f"  ⚠️  备份 {_name} 的 meta.json 无法解析（{_err}），无法精确回滚")
    for _name in foreign:
        print(f"  ℹ️  {_name} 是整账号备份，回滚请用：python3 scripts/migrate.py --rollback {_name}")


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
            # 严格大于才算「源更新」：秒级时间戳相同时按 overwrite 处理会误覆盖
            return "overwrite" if src.effective_last_ts > dst.effective_last_ts else "skip"
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
        target_uid, how = get_current_uid(ep)
        if target_uid:
            print(f"  目标账号: {target_uid[:12]}…（来源: {_uid_src_text(how)}）")
            # 同版本路径以前完全没接这个告警：靠 DB 反推出的 uid 会静默当作目标账号
            _warn_weak_target_uid(how)
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
    # kind 一次写定：建完再改的话，两次写之间中断会让回滚走通用分支，
    # 同库场景下反而把还没动过的原对话行删掉
    bp = create_backup(
        ep, ep, src_row, None, sid, "intra", None,
        kind="session_intra",
        extra_meta={"source_user_id": old_uid, "target_user_id": target_uid},
    )
    # 这里以前会把 meta.json 读回来塞进 meta 变量，但后面一次都没用过：
    # 多一个无关的崩溃点（meta 半截损坏时反而把正常的同版本迁移带崩），删掉
    print(f"  📦 备份: {bp.name}")

    conn = connect_rw(ep.db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cur = conn.execute("UPDATE sessions SET user_id = ? WHERE id = ?", (target_uid, sid))
        # UPDATE 的命中行数必须核对：命中 0 行时 commit 一样「成功」，
        # 只靠事后 COUNT 会把「其实没改到」和「本来就等于目标值」混为一谈。
        updated = cur.rowcount
        if updated != 1:
            conn.rollback()
            raise RuntimeError(
                f"UPDATE 预期改动 1 行，实际改动 {updated} 行"
                f"（对话 {sid} 可能已被删除或被并发修改），已回滚、未做任何改动"
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        n = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ? AND user_id = ?", (sid, target_uid)
        ).fetchone()[0]
    finally:
        conn.close()
    print()
    _report_verify("完成（user_id 已改到目标账号）", n)
    print(f"  回滚: python3 scripts/migrate_session.py --rollback {bp.name}")


def _session_id_field_re(sid: str):
    """匹配正文里 `"sessionId":"<sid>"` 这个字段（容忍冒号/引号两侧空白）

    _rewrite_session_id() 与 _contains_old_session_id() **必须共用这一个正则**：
    以前改写用的是 `\\s*` 容忍空格，残留校验用的是字面量 `"sessionId":"<sid>"`，
    于是写成 `"sessionId": "<sid>"` 时「改写得到、校验查不出」—— 残留校验会漏报。
    """
    return re.compile(r'("sessionId"\s*:\s*")' + re.escape(sid) + r'(")')


def _rewrite_session_id(path: Path, old_sid: str, new_sid: str):
    """把正文 jsonl 里内嵌的 sessionId 换成新 id

    只替换 "sessionId":"<old>" 这种字段值：会话正文（text）里同样会出现这个 id 串
    （日志、路径、引用等），整行 replace 会把用户可见的消息内容一起改坏。

    ⚠️ 读写两端都用 `errors="surrogateescape"`：正文里可能夹杂非 UTF-8 字节
    （二进制转义、被截断的多字节字符）。用 `errors="replace"` 读会把它们变成
    U+FFFD，再写回磁盘就是**静默篡改用户数据**，而且不可逆。surrogateescape
    能把读到的字节原样带回去写。

    逐行流式处理，避免把几 MB 的正文整个读进内存。
    """
    pat = _session_id_field_re(old_sid)
    tmp = path.with_name(path.name + ".tmp")
    try:
        # newline="" 保证 \r\n 原样保留，不被通用换行模式改写
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fin, \
                open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as fout:
            for line in fin:
                fout.write(pat.sub(lambda m: m.group(1) + new_sid + m.group(2), line))
        tmp.replace(path)
    except Exception:
        # 失败时清掉 .tmp，否则下次复制会撞上一个残缺文件
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _contains_old_session_id(path: Path, old_sid: str) -> bool:
    """流式判断文件里是否还残留 `"sessionId":"<old>"` 字段

    只认字段形态：消息正文里引用到的旧 id 是有意保留的，不能被算成残留。
    逐行扫描，避免把整份正文读进内存。

    用与改写同一套正则（容忍 `"sessionId": "<old>"` 这种带空格的写法），
    否则改写得到的写法校验查不出 → 漏报。
    """
    pat = _session_id_field_re(old_sid)
    try:
        # 与 _rewrite_session_id 同一套 errors：非 UTF-8 字节不该在这里被替换掉
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                if pat.search(line):
                    return True
    except OSError:
        return False
    return False


def _clone_title(old_title: str) -> str:
    """克隆副本的标题：每次克隆都再追加一个「（副本）」

    只判断"是否已以（副本）结尾"会不够：克隆副本时不再加标记，
    客户端里就会出现两条同名「xxx（副本）」，分不清哪份是哪份。
    """
    return f"{old_title}（副本）" if old_title else "（副本）"


def _rewrite_tree_ids(path: Path, old_sid: str, new_sid: str):
    """改写一处正文产物里的 sessionId（文件自身，以及目录内的文件）

    顶层按扩展名过滤，tool-results/ 这类目录则递归处理内部文件——
    只改顶层的话，目录里内嵌的旧 id 会残留。
    """
    if path.is_file():
        if path.suffix in (".jsonl", ".json", ".ndjson", ".txt"):
            _rewrite_session_id(path, old_sid, new_sid)
        return
    if path.is_dir():
        for inner in path.rglob("*"):
            if inner.is_file() and inner.suffix in (".jsonl", ".json", ".ndjson", ".txt"):
                _rewrite_session_id(inner, old_sid, new_sid)


def _clone_intra(ep, src_row, target_uid, dry_run, assume_yes):
    """同版本内克隆一条对话：新 id + 复制正文（改写 sessionId）+ 任务数据 + 工具结果"""
    sid = src_row["id"]
    new_sid = str(uuid.uuid4())
    old_title = (src_row.get("custom_title") or src_row.get("title") or "").strip()
    new_title = _clone_title(old_title)

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
    bp = create_backup(
        ep, ep, src_row, None, sid, "copy", None,
        kind="session_clone",
        extra_meta={
            "new_session_id": new_sid,
            "source_user_id": src_row.get("user_id", ""),
            "target_user_id": target_uid,
            "cloned_tasks": False,
            "copied_to": [],   # 回滚按此列表删除克隆产物
        },
    )
    meta = json.loads((bp / "meta.json").read_text(encoding="utf-8"))
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
    print()
    _report_verify("新 session 行已写入", n)

    # 正文文件：文件名换成新 id；jsonl 内部的 sessionId 也要跟着换
    print("\n📄 复制对话正文...")
    for f in src_info.files:
        if not f.exists():
            continue
        target = f.parent / f.name.replace(sid, new_sid)
        if not safe_copy(f, target):
            raise RuntimeError("复制对话正文失败，已中止（请执行回滚）")
        # 先登记再改写：反过来做的话，改写失败会留下磁盘上有文件、
        # meta 里却没记录 → 回滚删不掉，成为孤儿文件（C8）
        meta["copied_to"].append(str(target))
        _write_meta(bp, meta)
        # .jsonl 是正文；.meta.json / .file-rollback.ndjson 也可能内嵌 sessionId，
        # tool-results/ 目录内部文件同样处理（S2）
        # OSError（只读/被占用/磁盘满）必须转成 RuntimeError：此刻新 id 的库行
        # 已经 commit 了，裸 OSError 会绕过顶层的人话提示，用户看到完整 traceback
        # 却不知道要先回滚，磁盘上还留着半截副本。
        try:
            _rewrite_tree_ids(target, sid, new_sid)
        except OSError as e:
            raise RuntimeError(
                f"改写 {target.name} 里的 sessionId 失败：{e}\n"
                f"   副本的库行已经写入（新 id {new_sid}），请用 --rollback 回滚本次克隆"
            )
        mark = "目录" if target.is_dir() else "文件"
        print(f"  ✅ {target.name} [{mark}] ({fmt_size(path_size(target))})")

    # 校验：只查 "sessionId" 字段是否仍是旧值。
    # 用 `sid in 全文` 会误报——消息正文里引用旧 id 现在是有意保留的。
    # 逐行流式扫描（与 _rewrite_session_id 一致）：整份 read_text 会把几 MB 的
    # 正文和 tool-results 全读进内存。
    leftovers = []
    for rel in meta.get("copied_to", []):
        p = Path(rel)
        try:
            if p.is_file():
                if _contains_old_session_id(p, sid):
                    leftovers.append(p.name)
            elif p.is_dir():
                for inner in p.rglob("*"):
                    if inner.is_file() and _contains_old_session_id(inner, sid):
                        leftovers.append(inner.name)
        except Exception:
            pass
    if leftovers:
        print(f"  ⚠️  以下文件的 sessionId 字段仍是旧值，请人工确认：{', '.join(leftovers)}")

    src_tasks = ep.tasks_dir / sid
    if src_tasks.exists():
        dst_tasks = ep.tasks_dir / new_sid
        if dst_tasks.exists() and not remove_path(dst_tasks):
            raise RuntimeError(f"清理已存在的目标任务目录 {new_sid[:8]}… 失败（请执行回滚）")
        if copy_tree(src_tasks, dst_tasks):
            meta["cloned_tasks"] = True
            _write_meta(bp, meta)
            print("  ✅ 任务数据已复制")
        else:
            # 半截目录也要能回滚：先登记，回滚按 cloned_tasks 清理（S5）
            meta["cloned_tasks"] = True
            _write_meta(bp, meta)
            raise RuntimeError("复制任务数据失败（请执行回滚）")

    # 这里以前还有一次裸 write_text(meta.json)：内容与上面每次 _write_meta 写的完全
    # 相同，却绕过了原子写（写到一半中断就得到损坏的 meta）。删除即可，
    # 最后一次状态变更已经由 _write_meta 落盘。

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
            print(f"  目标账号: {target_uid[:12]}…"
                  f"（来源: {_uid_src_text(how)}）")
            _warn_weak_target_uid(how)
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
        if dry_run:
            # dry-run 只展示计划、不写盘，不能向用户提问：
            # 以前 ask_conflict 在 dry-run 判断之前，管道输入时 EOFError
            # 会被当成"已取消"，计划根本不打印。
            print(render_diff(src_info, dst_info, "id"))
        else:
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
            if len(same_title) > 1:
                # 只处理第一条，其余同名对话仍在客户端里继续重复，必须让用户知道
                print(f"\n  ⚠️  目标版本里共有 {len(same_title)} 条同名对话，"
                      f"本次只处理其中 1 条，剩余的仍需手动清理：")
                for r in same_title[1:]:
                    print(f"     {r.get('id')}  {str(r.get('custom_title') or r.get('title'))[:30]}")
            d_info = collect_info(dst_ep, same_title[0], deep=True)
            if dry_run:
                print(render_diff(src_info, d_info, "title"))
                override_row = same_title[0]   # 只为把"删除旧对话"这一步写进计划
            else:
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
    # 备份落点要和 create_backup 实际用的目录一致：
    # 单对话备份在 migrate_backups/session/（以前这里只打印 migrate_backups/，
    # 和实际落点对不上）；给了 --backup-dir 时以它为准。
    if backup_root:
        backup_desc = f"{Path(backup_root).name}/session/"
    else:
        backup_desc = f"{dst_ep.backup_dir.name}/session/"
    steps.append(f"备份到 {backup_desc}")

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
    # 立即落盘：等到目标库写完才写 meta 的话，中途失败时磁盘上的 meta 会缺这个字段
    _write_meta(bp, meta)
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
            except Exception as e:
                # 以前这里是 except: pass —— 登记失败会导致"迁移成功但客户端打不开"，
                # 属于静默失败，至少要告警
                print(f"  ⚠️  workspaces 登记失败（{e}）：目标客户端可能找不到该工作目录，")
                print("     如果迁移后打不开这条对话，请手动打开一次该工作目录")

        # 软冲突覆盖：先在事务内删数据库行，文件留到 commit 成功之后再删
        # （反过来做的话，commit 失败会出现"行还在、正文没了"的不一致）。
        #
        # override_deleted 必须在 commit **之前**落盘：它是回滚把旧对话整条还原的
        # 唯一依据，等 commit 与删文件都做完再写的话，中途崩掉会留下
        # 「库里已经删了、meta 里没记」的状态，回滚会静默漏掉这条旧对话。
        # 先记 intent 是安全的：回滚的还原动作（INSERT OR REPLACE 原行 + 从备份
        # 原样复制回文件）在「其实还没删」时是幂等的。
        override_files = []
        if override_row:
            ov_id = override_row["id"]
            conn.execute("DELETE FROM session_usage WHERE session_id = ?", (ov_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (ov_id,))
            override_files = find_project_files(dst_ep, ov_id)
            meta["override_deleted"] = True
            meta["target_overwritten"] = dst_row is not None
            _write_meta(bp, meta)

        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        n = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ? AND user_id = ?", (sid, target_uid)
        ).fetchone()[0]
        _report_verify("session 行已写入目标库", n)
        meta["target_overwritten"] = dst_row is not None
        _write_meta(bp, meta)

        if override_row:
            # remove_path 现在不抛 OSError（失败返回 False），所以这里的
            # except 分支是死代码；而且不管删没删掉都打印 ✅，用户以为清干净了。
            stuck = [f.name for f in override_files if not remove_path(f, quiet=True)]
            print(f"  ✅ 已删除目标中的旧对话行 {override_row['id'][:8]}…"
                  + (f"（{len(stuck)} 个附属文件未能删除）" if stuck else ""))
            if stuck:
                print(f"  ⚠️  未删掉的附属文件：{', '.join(stuck)}")
                print("     通常是仍被客户端 / 索引服务占用；这些文件已备份，"
                      "回滚会原样还原，也可手动清理")
            _write_meta(bp, meta)
    finally:
        conn.close()

    # 复制正文文件
    print("\n📄 复制对话正文...")
    # 目标目录必须与【写进库里的 cwd】一致：库行直接沿用源行，cwd 也是源的值，
    # 客户端会按这个 cwd 推导 slug。以前优先沿用目标侧旧的 slug 目录，
    # 目标原行 cwd 与源不同时就会出现"库行 cwd 指向新目录、正文却在旧目录"
    # → 客户端按新 cwd 推 slug 找不到正文。
    # 目录名优先用【源侧真实存在的目录名】：那是客户端按 cwd 实际建出来的 slug。
    # cwd_to_slug() 只是我们自己推的规则（折叠连续 "-"、盘符转小写），只作兜底。
    slug = ""
    if src_info.files:
        slug = src_info.files[0].parent.name
    if not slug:
        slug = cwd_to_slug(src_row.get("cwd", "")) or cwd_to_slug(src_info.cwd)
    if slug:
        dst_dir = dst_ep.projects_dir / slug
    else:
        # 源侧连正文都没有（罕见）：没有文件要复制，沿用目标已有目录即可
        existing0 = find_project_files(dst_ep, sid)
        if not existing0:
            raise RuntimeError(
                "无法确定目标 projects 子目录（会话记录里 cwd 为空），"
                "为避免正文落到 projects 根目录导致客户端打不开，已中止；请回滚本次迁移"
            )
        dst_dir = existing0[0].parent
    dst_dir.mkdir(parents=True, exist_ok=True)
    if dst_dir == dst_ep.projects_dir:
        raise RuntimeError("目标目录退化成 projects 根目录，已中止（请回滚本次迁移）")

    existing = find_project_files(dst_ep, sid)
    if existing and existing[0].parent != dst_dir:
        print(f"  ℹ️  目标原有正文在 {existing[0].parent.name}/，本次写入 {dst_dir.name}/"
              f"（库里的 cwd 已改为源的值，客户端按新目录查找）")

    for f in src_info.files:
        if not f.exists():
            continue
        target = dst_dir / f.name
        if not safe_copy(f, target):
            raise RuntimeError("复制对话正文失败，已中止（请先回滚或将源数据手动恢复）")
        # 先登记再继续：以前是循环跑完才写 meta["copied_to"]，第 2 个及以后文件
        # 失败时，先前已复制到目标侧的文件不在 meta 里 → 精确回滚漏删 → 孤儿正文。
        # （_clone_intra 早已按这个顺序修过，跨版本这条路径漏改。）
        meta["copied_to"].append(str(target))
        _write_meta(bp, meta)
        mark = "目录" if f.is_dir() else "文件"
        print(f"  ✅ {f.name} [{mark}] ({fmt_size(path_size(f))})")

    # 覆盖场景：目标侧「源里没有」的同 id 附属文件（例如目标多出的 {sid}.meta.json /
    # tool-results/）要清掉，否则新旧混搭、客户端可能读到过期元数据。
    # 这些文件已备份在 dst_files/，回滚会原样还原。
    # 以前这里的判据是 `if dst_row:`：目标行不存在时（典型场景——上次迁移半途崩了，
    # 库行回滚掉了但正文没删）明明会削到这些孤儿文件，却因为没"行"就整段跳过，
    # 新旧文件混在同一目录里，客户端可能读到过期元数据。
    # 现在改成 always：备份那一步已无条件把原文件存进 dst_files/，删得放心。
    if existing:
        copied_names = {f.name for f in src_info.files}
        for f in existing:
            if f.name in copied_names or not f.exists():
                continue
            print(f"  ℹ️  目标侧残留文件 {f.name} 本次将被替换"
                  f"{'（目标库中已无对应行，属于孤儿文件）' if not dst_row else ''}")
            # quiet=True：失败提示由下面这条带上下文的分支统一打，别重复两遍
            if remove_path(f, quiet=True):
                print(f"  ✅ 已清理目标侧多余的附属文件 {f.name}")
            else:
                print(f"  ⚠️  未能清理目标侧旧文件 {f.name}（已备份在 dst_files/，"
                      f"可手动删除）")

    # 任务数据
    src_tasks = src_ep.tasks_dir / src_row["id"]
    if src_tasks.exists():
        dst_tasks = dst_ep.tasks_dir / sid
        if dst_tasks.exists() and not remove_path(dst_tasks):
            # 顶层只捕 RuntimeError / sqlite3.Error，裸 OSError 会直接变 traceback，
            # 而此时数据库已经 commit、正文也复制完了 —— 必须给出可操作的回滚提示。
            raise RuntimeError("清理目标侧已存在的任务目录失败（数据库已写入，请执行回滚）")
        # 复制失败同理：先登记再抛，半截目录也要能被回滚清掉
        if not copy_tree(src_tasks, dst_tasks):
            meta["tasks_copied"] = True
            _write_meta(bp, meta)
            raise RuntimeError("复制任务数据失败（数据库已写入，请执行回滚）")
        meta["tasks_copied"] = True
        _write_meta(bp, meta)
        print("  ✅ 任务数据已复制")

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
            _report_verify("源 session 行已删除", left, expect=0)
            meta["source_deleted"] = True
            _write_meta(bp, meta)
        finally:
            conn.close()
        for f in src_info.files:
            if f.exists():
                kind = "目录" if f.is_dir() else "文件"  # 需在删除前判断
                # remove_path 只对**不可恢复**的情况抛 OSError，其余失败返回 False，
                # 只 catch OSError 的话，"返回 False" 那一路照样打印 ✅，
                # 用户以为删干净了，结果源和目标各留一份。
                # remove_path 失败只返回 False（不抛），所以这里的 except 是死代码；
                # 且它自己还会打一段 ⚠️，调用方再打一遍就是双重信息。
                # quiet=True 关掉它自带的那段，由下面两条带上下文的说明统一输出。
                ok = remove_path(f, quiet=True)   # 可能是文件，也可能是 tool-results 目录
                if ok:
                    print(f"  ✅ 已删除源{kind} {f.name}")
                else:
                    print(f"  ⚠️  未能删除源{kind} {f.name}（被占用或权限不足）")
                    print("     目标侧已写入完成；该源文件需要你手动清理（否则两边各留一份）")
        if src_tasks.exists():
            # 走 remove_path：只读的任务文件在 Windows 上 rmtree 会直接失败，
            # 而它会先抹掉只读位重试一次（见 remove_path）
            if remove_path(src_tasks, quiet=True):
                print(f"  ✅ 已删除源任务目录 {src_tasks.name}")
            else:
                print(f"  ⚠️  未能删除源任务目录 {src_tasks.name}（被占用或只读）")
                print("     目标侧已复制完成；该源目录需要你手动清理")
        # 文件删完后源目录可能空了，顺手清理，避免残留空目录
        for d in {f.parent for f in src_info.files}:
            try:
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
                    print(f"  ✅ 已清理空目录 {d.name}")
            except Exception:
                pass

    _write_meta(bp, meta)

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


def interactive(force=False, mode=None, dry_run=False, assume_yes=False,
                target_uid=None, backup_root=None, assume_closed=False,
                on_conflict=None, src=None, dst=None, query=None):
    """交互式向导

    force: 透传 --force。以前向导里检测不可信（tasklist/ps 调用失败）时固定
           force=False，直接 exit(2)，命令行给的 --force 形同虚设。
    on_conflict: 透传 --on-conflict。以前向导里三处调用都把冲突策略写死成 "ask"，
           `--on-conflict overwrite` 在没给 --session-id 时形同虚设。
    src / dst / query: 透传 --from / --to / --query，给了就不再重复问。
    assume_closed: 透传 --assume-clients-closed（只绕「检测失败」，不绕「真检测到」）。
    mode / dry_run / assume_yes / target_uid / backup_root：同样透传命令行值。
    以前向导路径把它们**全部静默丢弃**（自己重问 mode、把 assume_yes 写死 False），
    `--mode copy --yes` 之类在「没给 --session-id」时形同虚设。
    传 None / False 表示"命令行没给"，此时由向导照常询问。

    历史上这里还有一个 `dir_arg` 形参：main() 从未传值、函数体内也从未引用，
    而本脚本的 CLI 根本没有 --dir 选项（--dir 是 migrate.py 的参数），已删除。
    """
    print("=" * 70)
    print("WorkBuddy 单对话跨版本迁移")
    print("=" * 70)

    # 与显式 --session-id 那条路径同口径：--dry-run 只看计划、不写盘，
    # 不该要求关客户端（以前向导里给了 --dry-run 照样被这条守卫拦下）
    if not dry_run and not require_clients_closed(force, assume_closed=assume_closed):
        sys.exit(2)

    # 命令行没给时按 ask（do_migrate 的默认值），但显式传 None 会盖掉它的默认
    on_conflict = on_conflict or "ask"

    if src:
        print(f"\n【第 1 步】源版本：{src}（来自命令行 --from）")
        src_name = src
    else:
        src_name = choose_edition("\n【第 1 步】选择【源版本】（对话当前所在的版本）")
    if dst:
        print(f"\n【第 2 步】目标版本：{dst}（来自命令行 --to）")
        dst_name = dst
    else:
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
    # --query 透传：以前向导里这一律按「列出全部」处理，命令行给的过滤词白给了
    sid = pick_session(src_ep, query=query)

    if mode:
        # 命令行已明确指定 --mode，不再重复询问
        print(f"\n【第 4 步】迁移模式：{mode}（来自命令行 --mode）")
    else:
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

    if dry_run:
        # 命令行给了 --dry-run：只预览，不落盘，也不再问"是否正式执行"
        do_migrate(src_ep, dst_ep, sid, mode=mode, on_conflict=on_conflict,
                   dry_run=True, assume_yes=True, target_uid=target_uid,
                   backup_root=backup_root)
        print("\n  [--dry-run] 未做任何改动")
        return

    # 默认 move 会删源，先预览一次更稳妥（非交互 --yes 时跳过询问）
    if not assume_yes and ask_yes_no("\n是否先【空跑预览】不写盘（dry-run）？(y/N): "):
        do_migrate(src_ep, dst_ep, sid, mode=mode, on_conflict=on_conflict,
                   dry_run=True, assume_yes=True, target_uid=target_uid,
                   backup_root=backup_root)
        if not ask_yes_no("\n按上面的计划【正式执行】？(y/N): "):
            print("已取消")
            return

    do_migrate(src_ep, dst_ep, sid, mode=mode, on_conflict=on_conflict,
               dry_run=False, assume_yes=assume_yes, target_uid=target_uid,
               backup_root=backup_root)


# ---------------------------------------------------------------- CLI


def main():
    parser = argparse.ArgumentParser(
        description="WorkBuddy 单对话跨版本迁移（国内版 ⇄ 国际版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --from / --to 默认 None（而不是写死 domestic）：向导路径要靠 None 区分
    # "命令行没给，该问用户" 与 "用户确实选了 domestic"
    parser.add_argument("--from", dest="src", choices=["domestic", "intl"], default=None,
                        help="源版本（默认询问；显式给值时向导不再问）")
    parser.add_argument("--to", dest="dst", choices=["domestic", "intl"], default=None,
                        help="目标版本（默认询问；显式给值时向导不再问）")
    parser.add_argument("--session-id", "-i", help="要迁移的对话 id（支持前缀）")
    parser.add_argument("--list", "-l", action="store_true", help="列出源版本的对话")
    parser.add_argument("--query", "-q", help="按标题/路径/id 过滤")
    parser.add_argument("--mode", "-m", choices=["move", "copy"], default=None,
                        help="迁移语义：move=迁移后删除源（默认），copy=保留源")
    parser.add_argument("--on-conflict", choices=["ask", "skip", "overwrite", "newer"],
                        default=None, help="冲突策略（默认 ask；向导路径同样生效）")
    parser.add_argument("--target-uid", help="手动指定目标版本的 user_id")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="非交互模式：跳过确认询问（冲突策略见 --on-conflict，"
                             "无终端时 ask 会降级为 skip）")
    parser.add_argument("--force", action="store_true", help="跳过「客户端必须关闭」检测")
    parser.add_argument("--assume-clients-closed", action="store_true",
                        help="只在【进程检测本身失败】时放行（比 --force 温和）："
                             "真检测到客户端仍在运行照样拦下来")
    parser.add_argument("--backups", action="store_true", help="列出可回滚的备份")
    parser.add_argument("--rollback", help="回滚指定备份（标签或前缀）")
    parser.add_argument("--full", action="store_true", help="与 --rollback 配合：整库恢复")
    parser.add_argument("--backup-dir",
                        help="备份目录：新建的备份放这里；查找/回滚时作为【额外】搜索根，"
                             "仍会回退扫描两个版本的标准目录")

    args = parser.parse_args()

    # 依赖参数检查：以前 --full 不带 --rollback 会被静默忽略 —— 用户以为自己选了
    # "整库恢复"，实际跑的还是不带 --full 的精确回滚，两边的数据结构捋不一致。
    # 与 migrate.py 的「--target 必须配 --source」「--generate-commands 必须配
    # --restore-tasks」同口径：这类组合静默忽略只会让人以为参数生效了。
    if args.full and not args.rollback:
        print("❌ --full 必须与 --rollback 一起使用"
              "（它决定的是【回滚】走整库恢复还是精确回滚，单独给不产生任何效果）")
        print("   用法：python3 scripts/migrate_session.py --rollback <TAG> --full")
        sys.exit(1)

    # --target-uid 直接决定「对话归到哪个账号下」，拼错就是把数据迁到一个
    # 不存在的 uid 里（表现同样是"迁移成功但对话消失"）。与 migrate.py 的
    # --target 同口径：不像 UUID 就告警，非交互模式另要一次显式确认。
    if args.target_uid and legacy is not None and not legacy._looks_like_uid(args.target_uid):
        print(f"⚠️  --target-uid 不像一个 WorkBuddy user_id（应为 UUID 形态）：{args.target_uid}")
        print("   拼错会把对话迁到不存在的账号下，请确认；可用 --rollback 回滚。")
        if not args.yes and not ask_yes_no("   确认继续？(y/N): "):
            print("已取消")
            sys.exit(1)

    if legacy is None:
        print("⚠️  未找到 scripts/migrate.py，部分功能（当前账号推断）将降级")

    if args.backups:
        list_backups(args.backup_dir)
        return

    if args.rollback:
        # 回滚同样要改库（UPDATE/DELETE/覆盖）+ 删文件，客户端持锁时
        # 内存缓存会在退出时把回滚结果覆盖回去 —— 与迁移路径同一风险，
        # 因此同样要求客户端已关闭（--backups 只读，不检测）。
        if not require_clients_closed(args.force,
                                      assume_closed=args.assume_clients_closed):
            sys.exit(2)
        rollback(args.rollback, args.backup_dir, full=args.full, assume_yes=args.yes)
        return

    # 显式路径上 --from / --to 没给时按 domestic（这两个参数的默认值是 None，
    # 好让向导区分"没给，该问"和"确实选了 domestic"）
    src_edition = args.src or "domestic"
    dst_edition = args.dst or "domestic"

    src_ep = resolve_edition(src_edition)

    if args.list:
        rows = list_sessions(src_ep, args.query)
        print_session_table(src_ep, rows)
        return

    if not args.session_id:
        # 透传命令行参数：以前只传 force，--mode / --yes / --dry-run / --target-uid /
        # --backup-dir 在"未给 --session-id"时全部被静默丢弃
        interactive(force=args.force, mode=args.mode, dry_run=args.dry_run,
                    assume_yes=args.yes, target_uid=args.target_uid,
                    backup_root=args.backup_dir,
                    assume_closed=args.assume_clients_closed,
                    on_conflict=args.on_conflict,
                    src=args.src, dst=args.dst, query=args.query)
        return

    # 显式指定对话 id
    if not args.dry_run and not require_clients_closed(
            args.force, assume_closed=args.assume_clients_closed):
        sys.exit(2)

    dst_ep = resolve_edition(dst_edition)
    if not src_ep.exists():
        print(f"❌ 源版本没有数据：{src_ep.db}")
        print(f"   本次使用的数据目录：{src_ep.root}")
        print("   若它属于另一个版本，请用 --from / --to 换方向，"
              "或设 WORKBUDDY_MIGRATE_HOME 指向正确的 home")
        sys.exit(1)
    if src_edition != dst_edition and not dst_ep.exists():
        print(f"❌ 目标版本没有数据：{dst_ep.db}")
        print(f"   本次使用的数据目录：{dst_ep.root}")
        print("   请至少登录并使用一次目标版本，或用 --to 换一个目标版本")
        sys.exit(1)

    do_migrate(
        src_ep, dst_ep, args.session_id,
        # `or "ask"` 与向导侧写法对齐：显式路径上 argparse 的默认值也是 None
        # （好区分"没给"与"确实选了"），靠 do_migrate 的 fallthrough 也能等价
        # 落到 ask，但两处写法不一致早晚有人踩
        mode=args.mode or "move", on_conflict=args.on_conflict or "ask",
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
