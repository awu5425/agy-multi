"""
agy_multi.runner
Intelligent session runner with quota-buffer watchdog, graceful intervention,
and seamless in-place relay resumption across isolated profiles.
"""

import os
import sys
import time
import signal
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from .manager import ProfileManager
from .usage import get_profile_usage
from .utils import (
    sync_profile_environment,
    find_process_running_conversation,
    BOLD, GREEN, YELLOW, RED, CYAN, MAGENTA, RESET
)


class SessionRunner:
    """
    Supervises an active agy CLI process, watching for 5H quota exhaustion or buffer thresholds.
    Intervenes gracefully (SIGINT -> save state -> relay -> in-place resume) or pauses with countdown.
    """

    def __init__(
        self,
        manager: ProfileManager,
        profile_identifier: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
        on_no_target: Optional[str] = None,
        poll_interval: float = 10.0,
    ):
        self.manager = manager
        self.profile_identifier = profile_identifier
        self.extra_args = list(extra_args or [])
        self.poll_interval = poll_interval

        config = self.manager.get_config() if self.manager else {}
        self.min_buffer_pct = float(config.get("min_buffer_pct", 0.0))
        self.on_no_target = on_no_target or config.get("on_no_target", "pause")
        self.auto_relay = bool(config.get("auto_relay", True))

        self.child_proc: Optional[subprocess.Popen] = None
        self._stop_watchdog = threading.Event()
        self._relay_requested = False
        self._next_profile: Optional[Dict[str, Any]] = None

    def _extract_conversation_id(self, args: List[str]) -> Optional[str]:
        """Extracts --conversation <cid> or -c <cid> from args if present."""
        for i, arg in enumerate(args):
            if arg in ("--conversation", "-c") and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith("--conversation="):
                return arg.split("=", 1)[1]
        return None

    def _watchdog_loop(self, curr_profile: Dict[str, Any]):
        """Background loop monitoring 5H quota and intervening when buffer is breached."""
        burned_buffer = False
        while not self._stop_watchdog.is_set():
            time.sleep(self.poll_interval)
            if self._stop_watchdog.is_set():
                break

            if not self.child_proc or self.child_proc.poll() is not None:
                break

            # Poll official quota
            try:
                usage = get_profile_usage(curr_profile, min_buffer_pct=self.min_buffer_pct)
                oq = usage.get("official_quota", {})
                gemini_q = oq.get("groups", {}).get("gemini", {}).get("buckets", {})
                g_5h = gemini_q.get("gemini-5h")
                g_wk = gemini_q.get("gemini-weekly")
                if (g_5h and g_5h.get("disabled")) or (g_wk and g_wk.get("remainingFraction", 1.0) == 0):
                    g_5h_rem = 0.0
                else:
                    g_5h_rem = float(g_5h.get("remainingPct", 100)) if g_5h else 100.0
            except Exception:
                continue

            # Check if quota breached buffer threshold
            if g_5h_rem <= self.min_buffer_pct:
                if burned_buffer:
                    continue  # Already burning buffer, let it run until 0% / 429

                if not self.auto_relay:
                    self._relay_requested = True
                    self._next_profile = None
                    self._relay_notice = f"账号 [{curr_profile['name']}] 5H 配额触碰保底 ({g_5h_rem:.1f}%)。自动接管已关闭，温和暂停现场。"
                    try:
                        self.child_proc.send_signal(signal.SIGINT)
                    except Exception:
                        pass
                    break

                # Try finding an idle relay candidate
                best_target = self.manager.get_best_relay_target(
                    curr_profile["name"],
                    min_buffer_pct=self.min_buffer_pct,
                    require_idle=True
                )

                if best_target:
                    self._relay_requested = True
                    self._next_profile = best_target
                    self._relay_notice = f"账号 [{curr_profile['name']}] 5H 配额触碰保底余量 ({g_5h_rem:.1f}% <= {self.min_buffer_pct:.1f}%)"
                    try:
                        self.child_proc.send_signal(signal.SIGINT)
                    except Exception:
                        pass
                    break
                else:
                    # No target available
                    if self.on_no_target == "burn_buffer" and g_5h_rem > 0:
                        burned_buffer = True
                    else:
                        self._relay_requested = True
                        self._next_profile = None  # None indicates pause with countdown
                        self._relay_notice = f"账号 [{curr_profile['name']}] 触碰保底 ({g_5h_rem:.1f}%)，且当前无可用空闲账号"
                        try:
                            self.child_proc.send_signal(signal.SIGINT)
                        except Exception:
                            pass
                        break

    def run(self) -> int:
        """Main execution loop for supervising and relaying sessions."""
        # 1. Determine starting profile
        if self.profile_identifier:
            curr_profile = self.manager.find_profile(self.profile_identifier)
            if not curr_profile:
                print(f"{RED}Error: Profile '{self.profile_identifier}' not found.{RESET}")
                return 1
        else:
            curr_profile = self.manager.get_active_or_recent_profile()
            if not curr_profile:
                profiles = self.manager.list_profiles()
                if not profiles:
                    print(f"{RED}Error: No profiles configured yet. Run 'agy-multi add' first.{RESET}")
                    return 1
                curr_profile = profiles[0]

        current_args = list(self.extra_args)

        while True:
            self._stop_watchdog.clear()
            self._relay_requested = False
            self._next_profile = None
            self._relay_notice = None

            # Check if current profile is already exhausted before starting
            try:
                usage = get_profile_usage(curr_profile, min_buffer_pct=self.min_buffer_pct)
                oq = usage.get("official_quota", {})
                gemini_q = oq.get("groups", {}).get("gemini", {}).get("buckets", {})
                g_5h = gemini_q.get("gemini-5h")
                g_wk = gemini_q.get("gemini-weekly")
                if (g_5h and g_5h.get("disabled")) or (g_wk and g_wk.get("remainingFraction", 1.0) == 0):
                    g_5h_rem = 0.0
                else:
                    g_5h_rem = float(g_5h.get("remainingPct", 100)) if g_5h else 100.0

                if g_5h_rem <= self.min_buffer_pct:
                    if not self.auto_relay:
                        print(f"{YELLOW}[agy-multi] 账号 [{curr_profile['name']}] 配额不足 ({g_5h_rem:.1f}% <= {self.min_buffer_pct:.1f}%)。自动接管已关闭，暂停启动。{RESET}")
                        self._relay_requested = True
                        self._next_profile = None
                    else:
                        print(f"{YELLOW}[agy-multi] 账号 [{curr_profile['name']}] 配额不足 ({g_5h_rem:.1f}% <= {self.min_buffer_pct:.1f}%)，正在查找就绪账号...{RESET}")
                        alt = self.manager.get_best_relay_target(curr_profile["name"], min_buffer_pct=self.min_buffer_pct, require_idle=True)
                        if alt:
                            curr_profile = alt
                            print(f"{GREEN}[agy-multi] 自动切换至就绪账号 [{curr_profile['name']}] (ID: {curr_profile['id']}){RESET}")
                        else:
                            if self.on_no_target == "burn_buffer" and g_5h_rem > 0:
                                print(f"{YELLOW}[agy-multi] ⚠️ 当前无可用空闲账号，策略为 burn_buffer，继续使用保底余量 ({g_5h_rem:.1f}%)...{RESET}")
                            else:
                                print(f"{YELLOW}[agy-multi] ⚠️ 当前无可用空闲账号，无法启动已耗尽的账号 [{curr_profile['name']}]。{RESET}")
                                self._relay_requested = True
                                self._next_profile = None
            except Exception:
                pass

            # Only spawn child process if relay countdown was not triggered immediately
            if not (self._relay_requested and not self._next_profile):
                pdir = self.manager.get_profile_dir(curr_profile["name"])
                sync_profile_environment(pdir, self.manager.real_home)

                env = os.environ.copy()
                env["HOME"] = str(pdir)
                env["GEMINI_CLI_PROFILE"] = curr_profile["name"]
                env["GEMINI_CLI_PROFILE_ID"] = str(curr_profile["id"])
                env["GEMINI_CLI_PROFILE_DIR"] = str(pdir)

                cmd = ["agy"] + current_args

                print(f"{CYAN}==================================================================={RESET}")
                print(f"{BOLD}🚀 正在启动 Antigravity 会话 [账号: {curr_profile['name']} (ID: {curr_profile['id']})]{RESET}")
                print(f"{CYAN}   - 保底余量阈值: {self.min_buffer_pct}% | 守护监听: 已激活{RESET}")
                print(f"{CYAN}==================================================================={RESET}\n", flush=True)

                # Spawn child process
                try:
                    self.child_proc = subprocess.Popen(cmd, env=env)
                except FileNotFoundError:
                    print(f"{RED}Error: 'agy' command not found in PATH.{RESET}")
                    return 1

                # Start watchdog thread
                watchdog_thread = threading.Thread(
                    target=self._watchdog_loop,
                    args=(curr_profile,),
                    daemon=True
                )
                watchdog_thread.start()

                # Wait for child process to exit
                try:
                    exit_code = self.child_proc.wait()
                except KeyboardInterrupt:
                    print(f"\n{YELLOW}[agy-multi] 接收到用户中断 (Ctrl+C)，正在退出...{RESET}")
                    if self.child_proc:
                        try:
                            self.child_proc.send_signal(signal.SIGINT)
                            self.child_proc.wait(timeout=3)
                        except Exception:
                            self.child_proc.kill()
                    return 130
                finally:
                    self._stop_watchdog.set()

                # Print clean notice after terminal mode is restored
                if self._relay_requested and self._relay_notice:
                    print(f"\n{YELLOW}[agy-multi] ⚠️ {self._relay_notice}{RESET}")
                    if self._next_profile:
                        print(f"{CYAN}[agy-multi] 🔄 正在温和存盘并准备接力至空闲账号 [{self._next_profile['name']}]...{RESET}", flush=True)
                    else:
                        print(f"{CYAN}[agy-multi] 🛑 为保护保底额度，正在温和暂停任务并安全保留现场...{RESET}", flush=True)

                # Case A: Normal clean exit without relay request
                if not self._relay_requested:
                    if exit_code == 0:
                        return 0
                    else:
                        # Non-zero exit: check if caused by quota exhaustion (429)
                        try:
                            usage = get_profile_usage(curr_profile, min_buffer_pct=0.0)
                            oq = usage.get("official_quota", {})
                            gemini_q = oq.get("groups", {}).get("gemini", {}).get("buckets", {})
                            g_5h = gemini_q.get("gemini-5h")
                            if g_5h and g_5h.get("remainingFraction", 1.0) == 0:
                                print(f"\n{RED}[agy-multi] 账号 [{curr_profile['name']}] 5H 配额已完全耗尽 (0%){RESET}")
                                target = self.manager.get_best_relay_target(curr_profile["name"], require_idle=True)
                                if target:
                                    self._relay_requested = True
                                    self._next_profile = target
                        except Exception:
                            pass

                    if not self._relay_requested:
                        return exit_code

            # Case C: Relay requested but no target available yet (Pause & Wait for idle account or reset)
            if self._relay_requested and not self._next_profile:
                target_cid = self._extract_conversation_id(current_args)
                if not target_cid:
                    recent = self.manager.get_most_recent_conversation(curr_profile["name"])
                    target_cid = recent["id"] if recent else None
                wait_start_ts = time.time()

                earliest = self.manager.get_earliest_cooldown_reset()
                if earliest:
                    rem_secs = earliest["remaining_seconds"]
                    hours = rem_secs // 3600
                    mins = (rem_secs % 3600) // 60
                    secs = rem_secs % 60
                    exact_str = datetime.fromtimestamp(earliest["reset_ts"]).strftime("%H:%M:%S")

                    print(f"\n{YELLOW}==================================================================={RESET}")
                    print(f"{YELLOW}⏳ 最早解冻账号: [{earliest['name']}] (ID: {earliest['id']}){RESET}")
                    print(f"{YELLOW}   - 预计释放时间: {exact_str} (剩余约 {hours}小时{mins}分{secs}秒){RESET}")
                    print(f"{CYAN}💤 任务已挂起等待，若有其他账号空闲或配额刷新将自动唤醒接管...{RESET}")
                    print(f"{CYAN}   (可随时按 Ctrl+C 安全退出){RESET}")
                    print(f"{YELLOW}==================================================================={RESET}\n", flush=True)

                    try:
                        # Sleep until reset with periodic check for newly idle/available accounts
                        while rem_secs > 0:
                            sleep_chunk = min(5, rem_secs)
                            time.sleep(sleep_chunk)
                            rem_secs -= sleep_chunk

                            # 1. Check if the conversation has already been taken over elsewhere
                            if target_cid:
                                running_info = find_process_running_conversation(
                                    target_cid,
                                    exclude_pids=[self.child_proc.pid] if self.child_proc else []
                                )
                                if running_info:
                                    print(f"\n{CYAN}[agy-multi] ℹ️ 检测到会话 [{target_cid[:8]}...] 已在其他终端/进程 (PID: {running_info['pid']}) 中被接管运行。{RESET}")
                                    print(f"{GREEN}[agy-multi] 本等待进程安全退出，避免后续额度恢复产生并发冲突。{RESET}\n", flush=True)
                                    return 0

                                relay_info = self.manager.get_last_relay_info(target_cid)
                                if relay_info and relay_info.get("timestamp", 0) > wait_start_ts:
                                    if relay_info.get("from_profile") == curr_profile["name"]:
                                        to_p = relay_info.get("to_profile", "其他账号")
                                        print(f"\n{CYAN}[agy-multi] ℹ️ 检测到会话 [{target_cid[:8]}...] 已被接管迁移至账号 [{to_p}]。{RESET}")
                                        print(f"{GREEN}[agy-multi] 本等待进程安全退出，避免后续额度恢复产生并发冲突。{RESET}\n", flush=True)
                                        return 0

                            # 2. Check if another account became idle with quota (if auto_relay enabled)
                            if self.auto_relay:
                                alt = self.manager.get_best_relay_target(
                                    curr_profile["name"],
                                    min_buffer_pct=self.min_buffer_pct,
                                    require_idle=True
                                )
                                if alt:
                                    print(f"\n{GREEN}[agy-multi] 🔔 检测到账号 [{alt['name']}] 已空闲且配额充裕，自动唤醒接管！{RESET}\n", flush=True)
                                    self._next_profile = alt
                                    break
                        else:
                            # Countdown reached 0: final check before wake up
                            if target_cid:
                                running_info = find_process_running_conversation(
                                    target_cid,
                                    exclude_pids=[self.child_proc.pid] if self.child_proc else []
                                )
                                if running_info:
                                    print(f"\n{CYAN}[agy-multi] ℹ️ 检测到会话 [{target_cid[:8]}...] 已在其他终端/进程 (PID: {running_info['pid']}) 中被接管运行。{RESET}")
                                    print(f"{GREEN}[agy-multi] 本等待进程安全退出，取消恢复启动。{RESET}\n", flush=True)
                                    return 0

                            print(f"\n{GREEN}[agy-multi] 🔔 账号 [{earliest['name']}] 配额已释放，自动唤醒启动！{RESET}\n", flush=True)
                            self._next_profile = self.manager.find_profile(earliest["name"])
                    except KeyboardInterrupt:
                        print(f"\n{YELLOW}[agy-multi] 用户取消等待。{RESET}")
                        return 0
                else:
                    print(f"\n{YELLOW}[agy-multi] 当前无可用账号且暂无刷新时间，正在等待其他账号释放空闲...{RESET}")
                    print(f"{CYAN}   (可随时按 Ctrl+C 安全退出){RESET}\n", flush=True)
                    try:
                        while True:
                            time.sleep(5)
                            # 1. Check if the conversation has already been taken over elsewhere
                            if target_cid:
                                running_info = find_process_running_conversation(
                                    target_cid,
                                    exclude_pids=[self.child_proc.pid] if self.child_proc else []
                                )
                                if running_info:
                                    print(f"\n{CYAN}[agy-multi] ℹ️ 检测到会话 [{target_cid[:8]}...] 已在其他终端/进程 (PID: {running_info['pid']}) 中被接管运行。{RESET}")
                                    print(f"{GREEN}[agy-multi] 本等待进程安全退出，避免后续额度恢复产生并发冲突。{RESET}\n", flush=True)
                                    return 0

                                relay_info = self.manager.get_last_relay_info(target_cid)
                                if relay_info and relay_info.get("timestamp", 0) > wait_start_ts:
                                    if relay_info.get("from_profile") == curr_profile["name"]:
                                        to_p = relay_info.get("to_profile", "其他账号")
                                        print(f"\n{CYAN}[agy-multi] ℹ️ 检测到会话 [{target_cid[:8]}...] 已被接管迁移至账号 [{to_p}]。{RESET}")
                                        print(f"{GREEN}[agy-multi] 本等待进程安全退出，避免后续额度恢复产生并发冲突。{RESET}\n", flush=True)
                                        return 0

                            # 2. Check if another account became idle with quota (if auto_relay enabled)
                            if self.auto_relay:
                                alt = self.manager.get_best_relay_target(
                                    curr_profile["name"],
                                    min_buffer_pct=self.min_buffer_pct,
                                    require_idle=True
                                )
                                if alt:
                                    print(f"\n{GREEN}[agy-multi] 🔔 检测到账号 [{alt['name']}] 已空闲且配额充裕，自动唤醒接管！{RESET}\n", flush=True)
                                    self._next_profile = alt
                                    break
                    except KeyboardInterrupt:
                        print(f"\n{YELLOW}[agy-multi] 用户取消等待。{RESET}")
                        return 0

            # Case B: Relay requested to a target profile (migrates conversation if needed)
            if self._relay_requested and self._next_profile:
                target_p = self._next_profile
                self._relay_requested = False

                if target_p["name"] != curr_profile["name"]:
                    # Determine conversation ID to relay
                    cid = self._extract_conversation_id(current_args)
                    if not cid:
                        recent = self.manager.get_most_recent_conversation(curr_profile["name"])
                        cid = recent["id"] if recent else None

                    if cid:
                        try:
                            res = self.manager.relay_conversation(
                                curr_profile["name"],
                                target_p["name"],
                                cid,
                                sync_brain=True
                            )
                            print(f"\n{GREEN}[agy-multi] ✅ 会话已成功迁移至账号 [{target_p['name']}] (ID: {target_p['id']}){RESET}")
                            print(f"{CYAN}[agy-multi] ⚡ 正在无缝接续会话 [{cid[:8]}...] 继续工作...{RESET}\n", flush=True)

                            # Update args to continue the same conversation
                            if "--conversation" not in current_args and "-c" not in current_args:
                                current_args = ["--conversation", cid] + [a for a in current_args if not a.startswith("--conversation")]
                        except Exception as e:
                            print(f"{RED}[agy-multi] 接力迁移失败: {e}{RESET}")
                            return 1
                    else:
                        print(f"{YELLOW}[agy-multi] 未找到可迁移的历史会话，切换至新账号启动。{RESET}")

                curr_profile = target_p
                time.sleep(1.0)
                continue
