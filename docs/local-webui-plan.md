# 本机网页版详细开发计划

- 日期：2026-11-12（Asia/Singapore，UTC+8）
- 仓库：https://github.com/xiaoliuzhuan666/workbuddy-account-migrate
- 功能分支：`feat/local-webui`（对 `main` 开 PR，不推 main、不合并，除非使用者在聊天里明确批准）
- 当前 PR：https://github.com/xiaoliuzhuan666/workbuddy-account-migrate/pull/13
- 依据：
  - 用量与桌面端调研（canvas [2053bd2c](https://cursor.com/canvas/store-0b6edd25-46b6-48b3-92e8-4fd77b14b17a/2053bd2c-3109-48f3-bf03-7107700396cb)）
  - 积分 / Token 换算调研（canvas [985ac755](https://cursor.com/canvas/store-0b6edd25-46b6-48b3-92e8-4fd77b14b17a/985ac755-a6c1-49cb-960e-69b1b81652e5)，2026-11-12）
- Python 3.8+，优先标准库；升 3.10+ 时在 README 说明。前端第一版用原生 HTML/CSS/JS，不上重型框架。

---

## 0. 原则（全程遵守）

1. 服务只绑定 `127.0.0.1`，校验 Host（只准 `127.0.0.1` / `localhost`），写操作必须带本机令牌并二次确认。日志和界面脱敏，不复述密钥、令牌、登录凭据，只报位置。
2. 默认只读。账号同步复用已有 `migrate.py`，必须先退出 WorkBuddy 客户端，网页不允许 `--force`。不改登录态文件，不上传真实用量。
3. 用量解析改编自 [workbuddy-usage-status](https://github.com/clancy-feng/workbuddy-usage-status)（MIT），保留 `Copyright (c) 2026 Clancy`，见 `NOTICE`。
4. **价格禁止编造。** `rates.json` 每条写来源 URL 和日期；没核实到的写「没读到」，估算时跳过。
5. **禁止**把社区插件里「1 积分 = 5 万 token」当成真值（[dsh-workbuddy-token](https://github.com/xsdao/dsh-workbuddy-token) 的 README 已写明这是作者 2026-10-29 的实测值、官方未公布）。
6. tokscale 只用来核对本机 token 总量，不当积分账本，也不用它默认的 LiteLLM 美元价当 WorkBuddy 成本。

---

## 1. 阶段一览

| 阶段 | 目标 | 今天是否交进第一版 PR | 谁来做 |
|:---|:---|:---|:---|
| **A. 骨架已落地** | 核心库 + 本机 HTTP API + 清爽单页；Host / 令牌 / 二次确认；jsonl 用量与 `credit_json` 交叉校验；CodeBuddy 能读就统计 | 已在 PR #13（提交 `1ee7b0c` / `b47d8f3`） | 助手已完成 |
| **B. 价格模块按新调研改口径**（本计划的重点） | 价格主源 ModelLink；models.dev 补国际官价；LiteLLM 只备用。WorkBuddy「每积分多少 token」只从本地 jsonl 反推，且必须拆开缓存命中 / 未命中 / 输出，再按官方 API 单价折等效人民币。火山 AFP 按官方公式，系数写入 `rates.json` | **今天尽量交进同一 PR** | 助手实现核心与测试；标了「练手」的页面和补充条目留给使用者 |
| **C. 对照与刷新** | 可选：本地缓存 ModelLink `catalog.json` 的刷新命令；用 tokscale 对照 token 合计（只读、不入库）；国际版 `~/.workbuddy-ai` 页面体验打磨 | 今天有余力就做刷新命令的骨架和测试；对照工具可后做 | 刷新命令的测试由助手写，实现可作练手（见第 8 节） |
| **D. 可选扩展**（不做进第一版） | 定时归档 traces；同一前端套 Tauri；Spring 接同一套 API；虚构数据的只读演示（真实用量不上 learn.sunning.fun） | 否 | 后续再排 |

---

## 2. 模块和文件结构

```
workbuddy-account-migrate/
├── config/
│   ├── model_rates.json              # 本地可编辑价格表（第一版已有，阶段 B 按新口径改结构）
│   └── modellink-catalog.example.json  # 阶段 C：说明如何放入本地缓存的 catalog，不含大规模爬取结果
├── docs/
│   ├── WEBUI.md                      # 已有：启动、令牌、安全注意
│   ├── local-webui-plan.md           # 本计划
│   └── screenshots/                  # 合成 fixture 截图，不含真实用量和令牌
├── NOTICE / THIRD_PARTY/             # Clancy MIT 声明（已有，不可删）
├── src/workbuddy_core/               # 核心库，网页和测试都走这里
│   ├── paths.py                      # 已有：--dir / --intl / 双版本
│   ├── accounts.py                   # 已有：只读账号与会话列表（不含正文、不含快照里的凭证）
│   ├── usage.py                      # 已有：jsonl rawUsage 解析与交叉校验
│   ├── rates.py                      # 已有骨架；阶段 B 扩展为多源价格与反推接口
│   ├── security.py / migrate_bridge.py  # 已有：门卫与同步封装
│   ├── codebuddy.py                  # 已有探测
│   ├── inference.py                  # 【阶段 B 新增】按模型反推「每积分 token」（命中/未命中/输出分开）
│   ├── equivalent_cost.py            # 【阶段 B 新增】同一批 token × 官方 API 单价 → 等效人民币
│   └── volc_afp.py                   # 【阶段 B 新增】AFP = (in×cin + out×cout) / 10000
├── scripts/webui.py                  # 已有本地服务
├── webui/                            # 已有单页（index.html / styles.css / app.js）
│   └── 阶段 B 在同一单页增加「官方价对照」「本地反推」区块；不新开框架
└── tests/run_webui_tests.py          # 已有 78 项；阶段 B 追加反推、AFP、禁止 5 万默认值、没读到跳过
```

既有三个迁移脚本（`migrate.py` / `migrate_session.py` / `migrate_project.py`）行为不变。阶段 A 只给 `migrate.py` 的自排除名单加了 `webui.py`，避免网页进程被误判成客户端。

---

## 3. API 列表

服务根地址：`http://127.0.0.1:18765/`。所有接口先过 Host 校验。写接口还要本机令牌、允许的 Origin/Referer，以及二次确认。

### 已有（阶段 A，保留）

| 方法 | 路径 | 权限 | 作用 |
|:---|:---|:---|:---|
| GET | `/`、`/styles.css`、`/app.js` | 只读 | 静态页 |
| GET | `/api/health` | 只读 | 探活，返回 bind / port |
| GET | `/api/meta` | 只读 | 数据目录、版本、令牌文件**路径**、确认语句、价格表路径。不含令牌内容 |
| GET | `/api/accounts` | 只读 | 账号列表（完整 uid 仅供本机下拉框，界面显示截断）、当前登录昵称 |
| GET | `/api/sessions?edition=&user_id=&limit=&offset=` | 只读 | 会话列表 + 每会话 token / 积分。不含对话正文 |
| GET | `/api/usage?edition=&conversion=` | 只读 | 按模型汇总、交叉校验、CodeBuddy、当前折算方式 |
| GET | `/api/rates` | 只读 | 价格表公开摘要（来源、日期、可用折算）。不含密钥 |
| GET | `/api/sync/preflight` | 只读 | 客户端是否在跑；检测失败则如实说不可信 |
| POST | `/api/sync` | **写：Bearer 令牌 + `confirm` + 确认语句「确认同步」** | 调用 `migrate.py`。客户端在跑返回 409，不改库。无令牌 401，未确认 400 |

查询参数 `edition=domestic|intl` 用于双版本切换。绑定地址不是 `127.0.0.1` / `localhost` 时进程直接退出。

### 阶段 B 新增（今天要在同一 PR 里给出）

| 方法 | 路径 | 权限 | 作用 |
|:---|:---|:---|:---|
| GET | `/api/usage` **扩展字段** | 只读 | 在现有汇总上增加：`inference`（每模型、每积分的未命中输入 / 缓存命中 / 输出 token，以及样本是否足够）、`equivalent_cny`（按官方 API 单价折算，缺价则「没读到」）、`pricing_sources`（用了 ModelLink / models.dev 的哪些条目） |
| GET | `/api/prices` | 只读 | 整理后的官方按量价与 AFP 系数：模型、输入/输出/缓存价、币种、峰谷、来源 URL、日期、状态（ok / 没读到） |
| GET | `/api/compare?model=` | 只读 | 某一模型三列对照：本地实际积分、反推的每积分 token（三项分开）、官方 API 等效人民币。缺任何一列就标「没读到」，不算出来一个假数 |

### 阶段 C（骨架可先交，实现可练手）

| 方法 | 路径 | 权限 | 作用 |
|:---|:---|:---|:---|
| POST | `/api/prices/refresh` | **写：令牌 + 确认** | 从本机已下载的 `catalog.json` 更新缓存（默认不上网；若以后加联网拉取，必须先在文档里写清、且默认关闭）。刷新失败保留旧表 |
| GET | `/api/tokscale-check` | 只读 | 若本机装了 tokscale，只跑 token 总量对照，把差额显示出来。读不到则「没读到」。**不读它的价格，不写入积分账本** |

---

## 4. 数据模型

### 4.1 本地用量（已实现，阶段 B 沿用）

从 `~/.workbuddy/projects/*/*.jsonl` 每行读取顶层 `providerData.rawUsage` 与 `providerData.model`（改编自 usage-status，保留 Clancy 版权）：

| 字段 | 含义 |
|:---|:---|
| `prompt_tokens` | 输入 token（含缓存） |
| `prompt_cache_hit_tokens`（及兼容别名） | 缓存命中 |
| `uncached_input` | `prompt_tokens - hit`，前者不大于后者时记 0 |
| `completion_tokens` | 输出 token |
| `completion_thinking_tokens` | 思考 token，单独记账，不与输出重复加总去充当效率分母 |
| `credit` | 这次调用扣的 WorkBuddy 积分 |
| `original_credit` | 有的免费模型会给「本来该扣」的值，只展示、不计入实际积分 |
| `timestamp` | 按调用自己的时间归日期，不按会话首日挂积分 |
| `session_id` | 文件名；再用 `sessions.user_id` 归属账号 |

交叉校验：把每会话 jsonl 积分合计，与 `workbuddy.db` 的 `session_usage.credit_json`（对象各值求和）对照，报告差额和差异样本。

### 4.2 本地反推：「1 积分对应多少 token」（阶段 B，只从 jsonl 来）

对每个模型，用该模型全部（或可筛选时间窗内的）调用，做无截距的三项拆分，而不是「总 token / 总积分」一个数：

```
credit ≈ a × 未命中输入 + b × 缓存命中 + c × 输出
```

单位：积分 / token，展示时再换成「每积分多少 token」（取倒数）以及「每百万 token 多少积分」。

- 样本太少、拟合不可靠、或该模型积分为 0：三项都标「没读到」，**不准回退到 5 万 token/积分**。
- 可靠性门槛参考 usage-status（样本数、R²），具体阈值写进配置并在界面注明；不够格只展示「合计总 token / 积分」作参考，并标明「含缓存，会被高命中拉高，不能当成官方汇率」。
- 之前在本机 14 天数据上见过：deepseek-v4.1-flash 总 token/积分约 62 万、去缓存后约 9056、命中率 98.8%。这只说明必须拆缓存，不把这些数字写进价格表当官方值。

### 4.3 官方按量价（阶段 B，主源 ModelLink）

本地 `rates.json`（或加载后的内存结构）每条至少包括：

```json
{
  "id": "deepseek-flash-offpeak",
  "match": ["deepseek-v4-flash", "deepseek-flash"],
  "currency": "CNY",
  "unit": "CNY / million tokens",
  "input": 1,
  "output": 4,
  "cached_read": 0.02,
  "cached_write": "没读到",
  "peak": { "input": 2, "output": 8, "cached_read": 0.04, "note": "高峰为闲时两倍" },
  "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing.md",
  "source_via": "ModelLink catalog.json cost_cn，并与 DeepSeek 官文核对",
  "retrieved_at": "2026-11-12",
  "status": "ok"
}
```

来源优先级：

1. **主源** [ModelLink](https://github.com/goroutined/modellink)（`@modellink/data` / [catalog.json](https://goroutined.github.io/modellink/)，MIT）：人民币、缓存价、峰谷、部分 `cost_points`。
2. **辅源** [models.dev](https://models.dev/)（`https://models.dev/api.json`，MIT）：国际官价和 ModelLink 为空的模型；美元要注明汇率来源和日期，折算失败则「没读到」。
3. **备用** LiteLLM model cost map：宽覆盖，仅当前两处都没有时出现，并标明「备用、美元、可能是高峰价」。不覆盖国内套餐积分规则。

调研里已核对、可写入第一版价格表的例子（仍要在文件里保留来源和日期；实现时再对照一次原页，对不上就改回「没读到」）：

| 模型 | 输入 | 输出 | 缓存读 | 来源日期 |
|:---|:---|:---|:---|:---|
| deepseek-flash 闲时 / 高峰 | 1 / 2 | 4 / 8 | 0.02 / 0.04 元/百万 token | 2026-10-17，DeepSeek 官文（ModelLink 已对上） |
| glm-5.3 | 8 | 28 | 2 | 2026-08-19，智谱文档 |
| glm-5.3-flash | 没读到人民币 `cost_cn` | 没读到 | 没读到 | ModelLink 该条为空；models.dev 有美元价（2026-08-26），若采用必须注明汇率 |
| kimi-k3.1-pro | 13 | 68 | 1.3（写缓存 13） | 2026-10-24，月之暗面文档 |
| qwen3.8-max | 12 | 36 | 1.5（写缓存 15） | 2026-08-03，百炼 |

### 4.4 等效人民币（阶段 B）

对同一批本地 token：

```
等效人民币 = (未命中输入 × 输入单价 + 缓存命中 × 缓存读单价 + 输出 × 输出单价) / 1e6
```

缺任何一个必需单价就整项标「没读到」。峰谷若本地 jsonl 没有时段标记，默认用闲时并注明「未区分高峰」；不要假装知道高峰。

这与「按加量包 1000 积分 50 元把实际积分折成人民币」是**两列对照**，不能互相替代：

- 左列：实际扣了多少 WorkBuddy 积分，再按官方[定价详情](https://www.workbuddy.cn/docs/workbuddy/Pricing)里的加量包 / 订阅档折人民币（这是套餐成本视角，阶段 A 已有）。
- 右列：同样这些 token 若走官方按量 API 会花多少人民币（这是「等效 API 成本」）。

### 4.5 火山 AFP（阶段 B）

官方公式（[个人版 AFP 规则](https://docs.volcengine.com/docs/ark/agent-plan-personal-afp-credits-billing-rules?lang=zh)）：

```
AFP = (输入 token × 输入系数 + 输出 token × 输出系数) / 10000
```

系数写入 `rates.json` 的 `volc_afp` 段，每条带来源 URL 和日期。Medium 档 200 元 / 10 万 AFP 只是套餐额度，不能单独推出「1 AFP = 多少 token」。调研里读到的例子（实现时再核对原页）：deepseek-v4-flash 输入/输出系数 0.5；deepseek-v4.1-flash 限时 0.875（原 2.5）；glm-5.3-flash 0.5；kimi-k3.1-pro 7。没核对到的模型写「没读到」。

### 4.6 账号与同步（已有，不改语义）

- 登录态只读 `account-snapshot.json` 的 `primary.uid` 和 `nickname`，其它字段丢掉。
- 同步：源 uid → 目标 uid，先备份，改 `sessions.user_id`、记忆、连接器。网页层确认后再调 `migrate()`。

---

## 5. 各页面的前端框架与布局

**框架选择：** 第一版继续用**原生 HTML/CSS/JS 单页**（已在 `webui/`）。理由：零构建、零依赖、离线可开、后面套 Tauri 或换 Spring 时只需对接同一套 API。不上 React/Vue，除非使用者把「用某某框架重写某一页」选为练手（见第 8 节），那时在新文件里做，不拆掉现有页。

**视觉：** 清爽、浅色纸感、留白充足；标题与数字清楚，表格可横滑；危险操作（同步）单独用暖色警示区。不引入外网字体或 CDN，遵守 CSP（`default-src 'self'`）。

整站一个 `index.html`，从上到下六块（阶段 B 在现有页上插入，不新开路由）：

| 区块 | 布局 | 内容 |
|:---|:---|:---|
| **顶栏** | 左标题「WorkBuddy 本机看板」，右上数据目录与国内版 / 国际版切换 | 标明「只在本机 · 127.0.0.1」 |
| **总览数字** | 6 个卡片一行 | 调用次数、实际积分、套餐折算人民币、官方 API 等效人民币（没有就「没读到」）、缓存命中率、去缓存效率 |
| **国内模型用量与价格对比**（主表，阶段 B 加列） | 表 + 折算方式下拉 | 每行：模型、调用、输入/输出/思考、命中率、**反推的每积分 token（未命中 / 命中 / 输出三列）**、实际积分、套餐折算费用、**API 等效人民币**、官方倍率（对得上才显示，否则「没读到」）。表下是交叉校验一句话和价格来源脚注 |
| **官方价与 AFP**（阶段 B 新块） | 两列表或卡片 | 左：ModelLink 等来源的按量单价（含缓存、峰谷、来源、日期）。右：火山 AFP 系数与用本机 token 估算的 AFP。缺数据的格子写「没读到」 |
| **其他本机工具** | 短文 + 可选小表 | CodeBuddy CLI：读到就列出；读不到写原因。tokscale：阶段 C 只显示 token 总量是否一致，不显示它的价钱 |
| **会话记录** | 可筛选表格 | 标题、模型、调用、积分、输入/输出、命中率、更新时间。不含正文 |
| **账号同步** | 警示底 + 表单 | 预检客户端、源/目标、令牌（密码框，不进地址栏）、输入「确认同步」、勾选「已退出客户端」。按钮默认禁用 |

移动宽度下卡片与同步表单项改单列。所有费用数字旁边都能点到或看到来源短注。

---

## 6. 验收标准

### 阶段 A（已满足，回归时必须仍通过）

- `python3 tests/run_webui_tests.py` 全部通过（现 78 项）。
- 既有 `run_tests.py`（137）、`run_dual_edition_tests.py`（24）、`run_project_tests.py`（52/0 失败）无回归。
- `--bind 0.0.0.0` 被拒绝；坏 Host 返回 403；无令牌 POST `/api/sync` 返回 401 且数据库未改；确认语句不对也不改库。
- 快照里即使有假凭证字段，接口响应也不出现。
- 默认只听 `127.0.0.1`，启动说明在 `docs/WEBUI.md`。

### 阶段 B（今天第一版 PR 要达到）

- **反推：** 用合成 fixture 可复现「未命中 / 命中 / 输出」三项；积分为 0 或样本不足时结果是「没读到」，测试里显式断言**不会**出现 50000 这个默认值。
- **等效人民币：** 给模型配好 ModelLink 口径的单价后，计算结果与手工公式一致；单价「没读到」时费用字段为「没读到」而不是 0。
- **AFP：** 对已写入系数的模型，`(in×cin + out×cout)/10000` 与官文公式一致；未写入的模型「没读到」。
- **价格表：** 每条有 `source` 和 `retrieved_at`；`rates.py` 拒绝把状态为「没读到」的数字拿去相乘。
- **界面：** 主表能同时看到「套餐折算」和「API 等效」两列；来源脚注可见。截图若更新，仍只用 fixture，不含令牌和真实会话。
- **安全与脱敏** 不回退。CodeBuddy 读不到仍写「没读到」。

### 阶段 C

- 刷新接口无令牌被拒；失败时旧价格表仍在。
- tokscale 未安装时「没读到」，安装后只对比 token 总量。

---

## 7. 风险

| 风险 | 说明 | 处理 |
|:---|:---|:---|
| 编造或误用非官方汇率 | 社区有「5 万 token/积分」等口径，官方未公布 | 代码和测试禁止该默认值；价格表缺条目就「没读到」 |
| 总 token / 积分被缓存拉高 | 高命中时每积分总 token 会到几十万，看起来像「特别便宜」 | 主表必须同时给出命中率、未命中/命中/输出三项反推 |
| 模型名对不上 | jsonl 里是 `deepseek-v4.1-flash`，目录里可能是 `deepseek-flash` | 只用价格表里显式配置的 `match` 列表，不用模糊子串（避免 `kimi-k3` 误配 `kimi-k3.1-pro`） |
| ModelLink / 官文调价 | 系数和单价会变 | 每条写日期；刷新是显式操作；界面展示 `retrieved_at` |
| 峰谷、限时折扣 | 本地 jsonl 不一定有时段 | 默认闲时并注明；限时系数到期后应改回「没读到」或新官价，不自动延长 |
| 本地 HTTP 暴露 | 会话标题在本机端口上 | 只绑 127.0.0.1、Host 校验、写操作令牌；不做局域网共享 |
| 同步误操作 | 会改库 | 二次确认、先备份、客户端必须关闭；用量默认只读 |
| 协议 | WorkBuddy / CodeBuddy 许可限制复制运行中数据 | 只读本地文件、不调非公开 API、不改登录态；同步保持明确告知 |
| 第一版价格覆盖不全 | glm-5.3-flash 的人民币 `cost_cn` 本次没读到；阿里 Token Plan 没有单模型积分单价；ModelLink 无 WorkBuddy | 如实标「没读到」，留给第 8 节练手去补 |

---

## 8. 适合使用者自己动手练全栈的部分

下面这些**明确留给你练**。助手先给骨架、接口约定、失败时的测试和视觉规范，不把整页一次性写死到「再改不动」。你本地改、跑测试、满意了再提交到同一功能分支（仍走 PR，不直接推 main）。

安全门卫、登录态读取、`migrate.py` 写库路径、令牌生成，**不作为入门练手**，以免不小心改掉底线。

### 练手 1：给一个「没读到」的模型补官方价（入门，改配置 + 看测试变绿）

- **你做：** 打开 `config/model_rates.json`，按文件里的字段模板，为例如 `glm-5.3-flash` 补人民币单价（若你在智谱或 ModelLink 官方页核对到了）。必须填写 `source` URL 和 `retrieved_at`。核对不到就保持「没读到」，不要抄未核对的博客数字。
- **助手给：** JSON 字段说明、一条会失败的测试（「该模型费用在没价时必须是没读到」）、一条在你填对之后会通过的测试夹具。
- **验收：** `python3 tests/run_webui_tests.py` 相关项通过；页面上来源脚注能看到你写的 URL 和日期。

### 练手 2：「官方价与 AFP」区块的前端（前端为主）

- **你做：** 在 `webui/` 里把阶段 B 的「官方价与 AFP」区块做成清爽的卡片或表格：显示单价、峰谷、系数、来源。可按 `docs/WEBUI.md` 的视觉（纸色底、松绿主色、数字用等宽）发挥，但不要引入外网脚本。
- **助手给：** `GET /api/prices` 的约定和返回例子、空容器和 CSS 变量、至少两个前端相关的接口契约测试（字段齐、缺价为「没读到」）。
- **验收：** 用仓库自带 fixture 启动网页，该区块能看懂、没有编造数字、CSP 不报错。

### 练手 3：从本地 `catalog.json` 刷新价格（全栈：小后端 + 小前端）

- **你做：** 实现 `POST /api/prices/refresh`：读取使用者事先放到本机的 ModelLink `catalog.json`（先不上网），合并进本地价格缓存，失败则保留旧文件。页面上一个需要令牌和确认的「从本地目录更新」按钮。
- **助手给：** 路由骨架（先返回 501）、权限（令牌、Host）已接好、带假 `catalog.json` 的测试（成功合并、缺字段跳过、无令牌 401、坏文件不覆盖旧表）。
- **验收：** 测试全绿；你自己的一次手动刷新能在 `/api/prices` 里看到新的 `retrieved_at`。

### 练手 4（可选）：用你熟悉的框架重写「对照页」

- **你做：** 若想练 React / Vue / 甚至后来的 Spring，可以**另起** `webui-practice/` 或一个小 Spring 模块，只读同一套 `/api/*`，做一页「某模型三列对照」。不要替换现有 `webui/`，以免影响主路径。
- **助手给：** API 不变的保证、CORS 仍然只允许本机、一份最小的「如何启动对照页」说明。
- **验收：** 仍只绑 127.0.0.1；对照数字与主页一致。

想从哪一项开始，告诉主控即可。第一版 PR 会把骨架和测试留在分支上，不会挡住你之后提交。

---

## 9. 今天第一版 PR 的交付范围

已交 / 将在本计划之后继续补进 **同一个** PR #13（`feat/local-webui`，不合并）：

1. 本计划文件 `docs/local-webui-plan.md`。
2. 阶段 A 已有的核心库、API、清爽单页、安全门卫、测试、`docs/WEBUI.md`、fixture 截图。
3. 阶段 B 的核心实现与测试：本地三项反推、`rates.json` 新口径（ModelLink 主源字段、AFP 系数段、来源与日期）、等效人民币、禁止 5 万默认值、主表加列。
4. 阶段 C 练手 3 的路由骨架和失败测试（实现留给使用者）。
5. 练手 1、2 的模板、空区块和测试夹具。

**不进第一版：** 联网自动爬价格、把真实用量传到公网、Tauri / Spring 主路径、用 tokscale 或 LiteLLM 当积分账本、合并进 `main`。

## 10. 本地启动（回顾）

```bash
python3 scripts/webui.py
# 浏览器打开终端打印的 http://127.0.0.1:18765/
# 令牌在 ~/.config/workbuddy-account-migrate/webui-token（只报路径；本机用 cat 查看）
python3 tests/run_webui_tests.py
```

完整说明见 [docs/WEBUI.md](WEBUI.md)。
