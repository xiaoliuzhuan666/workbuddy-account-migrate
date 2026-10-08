---
name: 账号迁移工具
description: WorkBuddy 账号切换后一键同步数据，将旧账号的 Session 历史、Memory 记忆、Connector 配置迁移到当前账号。触发关键词：切账号、迁移、同步数据、账号切换、数据丢失、记录没了、跨设备、导出、导入。
version: 1.7.0
agent_created: true
---

# 账号迁移工具

WorkBuddy 切换账号后，数据通过 `user_id` 隔离，旧账号的 Session、Memory、Connectors 在新账号下不可见。本 Skill 实现一键迁移，将所有历史数据合并到当前登录账号。

## 三个脚本，别用错

| 场景 | 用哪个 |
|:---|:---|
| 切账号后，把**整个账号**的数据合并过来（同一版本内） | `scripts/migrate.py` |
| 只想把**一个对话**从国内版搬到国际版（或反向） | `scripts/migrate_session.py` |
| **跨设备**：电脑A项目A账号A → 电脑B项目A账号B（issue #8） | `scripts/migrate_project.py` |

## 快速使用

**获取代码**（`git clone` 在某些受限 shell / 沙箱环境里会因为目标目录被预创建而失败，用 tarball 更稳）：

```bash
git clone https://github.com/xiaoliuzhuan666/workbuddy-account-migrate.git
# 克隆失败时改用：
mkdir -p workbuddy-account-migrate && cd workbuddy-account-migrate
curl -sSL https://codeload.github.com/xiaoliuzhuan666/workbuddy-account-migrate/tar.gz/refs/heads/main \
  | tar xz --strip-components=1
```

**整账号迁移（最简方式，交互式向导，用户无需知道 user_id）：**

```bash
python3 scripts/migrate.py
```

运行后自动诊断、列出可选账号、用户输入序号即可。**目标账号请选带「← 客户端登录态」标记的那个** —— 选错 uid 迁完重启面板还是空的。

**单对话跨版本迁移：**

```bash
python3 scripts/migrate_session.py                 # 交互式向导
python3 scripts/migrate_session.py --list          # 列出国内版对话
python3 scripts/migrate_session.py --from domestic --to intl --session-id <ID>
```

> ⚠️ 单对话迁移**必须先在真实环境关闭两个版本的 WorkBuddy 窗口**，否则脚本拒绝执行。

**其他模式：**

```bash
python3 scripts/migrate.py --diagnose              # 仅诊断，查看数据分布（只读，无需关客户端）
python3 scripts/migrate.py --source <USER_ID>      # 指定源账号迁移（高级用户）
python3 scripts/migrate.py --intl                  # 国际版（数据目录 ~/.workbuddy-ai）
python3 scripts/migrate.py --dir ~/.workbuddy-ai   # 显式指定数据目录（优先级最高）
python3 scripts/migrate.py --rollback <TAG>        # 回滚到指定备份
python3 scripts/migrate.py --source <UID> --yes --restart  # 迁移后自动重启客户端（macOS，会话列表立即刷新）
python3 scripts/migrate.py --source <UID> --target <UID>   # 显式指定目标（登录态两来源不一致时必加）
python3 scripts/migrate.py --source <UID> --keep-cloud-mapping  # 不重置 edge-sync 云端通道映射（默认会重置）

python3 scripts/migrate_session.py --mode copy     # 迁移后保留源（默认 move 会删源）
python3 scripts/migrate_session.py --dry-run       # 只预览不写盘
python3 scripts/migrate_session.py --backups       # 查看可回滚的备份
python3 scripts/migrate_session.py --rollback <TAG>
```

> ⚠️ `migrate.py` 的 `--source` 迁移与 `--rollback` **同样会检测客户端进程并拒绝执行**
> （v1.6.1 起与 `migrate_session.py` 对齐）；`--diagnose` / `--list-tasks` 这类只读操作不检测。
> 确知风险时可加 `--force` 跳过检测。
>
> 只想绕过「检测本身失败」（查不出进程列表）时用 `--assume-clients-closed`：它按
> "我已确认客户端退出"继续，但**真检测到客户端仍在运行时照样拦截**；`--force` 则把
> 整项检查一起关掉。两个脚本都支持这两个开关。
>
> 被这条守卫拦下时，真终端里会**先问一次**：输入 `y` = 当场确认接受风险并继续，
> 回车 / 其他 = 取消。非终端（CI、管道、重定向）不询问、直接拒绝 —— 问不到人时
> `input()` 要么立刻 EOF 要么把进程挂住。

**退出码**（自动化里别只看 `rc == 0`）：

| 码 | 含义 | 谁会产生 |
|---|---|---|
| `0` | 成功 | 三个脚本 |
| `1` | 参数错误 / 用户取消 / 业务性中止（备份失败、标签不合法、被占用等） | 三个脚本 |
| `2` | 被「客户端仍在运行」守卫拦下（非终端，或终端里回答了否） | 三个脚本 |
| `3` | **未做改动**：源账号没有数据，或源有数据但与目标完全重合、合并无新增 | **只有 `migrate.py`** |

`3` 不是新故障 —— v1.6.1 之前这情况返回 `0`，长期被当成"成功"。

**国内版 vs 国际版**：不只是数据目录不同——**登录态来源也不同**。国内版使用 `~/.workbuddy/`，国际版使用 `~/.workbuddy-ai/`；两个版本都以数据目录内的 `storage/skeleton/account-snapshot.json` → `primary.uid` 为登录态权威来源，国内版额外可用平台 `storage.json` 的 `genie.userId` 兜底，而**国际版一律不读平台 `storage.json`**（它是国内版的登录态文件；读了会把国内版 uid 当成国际版当前账号，`--intl` 迁移后表现为"迁移成功但所有对话消失"）。目录优先级：`--dir` > `--intl` > 自动探测（`~/.workbuddy-ai` 存在且非空判为国际版）。交互式向导会提示选择版本，默认取自动探测结果。

## 跨设备项目迁移（v1.7，issue #8）

场景：家用电脑（账号A）+ 办公电脑（账号B），两地交替做同一个项目。工具只做「打包 / 解包」两件本地事，传输、路径、账号全由用户决定。

**小白路线（推荐）**：无参数运行进向导，全程输序号 + 拖文件，不打任何 flag——源机选「1 打包带走」（包默认放桌面），目标机选「2 导入进来」（自动发现桌面/下载的包，项目文件夹拖进窗口回车）。flag 都是高级用法。

```bash
python3 scripts/migrate_project.py                  # 向导（推荐）
python3 scripts/migrate_project.py export            # 源机：只读打包，客户端开着也能跑，包默认放桌面
python3 scripts/migrate_project.py info x.wbproj     # 看包
python3 scripts/migrate_project.py import x.wbproj --cwd /新路径 --dry-run   # 目标机：先关客户端
python3 scripts/migrate_project.py --rollback <TAG>  # 导入回滚（整库快照 + 文件还原）
```

**关键机制**：
- import 自动探测目标机登录 uid（account-snapshot 权威），`user_id` / `cwd` / `workspaces.path` / `projects/{slug}/` 目录名 / **jsonl 每条消息顶层 `cwd` 字段** 全部重映射到目标机
- slug 一律按**目标机新路径**重推导，不得沿用源 slug（沿用 = 客户端按 `projects/<slug>/<sid>.jsonl` 找不到，"迁移成功却打不开"）
- 冲突语义：**同 id 覆盖**（同一个包反复导入不出双份——"两地交替"靠这个撑住）；同标题不同 id 默认跳过；无 TTY 降级跳过，`--on-conflict overwrite` 显式批量覆盖
- 包内容：sessions/usage 行 + 正文 + tool-results + todos/tasks + `{项目}/.workbuddy/` 工作区记忆（可选）。**账号级 memory/connectors 不在范围**
- 测试：`python3 tests/run_project_tests.py`（37 项合成 fixture，两台"虚拟机器"全链路，不依赖真实数据）

## 问题背景

WorkBuddy 数据存储架构：**本地优先 + 账号隔离**

| 数据类型 | 存储位置 | 隔离方式 | 切账号后 |
|:---|:---|:---|:---|
| Session 历史 | `workbuddy.db` sessions 表 | `user_id` 字段 | ❌ 不可见 |
| 长期记忆 Memory | `~/.workbuddy/memory/{user_id}_memory.md` | 按文件名 | ❌ 不可见 |
| Connectors | `~/.workbuddy/connectors/{user_id}/` | 按子目录 | ❌ 不可见 |
| **历史任务** | `~/.workbuddy/tasks/{session_id}/*.json` | 按 session | ⚠️ 文件在但 UI 不读 |
| Skills | `~/.workbuddy/skills/` | 无隔离 | ✅ 可见 |
| Settings/MCP/Plugins | 全局文件 | 无隔离 | ✅ 可见 |
| 工作空间 Memory | `{workspace}/.workbuddy/memory/` | 绑定工作空间 | ✅ 可见 |

## 迁移执行流程

### Phase 1：环境诊断

1. 自动读取当前登录 `user_id`（**多源交叉验证**，见下方说明）
2. 自动扫描所有已有 `user_id`：
   - `workbuddy.db` sessions 表：`SELECT DISTINCT user_id FROM sessions`
   - `~/.workbuddy/memory/` 下的 `*_memory.md` 文件
   - `~/.workbuddy/connectors/` 下的子目录
3. 展示对比表格，用户输入序号选择要迁移的源账号（无需知道 user_id）

**⚠️ 获取当前 user_id 的关键逻辑（v1.6.1 修订，两个脚本口径统一）**：

当前策略（`migrate.py:get_current_user_id()` 与 `migrate_session.py:get_current_uid()` 完全一致）：

1. **首选**：数据目录内 `storage/skeleton/account-snapshot.json` → `primary.uid`。
   两个版本都会写它，跨平台路径统一（不依赖 `%APPDATA%` 探测），且天然区分国内/国际版
2. **兜底**：国内版平台 `storage.json` 的 `genie.userId`。**国际版一律不读它** ——
   那是国内版登录态文件，读了会把国内版 uid 当成国际版当前账号
3. DB 中 session 数最多的 user_id 仅作辅助交叉验证；登录态来源与 DB 推断不一致时，
   优先登录态来源并**发出警告**
4. **人工复核手段**：查 DB 最新一条 session（`ORDER BY updated_at DESC LIMIT 1`）看它的标题
   是不是你正在用的对话——当前对话所属的 user_id 就是真实登录身份（2026-09-20 实战验证有效）。
   注意这只是**给你核对用的**：脚本里的第 3 条辅助判断用的是"session 数最多的 user_id"，
   不是"最新的"，两者不要混为一谈

> **版本演进（历史，别按旧口径理解）**：
> - v1.3 曾"优先从 DB 最新 session 推断"——实战发现**旧账号在切换前的最后一条 session
>   可能比当前账号的 session 更新**，会误把旧账号当成当前账号
> - v1.4～v1.6.0 改成"按版本区分"（国内版 `storage.json` 权威、国际版 `account-snapshot` 权威），
>   但两个脚本的**优先级顺序相反**：`migrate.py` 把平台 storage.json 排前面，
>   `migrate_session.py` 把 account-snapshot 排前面 → 同一台机器上可能解析出不同的"当前账号"
> - **v1.6.1 起统一为上面这套「account-snapshot 优先、平台 storage.json 兜底」**

> 上面第 1～2 条与下面最佳实践第 3 条并不矛盾：**脚本**按「account-snapshot 优先、
> 平台 storage.json 兜底」读取登录态，但**你在对话里手动判断**时，`storage.json`
> 可能未随账号切换即时更新，此时应当以"当前对话所属 user_id"为准。

**AI 手动迁移时的最佳实践**：

当 AI 在对话中直接执行迁移（而非运行 migrate.py），应：
1. 先查询 `SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id` 看分布
2. 通过当前对话 session 的 user_id 确定目标 ID（最可靠）
3. **不要单独依赖** `storage.json` 的 `genie.userId`——账号切换后它可能没同步更新（仍为旧 ID），
   只把它当作辅助信号，与当前对话的 user_id 交叉核对
4. 执行 UPDATE 后**必须**做 `PRAGMA wal_checkpoint(TRUNCATE)` 确保持久化；
   并留意 checkpoint 返回值 `(busy, log, checkpointed)` 的 busy 标志：
   busy > 0 说明有进程占锁、结果尚未落盘；**busy == -1 表示库不在 WAL 模式，不是占锁**，
   别把它误当成"客户端还在跑"
5. 验证 `SELECT COUNT(*) FROM sessions WHERE user_id = ?`（参数填旧 ID）确认归零
6. user_id 这类外部值**一律用 `?` 占位符**，不要把变量 f-string 拼进 SQL：
   本 Skill 的示例会被 AI 直接照搬执行，拼字符串一旦成为习惯，遇到带引号的值
   就会写出能改坏整张表的语句

### Phase 2：备份（必须）

1. 备份 `workbuddy.db`：**不要用 `cp`**。数据库是 WAL 模式，只复制主库文件会漏掉
   `workbuddy.db-wal` 里还没落盘的数据，备份是陈旧快照。用 sqlite backup API：
   ```bash
   python3 -c "
   import sqlite3, sys
   src, dst = sys.argv[1], sys.argv[2]
   s = sqlite3.connect('file:' + src + '?mode=ro', uri=True)
   d = sqlite3.connect(dst)
   with d: s.backup(d)
   s.close(); d.close()
   print('backup ok ->', dst)
   " ~/.workbuddy/workbuddy.db ~/.workbuddy/workbuddy.db.bak.$(date +%Y%m%d%H%M%S)
   ```
   （临时应急才用 `cp`，但必须**先关闭客户端**并一并带上 `-wal` / `-shm`）
2. 备份 Memory 文件：
   ```bash
   cp ~/.workbuddy/memory/{target_user_id}_memory.md \
      ~/.workbuddy/memory/{target_user_id}_memory.md.bak.$(date +%Y%m%d%H%M%S)
   ```
3. 备份 Connectors：
   ```bash
   cp -r ~/.workbuddy/connectors/{target_user_id}/ \
      ~/.workbuddy/connectors/{target_user_id}.bak.$(date +%Y%m%d%H%M%S)/
   ```

### Phase 3：执行迁移

#### 3.1 Session 历史迁移

```bash
SOURCE_UID='<源user_id>' TARGET_UID='<目标user_id>' python3 - <<'PY'
import os, sqlite3
# 外部值经环境变量传入；把它们拼进 SQL 字符串的例子会被 AI 照搬，值里带个引号
# 就能写出改坏整张表的语句，所以这里一律用 ? 占位符
src_uid, dst_uid = os.environ['SOURCE_UID'], os.environ['TARGET_UID']
conn = sqlite3.connect(os.path.expanduser('~/.workbuddy/workbuddy.db'))
cur = conn.cursor()

# 迁移前 WAL checkpoint
cur.execute('PRAGMA wal_checkpoint(TRUNCATE)')

# 执行迁移：参数化查询（占位符），不做字符串拼接
cur.execute('UPDATE sessions SET user_id = ? WHERE user_id = ?', (dst_uid, src_uid))
print(f'Migrated {cur.rowcount} sessions')
conn.commit()

# 迁移后 WAL checkpoint（确保持久化）
cur.execute('PRAGMA wal_checkpoint(TRUNCATE)')
print(f'WAL checkpoint: {cur.fetchone()}')

# 验证源 user_id 归零
cur.execute('SELECT COUNT(*) FROM sessions WHERE user_id = ?', (src_uid,))
remaining = cur.fetchone()[0]
if remaining > 0:
    print(f'⚠️ 警告：源账号仍有 {remaining} 个 session 未迁移！')
else:
    print('✅ 验证通过：源账号 session 已全部迁移')

conn.close()
PY
```

**注意**：
- 这是**合并**操作，不是覆盖。只改变旧 session 的 user_id，不影响当前账号已有的 session
- 迁移后旧账号的 session 在 UI 上"消失"，但所有数据归到新账号下可见

#### 3.2 Memory 迁移

Memory 是追加式文本文件，策略是**合并而非覆盖**。注意两点：

1. 必须**显式 `encoding="utf-8"`**（Windows 裸 `open()` 按 GBK 解码会直接失败）
2. 结构化 Memory 的正文在一个 `<!-- RAW_JSON_START … RAW_JSON_END -->` 注释块里，
   **按块去重**，不要按行去重——按行去重会把同一个 RAW_JSON 块拆散重复追加

```bash
python3 -c "
import json, os, re
home = os.path.expanduser('~')
src = f'{home}/.workbuddy/memory/{source_user_id}_memory.md'
dst = f'{home}/.workbuddy/memory/{target_user_id}_memory.md'
RAW = re.compile(r'<!--\s*RAW_JSON_START(.*?)RAW_JSON_END\s*-->', re.DOTALL)

def blocks(text):
    out = []
    for m in RAW.finditer(text):
        try:
            b = json.loads(m.group(1).strip()).get('memoryBlock', '')
        except Exception as e:
            print('  ⚠️  有块解析失败，不参与去重:', e); continue
        if b:
            out.append(b)
    return out

def atomic_write(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text); f.flush()
    os.replace(tmp, path)

if not os.path.exists(src):
    print('No source memory file, skipping')
else:
    src_text = open(src, encoding='utf-8').read().strip()
    if not src_text:
        print('Source memory is empty, skipping')
    elif not os.path.exists(dst):
        atomic_write(dst, src_text); print('Copied memory (target was empty)')
    else:
        dst_text = open(dst, encoding='utf-8').read().strip()
        src_b, dst_b = blocks(src_text), blocks(dst_text)
        if src_b and dst_b:
            pending = [b for b in src_b if b.strip() and b not in dst_b]
            if not pending:
                print('Nothing new to migrate (block-level dedupe)')
            else:
                chunks = [m.group(0) for m in RAW.finditer(src_text)
                          if json.loads(m.group(1).strip()).get('memoryBlock', '') in set(pending)]
                # 原子写：追加到一半失败会留下半截未闭合的 RAW_JSON 块
                atomic_write(dst, dst_text + '\n\n---\n## Migrated from {source_user_id}\n\n'
                                  + '\n\n'.join(chunks) + '\n')
                print(f'Appended {len(chunks)} memory block(s)')
        else:
            # 旧格式（无 RAW_JSON）：退回按行去重
            dst_lines = set(dst_text.split('\n'))
            new_lines = [l for l in src_text.split('\n') if l.strip() and l not in dst_lines]
            if new_lines:
                atomic_write(dst, dst_text + '\n\n---\n## Migrated from {source_user_id}\n\n'
                                  + '\n'.join(new_lines) + '\n')
                print(f'Appended {len(new_lines)} unique lines')
            else:
                print('No new content to migrate')
"
```

#### 3.3 Connector 配置迁移

Connector 配置是 JSON 文件，策略是**深度合并**（目标没有的 key 从源补充，已有的保留）。

两个容易做错的细节：

1. **目标侧的空壳不算「已有」**：`args: []` / `env: {}` / `command: ""` 应当从源补齐，
   否则源里同名 key 的实质配置会被空壳挡在外面，等于没合并。
2. **冲突必须报告**：两边都配了不同内容时按「目标已有配置保留不动」保留目标值，
   但一定要列出来告诉用户。以前是静默跳过、还打印「无新增内容」，
   用户会以为源配置已经合并进来了。list（`args`）不做拼接。

⚠️ **不要只比较顶层 key 再决定合不合并**：`mcp.json` 结构是 `{"mcpServers": {...}}`，
顶层只有一个 key。目标一旦已有 `mcpServers`，"没有新增顶层 key" 就会让整个文件被跳过，
一个 server 都合不进去（这正是 v1.6.1 修掉的那个 bug）。做法是**先递归合并、再比较合并前后是否变化**：

```bash
python3 -c "
import json, os
home = os.path.expanduser('~')
src_dir = f'{home}/.workbuddy/connectors/{source_user_id}'
dst_dir = f'{home}/.workbuddy/connectors/{target_user_id}'

def atomic_write(p, text):
    # 先写 .tmp 再 os.replace：直接覆盖写一半断电会留下截断的 JSON
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, p)

def is_unconfigured(v):
    # 空壳视为「没配过」，否则 args 列表会把源的配置挡在外面
    return v is None or v == '' or v == [] or v == {}

def deep_merge(src, dst, path='', stats=None):
    if stats is None:
        stats = {'added': [], 'conflicts': []}
    for k, v in src.items():
        here = f'{path}.{k}' if path else str(k)
        cur = dst.get(k, None)
        if k not in dst or is_unconfigured(cur):
            dst[k] = v
            stats['added'].append(here)
        elif isinstance(v, dict) and isinstance(cur, dict):
            deep_merge(v, cur, here, stats)
        elif v != cur:
            stats['conflicts'].append(here)   # 保留目标值，但必须报告
    return stats

os.makedirs(dst_dir, exist_ok=True)   # connectors/ 本身可能不存在
for fname in ['mcp.json', 'connector-states.json']:
    src_file = os.path.join(src_dir, fname)
    dst_file = os.path.join(dst_dir, fname)
    if not os.path.exists(src_file):
        continue
    # 必须显式 utf-8：Windows 裸 open() 按 GBK 解码中文会失败
    with open(src_file, encoding='utf-8') as f: src_data = json.load(f)
    if os.path.exists(dst_file):
        with open(dst_file, encoding='utf-8') as f: dst_data = json.load(f)
        if isinstance(src_data, dict) and isinstance(dst_data, dict):
            before = json.dumps(dst_data, sort_keys=True, ensure_ascii=False)
            stats = deep_merge(src_data, dst_data)
            n = len(stats['added'])
            if json.dumps(dst_data, sort_keys=True, ensure_ascii=False) == before:
                print(f'Skipped {fname} (nothing new)')
            else:
                atomic_write(dst_file, json.dumps(dst_data, indent=2, ensure_ascii=False))
                print(f'Merged {fname}: +{n}')
            for c in stats['conflicts']:
                print(f'  ! kept target value at {c}')
        else:
            print(f'Skipped {fname} (type conflict)')
    else:
        with open(dst_file, 'w', encoding='utf-8') as f:
            json.dump(src_data, f, indent=2, ensure_ascii=False)
        print(f'Copied {fname}')
"
```

### Phase 4：验证

1. 查询 sessions 数量：
   ```bash
   python3 -c "
   import sqlite3
   conn = sqlite3.connect('$HOME/.workbuddy/workbuddy.db')
   cur = conn.cursor()
   cur.execute('SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id')
   for row in cur.fetchall():
       print(f'  {row[0]}: {row[1]} sessions')
   conn.close()
   "
   ```
2. 检查 Memory 文件是否存在且非空
3. 检查 Connectors 配置是否完整

### Phase 5：收尾

- 告知用户**重启 WorkBuddy 客户端**让变更生效
- 备份文件保留 7 天，可手动删除
- 如果迁移有问题，用备份恢复：
  ```bash
  cp ~/.workbuddy/workbuddy.db.bak.{timestamp} ~/.workbuddy/workbuddy.db
  ```

### Phase 6：历史任务恢复（新版兼容）

**问题**：新版 WorkBuddy 的 `/todos` 面板只读取当前 session 的内存数据，不会扫描 `~/.workbuddy/tasks/` 下的历史 JSON 文件。迁移后这些任务文件虽然还在磁盘上，但 UI 不可见。

**诊断**：

```bash
python3 scripts/migrate.py --list-tasks
```

输出示例：
```
📋 7e46a1bb... | WorkBuddy会议纪要生成与账号迁移工具开发
   13 个任务: 12 完成 / 1 待办
   🔲 待办: 调研 WorkBuddy 数据存储与账号切换机制
```

**恢复方式一：AI 直接用 TaskCreate 创建（推荐）**

这是最可靠的方式，因为 TaskCreate 创建的任务会立即出现在 `/todos` 面板：

```bash
python3 scripts/migrate.py --restore-tasks --generate-commands
```

会生成每个 pending 任务的 TaskCreate 参数 JSON，将它们逐个传给 AI 的 TaskCreate 工具即可。

**恢复方式二：写入文件系统**

将历史任务文件复制到当前 session 的 tasks 目录：

```bash
# 恢复所有 pending 任务
python3 scripts/migrate.py --restore-tasks

# 恢复指定 session 的全部任务（含 completed）
python3 scripts/migrate.py --restore-tasks --session <SESSION_ID>
```

⚠️ 此方式写入文件后需要重启编辑器才可能生效，且新版 UI 可能仍不读取这些文件。

**恢复方式三：在对话中直接执行（最推荐）**

当用户报告"任务记录丢失"时，AI 应：

1. 先用 `--list-tasks` 诊断历史任务
2. 读取 `~/.workbuddy/tasks/` 下各 session 的 JSON 文件
3. 对每个 pending 任务，使用 TaskCreate 工具在当前 session 中重新创建
4. 告知用户任务已恢复

示例：
```python
# 读取历史任务
import json, glob
for f in glob.glob(os.path.expanduser("~/.workbuddy/tasks/*/*.json")):
    with open(f) as fh:
        task = json.load(fh)
    if task.get("status") == "pending":
        # 使用 TaskCreate 工具创建
        pass
```

## 踩坑记录

| 坑 | 说明 | 解决 |
|:---|:---|:---|
| Session 查询按 user_id 过滤 | UI 层只展示当前 user_id 的 session | UPDATE sessions SET user_id 迁移 |
| Memory 按文件名隔离 | `{user_id}_memory.md` 命名绑定账号 | 合并内容到新文件 |
| Connector 按目录隔离 | `connectors/{user_id}/mcp.json` | 深度合并 JSON |
| automations 表无 user_id | 定时任务不按账号隔离，无需迁移 | 跳过 |
| Skills 全局共享 | 不按账号隔离 | 无需迁移 |
| 修改 DB 后需重启 | WorkBuddy 客户端有内存缓存 | 迁移后提示重启 |
| workbuddy.db 有 WAL 模式 | SQLite WAL 日志可能导致数据不一致 | 迁移前先 checkpoint |
| 迁移中创建的会话 user_id 不匹配 | 迁移脚本运行时，当前对话可能以旧 user_id 写入 sessions 表 | Phase 4 用**另开的只读连接**重查源账号 session 数是否归零（同一连接必然看到自己的写入，不能作证据；checkpoint 未完成时查询结果也不可信，脚本会明确说"校验未完成"而不是报成功）。`migrate_session.py` 每处写入也会回查命中行数。**脚本不会自动改回**，发现残留会打印告警要求人工确认 |
| **历史任务 UI 不可见** | **新版 /todos 只读当前 session 内存，不扫描 `tasks/` 目录** | **AI 用 TaskCreate 工具重新创建 pending 任务** |
| **tasks 文件格式兼容** | **旧版任务 JSON 有 subject/description/status 等字段，新版 TaskCreate 参数格式一致** | **字段可直接映射** |
| **storage.json 中 genie.userId 过时** | **账号切换后 storage.json 的 genie.userId 可能没有同步更新，仍为旧 ID。迁移脚本读到旧 ID 作为 target，导致 source=target 跳过迁移** | **改为 `account-snapshot.json`（数据目录内，两版本都写）优先，平台 `storage.json` 只作兜底；DB 按 session 数最多交叉验证，不一致时警告（v1.6.1 起两个脚本口径统一）** |
| **WAL 未 checkpoint 导致迁移丢失** | **即使 UPDATE sessions 成功 + commit，如果 WAL 日志没有 checkpoint，客户端重启后可能读不到修改，数据恢复为旧状态** | **迁移前后各做一次 `PRAGMA wal_checkpoint(TRUNCATE)`，并用另一只只读连接验证源 user_id 归零（v1.3 起；v1.6.1 起额外判断 checkpoint 的 busy 标志）** |
| **AI 手动迁移时的常见错误** | **AI 在对话中直接写 SQL 迁移时，可能：(1) 从 storage.json 读到错误的 target_uid (2) 忘记 WAL checkpoint (3) 不验证结果** | **必须：(1) 从当前对话 session 的 user_id 确定目标 (2) UPDATE 后做 WAL checkpoint (3) 验证源 user_id 归零** |
| **WorkBuddy 会话内运行脚本被沙箱 shim 劫持** | **在 WorkBuddy 会话的 Bash 里跑 migrate.py 时，PYTHONPATH 指向沙箱 shim（sitecustomize.py），拦截 Path.mkdir；目录已存在时 mkdir(exist_ok=True) 抛 PermissionError EEXIST。托管 Python 和系统 Python 都会被劫持** | **v1.6.2 起脚本启动时自动剥离 PYTHONPATH 并 re-exec，直接 `python3 scripts/migrate.py ...` 即可；旧版本用 `env -u PYTHONPATH python3 scripts/migrate.py ...`（2026-09-20 实战踩坑）** |
| **登录态有两个来源，可能长期不一致** | **国内版 `storage.json` 的 `genie.userId`（扩展侧记录）与 `storage/skeleton/account-snapshot.json` 的 `primary.uid`（客户端真实登录态）可能是两个不同的 uid；左侧会话列表按后者过滤** | **v1.6.3 起目标账号改为 account-snapshot.json 优先，diagnose 并列展示三个来源；迁移务必显式 `--target <客户端登录态 uid>`（2026-09-22 实例：反复迁到 storage.json 里的旧 uid，重启后面板始终空白，来回折腾 6 次）** |
| **迁移"成功"但面板仍空白** | **数据迁到了非登录态的那个 uid；面板按登录态过滤，等于没迁** | **migrate() 新增 Phase 4.5 一致性检查：target ≠ 客户端登录态时明确给出两条补救路径（切账号 / 回滚后加 --target 重跑）** |
| **daemon.log 里 uid 搜不到** | **嵌套 JSON 的引号是转义的（`\"userId\":\"...\"`），用 `"userId"` 直接搜匹配不到** | **正则写成 `\\?"userId\\?"\s*:\s*\\?"(...)`；仅作旁证展示，不参与判定** |

## 登录态：先确认你到底登在哪个账号（v1.6.3 必读）

**迁移前唯一要确认的事**：目标账号必须 = **客户端登录态**那个 uid，不是别的。

| 来源 | 路径 | 含义 | 可靠度 |
|:---|:---|:---|:---|
| **客户端登录态** | `{数据目录}/storage/skeleton/account-snapshot.json` → `primary.uid` | 对话客户端真实登录的账号；**左侧会话列表按它过滤** | ⭐ 权威（v1.6.3 起为第一优先级） |
| 扩展侧记录 | 平台 `storage.json` → `genie.userId` | 扩展宿主记录的账号，账号切换后**可能滞后** | 第二优先级 |
| DB 会话最多 | `workbuddy.db` | 辅助兜底，只有前面都读不到才用 | 兜底 |
| 旁证 | `logs/daemon.log` 里最近一次 `listSessions` 的 `userId` | 面板最近一次刷新实际用的 uid | 仅用于打印 |

**为什么必须较真**：2026-09-22 实例中，`storage.json` 一直写着**账号 A**，客户端实际登录的却是**账号 B**（两个都是真实账号，昵称不同）。工具按旧策略每次都把数据并到 A，重启后面板（按 B 过滤）依旧空白，用户来回折腾 6 次。**两个来源不一致时，一切以客户端登录态为准，并显式传 `--target`。**

**30 秒自查**：

```bash
cat ~/.workbuddy/storage/skeleton/account-snapshot.json          # 客户端登录态（真身）
grep genie.userId "$HOME/Library/Application Support/WorkBuddy/User/globalStorage/storage.json"
tail -c 200000 ~/.workbuddy/logs/daemon.log | grep listSessions | tail -1
sqlite3 ~/.workbuddy/workbuddy.db "SELECT user_id,COUNT(*) FROM sessions GROUP BY user_id"
```

**左侧面板空白 = 大概率登错账号**，别急着改数据库：先看上面第 1 条和第 4 条的 uid 是否一致。不一致时，改数据没用，登录对账号才有用。

**附录：强制重登手法与其局限**

客户端反复自动登录到错账号时，可以先把启动快照移走，逼它弹登录：

```bash
mv ~/.workbuddy/storage/skeleton/account-snapshot.json \
   ~/.workbuddy/storage/skeleton/account-snapshot.json.disabled-$(date +%Y%m%d-%H%M%S)
# 然后 Cmd+Q 完全退出客户端 → 重开 → 会要求重新登录
```

⚠️ 实测局限（2026-09-22）：这招**能拿到正确账号**（登录瞬间 `LOCAL_LIST_DONE raw=117`），但**挡不住 1 分钟后的自动回切**——回切源头在加密凭据层（明文 JSON 和 Electron Local Storage 里都没有 token，疑似 `security/<uid>/cipher/entries.json.enc`），不在这个快照文件。所以真正常用的账号建议直接**把数据并过去**，而不是跟登录态搏斗。备份与回滚见 `~/.workbuddy/migrate_backups/`。

**附录：强制重登的辅助脚本**

```bash
bash scripts/force-relogin.sh            # 国内版；--intl 国际版，--dir 指定目录
bash scripts/force-relogin.sh --restore  # 回滚（把最近一个 .disabled-* 改回原名）
```

它做的事就是把 `storage/skeleton/account-snapshot.json` 移走，逼客户端弹登录。默认会检测客户端是否已退出，`--force` 可跳过。

⚠️ 实测局限（2026-09-22）：这招**能拿到正确账号**（登录瞬间 `LOCAL_LIST_DONE raw=117`），但**挡不住约 1 分钟后的自动回切**——回切源头在加密凭据层（明文 JSON 和 Electron Local Storage 里都没有 token，疑似 `security/<uid>/cipher/entries.json.enc`），不在这个快照文件。所以真正常用的账号建议直接**把数据并过去**，而不是跟登录态搏斗。备份与回滚见数据目录下的 `migrate_backups/`。

## 迁移边界：哪些在迁移范围，哪些不在

| 数据 | 位置 | 迁移 | 说明 |
|:---|:---|:---:|:---|
| Session 对话记录 | `workbuddy.db` sessions 表 | ✅ | `UPDATE user_id` |
| 长期记忆 | `memory/{uid}_memory.md` | ✅ | 追加去重合并 |
| MCP 连接器 | `connectors/{uid}/mcp.json` | ✅ | JSON 深度合并 |
| **云端通道映射** | `edge-sync-mapping*.db` 的 `msg_channel` | ✅ **v1.6.3 起自动处理** | 不清理的话，对话在云端仍挂在**旧账号**的通道下，EdgeSync 认为"已同步过"不会重传 → 本机看得到，**换台设备登录新账号看不到** |
| todos / tasks | `todos/{sessionId}.json`、`tasks/{sessionId}/` | ❌ | 按 sessionId 命名，**无账号隔离**，不用迁 |
| Skills / Automations / Settings | 全局配置 | ❌ | 无账号隔离 |
| inspiration | `inspiration/{uid}/` | ❌ | 按 uid 分目录，需要时手动 `mv` 到目标 uid 目录 |
| security | `security/{uid}/` | ❌ | 安全检测模块的加密库，**不要动** |
| storage/user-\<uid\>* | `storage/` | ❌ | 客户端 UI 偏好等，按 uid 分目录，未迁移 |

> `todos/` 与 `tasks/` 没有账号隔离，所以「迁移后左侧任务面板看不到任务」这类问题通常不是迁移造成的，而是新版客户端 `/todos` 只读当前 session 的内存数据（见「Phase 6：历史任务恢复」）。

## 安全规则

1. **必须先备份**，Phase 2 不可跳过
2. **源 user_id 和目标 user_id 必须不同**，防止自我覆盖
3. **Memory 合并用追加而非覆盖**，避免丢失目标账号已有记忆
4. **Connector 用深度合并**，保留目标账号已有配置
5. **迁移完成后提示重启**，确保 UI 刷新缓存
6. **备份文件 7 天后可手动清理**

## 单对话跨版本迁移流程（v1.6）

### 前置条件（强制）

**两个版本的 WorkBuddy 客户端都必须关闭**，脚本会检测进程并拒绝执行。
**迁移与 `--rollback` 都会检测**（回滚同样要改库 + 删文件，客户端在跑时内存缓存会把回滚结果
覆盖回去）；只有只读操作（`--list` / `--backups` / `--dry-run`）不检测：

```python
# Windows:   tasklist /FO CSV /NH        → 按映像名匹配，并用 PID 排除脚本自身
# macOS/Linux: ps -eo pid=,comm= 与 ps -eo pid=,args=
#              （comm 只有进程名，Electron 应用的进程名常是包名，要靠 args 匹配安装路径）
```

⚠️ **必须排除脚本自身**：仓库目录名就叫 `workbuddy-account-migrate`，`ps -eo args=` 里
`python3 .../workbuddy-account-migrate/scripts/migrate_session.py` 这一行自己就会命中关键字，
不排除的话用户会被"检测到客户端正在运行"无条件拦住，只能加 `--force` 把整项检查关掉。
另外要检查命令的 `returncode`：命令失败但没抛异常时 stdout 为空，
那样会把"检测失败"当成"客户端已关闭"。

> **实现只有一份**：进程检测（`PROC_KEYWORDS` / `_is_self_process` / `_client_display_name` /
> `find_running_clients` / `require_clients_closed`）全部在 `scripts/migrate.py` 里；
> `scripts/migrate_session.py` 只做一层薄委托（`require_clients_closed` → `legacy.*`）。
> 这样两个脚本不会再各自漂移，测试也只需打桩 `migrate.find_running_clients` 一处。
> 改检测逻辑时改 `migrate.py` 即可，两边同时生效。

原因（为什么必须关闭）：① 数据还在 WAL 没落盘；② 客户端退出时内存缓存会覆盖写入；③ 两个客户端同时持锁。

### 一个对话 = 5 样东西

只搬数据库行会导致**对话打开是空的**，缺一不可：

1. `sessions` 表 1 行 —— 跨库插入，`user_id` **必须改写为目标版本当前 uid**
2. `session_usage` 表 1 行（token 统计）
3. `workspaces` 表登记 cwd（否则客户端找不到路径）
4. `projects/{slug}/{id}.jsonl` + `.meta.json` + `.file-rollback.ndjson`
5. `projects/{slug}/{id}/tool-results/*.txt` —— **目录**，大工具输出外溢处（第 5 样容易被忘）

### 两个版本 sessions 表列一致，但顺序不同

实测两版本都是 30 列。**必须按列名对齐插入**，不能靠位置：

```python
cols = [c for c in src_row.keys() if c in dst_cols]   # 取交集
INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})
```

### 当前账号 uid 的可靠来源

```
{数据目录}/storage/skeleton/account-snapshot.json → primary.uid
```

在数据目录内，**天然区分国内/国际版**，跨平台统一。旧的 `%APPDATA%/WorkBuddy/.../storage.json` 方案在部分机器上探测不到（`APPDATA` 为空），仅作兜底。

### 冲突分级

| 类型 | 判定 | 选项 |
|:---|:---|:---|
| 硬冲突 | 目标存在**相同 id** | 覆盖 / 不操作 |
| 软冲突 | 目标存在**相同标题但不同 id** | 覆盖 / 不覆盖 / 不操作 |

软冲突最容易踩：重复迁移会在客户端里出现两条一模一样的对话，必须拦。

**覆盖时用源的 ID 写入，删除目标那条旧记录**——不能改成用目标 ID，否则数据库 ID 与正文文件名对不上，对话读不出内容。

### 差异对比指标来源

| 指标 | 来源 |
|:---|:---|
| 最后活动 | `last_activity_at` 与 jsonl 末条 `timestamp` 取较大值 |
| 消息数 | jsonl 中 `type=message`（区分 role） |
| 对话大小 | `.jsonl` 字节数 + 行数 |
| 最后提问 | 最后一条 user message 的 content（**块类型是 `input_text` 不是 `text`**） |

## 踩坑记录（补充）

| 坑 | 说明 | 解决 |
|:---|:---|:---|
| **只搬 DB 行导致对话空白** | **对话正文在 `projects/{slug}/{id}.jsonl`，不在数据库里** | **跨版本必须复制正文文件** |
| **两库列顺序不同** | 列集合相同但顺序不同 | 按列名对齐 INSERT |
| **user_id 没改写** | 国内/国际账号 uid 不同，照搬会导致目标看不到 | 写入时替换为目标版本当前 uid |
| **覆盖时改用目标 ID** | 数据库 ID 与正文文件名不一致 → 对话为空 | 始终用源 ID，删除目标旧记录 |
| **消息块类型不是 text** | content 块是 `input_text` / `output_text` | 取所有带 `text` 字段的块 |
| **重复迁移产生双份** | id 不同但标题相同，检测不到 | 软冲突拦截（标题比对） |
| **客户端未关闭** | WAL 未落盘 + 内存缓存覆盖写入 | 迁移前检测进程，拒绝执行 |
| **双重包装 stdout 崩溃** | `migrate.py` 顶层已包装过 stdout，再包一层会导致第一个 wrapper 被 GC 时关闭共享 buffer | 复用前先判断 `encoding` 是否已为 utf-8 |
| **无冲突时跳过确认** | `decision` 变量若初始化成 `"overwrite"`，无冲突时会被误判为"已确认覆盖"，直接执行 move 删源 | 初始值设为 `None`，只有真正走过冲突询问才跳过确认 |
| **rollback 空 uid 灾难** | `backup_path / ""` 会退化成备份目录本身、`CONNECTORS_DIR / ""` 退化成整个 connectors 目录，`rmtree` 会删光所有账号配置 | 回滚前必须校验 `target_uid` 非空，否则跳过 |
| **非交互模式默认覆盖** | `--yes` 时无法询问，若默认覆盖等于替用户做决定 | 无 TTY 一律降级 skip，需覆盖显式加 `--on-conflict overwrite` |
| **正文不只是文件，还有目录** | `find_project_files()` 用 `glob("*/{sid}*")` 匹配，会命中与会话同名的 **`tool-results/` 目录**；对它 `shutil.copy2()` 在 Windows 上抛 `PermissionError: [Errno 13]`，备份阶段直接崩 | 文件与目录统一走 `copy_path()` / `remove_path()`；统计大小用递归 `path_size()` |
| **目录大小被算成 0** | 目录 `stat().st_size` 不含内部文件，`tool-results/` 的贡献被漏掉 | 递归累加 `p.rglob("*")` |
| **同版本 copy 空转** | `_migrate_intra` 只会改 `user_id`，源对话已属于当前账号时打印「无需迁移」就退出，用户想要的那份复制根本没发生 | uid 相同走克隆分支：新 id + 新标题 + 复制正文/任务 |
| **克隆后正文仍指向原对话** | 每条消息都内嵌 `"sessionId":"<sid>"`，只改文件名不改正文，副本内部还是旧 id | `_rewrite_session_id()` 逐行流式替换（大文件不能整个读进内存），且**只替换 `"sessionId":"..."` 字段值**——消息正文里引用到同串 id 的日志/路径属于用户可见内容，不能改 |
| **克隆回滚误删原对话** | 通用回滚按 `session_id`（= 原始对话 id）删行，同版本克隆时源目标同库，会把原对话一起删掉 | 备份写 `kind=session_clone` + `new_session_id`，回滚走独立分支只删副本 |

## 同版本迁移的两种语义

`--from` 与 `--to` 相同时判断顺序如下（`--mode` 优先于账号归属）：

| 情况 | 行为 | 备份 kind |
|:---|:---|:---|
| `--mode copy`（不论源属于哪个账号） | **保留源**，克隆出新对话归属到目标账号（新 id，标题加「（副本）」） | `session_clone` |
| `--mode move` + 源对话属于**别的账号** | 只 `UPDATE sessions.user_id`（归属转移，源账号将看不到该对话） | `session_intra` |
| `--mode move` + 源对话**已属于当前账号** | 无归属可改，退化为克隆（新 id，标题加「（副本）」） | `session_clone` |

> 曾经 `mode` 参数收下却没用：跨账号 + copy 走的是 UPDATE，源账号会丢失该对话，与"copy=保留源"矛盾。

## 已知坑位与应对（2026-09-21）

| 现象 | 根因 | 处理 |
|:---|:---|:---|
| 备份是陈旧快照 | `migrate.py` 用 `shutil.copy2` 复制主库文件，WAL 里未落盘的数据没带上 | 改用 sqlite backup API（`_backup_db()`），失败退回文件复制并告警 |
| checkpoint 失败仍报"验证通过" | 只打印 `PRAGMA wal_checkpoint` 返回值，没看 busy 标志 | `_wal_checkpoint()` 返回成功与否；busy 时提示"校验未完成" |
| Memory 重复迁移重复追加 | 只比较首个 `memoryBlock`，迁移一次后目标首块≠源块 → 再追加一遍 | 与目标里**所有**已有块比对（`_extract_memory_blocks` + finditer） |
| fixture 里读到真实登录态 | `_get_storage_json_path()` 走 `Path.home()` / `%APPDATA%`，不受 `WORKBUDDY_MIGRATE_HOME` 约束 | 一律从 `_home()` 推导；home 被覆盖时忽略 APPDATA |
| 中文 mcp.json 显示 0 个 server | 裸 `open()` 按 GBK 解码失败，又被裸 `except` 吞掉 | 显式 `encoding="utf-8"` + 打印失败原因 |
| 普通目录被当成账号 | 判定"目录名含 `-`" | UUID 形态匹配（`_looks_like_uid`） |
| `--rollback` 卡在确认 / 与 `--source` 静默冲突 | 硬编码 `input()`，无互斥检查 | `rollback(skip_confirm=)`；组合非法时报错退出 |
| cwd 为空时正文落到 `projects/` 根目录 | `if not dst_dir` 恒为 False，slug 为空直接拼到根目录 | 三级 slug 回退 + 退化即中止 |
| 列表大小统计偏小 | `--list` 路径仍用 `f.stat().st_size` | 改用递归 `path_size()` |
| 跨库插入裸抛 sqlite 异常 | 顶层只捕获 `RuntimeError` | 增加 `sqlite3.Error` 分支给出回滚指引 |
| 进程检测失败被当成"没在跑" | `except: pass` 吞掉异常 | `find_running_clients()` 返回 `(found, trustworthy)`，不可信时要求 `--force` 或更温和的 `--assume-clients-closed`（后者不绕过"真检测到客户端"） |
| 正文 id 改写误伤用户文本 | 整行 `replace(old_sid, new_sid)` | 只替换 `"sessionId":"<old>"` 字段值（实测正文里 2/3 的出现是消息文本） |
| 中途失败后回滚漏项 | `meta.json` 只在最后写一次，中途失败时磁盘上的 meta 缺 `override_deleted` / `source_deleted` / `copied_to`，精确回滚靠这些字段判断 → 静默跳过 | 每个破坏性步骤后 `_write_meta()` 增量落盘 |
| 目标目录名与客户端不一致 | 用 `cwd_to_slug()` 自己推 slug（折叠连续 `-`、盘符小写），未必等于客户端真实建出的目录名 | 优先复用**源侧真实目录名**，`cwd_to_slug()` 只兜底 |
| 覆盖时"行还在、正文没了" | 文件删除发生在 `commit()` 之前，commit 失败则 DB 回滚而文件已删 | 先 commit，成功后再删文件 |
| 回滚后 WAL 被重放 | 只 `copy2` 主库，残留 `-wal`/`-shm` 与新主库不匹配，SQLite 打开时会重放 | 恢复前 `_remove_db_sidecars()` 清掉边车文件 |
| `--intl` 拿到国内版 uid | `_set_workbuddy_dir()` 里 `STORAGE_JSON` 走平台路径，与版本无关；优先级 storage.json 在先 | 国际版目录不读平台 storage.json（`_storage_json_for()`），一律走 account-snapshot |
| mcp.json 合并不生效 | 用"顶层 key 有无新增"判断，而 mcp.json 顶层只有 `mcpServers` → 目标一旦已有就整体 skip | 先 `deep_merge_dict` 再比较合并前后 JSON 是否变化 |
| 脚本把自己当客户端 | 仓库目录名含 `workbuddy`，`ps -eo args=` 会命中脚本自身命令行 → 无条件拦截 | `_is_self_process()` 排除自身 pid 与含脚本路径的命令行 |
| **workspaces 表没有 UNIQUE 约束** | `INSERT OR IGNORE` 拦不住重复导入，同一 path 会插两行 | 先 `DELETE FROM workspaces WHERE path = ?` 再 INSERT（2026-10-06 测试实测） |
| **jsonl 每条消息顶层带 cwd 字段** | 跨设备不改写 → 客户端继续对话仍指向源机路径 | 流式改写顶层 `"cwd"` 字段值（json 解码转义后比对，兼容 `\uXXXX`）；消息文本里出现的路径一律不动 |
| **回滚还原 DB 前不清 WAL** | 陈旧 `-wal/-shm` 会在新连接上重放，把刚还原的快照又盖回导入后状态 | 还原前先删 `workbuddy.db-wal` / `-shm` 再做 sqlite backup API 还原 |
| **部分会话没有 .meta.json** | 按必有文件处理会在打包/导入时空指针 | meta / file-rollback 一律按可选文件收集（实测最新会话无 meta） |
| **schema 漂移是现实** | 同一账号的库，本机 43 列、另一版本 30 列 | 按列名对齐插入是刚需不是优化；包内保留原始行，导入侧取交集 |

## 测试

```bash
python3 tests/prepare_fixture.py    # 在临时目录构造 fixture（只读复制真实数据子集）
python3 tests/run_tests.py          # 端到端 + 单元级用例（数量随本机 fixture 浮动，看末尾「结果」行）
```

新增用例覆盖：跨账号 copy 保留源、软冲突覆盖后 usage 回滚、cwd 为空不落根目录、
UUID 判定、Memory 重复迁移、fixture 下不读真实 storage.json、sqlite backup API 备份、
中文 mcp.json 解析、正文 id 改写范围、`--rollback` 互斥与 `--yes`。

**严禁在真实数据目录上跑迁移测试**——fixture 用 `WORKBUDDY_MIGRATE_HOME` 环境变量指向临时目录，脚本内所有路径都从它派生。

⚠️ **测试脚本仅 Windows 实测通过**（Windows 11 + Python 3.13）。fixture 复制的是本机真实数据，其中 session 的 cwd、projects 目录名都是 Windows 路径格式；macOS / Linux 未测试，本机未安装并登录 WorkBuddy 时造不出 fixture。

## 参考文件

- `references/data_isolation_map.md` — 数据隔离全景图
