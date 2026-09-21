# 数据隔离全景图

## WorkBuddy 数据存储位置与隔离机制

### 核心存储目录

```
~/.workbuddy/
├── workbuddy.db                  # SQLite 主数据库
│   ├── sessions 表               # 按 user_id 隔离 ← 迁移目标
│   ├── workspaces 表             # 无 user_id，全局共享
│   ├── automations 表            # 无 user_id，全局共享
│   └── automation_runs 表        # 无 user_id，全局共享
│
├── memory/                       # 长期记忆
│   ├── {user_id}_memory.md       # 按文件名隔离 ← 迁移目标
│   └── ...
│
├── connectors/                   # Connector 配置
│   ├── {user_id}/                # 按子目录隔离 ← 迁移目标
│   │   ├── mcp.json
│   │   └── connector-states.json
│   ├── default/
│   └── skills/
│
├── skills/                       # 用户自建 Skills
│   └── {skill_name}/             # 无隔离，全局共享 ✅
│
├── settings.json                 # 全局设置，无隔离 ✅
├── mcp.json                      # 全局 MCP 配置，无隔离 ✅
├── models.json                   # 自定义模型，无隔离 ✅
│
├── projects/                     # 对话正文（不在数据库里！）
│   └── {workspace_path}/         # 按工作空间路径，不按账号 ✅
│       ├── {session_id}.jsonl            # 对话正文
│       ├── {session_id}.meta.json        # 元信息
│       ├── {session_id}.file-rollback.ndjson
│       └── {session_id}/                 # ⚠️ 目录！大工具输出外溢处
│           └── tool-results/*.txt
│
│   # 注意：glob("*/{session_id}*") 会同时命中上面的【文件】和这个【目录】，
│   #      对目录做 shutil.copy2()/unlink() 在 Windows 上会抛 PermissionError / IsADirectoryError
│
├── tasks/                        # 任务列表
│   └── {session_id}/             # 按 session 归属，新版 UI 不读取 ← 恢复目标
│       └── {id}.json             # 任务 JSON（subject/description/status等）
│
├── storage/skeleton/
│   └── account-snapshot.json     # 当前登录账号（primary.uid）← 最可靠的 uid 来源
│
└── logs/                         # 日志，全局共享 ✅
```

### projects 目录名（slug）推导规则

```
C:\Users\alice\WorkBuddy\2026-09-10-14-49-02
  → c-Users-alice-WorkBuddy-2026-09-10-14-49-02
```

盘符转小写、去掉 `:`、`\` 和 `/` 转 `-`，空格保留。
跨版本迁移时优先用 `glob("*/{session_id}*")` 反查，不要硬算。

### 当前登录 user_id 获取方式

**首选**（在数据目录内，天然区分国内/国际版，跨平台统一）：

```bash
python3 -c "
import json,os
p=os.path.expanduser('~/.workbuddy/storage/skeleton/account-snapshot.json')
print(json.load(open(p)).get('primary',{}).get('uid',''))
"
```

**兜底**（旧方式，部分机器上 `%APPDATA%` 探测不到）：

```bash
cat ~/Library/Application\ Support/WorkBuddy/User/globalStorage/storage.json | \
  python3 -c "import json,sys; print(json.load(sys.stdin).get('genie.userId',''))"
```

### 隔离层级速查表

| 数据 | 隔离维度 | 迁移方式 | 风险等级 |
|:---|:---|:---|:---|
| sessions | user_id 字段 | UPDATE SQL | 🟡 中（改DB） |
| memory | 文件名 | 追加合并 | 🟢 低（文本） |
| connectors/mcp.json | 子目录 | JSON 深度合并 | 🟡 中（配置） |
| connectors/states.json | 子目录 | JSON 深度合并 | 🟢 低 |
| **tasks** | **按 session** | **TaskCreate 重建 / 文件复制** | **🟡 中（新版 UI 不读文件）** |
| **projects/*.jsonl** | **按 session（非数据库）** | **跨版本必须复制文件** | **🔴 高（漏了对话就是空的）** |
| **projects/{sid}/tool-results/** | **按 session（目录）** | **必须整目录复制** | **🔴 高（备份阶段若按文件处理会崩溃）** |
| skills | 无 | 不需要迁移 | - |
| automations | 无 | 不需要迁移 | - |
| settings/mcp/models | 无 | 不需要迁移 | - |

### WAL 模式处理

workbuddy.db 使用 SQLite WAL（Write-Ahead Logging）模式。迁移前应执行 checkpoint：

```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('$HOME/.workbuddy/workbuddy.db')
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.close()
print('WAL checkpoint done')
"
```

这确保所有 WAL 日志写入主数据库文件，避免数据不一致。
