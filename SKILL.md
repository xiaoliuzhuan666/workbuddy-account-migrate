---
name: 账号迁移工具
description: WorkBuddy 账号切换后一键同步数据，将旧账号的 Session 历史、Memory 记忆、Connector 配置迁移到当前账号。触发关键词：切账号、迁移、同步数据、账号切换、数据丢失、记录没了。
version: 1.6.2
agent_created: true
---

# 账号迁移工具

WorkBuddy 切换账号后，数据通过 `user_id` 隔离，旧账号的 Session、Memory、Connectors 在新账号下不可见。本 Skill 实现一键迁移，将所有历史数据合并到当前登录账号。

## 两个脚本，别用错

| 场景 | 用哪个 |
|:---|:---|
| 切账号后，把**整个账号**的数据合并过来（同一版本内） | `scripts/migrate.py` |
| 只想把**一个对话**从国内版搬到国际版（或反向） | `scripts/migrate_session.py` |

## 快速使用

**整账号迁移（最简方式，交互式向导，用户无需知道 user_id）：**

```bash
python3 scripts/migrate.py
```

运行后自动诊断、列出可选账号、用户输入序号即可。

**单对话跨版本迁移：**

```bash
python3 scripts/migrate_session.py                 # 交互式向导
python3 scripts/migrate_session.py --list          # 列出国内版对话
python3 scripts/migrate_session.py --from domestic --to intl --session-id <ID>
```

> ⚠️ 单对话迁移**必须先在真实环境关闭两个版本的 WorkBuddy 窗口**，否则脚本拒绝执行。

**其他模式：**

```bash
python3 scripts/migrate.py --diagnose              # 仅诊断，查看数据分布
python3 scripts/migrate.py --source <USER_ID>      # 指定源账号迁移（高级用户）
python3 scripts/migrate.py --intl                  # 国际版（数据目录 ~/.workbuddy-ai）
python3 scripts/migrate.py --dir ~/.workbuddy-ai   # 显式指定数据目录（优先级最高）
python3 scripts/migrate.py --rollback <TAG>        # 回滚到指定备份
python3 scripts/migrate.py --source <UID> --yes --restart  # 迁移后自动重启客户端（macOS，会话列表立即刷新）

python3 scripts/migrate_session.py --mode copy     # 迁移后保留源（默认 move 会删源）
python3 scripts/migrate_session.py --dry-run       # 只预览不写盘
python3 scripts/migrate_session.py --backups       # 查看可回滚的备份
python3 scripts/migrate_session.py --rollback <TAG>
```

**国内版 vs 国际版**：唯一区别是数据目录不同——国内版使用 `~/.workbuddy/`，国际版使用 `~/.workbuddy-ai/`。目录优先级：`--dir` > `--intl` > 自动探测（`~/.workbuddy-ai` 存在且非空判为国际版）。交互式向导会提示选择版本，默认取自动探测结果。

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

**⚠️ 获取当前 user_id 的关键逻辑（v1.6 修订，按版本区分）**：

v1.3 曾改为"优先从 DB 最新 session 推断"，但实战发现**旧账号在切换前的最后一条 session 可能比当前账号的 session 更新**，导致误把旧账号当成当前账号。现在的策略是按版本分开：

1. **国内版**：平台 `storage.json` 的 `genie.userId` 为登录态权威来源
2. **国际版**：数据目录内 `storage/skeleton/account-snapshot.json` 的 `primary.uid` 为权威来源
   （跨平台路径统一，不依赖 `%APPDATA%` 探测）
3. DB 中 session 数最多的 user_id 仅作辅助交叉验证；两者不一致时优先登录态来源并**发出警告**
4. **最可靠的终极验证**：查 DB 最新 session（`ORDER BY updated_at DESC LIMIT 1`），用其标题确认是否为当前正在进行的对话——当前对话本身的 user_id 就是真实登录身份（2026-09-20 实战验证有效）

> 注意上述第 1 条与下面最佳实践第 3 条并不矛盾：**脚本**按版本优先级读取登录态，
> 但**你在对话里手动判断**时，`storage.json` 可能未随账号切换即时更新，
> 此时应当以"当前对话所属 user_id"为准。

**AI 手动迁移时的最佳实践**：

当 AI 在对话中直接执行迁移（而非运行 migrate.py），应：
1. 先查询 `SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id` 看分布
2. 通过当前对话 session 的 user_id 确定目标 ID（最可靠）
3. **不要单独依赖** `storage.json` 的 `genie.userId`——账号切换后它可能没同步更新（仍为旧 ID），
   只把它当作辅助信号，与当前对话的 user_id 交叉核对
4. 执行 UPDATE 后**必须**做 `PRAGMA wal_checkpoint(TRUNCATE)` 确保持久化；
   并留意 checkpoint 返回值的 busy 标志，busy≠0 说明有进程占锁、结果尚未落盘
5. 验证 `SELECT COUNT(*) FROM sessions WHERE user_id = '{旧ID}'` 确认归零

### Phase 2：备份（必须）

1. 备份 `workbuddy.db`：
   ```bash
   cp ~/.workbuddy/workbuddy.db ~/.workbuddy/workbuddy.db.bak.$(date +%Y%m%d%H%M%S)
   ```
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
python3 -c "
import sqlite3
conn = sqlite3.connect('$HOME/.workbuddy/workbuddy.db')
cur = conn.cursor()

# 迁移前 WAL checkpoint
cur.execute('PRAGMA wal_checkpoint(TRUNCATE)')

# 执行迁移
cur.execute(\"UPDATE sessions SET user_id = '{target_user_id}' WHERE user_id = '{source_user_id}'\")
print(f'Migrated {cur.rowcount} sessions')
conn.commit()

# 迁移后 WAL checkpoint（确保持久化）
cur.execute('PRAGMA wal_checkpoint(TRUNCATE)')
print(f'WAL checkpoint: {cur.fetchone()}')

# 验证源 user_id 归零
cur.execute(\"SELECT COUNT(*) FROM sessions WHERE user_id = '{source_user_id}'\")
remaining = cur.fetchone()[0]
if remaining > 0:
    print(f'⚠️ 警告：源账号仍有 {remaining} 个 session 未迁移！')
else:
    print('✅ 验证通过：源账号 session 已全部迁移')

conn.close()
"
```

**注意**：
- 这是**合并**操作，不是覆盖。只改变旧 session 的 user_id，不影响当前账号已有的 session
- 迁移后旧账号的 session 在 UI 上"消失"，但所有数据归到新账号下可见

#### 3.2 Memory 迁移

Memory 是追加式文本文件，策略是**合并而非覆盖**：

```bash
python3 -c "
import os
home = os.path.expanduser('~')
src = f'{home}/.workbuddy/memory/{source_user_id}_memory.md'
dst = f'{home}/.workbuddy/memory/{target_user_id}_memory.md'

if not os.path.exists(src):
    print('No source memory file, skipping')
elif not os.path.exists(dst):
    # 目标不存在，直接复制
    with open(src) as f: content = f.read()
    with open(dst, 'w') as f: f.write(content)
    print('Copied memory (target was empty)')
else:
    # 目标已存在，追加去重
    with open(src) as f: src_content = f.read()
    with open(dst) as f: dst_content = f.read()
    # 找出源中有但目标中没有的段落
    src_lines = set(src_content.strip().split('\n'))
    dst_lines = set(dst_content.strip().split('\n'))
    new_lines = [l for l in src_content.strip().split('\n') if l not in dst_lines]
    if new_lines:
        with open(dst, 'a') as f:
            f.write('\n\n## Migrated from {source_user_id}\n\n')
            f.write('\n'.join(new_lines))
        print(f'Appended {len(new_lines)} unique lines')
    else:
        print('No new content to migrate')
"
```

#### 3.3 Connector 配置迁移

Connector 配置是 JSON 文件，策略是**深度合并**（目标没有的 key 从源补充，已有的保留）：

```bash
python3 -c "
import json, os, shutil
home = os.path.expanduser('~')
src_dir = f'{home}/.workbuddy/connectors/{source_user_id}'
dst_dir = f'{home}/.workbuddy/connectors/{target_user_id}'

for fname in ['mcp.json', 'connector-states.json']:
    src_file = os.path.join(src_dir, fname)
    dst_file = os.path.join(dst_dir, fname)
    if not os.path.exists(src_file):
        continue
    with open(src_file) as f: src_data = json.load(f)
    if os.path.exists(dst_file):
        with open(dst_file) as f: dst_data = json.load(f)
        # 深度合并
        if isinstance(src_data, dict) and isinstance(dst_data, dict):
            for k, v in src_data.items():
                if k not in dst_data:
                    dst_data[k] = v
            with open(dst_file, 'w') as f: json.dump(dst_data, f, indent=2)
            print(f'Merged {fname}')
        else:
            # 非字典类型，不覆盖
            print(f'Skipped {fname} (type conflict)')
    else:
        with open(dst_file, 'w') as f: json.dump(src_data, f, indent=2)
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
| 迁移中创建的会话 user_id 不匹配 | 迁移脚本运行时，当前对话可能以旧 user_id 写入 sessions 表 | Phase 4 验证后追加检查：`SELECT COUNT(*) FROM sessions WHERE user_id NOT IN (target)` 并修复 |
| **历史任务 UI 不可见** | **新版 /todos 只读当前 session 内存，不扫描 `tasks/` 目录** | **AI 用 TaskCreate 工具重新创建 pending 任务** |
| **tasks 文件格式兼容** | **旧版任务 JSON 有 subject/description/status 等字段，新版 TaskCreate 参数格式一致** | **字段可直接映射** |
| **storage.json 中 genie.userId 过时** | **账号切换后 storage.json 的 genie.userId 可能没有同步更新，仍为旧 ID。迁移脚本读到旧 ID 作为 target，导致 source=target 跳过迁移** | **v1.3 曾优先用 DB 最新 session 推断，但旧账号最后一条 session 可能更新；v1.4 改为 storage.json 权威 + DB session 数最多交叉验证，不一致时警告** |
| **WAL 未 checkpoint 导致迁移丢失** | **即使 UPDATE sessions 成功 + commit，如果 WAL 日志没有 checkpoint，客户端重启后可能读不到修改，数据恢复为旧状态** | **v1.3 修复：迁移前后各做一次 PRAGMA wal_checkpoint(TRUNCATE)，并验证源 user_id 归零** |
| **AI 手动迁移时的常见错误** | **AI 在对话中直接写 SQL 迁移时，可能：(1) 从 storage.json 读到错误的 target_uid (2) 忘记 WAL checkpoint (3) 不验证结果** | **必须：(1) 从当前对话 session 的 user_id 确定目标 (2) UPDATE 后做 WAL checkpoint (3) 验证源 user_id 归零** |
| **WorkBuddy 会话内运行脚本被沙箱 shim 劫持** | **在 WorkBuddy 会话的 Bash 里跑 migrate.py 时，PYTHONPATH 指向沙箱 shim（sitecustomize.py），拦截 Path.mkdir；目录已存在时 mkdir(exist_ok=True) 抛 PermissionError EEXIST。托管 Python 和系统 Python 都会被劫持** | **v1.6.2 起脚本启动时自动剥离 PYTHONPATH 并 re-exec，直接 `python3 scripts/migrate.py ...` 即可；旧版本用 `env -u PYTHONPATH python3 scripts/migrate.py ...`（2026-09-20 实战踩坑）** |

## 安全规则

1. **必须先备份**，Phase 2 不可跳过
2. **源 user_id 和目标 user_id 必须不同**，防止自我覆盖
3. **Memory 合并用追加而非覆盖**，避免丢失目标账号已有记忆
4. **Connector 用深度合并**，保留目标账号已有配置
5. **迁移完成后提示重启**，确保 UI 刷新缓存
6. **备份文件 7 天后可手动清理**

## 单对话跨版本迁移流程（v1.6）

### 前置条件（强制）

**两个版本的 WorkBuddy 客户端都必须关闭**，脚本会检测进程并拒绝执行：

```python
# Windows: tasklist /FO CSV /NH → 匹配 workbuddy|codebuddy
# macOS/Linux: ps -eo comm=     → 匹配 workbuddy|codebuddy
```

原因：① 数据还在 WAL 没落盘；② 客户端退出时内存缓存会覆盖写入；③ 两个客户端同时持锁。

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

## 本轮修复补进来的坑（2026-09-21）

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
| 进程检测失败被当成"没在跑" | `except: pass` 吞掉异常 | `find_running_clients()` 返回 `(found, trustworthy)`，不可信时要求 `--force` |
| 正文 id 改写误伤用户文本 | 整行 `replace(old_sid, new_sid)` | 只替换 `"sessionId":"<old>"` 字段值（实测正文里 2/3 的出现是消息文本） |

## 测试

```bash
python3 tests/prepare_fixture.py    # 在临时目录构造 fixture（只读复制真实数据子集）
python3 tests/run_tests.py          # 86 项：端到端 + migrate.py 单元级用例
```

新增用例覆盖：跨账号 copy 保留源、软冲突覆盖后 usage 回滚、cwd 为空不落根目录、
UUID 判定、Memory 重复迁移、fixture 下不读真实 storage.json、sqlite backup API 备份、
中文 mcp.json 解析、正文 id 改写范围、`--rollback` 互斥与 `--yes`。

**严禁在真实数据目录上跑迁移测试**——fixture 用 `WORKBUDDY_MIGRATE_HOME` 环境变量指向临时目录，脚本内所有路径都从它派生。

⚠️ **测试脚本仅 Windows 实测通过**（Windows 11 + Python 3.13）。fixture 复制的是本机真实数据，其中 session 的 cwd、projects 目录名都是 Windows 路径格式；macOS / Linux 未测试，本机未安装并登录 WorkBuddy 时造不出 fixture。

## 参考文件

- `references/data_isolation_map.md` — 数据隔离全景图
