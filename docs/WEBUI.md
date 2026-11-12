# 本机网页版

详细开发计划（阶段、API、数据模型、练手项）见 [local-webui-plan.md](local-webui-plan.md)。

在这台电脑的浏览器里看账号、会话和国内模型用量，也可以带令牌做账号同步。
**不是公网服务，也不上传到任何网站。** 服务只监听 `127.0.0.1`。

需要 Python 3.8+，只用标准库，没有额外依赖。用量解析对 3.8 可用；若你的环境更老，请先升级。

## 启动

在仓库根目录：

```bash
python3 scripts/webui.py
```

终端会打印：

- 浏览器地址，默认 `http://127.0.0.1:18765/`
- 本机写操作令牌的**文件路径**（不打印令牌本身）

第一次启动会生成令牌文件（权限 0600），默认在：

```text
~/.config/workbuddy-account-migrate/webui-token
```

查看令牌：

```bash
cat ~/.config/workbuddy-account-migrate/webui-token
```

只读浏览（账号、会话、用量）不需要令牌。点「同步账号」时，把令牌粘贴到网页里，勾选确认，并输入「确认同步」。

只生成令牌、不启动服务：

```bash
python3 scripts/webui.py --init-token
```

## 常用参数

| 参数 | 作用 |
|:---|:---|
| `--dir <路径>` | 指定 WorkBuddy 数据目录，优先级最高 |
| `--intl` | 默认看国际版 `~/.workbuddy-ai` |
| `--port 18765` | 换端口 |
| `--token-file <路径>` | 令牌文件位置 |
| `--rates <路径>` | 价格表 JSON，默认 `config/model_rates.json` |
| `--open` | 启动后尝试打开本机浏览器 |
| `--home <路径>` | 覆盖家目录，仅测试用 |
| `--bind` | 只能是 `127.0.0.1` 或 `localhost`。绑 `0.0.0.0` 会被拒绝 |

本机同时装了国内版和国际版时，默认读国内版，页面上可以切换。口径与 v1.7.1 的 CLI 相同。

## 页面能做什么

- **用量与价格对比**：按模型汇总 token、缓存命中率、去缓存效率（非缓存输入 + 输出，每积分多少 token）、总 token / 积分、实际积分，以及按价格表估算的人民币。
- **交叉校验**：把 jsonl 里的 `providerData.rawUsage.credit` 合计，和 `workbuddy.db` 里 `session_usage.credit_json` 对照，差额会写在表格下方。
- **会话列表**：标题、模型、每会话 token 和积分。不含对话正文。
- **CodeBuddy CLI**：若本机 `~/.codebuddy/projects` 里有同样结构的 jsonl，会一并统计；读不到会标明「没读到」。
- **账号同步**：调用现有的 `scripts/migrate.py`。和 CLI 一样，**必须先退出 WorkBuddy 客户端**，否则接口返回 409，不会改库。同步前会做备份。网页**不允许** `--force`。

## 安全注意

- 服务只绑定 `127.0.0.1`，并校验 `Host`（只准 `127.0.0.1` / `localhost`）。带非本机的 `X-Forwarded-Host` 或外站 `Origin` 会被拒绝。
- 写操作必须带请求头 `Authorization: Bearer <令牌>`，并且 JSON 里 `confirm` 为真、`confirm_text` 为「确认同步」。
- 不要把服务改绑到局域网或公网，不要把令牌、会话标题或用量报告发到网上。
- 用量默认只读，不改登录态文件（不会动 `account-snapshot.json`）。
- 日志会脱敏，不打印令牌内容。

## 价格表

`config/model_rates.json` 可以改。里面已经写入 2026-11-12 从官方页面核对过的数字：

- [定价详情](https://www.workbuddy.cn/docs/workbuddy/Pricing)：加量包 1000 积分 50 元，以及标准版月付折算。默认估算用加量包（每积分 0.05 元）。
- [WorkBuddy Mini 选择模型](https://www.workbuddy.cn/docs/workbuddymini/features/Select-Model)：部分模型的界面倍率。该页**没有**公布每百万 token 的积分，对不上或没写的模型会标「没读到」。

`per_million_token_credits` 默认是空的。官方未公布逐 token 费率，不要把未核对的数字填进去当成官方价格。若你用本机 `rawUsage` 自己核对过，可以按模型填写 `input` / `cached` / `output`（单位：积分 / 百万 token）。

## 开发与测试

```bash
python3 tests/run_webui_tests.py
```

全部走临时 fixture，不碰真实 `~/.workbuddy`。现有迁移测试不受影响，仍用原来的命令跑。

用量解析有一部分改编自 [workbuddy-usage-status](https://github.com/clancy-feng/workbuddy-usage-status)（MIT，Copyright (c) 2026 Clancy），版权声明见仓库根目录的 `NOTICE` 和 `THIRD_PARTY/workbuddy-usage-status.LICENSE`。

## 界面预览

`docs/screenshots/webui-usage.png` 是用临时 fixture 数据截的图（合成会话，不是真实用量，也不含令牌）。
