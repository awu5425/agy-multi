# 项目协作约定

## 协作规则

本项目遵循 Hub 规则仓 `_collab`（连接方式见授权接入记录） 的《项目接力约定》（已核对 v1.4 / commit `713bd29`；之后有新指令先核对最新规则）。
两者冲突时以规则仓为准；本文件只写本项目特有的约定。

---

## 敏感信息与不入库规则

以下内容只留在本机，已写入 `.gitignore`，禁止入库：

- `LOCAL_CONFIG*.md`：本地私有备份。
- `jev-research/`：本地研究材料。
- `usage_dashboard.html`：看板快照，含账号与用量数据。
- `.env`、`.env.*`（仅 `.env.example` 入库模板）。
- 本地日志、数据库与构建缓存。

公开与共享文档不得写入真实密钥、令牌、私人邮箱或隧道临时私有地址。应用凭据由 ops 按当前手册授权分发；实际目录与可读范围须核验，不能把计划当成已部署。项目根 `.env` 不会自动读取；加载位置和环境优先级见 README 的 Developer onboarding。不为测试导入生产账号或改动共享服务。

---

## 最小正确改动

- 先理解真实调用链和影响范围，再选择最小正确改动。
- 优先复用现有实现、标准库、平台原生能力和已经安装的依赖。
- 不增加未经请求的抽象、依赖、配置、兼容层或"以后可能会用"的脚手架。
- 优先从共同根因修复问题，不在多个调用方重复修补同一症状。
- 不得为了精简而省略输入验证、安全控制、错误处理或必要测试。
- 非平凡逻辑留下与改动范围相称、可重复运行的验证证据。单测、静态检查和真实联调分别报告；无凭据时不假报联调通过。纯文档改动检查链接、命令和配置一致性，不伪称业务回归已重跑。

---

## CI / 发布流程

本项目已接入 GitHub Actions，并正式发布到 GitHub Releases，**所有实质性代码改动必须遵循以下流程**：

### 分支策略
- `main` 为集成分支；保护设置需以实际服务端为准。`.github/workflows/ci.yml` 只在 GitHub 对应 push/PR 事件触发，不是每次 Hub push 都已跑 CI。
- 禁止在 `main` 上直接推送未经测试的代码；实质改动需在本地测试通过后再 push。

### 测试要求

干净环境先按 `README.md` 的 Developer onboarding 建立虚拟环境并安装开发工具。Python 代码变化运行受影响测试；共享鉴权、账号和配置逻辑变动需跑完整套件及相关真实联调（有授权时）。

```bash
python -m pytest -v
ruff check --select F821,F841,B023 agy_multi tests/test_audit_fixes.py
ruff check --select F821,B023 tests
```

记录命令、环境、基准版本、断言结果和进程退出状态；失败、未运行、环境阻塞分别标注。只读输出或出现部分 passed 不代表整个流程通过。

### 版本与发布
- 版本号遵循 [Semantic Versioning](https://semver.org/)，唯一权威位置：`pyproject.toml` 的 `version` 字段。
- 仅在阿呜批准发布时执行下列流程；普通修复/文档提交不自动打 tag 或发布：
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

- 旧记录提到 Hub 上的常驻服务、端口及隧道，属于特定部署，不适用于所有接手沙盒；当前状态由 ops 核验。
- 本地开发使用独立配置及 loopback 监听。`0.0.0.0` 是绑定所有接口，不是自动获得局域网/Tailscale 可达性；网络访问还受 ACL、防火墙及服务鉴权限制。
- 默认不操作 systemd、不重启 Hub 服务、不发布 Tunnel/Funnel。需要部署或变更凭据时交由 ops 按实际授权处理。
- OAuth 的旧本机 shell/systemd 配置只说明历史位置，不是通用保管规范。变量名和支持的加载位置见 README；真实值不进日志、交接或版本库。
- `usage_dashboard.html` 为账号/用量快照，继续忽略，不作为可以共享的测试产物。

---

## 工作区洁净

- 依赖缓存、构建产物（`dist/`、`*.egg-info/`）、临时文件、日志不入库（见 `.gitignore`）。
- 任务自行创建的临时文件、链接、日志，用完后及时清理。
- 删除前必须确认目标路径精确且位于预期范围内，不得对工作区根目录或用户目录执行递归删除。
- 用户已有文件、未跟踪文件、历史材料以及所有权不明确的内容不得擅自删除。
- `LOCAL_CONFIG*.md`、`jev-research/`、`usage_dashboard.html` 不得提交。
