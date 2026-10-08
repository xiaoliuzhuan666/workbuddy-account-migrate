# workbuddy-account-migrate

> WorkBuddy 数据搬家工具：**跨设备迁移**（家用电脑 ⇄ 办公电脑，两个账号也能把项目会话打包带走）+ **账号切换恢复**（切账号后对话记录、记忆、连接器一键找回）。
>
> Move your WorkBuddy data: **carry project sessions across computers** (home ⇄ office, two accounts) + **recover everything after switching accounts** (conversations, memory, connectors).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: macOS | Windows | Linux](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20%7C%20Linux-blue.svg)](https://github.com/xiaoliuzhuan666/workbuddy-account-migrate)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-green.svg)](https://www.python.org/)
[![Version 1.7.0](https://img.shields.io/badge/Version-1.7.0-brightgreen.svg)](https://github.com/xiaoliuzhuan666/workbuddy-account-migrate)

**[English](#english) | [中文](#chinese)**

---

<h2 id="chinese">中文</h2>

### ⭐ 核心功能

| | 功能 | 解决什么场景 |
|:--|:---|:---|
| ⭐ | **跨设备项目迁移**<br>`migrate_project.py`（v1.7 新增） | **家用电脑和办公电脑各一个账号，两地交替做同一个项目**——项目会话打包成单个文件带走，向导模式输序号 + 拖文件，三步完成 |
| 🔄 | **整账号迁移**<br>`migrate.py` | 切换账号 / 重新登录后，对话记录、长期记忆、MCP 连接器全部"消失"——一键合并恢复可见 |
| 💬 | **单对话跨版本迁移**<br>`migrate_session.py` | 只想把某一个对话从国内版搬到国际版（或反向），不动其他数据 |

**三个脚本都支持无参数运行进交互向导，全程不需要知道 user_id。**

跨设备迁移三步走（这是本工具的招牌场景）：

```
电脑A（账号A）                                电脑B（账号B）
┌─────────────────────┐                  ┌─────────────────────┐
│ 项目A · 77 个会话    │  ① 导出（选1）   │                     │
│ 正文/工具结果/任务/  │ ────────────────→│  包出现在桌面        │
│ 工作区记忆           │  包自动放桌面     │  ③ 导入（选2）       │
└─────────────────────┘                  │  选包 → 拖入项目路径  │
        │                                └─────────────────────┘
        │ ② 传输：AirDrop / U盘 / scp / 网盘           │
        └─────────────────────────────────────────────→
                                           自动改写账号 + 项目路径
                                           会话全部归来 ✅
```

- 🔁 **同一个包反复导入不出双份**——"两地交替工作"来回带的底气
- 🧳 工作区记忆（`{项目}/.workbuddy/`）一起带走
- 💻 macOS ⇄ Windows 路径风格自动重映射（盘符、分隔符、中文路径）
- 🛡️ 导入前自动备份，`--rollback` 一键还原

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
| ✅ 登录态权威识别 | v1.6.3：以 `account-snapshot.json` 的 `primary.uid`（客户端真实登录态，**左侧面板按它过滤**）为权威，`storage.json` 降为兜底；两来源不一致时强烈提示显式传 `--target` |
| ✅ 迁移结果验证 | v1.3：UPDATE 后验证源 user_id 归零，确认迁移成功 |
| ✅ 云端通道映射重置 | v1.6.3：清掉旧账号的 `edge-sync` 映射行，对话按新账号通道重新上传（回滚自动还原）；`--keep-cloud-mapping` 可关闭 |
| 🧰 强制重登辅助脚本 | `scripts/force-relogin.sh`：客户端反复自动登录到错账号时逼它弹登录界面（附带实测局限说明） |
| ✅ 零依赖 | 仅需 Python 3.8+，无第三方包 |

### 快速开始

```bash
git clone https://github.com/xiaoliuzhuan666/workbuddy-account-migrate.git
cd workbuddy-account-migrate
python3 scripts/migrate.py
```

> 受限 shell / 沙箱环境里 `git clone` 可能报「目标路径已存在」，改用 tarball：
> ```bash
> mkdir -p workbuddy-account-migrate && cd workbuddy-account-migrate
> curl -sSL https://codeload.github.com/xiaoliuzhuan666/workbuddy-account-migrate/tar.gz/refs/heads/main \
>   | tar xz --strip-components=1
> ```

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

# 进程检测"查不出来"时按「客户端已关闭」谨慎继续
#   与 --force 的区别：真检测到客户端仍在运行时，它照样拦截
python3 scripts/migrate.py --assume-clients-closed

# 回滚到指定备份
python3 scripts/migrate.py --rollback <TAG>

# 迁移完成后自动重启 WorkBuddy 客户端（macOS），会话列表立即刷新
python3 scripts/migrate.py --source <USER_ID> --yes --restart
```

> **国内版 vs 国际版**：目录结构完全一致，区别在于**数据目录位置**与**登录态来源**：
>
> | | 国内版 | 国际版 |
> |:---|:---|:---|
> | 数据目录 | `~/.workbuddy/` | `~/.workbuddy-ai/` |
> | 登录态权威来源 | 目录内 `storage/skeleton/account-snapshot.json` → `primary.uid`（平台 `storage.json` → `genie.userId` 仅兜底） | 目录内 `storage/skeleton/account-snapshot.json` → `primary.uid`（**不读**平台 `storage.json`） |
>
> 因此同一台机器上装了两个版本时，脚本会按**数据目录**决定读哪份登录态：
> 指向国际版目录时不会去读国内版的 `storage.json`（否则会把国际版数据迁到一个国际版里不存在的账号下）。
> **目录优先级**：`--dir` > `--intl` > 自动探测（`~/.workbuddy-ai` 存在且非空时判为国际版，否则国内版）。交互式向导还会让你确认一次版本。

### 备份目录布局

> ⚠️ v1.6 起**单对话迁移**的备份挪到了 `migrate_backups/session/` 子目录，
> 与 `scripts/migrate.py` 的整账号备份（直接放 `migrate_backups/` 根下）分开——
> 两者的 `meta.json` 格式不同，混在一个目录里会让 `--backups` 列出、
> `--rollback` 撞上 `KeyError`。
>
> | 路径 | 内容 | 回滚命令 |
> |:---|:---|:---|
> | `~/.workbuddy/migrate_backups/<TAG>/` | 整账号备份 | `scripts/migrate.py --rollback <TAG>` |
> | `~/.workbuddy/migrate_backups/session/<TAG>/` | 单对话备份 | `scripts/migrate_session.py --rollback <TAG>` |
>
> **旧位置的单对话备份仍然能被 `--backups` / `--rollback` 找到**（两个目录都会扫），
> 但如果外部脚本里硬编码了备份路径，请注意这次布局变化。
>
> `--backup-dir`（**只有 `migrate_session.py` 有这个参数**，`migrate.py` 没有）
> 的语义是「**额外**加入一个搜索根」而不是「限定只搜它」：
> 新建的备份写进 `<指定目录>/session/`（同样套一层命名空间，避免你把自定义目录
> 指向 `migrate_backups` 时和整账号备份挤在一起），查找 / 回滚时仍会回退扫描
> 上面两个标准目录——命中的备份不在你指定的目录里时，脚本会显式告警，
> 避免误滚了不相干的旧备份。

### 退出码

下表三个脚本共用，但 **`3` 只有 `migrate.py` 会产生**（整账号迁移才有"源账号空"这个概念；
另两个脚本遇到源/目标版本目录没数据时是 `1`）。

| 退出码 | 含义 |
|:---:|:---|
| `0` | 确实迁移 / 回滚了东西 |
| `1` | 出错（含：`migrate_session.py` 的源/目标版本目录里根本没有库 = 版本选错） |
| `2` | 「客户端必须关闭」检测拦截（检测到客户端仍在运行，或检测不可信且未给 `--force` / `--assume-clients-closed`） |
| `3` | **仅 `migrate.py`**：无数据可迁，本次未做任何改动。涵盖两种情况——源账号确实没有 session / memory / connector；或源有数据但与目标完全一致、合并后没有任何新增内容 |

> ⚠️ 自动化脚本不要把「退出码 0」当成「一定迁了东西」，也不要把「3」当成失败：
> 它表示本次没有产生任何改动。判读建议：`0` → 成功，`3` → 跳过，其他 → 失败。
>
> ⚠️ **破坏性变更**：v1.6.1 之前，源账号没数据时 `migrate.py` 返回 `0`。
> 如果你在 CI / 自动化里按 `rc == 0` 判定成功，升级后会看到原本"成功"的场合变成 `3`
> ——这不是新出现的失败，只是以前把"跳过"报成了"成功"。

### 迁移内容

| 数据类型 | 存储位置 | 隔离方式 | 是否迁移 | 迁移策略 |
|:---|:---|:---|:---:|:---|
| Session 对话记录 | `workbuddy.db` sessions 表 | `user_id` 字段 | ✅ | UPDATE user_id |
| 长期记忆 Memory | `~/.workbuddy/memory/{uid}_memory.md` | 按文件名 | ✅ | 追加去重合并 |
| Connector 连接器配置 | `~/.workbuddy/connectors/{uid}/mcp.json` | 按子目录 | ✅ | JSON 深度合并 |
| **云端通道映射** | `edge-sync-mapping*.db` 的 `msg_channel` | 按 `convmsg:{uid}` 记账 | ✅ **v1.6.3 新增** | 删除旧账号的映射行，让 EdgeSync 按新账号通道重传（回滚时从备份还原） |
| todos / tasks | `todos/{sessionId}.json`、`tasks/{sessionId}/` | 无隔离（按 sessionId） | ❌ | 不用迁；面板看不到任务是客户端只读当前 session 内存导致的 |
| Skills 技能 | `~/.workbuddy/skills/` | 无隔离 | ❌ | 全局共享，无需迁移 |
| Automations 定时任务 | `workbuddy.db` automations 表 | 无 user_id | ❌ | 全局共享，无需迁移 |
| Settings / MCP / Plugins | 全局配置文件 | 无隔离 | ❌ | 全局共享，无需迁移 |
| inspiration | `~/.workbuddy/inspiration/{uid}/` | 按 uid 子目录 | ❌ | 需要时手动 `mv` 到目标 uid 目录 |
| security | `~/.workbuddy/security/{uid}/` | 按 uid 子目录 | ❌ | 安全检测模块的加密库，**不要动** |
| storage/user-\<uid\>* | `~/.workbuddy/storage/` | 按 uid 子目录 | ❌ | 客户端 UI 偏好等，未迁移 |

> **为什么要管「云端通道映射」**：本地 `user_id` 改对只是让**本机**看得到；对话在云端仍挂在旧账号的 `convmsg:{旧uid}` 通道下，EdgeSync 会认为"已同步过"而不重传 —— 结果换台设备登录新账号时看不到这些历史。v1.6.3 起迁移会自动清掉旧账号的映射行（删前整库备份），回滚时自动还原。

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
| 默认语义 | **归属转移**（`UPDATE sessions.user_id`，源账号不再看到这些对话；记忆/连接器是合并） | **移动（源删除）**，可 `--mode copy` |

> ⚠️ `migrate.py` 的"合并"只针对 Memory / Connectors：**对话是改 `user_id` 转移归属**，
> 迁移后源账号下就看不到它们了（数据没被删除，只是归属变了）。想两边都保留请改用
> `scripts/migrate_session.py --mode copy`。
>
> ⚠️ `migrate.py` 只在**一个数据目录内**工作（`--intl` 只是把目标目录切到 `~/.workbuddy-ai`，
> 不是"跨版本搬数据"）。它**不搬** `projects/{slug}/*.jsonl` 对话正文与 `tasks/`：
> 同一数据目录内这些内容本来就按 session 共享、不需要搬；但如果你想把这些对话搬到
> **另一个版本**的数据目录，必须用 `scripts/migrate_session.py`——否则目标版本里
> 只会多出一条 session 行，正文为空（打开是空对话）。
>
> **`--mode copy` 的语义**（migrate_session.py）：无论源对话属于哪个账号，copy 都会**保留源**，
> 在目标账号下克隆出一份新对话。此前跨账号 + copy 走的是改 `user_id`（归属转移），
> 源账号会丢失该对话，与"保留源"矛盾，已修正。

**⚠️ 迁移与回滚前都必须关闭两个版本的 WorkBuddy 窗口**，脚本会检测并拒绝执行（`--rollback` 同样检测——回滚也要改库 + 删文件，客户端在跑时内存缓存会把回滚结果覆盖回去）。原因：数据还在 WAL 里没落盘、客户端内存缓存会覆盖你的写入。

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
| `--list` | 列出源版本的对话（配合 `--query` 过滤） | - |
| `--query` | 按标题 / 工作目录 / id 过滤列表 | - |
| `--session-id` | 对话 id（支持前缀） | - |
| `--mode` | `move`（迁移后删源）/ `copy`（保留源并克隆一份到目标账号） | **`move`** |
| `--on-conflict` | `ask`/`skip`/`overwrite`/`newer`（无终端询问时 `ask` 降级为 `skip`） | `ask` |
| `--target-uid` | 手动指定目标版本的 user_id（不传则从 account-snapshot 推断）。**形态不像 UUID 时会先警告**（拼错会把对话挂到不存在的账号下，表现同样是"迁移成功但对话消失"）；非交互模式下还需再确认一次 | - |
| `--dry-run` | 只打印计划不写盘（不会再弹冲突询问） | 关 |
| `--yes` | 非交互模式：跳过「确认执行 / 确认回滚」这类询问。**冲突处理不受它控制**——由 `--on-conflict` 决定，`ask` 在无终端时降级为 `skip` | 关 |
| `--force` | 跳过"客户端必须关闭"检测 | 关 |
| `--assume-clients-closed` | 比 `--force` 温和：只在**进程检测本身失败**（查不出结果）时按「客户端已退出」继续；**真检测到客户端仍在运行时照样拦下** | 关 |
| `--backups` / `--rollback TAG` | 查看备份 / 回滚（`--rollback` 支持标签前缀） | - |
| `--full` | 与 `--rollback` 配合：整库恢复（库 + 正文 + 任务）。**必须**与 `--rollback` 同用，单独给会报错退出 | 关 |
| `--backup-dir` | 备份目录（**仅本脚本有**；`migrate.py` 无此参数）：新备份写进 `<该目录>/session/`；查找/回滚时作为**额外**搜索根，仍会回退扫描两个版本的标准目录 | - |

> 未给 `--session-id` 时进入交互式向导；此时 `--mode` / `--yes` / `--dry-run` / `--target-uid` / `--backup-dir` / `--assume-clients-closed` / `--on-conflict` / `--from` / `--to` / `--query` 同样会透传（此前在向导路径被静默丢弃）。

回滚精确到单条，不影响其他对话：`python3 scripts/migrate_session.py --rollback <TAG>`。

> ⚠️ **平台说明**：单对话迁移的**完整跨版本链路仅 Windows 实测通过**（Windows 11 + Python 3.13）。
> macOS 已部分验证（2026-09-21）：`--list` 在国内版真实数据 fixture 上工作正常（含中文名渲染、
> 大小统计），脚本本身是跨平台的（路径走 pathlib、进程检测 Windows 用 `tasklist`、其他平台用
> `ps`），但跨版本迁移全流程在 macOS / Linux 未经实测，欢迎提 Issue 反馈。

### 跨设备项目迁移（v1.7.0）

前面两个脚本都作用于**同一台机器**。如果你是「家用电脑 + 办公电脑，两个账号，两地交替做同一个项目」（issue #8），用第三个脚本——**项目打包导出 / 导入**。

**小白路线（推荐）：无参数进向导，全程输序号 + 拖文件**

```bash
python3 scripts/migrate_project.py
```

```
  你现在在哪台电脑上？
    1. 要离开这台电脑 —— 把项目打包带走（导出）
    2. 到了新电脑 —— 把迁移包导入进来（导入）
```

- 选 1：列出这台电脑的所有项目 → 输序号 → 包自动放到**桌面**，拷去 U 盘 / AirDrop 即可
- 选 2：自动发现**桌面 / 下载**里的迁移包 → 输序号 → 把项目文件夹**拖进窗口**回车 → 确认导入
- 导入前自动备份，出问题 `--rollback` 一键还原；账号、路径重映射全自动

**高级用法（可跳过）**：

```bash
python3 scripts/migrate_project.py export --cwd /path/项目A              # 指定项目导出
python3 scripts/migrate_project.py export --cwd /path/项目A --out /tmp/项目A.wbproj   # 指定导出路径（默认落桌面）
python3 scripts/migrate_project.py info 项目A.wbproj                     # 查看包内容
python3 scripts/migrate_project.py import 项目A.wbproj --cwd /新路径 --dry-run
python3 scripts/migrate_project.py import 项目A.wbproj --cwd /新路径 --on-conflict overwrite
python3 scripts/migrate_project.py --backups                             # 查看备份
python3 scripts/migrate_project.py --rollback <TAG>                      # 回滚一次导入
```

**设计边界**：工具只做「打包」和「解包」两件纯本地的事。包怎么传（AirDrop / U 盘 / scp / 网盘）、项目在目标机放哪、用哪个账号登录——全部由你决定。导入时自动把 `user_id` 改写为**目标机登录账号**、`cwd` 与正文内嵌路径改写为新路径，无需手动处理。

**包内容**（一个项目的完整会话数据，缺一样客户端就显示异常）：

| 数据 | 说明 |
|:---|:---|
| sessions + session_usage 表行 | `user_id` / `cwd` 在导入侧改写 |
| `projects/{slug}/` 正文 | `.jsonl` + `.meta.json` + `.file-rollback.ndjson` |
| `tool-results/` 目录 | 大工具输出外溢处，整目录打包 |
| `todos/` + `tasks/` | 任务数据 |
| `{项目}/.workbuddy/` 工作区记忆 | 默认包含，`--no-workspace-memory` 排除 |
| 账号级 memory / connectors | ❌ **不在范围**（跨设备项目迁移不含账号数据合并） |

**冲突语义**（"两地交替"场景的命根子）：

| 冲突 | 行为 |
|:---|:---|
| 同 id（重复导入同一个包） | **覆盖**——反复导入不会产生双份对话 |
| 同标题不同 id | 默认跳过并警告；交互模式询问 |
| 非交互终端（无 TTY） | 一律降级跳过，需显式 `--on-conflict overwrite` 批量覆盖 |

回滚：导入自动备份，`--backups` 查看、`--rollback <TAG>` 整体还原（数据库快照 + 文件）。工作区记忆合并是追加式文本，不参与回滚。

> ⚠️ **平台说明**：本脚本逻辑复用 v1.6 系列已验证机制（列名对齐、目录/文件统一复制、WAL checkpoint），并附 37 项合成 fixture 测试（两台"虚拟机器"全链路）。但**跨设备全流程尚未在两台真机间实测**，首次使用建议先 `--dry-run`，欢迎提 Issue 反馈。

### 工作原理

**Step 1：自动诊断** — 从数据库、Memory 文件、Connector 目录三个来源自动发现所有账号。当前登录账号的判定顺序（v1.6.3）：**① `{数据目录}/storage/skeleton/account-snapshot.json` 的 `primary.uid`** → ② `storage.json` 的 `genie.userId` → ③ DB 中 session 数最多的 user_id。三者与 daemon 日志里的面板 uid 会一起打印出来，不一致时明确告警。
> v1.4~v1.6.2 曾以 `storage.json` 为唯一权威，但国内版实测它与客户端真实登录态**可能长期是两个不同的 uid**，导致迁移方向每次判错、迁完左侧列表仍然空白（2026-09-22 实例）。**判定口径已改为「客户端登录态优先」**；也别用"最新 session"推断——旧账号切换前的最后一条 session 可能比当前账号更新。

**Step 2：安全备份** — 迁移前自动备份到 `~/.workbuddy/migrate_backups/{timestamp}_{uid前8位}/`（同一秒重复迁移会自动加序号，不会覆盖前一份备份）

**Step 3：执行迁移** — Session 用 `UPDATE user_id`，Memory 按 **`memoryBlock` 语义块**去重后追加（结构化 Memory；无 `RAW_JSON` 块的旧格式才退回按行去重），Connector JSON 深度合并

**Step 4：持久化 + 验证** — 迁移后执行 WAL checkpoint 确保数据落盘，验证源 user_id 归零确认迁移成功

**Step 5：重启提示** — 提示重启 WorkBuddy 客户端，UI 刷新缓存后数据可见

### 兼容性

| 平台 | 状态 |
|:---|:---|
| WorkBuddy 国内版 (Windows) | ✅ 已测试（Windows 11 + Python 3.13，数据目录 `~/.workbuddy/`） |
| WorkBuddy 国际版 (Windows) | ✅ 已测试（v1.5，数据目录 `~/.workbuddy-ai/`，使用 `--intl` 参数） |
| WorkBuddy 国内版 (macOS) | ⚠️ 理论支持，**未实测**（`~/Library/Application Support/...` 路径逻辑沿用跨平台实现） |
| WorkBuddy 国内版 (Linux) | ⚠️ 理论支持，**未实测**（`XDG_CONFIG_HOME` 路径） |
| WorkBuddy 国际版 (macOS / Linux) | ⚠️ 理论支持，**未实测** |
| CodeBuddy CLI | ❌ 不适用（见下方说明） |

> ⚠️ 本项目**目前仅 Windows 实测通过**（Windows 11 + Python 3.13）。上面标"未实测"的平台
> 走的是同一套 pathlib 路径推导与进程检测（Windows 用 `tasklist`、其他平台用 `ps`），
> 但没有任何实测记录，欢迎提 Issue 反馈结果。

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
# 整账号备份（migrate.py）
ls ~/.workbuddy/migrate_backups/          # 国内版（国际版在 ~/.workbuddy-ai/migrate_backups/）
python3 scripts/migrate.py --rollback 20260525170000_abc12345

# 单对话备份（migrate_session.py，v1.6 起放在 migrate_backups/session/）
python3 scripts/migrate_session.py --backups
python3 scripts/migrate_session.py --rollback 20260922000000_domestic2intl_12345678

# 整库恢复（migrate_session.py）：把迁移波及到的库整体还原到迁移那一刻
python3 scripts/migrate_session.py --rollback 20260922000000_domestic2intl_12345678 --full
```

> ℹ️ **`--full` 的还原范围**：整库恢复会把迁移**波及到**的数据库整体还原到迁移那一刻
> （库 + 正文 + 任务），所以迁移之后新增的对话与消息会一并丢失，确认前脚本会明确提示。
> `copy` 模式（`--mode copy`）下源版本是用户特意保留的、迁移本身没改动过它，
> `--full` 也**不会**还原源侧（打印 ⏭️ 说明跳过），只还原目标版本。

> ⚠️ **备份目录含个人数据，用完请及时清理**。备份里可能有：
> `workbuddy.db`（整库快照）、`mcp.json` / `connector-states.json`（可能含 token）、
> `.master.key`、`{uid}_memory.md`、以及对话正文 `*.jsonl` 与 `tool-results/`。
> 确认不再需要回滚后，建议**移入回收站**而不是直接删除，例如：
>
> ```bash
> # 先看清有哪些（两个版本各自一个目录）
> ls -la ~/.workbuddy/migrate_backups/ ~/.workbuddy-ai/migrate_backups/
> # macOS：移入废纸篓
> trash ~/.workbuddy/migrate_backups/20260525170000_abc12345
> # Linux：移入回收站
> gio trash ~/.workbuddy/migrate_backups/20260525170000_abc12345
> # Windows PowerShell：移入回收站
> #   Add-Type -AssemblyName Microsoft.VisualBasic
> #   [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
> #     "$env:USERPROFILE\.workbuddy\migrate_backups\20260525170000_abc12345",
> #     'OnlyErrorDialogs', 'SendToRecycleBin')
> ```
>
> 迁移脚本**不会**自动清理旧备份 —— 什么时候不再需要回滚由你自己判断。

### 竞品对比

| 项目 | 定位 | 同平台账号切换 | Session 迁移 | Memory 迁移 |
|:---|:---|:---:|:---:|:---:|
| **本项目** | 同平台账号切换数据合并 | ✅ | ✅ | ✅ |
| [ai-memory-sync](https://github.com/supercrzy/ai-memory-sync) | 跨设备记忆同步 | ❌ | ❌ | ✅ |
| [claw-migrate](https://github.com/citriac/claw-migrate) | 跨平台记忆迁移 | ❌ | ❌ | ✅ |
| [workbuddy-manager](https://github.com/starsss0416/workbuddy-manager) | 本地会话管理 | ❌ | ✅ 本地 | ❌ |

**本项目填补的空白**：跨平台迁移和跨设备同步都有人做了，但**同平台账号切换后的数据合并**是唯一没人覆盖的场景。

### FAQ

**Q: WorkBuddy 两台电脑怎么同步对话记录？家用和办公电脑各一个账号怎么办？**

A: 用**跨设备项目迁移**（v1.7 核心功能）：在电脑A上运行 `python3 scripts/migrate_project.py` 选「打包带走」，把桌面上的 `.wbproj` 包传到电脑B（AirDrop / U盘 / 网盘均可），再运行一次选「导入进来」。账号与项目路径自动重映射；同一个包反复导入不会产生重复对话——两地交替工作就靠这套流程来回带。

**Q: WorkBuddy 换新电脑了，项目的历史会话能带走吗？**

A: 能。会话记录不在代码仓库里，而在本机数据目录（`~/.workbuddy/`）。用 `migrate_project.py export` 把指定项目的全部会话（对话正文、工具结果、任务列表、工作区记忆）打包成一个文件，到新电脑上 `import` 即可，代码本身照旧走 git。

**Q: WorkBuddy 切换账号后对话记录 / 历史记录真的没丢吗？**

A: 没丢。数据文件全部还在磁盘上，只是 UI 按 `user_id` 过滤导致看不到。本工具把这些数据合并到当前账号下即可恢复可见。

**Q: 迁移后旧账号数据还在吗？**

A: Session 的 `user_id` 被改为新账号，所以在旧账号的 UI 下不可见了。Memory 和 Connector 的源文件仍然保留，可手动清理。

**Q: 支持双向迁移吗？**

A: 支持。从 B 迁到 A 后，可以登录 B 再执行 `--source <A的user_id>`，或者用 `--target` 直接指定目标账号、无需切换登录。Memory 按语义块（`memoryBlock`）去重、Connector 按 key 合并，反向迁移不会产生重复内容。注意：反向迁移会把 A 名下**所有** session 一起迁走（包括 A 原有的）；如果只是想撤销上一次迁移，用 `--rollback` 回滚更干净。

**Q: 支持 CodeBuddy CLI 吗？**

A: 暂不支持。CodeBuddy CLI 不存在账号切换数据丢失的问题。详见上方「兼容性」章节。

**Q: macOS / Linux 可以用吗？**

A: 路径层面支持。v1.4 起 storage.json 路径已按平台自动适配（macOS `~/Library/Application Support/...`、Windows `%APPDATA%`、Linux `XDG_CONFIG_HOME`），v1.6.1 起登录态改以数据目录内的 `account-snapshot.json` 为准，不再依赖平台 `storage.json` 的具体路径。

**但只有 Windows 真正实测过**（Windows 11 + Python 3.13），macOS / Linux 属于"代码跨平台、没在本体上跑过"。进程检测在 Windows 用 `tasklist`、其他平台用 `ps`，后者完全没有实测覆盖。详见「兼容性」章节。

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
├── TOPICS.md                              # 专题索引（给 Skill / AI 检索用）
├── scripts/
│   ├── migrate.py                         # 整账号迁移（同版本内）
│   ├── migrate_session.py                 # 单对话迁移（支持跨版本，v1.6）
│   └── migrate_project.py                 # 跨设备项目导出/导入（v1.7）
├── tests/
│   ├── prepare_fixture.py                 # 构造临时测试 fixture（只读复制真实数据）
│   ├── run_tests.py                       # 端到端 + 单元级测试（用例数随 fixture 内容浮动，以运行末尾输出为准）
│   └── run_project_tests.py               # 跨设备项目迁移合成测试（37 项，不依赖真实数据）
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
| 软冲突多条同名 | 目标里有多条同标题对话时，脚本**只处理其中一条**（会打印其余的 id 与标题提醒），剩下的需要在客户端里手动清理 |
| 会话 `cwd` 为空 | 极少数会话记录里 `cwd` 为空，正文目录只能靠源侧目录名回退；若连这也取不到，脚本会中止而不是静默放错位置 |
| 正文 id 改写范围 | 只改写 `"sessionId":"..."` 字段值。消息正文里引用到的旧 id（日志、路径）保持原样——那是用户可见内容，不应被改 |

### 更新日志

#### v1.7.0 (2026-10-06)

**新增：`scripts/migrate_project.py` — 跨设备项目导出/导入**（响应 issue #8）

- **场景**：家用电脑（账号A）+ 办公电脑（账号B），两地交替做同一个项目，项目会话数据随人走
- **小白向导**：无参数运行即进向导（源机选「打包带走」/ 目标机选「导入进来」），全程输序号 + 拖文件；导出包默认放**桌面**，导入自动发现**桌面/下载**里的包；交互导入写入前有确认步骤（小白的 dry-run 替代品）
- **export**：按 `cwd` 列出项目 → 打包为 `.wbproj`（sessions/usage/正文/tool-results/todos/tasks/工作区记忆 + manifest）。**只读操作**，客户端开着也能跑
- **import**：探测目标机登录 uid（account-snapshot 权威）→ 指定新路径（强制校验存在）→ 重映射写入（`user_id`/`cwd`/slug 重推导/jsonl 顶层 cwd 字段流式改写）
- **冲突语义**：同 id 覆盖（反复导入不出双份，"两地交替"靠它）；同标题不同 id 默认跳过；无 TTY 降级跳过，`--on-conflict overwrite` 批量覆盖
- **回滚**：导入前 sqlite backup API 整库快照 + 覆盖文件 stash，`--rollback` 一键还原（还原前清理残留 `-wal/-shm`，防陈旧 WAL 重放）
- **测试**：`tests/run_project_tests.py` 37 项合成 fixture 测试——两台"虚拟机器"（不同 uid / 不同路径风格 / 不同表列集合）走 export→传输→import→重复导入→回滚全链路，不依赖真实数据
- **明确不做**：网络传输、账号级 memory/connectors 合并、自动双向同步（传输交给用户，二期再议）

#### v1.6.3 (2026-09-22)

**修复：目标账号会判成"另一个账号"，迁移"成功"但左侧列表依旧空白**

- **判定口径翻转**：`get_current_user_id()` 改为 **`account-snapshot.json` → `primary.uid` 优先**，`storage.json` 的 `genie.userId` 降为第二优先级、DB 会话数兜底。原因见下
- **踩到的坑**：国内版 `storage.json` 记的账号（扩展侧）与客户端真实登录态可以是两个不同 uid，且**长期不一致**。工具当时按 `storage.json` 选目标，每次都把数据并到「面板看不到」的那个账号，用户重启后依然是空列表，来回试了 6 次
- **diagnose 重排**：并列打印四个信号 —— 客户端登录态(account-snapshot) / 扩展侧记录(storage.json) / daemon 最近一次 `listSessions` 的 uid / DB 各账号会话数；不一致时直接给出「迁移务必带 `--target`」的结论
- **migrate 新增 Phase 4.5 一致性检查**：目标账号 ≠ 客户端登录态时，明确列出两条补救路径（切账号看 / 回滚后加 `--target` 重跑），不再只提示"迁移完成"
- **迁移前告警**：打印目标账号时同时打印客户端登录态，`--target` 与登录态不符会先警告（`--target` 是手动指定时提示"确认有意为之"）
- **备份 meta 补字段**：`meta.json` 除 `target_uid` 外新增 `source_uid`、`client_login_uid`、`storage_json_uid`、`session_counts` —— 事后复盘"当时到底登在哪个账号"全靠它
- **建议命令带 `--target`**：`--diagnose` 的迁移建议按数据量排序，并直接输出含 `--target` 的完整命令（0 数据的账号不再生成命令）
- **修 daemon 日志解析**：嵌套 JSON 的引号是转义的（`\"userId\":\"...\"`），原正则匹配不到面板 uid

**（沿用 v1.6.2）沙箱 shim 与 `--restart`**：脚本启动自动剥离 `PYTHONPATH`；`--restart` 迁移后自动重启客户端。

**新增：云端通道映射重置（`edge-sync-mapping*.db`）**

- 本地 `user_id` 改对只让**本机**看得到；对话在云端仍挂在 `convmsg:{旧uid}` 通道下，EdgeSync 认为"已同步过"不会重传 → **换台设备登录新账号看不到这些历史**
- 迁移时自动删除旧账号的映射行（只删映射、不碰对话内容，删前整库备份到 `<备份>/edge-sync/`），下次启动客户端由 EdgeSync 重新上传
- `--rollback` 会一并还原映射库；`--keep-cloud-mapping` 可跳过本步骤

**新增：`scripts/force-relogin.sh`（强制重登辅助）**

- 客户端反复自动登录到错账号时，移走 `storage/skeleton/account-snapshot.json` 逼它弹登录；`--restore` 还原，默认检测客户端是否已退出
- 脚本头部如实写明实测局限：**能拿到正确账号，但挡不住约 1 分钟后的自动回切**（回切源头在加密凭据层），因此长期方案是把数据并到客户端实际登录的账号

**交互向导标记目标账号**

- 账号列表给「客户端登录态」那一行加 `← 客户端登录态（面板按它过滤）` 标记，并提示**目标通常就选它**，避免人工选错方向
- 迁移前打印目标账号时同时打印客户端登录态；两者不符先告警

**测试与文档**

- `tests/run_tests.py`：本机缺国际版数据时显式跳过（退出码 0），不再抛 `sqlite3 "unable to open database file"` traceback
- README/SKILL 补「迁移边界」清单（todos / inspiration / security / storage/user-* 为何不迁）、「登录态有两个来源」章节、tarball 安装方式

#### v1.6.2 (2026-09-22)

**修复：WorkBuddy 会话内运行被沙箱 shim 劫持导致迁移崩溃**

- WorkBuddy 会话的 Bash 里运行时，注入的 `PYTHONPATH` 指向沙箱 shim（sitecustomize.py）会劫持 `Path.mkdir`：即使传 `exist_ok=True`，目录已存在也抛 `PermissionError EEXIST`，迁移在备份阶段就崩溃（托管 Python 和系统 Python 都中招）
- 现在脚本启动时自动剥离 `PYTHONPATH` 并 re-exec 自身（等价于 `env -u PYTHONPATH python3 migrate.py ...`，但无需记住特殊用法）；所有 `mkdir` 处保留 `exists()` 先判断作为双保险

**新增：`--restart` 迁移完成后自动重启客户端**

- `python3 migrate.py --source <UID> --yes --restart`：迁移/回滚完成后延迟数秒自动退出并重新拉起 WorkBuddy（macOS），左侧会话列表立即刷新，不用手动重启
- 采用后台延迟执行（脱离进程组），脚本先输出完整结果再触发重启；在 WorkBuddy 会话内调用时当前 AI 会话会中断，属预期行为。Windows / Linux 提示手动重启

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

**修复：高危数据安全问题**

- 备份 `meta.json` 改为**增量落盘**：每个破坏性步骤（覆盖删旧对话、复制正文、move 删源行、
  删源文件）之后立即写盘。以前只在最后写一次，中途失败会让磁盘上的 meta 缺
  `override_deleted` / `source_deleted` / `copied_to` → 精确回滚静默漏项
- 跨版本迁移的 `projects/<slug>/` 目录名改为**直接沿用源侧真实目录名**（那是客户端按 cwd
  实际建出来的），`cwd_to_slug()` 只作兜底。自己推的规则一旦与客户端不一致，
  正文会落到客户端不扫描的目录 → "迁移成功却打不开"
- 软冲突覆盖：先把数据库行 commit 成功，**再**删目标文件。以前反着做，commit 失败会留下
  "行还在、正文没了"的不一致
- 回滚 / `--full` 整库恢复时会一并清掉 `workbuddy.db-wal` / `-shm`：只覆盖主库的话，
  SQLite 下次打开会把与新主库不匹配的旧 WAL 重放上去，回滚可能无效甚至数据错乱。
  `migrate.py --rollback` 也补上了"整库覆盖会抹掉迁移后新增数据"的警告

**修复：`--intl` 与 Connector 合并的静默失效**

- `--intl` 时不再读平台 `storage.json`：它是国内版登录态文件，机器上同时装两个版本时会把
  国内版 uid 当成国际版当前账号，导致国际版数据被迁到一个不存在的账号下（表现为对话全部消失）。
  国际版一律走 `account-snapshot.json`
- `migrate_connectors()` 的"深度合并"以前只看顶层 key：`mcp.json` 顶层只有 `mcpServers`，
  目标一旦已有该 key 就被判"无新增"整体跳过，一个 server 都合并不进去。
  现在先 `deep_merge_dict` 再比较合并前后差异

**修复：其他一致性与健壮性**

- 进程检测排除脚本自身（仓库目录名含 `workbuddy`，脚本命令行会命中关键字，
  否则用户被"检测到客户端正在运行"无条件拦住，只能加 `--force` 关掉整项检查）
- `snapshot_db()` 改为只读打开源库 + 异常时关闭连接（与 `_backup_db()` 实现统一）
- `mkdir` 补 `parents=True`（备份目录 / 目标 connectors 目录 / tasks 目录）
- memory 文件名也做 UUID 校验（之前只修了 connectors 目录）
- 前缀匹配 `id LIKE ?` 加 `ESCAPE`：用户粘贴的 id 含 `%` / `_` 不再被当通配符
- `_wal_checkpoint()`：非 WAL 库返回的 `-1` 不再被当成"有进程占锁"
- `--target` 不带 `--source`、`--generate-commands` 不带 `--restore-tasks` 时明确报错，
  不再静默忽略；`--target` 非 UUID 形态时告警
- Phase 4 验证顺带校验 Memory / Connector，并对"静默跳过"给出警示
- 删除死代码 `_extract_memory_block()`；`render_diff(kind=)` 现在真的用上了
- `_rewrite_session_id()` 失败时清理 `.tmp`，不留残缺文件

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
- 新增 `tests/`：`prepare_fixture.py` 从真实数据只读复制出临时 fixture，`run_tests.py` 提供一整套端到端 + 单元级用例（用例数随本机 fixture 内容浮动，实际数量看运行末尾的「结果」行），全程在临时目录运行

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

### ⭐ Core Features

| | Feature | What it solves |
|:--|:---|:---|
| ⭐ | **Cross-device project migration**<br>`migrate_project.py` (new in v1.7) | **Home and office computers with different accounts, alternating on the same project** — pack a project's sessions into one file and carry it over; wizard mode, three steps |
| 🔄 | **Full-account merge**<br>`migrate.py` | After switching accounts / re-logging in, conversations, memory and MCP connectors "disappear" — one command merges them back |
| 💬 | **Single-session cross-edition migration**<br>`migrate_session.py` | Move one conversation between the domestic and international editions |

**All three scripts open an interactive wizard when run with no arguments — no user_id knowledge required.**

Cross-device highlights: re-importing the same package never duplicates conversations (built for alternating work), workspace memory travels along, macOS ⇄ Windows path remapping, automatic backup with one-command rollback.

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
python3 scripts/migrate.py --diagnose                            # Diagnose only
python3 scripts/migrate.py --source <USER_ID>                    # Specify source account
python3 scripts/migrate.py --source <USER_ID> --target <USER_ID> # Pin the target too (no login switch)
python3 scripts/migrate.py --intl                                # International edition (~/.workbuddy-ai)
python3 scripts/migrate.py --intl --diagnose                     # Diagnose international edition
python3 scripts/migrate.py --intl --source <USER_ID>             # Migrate within the international edition
python3 scripts/migrate.py --dir ~/.workbuddy-ai                 # Explicit data dir (beats --intl)
python3 scripts/migrate.py --assume-clients-closed               # Continue when process *detection fails*
                                                                 # (still blocks if a client is detected)
python3 scripts/migrate.py --rollback <TAG>                      # Rollback to a backup
```

> **Domestic vs International**: not just a different data directory — the **login source differs too** (both editions treat `storage/skeleton/account-snapshot.json` as authoritative; only the domestic edition falls back to the platform `storage.json`, and the international edition never reads it). Domestic uses `~/.workbuddy/`, international uses `~/.workbuddy-ai/`. Directory resolution order: `--dir` > `--intl` > auto-detect (`~/.workbuddy-ai` non-empty means international). See the note under **Compatibility** for details.

### Backup layout

Since v1.6 the two scripts keep their backups apart — the `meta.json` formats differ, so mixing them makes `--backups` list garbage and `--rollback` crash with `KeyError`:

| Path | Contents | Roll back with |
|:---|:---|:---|
| `~/.workbuddy/migrate_backups/<TAG>/` | whole-account backup | `scripts/migrate.py --rollback <TAG>` |
| `~/.workbuddy/migrate_backups/session/<TAG>/` | single-session backup | `scripts/migrate_session.py --rollback <TAG>` |

Backups in the old location are still found (both directories are scanned). `--backup-dir` (**only `migrate_session.py` has it — `migrate.py` does not**) adds an **extra** search root rather than restricting the search: new backups are written to `<dir>/session/` (the same namespace, so pointing a custom dir at `migrate_backups` can't mix them with whole-account backups), lookups still fall back to the two standard directories, and a hit outside your directory is reported explicitly.

### Exit codes

All three scripts share this table, **except `3`, which only `migrate.py` can return** — "nothing to migrate" is a whole-account concept. The single-session script exits `1` when the source or target edition has no DB at all.

| Code | Meaning |
|:---:|:---|
| `0` | Something was actually migrated / rolled back |
| `1` | Error (including: `migrate_session.py` found no DB in the source or target edition — wrong edition) |
| `2` | Blocked by the "clients must be closed" check (a client is running, or detection was unreliable and no `--force` / `--assume-clients-closed` was given) |
| `3` | **`migrate.py` only**: nothing to migrate, nothing was changed. Covers two cases — the source account really has no session / memory / connector, *or* it has data that was already identical to the target, so the merge produced nothing new |

> ⚠️ Do not treat exit code `0` as "data was definitely moved" in automation, and do not treat `3` as a failure: it means nothing was changed. Rule of thumb: `0` → success, `3` → skipped, anything else → failure.
>
> ⚠️ **Breaking change**: before v1.6.1 `migrate.py` returned `0` when the source account had no data. If your automation checks `rc == 0`, runs that used to "succeed" now return `3` — that is not a new failure, it is a long-standing skip finally being reported honestly.

### What Gets Migrated

| Data | How | Strategy |
|:---|:---|:---|
| Session history | SQLite `user_id` field | UPDATE to new account |
| Long-term Memory | `~/.workbuddy/memory/{uid}_memory.md` | Append + deduplicate |
| MCP Connectors | `~/.workbuddy/connectors/{uid}/mcp.json` | JSON deep merge |
| Cloud channel mapping | `edge-sync-mapping*.db` → `msg_channel` | v1.6.3: drop the old account's rows so EdgeSync re-uploads under the new account (restored on rollback; `--keep-cloud-mapping` opts out) |
| Skills | `~/.workbuddy/skills/` | ❌ — global, no isolation, nothing to migrate |
| Automations | `workbuddy.db` → `automations` table | ❌ — no `user_id` column, global |
| Settings / MCP / Plugins | global config files | ❌ — global |

Not migrated (no account isolation): `todos/`, `tasks/`, `skills/`, automations, settings. Per-uid dirs left alone: `inspiration/{uid}/`, `security/{uid}/`, `storage/user-{uid}*`. See the Chinese section for the full boundary table.

### Features

- 🧙 Interactive wizard (pick target & source accounts by number, no user_id needed)
- 🖥️ Cross-platform: data-dir paths resolved via pathlib, platform `storage.json` auto-detected for macOS / Windows / Linux (v1.4)
- 🌍 Domestic / International edition: interactive wizard prompts for edition, or use `--intl` for `~/.workbuddy-ai`
- 💾 Automatic backup before every migration, one-command `--rollback` afterwards
- 🔒 Safe: append-only memory, deep-merge connectors (existing target config is kept), WAL checkpoint before & after
- 🔍 Authoritative login detection: `storage/skeleton/account-snapshot.json` inside the data dir first, platform `storage.json` as fallback (domestic edition only), DB session-count as cross-check (both scripts share this order since v1.6.1)
- ✅ Post-migration verification (source user_id must be zero)
- 🪶 Zero dependencies (Python 3.8+ only)

### How it works

1. **Auto-diagnose** — discover every account from the DB, memory files and connector directories. The current account comes from `storage/skeleton/account-snapshot.json` → `primary.uid` inside the data dir (authoritative, edition-aware); the domestic edition additionally falls back to the platform `storage.json` → `genie.userId`, and the DB's highest session-count user_id is a cross-check. Conflicts prefer the login source and warn. (Never "latest session": an old account's last session can be newer than the current account's.)
2. **Safe backup** — into `~/.workbuddy/migrate_backups/{timestamp}_{first-8-of-uid}/` before anything is written (same-second reruns get a numeric suffix instead of overwriting the previous backup).
3. **Migrate** — sessions via `UPDATE user_id`; memory appended after deduplicating by semantic `memoryBlock` (line-based dedup only for the old format without `RAW_JSON`); connectors deep-merged.
4. **Persist + verify** — WAL checkpoint, then a fresh read-only connection re-checks that the source user_id is down to zero.
5. **Restart prompt** — restart the client so its in-memory cache is refreshed.

### Compatibility

- ✅ WorkBuddy Domestic edition — Windows (tested: Win 11 + Python 3.13)
- ✅ WorkBuddy International edition — Windows (tested: data dir `~/.workbuddy-ai/`, use `--intl`, v1.5)
- ⚠️ WorkBuddy Domestic / International edition — macOS / Linux (paths adapted in v1.4, **untested**)
- ❌ CodeBuddy CLI (not needed — memory is isolated per project at `~/.codebuddy/memories/{project-id}/` and sessions are per-`{sessionId}.jsonl` files, with no `user_id` filter, so switching accounts loses nothing)

> ⚠️ **Only Windows is actually tested** (Windows 11 + Python 3.13). Everything marked "untested"
> shares the same pathlib-based path handling and process detection (`tasklist` on Windows, `ps`
> elsewhere), but there is no test record — issue reports welcome.

> **Domestic vs International**: not just a different data directory — the **login source differs too**.
> Both editions write `storage/skeleton/account-snapshot.json` (authoritative); the domestic edition
> additionally has the platform `storage.json` as a fallback, while the **international edition never
> reads it** (that file holds the domestic login, and reading it would make `--intl` migrate data
> under an account that does not exist there). Directory priority: `--dir` > `--intl` > auto-detect
> (`~/.workbuddy-ai` wins when present and non-empty).

### Safety rules

1. **Backup first, always** — a backup is created automatically before any migration and cannot be skipped
2. **Source ≠ target** — prevents self-overwrite
3. **Memory is appended, never overwritten** — the current account's existing memory is preserved
4. **Connectors are deep-merged** — the target account's existing config is kept
5. **Restart after migrating** — the WorkBuddy client caches state in memory
6. **Backups can be cleared after 7 days** — delete them manually when you no longer need rollback


### Rollback

```bash
# Whole-account backups (migrate.py)
ls ~/.workbuddy/migrate_backups/          # domestic edition (intl: ~/.workbuddy-ai/migrate_backups/)
python3 scripts/migrate.py --rollback 20260525170000_abc12345

# Single-session backups (migrate_session.py; under migrate_backups/session/ since v1.6)
python3 scripts/migrate_session.py --backups
python3 scripts/migrate_session.py --rollback 20260922000000_domestic2intl_12345678

# Whole-DB restore (migrate_session.py): restores every DB the migration touched
python3 scripts/migrate_session.py --rollback 20260922000000_domestic2intl_12345678 --full
```

> ℹ️ **What `--full` restores**: it rolls the databases the migration **touched** back to the
> moment of migration (DB + transcripts + tasks), so conversations and messages created
> *after* the migration are lost along with it — the script warns explicitly before you confirm.
> In `copy` mode (`--mode copy`) the source edition was deliberately kept and the migration
> never modified it, so `--full` **skips** the source side (printed as ⏭️) and only restores the target.

> ⚠️ **Backup directories contain personal data — clean them up when done.** A backup may hold:
> `workbuddy.db` (whole-DB snapshot), `mcp.json` / `connector-states.json` (may contain tokens),
> `.master.key`, `{uid}_memory.md`, plus transcripts `*.jsonl` and `tool-results/`.
> Once you no longer need to roll back, move them to the **trash** rather than deleting outright:
>
> ```bash
> # See what's there (one directory per edition)
> ls -la ~/.workbuddy/migrate_backups/ ~/.workbuddy-ai/migrate_backups/
> # macOS: move to Trash
> trash ~/.workbuddy/migrate_backups/20260525170000_abc12345
> # Linux: move to trash
> gio trash ~/.workbuddy/migrate_backups/20260525170000_abc12345
> # Windows PowerShell: move to Recycle Bin
> #   Add-Type -AssemblyName Microsoft.VisualBasic
> #   [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
> #     "$env:USERPROFILE\.workbuddy\migrate_backups\20260525170000_abc12345",
> #     'OnlyErrorDialogs', 'SendToRecycleBin')
> ```
>
> The scripts never delete old backups for you — only you know when rollback is no longer needed.


### Single-session cross-edition migration

To move **one conversation** between editions (domestic ⇄ international), use the second script:

```bash
python3 scripts/migrate_session.py                                    # interactive wizard
python3 scripts/migrate_session.py --list --from domestic             # list what's there first
python3 scripts/migrate_session.py --from domestic --to intl --session-id <ID>
```

**How it differs from `migrate.py`**

| | `migrate.py` | `migrate_session.py` |
|:---|:---|:---|
| Scope | whole account (all conversations + memory + connectors) | **one conversation** |
| Editions | within a single edition | **domestic ⇄ international** |
| Default semantics | **ownership transfer** (`UPDATE sessions.user_id`; the source account stops seeing them — nothing is deleted). Memory / connectors are merged | **move** (source deleted), or `--mode copy` to keep it |

> ⚠️ `migrate.py` works **inside one data directory** (`--intl` only switches which directory is the target — it does not move data across editions) and it does **not** carry `projects/{slug}/*.jsonl` or `tasks/` along, because within one directory those are shared per session anyway. To move conversations to the *other* edition you must use `migrate_session.py`, otherwise the target only gets a session row with an empty transcript.
>
> ⚠️ **`--mode copy`**: whatever account the source belongs to, `copy` keeps the source and produces a separate conversation owned by the target. (Cross-edition this is a copy into the other edition's DB under the same session id; same-edition it is a clone with a **new** session id. It used to degrade into a `user_id` reassignment across accounts, which lost the conversation from the source account — contradicting "copy keeps the source".)

- **Close both WorkBuddy clients first** — the script refuses to run otherwise (WAL not flushed + in-memory cache would overwrite your changes). This applies to `--rollback` as well: rollback also rewrites the DB and deletes files.
- If the target already has the conversation, you get a diff (last activity / message count / size / last prompt) plus a recommendation, then a choice:
  - **hard conflict** (same ID) → overwrite / cancel
  - **soft conflict** (same title, different ID) → overwrite / don't overwrite / cancel
  - overwriting always writes under the **source's** ID and removes the target's old row, so the transcript filename matches the ID.

**What one conversation actually consists of** (miss one and the client misbehaves):

| Data | Where | Note |
|:---|:---|:---|
| session row | `workbuddy.db` → `sessions` | inserted cross-DB, `user_id` rewritten to the target account |
| usage stats | `session_usage` | token counters |
| workspace entry | `workspaces` | without it the client can't resolve the path |
| **transcript** | `projects/{slug}/{id}.jsonl` | **without it the conversation opens empty** |
| tool results | `projects/{slug}/{id}/tool-results/*.txt` | spilled large outputs; missing = missing content |

**Same-edition runs** (`--from` equals `--to`): if the conversation belongs to another account, `move` just reassigns `user_id`; if it already belongs to the current account (or you passed `--mode copy`) it is **cloned** — new session id, title suffixed with 「（副本）」, every in-transcript `"sessionId"` rewritten, transcript and `tool-results/` renamed to the new id. The original is never touched, and rollback deletes only the copy.

**Parameters**

| Flag | Meaning | Default |
|:---|:---|:---|
| `--from` / `--to` | source / target edition (`domestic` \| `intl`) | `domestic` |
| `--list` / `--query` | list conversations (optionally filtered by title / cwd / id) | - |
| `--session-id` | conversation id (prefix accepted) | - |
| `--mode` | `move` (delete source) / `copy` (keep source) | **`move`** |
| `--on-conflict` | `ask` / `skip` / `overwrite` / `newer` (without a TTY `ask` degrades to `skip`) | `ask` |
| `--target-uid` | pin the target edition's user_id (otherwise inferred from `account-snapshot.json`). **Warns first if the value doesn't look like a UUID** — a typo would attach the conversation to a non-existent account, which looks exactly like "migrated fine but the conversation vanished"; in non-interactive mode it asks for one more confirmation | - |
| `--dry-run` | print the plan, write nothing (no conflict prompt either) | off |
| `--yes` | skip confirmation prompts — **conflict handling is governed by `--on-conflict`, not by this flag** | off |
| `--force` | skip the "clients must be closed" check | off |
| `--assume-clients-closed` | gentler than `--force` and accepted by both scripts: continues only when process **detection itself fails**; still blocks when a client is actually detected running | off |
| `--backups` / `--rollback TAG` | list backups / roll back (prefix accepted) | - |
| `--full` | with `--rollback` only: whole-DB restore (DB + transcript + tasks). On its own it **errors out** | off |
| `--backup-dir` | backup dir (**this script only** — `migrate.py` has no such flag): new backups go to `<dir>/session/`; also an **extra** search root when looking up / rolling back | - |

Without `--session-id` you get the interactive wizard, which passes `--mode` / `--yes` / `--dry-run` / `--target-uid` / `--backup-dir` / `--assume-clients-closed` / `--on-conflict` / `--from` / `--to` / `--query` through.

**Rollback** (single-session backups — for `migrate.py` whole-account backups use `migrate.py --rollback <TAG>`)

```bash
python3 scripts/migrate_session.py --backups
python3 scripts/migrate_session.py --rollback 20260922000000_domestic2intl_12345678
python3 scripts/migrate_session.py --rollback <TAG> --full     # whole-DB restore
```

Rollback is precise to that one conversation — other conversations are untouched.

> ℹ️ **`--full` scope**: the whole-DB restore puts the databases the migration **actually touched** back to the pre-migration state (DB + transcript + tasks), so anything created after the migration is lost — the script says so before you confirm. In `copy` mode the source edition was deliberately kept and was never modified, so `--full` does **not** restore it (it prints a ⏭️ line saying it skipped); only the target is restored.

> ⚠️ **Backups contain personal data** — a `workbuddy.db` snapshot, `mcp.json` / `connector-states.json` (possibly tokens), `.master.key`, `{uid}_memory.md`, plus transcripts and `tool-results/`. Move them to the recycle bin rather than deleting them outright once you no longer need to roll back. The scripts never clean up old backups for you.

### Known limitations

| Limitation | Detail |
|:---|:---|
| Multiple memory blocks | Structured memory is **appended** as one `RAW_JSON` block. Whether the client merges several blocks is unverified — if it reads only the first one, migrated memory exists on disk but stays invisible in the UI. The script prompts you to check |
| Non-Windows process detection | the "clients must be closed" check uses `tasklist` on Windows (tested); on macOS / Linux it uses `ps`, where Electron apps may report a package name — false negatives are possible, so verify yourself if you need `--force` |
| Several same-titled targets | with more than one same-title conversation in the target, the script handles **one** of them (it prints the other ids/titles) — clean up the rest in the client |
| Empty session `cwd` | a few rows have no `cwd`; the transcript directory then falls back to the source-side directory name, and if that fails the script aborts instead of silently writing to the wrong place |
| Transcript id rewriting | only the `"sessionId":"..."` **field value** is rewritten. Old ids referenced inside message text (logs, paths) are left alone — that is user-visible content |

### Comparison with alternatives

| Project | Focus | Same-platform account switch | Session migration | Memory migration |
|:---|:---|:---:|:---:|:---:|
| **This project** | Merging data after a same-platform account switch | ✅ | ✅ | ✅ |
| [ai-memory-sync](https://github.com/supercrzy/ai-memory-sync) | Cross-device memory sync | ❌ | ❌ | ✅ |
| [claw-migrate](https://github.com/citriac/claw-migrate) | Cross-platform memory migration | ❌ | ❌ | ✅ |
| [workbuddy-manager](https://github.com/starsss0416/workbuddy-manager) | Local session management | ❌ | ✅ local | ❌ |

**The gap this project fills**: cross-platform migration and cross-device sync are both covered
elsewhere, but **merging data after a same-platform account switch** is the one scenario nobody handled.


### FAQ

**Q: Is my history really not lost after switching accounts?**

A: Right — the files are still on disk, the UI just filters by `user_id`. Merging them into the current account makes them visible again.

**Q: Can I migrate back?**

A: Yes. Login to B again and run `--source <A's user_id>`, or use `--target` and skip the login switch. Memory is deduplicated by semantic block (`memoryBlock`) and connectors merge by key, so reverse runs don't duplicate. Note that a reverse run moves **all** sessions owned by A (including A's own) — to undo one specific migration, `--rollback` is cleaner.

**Q: Is the old account's data still there after a migration?**

A: Sessions are reassigned (`user_id` changed), so they are invisible under the old account — nothing was deleted. Memory and connector source files are kept as-is; delete them yourself if you want.

**Q: CodeBuddy CLI?**

A: Not supported, and not needed — see **Compatibility**.

**Q: Does it work on macOS / Linux?**

A: Supported at the path level. Paths have been adapted per platform since v1.4 (macOS `~/Library/Application Support/...`, Windows `%APPDATA%`, Linux `XDG_CONFIG_HOME`), and since v1.6.1 login state comes from `account-snapshot.json` inside the data dir, so it no longer depends on where the platform `storage.json` lives.

**Only Windows is actually tested**, though (Windows 11 + Python 3.13) — macOS / Linux are "cross-platform code, never run here". Process detection uses `tasklist` on Windows and `ps` elsewhere, and the `ps` path has zero test coverage. See **Compatibility**.

**Q: How do I sync WorkBuddy conversations between two computers with different accounts?**

A: Use **cross-device project migration**: on computer A run `python3 scripts/migrate_project.py` and choose "pack & take away"; copy the `.wbproj` file from the Desktop to computer B, run it again and choose "import". Accounts and project paths are remapped automatically, and re-importing the same package never duplicates conversations.

### Project structure

```
workbuddy-account-migrate/
├── scripts/
│   ├── migrate.py            # whole-account migration (within one edition)
│   └── migrate_session.py    # single-session migration (cross-edition, v1.6)
├── tests/
│   ├── prepare_fixture.py    # builds a temp fixture (read-only copy of real data)
│   └── run_tests.py          # end-to-end + unit tests (count varies with your fixture)
└── references/
    └── data_isolation_map.md # data-isolation map
```

Tests run entirely inside the temp fixture and never touch your real data directory: `python3 tests/run_tests.py`. ⚠️ The test scripts are **Windows-only in practice** (Win 11 + Python 3.13): the fixture is copied from your real data, whose paths are Windows-shaped, and a machine without WorkBuddy installed can't build a fixture at all.

### Cross-device project migration (v1.7.0)

Both scripts above operate on **one machine**. For the "home PC (account A) + office PC (account B), alternating work on the same project" scenario (issue #8), use the third script — **project export / import**.

**Beginner route (recommended): run it with no arguments for a wizard — pick numbers, drag files**

```bash
python3 scripts/migrate_project.py
```

- Choose 1 on the source machine: it lists all projects → pick one → the package lands on your **Desktop**, ready to AirDrop / copy
- Choose 2 on the target machine: packages on **Desktop / Downloads** are auto-discovered → pick one → drag the project folder into the window → confirm
- Every import is backed up first (`--rollback` to undo); account and path remapping are fully automatic

**Advanced (optional)**:

```bash
python3 scripts/migrate_project.py export --cwd /path/ProjectA
python3 scripts/migrate_project.py export --cwd /path/ProjectA --out /tmp/ProjectA.wbproj   # custom output path (Desktop by default)
python3 scripts/migrate_project.py info ProjectA.wbproj
python3 scripts/migrate_project.py import ProjectA.wbproj --cwd /new/path --dry-run
python3 scripts/migrate_project.py import ProjectA.wbproj --cwd /new/path --on-conflict overwrite
python3 scripts/migrate_project.py --rollback <TAG>
```

**Scope**: the tool only packs and unpacks. How you transfer the file (AirDrop / USB / scp), where the project lives on the target machine, which account you log in with — all up to you. On import, `user_id` is rewritten to the **target machine's login account**, `cwd` and in-transcript paths are remapped to the new path automatically.

**Package contents**: session + usage rows, transcripts (`projects/{slug}/`), `tool-results/`, `todos/`, `tasks/`, and the project's `.workbuddy/` workspace memory (opt out with `--no-workspace-memory`). Account-level memory/connectors are **out of scope**.

**Conflict semantics**: same-id conflicts are **overwritten** (re-importing the same package never duplicates conversations); same-title-different-id defaults to skip; non-interactive terminals degrade to skip unless `--on-conflict overwrite` is given. Imports are backed up and reversible via `--rollback <TAG>`.

> The script reuses mechanisms battle-tested in v1.6 (column-name-aligned inserts, file/dir-safe copies, WAL checkpointing) and ships with 37 synthetic fixture tests; the full cross-device flow has not yet been verified between two real machines — run `--dry-run` first and report issues.

### Contributing

- Bug reports / feature requests → [Issues](https://github.com/xiaoliuzhuan666/workbuddy-account-migrate/issues)
- Code → open a PR; make sure no user_id or token is hardcoded
- Real-world feedback on Windows / Linux → issues are very welcome


### Changelog

#### v1.7.0 (2026-10-06)

**New: `scripts/migrate_project.py` — cross-device project export/import** (answers issue #8)

- **Scenario**: home PC (account A) + office PC (account B), alternating work on one project
- **Beginner wizard**: run with no arguments (source machine: "pack & take away" / target machine: "import"); pick numbers and drag files throughout; exported packages land on the **Desktop**, imports auto-discover packages on **Desktop/Downloads**; interactive imports confirm before writing
- **export**: lists projects by `cwd`, packs sessions/usage/transcripts/tool-results/todos/tasks/workspace memory into a `.wbproj` archive with a manifest. Read-only — safe while the client runs
- **import**: detects the target machine's login uid (account-snapshot authoritative), validates the new local path, remaps everything (`user_id`, `cwd`, slug re-derived from the new path, in-transcript top-level `cwd` fields rewritten streaming)
- **Conflicts**: same id → overwrite (re-import never duplicates; this is what makes alternating use work); same title different id → skip by default; non-TTY degrades to skip, `--on-conflict overwrite` to force
- **Rollback**: sqlite backup-API snapshot + stashed overwritten files before import; `--rollback` restores the whole thing (stale `-wal/-shm` are cleared first so old WAL frames can't replay over the restore)
- **Tests**: `tests/run_project_tests.py` — 37 synthetic fixture checks running export → transfer → import → re-import → rollback across two "virtual machines" with different uids, path styles and table schemas; no real data required
- **Explicitly out of scope**: network transfer, account-level memory/connector merging, automatic two-way sync (a possible later phase)

#### v1.6.3 (2026-09-22)

**Fixed: the target account was resolved to the wrong uid — migration "succeeds" but the sidebar stays empty**

- `get_current_user_id()` now prefers **`account-snapshot.json` → `primary.uid`** (the client's real login, which is what the session list filters by). `storage.json`'s `genie.userId` drops to second priority, DB session count is the last resort
- Why: on the domestic edition those two sources can hold **two different uids** for a long time. The tool used to pick `storage.json`, so every run merged data into the account the UI never reads — the user retried 6 times and still saw an empty sidebar
- `--diagnose` now prints all four signals side by side (client login / storage.json / last daemon `listSessions` uid / per-account session counts) and states the `--target` conclusion explicitly
- `migrate()` gained a Phase 4.5 consistency check: when the target ≠ client login it lists the two recovery paths instead of just printing "done"
- Backup `meta.json` now records `source_uid`, `client_login_uid`, `storage_json_uid`, `session_counts` for post-mortems
- Fixed daemon-log parsing: nested JSON escapes its quotes (`\"userId\":\"...\"`), so the previous regex never matched
- **New: cloud channel mapping reset** — `edge-sync-mapping*.db` rows still point at the old account's channel, so EdgeSync believes the conversations are already synced and never re-uploads them; the migration now deletes only those rows (full DB backed up first, restored by `--rollback`, opt out with `--keep-cloud-mapping`)
- **New: `scripts/force-relogin.sh`** — moves `storage/skeleton/account-snapshot.json` aside to force a fresh login; its header documents the measured limitation (works, but the client may switch back after ~1 minute, so merging data is the durable fix)
- Interactive wizard now tags the client-login account with `← client login state (the sidebar filters by this)`
- `tests/run_tests.py` skips gracefully (exit 0) when the international edition is absent
- Docs: migration boundary list, two-login-sources section, tarball install fallback

#### v1.6.2 (2026-09-22)

**Fixed: sandbox-shim hijack crashing runs inside WorkBuddy sessions**

- When run from a WorkBuddy session's Bash, the injected `PYTHONPATH` points to a sandbox shim (sitecustomize.py) that hijacks `Path.mkdir`: even with `exist_ok=True`, an existing directory raises `PermissionError EEXIST`, crashing the migration at the backup phase (both the managed and the system Python are affected)
- The script now strips `PYTHONPATH` on startup and re-executes itself (equivalent to `env -u PYTHONPATH python3 migrate.py ...` without having to remember it); all `mkdir` call sites keep an `exists()` pre-check as a second line of defense

**Added: `--restart` to auto-restart the client after migration**

- `python3 migrate.py --source <UID> --yes --restart`: after migration/rollback, automatically quits and relaunches WorkBuddy (macOS) after a short delay, refreshing the session list immediately — no manual restart needed
- Implemented as a detached background job: the script prints its full output first, then triggers the restart; when invoked inside a WorkBuddy session, the current AI session will be interrupted (expected). Windows / Linux print a manual-restart reminder

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

**Fixed: data-safety and rollback-integrity issues**

- Cross-edition transcript copy now registers each file in `meta["copied_to"]` **as it goes**: if the 2nd (or later) file fails, the files already copied are still tracked and get deleted by precise rollback (previously they were left behind as orphan transcripts; the same-edition clone path had already been fixed, the cross-edition one had not)
- `--full` whole-DB rollback now **aborts** when `-wal` / `-shm` cannot be removed (client still holding the DB) instead of overwriting the main DB anyway — otherwise SQLite replays the stale WAL and the rollback silently fails
- `migrate.py` rollback only clears sidecar files and overwrites the DB when the backup **actually contains** `workbuddy.db`; a wrong tag (e.g. a memory-only backup) no longer deletes an un-checkpointed WAL (which would be permanent data loss)
- `migrate.py` whole-account backup `meta.json` is now written atomically; backup directories that collide within the same second get a numeric suffix instead of overwriting the previous backup's meta
- `--rollback <TAG>` (both scripts) rejects tags containing path separators, `..` or absolute paths — they used to be concatenated into the backup directory and reused as rmtree/copytree targets
- `migrate.py` gained the "client must be closed" process check (new `--force` flag), aligning it with `migrate_session.py`; **both scripts now check on rollback too** (rollback also rewrites the DB and deletes files)
- `migrate_session.py` `--rollback` now requires the clients to be closed as well (previously only the migration path was guarded)
- `--dry-run` no longer pops the conflict prompt (with piped stdin an EOFError was treated as "cancelled" and the plan was never printed)
- The four "verified N rows" messages now warn when the count does not match (previously a write that silently did nothing still showed ✅); `move` deleting source files / the source tasks directory is wrapped in `try/except OSError` instead of raising a bare traceback
- Cross-edition target directory **always uses the source-side slug** (the `cwd` written into the DB is the source's); on overwrite, target-only sibling files for the same id are cleaned up so old and new data don't mix
- Overwrite no longer leaves the target with a stale transcript: files present in the target but absent from the source are removed (they are backed up in `dst_files/` and restored on rollback)
- The interactive wizard now passes `--mode` / `--dry-run` / `--yes` / `--target-uid` / `--backup-dir` through (they were silently dropped when `--session-id` was omitted); removed the never-used `dir_arg` parameter
- Current-account resolution is now consistent across both scripts and the docs: **`storage/skeleton/account-snapshot.json` first, platform `storage.json` as fallback**, with a warning when the two disagree. `migrate.py` previously put platform `storage.json` first, so the two scripts could resolve different "current accounts" on the same machine

**Fixed: high-risk data-safety issues**

- Backup `meta.json` is written **incrementally** after every destructive step (override deletion, transcript copy, `move` source-row deletion, source-file deletion). Previously it was written once at the end, so a mid-way failure left `override_deleted` / `source_deleted` / `copied_to` missing on disk → precise rollback silently skipped items
- The cross-edition `projects/<slug>/` directory name now reuses the **source-side real directory name** (the one the client actually created from the cwd), with `cwd_to_slug()` only as fallback — a self-invented rule that disagrees with the client drops the transcript into a directory the client never scans
- Soft-conflict overwrite commits the DB rows **before** deleting the target files (the reverse order could leave "row present, transcript gone" if the commit failed)
- Rollback / `--full` restore clears `workbuddy.db-wal` / `-shm` first: overwriting only the main DB lets SQLite replay an old WAL, which can make the rollback ineffective or corrupt data. `migrate.py --rollback` also warns that a whole-DB restore wipes data created after the migration

**Fixed: silent failures with `--intl` and connector merging**

- `--intl` no longer reads the platform `storage.json` (a domestic-edition login file): on a machine with both editions installed it would take the domestic uid as the international current account, migrating international data under an account that does not exist there (all conversations appear to vanish). The international edition always uses `account-snapshot.json`
- `migrate_connectors()` "deep merge" used to look only at top-level keys: `mcp.json` has just one (`mcpServers`), so once the target had it the whole file was skipped and not a single server was merged. It now deep-merges first and then compares before/after

**Fixed: other consistency and robustness issues**

- Process detection excludes the script itself (the repo directory is named `workbuddy`, so the script's own command line matched the keyword, blocking users unconditionally unless they disabled the whole check with `--force`)
- `snapshot_db()` opens the source read-only and closes connections on error (unified with `_backup_db()`)
- `mkdir` gained `parents=True`; memory filenames are UUID-validated too; prefix matching `id LIKE ?` uses `ESCAPE`; `_wal_checkpoint()` no longer treats a non-WAL `-1` as "locked by a process"
- `--target` without `--source` and `--generate-commands` without `--restore-tasks` now error instead of being ignored; a non-UUID `--target` warns
- Phase 4 verification also checks Memory / Connectors and flags silent skips
- Removed dead code `_extract_memory_block()`; `_rewrite_session_id()` cleans up its `.tmp` file on failure

#### v1.6.0 (2026-09-10)

**Single-session cross-edition migration (domestic ⇄ international)** (this section's work by [@bukall](https://github.com/bukall), PR #5)

- **New**: `scripts/migrate_session.py` — migrate one conversation between editions
- **New**: carries `session_usage`, `workspaces` and the `projects/*.jsonl` transcript along (DB row alone = empty conversation)
- **New**: conflict prompts — hard conflict (same ID) offers 2 choices, soft conflict (same title, different ID) offers 3, both with a side-by-side diff and an overwrite recommendation
- **New**: `move` by default, `copy` optional; per-session backup, rollback touches nothing else
- **Improved**: `migrate_session.py` resolves the current account from `storage/skeleton/account-snapshot.json` inside the data dir (edition-aware, cross-platform) — `migrate.py` only switched to that order in v1.6.1, see below
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
