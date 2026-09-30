# 项目协作约定

## 协作规则

本项目遵循 Hub 规则仓 `/data/git/_collab.git` 的《项目接力约定》（当前 v1.2）。
两者冲突时以规则仓为准；本文件只写本项目特有的约定。

---

## 敏感信息与不入库规则

以下内容只留在本机，已写入 `.gitignore`，禁止入库：

- `LOCAL_CONFIG*.md`：本地私有备份。
- `jev-research/`：本地研究材料。
- `usage_dashboard.html`：看板快照，含账号与用量数据。
- `.env`、`.env.*`（仅 `.env.example` 入库模板）。
- 本地日志、数据库与构建缓存。

公开与共享文档不得写入真实密钥、令牌、私人邮箱或隧道临时私有地址。真实密钥仅由 ops 统一保管于 `/data/secrets/agy-multi/`，或通过环境与 systemd 注入。

---

## 最小正确改动

- 先理解真实调用链和影响范围，再选择最小正确改动。
- 优先复用现有实现、标准库、平台原生能力和已经安装的依赖。
- 不增加未经请求的抽象、依赖、配置、兼容层或"以后可能会用"的脚手架。
- 优先从共同根因修复问题，不在多个调用方重复修补同一症状。
- 不得为了精简而省略输入验证、安全控制、错误处理或必要测试。
- 非平凡逻辑必须留下可重复运行的最小验证证据（`pytest` 通过即可）。

---

## CI / 发布流程

本项目已接入 GitHub Actions，并正式发布到 GitHub Releases，**所有实质性代码改动必须遵循以下流程**：

### 分支策略
- `main` 分支为集成分支与保护分支，CI（`ci.yml`）在每次 push / PR 时自动运行。
- 禁止在 `main` 上直接推送未经测试的代码；实质改动需在本地测试通过后再 push。

### 测试要求（每次改动后必跑）
```bash
pip install -e . --break-system-packages --no-deps && pytest -v
```
- 测试必须全部通过，方可提交。套件在 `tests/test_creds.py`、`tests/test_manager.py`、`tests/test_runner.py`、`tests/test_server.py`、`tests/test_usage.py`、`tests/test_audit_fixes.py`、`tests/test_windows.py`。

### 版本与发布
- 版本号遵循 [Semantic Versioning](https://semver.org/)，唯一权威位置：`pyproject.toml` 的 `version` 字段。
- 发布流程：
  1. 在 `CHANGELOG.md` 顶部追加新版本节（格式参照现有条目）。
  2. 更新 `pyproject.toml` 的 `version`。
  3. commit → push 至 Hub，并打 `vX.Y.Z` 标签。
  4. GitHub 镜像与 Release 由 Hub/ops 负责同步。

### Changelog 规范
- 文件：`CHANGELOG.md`（根目录，唯一）。
- 格式：[Keep a Changelog](https://keepachangelog.com/en/1.0.0/)，节标题为 `## [版本] - YYYY-MM-DD`。
- 不得创建 `CHANGELOG_v2`、`CHANGELOG_new` 等竞争性文件。

---

## Dashboard 服务运维约定

本项目在本机运行一个常驻 HTTP Dashboard 服务，多 Agent 共管时需注意：

- **服务管理**：`sudo systemctl {start|stop|restart|status} agy-multi-dashboard.service`
- **端口**：`8989`，绑定 `0.0.0.0`（Tailscale / LAN / Loopback 可达）
- **日志**：`journalctl -u agy-multi-dashboard.service -f`
- **Tailscale MagicDNS**：`http://vm-0-4-ubuntu.taila5af92.ts.net:8989` 或短域名 `http://vm-0-4-ubuntu:8989` 直接内网可达
- **Cloudflare Quick Tunnel**：每次重启地址变化，日志在 `/tmp/cloudflared-8989.log`
  - 启动命令：`setsid cloudflared tunnel --protocol http2 --url http://127.0.0.1:8989 > /tmp/cloudflared-8989.log 2>&1 & disown`
  - 检查地址：`grep 'trycloudflare.com' /tmp/cloudflared-8989.log`
- **敏感配置**：OAuth Client ID/Secret 仅存于 `~/.bashrc` 和 systemd 单元文件的 `Environment=` 行，不得写入源码或文档。变量名：`AGY_OAUTH_CLIENT_ID` / `AGY_OAUTH_CLIENT_SECRET`。
- **`usage_dashboard.html`**：此文件在 `.gitignore` 中（含快照数据，不入库）；每次运行 `agy-multi usage` 或 `save_html_dashboard()` 时会重新生成，打开时自动通过 `/api/usage` 拉取最新数据。

---

## 工作区洁净

- 依赖缓存、构建产物（`dist/`、`*.egg-info/`）、临时文件、日志不入库（见 `.gitignore`）。
- 任务自行创建的临时文件、链接、日志，用完后及时清理。
- 删除前必须确认目标路径精确且位于预期范围内，不得对工作区根目录或用户目录执行递归删除。
- 用户已有文件、未跟踪文件、历史材料以及所有权不明确的内容不得擅自删除。
- `LOCAL_CONFIG*.md`、`jev-research/`、`usage_dashboard.html` 不得提交。
