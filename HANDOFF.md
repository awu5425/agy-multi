# HANDOFF

当前权威仓库：Hub 项目仓 `agy-multi`（真实连接地址由授权接入记录提供）；`hub` remote 使用本机获授权账号，不能复制上一位的机器身份。
私有接入信息：本文只记录逻辑项目名和仓库相对路径。真实主机、账号、本机目录映射及凭据引用由 ops/本机受控接入记录提供；不要写入可同步文档，缺少时向 ops 请求该项目授权，不扫描或猜测。
集成分支：`main`
GitHub：`https://github.com/awu5425/agy-multi.git`（ops 单向同步副本）
同步范围与状态：Hub 已存在 `main` 及截至 v1.5.5 的版本标签；GitHub 同步本轮未核验，由 ops 确认，不能从 Hub push 成功推断。
项目文件区：交付物按 ops 指定的项目投递区存放。应用凭据由 ops 授权；secrets 目录、授权名单和实际分发状态本轮未核验。
共用规则：Hub `_collab.git`《项目接力约定》v1.4（已核对规则仓 commit `713bd29`；之后有新指令先核对最新规则）。
当前负责人 / 状态 / 更新时间：`lady-hermes@sandbox` · 小任务完成（测试数据去 JWT 字面量），状态待命 · 2026-10-01 CST (UTC+8)。上一棒 `Antigravity` 接手验收 `7432a4e`。
本棒工作台 / 工作分支 / 基准 commit：Linux 开发机的项目工作副本 / `main` / `ae4101d`（v1.5.5）。未改动业务代码。
文件清单及验证记录：接手验收建立隔离 `.venv` 并完成依赖安装与 109 项单测及静态分析；更新 `HANDOFF.md` 验收记录，不存凭据值。

---

## 安装、构建与测试入口 / 验收环境

- **新环境入口**：先读 `README.md` 的 `Developer onboarding`；Python ≥3.10，隔离虚拟环境安装项目及测试工具。零外部运行时依赖不等于 pytest/ruff 已预装，不用 `--break-system-packages` 改系统 Python。
- **测试命令**：激活虚拟环境后执行 `python -m pytest -v`；静态检查见 README，与 CI 的两条 Ruff 选择规则一致。
- **本地开发入口**：`agy-multi --help` 可确认 CLI；需要看板时使用独立本地环境和显式 `--host 127.0.0.1 --port <空闲端口>`，不触碰 Hub 的常驻服务。
- **配置如何加载**：项目根 `.env` 不会自动加载。源码默认读取 `~/.config/agy-multi/env`，不存在时回退 `oauth.env`；既有进程环境值优先。模板只是变量清单，真实配置由 ops 经授权注入或放到实际支持的位置。CLI 的 host/port 应显式传参，不能假设模板值一定覆盖 CLI 默认值。
- **最近验证**：
  - 2026-10-01（gitleaks 门禁修复）：基准 `7432a4e`，Linux/Python 3.11，临时 HOME、无真实凭据、全新 `.venv`；只改 `tests/test_manager.py` 的 3 个假 JWT 构造方式。pytest **109 passed / exit 0**，两条 CI 同等 Ruff 检查 exit 0；gitleaks 8.30.1 扫工作树 **no leaks / exit 0**。历史中 3 条命中待 ops 按指纹加受控例外（阿呜已同意由其向 ops 说明）。
  - 2026-10-01（接手验收）：基准 `ae4101d`，Linux (Ubuntu 24.04) / Python 3.12.3，在全新 `.venv` 隔离环境中完成 editable 安装与开发工具链安装；pytest **109 passed / 9.83s / exit 0**，两条 CI 同等 Ruff 规则检查通过（`F821,F841,B023` 与 `F821,B023`，exit 0），CLI `--help` 与 `list` 入口通过（exit 0）。未重启共享 systemd 服务，未调用真实 OAuth 授权刷新接口，原生 Windows 与 Python 3.10/3.11 未在本机重测。
  - 2026-10-01（上一棒沙盒）：业务基准 `c721c8d`，Linux/Python 3.11，临时 HOME、无真实凭据：全新虚拟环境安装成功；pytest **109 passed / 6.58s / exit 0**，两条 CI 同等 Ruff 检查和 CLI help 通过。详情见 CHANGELOG 的 Unreleased。
- **验证边界**：无凭据测试可使用临时 HOME/模拟数据；真实登录、凭据刷新或 relay 需要获授权的测试账号和隔离配置，不对现有开发会话操作。

### 既有部署（历史状态，仅 ops 按需核验）

上一棒报告 Hub 上存在 `agy-multi-dashboard.service`，看板端口 8989，并有 Quick Tunnel。它们不是新沙盒自带环境，本轮未检查当前运行、ACL 可达性或临时 URL。

开发交接不要求 sudo、重启共享服务或创建公开隧道；确需部署时交给当前 ops 并取得对应授权。`0.0.0.0` 是监听地址，不是客户端访问地址。

---

## 已完成与验证结果

| 版本 | 状态 | 交付内容与要点 |
|------|------|----------------|
| **v1.0.0** | ✅ 已发布 | 多账号并发隔离管理器 CLI 核心底座 |
| **v1.1.0** | ✅ 已发布 | Capsule 动态进度条 + 跨平台交互体验 |
| **v1.2.0** | ✅ 已发布 | 近零周额探测、订阅档位识别、账号状态清单 |
| **v1.3.0** | ✅ 已发布 | 配置继承与克隆，Token 临期预警，清除旧别名 |
| **v1.4.0** | ✅ 已发布 | 全生命周期终端分屏标题更新，智能接力续跑动画 |
| **v1.5.0** | ✅ 已发布 | Windows 原生支持（NTFS Junctions、Win32 进程探活、Sentinel IPC、Windows Terminal 分屏） |
| **v1.5.1** | ✅ 已发布 | Orca 深度适配，终端控制台退出自愈（`restore_terminal`） |
| **v1.5.2** | ✅ 已发布 | 外审核心安全缺陷修复（B1/S1-S5），Cookie HttpOnly，底层权限防竞态，接入 Ruff |
| **v1.5.3** | ✅ 已发布 | `secure_write` 临时文件原子替换与断电防损坏，302 跳转保留非 token 参数，启动日志 Token 脱敏 |
| **v1.5.4** | ✅ 已发布 | 修复 Cloudflare 隧道 403 阻断，支持 `AGY_MULTI_TRUSTED_HOSTS` |
| **v1.5.5** | ✅ 已发布 | 修复 Tailscale MagicDNS（`*.ts.net`）与短主机名 403 阻断，放行 WireGuard CGNAT 客户端直连 |
| **Hub 接入** | ✅ 已就绪 | 根目录完成入库规范对齐：补充 `.gitignore`、`.env.example`、规则仓标准引用与交接文件标准化 |

---

## 未完成、待合并、风险

- **真实联调未验证**：登录/刷新需有效测试账号、有效用户 token 和可用 OAuth client 配置。客户端凭据可能由已有 Antigravity 安装发现，也可由 ops 注入；变量名为 `AGY_OAUTH_CLIENT_ID` / `AGY_OAUTH_CLIENT_SECRET`。离线单测通过不代表真实登录/刷新可用；未授权时标记“联调未跑”，不复制生产账号状态。
- **公网入口待决策**：历史 Quick Tunnel 地址会随重启变化。固定公网入口涉及授权、鉴权和网络暴露，只有阿呜明确要求时才交由 ops 评估；不是下一棒默认任务。

---

## 文件清单与结构

- `agy_multi/`：核心模块代码（`cli.py`、`manager.py`、`runner.py`、`server.py`、`usage.py`、`utils.py`）
- `tests/`：完整自动化回归测试套件（109 个用例）
- `pyproject.toml`：项目元数据与版本权威位置（v1.5.5）
- `CHANGELOG.md`：Keep a Changelog 格式完整版本历史
- `AGENTS.md`：本项目专用协作与运维规则，引用 `_collab.git`
- `HANDOFF.md`：项目交接状态记录
- `.env.example`：环境变量模板文件

---

## 下一步

1. **已经完成**：Hub 首次接入；本次读取到 `main` 和版本标签。不要重复初始化仓库或覆盖已有远端。
2. **待 ops**：核对 GitHub 对应引用/同步状态；如业务联调需要凭据，确认项目授权和安全配置入口。
3. **待阿呜指定**：下一项功能、修复或重构；没有新目标就保持待接手，不自行发布、打 tag 或改变线上服务。
4. **换人检查**：确认旧会话停写，读取 Hub 最新 handoff，用自己的账号 fetch，保留未提交改动，按本次改动范围测试并交付。
