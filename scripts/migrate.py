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
import json
import os
import platform
import re
import sys

# Windows 终端可能使用 GBK/CP936 编码，强制 stdout/stderr 为 UTF-8 避免 emoji 崩溃
if platform.system() == "Windows":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

def _home_override() -> str:
    """WORKBUDDY_MIGRATE_HOME 的值（未设置时返回空串）"""
    return os.environ.get("WORKBUDDY_MIGRATE_HOME", "")


def _home() -> Path:
    """home 目录，支持环境变量覆盖（测试时指向临时 fixture，不碰真实数据）"""
    env = _home_override()
    return Path(env) if env else Path.home()


# user_id 形如 eadd8ba3-4fd7-46e0-9027-854ef5696bd3（8-4-4-4-12 十六进制）。
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
    import platform
    system = platform.system()
    home = _home()
    candidates = []
    if system == "Darwin":
        candidates.append(home / "Library" / "Application Support" / "WorkBuddy" / "User" / "globalStorage" / "storage.json")
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        # home 被覆盖时，APPDATA 指向的是真实机器，必须改从 home 推导
        base = Path(appdata) if (appdata and not _home_override()) else home / "AppData" / "Roaming"
        candidates.append(base / "WorkBuddy" / "User" / "globalStorage" / "storage.json")
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))
        candidates.append(Path(config_home) / "WorkBuddy" / "User" / "globalStorage" / "storage.json")
    for p in candidates:
        if p.exists():
            return p
    return None


def _set_workbuddy_dir(path):
    """设置数据目录并重算所有派生路径（供 --dir 覆盖时调用）"""
    global WORKBUDDY_DIR, DB_PATH, MEMORY_DIR, CONNECTORS_DIR, TASKS_DIR
    global STORAGE_JSON, ACCOUNT_SNAPSHOT, BACKUP_DIR
    WORKBUDDY_DIR = Path(path).expanduser()
    DB_PATH = WORKBUDDY_DIR / "workbuddy.db"
    MEMORY_DIR = WORKBUDDY_DIR / "memory"
    CONNECTORS_DIR = WORKBUDDY_DIR / "connectors"
    TASKS_DIR = WORKBUDDY_DIR / "tasks"
    STORAGE_JSON = _get_storage_json_path()
    ACCOUNT_SNAPSHOT = WORKBUDDY_DIR / "storage" / "skeleton" / "account-snapshot.json"
    BACKUP_DIR = WORKBUDDY_DIR / "migrate_backups"


WORKBUDDY_DIR = _find_workbuddy_dir()
DB_PATH = WORKBUDDY_DIR / "workbuddy.db"
MEMORY_DIR = WORKBUDDY_DIR / "memory"
CONNECTORS_DIR = WORKBUDDY_DIR / "connectors"
TASKS_DIR = WORKBUDDY_DIR / "tasks"
STORAGE_JSON = _get_storage_json_path()

# 国际版登录态权威来源：account-snapshot.json 的 primary.uid
ACCOUNT_SNAPSHOT = WORKBUDDY_DIR / "storage" / "skeleton" / "account-snapshot.json"

# 备份目录
BACKUP_DIR = WORKBUDDY_DIR / "migrate_backups"


def get_current_user_id():
    """获取当前登录的 user_id

    优先级策略：
    1. 从 storage.json 的 genie.userId 读取（国内版，登录态权威来源）
    2. 国际版从 account-snapshot.json 的 primary.uid 读取（登录态权威来源）
    3. 从 workbuddy.db 中 session 数量最多的 user_id 推断（辅助验证）
    4. 如果上述来源不一致，优先使用登录态权威来源，并发出警告

    ⚠️  注意：不能用"最新 session"来判断当前账号！
    因为旧账号在切换前的最后一条 session 可能比当前账号的 session 更新，
    导致误把旧账号当成当前账号。
    """
    db_uid = ""
    login_uid = ""

    # 方法1a：从 storage.json 读取（国内版，登录态权威来源）
    # 注意：_get_storage_json_path() 可能返回 None（平台路径不存在）
    if STORAGE_JSON is not None:
        try:
            with open(STORAGE_JSON, encoding="utf-8") as f:
                data = json.load(f)
            login_uid = data.get("genie.userId", "")
        except Exception:
            pass

    # 方法1b：国际版 account-snapshot.json（登录态权威来源）
    if not login_uid and ACCOUNT_SNAPSHOT.exists():
        try:
            with open(ACCOUNT_SNAPSHOT, encoding="utf-8") as f:
                data = json.load(f)
            primary = data.get("primary", {})
            login_uid = primary.get("uid", "")
        except Exception:
            pass

    # 方法2：从 DB 推断——用 session 数量最多的 user_id（而非最新 session）
    # 避免被偶发的旧账号 session 欺骗
    if DB_PATH.exists():
        conn = None
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute(
                "SELECT user_id, COUNT(*) as cnt FROM sessions "
                "WHERE user_id IS NOT NULL GROUP BY user_id ORDER BY cnt DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row and row[0]:
                db_uid = row[0]
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()

    # 交叉验证
    if login_uid and db_uid and login_uid != db_uid:
        print(f"⚠️  检测到 user_id 不一致！")
        print(f"   登录态 (storage.json / account-snapshot.json): {login_uid}")
        print(f"   DB session 数最多的 user_id: {db_uid}")
        print(f"   → 优先使用登录态 user_id（登录态权威）: {login_uid}")
        print()

    if login_uid:
        return login_uid

    if db_uid:
        return db_uid

    print("❌ 无法获取当前 user_id（storage.json / account-snapshot.json 和 DB 均无数据）")
    return ""


def get_all_user_ids():
    """扫描所有已知的 user_id"""
    user_ids = set()

    # 从 DB
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        try:
            cur.execute("SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL")
            for row in cur.fetchall():
                user_ids.add(row[0])
        except sqlite3.OperationalError:
            pass
        conn.close()

    # 从 memory 文件
    if MEMORY_DIR.exists():
        for f in MEMORY_DIR.glob("*_memory.md"):
            uid = f.stem.replace("_memory", "")
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
        cur = conn.cursor()
        try:
            cur.execute("SELECT user_id, COUNT(*) FROM sessions WHERE user_id IS NOT NULL GROUP BY user_id")
            for row in cur.fetchall():
                counts[row[0]] = row[1]
        except sqlite3.OperationalError:
            pass
        conn.close()
    return counts


def get_memory_sizes():
    """获取各 user_id 的 memory 文件大小"""
    sizes = {}
    if MEMORY_DIR.exists():
        for f in MEMORY_DIR.glob("*_memory.md"):
            uid = f.stem.replace("_memory", "")
            sizes[uid] = f.stat().st_size
    return sizes


def get_connector_info():
    """获取各 user_id 的 connector 配置信息"""
    info = {}
    if CONNECTORS_DIR.exists():
        for d in CONNECTORS_DIR.iterdir():
            if d.is_dir() and d.name not in ("default", "skills") and _looks_like_uid(d.name):
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
    current_uid = get_current_user_id()
    all_uids = get_all_user_ids()
    session_counts = get_session_counts()
    memory_sizes = get_memory_sizes()
    connector_info = get_connector_info()

    print("=" * 70)
    print("WorkBuddy 账号数据诊断")
    print("=" * 70)
    print(f"\n当前登录: {current_uid}\n")

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

    # 建议迁移方向
    other_uids = [u for u in all_uids if u != current_uid]
    if other_uids:
        print("💡 迁移建议:")
        for uid in other_uids:
            sc = session_counts.get(uid, 0)
            ms = memory_sizes.get(uid, 0)
            print(f"   {uid[:20]}... → 当前账号 ({sc} sessions, {ms / 1024:.1f}KB memory)")
        print(f"\n   执行命令: python3 migrate.py --source {other_uids[0]}")


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
        return True
    except Exception as e:
        print(f"  ⚠️  数据库在线备份失败（{e}）")
        return False
    finally:
        if src_conn is not None:
            src_conn.close()
        if dst_conn is not None:
            dst_conn.close()


def create_backup(target_uid, timestamp):
    """创建备份"""
    BACKUP_DIR.mkdir(exist_ok=True)
    backup_tag = f"{timestamp}_{target_uid[:8]}"
    backup_path = BACKUP_DIR / backup_tag
    backup_path.mkdir(exist_ok=True)

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
        shutil.copy2(str(mem_file), str(backup_path / f"{target_uid}_memory.md"))
        print(f"  ✅ 已备份 Memory → {backup_path / f'{target_uid}_memory.md'}")

    # 备份 Connectors
    conn_dir = CONNECTORS_DIR / target_uid
    if conn_dir.exists():
        dst_dir = backup_path / target_uid
        if dst_dir.exists():
            shutil.rmtree(str(dst_dir))
        shutil.copytree(str(conn_dir), str(dst_dir))
        print(f"  ✅ 已备份 Connectors → {backup_path / target_uid}/")

    # 写入备份元数据
    meta = {
        "timestamp": timestamp,
        "target_uid": target_uid,
        "created_at": datetime.now().isoformat(),
    }
    with open(backup_path / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

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
    if busy:
        print(f"  ⚠️  WAL checkpoint 未完成（{label}busy={busy}）："
              f"有其他进程占用数据库锁，请关闭 WorkBuddy 客户端后重试")
        return False
    print(f"  📋 WAL checkpoint 完成（{label}log={row[1]}, checkpointed={row[2]}）")
    return True


def migrate_sessions(source_uid, target_uid):
    """迁移 Session 历史"""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # 先 checkpoint WAL（确保读取到最新数据）
    checkpoint_ok = _wal_checkpoint(cur, "迁移前 ")

    # 统计
    cur.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (source_uid,))
    count = cur.fetchone()[0]

    if count == 0:
        print(f"  ⏭️  源账号无 session，跳过")
        conn.close()
        return 0

    # 执行迁移
    cur.execute("UPDATE sessions SET user_id = ? WHERE user_id = ?", (target_uid, source_uid))
    migrated = cur.rowcount
    conn.commit()

    # 迁移后再 checkpoint WAL（确保写入持久化）
    checkpoint_ok = _wal_checkpoint(cur, "迁移后 ") and checkpoint_ok

    # 验证：确认源账号不再有 session
    cur.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (source_uid,))
    remaining = cur.fetchone()[0]
    if remaining > 0:
        print(f"  ⚠️  警告：源账号仍有 {remaining} 个 session 未迁移！")
    elif not checkpoint_ok:
        # checkpoint 没做完的话，这次查询的结果本身也不可信，不能报"验证通过"
        print(f"  ⚠️  校验未完成：WAL checkpoint 被锁占用，无法确认迁移结果是否已落盘")
    else:
        print(f"  ✅ 验证通过：源账号 session 已全部迁移")

    conn.close()

    print(f"  ✅ 迁移 {migrated} 个 session（{source_uid[:12]}... → {target_uid[:12]}...）")
    return migrated


RAW_JSON_RE = re.compile(r"<!--\s*RAW_JSON_START(.*?)RAW_JSON_END\s*-->", re.DOTALL)


def _extract_memory_block(content):
    """从 memory 文件中提取【首个】memoryBlock 内容，解析失败返回 None"""
    m = RAW_JSON_RE.search(content)
    if not m:
        return None
    try:
        data = json.loads(m.group(1).strip())
        return data.get("memoryBlock", "")
    except Exception:
        return None


def _extract_memory_blocks(content):
    """提取 memory 文件中【所有】RAW_JSON 块里的 memoryBlock 文本

    只比较首个块是不够的：迁移过一次后目标文件里会有两个块，再跑一次迁移时
    首块是目标自己的内容，与源块不同 → 同一个块被追加两遍。
    """
    blocks = []
    for m in RAW_JSON_RE.finditer(content or ""):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
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

        with open(dst_file, "a", encoding="utf-8") as f:
            f.write(f"\n\n---\n## 迁移自 {source_uid[:12]}...\n\n" + "\n\n".join(chunks) + "\n")
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

    # 追加
    with open(dst_file, "a", encoding="utf-8") as f:
        f.write(f"\n\n---\n## 迁移自 {source_uid[:12]}...\n\n")
        f.write("\n".join(new_lines))
        f.write("\n")

    print(f"  ✅ 追加 {len(new_lines)} 行新内容到 Memory")


def deep_merge_dict(source, target):
    """深度合并字典，target 中已有的 key 保留不动"""
    for k, v in source.items():
        if k not in target:
            target[k] = v
        elif isinstance(v, dict) and isinstance(target[k], dict):
            deep_merge_dict(v, target[k])
    return target


def migrate_connectors(source_uid, target_uid):
    """迁移 Connector 配置（深度合并）"""
    src_dir = CONNECTORS_DIR / source_uid
    dst_dir = CONNECTORS_DIR / target_uid

    if not src_dir.exists():
        print(f"  ⏭️  源账号无 Connector 目录，跳过")
        return

    # 确保目标目录存在
    dst_dir.mkdir(exist_ok=True)

    for fname in ["mcp.json", "connector-states.json"]:
        src_file = src_dir / fname
        dst_file = dst_dir / fname

        if not src_file.exists():
            continue

        with open(src_file, encoding="utf-8") as f:
            src_data = json.load(f)

        if dst_file.exists():
            with open(dst_file, encoding="utf-8") as f:
                dst_data = json.load(f)

            if isinstance(src_data, dict) and isinstance(dst_data, dict):
                # 深度合并
                added_keys = [k for k in src_data if k not in dst_data]
                if added_keys:
                    deep_merge_dict(src_data, dst_data)
                    with open(dst_file, "w", encoding="utf-8") as f:
                        json.dump(dst_data, f, indent=2, ensure_ascii=False)
                    print(f"  ✅ 合并 {fname}（新增 {len(added_keys)} 个 key: {added_keys[:5]}...）")
                else:
                    print(f"  ⏭️  {fname} 无新增内容，跳过")
            else:
                print(f"  ⚠️  {fname} 类型冲突，跳过（源={type(src_data).__name__}, 目标={type(dst_data).__name__}）")
        else:
            with open(dst_file, "w", encoding="utf-8") as f:
                json.dump(src_data, f, indent=2, ensure_ascii=False)
            print(f"  ✅ 复制 {fname}（目标不存在）")


def migrate(source_uid, target_uid=None, skip_confirm=False, target_is_manual=False):
    """执行完整迁移流程

    target_uid: 目标账号 ID。如果为 None，则自动从登录态（storage.json / account-snapshot.json）推断。
    target_is_manual: 目标账号是否由用户手动指定（--target 或交互向导），用于打印区分。
    """
    if target_uid is None:
        target_uid = get_current_user_id()
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
    target_label = "手动指定" if target_is_manual else "当前登录"
    print(f"  目标账号: {target_uid} ({target_label})")
    print()

    # Phase 1: 诊断
    print("📊 Phase 1: 诊断数据分布...")
    session_counts = get_session_counts()
    memory_sizes = get_memory_sizes()
    src_sessions = session_counts.get(source_uid, 0)
    src_memory = memory_sizes.get(source_uid, 0)
    print(f"  源账号: {src_sessions} sessions, {src_memory / 1024:.1f}KB memory")
    print(f"  目标账号: {session_counts.get(target_uid, 0)} sessions, {memory_sizes.get(target_uid, 0) / 1024:.1f}KB memory")

    if src_sessions == 0 and src_memory == 0:
        print("\n⚠️  源账号无任何数据，无需迁移")
        return

    # 确认
    if not skip_confirm:
        print()
        answer = input("确认执行迁移？(y/N): ").strip().lower()
        if answer != "y":
            print("已取消")
            return

    # Phase 2: 备份
    print("\n📦 Phase 2: 创建备份...")
    backup_tag = create_backup(target_uid, timestamp)

    # Phase 3: 迁移
    print("\n🔄 Phase 3: 执行迁移...")

    print("\n  [Session 迁移]")
    migrated_sessions = migrate_sessions(source_uid, target_uid)

    print("\n  [Memory 迁移]")
    migrate_memory(source_uid, target_uid)

    print("\n  [Connector 迁移]")
    migrate_connectors(source_uid, target_uid)

    # Phase 4: 验证
    print("\n✅ Phase 4: 验证...")
    new_session_counts = get_session_counts()
    new_target_sessions = new_session_counts.get(target_uid, 0)
    print(f"  当前账号 session 数: {session_counts.get(target_uid, 0)} → {new_target_sessions}")

    # Phase 5: 收尾
    print("\n" + "=" * 70)
    print("迁移完成！")
    print("=" * 70)
    print(f"\n  📦 备份标签: {backup_tag}")
    print(f"  🔄 已迁移: {migrated_sessions} sessions + memory + connectors")
    print(f"\n  ⚠️  请重启 WorkBuddy 客户端让变更生效！")
    print(f"  📁 备份位置: {BACKUP_DIR / backup_tag}")
    print(f"  🔙 回滚命令: python3 migrate.py --rollback {backup_tag}")


def rollback(backup_tag, skip_confirm=False):
    """回滚到指定备份"""
    backup_path = BACKUP_DIR / backup_tag
    if not backup_path.exists():
        print(f"❌ 备份不存在: {backup_tag}")
        sys.exit(1)

    # 读取元数据
    meta_file = backup_path / "meta.json"
    if meta_file.exists():
        with open(meta_file, encoding="utf-8") as f:
            meta = json.load(f)
        target_uid = meta.get("target_uid", "")
    else:
        # 从文件名推算
        target_uid = ""

    print("=" * 70)
    print("WorkBuddy 账号迁移回滚")
    print("=" * 70)
    print(f"\n  备份标签: {backup_tag}")
    print(f"  目标账号: {target_uid}")
    print()

    if skip_confirm:
        answer = "y"
    else:
        answer = input("确认回滚？这将覆盖当前数据！(y/N): ").strip().lower()
    if answer != "y":
        print("已取消")
        return

    # 恢复数据库
    db_backup = backup_path / "workbuddy.db"
    if db_backup.exists():
        shutil.copy2(str(db_backup), str(DB_PATH))
        print("  ✅ 已恢复数据库")

    if not target_uid:
        # target_uid 为空时 CONNECTORS_DIR / target_uid 会退化成整个 connectors 目录，
        # 此时绝不能做任何删除/覆盖操作，否则会误删全部连接器配置。
        print("  ⚠️  备份缺少目标账号信息（meta.json 不完整），跳过 Memory / Connectors 恢复")
        print("     （target_uid 为空时路径会退化成整个目录，不能做任何删除操作）")
    else:
        # 恢复 Memory：只要备份里有就恢复，不要求目标文件当前必须存在
        mem_backup = backup_path / f"{target_uid}_memory.md"
        if mem_backup.exists():
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(mem_backup), str(MEMORY_DIR / f"{target_uid}_memory.md"))
            print("  ✅ 已恢复 Memory")

        # 恢复 Connectors：同样只看备份里有没有。
        # 目标目录当前不存在是常见情况（比如迁移后手动清理过），此时应当重建而不是跳过。
        conn_backup = backup_path / target_uid
        conn_target = CONNECTORS_DIR / target_uid
        if conn_backup.exists():
            if conn_target.exists():
                shutil.rmtree(str(conn_target))
            shutil.copytree(str(conn_backup), str(conn_target))
            print("  ✅ 已恢复 Connectors")
        else:
            print("  ⏭️  备份中没有 Connector 数据，跳过")

    print("\n  ⚠️  请重启 WorkBuddy 客户端让变更生效！")


def interactive_migrate(skip_edition_prompt=False):
    """交互式迁移向导：列出所有账号，用户分别选择源和目标"""

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
    print("\n请选择迁移方向：先选【目标账号】（接收数据），再选【源账号】（被迁移）\n")
    print(f"  {'序号':<4} {'user_id':<40} {'Sessions':>8} {'Memory':>10} {'Connectors':>12}")
    print("  " + "-" * 72)
    for i, uid in enumerate(all_uids, 1):
        sc, ms_str, conn_str = format_uid(uid)
        print(f"  {i:<4} {uid:<40} {sc:>8} {ms_str:>10} {conn_str:>12}")

    print("\n  ⚠️  如果下方「当前登录」显示有误，请忽略，直接按实际登录状态选择。\n")

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
        print(f"  {i:<4} {uid:<40} {sc:>8} {ms_str:>10} {conn_str:>12}")

    while True:
        try:
            choice = input("\n请选择【源账号】（要迁移出的账号，输入序号）: ").strip()
            if choice.lower() == "q":
                print("已取消")
                return
            idx = int(choice) - 1
            if 0 <= idx < len(other_uids):
                source_uid = other_uids[idx]
                break
            else:
                print(f"⚠️  无效序号，请输入 1-{len(other_uids)} 之间的数字")
        except ValueError:
            print("⚠️  请输入数字或 q")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return

    print(f"\n  源账号:   {source_uid[:20]}... ({session_counts.get(source_uid, 0)} sessions)")
    print(f"  目标账号: {target_uid[:20]}... ({session_counts.get(target_uid, 0)} sessions)")
    print()
    migrate(source_uid, target_uid=target_uid, target_is_manual=True)


def get_task_stats():
    """获取 tasks 目录下所有历史任务的统计信息"""
    stats = {}
    if not TASKS_DIR.exists():
        return stats

    for session_dir in sorted(TASKS_DIR.iterdir()):
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        tasks = []
        for task_file in sorted(session_dir.glob("*.json")):
            try:
                with open(task_file, encoding="utf-8") as f:
                    task_data = json.load(f)
                tasks.append(task_data)
            except Exception:
                pass
        if tasks:
            stats[session_id] = tasks
    return stats


def get_session_title(session_id):
    """根据 session_id 查询 session 标题"""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT title FROM sessions WHERE id = ?", (session_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


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


def restore_tasks(target_session_id=None, skip_confirm=False):
    """恢复历史任务数据

    将 ~/.workbuddy/tasks/ 下的历史任务文件重新创建到当前 session 中。
    新版 WorkBuddy 的 /todos 面板只显示当前 session 的内存任务，
    此功能通过 TaskCreate 工具将历史任务逐条恢复。

    策略:
    1. 如果指定了 --session，只恢复该 session 的任务
    2. 否则恢复所有 session 中的 pending（待办）任务
    3. 已 completed 的任务默认不恢复，除非加 --include-completed
    """
    stats = get_task_stats()

    if not stats:
        print("❌ 没有发现任何历史任务数据")
        print(f"   检查路径: {TASKS_DIR}")
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
        target_stats = {target_session_id: stats[target_session_id]}
    else:
        target_stats = stats

    # 收集要恢复的任务
    tasks_to_restore = []
    for session_id, tasks in target_stats.items():
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
        answer = input("\n确认恢复？(y/N): ").strip().lower()
        if answer != "y":
            print("已取消")
            return

    # 执行恢复：将任务写入当前 session 的 tasks 目录
    # 首先获取当前 session ID
    current_session_id = None

    # 方法1: 从 sessions.json 获取当前活跃 session
    sessions_dir = WORKBUDDY_DIR / "sessions"
    if sessions_dir.exists():
        for session_file in sorted(sessions_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                with open(session_file, encoding="utf-8") as f:
                    session_data = json.load(f)
                if session_data.get("kind") == "interactive" and session_data.get("sessionId"):
                    current_session_id = session_data["sessionId"]
                    if not current_session_id.startswith("interactive-"):
                        break  # 优先使用非 interactive- 的真实 session ID
            except Exception:
                pass

    # 方法2: 从 workbuddy.db 获取最新的 working session
    if not current_session_id and DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT id FROM sessions WHERE status = 'working' ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            conn.close()
            if row:
                current_session_id = row[0]
        except Exception:
            pass

    if not current_session_id:
        print("❌ 无法获取当前 session ID")
        print("   请在 WorkBuddy 对话中执行此操作")
        return

    print(f"\n  当前 session: {current_session_id}")

    # 将任务写入当前 session 的 tasks 目录
    current_tasks_dir = TASKS_DIR / current_session_id
    current_tasks_dir.mkdir(exist_ok=True)

    # 找出当前 session 已有的最大任务 ID
    existing_ids = []
    for f in current_tasks_dir.glob("*.json"):
        try:
            existing_ids.append(int(f.stem))
        except ValueError:
            pass
    next_id = max(existing_ids, default=0) + 1

    restored_count = 0
    for item in tasks_to_restore:
        task = item["task"]
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
                "restored_at": datetime.now().isoformat(),
            }
        }

        task_file = current_tasks_dir / f"{next_id}.json"
        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(new_task, f, indent=2, ensure_ascii=False)

        next_id += 1
        restored_count += 1
        status_icon = "✅" if new_task["status"] == "completed" else "🔲"
        print(f"  {status_icon} 恢复: {new_task['subject']} → {task_file.name}")

    print()
    print("=" * 70)
    print("任务恢复完成！")
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


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy 账号迁移工具")
    parser.add_argument("--dir", type=str, help="WorkBuddy 数据目录（默认自动探测：~/.workbuddy-ai 或 ~/.workbuddy）")
    parser.add_argument("--intl", action="store_true", help="强制使用国际版数据目录 ~/.workbuddy-ai（默认自动探测，--dir 优先级更高）")
    parser.add_argument("--diagnose", "-d", action="store_true", help="诊断模式：查看所有账号数据分布")
    parser.add_argument("--source", "-s", type=str, help="源账号 user_id（要迁移出的账号）")
    parser.add_argument("--target", "-t", type=str, help="目标账号 user_id（接收数据的账号，默认从登录态 storage.json / account-snapshot.json 自动推断）")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认直接迁移/回滚")
    parser.add_argument("--rollback", "-r", type=str, help="回滚到指定备份标签")
    parser.add_argument("--restore-tasks", action="store_true", help="恢复历史任务到当前 session")
    parser.add_argument("--list-tasks", action="store_true", help="列出所有历史任务概览")
    parser.add_argument("--session", type=str, help="指定要恢复任务的 session ID")
    parser.add_argument("--generate-commands", action="store_true", help="生成 TaskCreate 命令（与 --restore-tasks 配合使用）")

    args = parser.parse_args()

    # 目录优先级：--dir > --intl > 自动探测（模块导入时已按 _find_workbuddy_dir() 算好）
    if args.dir:
        _set_workbuddy_dir(args.dir)
    elif args.intl:
        _setup_paths("intl")

    # 互斥参数检查：以前 --source 与 --rollback 一起给时 rollback 会静默优先
    if args.rollback and (args.source or args.restore_tasks or args.list_tasks):
        print("❌ --rollback 不能与 --source / --restore-tasks / --list-tasks 同时使用")
        print("   请单独执行，例如：python3 migrate.py --rollback <TAG> --yes")
        sys.exit(1)

    if args.diagnose:
        diagnose()
    elif args.rollback:
        rollback(args.rollback, skip_confirm=args.yes)
    elif args.list_tasks:
        list_tasks()
    elif args.restore_tasks:
        if args.generate_commands:
            generate_task_create_commands(target_session_id=args.session)
        else:
            restore_tasks(target_session_id=args.session, skip_confirm=args.yes)
    elif args.source:
        migrate(args.source, target_uid=args.target, skip_confirm=args.yes, target_is_manual=args.target is not None)
    else:
        # 无参数时进入交互式向导
        interactive_migrate(skip_edition_prompt=args.intl)


if __name__ == "__main__":
    main()
