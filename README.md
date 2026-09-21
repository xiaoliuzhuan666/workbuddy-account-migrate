# workbuddy-account-migrate

> WorkBuddy 切换账号后对话记录不见了？一键恢复。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: macOS | Windows | Linux](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20%7C%20Linux-blue.svg)](https://github.com/xiaoliuzhuan666/workbuddy-account-migrate)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-green.svg)](https://www.python.org/)
[![Version 1.6.1](https://img.shields.io/badge/Version-1.6.1-brightgreen.svg)](https://github.com/xiaoliuzhuan666/workbuddy-account-migrate)

**[English](#english) | [中文](#chinese)**

---

<h2 id="chinese">中文</h2>

### 你是不是遇到了这个问题？

WorkBuddy 切换账号 / 重新登录 / 换了腾讯云身份后，**之前的对话记录全没了**？长期记忆、MCP 连接器配置也看不到了？

**数据其实没丢**——它们还在磁盘上，只是 WorkBuddy 用 `user_id` 做了账号隔离，新账号的 UI 看不到旧账号的数据。

本工具一键把旧账号的数据合并到当前登录账号，**对话记录、记忆、连接器全部恢复可见**。

```
切换账号前：                       切换账号后：
┌──────────────────┐              ┌──────────────────┐
│  账号 A           │              │  账号 B           │
│  26 个对话 ✅     │    ──→      │  26 个对话 ❌     │ ← UI 看不到了
│  13KB 记忆 ✅     │              │  13KB 记忆 ❌     │ ← 文件还在磁盘上
│  17 个 MCP ✅     │              │  17 个 MCP ❌     │
└──────────────────┘              └──────────────────┘
                                         │
                                    运行迁移脚本
                                         │
                                         ▼
                                  ┌──────────────────┐
                                  │  账号 B           │
                                  │  26 个对话 ✅     │ ← 合并到当前账号
                                  │  13KB 记忆 ✅     │ ← 追加去重
                                  │  17 个 MCP ✅     │ ← 深度合并
                                  └──────────────────┘
```

### 功能特性

| 特性 | 说明 |
|:---|:---|
| ✅ 交互式向导 | 运行即用，列出所有账号，手动选择目标/源账号，无需知道 user_id |
| ✅ 跨平台路径适配 | v1.4：storage.json 路径自动适配 macOS / Windows / Linux |
| ✅ 国内版 / 国际版 | 交互式向导可选版本，或 `--intl` 参数指定国际版（`~/.workbuddy-ai`） |
| ✅ Session 对话记录迁移 | 修改 SQLite 数据库中的 `user_id` 字段，对话记录全部回归 |
| ✅ Memory 长期记忆合并 | 追加式去重合并，不会丢失当前账号已有记忆 |
| ✅ Connector MCP 连接器合并 | JSON 深度合并，目标账号已有配置保留不动 |
| ✅ 自动备份 + 回滚 | 迁移前自动备份数据库、记忆、连接器，支持一键回滚 |
| ✅ WAL 安全处理 | 迁移前后执行 SQLite checkpoint，确保数据持久化 |
| ✅ 登录态权威识别 | v1.4：以 storage.json 为当前账号权威来源，DB 按 session 数最多辅助验证 |
| ✅ 迁移结果验证 | v1.3：UPDATE 后验证源 user_id 归零，确认迁移成功 |
| ✅ 零依赖 | 仅需 Python 3.8+，无第三方包 |

### 快速开始

```bash
git clone https://github.com/xiaoliuzhuan666/workbuddy-account-migrate.git
cd workbuddy-account-migrate
python3 scripts/migrate.py
```

运行效果：

```
======================================================================
WorkBuddy 版本选择
======================================================================

  1. 国内版（数据目录 ~/.workbuddy）
  2. 国际版（数据目录 ~/.workbuddy-ai）

请选择 WorkBuddy 版本（输入序号，默认 1）:
```

选择版本后进入账号选择：

```
======================================================================
WorkBuddy 账号迁移向导
======================================================================

请选择迁移方向：先选【目标账号】（接收数据），再选【源账号】（被迁移）

  序号   user_id                                  Sessions     Memory   Connectors
  ------------------------------------------------------------------------
  1      abc12345-6789-...                              18     13.0KB  17mcp/6conn
  2      def67890-1234-...                               7      5.1KB  17mcp/4conn

请选择【目标账号】（接收数据的账号，输入序号）: 1
```

输入序号即可，全程不需要知道 user_id。

**其他模式：**

```bash
# 仅诊断 — 查看所有账号数据分布
python3 scripts/migrate.py --diagnose

# 指定源账号迁移（高级用户）
python3 scripts/migrate.py --source <USER_ID>

# 显式指定目标账号（不依赖当前登录态推断，v1.4 新增）
python3 scripts/migrate.py --source <USER_ID> --target <USER_ID>

# 国际版（数据目录 ~/.workbuddy-ai）
python3 scripts/migrate.py --intl
python3 scripts/migrate.py --intl --diagnose
python3 scripts/migrate.py --intl --source <USER_ID>

# 显式指定数据目录（优先级高于 --intl）
python3 scripts/migrate.py --dir ~/.workbuddy-ai

# 回滚到指定备份
python3 scripts/migrate.py --rollback <TAG>
```

> **国内版 vs 国际版**：唯一区别是数据目录不同——国内版使用 `~/.workbuddy/`，国际版使用 `~/.workbuddy-ai/`。其他命令和行为完全一致。
> **目录优先级**：`--dir` > `--intl` > 自动探测（`~/.workbuddy-ai` 存在且非空时判为国际版，否则国内版）。交互式向导还会让你确认一次版本。

### 迁移内容

| 数据类型 | 存储位置 | 隔离方式 | 是否迁移 | 迁移策略 |
|:---|:---|:---|:---:|:---|
| Session 对话记录 | `workbuddy.db` sessions 表 | `user_id` 字段 | ✅ | UPDATE user_id |
| 长期记忆 Memory | `~/.workbuddy/memory/{uid}_memory.md` | 按文件名 | ✅ | 追加去重合并 |
| Connector 连接器配置 | `~/.workbuddy/connectors/{uid}/mcp.json` | 按子目录 | ✅ | JSON 深度合并 |
| Skills 技能 | `~/.workbuddy/skills/` | 无隔离 | ❌ | 全局共享，无需迁移 |
| Automations 定时任务 | `workbuddy.db` automations 表 | 无 user_id | ❌ | 全局共享，无需迁移 |
| Settings / MCP / Plugins | 全局配置文件 | 无隔离 | ❌ | 全局共享，无需迁移 |

### 单对话跨版本迁移（v1.6.0）

上面是「整个账号」的迁移。如果你只想把**某一个对话**从国内版搬到国际版（或反过来），用另一个脚本：

```bash
python3 scripts/migrate_session.py                 # 交互式向导，一步到位
python3 scripts/migrate_session.py --list          # 先看看国内版有哪些对话
python3 scripts/migrate_session.py --from domestic --to intl --session-id <SESSION_ID>
```

**与整账号迁移的区别**

| | `migrate.py` | `migrate_session.py` |
|:---|:---|:---|
| 范围 | 整个账号（全部对话 + 记忆 + 连接器） | **一个对话** |
| 版本 | 同一版本内 | **支持国内 ⇄ 国际** |
| 默认语义 | 合并（源保留） | **移动（源删除）**，可 `--mode copy` |

> **`--mode copy` 的语义**：无论源对话属于哪个账号，copy 都会**保留源**，在目标账号下克隆出一份新对话。
> 此前跨账号 + copy 走的是改 `user_id`（归属转移），源账号会丢失该对话，与"保留源"矛盾，已修正。

**⚠️ 迁移前必须关闭两个版本的 WorkBuddy 窗口**，脚本会检测并拒绝执行。原因：数据还在 WAL 里没落盘、客户端内存缓存会覆盖你的写入。

**一个对话实际包含哪些东西**（少一样客户端就显示异常）：

| 数据 | 位置 | 说明 |
|:---|:---|:---|
| session 行 | `workbuddy.db` sessions 表 | 跨库插入，`user_id` 改写为目标版本账号 |
| 用量行 | `session_usage` 表 | token 统计 |
| 工作区登记 | `workspaces` 表 | 否则客户端找不到路径 |
| **对话正文** | `projects/{slug}/{id}.jsonl` | **不复制的话对话是空的** |
| 工具结果 | `projects/{slug}/{id}/tool-results/*.txt` | 大工具输出外溢目录，缺失会丢内容 |

**冲突处理**（目标已存在时询问，并展示差异帮你判断）：

```
⚠️  目标版本已存在【标题相同】但 ID 不同的对话
  原因：标题一致但 id 不同，很可能是同一段对话被迁移过一次，
       再次迁移会在客户端里出现两条看起来一样的对话。

  指标          目标现有（将被覆盖）        源（将写入）
  ─────────────────────────────────────────────────────────
  ★ 最后活动    09-10 08:26                09-10 15:02
  ★ 消息数      5 条（我 5 / AI 0）         26 条（我 3 / AI 23）
  ★ 对话大小    453 B · 5 行               605.6 KB · 140 行
    最后提问    老的提问内容                …
  ─────────────────────────────────────────────────────────
  → 源比目标新 6 小时 36 分钟，消息多 21 条，内容远超目标（约 1369 倍）
  → 建议：覆盖（源更新且更完整）

  请确认 [y] 覆盖 / [s] 不覆盖（跳过该对话） / [n] 不操作（取消）:
```

- **硬冲突**（ID 相同）：`覆盖` / `不操作`
- **软冲突**（标题相同、ID 不同）：`覆盖` / `不覆盖` / `不操作`
- 覆盖时始终以**源的 ID** 写入并删除目标那条旧记录，保证正文文件名与 ID 一致

**同版本复制**（`--from` 与 `--to` 相同）

同一版本内有两种语义，脚本会自动判断：

| 情况 | 行为 |
|:---|:---|
| 源对话属于**别的账号** | 只把 `user_id` 改到当前账号（归属转移） |
| 源对话**已属于当前账号** | 克隆出一条新对话：新的 session id，标题加「（副本）」 |

克隆会一并处理三件容易漏掉的事：正文文件按新 id 改名、正文内部每条消息的
`"sessionId"` 全部改写为新 id、工具结果目录 `tool-results/` 一起复制。
原对话保持不变，回滚只删副本、不动原对话。

**参数**

| 参数 | 说明 | 默认 |
|:---|:---|:---|
| `--from` / `--to` | 源/目标版本 `domestic`\|`intl` | `domestic` |
| `--session-id` | 对话 id（支持前缀） | - |
| `--mode` | `move`（迁移后删源）/ `copy`（保留源并克隆一份到目标账号） | **`move`** |
| `--on-conflict` | `ask`/`skip`/`overwrite`/`newer`（无终端询问时 `ask` 降级为 `skip`） | `ask` |
| `--dry-run` | 只打印计划不写盘 | 关 |
| `--force` | 跳过"客户端必须关闭"检测 | 关 |
| `--backups` / `--rollback TAG` | 查看备份 / 回滚 | - |

回滚精确到单条，不影响其他对话：`python3 scripts/migrate_session.py --rollback <TAG>`。

> ⚠️ **平台说明**：单对话迁移的**完整跨版本链路仅 Windows 实测通过**（Windows 11 + Python 3.13）。
> macOS 已部分验证（2026-09-21）：`--list` 在国内版真实数据 fixture 上工作正常（含中文名渲染、
> 大小统计），脚本本身是跨平台的（路径走 pathlib、进程检测 Windows 用 `tasklist`、其他平台用
> `ps`），但跨版本迁移全流程在 macOS / Linux 未经实测，欢迎提 Issue 反馈。

### 工作原理

**Step 1：自动诊断** — 从数据库、Memory 文件、Connector 目录三个来源自动发现所有账号。当前登录账号以 **storage.json 的 genie.userId 为权威来源**，DB 中 session 数最多的 user_id 作为辅助验证，不一致时以 storage.json 为准并发出警告（v1.4 起；此前用"最新 session"推断，旧账号的最后一条 session 可能比当前账号更新，导致误判）。

**Step 2：安全备份** — 迁移前自动备份到 `~/.workbuddy/migrate_backups/{timestamp}_{uid}/`

**Step 3：执行迁移** — Session 用 `UPDATE user_id`，Memory 逐行去重追加，Connector JSON 深度合并

**Step 4：持久化 + 验证** — 迁移后执行 WAL checkpoint 确保数据落盘，验证源 user_id 归零确认迁移成功

**Step 5：重启提示** — 提示重启 WorkBuddy 客户端，UI 刷新缓存后数据可见

### 兼容性

| 平台 | 状态 |
|:---|:---|
| WorkBuddy 国内版 (macOS) | ✅ 已测试 |
| WorkBuddy 国内版 (Windows) | ✅ 已适配（v1.4，`%APPDATA%` 路径，欢迎实测反馈） |
| WorkBuddy 国内版 (Linux) | ✅ 已适配（v1.4，`XDG_CONFIG_HOME` 路径，欢迎实测反馈） |
| WorkBuddy 国际版 (Windows) | ✅ 已测试（v1.5，数据目录 `~/.workbuddy-ai/`，使用 `--intl` 参数） |
| WorkBuddy 国际版 (macOS / Linux) | ⚠️ 理论支持，未实测 |
| CodeBuddy CLI | ❌ 不适用（见下方说明） |

> **国内版 vs 国际版**：国内版数据目录为 `~/.workbuddy/`，国际版为 `~/.workbuddy-ai/`。迁移工具默认操作国内版，加 `--intl` 参数操作国际版。交互式向导会提示选择版本。

**为什么不支持 CodeBuddy CLI？** CodeBuddy CLI 的记忆按项目维度隔离（`~/.codebuddy/memories/{project-id}/`），对话记录按 `{sessionId}.jsonl` 独立文件存储，不依赖 `user_id` 过滤，**不存在账号切换后数据丢失的问题**。如果你是 CodeBuddy 用户遇到类似问题，欢迎提 Issue。

### 安全规则

1. **必须先备份** — 迁移前自动创建备份，不可跳过
2. **源 ≠ 目标** — 防止自我覆盖
3. **Memory 追加不覆盖** — 不会丢失当前账号已有记忆
4. **Connector 深度合并** — 保留目标账号已有配置
5. **迁移后重启** — WorkBuddy 客户端有内存缓存
6. **备份 7 天可清** — 手动删除即可

### 回滚

```bash
ls ~/.workbuddy/migrate_backups/          # 国内版（国际版在 ~/.workbuddy-ai/migrate_backups/）
python3 scripts/migrate.py --rollback 20260525170000_abc12345
```

### 竞品对比

| 项目 | 定位 | 同平台账号切换 | Session 迁移 | Memory 迁移 |
|:---|:---|:---:|:---:|:---:|
| **本项目** | 同平台账号切换数据合并 | ✅ | ✅ | ✅ |
| [ai-memory-sync](https://github.com/supercrzy/ai-memory-sync) | 跨设备记忆同步 | ❌ | ❌ | ✅ |
| [claw-migrate](https://github.com/citriac/claw-migrate) | 跨平台记忆迁移 | ❌ | ❌ | ✅ |
| [workbuddy-manager](https://github.com/starsss0416/workbuddy-manager) | 本地会话管理 | ❌ | ✅ 本地 | ❌ |

**本项目填补的空白**：跨平台迁移和跨设备同步都有人做了，但**同平台账号切换后的数据合并**是唯一没人覆盖的场景。

### FAQ

**Q: WorkBuddy 切换账号后对话记录 / 历史记录真的没丢吗？**

A: 没丢。数据文件全部还在磁盘上，只是 UI 按 `user_id` 过滤导致看不到。本工具把这些数据合并到当前账号下即可恢复可见。

**Q: 迁移后旧账号数据还在吗？**

A: Session 的 `user_id` 被改为新账号，所以在旧账号的 UI 下不可见了。Memory 和 Connector 的源文件仍然保留，可手动清理。

**Q: 支持双向迁移吗？**

A: 支持。从 B 迁到 A 后，可以登录 B 再执行 `--source <A的user_id>`，或者用 `--target` 直接指定目标账号、无需切换登录。Memory 按行去重、Connector 按 key 合并，反向迁移不会产生重复内容。注意：反向迁移会把 A 名下**所有** session 一起迁走（包括 A 原有的）；如果只是想撤销上一次迁移，用 `--rollback` 回滚更干净。

**Q: 支持 CodeBuddy CLI 吗？**

A: 暂不支持。CodeBuddy CLI 不存在账号切换数据丢失的问题。详见上方「兼容性」章节。

**Q: Windows / Linux 可以用吗？**

A: 可以。v1.4 起 storage.json 路径已按平台自动适配（macOS `~/Library/Application Support/...`、Windows `%APPDATA%`、Linux `XDG_CONFIG_HOME`）。

**Q: 在 WorkBuddy / AI 助手的会话里运行脚本报错 `PermissionError: [Errno 13]` / mkdir 异常？**

A: 部分 AI 助手的会话内 Shell 会通过 `PYTHONPATH` 注入沙箱 shim（如 sitecustomize.py），劫持所有 Python 进程的文件操作——备份目录已存在时 `mkdir(exist_ok=True)` 也会抛异常，迁移还没开始就崩（2026-09-20 实战踩坑，SKILL.md 有记录）。解法是剥掉该变量运行（脚本仅用标准库，不需要它）：

```bash
env -u PYTHONPATH python3 scripts/migrate.py
```

### 项目结构

```
workbuddy-account-migrate/
├── README.md                              # 本文档
├── LICENSE                                # MIT 许可证
├── .gitignore                             # 排除敏感文件
├── SKILL.md                               # WorkBuddy Skill 描述符
├── scripts/
│   ├── migrate.py                         # 整账号迁移（同版本内）
│   └── migrate_session.py                 # 单对话迁移（支持跨版本，v1.6）
├── tests/
│   ├── prepare_fixture.py                 # 构造临时测试 fixture（只读复制真实数据）
│   └── run_tests.py                       # 端到端测试（86 项，含 migrate.py 单元级用例）
└── references/
    └── data_isolation_map.md              # 数据隔离全景图
```

> 测试全部在临时 fixture 中运行，不会触碰真实数据目录。
> `python3 tests/run_tests.py` 即可复现全部验证。
>
> ⚠️ **完整跨版本测试链仅 Windows 实测通过**（Windows 11 + Python 3.13）。fixture 复制的是本机
> 真实 WorkBuddy 数据，跨版本测试用例要求本机**同时有国内版和国际版**数据（fixture 缺某版目录
> 时会如实跳过并报告）。macOS 实测：仅有国内版数据时 fixture 只能造出 domestic 一半，
> `run_tests.py` 会因缺少国际版库中止——这是环境限制而非脚本缺陷。

### 贡献

- Bug 报告 / 功能请求 → [Issues](https://github.com/xiaoliuzhuan666/workbuddy-account-migrate/issues)
- 代码贡献 → 提交 PR，请确保无硬编码的 user_id 或 Token
- Windows / Linux 实测反馈 → 欢迎 Issue

### 已知限制

| 限制 | 说明 |
|:---|:---|
| Memory 多语义块 | 结构化 Memory 迁移是**追加**一个 `RAW_JSON` 块。WorkBuddy 客户端是否合并读取多个块未经验证——若客户端只读首块，迁移过去的记忆在文件里存在但 UI 不显示。脚本追加后会提示你去客户端确认 |
| 非 Windows 进程检测 | 客户端"必须关闭"检测在 Windows 用 `tasklist` 实测有效；macOS / Linux 走 `ps`，Electron 应用的进程名可能是包名，存在漏报，必要时用 `--force` 并自行确认 |
| 会话 `cwd` 为空 | 极少数会话记录里 `cwd` 为空，正文目录只能靠源侧目录名回退；若连这也取不到，脚本会中止而不是静默放错位置 |
| 正文 id 改写范围 | 只改写 `"sessionId":"..."` 字段值。消息正文里引用到的旧 id（日志、路径）保持原样——那是用户可见内容，不应被改 |

### 更新日志

#### v1.6.0 (2026-09-10)

**新增：`scripts/migrate_session.py` — 单对话跨版本迁移**（本节主体来自 [@bukall](https://github.com/bukall)，PR #5）

只迁移**指定的一个对话**，并支持**国内版 ⇄ 国际版**双向：

- 默认 `move`（迁移后删除源版本中的该对话），可选 `--mode copy` 保留源
- 迁移单元完整：**session 行 + `session_usage` + `workspaces` 登记 + `projects/*.jsonl` 对话正文**。只搬数据库行是不够的，正文不在数据库里，漏了对话就是空的
- 跨版本迁移自动把 `user_id` 改写为目标版本当前登录账号，否则目标版本里依然看不到
- 冲突分级询问：
  - 硬冲突（ID 相同）→ 覆盖 / 不操作
  - 软冲突（标题相同、ID 不同，多为重复迁移）→ 覆盖 / 不覆盖 / 不操作
  - 询问时展示差异对比（最后活动时间、消息数、对话大小、工具调用、token 用量、最后提问）并给出覆盖建议
- 覆盖时始终以**源的 ID** 写入并删除目标那条旧记录，保证正文文件名与 ID 一致
- 备份精确到单条，回滚不影响其他对话；`--dry-run` 可先预览
- 安全：迁移前检测客户端是否运行，**未关闭则拒绝执行**（WAL 未落盘 + 内存缓存会覆盖写入）
- 新增 `tests/`：`prepare_fixture.py` 从真实数据只读复制出临时 fixture，`run_tests.py` 提供 86 项端到端 + 单元级测试，全程在临时目录运行

**修复：正文含 `tool-results/` 目录时备份直接崩溃**

- 大工具输出会被外溢到 `projects/{slug}/{id}/tool-results/*.txt`（一个与会话同名的**目录**）。
  备份阶段对目录调用 `shutil.copy2()` 在 Windows 上抛 `PermissionError: [Errno 13]`，
  整个迁移中断。现在文件与目录统一走 `copy_path()` / `remove_path()`
- 同一根因还波及迁移复制、move 删源、回滚还原、软冲突清理旧记录四处，一并修复
- 对话大小统计改为递归累加，此前 `tool-results/` 被算成 0，显示的体积偏小
- 列表里区分显示「N 个文件 + N 个目录（tool-results）」，不再让人误以为多出异常项

**修复：同版本选 copy 时提示「无需迁移」却什么也没做**

- `_migrate_intra` 原先只实现「改 `user_id`」一种语义，源对话已属于当前账号时无从可改就空转
- 现在自动按**克隆**处理：生成新 session id，标题加「（副本）」，复制正文与任务数据
- 克隆会改写正文中每条消息的 `"sessionId"`（否则副本内部仍指向原对话）、
  并按新 id 重命名正文文件与 `tool-results/` 目录
- 回滚按 `kind=session_clone` 单独处理，**只删副本、不动原对话**
  （走通用回滚分支会按原 id 删行，把原始对话一起删掉）
- 备份中途失败会自清理，不再残留没有 `meta.json` 的半成品目录

**改进：原有 `scripts/migrate.py`**

- 当前账号识别：国内版继续以平台 `storage.json` 的 `genie.userId` 为权威；国际版使用数据目录内的 `storage/skeleton/account-snapshot.json` → `primary.uid`（跨平台路径统一，不依赖 `%APPDATA%` 探测，来自 [@fhjowe](https://github.com/fhjowe)，PR #6）；两者都取不到时回落 DB 中 session 数最多的 `user_id`
- 回滚安全性：备份 `meta.json` 缺失导致 `target_uid` 为空时，跳过 Memory / Connectors 恢复。原先路径会退化成整个 `connectors/` 目录并被 `rmtree` **删光所有账号的连接器配置**
- 回滚完整性：Connectors / Memory 的恢复不再要求目标当前必须存在，只要备份里有就恢复。原先迁移后清理过目录就恢复不了
- Memory 迁移在 `memory/` 目录不存在时自动创建，不再报错

#### v1.6.1 (2026-09-21)

**修复：`migrate_session.py` 行为与文档不符 / 静默失败**（全部改动来自 [@bukall](https://github.com/bukall)，PR #5）

- `--mode copy` 跨账号时不再退化成"改 `user_id` 转移归属"：copy 一律保留源，克隆一份归属到目标账号
- 会话 `cwd` 为空时不再把正文静默写到 `projects/` 根目录（客户端按 `projects/<slug>/<id>.jsonl` 找，
  放根目录等于迁移成功却打不开）。现在按「行 cwd → 会话画像 cwd → 源正文所在目录名」三级回退，
  仍无法确定则中止并提示回滚
- 列表大小统计改为递归累加（`--list` 这一处漏改，`tool-results/` 仍被算成 ~4KB）
- 顶层补上 `sqlite3.Error` 分支：跨库插入撞上目标库新增的 NOT NULL 无默认值列时，
  给出"用 --rollback 回滚"的可操作提示，而不是 traceback
- 软冲突覆盖：被删掉的那条目标对话的 `session_usage` 现在会一起备份与回滚
- 客户端进程检测失败不再静默当成"已关闭"（要求显式 `--force`）；非 Windows 额外用
  `ps -eo args=` 匹配完整命令行，避免 Electron 包名漏报
- 正文 id 改写只动 `"sessionId":"..."` 字段值：以前整行 replace 会把消息正文里
  恰好出现同串 id 的文本（日志、路径）一起改坏

**修复：`migrate.py` 的数据安全与解析问题**

- 备份数据库改用 sqlite backup API（带 WAL），不再 `shutil.copy2` 主库文件
  ——客户端没退出时后者拿到的是陈旧快照；失败时退回文件复制并明确告警
- `PRAGMA wal_checkpoint` 的 busy 标志现在会判断：checkpoint 没做完时不再宣称"验证通过"
- Memory 结构化迁移改为与目标里**所有**已有 `memoryBlock` 比对，重复执行不再重复追加同一块
- `_get_storage_json_path()` 改为受 `WORKBUDDY_MIGRATE_HOME` / `--dir` 约束，
  不再去读真实机器的平台 storage.json；`STORAGE_JSON` 为 `None` 时不再直接 `open()`
- `get_connector_info()` 显式按 utf-8 读取 `mcp.json`（中文配置此前被静默吞掉显示 0 个 server）
- user_id 判定改用 UUID 形态匹配，不再"目录名含连字符就算账号"
- `--rollback` 支持 `--yes` 跳过确认；与 `--source` 等参数同时给出时明确报错，不再静默优先

#### v1.5.0 (2026-09-09)

**国内版 / 国际版双版本支持**

- **新增**：支持 WorkBuddy 国际版（数据目录 `~/.workbuddy-ai/`），通过 `--intl` 参数或交互式向导选择
- **改进**：交互式向导新增版本选择步骤，展示两个版本的路径区别
- **默认行为**：不加参数时自动探测数据目录（`~/.workbuddy-ai` 存在且非空判为国际版，否则国内版），`--intl` / `--dir` 可显式指定

#### v1.4.0 (2026-08-06)

**跨平台支持 + 当前账号识别修复**（感谢 [@yuren238](https://github.com/yuren238)，PR #1）

- **跨平台**：storage.json 路径自动适配 macOS / Windows / Linux，不再硬编码 macOS 路径
- **Bug 修复**：当前账号识别改为以 storage.json 为权威来源，DB 按 session 数最多做辅助验证。此前用"最新 session"推断，旧账号的最后一条 session 可能比当前账号更新，导致误把旧账号当成当前账号
- **新增**：`--target` 参数，可手动指定目标账号，无需切换登录
- **改进**：交互式向导改为手动选择目标/源账号，避免自动推断错误
- **Bug 修复**：Windows GBK 编码终端下 emoji 输出导致 UnicodeEncodeError 崩溃

#### v1.3.0 (2026-05-26)

**关键修复：账号切换后 storage.json 中 genie.userId 未同步，导致迁移被静默跳过**

- **Bug 修复**：`get_current_user_id()` 改为多源交叉验证——同时从 DB 最新 session 和 storage.json 读取 user_id，不一致时警告并优先使用 DB 值。此前仅依赖 `genie.userId`，账号切换后可能过时，导致 source=target 迁移被跳过。
- **Bug 修复**：`migrate_sessions()` 迁移后增加 WAL checkpoint + 验证源 user_id 归零。此前修改可能因 WAL 未落盘而在客户端重启后丢失。
- **文档更新**：SKILL.md 新增 AI 手动迁移最佳实践、3 条新踩坑记录。

#### v1.2.0 (2026-05-25)

- 新增历史任务恢复（`--list-tasks`、`--restore-tasks`）
- 新增交互式向导模式
- 新增 `--generate-commands` 生成 TaskCreate 命令

#### v1.1.0 (2026-05-25)

- 首次公开发布
- Session、Memory、Connector 迁移
- 自动备份 + 回滚

### License

[MIT](LICENSE) © 2026

---

<h2 id="english">English</h2>

### The Problem

After switching accounts in WorkBuddy (Tencent Cloud AI assistant desktop app), **all your previous conversation history, long-term memory, and MCP connector configs disappear from the UI**. The data is still on disk — just hidden by `user_id` isolation.

This tool merges old account data into your current account with a single command.

### Quick Start

```bash
git clone https://github.com/xiaoliuzhuan666/workbuddy-account-migrate.git
cd workbuddy-account-migrate
python3 scripts/migrate.py
```

Interactive wizard — pick your edition (domestic or international), then select accounts by number.

**Other modes:**

```bash
python3 scripts/migrate.py --diagnose              # Diagnose only
python3 scripts/migrate.py --source <USER_ID>      # Specify source account
python3 scripts/migrate.py --intl                  # International edition (~/.workbuddy-ai)
python3 scripts/migrate.py --intl --diagnose       # Diagnose international edition
python3 scripts/migrate.py --rollback <TAG>        # Rollback to backup
```

> **Domestic vs International**: the only difference is the data directory — domestic uses `~/.workbuddy/`, international uses `~/.workbuddy-ai/`. Directory resolution order: `--dir` > `--intl` > auto-detect (`~/.workbuddy-ai` non-empty means international).

### What Gets Migrated

| Data | How | Strategy |
|:---|:---|:---|
| Session history | SQLite `user_id` field | UPDATE to new account |
| Long-term Memory | `~/.workbuddy/memory/{uid}_memory.md` | Append + deduplicate |
| MCP Connectors | `~/.workbuddy/connectors/{uid}/mcp.json` | JSON deep merge |

Skills, Automations, Settings are global (no user_id) — no migration needed.

### Features

- 🧙 Interactive wizard (pick target & source accounts by number, no user_id needed)
- 🖥️ Cross-platform: storage.json path auto-detected for macOS / Windows / Linux (v1.4)
- 🌍 Domestic / International edition: interactive wizard prompts for edition, or use `--intl` for `~/.workbuddy-ai`
- 🔒 Safe: append-only memory, deep-merge connectors, WAL checkpoint before & after
- 🔍 Authoritative login detection: storage.json first, DB session-count as cross-check (v1.4)
- ✅ Post-migration verification (source user_id must be zero)
- 🪶 Zero dependencies (Python 3.8+ only)

### Compatibility

- ✅ WorkBuddy Domestic edition — macOS (tested), Windows / Linux (paths adapted in v1.4)
- ✅ WorkBuddy International edition — Windows (data dir `~/.workbuddy-ai/`, use `--intl`, v1.5, tested)
- ⚠️ WorkBuddy International edition — macOS / Linux (theoretically supported, untested)
- ❌ CodeBuddy CLI (not needed — it uses project-level isolation, not user-level)

> **Domestic vs International**: domestic edition stores data in `~/.workbuddy/`, international in `~/.workbuddy-ai/`. The migration tool auto-detects the directory, or you can pin it with `--intl` / `--dir`.

### Single-session cross-edition migration

To move **one conversation** between editions (domestic ⇄ international), use the second script:

```bash
python3 scripts/migrate_session.py                 # interactive wizard
python3 scripts/migrate_session.py --from domestic --to intl --session-id <ID>
```

- **Close both WorkBuddy clients first** — the script refuses to run otherwise (WAL not flushed + in-memory cache would overwrite your changes).
- Default is `move` (deletes the source after migrating); use `--mode copy` to keep it.
- If the target already has the conversation, you get a diff (last activity / message count / size / last prompt) and a choice: overwrite / skip / cancel.
- Migrates the session row, usage stats, workspace entry **and** the `projects/*.jsonl` transcript — without the transcript the conversation opens empty.
- Platform note: the full cross-edition flow is only tested on Windows (Win 11 + Python 3.13); on macOS, `--list` has been verified against a real domestic-edition fixture (2026-09-21). The script itself is cross-platform — issue reports welcome.

### Changelog

#### v1.6.1 (2026-09-21)

**Fixed: `migrate_session.py` behavior/docs mismatches & silent failures** (all changes by [@bukall](https://github.com/bukall), PR #5)

- `--mode copy` across accounts no longer degrades to "reassign `user_id`": copy always keeps the source and clones one into the target account
- Sessions with an empty `cwd` no longer silently write the transcript into the `projects/` root (the client looks it up at `projects/<slug>/<id>.jsonl` — the migration would "succeed" but the conversation would never open). Now a three-level fallback (row `cwd` → session profile `cwd` → source transcript's directory name), and it aborts with a rollback hint if still undeterminable
- `--list` size stats now recurse into directories (previously `tool-results/` was under-counted as ~4KB)
- Top-level `sqlite3.Error` handler added: when a cross-edition insert hits a new NOT NULL column without default in the target DB, you get an actionable "rollback with --rollback" message instead of a traceback
- Soft-conflict overwrite: the deleted target conversation's `session_usage` rows are now backed up and rolled back too
- Client process detection no longer treats a failed check as "client is closed" (explicit `--force` required); non-Windows platforms additionally match `ps -eo args=` against full command lines, avoiding Electron bundle-name misses
- Transcript id rewriting now only touches `"sessionId":"..."` field values — a naive full-line replace used to corrupt message text that happened to contain the same id string (logs, paths)

**Fixed: data-safety & parsing issues in `migrate.py`**

- DB backup now uses the sqlite backup API (includes WAL data) instead of `shutil.copy2` on the main DB file — the latter captured a stale snapshot when the client was still running; falls back to file copy with an explicit warning
- `PRAGMA wal_checkpoint`'s busy flag is now checked: no more claiming "verification passed" when the checkpoint didn't complete
- Structured memory migration compares against **all** existing `memoryBlock`s in the target — repeated runs no longer append the same block twice
- `_get_storage_json_path()` respects `WORKBUDDY_MIGRATE_HOME` / `--dir` and no longer reads the real machine's platform storage.json; `STORAGE_JSON` being `None` no longer leads to a bare `open()`
- `get_connector_info()` reads `mcp.json` as UTF-8 explicitly (Chinese configs were silently swallowed, showing 0 servers)
- user_id detection now uses UUID-shape matching instead of "directory name contains a hyphen"
- `--rollback` accepts `--yes` to skip confirmation; combining it with `--source` etc. now errors out explicitly instead of silently prioritizing rollback

#### v1.6.0 (2026-09-10)

**Single-session cross-edition migration (domestic ⇄ international)** (this section's work by [@bukall](https://github.com/bukall), PR #5)

- **New**: `scripts/migrate_session.py` — migrate one conversation between editions
- **New**: carries `session_usage`, `workspaces` and the `projects/*.jsonl` transcript along (DB row alone = empty conversation)
- **New**: conflict prompts — hard conflict (same ID) offers 2 choices, soft conflict (same title, different ID) offers 3, both with a side-by-side diff and an overwrite recommendation
- **New**: `move` by default, `copy` optional; per-session backup, rollback touches nothing else
- **Improved**: current account now resolved from `storage/skeleton/account-snapshot.json` inside the data dir (edition-aware, cross-platform)
- **Safety**: refuses to run while a WorkBuddy client is running
- **Tests**: new `tests/` with fixture builder + 86 end-to-end checks, all in a temp dir

**Fixed: backup crashed when the transcript included a `tool-results/` directory**

- Large tool outputs spill to `projects/{slug}/{id}/tool-results/*.txt` — a **directory** named after the session.
  `shutil.copy2()` on it raised `PermissionError: [Errno 13]` on Windows and aborted the whole migration.
  Files and directories now go through a shared `copy_path()` / `remove_path()`.
- Same root cause affected migration copy, `move` source deletion, rollback restore and soft-conflict cleanup — all fixed.
- Size reporting now recurses into directories (previously `tool-results/` counted as 0).

**Fixed: same-edition `copy` said "nothing to migrate" and did nothing**

- `_migrate_intra` only implemented the "reassign `user_id`" case; when the session already belonged to the current account there was nothing to reassign, so it bailed out.
- It now clones: new session id, title suffixed with 「（副本）」, transcript and task data copied.
- The clone rewrites every in-transcript `"sessionId"` and renames the transcript / `tool-results/` to the new id.
- Rollback handles `kind=session_clone` separately — it removes only the copy, never the original.
- A failed backup now cleans itself up instead of leaving a half-written directory.

**Changes to the existing `migrate.py`:**

- Current account detection now uses `storage/skeleton/account-snapshot.json` (edition-aware, cross-platform — by [@fhjowe](https://github.com/fhjowe), PR #6)
- Rollback safety: if `meta.json` is missing and `target_uid` is empty, Memory/Connectors restore is skipped — the path would otherwise degrade to the whole `connectors/` dir and `rmtree` **every account's config**
- Rollback completeness: Connectors/Memory are restored whenever the backup has them, even if the target no longer exists
- Memory migration creates `memory/` when missing instead of crashing

#### v1.5.0 (2026-09-09)

**Domestic / International edition support**

- **New**: support for WorkBuddy International edition (data directory `~/.workbuddy-ai/`) via `--intl` flag or interactive wizard selection
- **Improved**: interactive wizard now prompts for edition choice with path details
- **Default**: without any flag, the data directory is auto-detected (non-empty `~/.workbuddy-ai` wins); `--intl` / `--dir` pin it explicitly

#### v1.4.0 (2026-08-06)

**Cross-platform support + current-account detection fix** (thanks [@yuren238](https://github.com/yuren238), PR #1)

- **Cross-platform**: storage.json path auto-adapts to macOS / Windows / Linux (no more hardcoded macOS path)
- **Bug fix**: current account is now detected from storage.json as the authoritative source, with the DB's most-frequent user_id as a cross-check. Previously the "latest session" heuristic could misidentify a stale old account as the current one
- **New**: `--target` flag to explicitly set the target account without switching logins
- **Improved**: interactive wizard now asks for target and source accounts explicitly, avoiding auto-inference errors
- **Bug fix**: emoji output no longer crashes on Windows GBK/CP936 terminals (UnicodeEncodeError)

#### v1.3.0 (2026-05-26)

**Critical fix: Migration was silently skipped due to stale user_id**

- **Bug fix**: `get_current_user_id()` now uses multi-source cross-validation — reads from both DB latest session and `storage.json`, warns when inconsistent, prioritizes DB value. Previously relied solely on `genie.userId` which could be stale after account switch, causing `source == target` and migration being skipped.
- **Bug fix**: `migrate_sessions()` now performs WAL checkpoint after UPDATE (not just before), and verifies source user_id is zero. Previously, modifications could be lost on client restart due to unflushed WAL logs.
- **SKILL.md**: Added AI manual migration best practices, 3 new troubleshooting entries.
- **README**: Updated feature list, workflow description, and version badge.

#### v1.2.0 (2026-05-25)

- Added task history recovery (`--list-tasks`, `--restore-tasks`)
- Added interactive wizard mode
- Added `--generate-commands` for TaskCreate tool

#### v1.1.0 (2026-05-25)

- Initial public release
- Session, Memory, Connector migration
- Auto-backup + rollback support

### License

[MIT](LICENSE) © 2026
