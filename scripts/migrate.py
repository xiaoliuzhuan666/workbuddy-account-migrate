#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 账号迁移工具
将旧账号的 Session、Memory、Connector 数据迁移到当前登录账号

用法:
  python3 migrate.py                           # 交互式向导（推荐）
  python3 migrate.py --diagnose                # 诊断模式：查看所有账号数据分布
  python3 migrate.py --source USER_ID          # 指定源账号迁移（高级用户）
  python3 migrate.py --source USER_ID --yes    # 跳过确认直接迁移
  python3 migrate.py --intl                    # 强制使用国际版数据目录 ~/.workbuddy-ai
  python3 migrate.py --dir PATH                # 显式指定数据目录（优先级高于 --intl）
  python3 migrate.py --rollback TIMESTAMP      # 回滚到指定备份
  python3 migrate.py --restore-tasks           # 恢复历史任务到当前 session
  python3 migrate.py --restore-tasks --session SESSION_ID  # 恢复指定 session 的任务
  python3 migrate.py --list-tasks              # 列出所有历史任务概览
"""

import argparse
import csv
import json
import os
import platform
import re
import stat
import subprocess
import sys

# Windows 终端可能使用 GBK/CP936 编码，强制 stdout/stderr 为 UTF-8 避免 emoji 崩溃。
# line_buffering=True：否则 Windows 下输出被块缓存，迁移过程的实时进度看不到。
#
# ⚠️ 必须先判断编码再包装：调用方（tests/run_tests.py、migrate_session.py）可能已经
# 包过一层，再包一次会共用同一个 buffer，而前一个 wrapper 失去引用被 GC 时会把
# buffer 一起关掉 —— 之后所有 print 都变成
# `ValueError: I/O operation on closed file`（实测用例 [22] 必崩）。
# 与 migrate_session.py / tests/ 两个脚本保持同一套写法。
if platform.system() == "Windows":
    import io
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name)
        _enc = (getattr(_stream, "encoding", "") or "").lower().replace("-", "")
        if _enc != "utf8":
            try:
                _stream.flush()
                setattr(
                    sys, _name,
                    io.TextIOWrapper(
                        _stream.buffer, encoding="utf-8", errors="replace",
                        line_buffering=True,
                    ),
                )
            except Exception:
                pass

import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

def _strip_sandbox_shim():
    """WorkBuddy 会话内运行时，注入的 PYTHONPATH 指向沙箱 shim（sitecustomize.py），
    会劫持 Path.mkdir 等文件操作：即使 exist_ok=True，目录已存在也抛 EEXIST。
    本脚本仅用标准库，直接剥离 PYTHONPATH 后 re-exec 自身，根治劫持。
    （等价于 `env -u PYTHONPATH python3 migrate.py ...`，但用户无需记住特殊用法）
    """
    if os.environ.pop("PYTHONPATH", None) is not None:
        os.execv(sys.executable, [sys.executable] + sys.argv)


def restart_client(delay=3):
    """迁移完成后延迟自动重启 WorkBuddy 客户端（macOS）

    必须后台延迟执行：若从 WorkBuddy 会话内（AI/Bash）调用本脚本，quit 会
    连带杀掉当前进程树。start_new_session 脱离进程组 + 先 sleep，让脚本把
    结果输出完整，再退出客户端并重新拉起，会话列表立即刷新。
    """
    system = platform.system()
    if system == "Darwin":
        chain = (
            f"sleep {delay}; "
            f"osascript -e 'tell application \"WorkBuddy\" to quit'; "
            f"sleep 3; open -a WorkBuddy"
        )
        subprocess.Popen(["bash", "-c", chain], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  🔄 {delay} 秒后自动重启 WorkBuddy 客户端（当前会话会中断，属预期行为）")
    else:
        print(f"  ⚠️  自动重启目前仅支持 macOS，请手动重启 WorkBuddy 让变更生效")


def _home_override() -> str:
    """WORKBUDDY_MIGRATE_HOME 的值（未设置时返回空串）"""
    return os.environ.get("WORKBUDDY_MIGRATE_HOME", "")


def _home() -> Path:
    """home 目录，支持环境变量覆盖（测试时指向临时 fixture，不碰真实数据）"""
    env = _home_override()
    return Path(env) if env else Path.home()


# user_id 形如 12345678-1234-1234-1234-123456789abc（8-4-4-4-12 十六进制）。
# 只凭"目录名里有连字符"判断会把 projects-backup 之类普通目录当成账号。
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _looks_like_uid(name: str) -> bool:
    """判断目录名是否像一个 WorkBuddy user_id"""
    return bool(UUID_RE.match(name or ""))


def _find_workbuddy_dir():
    """自动探测数据目录：国际版用 ~/.workbuddy-ai，国内版用 ~/.workbuddy"""
    ai_dir = _home() / ".workbuddy-ai"
    # 目录存在且非空即视为国际版（不依赖 DB 是否已生成）
    if ai_dir.is_dir() and any(ai_dir.iterdir()):
        return ai_dir
    return _home() / ".workbuddy"


def _setup_paths(edition):
    """按版本切换到对应的 WorkBuddy 数据目录

    Args:
        edition: "domestic" 使用 ~/.workbuddy，其他值（"intl"）使用 ~/.workbuddy-ai

    等价于 _set_workbuddy_dir(_home() / "<版本目录>")，与 _find_workbuddy_dir()
    共用同一套派生路径推导，避免两处逻辑各自演化。
    """
    _set_workbuddy_dir(_home() / (".workbuddy-ai" if edition == "intl" else ".workbuddy"))


def _get_storage_json_path():
    """storage.json 候选路径：仅返回确实存在的路径，否则返回 None

    国内版登录态权威来源。国际版不使用此文件（改用 account-snapshot.json）。

    候选路径一律基于 _home()：一旦用 WORKBUDDY_MIGRATE_HOME 指向 fixture，
    就不会再去读真实机器的平台路径，避免把真实登录态带进测试/迁移。
    """
    system = platform.system()
    home = _home()
    candidates = []
    if system == "Darwin":
        candidates.append(home / "Library" / "Application Support" / "WorkBuddy" / "User" / "globalStorage" / "storage.json")
    elif system == "Windows":
        # APPDATA 为空串 / 只有空白 / 相对路径时不能直接 Path(...)：
        # Path("") 是相对路径，会解析成【当前工作目录】，storage.json 的候选路径
        # 于是飘到 cwd 下去（读到无关文件，或把别的目录误认成登录态）。
        appdata = (os.environ.get("APPDATA") or "").strip()
        # home 被覆盖时，APPDATA 指向的是真实机器，必须改从 home 推导
        if appdata and not _home_override() and Path(appdata).is_absolute():
            base = Path(appdata)
        else:
            base = home / "AppData" / "Roaming"
        candidates.append(base / "WorkBuddy" / "User" / "globalStorage" / "storage.json")
    else:
        # Linux：home 被覆盖时 XDG_CONFIG_HOME 指向的是真实机器，同样不能读。
        # XDG_CONFIG_HOME 被设成【空串】时 os.environ.get(...) 返回 "" 而不是默认值，
        # Path("") 就成了「当前工作目录」——与 Windows 分支同样的坑，必须显式过滤。
        xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
        if xdg and not _home_override() and Path(xdg).is_absolute():
            config_home = Path(xdg)
        else:
            config_home = home / ".config"
        candidates.append(config_home / "WorkBuddy" / "User" / "globalStorage" / "storage.json")
    for p in candidates:
        if p.exists():
            return p
    return None


def _is_intl_dir(path: Path) -> bool:
    """是否为国际版数据目录（~/.workbuddy-ai）"""
    return Path(path).name == ".workbuddy-ai"


def _storage_json_for(wb_dir: Path):
    """该数据目录对应的平台 storage.json

    国际版**不读**平台 storage.json——它是国内版的登录态文件，机器上同时装了
    两个版本时，读它会把国内版的 uid 当成国际版的当前账号，
    于是 --intl 迁移会把国际版数据改到一个国际版里根本不存在的账号下，
    表现就是"迁移成功但所有对话消失"。国际版一律走 account-snapshot.json。

    --dir 指向任意非标准目录时（拼错、自定义路径）不能默认按国内版处理：
    先按目录内有没有 account-snapshot.json 判断，都判断不了才回落平台路径并告警。
    """
    wb_dir = Path(wb_dir)
    if _is_intl_dir(wb_dir):
        return None
    if wb_dir.name != ".workbuddy":
        # 非标准目录名：目录内有账号快照 → 按国际版处理，别去读真机 storage.json
        if (wb_dir / "storage" / "skeleton" / "account-snapshot.json").exists():
            return None
        print(f"⚠️  数据目录 {wb_dir} 不是标准的 ~/.workbuddy 或 ~/.workbuddy-ai，"
              f"将按国内版读取平台 storage.json 推断登录态；"
              f"若不符预期请用 --target 显式指定目标账号")
    return _get_storage_json_path()


def _set_workbuddy_dir(path):
    """设置数据目录并重算所有派生路径（供 --dir / --intl 调用）"""
    global WORKBUDDY_DIR, DB_PATH, MEMORY_DIR, CONNECTORS_DIR, TASKS_DIR
    global STORAGE_JSON, ACCOUNT_SNAPSHOT, BACKUP_DIR
    WORKBUDDY_DIR = Path(path).expanduser()
    DB_PATH = WORKBUDDY_DIR / "workbuddy.db"
    MEMORY_DIR = WORKBUDDY_DIR / "memory"
    CONNECTORS_DIR = WORKBUDDY_DIR / "connectors"
    TASKS_DIR = WORKBUDDY_DIR / "tasks"
    STORAGE_JSON = _storage_json_for(WORKBUDDY_DIR)
    ACCOUNT_SNAPSHOT = WORKBUDDY_DIR / "storage" / "skeleton" / "account-snapshot.json"
    BACKUP_DIR = WORKBUDDY_DIR / "migrate_backups"


WORKBUDDY_DIR = _find_workbuddy_dir()
DB_PATH = WORKBUDDY_DIR / "workbuddy.db"
MEMORY_DIR = WORKBUDDY_DIR / "memory"
CONNECTORS_DIR = WORKBUDDY_DIR / "connectors"
TASKS_DIR = WORKBUDDY_DIR / "tasks"
STORAGE_JSON = _storage_json_for(WORKBUDDY_DIR)

# 国际版登录态权威来源：account-snapshot.json 的 primary.uid
ACCOUNT_SNAPSHOT = WORKBUDDY_DIR / "storage" / "skeleton" / "account-snapshot.json"

# 备份目录
BACKUP_DIR = WORKBUDDY_DIR / "migrate_backups"


def get_client_login_uid():
    """客户端真实登录态：{数据目录}/storage/skeleton/account-snapshot.json → primary.uid

    这是**左侧会话列表按哪个 uid 过滤**的直接来源——面板跟着它走。
    国内版历史上只认平台 storage.json，而两者可能长期不一致
    （2026-09-22 实例：storage.json 记的是扩展侧账号，客户端实际登录的却是另一个账号），
    导致迁移每次都并到面板看不到的账号，重启后面板依旧空白。
    """
    if ACCOUNT_SNAPSHOT.exists():
        try:
            with open(ACCOUNT_SNAPSHOT, encoding="utf-8") as f:
                data = json.load(f)
            primary = data.get("primary") or {}
            return primary.get("uid", "") or ""
        except Exception:
            pass
    return ""


def get_client_login_nickname():
    """account-snapshot.json 里的昵称，仅用于把 uid 打印成人能看懂的样子"""
    if ACCOUNT_SNAPSHOT.exists():
        try:
            with open(STORAGE_JSON, encoding="utf-8") as f:
                data = json.load(f)
            return ((data.get("primary") or {}).get("nickname") or "")
        except Exception:
            pass
    return ""


def get_storage_json_uid():
    """扩展侧记录的账号：平台 storage.json → genie.userId

    可能滞后于客户端登录态（账号切换后不一定同步更新），因此只作为第二优先级。
    """
    if STORAGE_JSON is None:
        return ""
    try:
        with open(STORAGE_JSON, encoding="utf-8") as f:
            return json.load(f).get("genie.userId", "") or ""
    except Exception:
        return ""


def get_panel_uid_hint():
    """旁证：daemon.log 里最近一次 listSessions 用的 uid —— 面板实际过滤用的就是它

    只用于打印，不参与目标账号判定（日志解析不该成为决策依据）。
    """
    log_file = WORKBUDDY_DIR / "logs" / "daemon.log"
    if not log_file.exists():
        return ""
    try:
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return ""
    hit = ""
    for line in tail.splitlines():
        if "listSessions" in line:
            # daemon.log 里嵌套 JSON 的引号是转义的（\"userId\"），必须容忍反斜杠
            m = re.search(r'\\?"userId\\?"\s*:\s*\\?"([0-9a-fA-F-]{36})', line)
            if m:
                hit = m.group(1)
    return hit


def get_db_top_uid():
    """DB 中 session 数最多的 user_id（辅助验证，不能当权威）"""
    if not DB_PATH.exists():
        return ""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, COUNT(*) as cnt FROM sessions "
            "WHERE user_id IS NOT NULL GROUP BY user_id ORDER BY cnt DESC LIMIT 1"
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""
    finally:
        if conn is not None:
            conn.close()


def get_current_user_id(verbose=True):
    """获取当前登录的 user_id

    优先级（v1.6.3 调整）：
    1. **account-snapshot.json → primary.uid**（客户端真实登录态，左侧面板按它过滤）
    2. storage.json → genie.userId（扩展侧记录，账号切换后可能滞后）
    3. workbuddy.db 中 session 数最多的 user_id（辅助兜底）

    verbose=False 时不重复打印「两个来源不一致」（diagnose 已经单独排版展示过）。

    ⚠️  不要再用「storage.json 优先」（v1.4~v1.6.2 的旧策略）：
    国内版实测两者会长期不一致，迁移会并到面板看不到的账号，重启后面板依旧空白。
    ⚠️  也不要用「最新 session」：旧账号切换前的最后一条 session 可能更新。
    """
    client_uid = get_client_login_uid()
    storage_uid = get_storage_json_uid()
    db_uid = get_db_top_uid()

    if verbose and client_uid and storage_uid and client_uid != storage_uid:
        print("⚠️  登录态有两个来源且不一致！")
        print(f"   客户端登录态 (account-snapshot.json，面板按它过滤): {client_uid}"
              f"{' (' + get_client_login_nickname() + ')' if get_client_login_nickname() else ''}")
        print(f"   扩展侧记录   (storage.json  genie.userId)         : {storage_uid}")
        hint = get_panel_uid_hint()
        if hint and hint != client_uid:
            print(f"   daemon 最近 listSessions 的 uid                    : {hint}")
            print("   （与客户端登录态也不一致，说明期间发生过账号切换，重启后以登录态为准）")
        print(f"   → 本次以客户端登录态为准: {client_uid}")
        print(f"   → 迁移请显式指定 --target {client_uid}，否则数据会并到面板看不到的账号")
        print()

    if client_uid:
        return client_uid
    if storage_uid:
        print(f"⚠️  未读到客户端登录态，回落到 storage.json 记录的账号: {storage_uid}")
        return storage_uid
    if db_uid:
        print(f"⚠️  未读到任何登录态，回落到 DB 中 session 最多的账号: {db_uid}")
        return db_uid

    print("❌ 无法获取当前 user_id（account-snapshot.json / storage.json 和 DB 均无数据）")
    return ""


def get_all_user_ids():
    """扫描所有已知的 user_id"""
    user_ids = set()

    # 从 DB
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL")
            for row in cur.fetchall():
                user_ids.add(row[0])
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()   # 异常时也要关，否则连接泄漏

    # 从 memory 文件（同样只认 UUID 形态，或已在 DB 里出现过的 uid）
    if MEMORY_DIR.exists():
        for f in MEMORY_DIR.glob("*_memory.md"):
            uid = f.stem.replace("_memory", "")
            if _looks_like_uid(uid) or uid in user_ids:
                user_ids.add(uid)

    # 从 connectors 目录
    if CONNECTORS_DIR.exists():
        for d in CONNECTORS_DIR.iterdir():
            if d.is_dir() and d.name not in ("default", "skills") and not d.name.startswith("."):
                # 只认 UUID 形态的目录（"含连字符"会把普通目录误判成账号）
                if _looks_like_uid(d.name):
                    user_ids.add(d.name)

    return sorted(user_ids)


def get_session_counts():
    """获取各 user_id 的 session 数量"""
    counts = {}
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id, COUNT(*) FROM sessions WHERE user_id IS NOT NULL GROUP BY user_id")
            for row in cur.fetchall():
                counts[row[0]] = row[1]
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()   # 异常时也要关，否则连接泄漏
    return counts


def get_memory_sizes():
    """获取各 user_id 的 memory 文件大小

    判定口径必须与 get_all_user_ids() 一致：只认 UUID 形态（或已在 DB 里出现过）
    的文件名。以前这里照单全收，`随便一个笔记_memory.md` 会被当成一个"账号"
    统计出来，与 --diagnose 列出的账号对不上。
    """
    sizes = {}
    if not MEMORY_DIR.exists():
        return sizes
    known = set(get_session_counts())
    for f in MEMORY_DIR.glob("*_memory.md"):
        uid = f.stem.replace("_memory", "")
        if _looks_like_uid(uid) or uid in known:
            sizes[uid] = f.stat().st_size
    return sizes


def get_connector_info():
    """获取各 user_id 的 connector 配置信息"""
    info = {}
    if CONNECTORS_DIR.exists():
        for d in CONNECTORS_DIR.iterdir():
            # 判定要与 get_all_user_ids 完全一致（含 startswith(".")）：
            # 否则隐藏目录会被算进 connector 统计，却不在账号列表里
            if (d.is_dir() and d.name not in ("default", "skills")
                    and not d.name.startswith(".") and _looks_like_uid(d.name)):
                mcp_file = d / "mcp.json"
                states_file = d / "connector-states.json"
                mcp_servers = 0
                states_count = 0
                if mcp_file.exists():
                    try:
                        # 必须显式 utf-8：Windows 默认 GBK，含中文的 mcp.json 会解码失败
                        with open(mcp_file, encoding="utf-8") as f:
                            mcp_data = json.load(f)
                        mcp_servers = len(mcp_data.get("mcpServers", {}))
                    except Exception as e:
                        print(f"  ⚠️  读取失败 {mcp_file.name}: {e}")
                if states_file.exists():
                    try:
                        with open(states_file, encoding="utf-8") as f:
                            states_data = json.load(f)
                        states_count = len(states_data) if isinstance(states_data, dict) else 0
                    except Exception as e:
                        print(f"  ⚠️  读取失败 {states_file.name}: {e}")
                info[d.name] = {"mcp_servers": mcp_servers, "connector_states": states_count}
    return info


def diagnose():
    """诊断模式：展示所有账号数据分布"""
    client_uid = get_client_login_uid()
    storage_uid = get_storage_json_uid()
    panel_uid = get_panel_uid_hint()

    print("=" * 70)
    print("WorkBuddy 账号数据诊断")
    print("=" * 70)

    # 登录态可能有两个来源，先把它们摆出来 —— 这是「迁移后左侧面板仍空白」的头号原因
    print("\n登录态来源:")
    nick = get_client_login_nickname()
    print(f"  account-snapshot.json 客户端登录态（左侧面板按它过滤）: {client_uid or '-'}"
          f"{('  「' + nick + '」') if nick else ''}")
    print(f"  storage.json genie.userId 扩展侧记录（账号切换后可能滞后）: {storage_uid or '-'}")
    print(f"  daemon 最近 listSessions uid（面板最近一次刷新用的）    : {panel_uid or '-'}")
    if client_uid and storage_uid and client_uid != storage_uid:
        print("\n  ⚠️  前两者不一致 —— 这是「迁移完左侧列表仍空白」的典型原因。")
        print("     迁移时务必显式加 --target，并指向【客户端登录态】那个 uid。")

    current_uid = get_current_user_id(verbose=False)
    all_uids = get_all_user_ids()
    session_counts = get_session_counts()
    memory_sizes = get_memory_sizes()
    connector_info = get_connector_info()

    print(f"\n当前登录（本次判定）: {current_uid}\n")

    print(f"{'user_id':<40} {'Sessions':>8} {'Memory':>10} {'Connectors':>12} {'当前':>4}")
    print("-" * 80)

    for uid in all_uids:
        sc = session_counts.get(uid, 0)
        ms = memory_sizes.get(uid, 0)
        ms_str = f"{ms / 1024:.1f}KB" if ms > 0 else "-"
        ci = connector_info.get(uid, {})
        conn_str = f"{ci.get('mcp_servers', 0)}mcp/{ci.get('connector_states', 0)}conn" if ci else "-"
        is_current = "✅" if uid == current_uid else ""
        print(f"{uid:<40} {sc:>8} {ms_str:>10} {conn_str:>12} {is_current:>4}")

    print(f"\n总计: {len(all_uids)} 个账号")
    print()

    if len(all_uids) <= 1:
        print("⚠️  只发现一个账号，无需迁移。")
        return

    # 建议迁移方向：按数据量排序，只对真正有数据的账号给命令（0 session + 0KB 的噪音不列）
    other_uids = [u for u in all_uids if u != current_uid]
    other_uids.sort(
        key=lambda u: (session_counts.get(u, 0), memory_sizes.get(u, 0)),
        reverse=True,
    )
    if other_uids:
        print("💡 迁移建议（按数据量排序）:")
        for uid in other_uids:
            sc = session_counts.get(uid, 0)
            ms = memory_sizes.get(uid, 0)
            print(f"   {uid[:20]}... → 当前账号 ({sc} sessions, {ms / 1024:.1f}KB memory)")

        targets = [u for u in other_uids
                   if session_counts.get(u, 0) > 0 or memory_sizes.get(u, 0) > 0]
        if targets:
            print(f"\n   执行命令: python3 migrate.py --source {targets[0]} --target {current_uid}")
            print("   （--target 必须写成上面【客户端登录态】的 uid，否则数据会并到面板看不到的账号）")
            print("   （源账号 memory 文件按设计保留，重复执行是幂等的，不会重复写入）")


def _ask_yes_no(prompt: str) -> bool:
    """交互式 y/N 确认；stdin 耗尽或 Ctrl-C 时按"否"处理而不是抛异常

    与 scripts/migrate_session.py 的 ask_yes_no 对齐：那里 EOFError 与
    KeyboardInterrupt 都捕，这里以前只捕 EOFError，于是 migrate.py 的任何
    确认处按 Ctrl-C 都会抛一段 traceback。
    """
    try:
        return input(prompt).strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print()   # 让输出换行，避免和提示挤在一行
        return False


# ---------------------------------------------------------------- 客户端进程检测

# 客户端进程名关键字（与 scripts/migrate_session.py 保持一致）
PROC_KEYWORDS = ("workbuddy", "codebuddy")

# 本工具自身的脚本名（用于把自己从"正在运行的客户端"里排除掉）
_SELF_SCRIPT_NAMES = ("migrate.py", "migrate_session.py", "run_tests.py", "prepare_fixture.py")


def _is_self_process(pid: str, cmdline: str) -> bool:
    """判断某条进程记录是不是本工具自己（或调用它的测试进程）

    仓库目录名含 "workbuddy"，脚本自己的命令行就会命中进程关键字，
    不排除的话用户会被"检测到客户端正在运行"无条件拦住，只能加 --force 关掉整项检查。

    只按「路径成分」匹配：以前用 `"migrate.py" in cmdline` 这种裸子串匹配，
    任何路径里恰好含 migrate.py 的进程（第三方同名脚本、备份副本）都会被当成自己，
    等于静默关闭了客户端检测。

    ⚠️  已知局限：命令行里只给裸脚本名（`python migrate.py`）时无法区分是不是
    本仓库的那份，只能当成自己。所以这套判定是**启发式**——误拦可用 --force
    绕过，漏报则会在客户端持锁时继续写库；非 Windows 的进程枚举也未实测。
    """
    try:
        if str(pid).isdigit() and int(pid) == os.getpid():
            return True
    except (ValueError, AttributeError):
        pass
    norm = (cmdline or "").lower().replace("\\", "/")
    if not norm:
        return False
    script_dir = str(Path(__file__).resolve().parent).lower().replace("\\", "/")
    if script_dir and script_dir in norm:
        return True
    # 脚本名兜底只在「裸名」或「就在本仓库目录下」时生效：以前按 basename 直接判真，
    # 任何路径下叫 migrate.py 的第三方脚本都会被当成自己，等于静默关掉客户端检测。
    repo_root = str(Path(__file__).resolve().parent.parent).lower().replace("\\", "/")
    for token in re.split(r"[\s\"']+", norm):
        if not token:
            continue
        cleaned = token.rstrip(":")
        if cleaned.rsplit("/", 1)[-1] not in _SELF_SCRIPT_NAMES:
            continue
        if "/" not in cleaned or cleaned.startswith(repo_root + "/"):
            # 裸脚本名（`python migrate.py`）无法区分是不是本仓库的那份，宁可当成自己：
            # 误拦可用 --force 绕过，漏报则会带锁写库
            return True
    return False


def _client_display_name(cmdline: str, keyword: str) -> str:
    """从进程命令行里取出一个可读的进程名

    Windows 的 tasklist 给的就是映像名；macOS/Linux 的 ps 给的是完整命令行
    （可能是一长串 Electron 参数），直接展示会让用户看不出是哪个进程。
    """
    text = (cmdline or "").strip()
    if not text:
        return keyword
    for token in text.split():
        if keyword in token.lower():
            cleaned = token.strip("\"'")
            base = cleaned.rstrip(":").replace("\\", "/").rsplit("/", 1)[-1]
            return base or cleaned
    return text.split()[0]


def find_running_clients():
    """检测正在运行的 WorkBuddy / CodeBuddy 客户端进程

    返回 (进程名列表, 检测是否可信)。检测命令失败（tasklist / ps 不存在或报错）时
    不能静默当成"没有客户端在跑"——那会让迁移在客户端持锁的情况下继续。
    """
    found = set()
    system = platform.system()
    try:
        procs = []
        if system == "Windows":
            procs.append(subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
            ))
        else:
            # comm= 只有进程名；args= 带完整命令行，能匹配到安装路径里的关键字
            for ps_args in (["ps", "-eo", "pid=,comm="], ["ps", "-eo", "pid=,args="]):
                procs.append(subprocess.run(
                    ps_args, capture_output=True, text=True,
                    errors="replace", timeout=15,
                ))

        for proc in procs:
            if proc.returncode != 0:
                raise RuntimeError(
                    f"{' '.join(str(a) for a in proc.args[:2])} 退出码 {proc.returncode}"
                    f"（stderr: {(proc.stderr or '').strip()[:80]}）"
                )
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                if system == "Windows":
                    row = next(csv.reader([line]), [])
                    if not row:
                        continue
                    pid = row[1] if len(row) > 1 else ""
                    name = row[0]
                else:
                    parts = line.split(None, 1)
                    if len(parts) != 2:
                        continue
                    pid, name = parts[0], parts[1]
                if _is_self_process(pid, name):
                    continue
                hit = next((k for k in PROC_KEYWORDS if k in name.lower()), "")
                if hit:
                    found.add(_client_display_name(name, hit))
    except Exception as e:
        # 检测失败要如实上报，由调用方决定如何处理
        print(f"⚠️  客户端进程检测失败（{e}），无法确认客户端是否已关闭")
        return [], False
    return sorted(found), True


def _stdin_can_prompt() -> bool:
    """当前能不能向用户提问

    输入被重定向 / 走管道 / 无终端时，`input()` 要么立刻 EOF、要么把进程挂住。
    服务端场景一律按"问不到人"处理，保持可直接判定退出码的行为。
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def require_clients_closed(force=False, assume_closed=False) -> bool:
    """迁移/回滚前确认客户端已关闭

    force（--force）：连「确实检测到客户端在跑」也一起跳过，风险最大的总开关。
    assume_closed（--assume-clients-closed）：**只在检测本身失败时**生效——
    你确认「客户端都已退出」但检测手段不可用、无法验证。此时「真的检测到
    客户端」那条拦截依然有效，比 --force 温和得多。
    """
    running, trustworthy = find_running_clients()
    if not running:
        if not trustworthy and not (force or assume_closed):
            print("  ⚠️  无法确认客户端是否已关闭（进程检测本身失败了，")
            print("     这不是「检测到客户端在跑」，只是查不出来）。两条出路：")
            print("       · --assume-clients-closed  我已确认客户端全部退出，谨慎继续")
            print("         （真检测到客户端仍在运行时仍会拦截）")
            print("       · --force                  连「检测到客户端在跑」也一并跳过（风险最大）")
            return False
        if not trustworthy:
            print("  ⚠️  进程检测不可信，已按你的确认谨慎继续（无法验证客户端是否已关闭）")
        return True

    print("=" * 70)
    print("❌ 检测到 WorkBuddy 客户端正在运行")
    print("=" * 70)
    for n in running:
        print(f"   • {n}")
    print()
    print("  迁移/回滚前必须关闭【两个版本】的 WorkBuddy 窗口，原因：")
    print("   1. 数据还在 WAL 日志里没落盘，会读到旧数据")
    print("   2. 客户端内存缓存会在退出时把你的修改覆盖回去")
    print("   3. 客户端持锁时写入/覆盖数据库可能失败")
    print()
    if force:
        print("  ⚠️  已用 --force 跳过检测，后果自负。")
        print()
        return True
    # 被拦下时不再直接退进程：先给一次「我确认已关闭，强制继续」的机会。
    # 用户输入 y = 继续（后果等同 --force，但要他当场确认）；
    # 回车 / 其他输入 = 取消。
    if not _stdin_can_prompt():
        # 不是终端（CI、管道、被重定向）就问不到人，问了还会把进程挂住 ——
        # 保持原来的行为：直接拒绝、退出码 2，由调用方给 --force。
        print("  请关闭所有 WorkBuddy 窗口后重新运行本命令。")
        print("  （确知风险可用 --force 跳过，但不建议）")
        return False
    if not _ask_yes_no("  已确认这些窗口都已关闭、接受上面三条风险？(输入 y 强制继续，回车取消): "):
        print("  已取消。请关闭所有 WorkBuddy 窗口后重新运行本命令。")
        return False
    print("  ⚠️  已按你的确认强制继续（客户端可能仍在运行，后果自负）。")
    print()
    return True


def _file_state(p: Path):
    """文件是否存在 + (大小, mtime)；用来判断「这一次有没有真的写进去」"""
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8"):
    """原子写：先写同名 .tmp 再 os.replace

    追加 Memory 时如果写到一半失败，会留下半截 RAW_JSON 块（注释未闭合），
    客户端下次解析直接异常。所以改成"读-拼-整体原子替换"。

    失败必须原样向上抛，且不能留下 .tmp：os.replace 抛 OSError（磁盘满 / 权限
    不足 / 目标被占用）时以前没有任何处理，调用方以为写成功了，磁盘上却是旧内容
    —— meta.json 就此缺项，回滚失去唯一依据。

    ⚠️  scripts/migrate_session.py 里有一份逐行相同的实现，**刻意不合并**：
    meta.json 的原子写是回滚的最后一道保险，不该依赖可选的 import。
    改这里请同步改那边（判去重与否看风险，不看"是否重复"）。
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


def _remove_db_sidecars(db_path: Path):
    """删除数据库的 -wal / -shm 边车文件

    只覆盖主库文件而不清理它们，SQLite 下次打开会把与新主库不匹配的旧 WAL 重放上去，
    轻则恢复无效、重则数据错乱。恢复/整库回滚前必须调用。

    返回是否全部清理成功：删不掉（客户端仍持有句柄）时必须让调用方知道，
    否则"清理失败但照样覆盖主库"等于没清理。
    """
    ok = True
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            try:
                p.unlink()
                print(f"  ✅ 已清理 {p.name}")
            except OSError as e:
                ok = False
                print(f"  ⚠️  清理 {p.name} 失败：{e}")
                print("     通常是 WorkBuddy 仍持有该文件句柄，请关闭客户端后重试")
    return ok


def _backup_db(src: Path, dst: Path) -> bool:
    """用 sqlite backup API 复制数据库（能带上 WAL 里还没落盘的数据）

    直接 shutil.copy2 主库文件只能拿到上次 checkpoint 的快照：客户端崩溃或
    未退出时，最近的会话还在 workbuddy.db-wal 里，备份会是陈旧的。
    失败时返回 False，调用方应提示用户。
    """
    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(dst))
        with dst_conn:
            src_conn.backup(dst_conn)
        # 保持与源库一致：源库是 WAL 而备份产物默认 DELETE 模式，
        # 恢复/直接用别的工具打开时行为可能不一致
        try:
            mode = src_conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() == "wal":
                dst_conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"  ⚠️  数据库在线备份失败（{e}）")
        return False
    finally:
        if src_conn is not None:
            src_conn.close()
        if dst_conn is not None:
            dst_conn.close()


def create_backup(target_uid, timestamp, source_uid=""):
    """创建备份

    meta.json 除 target_uid（rollback 依赖它）外，额外记录 source_uid 与当时的
    两个登录态来源、各账号数据量 —— 事后复盘「为什么并错了方向」时全靠它。

    备份目录名是 {时间戳}_{uid 前 8 位}，同一秒内对同一账号重复迁移会撞名，
    因此目录已存在且非空时自动加序号，避免 meta.json 被覆盖（前一份备份失去回滚依据）。

    半途失败会**自清理**：没有 meta.json 的备份既不能回滚，又会在 --backups 里
    留下一条永远找不到的记录（与 migrate_session._abort_backup 同一处理）。
    """
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建备份目录 {BACKUP_DIR}：{e}")

    base_tag = f"{timestamp}_{target_uid[:8]}"
    backup_path = BACKUP_DIR / base_tag
    seq = 2
    while backup_path.exists() and any(backup_path.iterdir()):
        backup_path = BACKUP_DIR / f"{base_tag}_{seq}"
        seq += 1
    backup_tag = backup_path.name
    try:
        backup_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"无法创建备份目录 {backup_path}：{e}")

    # 从这里开始任何一步失败都自清理：半成品备份没有 meta.json，既不能回滚，
    # 又会在 --backups 里留一条永远「找不到」的记录
    try:
        return _create_backup_inner(backup_path, backup_tag, target_uid, timestamp,
                                    source_uid)
    except Exception as e:
        try:
            shutil.rmtree(str(backup_path))
            # 只说清理这件事：失败原因由 RuntimeError 带上去，顶层统一打印，
            # 三层各打一遍会把同一件事说三遍（N5）
            print(f"  🧹 已清理半成品备份目录 {backup_path.name}")
        except OSError as ce:
            print(f"  ⚠️  半成品备份目录未能删除（{ce}）：{backup_path}")
        raise RuntimeError(f"创建备份失败（未改动任何数据）：{e}") from e


def _ensure_writable(p: Path):
    """给文件或目录树补上写权限；失败不影响主流程

    `copy2` / `copytree` 会把源的只读属性一起带到产物上。只读文件在 Windows 上
    删不掉：备份的半成品清不掉、回滚覆盖不了、临时 fixture 也清不掉。
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


def _copy_tree(src: Path, dst: Path) -> bool:
    """复制目录树并补齐写权限；返回是否成功

    与 `migrate_session.copy_tree()` 同一处理，这里刻意**不复用**它：
    migrate.py 不 import migrate_session，而备份 / 回滚是最后一道保险，
    不该依赖任何可选模块（同 `_atomic_write_text` 的判断）。
    """
    try:
        shutil.copytree(str(src), str(dst))
        _ensure_writable(dst)
        return True
    except (OSError, shutil.Error) as e:
        print(f"  ⚠️  复制目录 {src.name} 失败：{e}")
        return False


def _copy_file(src: Path, dst: Path) -> bool:
    """复制单个文件并补齐写权限（`copy2` 会把只读位一起带过去）"""
    try:
        shutil.copy2(str(src), str(dst))
        _ensure_writable(dst)
        return True
    except OSError as e:
        print(f"  ⚠️  复制文件 {src.name} 失败：{e}")
        return False


def _create_backup_inner(backup_path, backup_tag, target_uid, timestamp, source_uid=""):
    """create_backup 的实际步骤（失败由调用方清理后上抛）"""
    # 备份数据库（必须带 WAL，否则客户端没退出时备份是陈旧快照）
    if DB_PATH.exists():
        if _backup_db(DB_PATH, backup_path / "workbuddy.db"):
            print(f"  ✅ 已备份数据库（含 WAL）→ {backup_path / 'workbuddy.db'}")
        else:
            # 在线备份失败（例如源库被独占锁），退回文件复制，但明确告知风险
            shutil.copy2(str(DB_PATH), str(backup_path / "workbuddy.db"))
            print(f"  ⚠️  已退回文件复制备份 → {backup_path / 'workbuddy.db'}")
            print(f"     （可能不含 WAL 中未落盘的数据，回滚前请确认客户端已完全退出）")

    # 备份 Memory
    mem_file = MEMORY_DIR / f"{target_uid}_memory.md"
    if mem_file.exists():
        _copy_file(mem_file, backup_path / f"{target_uid}_memory.md")
        print(f"  ✅ 已备份 Memory → {backup_path / f'{target_uid}_memory.md'}")

    # 备份 Connectors
    conn_dir = CONNECTORS_DIR / target_uid
    if conn_dir.exists():
        dst_dir = backup_path / target_uid
        if dst_dir.exists():
            shutil.rmtree(str(dst_dir))
        _copy_tree(conn_dir, dst_dir)
        print(f"  ✅ 已备份 Connectors → {backup_path / target_uid}/")

    # 写入备份元数据
    # ⚠️ target_uid 是 --rollback 的依赖字段，不能改名/删掉
    meta = {
        "version": 2,
        "kind": "account",
        "tag": backup_tag,
        "timestamp": timestamp,
        "target_uid": target_uid,
        "source_uid": source_uid or "",
        "created_at": datetime.now().isoformat(),
        # 事后复盘用：当时客户端登录态 vs 扩展侧记录（不一致是踩坑的信号）
        "client_login_uid": get_client_login_uid(),
        "storage_json_uid": get_storage_json_uid(),
        "session_counts": get_session_counts(),

        # 记录下来，回滚时才知道目标侧这些是"迁移新建的"还是"本来就有的"
        "target_had_memory": (MEMORY_DIR / f"{target_uid}_memory.md").exists(),
        "target_had_connectors": (CONNECTORS_DIR / target_uid).exists(),
    }
    # 原子写：meta.json 是回滚判断的唯一依据，写到一半断电就成了损坏文件
    # （没有 meta 的备份等于不可用），所以先写 .tmp 再 os.replace。
    _atomic_write_text(
        backup_path / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2)
    )

    print(f"  📦 备份标签: {backup_tag}")
    return backup_tag


def _wal_checkpoint(cur, label="") -> bool:
    """执行 WAL checkpoint，返回是否完全成功

    PRAGMA wal_checkpoint 返回 (busy, log, checkpointed)；busy != 0 表示
    有其他连接占用了锁、checkpoint 没做完——此时读到/写到的可能不是最新状态，
    不能当成"验证通过"。
    """
    cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    row = cur.fetchone() or (1, 0, 0)
    busy = row[0] if len(row) > 0 else 1
    if busy == -1:
        # 非 WAL 模式的库会返回 -1，不代表"有进程占锁"，别报误导性告警
        print(f"  ℹ️  数据库不在 WAL 模式（{label}busy=-1），无需 checkpoint")
        return True
    if busy > 0:
        print(f"  ⚠️  WAL checkpoint 未完成（{label}busy={busy}）："
              f"有其他进程占用数据库锁，请关闭 WorkBuddy 客户端后重试")
        return False
    print(f"  📋 WAL checkpoint 完成（{label}log={row[1]}, checkpointed={row[2]}）")
    return True


def migrate_sessions(source_uid, target_uid):
    """迁移 Session 历史"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        return _migrate_sessions_inner(conn, source_uid, target_uid)
    finally:
        conn.close()   # 以前每个 return 分支各 close 一次，异常分支会漏


def _count_sessions_ro(uid: str) -> int:
    """另开一只读连接统计某账号的 session 数（验证用）

    同一连接上的 SELECT 必然看到自己的写入，不能用来证明"已落盘"；
    只有新开的只读连接在 checkpoint 之后能读到相同结果才算真落盘。
    打不开库时返回 -1。
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
        row = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return -1
    finally:
        if conn is not None:
            conn.close()


def _migrate_sessions_inner(conn, source_uid, target_uid):
    cur = conn.cursor()

    # 先 checkpoint WAL（确保读取到最新数据）
    checkpoint_ok = _wal_checkpoint(cur, "迁移前 ")

    # 统计
    cur.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (source_uid,))
    count = cur.fetchone()[0]

    if count == 0:
        # 以前这个提前 return 完全不提 checkpoint 失败，用户会以为"确实没东西可迁"，
        # 而其实上面的统计读到的可能是旧快照
        if not checkpoint_ok:
            print("  ⚠️  WAL checkpoint 未完成，上面的统计结果可能不是最新状态")
        print(f"  ⏭️  源账号无 session，跳过")
        return 0

    if not checkpoint_ok:
        # 读到的可能是旧快照，写下去也可能被客户端的内存缓存覆盖回去。
        # 必须停下，而不是照常 UPDATE、只在末尾补一句"校验未完成"。
        raise RuntimeError(
            "WAL checkpoint 被其他进程占用，无法确认数据库处于一致状态，已中止"
            "（会话数据未改动）。请关闭两个版本的 WorkBuddy 客户端后重试"
        )

    # 执行迁移
    cur.execute("UPDATE sessions SET user_id = ? WHERE user_id = ?", (target_uid, source_uid))
    migrated = cur.rowcount
    conn.commit()

    # 迁移后再 checkpoint WAL（确保写入持久化）
    # （迁移前的 checkpoint 不通过时已经直接中止了，这里只需看迁移后的结果）
    checkpoint_ok = _wal_checkpoint(cur, "迁移后 ")

    # 验证：另开一只读连接重查（同一连接必然看到自己的写入，不算证据）
    remaining = _count_sessions_ro(source_uid)
    if remaining > 0:
        print(f"  ⚠️  警告：源账号仍有 {remaining} 个 session 未迁移！")
    elif remaining < 0:
        print(f"  ⚠️  校验未完成：无法以只读方式重新打开数据库确认")
    elif not checkpoint_ok:
        # checkpoint 没做完的话，这次查询的结果本身也不可信，不能报"验证通过"
        print(f"  ⚠️  校验未完成：WAL checkpoint 被锁占用，无法确认迁移结果是否已落盘")
    else:
        print(f"  ✅ 验证通过：源账号 session 已全部迁移")

    print(f"  ✅ 迁移 {migrated} 个 session（{source_uid[:12]}... → {target_uid[:12]}...）")
    return migrated


def reset_cloud_mapping(source_uid, backup_dir=None):
    """清掉源账号在 `edge-sync-mapping*.db` 里的云端通道映射

    为什么必须做：左侧列表按 `sessions.user_id` 过滤，但**云端同步**是按 edge-sync
    的 `msg_channel` 记账的。会话的 user_id 改成新账号后，映射表里仍写着
    `convmsg:<旧账号>`，EdgeSync 会判定"这些对话早就同步过了"，于是**不重新上传**。
    后果：本机看得到，换台设备登录新账号却看不到这些历史（云端归属还挂在旧账号）。

    清掉这些映射行后，EdgeSync 下次启动会按新账号的通道重新上传。
    只删映射（不碰对话内容），删除前整库备份，失败也只告警不中断。

    返回 (删除行数, 涉及的库数)。
    """
    if not source_uid:
        return 0, 0
    channel = f"convmsg:{source_uid}"
    total_deleted = 0
    touched = 0

    for db_file in sorted(WORKBUDDY_DIR.glob("edge-sync-mapping*.db")):
        if not db_file.is_file() or db_file.name.endswith(("-shm", "-wal")):
            continue
        conn = None
        try:
            conn = sqlite3.connect(str(db_file))
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='edge_sync_mapping'"
            )
            if not cur.fetchone():
                conn.close()
                continue
            cur.execute(
                "SELECT COUNT(*) FROM edge_sync_mapping WHERE msg_channel = ?", (channel,)
            )
            n = cur.fetchone()[0]
            if n == 0:
                conn.close()
                continue

            # 删除前整库备份（含 WAL/SHM）
            if backup_dir is not None:
                if not backup_dir.exists():
                    backup_dir.mkdir(exist_ok=True)
                dst_dir = backup_dir / "edge-sync"
                if not dst_dir.exists():
                    dst_dir.mkdir(exist_ok=True)
                for suffix in ("", "-wal", "-shm"):
                    src = Path(str(db_file) + suffix)
                    if src.exists():
                        shutil.copy2(str(src), str(dst_dir / src.name))

            cur.execute("DELETE FROM edge_sync_mapping WHERE msg_channel = ?", (channel,))
            deleted = cur.rowcount
            conn.commit()
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            total_deleted += deleted
            touched += 1
            print(f"  🧹 {db_file.name}: 清掉 {deleted} 条指向旧账号云通道的映射")
        except sqlite3.Error as e:
            print(f"  ⚠️  {db_file.name} 处理失败（不影响本地数据，可忽略）：{e}")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    if total_deleted:
        print("  ℹ️  下次启动客户端时，EdgeSync 会把这些对话重新上传到新账号的云端通道")
    return total_deleted, touched


RAW_JSON_RE = re.compile(r"<!--\s*RAW_JSON_START(.*?)RAW_JSON_END\s*-->", re.DOTALL)


def _extract_memory_blocks(content):
    """提取 memory 文件中【所有】RAW_JSON 块里的 memoryBlock 文本

    只比较首个块是不够的：迁移过一次后目标文件里会有两个块，再跑一次迁移时
    首块是目标自己的内容，与源块不同 → 同一个块被追加两遍。
    """
    blocks = []
    for m in RAW_JSON_RE.finditer(content or ""):
        try:
            data = json.loads(m.group(1).strip())
        except Exception as e:
            # 静默 continue 会让这块内存"查不到"→ 去重失效 → 可能被追加两遍
            print(f"  ⚠️  Memory 中有个 RAW_JSON 块解析失败（{e}），该块不参与去重判断")
            continue
        block = data.get("memoryBlock", "")
        if block:
            blocks.append(block)
    return blocks


def migrate_memory(source_uid, target_uid):
    """迁移 Memory 文件（追加合并，语义块级去重）"""
    src_file = MEMORY_DIR / f"{source_uid}_memory.md"
    dst_file = MEMORY_DIR / f"{target_uid}_memory.md"

    if not src_file.exists():
        print(f"  ⏭️  源账号无 Memory 文件，跳过")
        return

    src_content = src_file.read_text(encoding="utf-8").strip()

    if not src_content:
        print(f"  ⏭️  源账号 Memory 为空，跳过")
        return

    if not dst_file.exists():
        # 目标不存在，直接复制（memory 目录本身也可能不存在）
        if not dst_file.parent.exists():
            dst_file.parent.mkdir(parents=True, exist_ok=True)
        dst_file.write_text(src_content, encoding="utf-8")
        print(f"  ✅ 复制 Memory（目标为空，直接复制 {len(src_content)} 字符）")
        return

    # 目标已存在，追加去重
    dst_content = dst_file.read_text(encoding="utf-8").strip()
    src_blocks = _extract_memory_blocks(src_content)
    dst_blocks = _extract_memory_blocks(dst_content)

    if src_blocks and dst_blocks:
        # 结构化 memory：按 memoryBlock 语义块去重（避免空模板按行误追加）
        # 与目标里【所有】已有块比对，保证重复执行不会追加第二遍
        pending = [b for b in src_blocks if b.strip() and b not in dst_blocks]
        if not pending:
            print(f"  ⏭️  源账号 Memory 的 memoryBlock 已存在于目标，跳过")
            return

        # 只追加尚未存在的那些块（完整注释块，保持 Markdown 合法）
        pending_set = set(pending)
        chunks = []
        for m in RAW_JSON_RE.finditer(src_content):
            try:
                data = json.loads(m.group(1).strip())
            except Exception:
                continue
            if data.get("memoryBlock", "") in pending_set:
                chunks.append(m.group(0))
        if not chunks:
            print(f"  ⏭️  源账号 Memory 无可追加内容，跳过")
            return

        # 原子写：追加到一半失败会留下半截 RAW_JSON 块，客户端下次解析直接异常
        _atomic_write_text(
            dst_file,
            dst_content
            + f"\n\n---\n## 迁移自 {source_uid[:12]}...\n\n"
            + "\n\n".join(chunks)
            + "\n",
        )
        print(f"  ✅ 追加源账号 Memory 语义块（{len(chunks)} 块）")
        print(f"  ⚠️  注意：目标文件现在有 {len(dst_blocks) + len(chunks)} 个 RAW_JSON 块，"
              f"WorkBuddy 客户端是否合并读取多个块尚未验证，请打开客户端确认记忆已生效")
        return

    # 旧格式/无 RAW_JSON：退回按行去重
    src_lines = src_content.split("\n")
    dst_lines_set = set(dst_content.split("\n"))
    new_lines = [l for l in src_lines if l.strip() and l not in dst_lines_set]

    if not new_lines:
        print(f"  ⏭️  源账号 Memory 内容已存在于目标，跳过")
        return

    # 追加（同样走原子写）
    _atomic_write_text(
        dst_file,
        dst_content
        + f"\n\n---\n## 迁移自 {source_uid[:12]}...\n\n"
        + "\n".join(new_lines)
        + "\n",
    )

    print(f"  ✅ 追加 {len(new_lines)} 行新内容到 Memory")


def _is_unconfigured(value):
    """目标侧「等于没配过」的值：None / 空串 / 空列表 / 空字典

    只判 `k not in target` 不够：目标里若是一份空壳（`args: []`、`env: {}`、
    `command: ""`），源里同名 key 的实质配置会被挡在外面，等于没合并。
    """
    return value is None or value == "" or value == [] or value == {}


def deep_merge_dict(source, target, path="", stats=None):
    """深度合并字典到 target，返回合并统计 {added: [...], conflicts: [...]}

    策略与 README / SKILL 一致：**目标账号已有配置保留不动**，只把目标没有的
    key 从源补进来（目标侧是空壳的同名 key 视为没配过，同样补齐）。

    两边都有实质内容且不同时**不合并、只记进 conflicts**：以前这种冲突被静默
    吞掉，调用方还打印「无新增内容，跳过」，用户以为合并过了、其实源的配置
    一条都没进去。list（mcp.json 的 args）刻意不做拼接——两个 args 列表拼起来
    得到的是没人想要的结果，只能保留目标值并报告。
    """
    if stats is None:
        stats = {"added": [], "conflicts": []}
    for k, v in source.items():
        here = f"{path}.{k}" if path else str(k)
        cur = target.get(k, None)
        if k not in target or _is_unconfigured(cur):
            target[k] = v
            stats["added"].append(here)
        elif isinstance(v, dict) and isinstance(cur, dict):
            deep_merge_dict(v, cur, here, stats)
        elif v != cur:
            stats["conflicts"].append(here)
    return stats


def migrate_connectors(source_uid, target_uid):
    """迁移 Connector 配置（深度合并）"""
    src_dir = CONNECTORS_DIR / source_uid
    dst_dir = CONNECTORS_DIR / target_uid

    if not src_dir.exists():
        print(f"  ⏭️  源账号无 Connector 目录，跳过")
        return

    # 确保目标目录存在（connectors/ 本身可能不存在，例如国际版新装）
    if not dst_dir.exists():
        dst_dir.mkdir(parents=True, exist_ok=True)

    for fname in ["mcp.json", "connector-states.json"]:
        src_file = src_dir / fname
        dst_file = dst_dir / fname

        if not src_file.exists():
            continue

        # 解析失败不能裸抛：此时 sessions 已经 UPDATE 并 commit 了，
        # 异常会直接冒到顶层，用户很可能连备份标签都没看到，不知道该回滚什么。
        try:
            with open(src_file, encoding="utf-8") as f:
                src_data = json.load(f)
        except (OSError, ValueError) as e:
            print(f"  ⚠️  源 {fname} 解析失败，跳过该文件：{e}")
            print("     （Session 迁移已生效，如不需要请用 --rollback <备份标签> 回滚）")
            continue

        if dst_file.exists():
            try:
                with open(dst_file, encoding="utf-8") as f:
                    dst_data = json.load(f)
            except (OSError, ValueError) as e:
                print(f"  ⚠️  目标 {fname} 解析失败，跳过该文件（未改动）：{e}")
                continue

            if isinstance(src_data, dict) and isinstance(dst_data, dict):
                # 深度合并：必须先合并、再比较合并前后是否有变化。
                # 不能只看"顶层 key 有没有新增"——mcp.json 顶层只有 mcpServers 一个 key，
                # 目标账号只要已有 mcpServers 就会被判成"无新增"而整体跳过，
                # 结果一个 server 都合并不进去（README 宣称的 MCP 深度合并形同虚设）。
                before = json.dumps(dst_data, sort_keys=True, ensure_ascii=False)
                stats = deep_merge_dict(src_data, dst_data)
                after = json.dumps(dst_data, sort_keys=True, ensure_ascii=False)
                if after != before:
                    # 原子写：open(w) 直接覆盖，写到一半断电 / 磁盘满会留下
                    # 截断的 mcp.json，客户端下次启动直接解析失败
                    _atomic_write_text(
                        dst_file, json.dumps(dst_data, indent=2, ensure_ascii=False)
                    )
                    print(f"  ✅ 合并 {fname}（新增 {len(stats['added'])} 项）")
                elif stats["conflicts"]:
                    print(f"  ⏭️  {fname} 没有可补充的内容，目标文件未改动")
                else:
                    print(f"  ⏭️  {fname} 无新增内容，跳过")
                # 冲突必须说出来：以前静默保留目标值，用户会以为源配置已经合并进来了
                if stats["conflicts"]:
                    print(f"     ⚠️  有 {len(stats['conflicts'])} 处两边都配了不同内容，"
                          f"按「目标账号已有配置保留不动」保留了目标值：")
                    for _c in stats["conflicts"][:10]:
                        print(f"        · {_c}")
                    if len(stats["conflicts"]) > 10:
                        print(f"        …以及另外 {len(stats['conflicts']) - 10} 处")
                    print("        如需以源账号的配置为准，请手动编辑目标侧对应项后重跑迁移")
            else:
                print(f"  ⚠️  {fname} 类型冲突，跳过（源={type(src_data).__name__}, 目标={type(dst_data).__name__}）")
        else:
            # 同样走原子写：直接覆盖会留下截断的 JSON
            _atomic_write_text(
                dst_file, json.dumps(src_data, indent=2, ensure_ascii=False)
            )
            print(f"  ✅ 复制 {fname}（目标不存在）")


def _confirm_weak_target_uid(uid: str, skip_confirm: bool) -> bool:
    """目标账号是「从 DB 反推」出来时，整账号迁移前必须显式确认

    单对话迁移（migrate_session._warn_weak_target_uid）只提示不拦，因为错了
    也只是那一条对话。整账号迁移是 `UPDATE sessions SET user_id` 全量改写，
    错了要把整个账号的对话都挂到别人名下 —— 所以这里要真拦一次。

    `skip_confirm`（--yes）= 用户已经说过"别问我"，按已确认处理，但警告照打。
    """
    print("=" * 70)
    print("⚠️  无法确定当前登录账号，目标账号是「猜」出来的")
    print("=" * 70)
    print("  两份登录态文件（account-snapshot.json / 平台 storage.json）都没读到，")
    print(f"  只能按「数据库里 session 数最多」推出 {uid}。")
    print()
    print("  典型机器上这个账号恰恰是**旧账号**（它的历史会话更多）。")
    print("  整账号迁移会把源账号名下全部对话改到它名下 → 表现是")
    print("  「迁移成功、但整个账号的对话都看不见了」。")
    print()
    print("  稳妥做法：Ctrl-C 中止，先用 --diagnose 确认账号分布，")
    print("  再用 --target <USER_ID> 明确指定目标账号。")
    print()
    if skip_confirm:
        print("  ⚠️  已给 --yes，按你已确认继续（后果自负）。")
        return True
    return _ask_yes_no("  仍然要把全部对话迁到这个账号下？(y/N): ")


def migrate(source_uid, target_uid=None, skip_confirm=False, target_is_manual=False,
            reset_mapping=True):
    """执行完整迁移流程

    target_uid: 目标账号 ID。如果为 None，则自动从登录态（account-snapshot.json / storage.json）推断。
    target_is_manual: 目标账号是否由用户手动指定（--target 或交互向导），用于打印区分。
    reset_mapping: 是否清理源账号的 edge-sync 云端通道映射（默认清理，见 reset_cloud_mapping）
    """
    if target_uid is None:
        target_uid = get_current_user_id()
        # get_current_user_id() 在两份登录态文件都没读到时会回落到「DB 里 session
        # 数最多的账号」。整账号迁移是 `UPDATE sessions SET user_id` 全量改写，
        # 而典型机器上 session 最多的恰恰是旧账号 —— 这一步走错的表现是
        # 「迁移成功、但整个账号的对话都看不见了」，所以必须真拦一次。
        if target_uid and not get_client_login_uid() and not get_storage_json_uid():
            if not _confirm_weak_target_uid(target_uid, skip_confirm):
                sys.exit(1)
    if not target_uid:
        print("❌ 无法获取目标账号 user_id，请确认 WorkBuddy 已登录")
        print("   也可使用 --target <USER_ID> 手动指定目标账号")
        sys.exit(1)

    if source_uid == target_uid:
        print("❌ 源账号和目标账号相同，无需迁移")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    print("=" * 70)
    print("WorkBuddy 账号迁移")
    print("=" * 70)
    print(f"\n  源账号:   {source_uid}")
    if target_is_manual:
        target_label = "手动指定"
    elif not get_client_login_uid() and not get_storage_json_uid():
        # 不能再说"当前登录"——登录态文件一份都没读到，这是从 DB 反推出来的
        target_label = "DB 反推（登录态文件都没读到）"
    else:
        target_label = "当前登录"
    print(f"  目标账号: {target_uid} ({target_label})")

    client_uid = get_client_login_uid()
    if client_uid:
        nick = get_client_login_nickname()
        print(f"  客户端登录态: {client_uid}{('  「' + nick + '」') if nick else ''}")
    print()

    # 目标账号 ≠ 客户端登录态的 uid → 迁移会「成功」但面板看不到（今天踩的坑）
    if client_uid and client_uid != target_uid:
        print("⚠️  目标账号不是客户端当前登录的账号！")
        print(f"   左侧会话列表按客户端登录态（{client_uid[:8]}…）过滤，")
        print("   迁完重启后大概率仍然看不到数据。")
        if target_is_manual:
            print("   你已手动指定 --target，确认这是有意为之再继续。")
        else:
            print(f"   建议改用: --target {client_uid}")
        print()

    # Phase 1: 诊断
    print("📊 Phase 1: 诊断数据分布...")
    session_counts = get_session_counts()
    memory_sizes = get_memory_sizes()
    src_sessions = session_counts.get(source_uid, 0)
    src_memory = memory_sizes.get(source_uid, 0)
    print(f"  源账号: {src_sessions} sessions, {src_memory / 1024:.1f}KB memory")
    print(f"  目标账号: {session_counts.get(target_uid, 0)} sessions, {memory_sizes.get(target_uid, 0) / 1024:.1f}KB memory")

    # 注意要把 Connector 也算进去：只有 connector 的账号以前会被判"无任何数据"而静默跳过
    src_has_connectors = (CONNECTORS_DIR / source_uid).is_dir()
    if src_sessions == 0 and src_memory == 0 and not src_has_connectors:
        print("\n⚠️  源账号无任何数据（session / memory / connector 都没有），无需迁移")
        # 只说这一句会误导：两个版本都装时自动探测会选国际版，用户在国内版里
        # 明明有数据，却看到"无任何数据"、退出码还是 0，很容易当成"真没东西可迁"。
        # 把本次用的目录和"怎么换版本"一起打出来。
        print(f"     本次使用的数据目录：{WORKBUDDY_DIR}")
        print(f"     源账号 {source_uid[:8]}… 在该目录下确实没有数据。")
        print("     若它属于另一个版本，请显式指定目录后重试：")
        print("       --intl                     国际版（~/.workbuddy-ai）")
        print("       --dir <数据目录路径>       显式指定（优先级最高）")
        # 退出码 3 = 无数据可迁、未做任何改动。以前返回 0，自动化会把
        # 「没东西可迁」误判成「迁移成功」（见 README「退出码」）。
        print("\n  退出码 3 = 无数据可迁，本次未做任何改动")
        sys.exit(3)

    # 确认
    if not skip_confirm:
        print()
        answer = _ask_yes_no("确认执行迁移？(y/N): ")
        if not answer:
            print("已取消")
            return

    # Phase 2: 备份
    print("\n📦 Phase 2: 创建备份...")
    try:
        backup_tag = create_backup(target_uid, timestamp, source_uid=source_uid)
    except Exception:
        # 备份失败时一个字节都还没动过，不需要回滚指引，别让用户以为数据有问题。
        # 这里不复述原因：create_backup 抛的 RuntimeError 里已经写了
        # 「创建备份失败（未改动任何数据）」，由顶层收口统一打印一次。
        raise
    # 立刻打印备份标签：Phase 3 中途失败时用户才知道该回滚哪一个（以前只在
    # Phase 5 收尾打印，失败的话标签从未出现过）
    print(f"  🔙 如需撤销，执行: python3 migrate.py --rollback {backup_tag}")

    # Phase 3: 迁移
    print("\n🔄 Phase 3: 执行迁移...")
    # 先记住目标 Memory / Connector 的「迁移前」状态：收尾文案要按「这次真的写了
    # 什么」判断，不能只看目标文件在不在——目标本来就有一份时文件必然存在，
    # Memory 因去重被跳过也会被误报成「已迁移」。
    _mem_before = _file_state(MEMORY_DIR / f"{target_uid}_memory.md")
    _conn_before = {
        _f: _file_state(CONNECTORS_DIR / target_uid / _f)
        for _f in ("mcp.json", "connector-states.json")
    }

    try:
        print("\n  [Session 迁移]")
        migrated_sessions = migrate_sessions(source_uid, target_uid)

        print("\n  [Memory 迁移]")
        migrate_memory(source_uid, target_uid)

        print("\n  [Connector 迁移]")
        migrate_connectors(source_uid, target_uid)
    except Exception as e:
        # 迁移是分步提交的（Session 先 commit），异常必须给出可操作的回滚指引，
        # 而不是让用户对着 traceback 猜发生了什么
        print(f"\n❌ 迁移中断：{e}")
        print(f"   Session 迁移可能已经生效。回滚命令：")
        print(f"   python3 migrate.py --rollback {backup_tag} --yes")
        raise

    # 实际有没有东西落到目标账号（用于判断是否白跑一趟、以及收尾文案是否诚实）
    # 只看「目标文件存在」不够：目标本来就有一份时文件必然存在，Memory 因去重
    # 被跳过也会被误报成「已迁移」。要看这次是不是真的写进去了（新建或内容变了）。
    def _written(p, before):
        now = _file_state(p)
        return now is not None and (before is None or now != before)

    memory_done = src_memory > 0 and _written(
        MEMORY_DIR / f"{target_uid}_memory.md", _mem_before)
    # 同样不能只看目标 connector 目录是否存在：migrate_connectors() 会无条件
    # mkdir 目标目录，源侧只有空目录时也会建出空目录 → 误报"已迁移"。
    connectors_done = src_has_connectors and any(
        _written(CONNECTORS_DIR / target_uid / _f, _conn_before[_f])
        for _f in ("mcp.json", "connector-states.json")
    )

    if migrated_sessions == 0 and not memory_done and not connectors_done:
        # 备份目录已经建了但什么都没迁：留着会在备份列表里出现一个空备份
        empty_bp = BACKUP_DIR / backup_tag
        if empty_bp.exists():
            try:
                shutil.rmtree(str(empty_bp))
            except OSError as e:
                # Windows 上文件被占用会抛 PermissionError；迁移本身已判定为无事可做，
                # 不该在收尾阶段抛 traceback
                print(f"  ⚠️  空备份目录清理失败（{e}），可手动删除：{empty_bp}")
        print("\n⚠️  没有任何数据被迁移，已清理空的备份目录")
        # 注意：这里不只是"源账号空"这一种情况 —— 源有数据、但与目标完全重合
        # （sessions 已在目标名下、memory 全被去重、connector 无新增）也会走到这行，
        # 所以文案不能只写"源账号无数据"，否则用户会对着有数据的源账号一头雾水。
        print("     可能是源账号确实没有数据，也可能是源里的内容目标已经有了（合并后无新增）")
        # 与上面「源账号无任何数据」同一口径：什么都没迁就不能返回 0
        print("  退出码 3 = 无数据可迁，本次未做任何改动")
        sys.exit(3)

    print("\n  [云端通道映射（edge-sync）]")
    if reset_mapping:
        reset_cloud_mapping(source_uid, backup_dir=BACKUP_DIR / backup_tag)
    else:
        print("  ⏭️  已按 --keep-cloud-mapping 跳过")
        print("     ⚠️  不清理的话，这些对话仍挂在旧账号的云端通道下，")
        print("        换台设备登录新账号时看不到它们（本机不受影响）")

    # Phase 4: 验证
    print("\n✅ Phase 4: 验证...")
    new_session_counts = get_session_counts()
    new_target_sessions = new_session_counts.get(target_uid, 0)
    print(f"  当前账号 session 数: {session_counts.get(target_uid, 0)} → {new_target_sessions}")

    # Phase 4.5: 面板一致性检查 —— 数据迁对了但面板看不到，是最常见的"看起来没成功"
    client_uid = get_client_login_uid()
    if client_uid and client_uid != target_uid:
        print()
        print("⚠️  迁移已写入，但目标账号 ≠ 客户端当前登录账号：")
        print(f"   目标账号     : {target_uid}")
        print(f"   客户端登录态 : {client_uid}"
              f"{('  「' + get_client_login_nickname() + '」') if get_client_login_nickname() else ''}")
        print("   左侧会话列表按客户端登录态过滤，重启后可能仍然看不到数据。二选一：")
        print(f"     ① 在客户端切到/登录 {target_uid[:8]}… 再看")
        print(f"     ② 回滚后重跑并加 --target {client_uid}")
        print(f"       回滚: python3 migrate.py --rollback {backup_tag} --yes")


    # 顺带校验 Memory / Connector，别让"静默跳过"看起来像成功
    tgt_mem = MEMORY_DIR / f"{target_uid}_memory.md"
    if src_memory > 0:
        if tgt_mem.exists():
            print(f"  ✅ Memory 文件已就位（{tgt_mem.stat().st_size / 1024:.1f}KB）")
        else:
            print(f"  ⚠️  源账号有 {src_memory / 1024:.1f}KB Memory，但目标账号没有生成 "
                  f"{target_uid[:8]}…_memory.md —— 上面的 Memory 迁移可能被跳过了，请回看 Phase 3 输出")
    src_conn = CONNECTORS_DIR / source_uid
    tgt_conn = CONNECTORS_DIR / target_uid
    if src_conn.exists():
        src_mcp = src_conn / "mcp.json"
        tgt_mcp = tgt_conn / "mcp.json"
        if src_mcp.exists() and not tgt_mcp.exists():
            print("  ⚠️  源账号有 mcp.json，目标账号没有 —— Connector 合并可能被跳过，请回看 Phase 3 输出")

    # Phase 5: 收尾
    print("\n" + "=" * 70)
    print("迁移完成！")
    print("=" * 70)
    print(f"\n  📦 备份标签: {backup_tag}")
    # 按实际结果打印：以前无论有没有跳过都写 "memory + connectors"，容易被误读成都成功了
    print(
        f"  🔄 已迁移: {migrated_sessions} sessions"
        f" + memory{'（已迁移）' if memory_done else '（跳过：无内容或内容与目标一致）'}"
        f" + connectors{'（已迁移）' if connectors_done else '（跳过：无内容或已被跳过）'}"
        f"{' + 云端通道映射已重置' if reset_mapping else ''}"
    )
    print(f"\n  ⚠️  请重启 WorkBuddy 客户端让变更生效！")
    print(f"  📁 备份位置: {BACKUP_DIR / backup_tag}")
    print(f"  🔙 回滚命令: python3 migrate.py --rollback {backup_tag}")


def rollback(backup_tag, skip_confirm=False):
    """回滚到指定备份"""
    # 标签来自命令行，可能是相对/绝对路径（如 ../../etc）——直接拼进 BACKUP_DIR
    # 会被用于 rmtree / copytree / copy2 的目标推导，必须限制在备份目录内。
    if (not backup_tag
            or "/" in backup_tag or "\\" in backup_tag
            or os.path.isabs(backup_tag)
            or ".." in Path(backup_tag).parts):
        print(f"❌ 备份标签不合法：{backup_tag!r}")
        print("   标签只能是备份目录名（如 20260921153000_12345678），不能包含路径分隔符")
        sys.exit(1)

    backup_path = BACKUP_DIR / backup_tag
    if not backup_path.exists():
        print(f"❌ 备份不存在: {backup_tag}")
        print("   （回滚需要备份目录名，可用 ls 查看 " + str(BACKUP_DIR) + "）")
        sys.exit(1)

    # 读取元数据。meta 必须先初始化为空字典：下面「无 meta.json」的兼容分支里
    # target_uid 为空会跳过整个恢复块，但静态检查看不出这层关系，
    # 一旦将来有人改动了这个守卫，就会直接 NameError 崩栈。
    meta = {}
    meta_file = backup_path / "meta.json"
    target_uid = ""
    if meta_file.exists():
        try:
            with open(meta_file, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            # 解析失败也要走「无 meta」这条路：以前这里只打一句 ⚠️，
            # 而下面「缺 meta 所以跳过 Memory/Connectors」的说明只在 else 分支，
            # 用户看不到"该怎么办"。
            print(f"⚠️  备份 meta.json 无法解析（{e}）")
            meta = {}
        if str(meta.get("kind", "")).startswith("session"):
            print("❌ 这是【单对话迁移】的备份，整账号回滚不适用：")
            print("   python3 scripts/migrate_session.py --rollback <TAG>")
            sys.exit(1)
        target_uid = meta.get("target_uid", "")
    else:
        # 备份目录名格式为 {时间戳}_{uid前8位}，据此推算（旧备份可能没有 meta.json）
        parts = backup_tag.split("_")
        if len(parts) >= 2 and parts[-1]:
            # 这个前缀只用来识别是哪份备份，不会被当成 target_uid 用：
            # 缺 meta.json 就无法确定目标账号，Memory / Connectors 会被跳过（见下）
            print(f"  ℹ️  备份缺少 meta.json（目录名里的目标账号前缀是 {parts[-1]}，仅用于识别备份）")
            print("     没有 meta.json 就无法确定目标账号，Memory / Connectors 将被跳过")

    if not target_uid:
        # meta.json 缺失 / 损坏 / 没有 target_uid 三种情况统一在这里说明，且放在
        # 确认询问【之前】——让用户在决定要不要回滚时就先知道会少恢复什么。
        # target_uid 为空时 CONNECTORS_DIR / target_uid 会退化成整个 connectors 目录，
        # 此时绝不能做任何删除/覆盖操作，否则会误删全部连接器配置。
        print("  ℹ️  无法确定目标账号（meta.json 缺失、损坏或没有 target_uid），")
        print("     已跳过 Memory / Connectors 的恢复；请改用带完整 meta.json 的备份。")
        print("     （target_uid 为空时路径会退化成整个目录，不能做任何删除操作）")

    print("=" * 70)
    print("WorkBuddy 账号迁移回滚")
    print("=" * 70)
    print(f"\n  备份标签: {backup_tag}")
    print(f"  目标账号: {target_uid}")
    print()

    print("  ⚠️  回滚是【整库覆盖】：迁移之后其他账号新产生的对话/任务也会一起被抹掉。")
    print("     只想撤销某一条对话的迁移，请用 scripts/migrate_session.py --rollback。")
    print()

    if skip_confirm:
        answer = "y"
    else:
        answer = "y" if _ask_yes_no("确认回滚？这将覆盖当前数据！(y/N): ") else "n"
    if answer != "y":
        print("已取消")
        return

    # 恢复数据库。
    # ⚠️ 顺序很关键：只有确实要覆盖主库时才清理 -wal / -shm。以前无条件先删边车、
    # 再判断备份里有没有 workbuddy.db —— 传错标签（例如只含 memory 的备份目录）时
    # 数据一点没恢复，却已经把未 checkpoint 的 WAL 删掉了 = 永久丢失。
    # 文件操作必须收口 OSError：Windows 上文件被客户端 / 索引服务占用会抛
    # PermissionError，以前直接冒到顶层变 traceback —— 用户既不知道哪一步失败，
    # 也不知道回滚到底进行到什么程度了。
    fs_errors = []

    def _fs(label, fn):
        try:
            fn()
            return True
        except OSError as e:
            fs_errors.append(f"{label}：{e}")
            print(f"  ⚠️  {label} 失败：{e}")
            return False

    db_backup = backup_path / "workbuddy.db"
    if db_backup.exists():
        # 必须先清掉 -wal / -shm，否则 SQLite 会把与新主库不匹配的旧 WAL
        # 重放上去，回滚结果可能失效甚至数据错乱
        if not _remove_db_sidecars(DB_PATH):
            print("  ❌ 未清理 -wal / -shm，继续覆盖主库会让回滚失效，已中止。")
            print("     请关闭 WorkBuddy 客户端后重新执行 --rollback")
            sys.exit(1)
        if _fs("恢复数据库", lambda: shutil.copy2(str(db_backup), str(DB_PATH))):
            print("  ✅ 已恢复数据库")
        else:
            # 数据库没恢复成功就不能宣称回滚过了，直接停下让用户处理
            print("  ❌ 数据库恢复失败，回滚未完成。请确认客户端已完全退出后重试")
            sys.exit(1)
    else:
        print("  ⚠️  备份里没有 workbuddy.db，跳过数据库恢复（未改动当前数据库）")

    if not target_uid:
        # 原因与"该怎么办"已在上面（确认询问之前）说明过，这里不重复
        print("  ⚠️  已跳过 Memory / Connectors 的恢复")
    else:
        # 恢复 Memory：只要备份里有就恢复，不要求目标文件当前必须存在
        mem_backup = backup_path / f"{target_uid}_memory.md"
        if mem_backup.exists():
            def _restore_mem():
                if not MEMORY_DIR.exists():
                    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
                _copy_file(mem_backup, MEMORY_DIR / f"{target_uid}_memory.md")
            if _fs("恢复 Memory", _restore_mem):
                print("  ✅ 已恢复 Memory")

        # 恢复 Connectors：同样只看备份里有没有。
        # 目标目录当前不存在是常见情况（比如迁移后手动清理过），此时应当重建而不是跳过。
        conn_backup = backup_path / target_uid
        conn_target = CONNECTORS_DIR / target_uid
        if conn_backup.exists():
            def _restore_conn():
                if conn_target.exists():
                    shutil.rmtree(str(conn_target))
                _copy_tree(conn_backup, conn_target)
            if _fs("恢复 Connectors", _restore_conn):
                print("  ✅ 已恢复 Connectors")
        else:
            print("  ⏭️  备份中没有 Connector 数据，跳过")
            # 目标账号迁移前没有 connector，但迁移复制出来了 → 回滚必须撤掉，
            # 否则回滚后源、目标双份并存（里面是源账号的连接器配置，可能带 token）
            if not meta.get("target_had_connectors", True):
                leftover = CONNECTORS_DIR / target_uid
                if leftover.exists():
                    if _fs("删除迁移新建的 Connectors", lambda: shutil.rmtree(str(leftover))):
                        print(f"  ✅ 已删除迁移新建的 Connectors（迁移前目标账号没有它）")

        # Memory 同理：迁移新建的目标 memory 文件要一并撤销
        if not meta.get("target_had_memory", True) and not mem_backup.exists():
            leftover_mem = MEMORY_DIR / f"{target_uid}_memory.md"
            if leftover_mem.exists():
                if _fs("删除迁移新建的 Memory", lambda: leftover_mem.unlink()):
                    print("  ✅ 已删除迁移新建的 Memory（迁移前目标账号没有它）")

    if fs_errors:
        print(f"\n  ⚠️  有 {len(fs_errors)} 项没有恢复成功（最常见原因：客户端仍占用文件）：")
        for _e in fs_errors:
            print(f"     · {_e}")
        print("     关闭客户端后重跑同一条 --rollback 即可续做（已完成的部分是幂等的）")

        # 恢复 edge-sync 云端通道映射（迁移时清过，回滚要还原，否则映射永久丢失，
        # 客户端会把所有对话当"未同步"重新上传一遍）
        es_backup = backup_path / "edge-sync"
        if es_backup.exists():
            restored = 0
            for f in sorted(es_backup.iterdir()):
                if f.is_file():
                    shutil.copy2(str(f), str(WORKBUDDY_DIR / f.name))
                    restored += 1
            if restored:
                print(f"  ✅ 已恢复 edge-sync 映射（{restored} 个文件）")
        else:
            print("  ⏭️  备份中没有 edge-sync 映射，跳过")

    print("\n  ⚠️  请重启 WorkBuddy 客户端让变更生效！")


def interactive_migrate(skip_edition_prompt=False, skip_confirm=False,
                        keep_cloud_mapping=False):
    """交互式迁移向导：列出所有账号，用户分别选择源和目标

    skip_confirm：透传给 migrate() 的 --yes。以前命令行的 --yes 在这条路径上
    被静默丢弃，向导最后照样弹「确认执行迁移？」，与 --help 里
    「--yes 跳过确认直接迁移/回滚」不符。
    """

    # 版本选择：默认沿用自动探测结果，--intl 时跳过询问
    detected = "intl" if WORKBUDDY_DIR.name == ".workbuddy-ai" else "domestic"
    if not skip_edition_prompt:
        default_choice = "2" if detected == "intl" else "1"
        print("=" * 70)
        print("WorkBuddy 版本选择")
        print("=" * 70)
        print()
        print("  1. 国内版（数据目录 ~/.workbuddy）")
        print("  2. 国际版（数据目录 ~/.workbuddy-ai）")
        print(f"\n  当前自动探测：{'国际版' if detected == 'intl' else '国内版'}（{WORKBUDDY_DIR}）")
        print()
        while True:
            try:
                choice = input(f"请选择 WorkBuddy 版本（输入序号，默认 {default_choice}）: ").strip()
                if not choice:
                    break
                if choice == "2":
                    _setup_paths("intl")
                    print("  -> 已选择国际版\n")
                    break
                elif choice == "1":
                    _setup_paths("domestic")
                    print("  -> 已选择国内版\n")
                    break
                else:
                    print("  请输入 1 或 2")
            except (EOFError, KeyboardInterrupt):
                print("\n已取消")
                return

    all_uids = get_all_user_ids()
    session_counts = get_session_counts()
    memory_sizes = get_memory_sizes()
    connector_info = get_connector_info()

    if len(all_uids) <= 1:
        print("⚠️  只发现一个账号，无需迁移。")
        return

    # 显示所有账号
    def format_uid(uid):
        sc = session_counts.get(uid, 0)
        ms = memory_sizes.get(uid, 0)
        ms_str = f"{ms / 1024:.1f}KB" if ms > 0 else "-"
        ci = connector_info.get(uid, {})
        conn_str = f"{ci.get('mcp_servers', 0)}mcp/{ci.get('connector_states', 0)}conn" if ci else "-"
        return sc, ms_str, conn_str

    print("=" * 70)
    print("WorkBuddy 账号迁移向导")
    print("=" * 70)
    client_uid = get_client_login_uid()
    print("\n请选择迁移方向：先选【目标账号】（接收数据），再选【源账号】（被迁移）\n")
    print(f"  {'序号':<4} {'user_id':<40} {'Sessions':>8} {'Memory':>10} {'Connectors':>12}")
    print("  " + "-" * 72)
    for i, uid in enumerate(all_uids, 1):
        sc, ms_str, conn_str = format_uid(uid)
        mark = "  ← 客户端登录态（面板按它过滤）" if uid == client_uid else ""
        print(f"  {i:<4} {uid:<40} {sc:>8} {ms_str:>10} {conn_str:>12}{mark}")

    if client_uid:
        nick = get_client_login_nickname()
        print(f"\n  ℹ️  客户端登录态 = {client_uid}{('  「' + nick + '」') if nick else ''}")
        print("     左侧会话列表按它过滤，**目标账号通常就选带这个标记的那个**；")
        print("     选成别的 uid，迁完重启后面板依旧看不到数据。\n")
    else:
        print("\n  ⚠️  读不到客户端登录态（account-snapshot.json 不存在），请按实际登录状态选择。\n")

    # 选目标账号
    while True:
        try:
            choice = input("请选择【目标账号】（接收数据的账号，输入序号）: ").strip()
            if choice.lower() == "q":
                print("已取消")
                return
            idx = int(choice) - 1
            if 0 <= idx < len(all_uids):
                target_uid = all_uids[idx]
                print(f"  ✅ 目标账号: {target_uid[:20]}... ({session_counts.get(target_uid, 0)} sessions)")
                break
            else:
                print(f"⚠️  无效序号，请输入 1-{len(all_uids)} 之间的数字")
        except ValueError:
            print("⚠️  请输入数字或 q")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return

    # 选源账号（排除目标）
    other_uids = [u for u in all_uids if u != target_uid]
    print()
    print(f"  可迁移到 {target_uid[:20]}... 的源账号：\n")
    print(f"  {'序号':<4} {'user_id':<40} {'Sessions':>8} {'Memory':>10} {'Connectors':>12}")
    print("  " + "-" * 72)
    for i, uid in enumerate(other_uids, 1):
        sc, ms_str, conn_str = format_uid(uid)
        mark = "  ← 客户端登录态" if uid == client_uid else ""
        print(f"  {i:<4} {uid:<40} {sc:>8} {ms_str:>10} {conn_str:>12}{mark}")

    source_uid = _pick_source_uid(other_uids, session_counts)
    if source_uid is None:
        return
    while True:
        print(f"\n  源账号:   {source_uid[:20]}... ({session_counts.get(source_uid, 0)} sessions)")
        print(f"  目标账号: {target_uid[:20]}... ({session_counts.get(target_uid, 0)} sessions)")
        print()
        try:
            migrate(source_uid, target_uid=target_uid, target_is_manual=True,
                    skip_confirm=skip_confirm, reset_mapping=not keep_cloud_mapping)
            return
        except SystemExit as e:
            # migrate() 用 sys.exit 表示中止：`3` = 无数据可迁（或合并后无新增），
            # `1` = 出错。以前 SystemExit 直接穿透向导：用户在向导里挑了个空的源
            # 账号就被踢出进程，而向导本来正好能让他换个源再试一次。
            if e.code != 3 or len(other_uids) < 2:
                raise
            if not _ask_yes_no("  换一个源账号重试？(y/N): "):
                raise
            nxt = _pick_source_uid(other_uids, session_counts)
            if nxt is None:
                return
            source_uid = nxt


def _pick_source_uid(other_uids, session_counts):
    """向导里选源账号；取消（q / Ctrl-C / 无 stdin）返回 None"""
    while True:
        try:
            choice = input("\n请选择【源账号】（要迁移出的账号，输入序号）: ").strip()
            if choice.lower() == "q":
                print("已取消")
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(other_uids):
                return other_uids[idx]
            print(f"⚠️  无效序号，请输入 1-{len(other_uids)} 之间的数字")
        except ValueError:
            print("⚠️  请输入数字或 q")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return None


def get_task_stats():
    """获取 tasks 目录下所有历史任务的统计信息

    解析失败的任务文件会单独计数并打印：以前 except: pass 把坏文件直接吞掉，
    --list-tasks 于是显示「没有发现任何历史任务数据」，用户以为任务真没了。
    """
    stats = {}
    if not TASKS_DIR.exists():
        return stats

    for session_dir in sorted(TASKS_DIR.iterdir()):
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        tasks = []
        broken = 0
        last_err = ""
        for task_file in sorted(session_dir.glob("*.json")):
            try:
                with open(task_file, encoding="utf-8") as f:
                    task_data = json.load(f)
                tasks.append(task_data)
            except Exception as e:
                broken += 1
                last_err = f"{type(e).__name__}: {e}"
        if broken:
            print(f"  ⚠️  tasks/{session_id[:8]}… 有 {broken} 个任务文件解析失败，已跳过（{last_err}）")
        if tasks:
            stats[session_id] = tasks
    return stats


def get_session_title(session_id):
    """根据 session_id 查询 session 标题"""
    if not DB_PATH.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT title FROM sessions WHERE id = ?", (session_id,))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()   # 异常时也要关，否则连接泄漏


def list_tasks():
    """列出所有历史任务概览"""
    stats = get_task_stats()

    if not stats:
        print("❌ 没有发现任何历史任务数据")
        print(f"   检查路径: {TASKS_DIR}")
        return

    print("=" * 70)
    print("WorkBuddy 历史任务概览")
    print("=" * 70)
    print()

    total_tasks = 0
    total_pending = 0
    total_completed = 0

    for session_id, tasks in stats.items():
        title = get_session_title(session_id) or "(未知 session)"
        # 截断过长的标题
        if len(title) > 50:
            title = title[:47] + "..."
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        pending = sum(1 for t in tasks if t.get("status") == "pending")
        other = len(tasks) - completed - pending

        total_tasks += len(tasks)
        total_completed += completed
        total_pending += pending

        print(f"  📋 {session_id[:8]}... | {title}")
        print(f"     {len(tasks)} 个任务: {completed} 完成 / {pending} 待办" + (f" / {other} 其他" if other else ""))

        # 列出待办任务详情
        for t in tasks:
            if t.get("status") == "pending":
                print(f"     🔲 待办: {t.get('subject', '(无标题)')}")

    print()
    print(f"  📊 总计: {len(stats)} 个 session, {total_tasks} 个任务")
    print(f"     {total_completed} 完成 / {total_pending} 待办")
    print()


def _resolve_restore_session():
    """找出「当前对话」的 session id（任务要恢复到它名下），取不到返回 ""

    方法1: 从 sessions.json 获取当前活跃 session（按 mtime 倒序 = 最新在前）
    方法2: 从 workbuddy.db 取最新的 working session
    """
    current_session_id = None
    sessions_dir = WORKBUDDY_DIR / "sessions"
    if sessions_dir.exists():
        fallback_id = None
        for session_file in sorted(sessions_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                with open(session_file, encoding="utf-8") as f:
                    session_data = json.load(f)
                if session_data.get("kind") == "interactive" and session_data.get("sessionId"):
                    sid = session_data["sessionId"]
                    if not sid.startswith("interactive-"):
                        current_session_id = sid
                        break  # 优先使用非 interactive- 的真实 session ID
                    # 只有 interactive- 时保留【最新】那条：以前会一路覆盖到最旧的一条
                    if fallback_id is None:
                        fallback_id = sid
            except Exception:
                pass
        if not current_session_id and fallback_id:
            current_session_id = fallback_id
            print("  ⚠️  只找到 interactive- 开头的 session，恢复目标可能不是你正在用的对话")
            print("     建议用 --session <ID> 显式指定")

    # 方法2: 从 workbuddy.db 获取最新的 working session
    if not current_session_id and DB_PATH.exists():
        conn = None
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT id FROM sessions WHERE status = 'working' ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                current_session_id = row[0]
        except Exception as e:
            # 锁库 / 坏库以前走 silent pass，用户分不清"查不到"和"库打不开"。
            # 打出类型，好让他判断该关客户端还是手动指定 --session。
            print(f"  ⚠️  读取 working session 失败（{type(e).__name__}: {e}）")
            print("     多为数据库被占用或损坏，可用 --session <ID> 直接指定")
        finally:
            # 异常路径也要关：以前 execute() 一抛异常就走 except，conn 再也没人 close，
            # 连接会一直占着库锁，后面的迁移 / 回滚会撞 SQLITE_BUSY
            if conn is not None:
                conn.close()

    return current_session_id or ""


def _restore_fingerprint(source_session, task_id, subject=None):
    """恢复去重的指纹；无法判重时返回 None（表示这条不去重）

    只用 (来源 session, 任务 id) 会在**没有 id 字段**的任务上退化成 (sid, "")：
    同一个会话里的每条任务指纹都相同，第二条起就被误判成"已恢复过"而跳过 ——
    老格式 / 手工改过的任务文件经常没有 id。    缺 id 时改用标题参与判重；
    连标题都没有的话无从判断，返回 None（保持"每次都写"的旧行为）。

    ⚠️ 已知局限（不是 bug，别再"修"）：同一会话里**两条标题相同且都没有 id**
    的任务会互相判重，第二条被跳过。这是标题回退的固有代价 —— 没有 id 时
    没有任何字段能区分它们，而误判成"重复复制一份"比漏掉一条更烦人。
    """
    tid = str(task_id or "")
    if tid:
        return (source_session, "id:" + tid)
    subj = str(subject or "")
    if subj:
        return (source_session, "subject:" + subj)
    return None


def restore_tasks(target_session_id=None, skip_confirm=False):
    """恢复历史任务数据

    将 ~/.workbuddy/tasks/ 下的历史任务文件重新创建到当前 session 中。
    新版 WorkBuddy 的 /todos 面板只显示当前 session 的内存任务，
    此功能通过 TaskCreate 工具将历史任务逐条恢复。

    策略:
    1. 如果指定了 --session，恢复该 session 的【全部状态】任务（含 completed）
    2. 否则恢复所有 session 中的 pending（待办）任务
    3. 注：CLI 没有 --include-completed 参数；--generate-commands 才是只取 pending，
       两者语义不同，不要混用
    """
    stats = get_task_stats()

    if not stats:
        print("❌ 没有发现任何历史任务数据")
        print(f"   检查路径: {TASKS_DIR}")
        return

    # 恢复目标（当前对话）必须先确定：它自己的 tasks/ 目录也在 TASKS_DIR 下面，
    # 会被 get_task_stats() 一起扫出来。不先定就等于"拿自己的输出当输入"，
    # 跑第二遍会把上一遍写好的任务再复制一份，id 一路递增。
    current_session_id = _resolve_restore_session()
    if not current_session_id:
        print("❌ 无法获取当前 session ID")
        print("   请在 WorkBuddy 对话中执行此操作")
        return

    # 确定要恢复哪些 session 的任务
    if target_session_id:
        if target_session_id not in stats:
            print(f"❌ 指定的 session 不存在任务数据: {target_session_id}")
            # 模糊匹配
            matches = [s for s in stats if s.startswith(target_session_id)]
            if matches:
                print(f"   可能的匹配: {matches}")
            return
        if target_session_id == current_session_id:
            print("⚠️  指定的 session 就是当前对话，它的任务本来就在你的待办里，无需恢复")
            return
        target_stats = {target_session_id: stats[target_session_id]}
    else:
        target_stats = stats

    # 收集要恢复的任务
    tasks_to_restore = []
    for session_id, tasks in target_stats.items():
        if session_id == current_session_id:
            continue  # 自己的任务不算「历史任务」
        for task in tasks:
            # 默认只恢复 pending 的任务
            if task.get("status") == "pending":
                tasks_to_restore.append({
                    "source_session": session_id,
                    "task": task,
                })
            elif target_session_id:
                # 指定了 session 时，恢复所有状态的任务
                tasks_to_restore.append({
                    "source_session": session_id,
                    "task": task,
                })

    if not tasks_to_restore:
        print("⚠️  没有找到需要恢复的任务")
        if not target_session_id:
            print("   提示: 默认只恢复 pending（待办）状态的任务")
            print("   如需恢复指定 session 的全部任务，使用 --session <SESSION_ID>")
        return

    # 展示待恢复任务
    print("=" * 70)
    print("WorkBuddy 历史任务恢复")
    print("=" * 70)
    print()

    for i, item in enumerate(tasks_to_restore, 1):
        task = item["task"]
        status_icon = "✅" if task.get("status") == "completed" else "🔲"
        src_session = item["source_session"][:8]
        print(f"  {i}. {status_icon} [{task.get('status', '?')}] {task.get('subject', '(无标题)')}")
        desc = task.get("description", "")
        if desc:
            desc_short = desc[:80] + "..." if len(desc) > 80 else desc
            print(f"     {desc_short}")
        print(f"     来源: session {src_session}... | ID: {task.get('id', '?')}")

    print()
    print(f"  共 {len(tasks_to_restore)} 个任务待恢复")

    if not skip_confirm:
        answer = "y" if _ask_yes_no("\n确认恢复？(y/N): ") else "n"
        if answer != "y":
            print("已取消")
            return

    # 执行恢复：将任务写入当前 session 的 tasks 目录
    print(f"\n  当前 session: {current_session_id}")

    # 将任务写入当前 session 的 tasks 目录（tasks/ 可能还不存在）
    current_tasks_dir = TASKS_DIR / current_session_id
    if not current_tasks_dir.exists():
        current_tasks_dir.mkdir(parents=True, exist_ok=True)

    # 找出当前 session 已有的最大任务 ID，顺带收集「本次已经恢复过」的指纹
    existing_ids = []
    already_restored = set()
    for f in current_tasks_dir.glob("*.json"):
        try:
            existing_ids.append(int(f.stem))
        except ValueError:
            pass
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 半截/损坏的任务文件：既不算 id，也不参与去重判断
            continue
        md = data.get("metadata")
        if not isinstance(md, dict):
            continue
        src_sid = md.get("restored_from_session")
        if src_sid:
            fp = _restore_fingerprint(src_sid, md.get("restored_from_task_id"),
                                      md.get("restored_from_subject"))
            if fp:
                already_restored.add(fp)
    next_id = max(existing_ids, default=0) + 1

    restored_count = 0
    skipped_count = 0
    for item in tasks_to_restore:
        task = item["task"]
        # 去重：restore_tasks 是幂等的才安全——中途失败、或者上一次没迁干净再跑一遍，
        # 以前会按 subject 全新复制一份（id 递增），同一个待办在面板里出现两次。
        # 指纹取「来源 session + 来源任务 id」，meta 里早就在写，这里只是拿来用；
        # 缺 id 的任务退化为按标题判重，两者都没有则不去重。
        fingerprint = _restore_fingerprint(item["source_session"],
                                           task.get("id"), task.get("subject"))
        if fingerprint is not None:
            if fingerprint in already_restored:
                skipped_count += 1
                print(f"  ⏭️  已恢复过，跳过: {task.get('subject', '(无标题)')}")
                continue
            already_restored.add(fingerprint)
        # 创建新的任务 JSON，更新 ID
        new_task = {
            "subject": task.get("subject", ""),
            "description": task.get("description", ""),
            "activeForm": task.get("activeForm", task.get("subject", "")),
            "status": task.get("status", "pending"),
            "id": str(next_id),
            "createdAt": task.get("createdAt", int(datetime.now().timestamp() * 1000)),
            "updatedAt": int(datetime.now().timestamp() * 1000),
            "metadata": {
                "restored_from_session": item["source_session"],
                "restored_from_task_id": task.get("id", ""),
                # 没有 id 的任务靠标题判重，所以标题也要一并记下来，
                # 否则下一次运行时读回的指纹又是空的
                "restored_from_subject": task.get("subject", ""),
                "restored_at": datetime.now().isoformat(),
            }
        }

        task_file = current_tasks_dir / f"{next_id}.json"
        # 原子写：半截 JSON 下次会被 get_task_stats 静默跳过 = 这条任务直接丢失
        _atomic_write_text(
            task_file, json.dumps(new_task, indent=2, ensure_ascii=False)
        )

        next_id += 1
        restored_count += 1
        status_icon = "✅" if new_task["status"] == "completed" else "🔲"
        print(f"  {status_icon} 恢复: {new_task['subject']} → {task_file.name}")

    print()
    print("=" * 70)
    if restored_count == 0 and skipped_count:
        print(f"本次没有新任务：{skipped_count} 个都已在当前对话里，未重复写入")
    else:
        print(f"任务恢复完成！新恢复 {restored_count} 个"
              + (f"，跳过已存在的 {skipped_count} 个" if skipped_count else ""))
    print("=" * 70)
    print(f"\n  📊 已恢复 {restored_count} 个任务到 session {current_session_id[:12]}...")
    print(f"  📁 任务文件: {current_tasks_dir}")
    print()
    print("  ⚠️  注意：")
    print("     - 新版 WorkBuddy 的 /todos 面板可能仍不会显示这些任务")
    print("     - 这是因为 /todos 读取的是当前 session 的内存数据，而非文件系统")
    print("     - 恢复后的任务文件已写入磁盘，重启编辑器后可能生效")
    print("     - 如需在当前对话中使用这些任务，请告知 AI 读取这些 JSON 文件")
    print()
    print("  💡 替代方案：")
    print("     在当前对话中让 AI 使用 TaskCreate 工具重新创建这些任务")
    print("     这样 /todos 面板就能立即显示")


def generate_task_create_commands(target_session_id=None):
    """生成 TaskCreate 工具的 JSON 命令，供 AI 在当前对话中执行

    这是恢复任务最可靠的方式：直接让 AI 在当前 session 中用 TaskCreate 创建任务，
    这样 /todos 面板能立即显示。
    """
    stats = get_task_stats()

    if not stats:
        print("❌ 没有发现任何历史任务数据")
        return

    # 确定要恢复哪些任务
    if target_session_id:
        if target_session_id not in stats:
            print(f"❌ 指定的 session 不存在任务数据: {target_session_id}")
            return
        target_stats = {target_session_id: stats[target_session_id]}
    else:
        target_stats = stats

    # 收集 pending 任务
    tasks_to_restore = []
    for session_id, tasks in target_stats.items():
        for task in tasks:
            if task.get("status") == "pending":
                tasks_to_restore.append(task)

    if not tasks_to_restore:
        print("⚠️  没有找到 pending 状态的任务")
        print("   如需恢复指定 session 的全部任务，使用 --session <SESSION_ID>")
        return

    print("=" * 70)
    print("TaskCreate 命令生成（供 AI 在当前对话中执行）")
    print("=" * 70)
    print()
    print(f"共 {len(tasks_to_restore)} 个待恢复的 pending 任务：\n")

    for i, task in enumerate(tasks_to_restore, 1):
        print(f"--- 任务 {i} ---")
        cmd = {
            "subject": task.get("subject", ""),
            "description": task.get("description", ""),
            "activeForm": task.get("activeForm", task.get("subject", "")),
        }
        print(json.dumps(cmd, ensure_ascii=False, indent=2))
        print()

    print("💡 将以上 JSON 逐个传给 TaskCreate 工具即可在当前 session 中创建任务")


def _run_migrate(args):
    """跑迁移（命令行指定源 / 交互式向导两条入口共用）

    异常不在这一层收：迁移内部遇到「WAL 被占用」「备份失败」这类可预见的中止
    会先打印人话提示再 raise，由 `main()` 统一收口成一行 ❌ + 退出码。
    （以前这一层自己收、其它分支裸着，rollback / restore_tasks 崩了照样是
    提示 + 一整段 traceback。）
    """
    if args.source:
        migrate(args.source, target_uid=args.target, skip_confirm=args.yes,
                target_is_manual=args.target is not None,
                reset_mapping=not args.keep_cloud_mapping)
    else:
        # --dir 已显式指定数据目录，再问版本会把 --dir 悄悄覆盖掉
        interactive_migrate(
            skip_edition_prompt=args.intl or bool(args.dir),
            skip_confirm=args.yes,
            keep_cloud_mapping=args.keep_cloud_mapping,
        )


def main():
    """入口；业务性异常在这里收口，别让用户看 traceback

    以前只有 `_run_migrate()` 包了 RuntimeError（迁移路径），而 rollback /
    restore_tasks 这些同样会 raise RuntimeError 的分支是裸的 —— 提示打完之后
    紧接着甩一整段 traceback，提示反而被淹没。与 migrate_session.py 的
    「三收口」对齐。
    """
    # 必须最先执行：剥离 WorkBuddy 沙箱 shim（PYTHONPATH 劫持 mkdir 等 API）后重跑
    _strip_sandbox_shim()
    try:
        _main()
    except RuntimeError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"\n❌ 数据库写入失败：{e}")
        print("   处理：用 --rollback <备份标签> 回滚，或先用 --diagnose 确认现状。")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)


def _main():
    parser = argparse.ArgumentParser(description="WorkBuddy 账号迁移工具")
    parser.add_argument("--dir", type=str, help="WorkBuddy 数据目录（默认自动探测：~/.workbuddy-ai 或 ~/.workbuddy）")
    parser.add_argument("--intl", action="store_true", help="强制使用国际版数据目录 ~/.workbuddy-ai（默认自动探测，--dir 优先级更高）")
    parser.add_argument("--diagnose", "-d", action="store_true", help="诊断模式：查看所有账号数据分布")
    parser.add_argument("--source", "-s", type=str, help="源账号 user_id（要迁移出的账号）")
    parser.add_argument("--target", "-t", type=str, help="目标账号 user_id（接收数据的账号，默认从登录态 storage.json / account-snapshot.json 自动推断）")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认直接迁移/回滚")
    parser.add_argument("--force", action="store_true", help="跳过「客户端必须关闭」检测（确有风险，不建议）")
    parser.add_argument(
        "--assume-clients-closed", action="store_true",
        help="进程检测【失败】时按「客户端已全部退出」谨慎继续；"
             "若确实检测到客户端仍在运行，仍会拦截（比 --force 温和）")

    parser.add_argument("--keep-cloud-mapping", action="store_true",
                        help="迁移后不重置 edge-sync 云端通道映射（默认重置：让对话按新账号的通道重新上传）")
    parser.add_argument("--restart", action="store_true", help="完成后自动重启 WorkBuddy 客户端（macOS），让会话列表立即刷新，无需手动重启")
    parser.add_argument("--rollback", "-r", type=str, help="回滚到指定备份标签")
    parser.add_argument("--restore-tasks", action="store_true", help="恢复历史任务到当前 session")
    parser.add_argument("--list-tasks", action="store_true", help="列出所有历史任务概览")
    parser.add_argument(
        "--session", type=str,
        help="指定要恢复任务的 session ID（恢复该 session 的全部状态；"
             "与之配合的 --generate-commands 只生成 pending 任务，两者语义不同）"
    )
    parser.add_argument("--generate-commands", action="store_true", help="生成 TaskCreate 命令（与 --restore-tasks 配合使用）")

    args = parser.parse_args()

    # 目录优先级：--dir > --intl > 自动探测（模块导入时已按 _find_workbuddy_dir() 算好）
    if args.dir:
        _set_workbuddy_dir(args.dir)
    elif args.intl:
        _setup_paths("intl")

    # 互斥参数检查：--rollback 与其它动作参数同给时，以前会被静默丢掉
    if args.rollback and (args.source or args.target or args.restore_tasks
                          or args.list_tasks or args.diagnose):
        print("❌ --rollback 不能与 --source / --target / --restore-tasks / --list-tasks 同时使用")
        print("   请单独执行，例如：python3 migrate.py --rollback <TAG> --yes")
        sys.exit(1)

    # --diagnose 是只读诊断，和其它动作参数同给时那些参数会被静默丢掉
    # （以前只有 --rollback 有这条互斥检查，--diagnose --source X 直接不吭声）
    if args.diagnose and (args.source or args.target or args.restore_tasks
                          or args.list_tasks):
        print("❌ --diagnose 是只读诊断，不能与 --source / --target / "
              "--restore-tasks / --list-tasks 同时使用")
        print("   请单独执行：python3 migrate.py --diagnose")
        sys.exit(1)
    # --list-tasks 是只读列出，同样会把动作参数吃掉（dispatch 是 elif 链，
    # 它排在 --source 前面）：`--list-tasks --source X` 以前一声不吭地只列了任务
    if args.list_tasks and (args.source or args.target or args.restore_tasks):
        print("❌ --list-tasks 是只读列出，不能与 --source / --target / "
              "--restore-tasks 同时使用")
        print("   请单独执行：python3 migrate.py --list-tasks")
        sys.exit(1)

    # 依赖参数检查：以前这些组合会被静默忽略（落到交互式或无操作）
    if args.target and not args.source:
        print("❌ --target 必须与 --source 一起使用（单独给 --target 不会产生任何迁移）")
        sys.exit(1)
    if args.generate_commands and not args.restore_tasks:
        print("❌ --generate-commands 必须与 --restore-tasks 一起使用")
        sys.exit(1)
    # --session 只有任务恢复这条路径会读：不带 --restore-tasks 时它会被静默丢弃，
    # 用户以为自己指定了 session，实际跑的是默认语义（恢复所有 session 的待办）
    if args.session and not args.restore_tasks:
        print("❌ --session 必须与 --restore-tasks 一起使用")
        print("   （它只用于指定「恢复哪个 session 的任务」，其它路径不看这个参数）")
        print("     python3 migrate.py --restore-tasks --session <SESSION_ID>")
        sys.exit(1)
    if args.target and not _looks_like_uid(args.target):
        print(f"⚠️  --target 不像一个 WorkBuddy user_id（应为 UUID 形态）：{args.target}")
        print("   拼错会把全部 session 改到不存在的账号下，请确认；可用 --rollback 回滚。")
        # 以前只警告一句就继续执行（--yes 时更是一路跑到底）。非交互模式下
        # 至少要求一次显式确认，避免手滑把整个账号迁到不存在的 uid 下。
        if not args.yes and not _ask_yes_no("   确认继续？(y/N): "):
            print("已取消")
            sys.exit(1)

    # 客户端检测：migrate.py 同样会在客户端持锁 / WAL 未落盘时改库和覆盖文件，
    # 与 migrate_session.py 保持一致（--diagnose 等只读操作不需要）。
    if args.rollback or args.source:
        if not require_clients_closed(force=args.force,
                                      assume_closed=args.assume_clients_closed):
            sys.exit(2)

    if args.diagnose:
        diagnose()
    elif args.rollback:
        rollback(args.rollback, skip_confirm=args.yes)
        if args.restart:
            restart_client()
    elif args.list_tasks:
        list_tasks()
    elif args.restore_tasks:
        if args.generate_commands:
            generate_task_create_commands(target_session_id=args.session)
        else:
            restore_tasks(target_session_id=args.session, skip_confirm=args.yes)
    elif args.source:
        _run_migrate(args)
        if args.restart:
            restart_client()
    else:
        # 无参数时进入交互式向导（向导最终也会改库，同样要求客户端已关闭）
        if not require_clients_closed(force=args.force,
                                      assume_closed=args.assume_clients_closed):
            sys.exit(2)
        # --dir 已显式指定数据目录，再问版本会把 --dir 悄悄覆盖掉
        interactive_migrate(skip_edition_prompt=args.intl or bool(args.dir),
                            skip_confirm=args.yes,
                            keep_cloud_mapping=args.keep_cloud_mapping)


if __name__ == "__main__":
    main()
