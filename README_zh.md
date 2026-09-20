# agy-multi

> 告别 5 小时配额墙，会话永不中断。

专为 Google Antigravity CLI (`agy`) 打造的并发多账号隔离与实时用量看板。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-green.svg)](https://github.com/awu5425/agy-multi)
[![GitHub stars](https://img.shields.io/github/stars/awu5425/agy-multi?style=social)](https://github.com/awu5425/agy-multi)

[English](README.md) | [简体中文](README_zh.md)

---

![agy-multi 看板预览](docs/assets/dashboard_preview.png)

> 🎬 **演示**: *配额耗尽瞬间，`agy-auto` 秒级无缝交接活跃会话至下一个账号 — 30 秒演示动图制作中。*

---

## 核心痛点

你正沉浸在长达 40 分钟的深度代码重构中。Gemini 突然撞上了 5 小时滚动配额墙。此时你只有两个选择：干等倒计时结束，或者新开一个会话把所有上下文重新解释一遍。

**agy-multi 给你第三种选择。** 让每个 `agy` 会话运行在独立的账号沙箱中。一旦当前账号配额耗尽，看门狗会优雅地将你的活跃会话 — 对话历史、上下文、Brain 产物全部完整保留 — 自动交接给下一个账号。同一个终端窗口，零上下文丢失。

---

## 核心特性

- 🔄 **配额看门狗 + 自动接力** — 后台监控官方 5 小时滚动配额；配额耗尽瞬间通过 SIGINT 安全保存，利用 SQLite 官方 Online Backup API 逐页备份，并在同一终端自动唤醒最空闲账号继续执行。
- 📊 **实时用量监控看板** — 5 小时 / 周配额进度条与精确到秒的重置倒计时，包含 Read / Write / Cache / Thinking 多维视角的 Token 消耗趋势，GitHub 风格活跃度日历，中英文一键切换。
- 🔒 **真正的单账号隔离沙箱** — 每个 Profile 拥有独立的 OAuth Token、会话日志与 SQLite 数据库；多终端并发绝无锁冲突。默认严格排除敏感目录（`.ssh`, `.gnupg`, `.aws`, `.azure`, `.kube`, `.docker`）。
- 🖥️ **兼容任意终端与分屏工具** — 普通标签页、独立窗口、VS Code 内置终端、tmux、zellij 等无缝支持。

*独立的社区开源项目 — 与 Google 无官方关联。详见 [免责与合规声明](#免责与合规声明)。*

---

## 快速上手 (2 分钟)

### 1. 安装与初始化

```bash
git clone https://github.com/awu5425/agy-multi.git
cd agy-multi
./install.sh && agy-multi init
```

向导会自动将你现有的 Antigravity 登录态导入为 Profile 1（主账号），引导添加副账号，并在 `~/.local/bin` 中注册全局快捷别名（`agy-1`, `agy-2`, `agy-auto`）。

### 2. 启动带看门狗守护的会话

```bash
agy-auto                                            # 推荐：自动选取最空闲账号启动，配额耗尽自动接力
agy-1                                               # 启动指定 profile
agy-coder -p "审查本仓库中未解决的 TODO"              # 原生 agy 参数均可透传
```

### 3. 查看配额与用量

```bash
agy-multi status    # 查看各账号配额、看门狗策略与下一个接力候选
agy-multi serve     # 本地 Web 看板 → http://127.0.0.1:8989
```

---

## 接力机制工作原理

1. **启动与就绪** — `agy-auto` 依据可用配额综合评分，自动选出最优空闲账号启动。
2. **运行时看门狗** — 后台监控轮询官方配额接口；当剩余配额 ≤ 设定的保留缓冲值（默认 0%，推荐 5%）时触发交接。
3. **优雅保存** — 向会话发送 SIGINT 中断信号，随后通过 SQLite 官方 Online Backup API 逐页备份数据库。绝无 WAL 损坏，拒绝粗暴的文件直接复制。
4. **智能目标优选** — 仅筛选当前活跃 PID 为 0 的空闲账号；正在其他终端忙碌的账号绝不会被误选。
5. **无缝重载** — 会话数据库、元数据与 Brain 产物平滑迁移；新会话在同一个终端窗口直接拉起。
6. **安全退出** — 在等待冷却期间，若发现会话已在其他地方被接管，supervisor 将以状态码 0 干净退出，杜绝脑裂。

> 每次接力均获取排他性 `fcntl` 文件锁 (`relay.lock`)，即使多个触发源同时激活也绝不发生竞态。

---

## 监控看板

```bash
agy-multi serve                                        # 仅限本地访问 (默认 127.0.0.1:8989)
agy-multi serve --port 8989
agy-multi serve --host 0.0.0.0 --port 8989 --token YOUR_SECURE_TOKEN  # 局域网 / 远程访问
```

- **配额一览** — ♊ Gemini 5 小时 + 周配额进度条，精确到秒的重置倒计时；🧠 Claude & GPT 周配额进度与耗尽预警。
- **Token 深度分析** — 趋势曲线支持 Total / Read·Write·Cache / Thinking·Output 视图，线性 ↔ 对数刻度自由切换，GitHub 风格活跃度日历支持悬浮查看明细（Prompt / Thinking / Output / 请求次数）。
- **一键接力** — 点击任意会话卡片上的 🚀 接力 按钮即可立即迁移。
- **设置面板** — 配额保留缓冲滑块（0–50%，步长 0.5%）、自动接管开关、实时保存生效。
- **接力生命周期图** — 全屏查看 5 阶段交接时序流程图。
- **终端导出** — `agy-multi usage`，支持 `--csv`、`--html`、`--json` 静态导出。

> 🔐 **安全性**：默认仅监听本地回环地址；一旦绑定非 127.0.0.1 地址，所有端点（包括 GET）均强制要求鉴权。未指定 `--token` 时将自动生成高强度 Token 并打印在控制台。OAuth 凭据文件权限为 `0600`，目录权限为 `0700`。

---

<details>
<summary><b>🧩 多终端并发工作流示例</b></summary>

在任意终端环境中多开会话（普通标签页、IDE 分屏终端、tmux / zellij）：
- **终端 1** — `agy-1`：核心代码编写与重构
- **终端 2** — `agy-2`：并发运行测试与 Code Review
- **终端 3** — `agy-3`：技术调研与系统架构设计

每个会话独立消耗各自账号的 Pro 配额，拥有独立的 SQLite 数据库 — 彻底杜绝锁冲突与跨账号速率限制拥堵。

</details>

<details>
<summary><b>⌨️ 完整命令速查</b></summary>

```bash
agy-multi list | ls                  # 查看所有 profile、鉴权状态与活跃 PID
agy-multi login 1                     # 针对指定账号执行 Google OAuth 授权
agy-multi add coder coder@ex.com -d "重构负责人"
agy-multi edit 2 --name coder-pro --email newcoder@ex.com
agy-multi run [profile]               # 启动账号会话（等同于 agy-auto）
agy-multi relay                       # 智能自动接力当前活跃会话
agy-multi relay --to 2                # 定向接力至指定账号
agy-multi relay <cid> --from 1 --to 2 # 显式跨账号迁移指定会话
agy-multi relay --no-exec             # 仅同步会话与 Brain 数据，不启动 agy
agy-multi relay --list-candidates     # 列出各候选账号的配额得分、5H 状态与空闲情况
agy-multi config                      # 查看当前配置
agy-multi config --min-buffer 5       # 设置保留缓冲配额 % (0–50)；达到阈值即触发交接
agy-multi config --on-no-target pause # 无可用账号策略：pause (默认：打印倒计时并自动唤醒) | burn_buffer (消耗缓冲至 429)
agy-multi usage [--csv] [--html PATH] [--json]
```

</details>

<details>
<summary><b>🖥️ 平台支持矩阵</b></summary>

| 功能 / 特性 | Linux (主力平台) | macOS (CLI) | Windows 原生 | 桌面 GUI 客户端 |
| :--- | :---: | :---: | :---: | :---: |
| Profile 与 Token 沙箱隔离 | ✅ 完全支持 | ⚠️ 基础支持 | ❌ 不支持 | ❌ 不支持 |
| 进程状态与 PID 追踪 | ✅ 原生 (/proc) | ⚠️ 基础 (ps/pgrep, 受 TCC 限制) | ❌ 不支持 | — |
| 5H 配额看门狗与自动接力 | ✅ 完全支持 | ⚠️ 基础支持 | ❌ 不支持 | ❌ 不支持 |
| Web 实时用量看板 | ✅ 完全支持 | ✅ 完全支持 | ✅ 完全支持 | ⚠️ 仅 CLI 日志 |
| 后台守护进程 | ✅ systemd | ⚠️ 手动 launchd | ❌ 不支持 | ❌ 不支持 |

> macOS CLI 支持为实验性并由社区驱动。Windows 用户请在 WSL2 中使用以获得完全一致的体验。桌面 GUI 客户端（Antigravity 2.0 / IDE）不在支持范围内 — 其凭据保存在系统钥匙串中，无法通过终端 `$HOME` 沙箱隔离。

</details>

---

## 常见问题 (FAQ)

### 这是否违反 Google 服务条款？
`agy-multi` 是一款独立的本地工具，直接调用 Google 官方 API — 绝无反向代理、无中间人、无流量劫持。本工具专为管理自己正当合法账号（个人、企业、客户）的开发者打造。用户须自行遵守 Google 服务条款；本项目不鼓励或纵容滥用配额或未授权的多账号行为。

### 为什么不直接注销重新登录？
因为重新登录会导致当前工作区状态和对话上下文丢失 — 且两个并发会话在同一个 Profile 下会发生 SQLite 写锁冲突。`agy-multi` 为每个账号分配独立沙箱，并将活跃会话无缝迁移。

### 是否支持 macOS / Windows？
macOS 终端：实验性支持。Windows 原生：不支持 — 请在 WSL2 中使用。

### 是否支持 Antigravity 桌面应用 / IDE？
不支持，仅限 CLI — GUI 桌面端将凭据保存在系统钥匙串中，无法通过终端 `$HOME` 沙箱或进程信号进行控制。

### 如果所有账号配额都耗尽了怎么办？
由兜底策略决定：`pause`（默认）会打印最早重置的账号倒计时并自动挂起等待唤醒；`burn_buffer` 则会继续消耗预留缓冲直至触发 429。

---

## 开发规划

- [ ] Claude / GPT 多日配额自动接力（看板已支持用量追踪）
- [ ] 社区共建的 macOS 深度支持
- [ ] 欢迎提交 Issue 与 PR 分享你的需求

---

## 免责与合规声明

### 法律与合规声明

1. **独立项目**：`agy-multi` 为独立的社区开源开发者工具，与 Google LLC、Alphabet Inc. 或其任何子公司无任何关联、授权、维护、赞助或认可。所有 Google 商标、服务标识和徽标均为 Google LLC 的资产。
2. **合法开发者用途**：本工具严格面向在本地开发环境中管理自身多个正当合法 Google 账号（如个人、企业 Workspace 及客户项目账号）的个人开发者或经授权的工程团队。
3. **服务条款合规**：用户应全权负责确保其使用多个 Google 账号的行为符合 [Google 服务条款](https://policies.google.com/terms)、[生成式 AI 附加条款](https://policies.google.com/terms/generative-ai) 及适用的可接受使用政策。项目维护者不鼓励或纵容任何滥用配额、绕过速率限制或未授权的多账号行为。
4. **责任限制**：本软件按“现状”提供，不提供任何明示或暗示的保证。在任何情况下，作者或版权持有人均不对因使用本软件而引起的任何索赔、损害、账号封禁或服务中断承担责任。

---

## 开源协议

本项目采用 [MIT 许可证](LICENSE)。
