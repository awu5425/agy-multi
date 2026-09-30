# HANDOFF

当前权威仓库：`/data/git/agy-multi.git`（Hub 本机集中仓库，Tailscale `100.83.177.95`）  
集成分支：`main`  
GitHub：`https://github.com/awu5425/agy-multi.git`  
同步范围与状态：`main` 分支与全部版本 tag（截至 v1.5.5）；GitHub 待 ops 同步  
项目文件区：`/data/drop/agy-multi/`（交接交付区）；真实密钥由 ops 集中放于 `/data/secrets/agy-multi/`  
共用规则：`/data/git/_collab.git`（《项目接力约定》v1.2）  
当前负责人 / 更新时间：Antigravity / 2026-10-01 02:00 CST (UTC+8)  
本棒工作台 / 工作分支 / 基准 commit：`/home/agentuser/workspace/my_CLI_works/gemini-switch` / `main` / `119d033`  

---

## 安装、构建与测试入口 / 验收环境

- **运行环境**：Python ≥ 3.10（已在 Python 3.10 / 3.11 / 3.12 验证通过，零外部运行时依赖）
- **安装命令**：
  ```bash
  pip install -e . --break-system-packages --no-deps
  ```
- **自动化测试**：
  ```bash
  pytest -v
  ```
  （当前 109 / 109 项测试全部 PASS，耗时 ~10s）
- **代码静态检查**：
  ```bash
  ruff check --select F821,F841,B023 agy_multi tests/test_audit_fixes.py
  ```
- **常驻后台看板服务**：
  - systemd 单元：`sudo systemctl status agy-multi-dashboard.service`（端口 8989）
  - 本地与局域网：`http://127.0.0.1:8989`、`http://0.0.0.0:8989`
  - Tailscale 访问：`http://vm-0-4-ubuntu.taila5af92.ts.net:8989` 或 `http://vm-0-4-ubuntu:8989`
  - Cloudflare Quick Tunnel：`/tmp/cloudflared-8989.log`

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

- **集成测试**：`agy-multi login` 真实交互授权与 `agy-multi creds --refresh` 需要真实 Google Cloud OAuth 凭据（`AGY_OAUTH_CLIENT_ID` / `AGY_OAUTH_CLIENT_SECRET`），未在脱机环境与 GitHub Actions CI 中自动化运行。
- **Cloudflare Quick Tunnel 动态地址**：当前 Quick Tunnel 重启后地址会变化；如需公网固定访问建议由 ops 配置 Named Tunnel 或使用 Tailscale Funnel。

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

1. 执行 Hub 仓库（`/data/git/agy-multi.git`）远程添加并推送 `main` 分支及所有 tags；
2. 核对 Hub 远程引用一致性；
3. 后续 GitHub 镜像同步由 ops 在 Hub 上统一配置。
