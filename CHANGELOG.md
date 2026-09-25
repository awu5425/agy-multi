# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-09-25

### Added
- **原生 Windows 跨平台完整支持 (Native Windows Platform Support)**：
  - **凭据与环境彻底隔离 (Credential & Profile Isolation)**：Windows 下全面同步设置 `%USERPROFILE%` 与 `%HOME%` 指向 Profile 沙箱目录，解决 Windows 版 `agy.exe` 仅读取 `%USERPROFILE%` 导致多账号凭据串扰的底层痛点，实现 100% 账号隔离。
  - **NTFS 目录联接免提权支持 (NTFS Directory Junctions)**：采用底层 `_winapi.CreateJunction` 创建目录联接，彻底规避 Windows 普通用户无法创建软链接的权限限制（`WinError 1314: 客户端没有所需的特权`），支持在非管理员环境下秒级共享 skills、mcp、plugins 等配置，并自带优雅的深复制降级保护。
  - **跨平台安全进程探活 (Safe Process Liveness Checking)**：全面规避 Windows 下 CPython 执行 `os.kill(pid, 0)` 会直接映射为 `TerminateProcess(hProcess, 0)` 导致进程瞬间自杀/猝死的平台深坑。统一采用 Win32 API `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` 与 `GetExitCodeProcess` 进行无损安全探活。
  - **跨平台排他文件锁 (Cross-Platform File Lock)**：实现零外部依赖的 `file_lock` 上下文管理器，Windows 平台基于 `msvcrt.locking`，POSIX 平台基于 `fcntl.flock`，为全局 Supervisor 注册表与接力锁（`relay.lock`）提供坚如磐石的并发防撕裂保护。
  - **Sentinel 哨兵文件 IPC 接力协议 (Sentinel File IPC Relay)**：针对 Windows 内核缺乏 POSIX 信号机制的问题，重构 IPC 通信为高响应度 Sentinel 哨兵文件机制（`relay_cmd_{pid}.json`），看门狗以 250ms 子切片轮询，实现 Web 看板与后台守护进程对运行中会话的原生秒级自动接力。
  - **Windows Terminal 原生集成 (`agy-multi wt`)**：新增 `wt` / `split` 命令行，原生集成 `wt.exe`，支持根据 `$WT_SESSION` 自动注入 `-w 0` 复用当前窗口，支持垂直分屏 (`--split v`)、水平分屏 (`--split h`) 与新建标签页 (`--tab`) 极速拉起独立账号会话。
  - **原生 Windows 批处理快捷脚本生成 (`cmd_install_helpers`)**：在 `~/.local/bin` 中同步生成原生 `.cmd` 快捷调用脚本（`agy-multi.cmd`, `agy-auto.cmd`, `agy-<id>.cmd`, `agy-<name>.cmd`），在 CMD 与 PowerShell 中免敲 `python -m` 直接全局调用。
  - **Dedicated Windows 单元测试套件 (`tests/test_windows.py`)**：新增 10 项专门针对 Windows 平台特性的自动化测试，覆盖 NTFS Junctions、进程安全探活、USERPROFILE 隔离、msvcrt 并发锁、Sentinel IPC 及 Windows Terminal 命令生成。全量测试用例扩充至 57 项，通过率 100%。

## [1.4.0] - 2026-09-25

### Added
- **Web 看板极客流光接力动效 (High-Tech Energy Beam Relay Transfer Animation)**：
  - 接力弹窗内引入动效状态轨道（`relay-anim-container`）：源账号与目标账号节点自适应发光脉冲（`pulse-src-glow`），配合高能电荷光束（`relay-beam-progress` 与 `relay-beam-spark` ⚡ 粒子抖动）实时展现接力三阶段推进过程（1/3 脑图与 SQLite 上下文热迁移 ➔ 2/3 终端分屏 IPC 调度与续跑注入 ➔ 3/3 目标账号接管成功）。
  - 接力成功目标节点转为翠绿高亮脉冲（`active`）并呈现成功标识，大幅强化 Web 接力交接仪式感与操作反馈。
- **终端多路复用器分屏与标签标题全生命周期自动命名 (Full-Lifecycle Terminal Pane & Tab Auto-Naming)**：
  - 新增 `set_terminal_pane_title` 统一接口，按环境自适应联动分屏管理器：
    - `Herdr`：双重重命名分屏与标签栏（`herdr pane rename <PANE_ID>` + `herdr tab rename <TAB_ID>`），解决此前仅修改子分屏而顶部 Tab Bar 仍显示旧名称的视觉脱节；
    - `Tmux / Rmux`：自动识别 `$TMUX` 与 `$TMUX_PANE`，执行 `tmux select-pane -t <pane> -T <title>` 设置分屏标题；
    - `Universal ANSI / OSC 2`：向终端 `sys.stdout` 发射 `\033]2;<title>\007` 控制序列，原生适配 Orca、WezTerm、Alacritty、GNOME Terminal、iTerm2 等终端模拟器。
  - **全生命周期账号标识自适应联动**：
    - **登录/认证时**：`agy-multi login <id>`（或 `auth`）触发时自动打标 `agy: <name> [P<id>] (authenticating)`，成功后转为 `agy: <name> [P<id>]`；
    - **会话运行时**：`agy-multi run <id>`、`agy-<id>`、`agy-<name>` 启动时打标 `agy: <name> [P<id>] • <cid[:8]>`；
    - **会话退出时**：退出保留账号标识 `agy: <name> [P<id>] [idle]`，防止会话结束后账号归属信息丢失；
    - **快捷切换与打标命令**：新增 `agy-multi use <id>`（别名 `switch`, `pane-title`），支持在任意空白分屏快速设定或切换当前分屏绑定的账号标签。
- **Web 看板一键就地全自动接力 (Dual-Tier In-Place Auto-Relay: Supervisor IPC + Multiplexer Fallback)**：
  - **Tier 1: Supervisor 进程内温和接管 IPC**（零击键注入风险、跨终端通用）：
    - `SessionRunner` 启动时自动将 PID、Profile 与分屏上下文登记至 `~/.gemini-profiles/active_supervisors.json`（配合文件排他锁与僵死进程自愈修剪）。
    - Web 看板点击「确认接力」发起 `/api/relay`，服务端完成会话上下文与脑图安全迁移后，智能探测源分屏活跃 Supervisor，写入接力指令并向其派发 `SIGUSR1` 信号。
    - 运行中终端的 Supervisor 捕获信号后，向运行中的 `agy` 发送 `SIGINT` 温和安全存盘退出，原地热切换环境变量与分屏标题，并自动拉起 `agy --conversation <cid>` 继续会话，无需人工复制粘贴命令。
  - **Tier 2: 终端复用器智能注入兜底 (Multiplexer Injection Fallback)**：
    - 当历史会话未被 Supervisor 托管（如直接启动 `--direct`、升级前旧进程，或 `agy` 已退出至 shell bash prompt）：
    - 自动根据系统活跃进程 `/proc/<pid>/environ`、`conversation_summaries.db` 工作区路径绑定以及 `Herdr` / `Tmux` 运行中分屏属性（`agent_session`、`label`、`title`、`cwd`、`focused`）智能多级评分，精准命中目标分屏；
    - 向目标分屏精准调度执行：发送中断清除当前输入行，通过 `send-text` 避免括号粘贴模式（Bracketed Paste Mode）冲突，注入新账号快捷命令（如 `agy-5 --conversation <cid>`）并敲击回车启动，同步刷新分屏标签，实现 100% 全覆盖的无人值守全自动接力。
  - Web 看板前端与静态快照同步增加 `auto_switched` 状态提示与 i18n 国际化，当指令成功下发到分屏终端时以绿色醒目标识展示分屏自动接续状态与分屏 ID（如 `[分屏: wC:p2]`），完全折叠手动复制框。
- **Web 看板近期会话明细默认精简 10 条展示与展开/收起联动 (Dashboard Recent Conversations Limiting & Expand)**：

  - 会话列表自适应聚合过滤：统一按最新活跃时间（`last_modified`）倒序排序，确保全账号及各独立账号视图均呈现最新的会话。
  - 默认仅展示最近 10 条会话记录，避免长会话列表拉垮页面篇幅，显著提升信息密度与阅读体验。
  - 表格底部新增展开/收起按钮组件（`展开更多会话 (已展示 10 / 共 X 条) ▼` 与 `收起会话 (仅保留最近 10 条) ▲`），支持一键延展查看全部记录并可随时按需收起。
  - 切换账号 Tab 时自动恢复为精简 10 条视图，并完整覆盖中英文 i18n 国际化。
- **Web 看板接力候选仅展示在榜账号 (Relay Candidates Hidden Account Filtering)**：
  - 接力弹窗与候选列表（`openRelayModal` 与 `/api/relay/candidates`）严格过滤隐藏账号（`show_on_dashboard: false`），仅列出在看板上展示且处于登录就绪状态的有效账号，避免未在看板展示的账号干扰接力目标选择。
  - 自动推荐逻辑（`find_best_relay_candidate`）支持 `require_visible_on_dashboard=True`，杜绝后台将任务自动调度至看板隐藏账号。
- **Web 看板空闲账号接力按钮智能置灰联动 (Idle Account Relay Button Disabling)**：
  - 当账号处于空闲状态（无活跃进程 `active_pids` 时），卡片右下角「⚡ 账号接力」按钮及 5H 冷却快捷接力按钮自动置灰禁用（`.disabled` + `cursor: not-allowed`），并展示中英文悬浮提示「账号当前处于空闲状态（无运行中任务），无法发起接力」，杜绝空闲无任务时误点接力。
  - 前端控制器函数（`openAccountRelay`）增加防御拦截，空闲账号杜绝打开接力弹窗。
- **跨账号接力自动带入续跑指令与身份宣告 (Auto-Resume Interactive Handoff Execution)**：
  - 针对接力后新账号仅静默停在 `>` 输入框未自动接跑的问题：在 `SessionRunner` 热切换拉起目标账号时，自动注入 `--prompt-interactive`（`-i`）并带入系统接力提示词（`【系统接力就绪】会话已由系统平滑接力至账号 [xxx] (ID: x)。请检查上一棒执行状态并自动继续推进完成当前任务。`）。
  - 新账号启动即自动向用户做出交接声明并继续推进任务，彻底消除“后台已换人但终端中断停滞”的断层感。





### Fixed
- **Claude & GPT 5H 进度条语义与停用联动**：当 Claude & GPT 周配额耗尽（< 1%）或 5H bucket 被停用时，5H 进度条同步判定为已停用，进度条颜色由绿变为红色（`fill-red`），胶囊状态与文本显示为「🚫 已停用 (0%)」，消除周额耗尽但 5H 仍显示绿色 100% 的误导问题。修复同时对齐 Web 看板动态服务与静态模板。
- **CLI 终端配额表格 5H 停用联动**：在 `agy-multi usage` 与 `agy-multi relay` 中，当周配额耗尽时，5H 剩余量明确标示为红字 `Disabled`，不再显示 `100.0%`。



## [1.3.0] - 2026-09-24

### Added
- **配置与技能安全继承与克隆机制 (`--inherit`, `clone`, `inherit`)**：
  - 新增 `agy-multi add <name> <email> [--inherit | --inherit-from <source>]` 支持从宿主环境或指定已有分身继承配置。
  - 新增 `agy-multi clone <source> <name> <email>` 独立克隆分身，深拷贝 Agent Skills、Plugins、MCP 服务配置与 settings.json。
  - 新增 `agy-multi inherit <target> [--from <source>]` 为现有分身同步或补齐技能与配置。
  - **严密安全红线防护**：建立配置安全白名单，强制过滤并剔除敏感密钥、OAuth Token、Google 账号信息，会话与对话库（`brain/`、`conversations/`、`conversation_summaries.db*`）物理硬隔离绝不交叉，全量应用 `0700/0600` 所有者专属权限。
- **纯标准库 JWT 解码与 Token 临期预警 (Token Expiry Intelligence)**：
  - 零外部依赖解析 Google OAuth ID Token 与 JWT Payload，精确提取 `exp` 到期时间戳。
  - 新增 `evaluate_token_expiry`：自动识别有效状态、具备 Refresh Token 的自动刷新态、7天内临期告警态与过期失效态。
  - 支持向后兼容与备选探测：当缺少标准 `antigravity-oauth-token` 时，自动兜底探测解析 `jetski-standalone-oauth-token`。
  - **CLI 与 Web 看板状态联动**：
    - `agy-multi list` 与 `agy-multi status` 终端表格实时显示 Token 到期时间与健康状态（如 `● Logged In (Auto)`、`⚠️ Expiring (3.0d)`）。
    - Web Dashboard 账号卡片与管理抽屉新增 Token 状态徽章（`自动刷新就绪`、`⚠️ 临期 (Xd)`），临期自动触发卡片顶部警示。

### Removed
- **彻底废除 `gemini-switch` 别名与所有旧命令**：
  - 移除 `pyproject.toml` 中的 `gemini-switch` 入口脚本。
  - `agy-multi install` 与 `install.sh` 停止生成 `gemini-switch` 包装别名，并自动清理残余的 `~/.local/bin/gemini-switch`。
  - 项目所有管理、隔离运行与工具链命令统一为 `agy-multi`，不再保留或提及 `gemini-switch`。

## [1.2.0] - 2026-09-23

### Added
- **订阅档位与账号清单**：额度刷新时额外调用 `loadCodeAssist`，在账号清单里显示 Free / Pro / Ultra。清单里可以勾选哪些账号进入额度看板；未单独设置时，地区受限账号默认不进看板，也不计入看板上的汇总。令牌到期时间仍只在后台刷新，不展示给用户。

### Fixed
- **近零周额仍显示可用**：官方配额常在周额实际耗尽后留下不足 1% 的余量（例如 0.37%，页面四舍五入为 0%）。可用性判断与看板状态原先只把 `remainingFraction == 0` 视为耗尽，于是状态仍写成「可用 (周: 0% | 5H: 100%)」。现在与进度条一致，周额低于 1% 即视为耗尽，不再标成可用，也不会被选为接力目标。
- **Dashboard 双版本数据不同步**：`usage_dashboard.html`（静态快照）的 `window.onload` 改为 `async`，页面打开时立即调用 `/api/usage` 覆盖旧硬编码快照数据，消除昨日数据残留（个别账号的额度与进度条颜色和实际状态不符）。`agy_multi/usage.py` 动态模板同步相同修复，确保两个版本行为一致。
- **Gemini 5H 进度条颜色语义错误**：当 `show5hAsDisabled=true`（因 Gemini 周额耗尽导致 5H bucket 被停用）时，进度条颜色从灰色（`fill-disabled`，易误认为"未登录"）改为红色（`fill-red`），与账号未认证状态明确区分。修复同时应用于 `usage_dashboard.html` 和 `agy_multi/usage.py`。

### Added
- `AGENTS.md`：项目级多 Agent 协作规范（会话接续、CI 约定、阶段检查点、工作区洁净）。
- `HANDOFF.md`：项目交接文件，记录当前状态、待办和环境依赖，供多 Agent 会话接续使用。

---

## [1.1.1] - 2026-09-21


### Added
- **Tailscale & LAN Network Support**: Dashboard server now supports non-loopback binding (`0.0.0.0`), natively recognizes and trusts Tailscale CGNAT subnet (`100.64.0.0/10`), and allows seamless browser access without authorization headers.
- **Flexible Web Authentication**: Added URL query parameter (`?token=...`) and HTTP cookie (`agy_token`) authentication for browser clients connecting to remote/LAN servers.
- **Automated CI/CD Release Pipeline**: Added `.github/workflows/release.yml` with automated test validation, wheel/sdist packaging, and direct asset publishing to GitHub Releases on tag pushes (`v*`).

### Changed
- Dashboard systemd service defaults to trusted network mode with auto-refresh support for Tailscale connections.

---

## [1.1.0] - 2026-09-21

### Added
- **Credentials Auto-Discovery (`agy-multi creds`)**: Subcommand with `--save` flag that automatically detects and extracts built-in Google Antigravity OAuth client credentials directly from the installed host binary (`agy`), securely saving them to `~/.config/agy-multi/env` (mode 0600) and `~/.bashrc`.
- **Automatic Env Loading**: Both CLI (`agy-multi`), runner wrappers, and HTTP dashboard server automatically load `~/.config/agy-multi/env` if present.
- **Low Quota Warning State**: Yellow warning status badge (`LOW_WEEKLY` / `status-warning`) when weekly quota drops below 20%, alerting users to upcoming exhaustion while keeping the account relay-eligible as a fallback.
- **Automated Installer Configuration**: `install.sh` now automatically runs `agy-multi creds --save` to enable 24/7 background token refresh out-of-the-box.
- **Unit Test Coverage**: Added `tests/test_creds.py` with 100% test coverage for OAuth extraction, environment loading, and credential saving idempotency.

### Changed
- **Official Order Alignment**: Quota display order now strictly aligns with official Google Antigravity semantics: **5-Hour rolling quota first**, followed by **Weekly quota second** across Web Dashboard cards, status pills, and CLI outputs.
- **High-Density Capsule Progress Bar**: Redesigned progress meters into sleek self-contained capsules (`.capsule-meter`) with embedded countdown and percentage indicators, adaptive color shifting (green > 20%, yellow <= 20%, red <= 0%), frosted glass text badges for guaranteed contrast, and elimination of redundant section subtitles.


### Security
- Maintained a strict **zero hardcoded secrets** policy in the repository: all OAuth secrets are dynamically extracted from local binaries or read from environment variables/local configuration files (`~/.config/agy-multi/env`), verified by automated test assertions.

---

## [1.0.0] - 2026-09-20

### Added
- Multi-account environment isolation with `$HOME` sandbox mapping under `~/.gemini-profiles/`.
- Safe symlink mechanism that mirrors necessary user dotfiles while strictly isolating `.gemini/` and sensitive credential stores (`.ssh`, `.aws`, `.gnupg`, etc.).
- Process runtime tracking via `/proc` to safely monitor active PID allocations.
- Real-time Web Dashboard with token telemetry, official quotas, and account status.
- CLI suite: `init`, `list`, `status`, `run`, `relay`, `login`, `add`, `edit`, `remove`, `install`.
- Smart conversation relay with SQLite online snapshot backup and hot profile failover.
- One-click profile shortcuts (`agy-1`, `agy-2`, `agy-auto`).
