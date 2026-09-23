# 产品需求与架构设计文档 (PRD)

**项目名称**：`agy-multi`（兼容别名 `gemini-switch`） — Antigravity (agy) 多账号并发隔离管理与用量监控系统  
**文档版本**：与软件 v1.2.0 对齐  
**更新日期**：2026-09-23  
**状态**：已实施 (Implemented)  

---

## 1. 文档概述与产品定位

### 1.1 研发背景
Google Antigravity CLI (`agy`) 是新一代 AI 辅助编程终端工具。在日常高强度编程与多 Agent 协同任务中，开发者面临以下瓶颈：
1. **Google AI Pro 5小时配额限制**：Google 对每个 Pro 账号设有 5 小时滚动窗口的调用上限，高强度编码容易触发限流。
2. **多进程并发写冲突**：`agy` 默认将配置、OAuth Token、会话历史与 SQLite 数据库存放在统一的 `~/.gemini/antigravity-cli/`。在 `herdr` 或 `tmux` 开多个 Pane 并发运行时，多进程同时写入会导致 SQLite 锁冲突、Token 刷新互相覆盖与会话混乱。
3. **开发环境割裂**：若通过切换 Linux 系统用户隔离，会导致 `.gitconfig`、`.ssh` 密钥、开发工具链及工作区配置全部丢失。
4. **配额用量黑盒**：官方缺乏全账号统揽界面，开发者无法直观获知各账号当前 5 小时内已消耗多少 Token、何时释放额度，以及各模型调用的思维链 (Thinking) 与输出 (Output) 细分。

### 1.2 产品定位
`agy-multi` 是专为 `agy` 打造的**轻量级、多账号并发隔离与用量可视化平台**。通过单机多 Profile 沙箱软链穿透，实现多账号真正无锁并发；通过逆向解析底层 SQLite Protobuf 元数据，构建实时 5 小时滚动配额、周度消耗及动态重置倒计时的可视化看板。

### 1.3 适用范围与边界 (Scope & Non-Goals)
1. **仅限 CLI 终端交互环境**：本项目专门为 Antigravity CLI (`agy`) 终端工具定制，**明确不支持**桌面 GUI 应用程序（如 Antigravity 2.0 桌面端或 Antigravity IDE 独立窗口），GUI 应用由系统桌面管理器启动并使用系统级钥匙串，不受终端 `$HOME` 隔离控制。
2. **严禁反向代理与流量劫持**：本项目坚决不采用任何反向代理、中间人拦截或网络层中转机制。所有模型交互与通信均由原生 `agy` 进程直连，确保使用安全与合规。
3. **平台适用性**：以 Linux (Ubuntu/Debian/Fedora) 及 Windows WSL2 为主力支持与验证平台；macOS (CLI) 终端因缺少原生 `/proc` 且 Shell/权限机制有差异，定位于实验性/社区支持；Windows 原生环境目前不支持。

---

## 2. 系统总体架构与隔离设计

### 2.1 隔离架构原理
系统在宿主真实家目录（如 `/home/developer/`）下创建独立的沙箱管理目录 `~/.gemini-profiles/<profile_name>/`：

```mermaid
flowchart TD
    Host["宿主环境 (Real Home: /home/developer)"] --> Switch["agy-multi 管理器"]
    
    Switch --> P1["Profile 1: main (~/.gemini-profiles/main)"]
    Switch --> P2["Profile 2: coder (~/.gemini-profiles/coder)"]
    Switch --> P3["Profile 3: reviewer (~/.gemini-profiles/reviewer)"]
    Switch --> P4["Profile 4: research (~/.gemini-profiles/research)"]

    subgraph "沙箱隔离区 (完全独立)"
        P1 --> T1["专属 OAuth Token\n专属 SQLite 会话库\n独立 日志与缓存"]
        P2 --> T2["专属 OAuth Token\n专属 SQLite 会话库\n独立 日志与缓存"]
        P3 --> T3["专属 OAuth Token\n专属 SQLite 会话库\n独立 日志与缓存"]
        P4 --> T4["专属 OAuth Token\n专属 SQLite 会话库\n独立 日志与缓存"]
    end

    subgraph "开发环境穿透区 (软链共享)"
        Host --> S1[".gitconfig / .ssh 密钥"]
        Host --> S2[".local / bin 工具链"]
        Host --> S3["workspace 项目工作区"]
        Host --> S4[".gemini/config/hooks (Herdr 监控原生透传)"]
    end
```

### 2.2 核心技术机制
1. **独立登录态与存储**：
   每个 Profile 独立持有 `.gemini/antigravity-cli/antigravity-oauth-token` 与 `conversations/*.db`，彻底消除并发写锁冲突与串号问题。
2. **透明环境穿透**：
   通过 `sync_profile_environment` 自动将宿主根目录下的配置文件以软链接形式挂载进各 Profile 目录，保持 Git 身份、SSH 鉴权与环境变量无感知一致。
3. **Herdr 监控原生兼容**：
   主动穿透 `~/.gemini/config/hooks`，在 `herdr` 多 Pane 并发运行时，侧边栏能够精准捕获每个 Pane 中 Agent 的思考、执行与空闲状态。
4. **真实家目录防退化机制 (`AGY_REAL_HOME`)**：
   在子 Profile 会话内部运行时，`$HOME` 被修改为 Profile 目录。系统内置智能回退探测：若检测到当前 `HOME` 位于 `.gemini-profiles/` 路径内，自动向上推导真实宿主目录，确保子环境中运行 `agy-multi` 依然正常识别全局账号列表。
5. **快捷指令矩阵**：
   通过 `agy-multi install` 向 `~/.local/bin` 注册全局快捷脚本：
   - ID 命令：`agy-1`, `agy-2`, `agy-3`, `agy-4`
   - 别名命令：`agy-main`, `agy-coder`, `agy-reviewer`, `agy-research`

---

## 3. 用量监控看板 (Usage Dashboard) 设计

### 3.1 数据逆向工程与采集方案
通过对 `agy` 底层存储机制的深入逆向，定位到模型每次交互均持久化于 `conversations/<conversation_id>.db` 的 `steps` 表中。

```mermaid
sequenceDiagram
    participant User as 用户 / Herdr
    participant CLI as agy 进程
    participant Backend as Google Gemini 平台
    participant DB as conversations/id.db
    participant Monitor as agy-multi usage

    User->>CLI: 输入 Prompt
    CLI->>Backend: 发送对话请求
    Backend-->>CLI: 流式响应 (含 UsageMetadata)
    CLI->>DB: 写入 steps 表 (Protobuf 二进制元数据)
    Note over DB: field 1: 精确时间戳<br/>field 9.2: Prompt Tokens<br/>field 9.3: Candidate Tokens<br/>field 9.9: Thinking Tokens<br/>field 9.10: Output Tokens
    Monitor->>DB: 本地只读解码 Protobuf 提取指标
    Monitor-->>User: 终端表格 + 动态 HTML 看板
```

用量分两路，互不替代：

- **本地历史**：只读 `conversations/*.db`，解码 steps 里的 Protobuf 元数据，得到请求数和 Token。这一路不把对话内容发到项目自己的服务器。
- **官方实时配额**：用该 Profile 已有的 access token 调用 `retrieveUserQuotaSummary`，读取 Gemini 与 Claude/GPT 的周额度、5 小时额度。令牌过期时在后台用 refresh token 刷新，页面不展示令牌到期时间。
- **订阅档**：同一次刷新里调用 `loadCodeAssist`，把 `paidTier` / `currentTier` 收成 Free、Pro、Ultra。接口没有会员账单截止日期。

### 3.2 核心业务指标定义
| 维度 | 指标项 | 说明 |
|:---|:---|:---|
| **请求量** | `requests` | 模型交互轮数（Turns），反映调用频度 |
| **输入 Token** | `prompt_tokens` | 上下文、系统指令与用户 Prompt 所占 Token |
| **思维 Token** | `thinking_tokens` | Gemini 思考模型（Reasoning）推导过程 Token |
| **输出 Token** | `output_tokens` | 模型最终生成答复的 Token |
| **总 Token** | `total_tokens` | `prompt_tokens + candidate_tokens`（含思考与输出） |

### 3.3 5 小时滚动配额与重置倒计时算法
Google AI Pro 采用 5 小时滑动窗口（Rolling Window）限制配额。系统针对每个账号实现以下时间窗口计算模型：

1. **窗口时间阈值**：
   $$\text{Threshold}_{5h} = \text{CurrentTime} - 5 \times 3600$$
2. **5小时窗口内使用量**：
   统计所有满足 $\text{Timestamp} \ge \text{Threshold}_{5h}$ 的 Step 记录。
3. **首个请求恢复倒计时 (Next Reset Countdown)**：
   $$\text{NextResetTS} = \min(\text{Timestamps}_{5h}) + 5 \times 3600$$
   $$\text{Countdown}_{\text{next}} = \max(0, \text{NextResetTS} - \text{CurrentTime})$$
   *业务意义*：距离 5 小时前最早一笔请求释放、开始恢复额度的精确剩余时间。
4. **完全重置倒计时 (Full Reset Countdown)**：
   $$\text{FullResetTS} = \max(\text{Timestamps}_{5h}) + 5 \times 3600$$
   $$\text{Countdown}_{\text{full}} = \max(0, \text{FullResetTS} - \text{CurrentTime})$$
   *业务意义*：若不再发起新请求，账号配额完全恢复为 100% 满额的时间。
5. **满额就绪状态**：
   若 $\text{requests}_{5h} = 0$，状态直接标记为 `100% 额度就绪`。

### 3.4 7 天周度趋势模型
- 以当前自然天为基准，统计近 7 天每日的请求数与 Token 消耗。
- 动态计算周内每日最高消耗量作为比例标尺，渲染迷你柱状图（Sparkline）。

### 3.5 看板界面 (UI/UX) 规范
- **视觉风格**：深色模式。官方额度用胶囊进度条，颜色按余量变化；周额耗尽导致 5 小时桶停用时，进度条为红色。
- **顶栏**：看板上的账号数、就绪数、周额耗尽数、保底余量、近 5 小时 / 7 天 / 全周期 Token。这些数字只统计勾选进看板的账号。
- **账号卡片**：ID、别名、邮箱、登录与额度状态、活跃进程。每张卡片有 Gemini 与 Claude/GPT 两组额度，每组包含周额度与 5 小时额度，以及本地 Token 历史。
- **周额耗尽判定**：官方余量低于 1% 视为耗尽。页面上四舍五入成 0% 时，状态不得再写成「可用」。这样的账号也不能作为 Gemini 接力目标。
- **账号清单**：单独一页，列出全部 Profile、订阅档、登录状态、是否地区受限。每行可勾选「额度看板」。勾选结果写入 `accounts.json` 的 `show_on_dashboard`。未单独设置时，地区受限账号默认不进看板。
- **会话明细与走势**：按账号过滤；「全部」只合并看板上的账号。
- **动态交互**：打开页面即请求 `/api/usage`；倒计时在页面内刷新；可导出 JSON。

### 3.6 账号是否进入看板

| 条件 | 是否出现在额度看板和汇总 |
|:---|:---|
| `show_on_dashboard` 已显式设置 | 按该值 |
| 未设置，且 `region_restricted` 为真 | 否 |
| 未设置，且未标地区受限 | 是 |

---

## 4. 命令行接口 (CLI) 规范

| 命令 | 参数 | 描述 |
|:---|:---|:---|
| `agy-multi list` (或 `ls`) | 无 | 查看所有 Profile 状态、认证邮箱、活跃 PID |
| `agy-multi usage` (或 `stats`) | `--html [path]`, `--json` | 打印终端统计概览，并自动更新/生成 HTML 看板 |
| `agy-multi serve` | `--port [P]`, `--host [H]` | 启动实时 Web 监控看板服务（默认 127.0.0.1:8989） |
| `agy-multi run` | `<ID\|Name> [agy_args...]` | 在指定 Profile 沙箱中启动 `agy`（支持透传任意原生参数） |
| `agy-multi login` | `<ID\|Name>` | 触发指定 Profile 的一次性 Google OAuth 浏览器认证 |
| `agy-multi add` | `<name> <email> [-d desc] [--id ID]` | 注册新 Profile 并初始化沙箱软链 |
| `agy-multi edit` | `<ID\|Name> [--name N] [--email E] [--region-restricted]` | 修改别名、邮箱、描述，或标记地区受限 |
| `agy-multi install` | 无 | 生成并同步全局 `agy-N` 快捷命令到 `~/.local/bin` |
| `agy-multi relay` | `[conversation_id] [--from] [--to]` | 把会话迁到另一个有额度的 Profile 并继续 |
| `agy-multi config` | `--min-buffer` `--auto-relay` `--on-no-target` | 查看或修改保底余量、自动接管、无目标时的策略 |
| `agy-multi creds` | `--save` | 从本机 `agy` 发现 OAuth 客户端配置并写到本机环境文件，不写入仓库 |

---

## 5. 示例配置账号与状态矩阵

| ID | 快捷指令 | 账号别名 | 示例邮箱 | 认证状态 | 5H 状态 | 说明 |
|:---|:---|:---|:---|:---|:---|:---|
| **1** | `agy-1` / `agy-main` | `main` | `main@example.com` | ● Logged In | Ready (100%) | 主开发账号 |
| **2** | `agy-2` / `agy-coder` | `coder` | `coder@example.com` | ● Logged In | Ready (100%) | 辅助开发账号 |
| **3** | `agy-3` / `agy-reviewer` | `reviewer` | `reviewer@example.com` | ● Logged In | 动态倒计时 | 测试审查账号 |
| **4** | `agy-4` / `agy-research` | `research` | `research@example.com` | ○ Not Auth | Ready (100%) | 备用研究账号（待认证） |

---

## 6. 质量保证与测试体系

### 6.1 自动化测试矩阵 (`pytest`)
测试在 `tests/test_creds.py`、`tests/test_manager.py`、`tests/test_runner.py`、`tests/test_server.py`、`tests/test_usage.py`。覆盖 Profile 生命周期、会话接力与 SQLite 备份、官方配额可用性（含周额低于 1%）、订阅档解析、看板可见性、OAuth 配置不入库，以及 Dashboard HTTP 鉴权。

---

## 7. 已交付与尚未做的事

v1.2.0 已经包含：多 Profile 隔离、`agy-auto` 接力、`agy-multi serve`、官方配额与订阅档、账号清单勾选进看板。

尚未做的是配额恢复后的桌面通知或外部 Webhook。本项目仍然不做反向代理。
