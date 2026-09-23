# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Backward compatibility alias `gemini-switch` and one-click shortcuts (`agy-1`, `agy-2`, `agy-auto`).
