#!/usr/bin/env bash
# ============================================================================
# force-relogin.sh — 客户端反复自动登录到「错误的账号」时，逼它弹出登录界面
#
# 原理：客户端启动时会读 {数据目录}/storage/skeleton/account-snapshot.json
#       把上次的账号"恢复"回来。把这个文件移走，启动时就恢复不出账号，
#       只能让你重新登录。
#
# 为什么需要它：账号切换后如果客户端每次都自动落回旧账号，
#   左侧会话列表（按客户端登录态过滤）就会一直是空的；先登录对账号，
#   再用 migrate.py 把数据并过去，才是干净的解法。
#
# 用法：
#   scripts/force-relogin.sh                # 国内版（~/.workbuddy）
#   scripts/force-relogin.sh --intl         # 国际版（~/.workbuddy-ai）
#   scripts/force-relogin.sh --dir /path    # 显式指定数据目录
#   scripts/force-relogin.sh --force        # 跳过「客户端已退出」检测
#   scripts/force-relogin.sh --restore      # 回滚：把最近一个 .disabled-* 改回原名
#
# ⚠️ 实测局限（2026-09-22，WorkBuddy 5.5.6 / macOS）：
#     这招**能拿到正确账号**（登录后本地列表立刻读到全部会话），
#     但**挡不住客户端约 1 分钟后的自动回切** —— 回切源头在加密凭据层
#     （疑似 security/<uid>/cipher/entries.json.enc，safeStorage/Keychain 加密；
#      明文 storage.json 与 Electron Local Storage 里都找不到 token）。
#     所以长期用的账号，推荐做法是「把数据并过去」，而不是跟登录态搏斗。
# ============================================================================
set -euo pipefail

EDITION="domestic"
DATA_DIR=""
FORCE=0
RESTORE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --intl) EDITION="intl"; shift ;;
    --dir) DATA_DIR="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --restore) RESTORE=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数: $1（--help 看用法）"; exit 1 ;;
  esac
done

if [ -z "$DATA_DIR" ]; then
  if [ "$EDITION" = "intl" ]; then
    DATA_DIR="$HOME/.workbuddy-ai"
  else
    DATA_DIR="$HOME/.workbuddy"
  fi
fi
SKEL="$DATA_DIR/storage/skeleton"
SNAPSHOT="$SKEL/account-snapshot.json"

if [ ! -d "$DATA_DIR" ]; then
  echo "❌ 数据目录不存在: $DATA_DIR"
  exit 1
fi

# ---------- 回滚模式 ----------
if [ "$RESTORE" = "1" ]; then
  latest="$(ls -1t "$SKEL"/account-snapshot.json.disabled-* 2>/dev/null | head -1 || true)"
  if [ -z "$latest" ]; then
    echo "ℹ️  没有找到 .disabled-* 备份，无需回滚。"
    exit 0
  fi
  mv "$latest" "$SNAPSHOT"
  echo "✅ 已还原: $(basename "$latest") -> account-snapshot.json"
  exit 0
fi

# ---------- 前置：客户端必须完全退出 ----------
if [ "$FORCE" != "1" ]; then
  running=0
  if command -v pgrep >/dev/null 2>&1 && pgrep -f "WorkBuddy" >/dev/null 2>&1; then
    running=1
  elif command -v tasklist >/dev/null 2>&1 && tasklist 2>/dev/null | grep -qi "workbuddy"; then
    running=1
  fi
  if [ "$running" = "1" ]; then
    echo "⚠️  WorkBuddy 还在运行。请先完全退出（macOS: Cmd+Q；确认活动监视器无残留），"
    echo "    或加 --force 强行继续（不建议：退出时客户端会把快照写回来，白做）。"
    exit 1
  fi
fi

# ---------- 主逻辑 ----------
if [ ! -f "$SNAPSHOT" ]; then
  echo "ℹ️  $SNAPSHOT 不存在（已移走或从未登录过），无需处理。"
else
  ts="$(date +%Y%m%d-%H%M%S)"
  mv "$SNAPSHOT" "$SNAPSHOT.disabled-$ts"
  echo "✅ 已移出: account-snapshot.json -> account-snapshot.json.disabled-$ts"
  echo "   （回滚：$0 $([ "$EDITION" = "intl" ] && echo --intl) --restore）"
fi

echo
echo "--- $SKEL 当前内容 ---"
ls -la "$SKEL"
echo
cat <<'TIP'
接着做三件事：
  1. 重新打开 WorkBuddy —— 应该会要求重新登录
  2. 登录「数据最多的那个账号」（也就是左侧面板该显示的那些对话的主人）
  3. 登录后立刻校验，确认新会话已经记在这个账号名下：

     # macOS / Linux
     sqlite3 ~/.workbuddy/workbuddy.db "SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id"

     期望：目标账号名下是全部会话；某个陌生 uid 名下只有刚创建的 1 条。

如果重启后又自动回到错账号，说明凭据层仍在回切：
  python3 scripts/migrate.py --diagnose     # 看四个登录态信号
  然后把数据并到「客户端实际登录的那个 uid」——比跟登录态搏斗更省事。
TIP
