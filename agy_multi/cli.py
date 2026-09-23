"""
agy_multi.cli
Command Line Interface for agy-multi.
"""

import sys
import os
import time
import json
import shutil
import re
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .manager import ProfileManager
from .utils import BOLD, GREEN, YELLOW, RED, CYAN, MAGENTA, RESET, load_env_config, detect_real_home


def format_table(rows: List[List[str]], headers: List[str]) -> str:
    all_data = [headers] + rows
    col_widths = [max(len(str(r[c])) for r in all_data) for c in range(len(headers))]
    
    # Border builders
    header_line = "  ".join(f"{headers[i]:<{col_widths[i]}}" for i in range(len(headers)))
    sep_line = "  ".join("-" * col_widths[i] for i in range(len(headers)))
    
    lines = [f"{BOLD}{header_line}{RESET}", sep_line]
    for row in rows:
        lines.append("  ".join(f"{str(row[i]):<{col_widths[i]}}" for i in range(len(headers))))
    return "\n".join(lines)


def cmd_list(manager: ProfileManager, args: argparse.Namespace) -> int:
    profiles = manager.list_profiles()
    if not profiles:
        print(f"{YELLOW}No profiles configured yet. Add one using: agy-multi add <name> <email>{RESET}")
        return 0

    headers = ["ID", "Name", "Target Email", "Status", "Auth Email", "Active PIDs", "Description"]
    rows = []
    for p in profiles:
        auth = p.get("auth", {})
        is_valid = auth.get("is_valid", False)
        exp_info = auth.get("expiry_info", {})
        exp_state = exp_info.get("state")
        days_rem = exp_info.get("days_remaining")

        if not is_valid:
            if exp_state == "expired":
                status_str = f"{RED}○ Expired{RESET}"
            else:
                status_str = f"{RED}○ Not Auth{RESET}"
        elif exp_state == "expiring_soon":
            status_str = f"{YELLOW}⚠️ Expiring ({days_rem}d){RESET}"
        elif exp_state == "refreshable":
            status_str = f"{GREEN}● Logged In (Auto){RESET}"
        else:
            status_str = f"{GREEN}● Logged In{RESET}"

        auth_email = auth.get("email") or "-"
        pids_str = ", ".join(map(str, p["active_pids"])) if p["active_pids"] else "idle"

        desc = p.get("description", "")
        if p.get("inherited_from"):
            desc = f"{desc} [from {p['inherited_from']}]" if desc else f"[from {p['inherited_from']}]"

        rows.append([
            p["id"],
            p["name"],
            p["email"],
            status_str,
            auth_email,
            pids_str,
            desc
        ])

    print(f"\n{BOLD}{CYAN}=== Google AI Pro Profiles (agy-multi) ==={RESET}\n")
    print(format_table(rows, headers))
    print(f"\nUsage: {BOLD}agy-multi run <ID/Name>{RESET} or {BOLD}agy-auto{RESET} or {BOLD}agy-<ID>{RESET} (e.g. {CYAN}agy-1{RESET}, {CYAN}agy-2{RESET})\n")
    return 0


def cmd_status(manager: ProfileManager, args: argparse.Namespace) -> int:
    from .usage import get_profile_usage
    print(f"\n{BOLD}{CYAN}=== agy-multi Relay & Quota Status ==={RESET}\n")

    # 1. Configuration
    cfg = manager.get_config()
    min_buf = float(cfg.get("min_buffer_pct", 0.0))
    auto_relay = bool(cfg.get("auto_relay", True))
    on_no_target = cfg.get("on_no_target", "pause")

    print(f"{BOLD}⚙️  Watchdog Policy:{RESET}")
    print(f"  • Min Reserve Buffer : {CYAN}{min_buf:.1f}%{RESET}")
    print(f"  • Auto Takeover      : {GREEN}Enabled{RESET}" if auto_relay else f"  • Auto Takeover      : {YELLOW}Disabled (Pause Only){RESET}")
    print(f"  • Fallback on Empty  : {CYAN}{on_no_target}{RESET} ({'Pause & wait for reset' if on_no_target == 'pause' else 'Burn remaining reserve buffer'})")
    print()

    # 2. Current active profile
    curr = manager.get_active_or_recent_profile()
    if not curr:
        print(f"{YELLOW}No profiles configured yet. Run 'agy-multi add' to register accounts.{RESET}\n")
        return 0

    usage = get_profile_usage(curr, min_buffer_pct=min_buf)
    oq = usage.get("official_quota", {})
    gemini_q = oq.get("groups", {}).get("gemini", {}).get("buckets", {})
    g_5h = gemini_q.get("gemini-5h")
    g_wk = gemini_q.get("gemini-weekly")
    g_5h_pct = float(g_5h.get("remainingPct", 100)) if g_5h else 100.0
    g_5h_reset = g_5h.get("resetTime", "-") if g_5h else "-"
    g_wk_pct = float(g_wk.get("remainingPct", 100)) if g_wk else 100.0
    g_wk_reset = g_wk.get("resetTime", "-") if g_wk else "-"
    u_code = usage.get("usability", {}).get("code", "UNKNOWN")

    pids = curr.get("active_pids", [])
    pids_str = ", ".join(map(str, pids)) if pids else "Idle"

    status_color = GREEN if u_code == "READY" else (YELLOW if "COOLDOWN" in u_code else RED)
    print(f"{BOLD}👤 Current Profile:{RESET}")
    print(f"  • Profile      : {BOLD}[{curr['id']}] {curr['name']}{RESET} ({curr['email']})")
    print(f"  • Health       : {status_color}{u_code}{RESET}")
    print(f"  • Weekly Quota : {CYAN}{g_wk_pct:.1f}%{RESET} (Reset: {g_wk_reset})")
    print(f"  • 5H Quota     : {CYAN}{g_5h_pct:.1f}%{RESET} (Reset: {g_5h_reset})")
    print(f"  • Status       : {GREEN}Active PID {pids_str}{RESET}" if pids else f"  • Status       : {CYAN}Idle{RESET}")

    auth_info = curr.get("auth", {})
    exp_info = auth_info.get("expiry_info", {})
    exp_state = exp_info.get("state")
    exp_iso = exp_info.get("expiry_iso") or auth_info.get("expiry") or "-"
    if exp_state == "expiring_soon":
        exp_display = f"{YELLOW}{exp_iso} (⚠️ Expiring in {exp_info.get('days_remaining')}d - Re-login recommended){RESET}"
    elif exp_state == "refreshable":
        exp_display = f"{CYAN}{exp_iso}{RESET} ({GREEN}Auto-refreshable ✅{RESET})"
    elif exp_state == "expired":
        exp_display = f"{RED}{exp_iso} (⚠️ Expired){RESET}"
    else:
        exp_display = f"{CYAN}{exp_iso}{RESET}"
    print(f"  • Token Expiry : {exp_display}")
    print()

    # 3. Next Relay Candidate
    print(f"{BOLD}🔄 Next Relay Target Decision:{RESET}")
    best = manager.get_best_relay_target(curr["name"], min_buffer_pct=min_buf, require_idle=True)
    if best:
        b_usage = get_profile_usage(best, min_buffer_pct=min_buf)
        b_oq = b_usage.get("official_quota", {})
        b_gemini_q = b_oq.get("groups", {}).get("gemini", {}).get("buckets", {})
        b_5h = b_gemini_q.get("gemini-5h")
        b_wk = b_gemini_q.get("gemini-weekly")
        b_pct = float(b_5h.get("remainingPct", 100)) if b_5h else 100.0
        b_wk_pct = float(b_wk.get("remainingPct", 100)) if b_wk else 100.0
        print(f"  • Recommended: {GREEN}[{best['id']}] {best['name']}{RESET} ({best['email']})")
        print(f"  • Reasoning  : Weekly ({b_wk_pct:.1f}%) and 5H ({b_pct:.1f}% >= {min_buf}%) are healthy, authenticated, and process is idle.")
    else:
        all_profiles = manager.list_profiles()
        others = [p for p in all_profiles if p["name"] != curr["name"]]
        if not others:
            print(f"  • None Available: Only 1 profile configured. Run {CYAN}agy-multi add{RESET} to add relay backups.")
        else:
            reasons = []
            for p in others:
                u = get_profile_usage(p, min_buffer_pct=min_buf)
                usability = u.get("usability", {})
                u_code = usability.get("code", "")
                if not p.get("auth", {}).get("is_valid"):
                    reasons.append(f"[{p['name']}] Not logged in (run 'agy-multi login {p['id']}')")
                elif u_code == "TOKEN_EXPIRED":
                    reasons.append(f"[{p['name']}] Token expired (run 'agy-{p['id']}' to refresh or login)")
                elif u_code == "REGION_PENDING":
                    reasons.append(f"[{p['name']}] Region restricted (Google account restricted)")
                elif p.get("active_pids"):
                    reasons.append(f"[{p['name']}] Busy with active process (PID {p['active_pids']})")
                elif usability.get("target_ineligible_reason"):
                    reasons.append(f"[{p['name']}] {usability['target_ineligible_reason']}")
                else:
                    reasons.append(f"[{p['name']}] Quota below buffer threshold ({min_buf}%)")
            print(f"  • None Available: No idle profile ready with >= {min_buf}% quota.")
            for r in reasons[:4]:
                print(f"    - {YELLOW}{r}{RESET}")
    print()

    # 4. Last Relay Record
    last_relay = manager.get_last_relay_info()
    if last_relay:
        ts_str = datetime.fromtimestamp(last_relay.get("timestamp", 0)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{BOLD}📜 Last Relay Event:{RESET}")
        print(f"  • Time         : {ts_str}")
        print(f"  • Conversation : {last_relay.get('conversation_id')}")
        print(f"  • Route        : [{last_relay.get('from_profile')}] ➔ [{last_relay.get('to_profile')}]")
    else:
        print(f"{BOLD}📜 Last Relay Event:{RESET} (No handovers recorded yet)")
    print()
    return 0


def cmd_usage(manager: ProfileManager, args: argparse.Namespace) -> int:
    from .usage import get_all_usage, render_html_dashboard, save_html_dashboard

    data = get_all_usage(manager)

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if getattr(args, "csv", False):
        import csv
        import sys
        writer = csv.writer(sys.stdout)
        writer.writerow([
            "ID", "Name", "Email", "Usability", "Gemini 5H Rem %", "Gemini Wk Rem %", "Claude Wk Rem %",
            "5H Requests", "5H Tokens", "5H Cached", "5H Thinking",
            "7D Requests", "7D Tokens", "7D Cached", "7D Thinking",
            "All Requests", "All Tokens", "All Cached", "All Thinking",
            "Active PIDs"
        ])
        for acc in data.get("accounts", []):
            oq = acc.get("official_quota", {})
            g_q = oq.get("groups", {}).get("gemini", {}).get("buckets", {})
            c_q = oq.get("groups", {}).get("claude_gpt", {}).get("buckets", {})
            g_5h = g_q.get("gemini-5h", {}).get("remainingPct", "N/A")
            g_wk = g_q.get("gemini-weekly", {}).get("remainingPct", "N/A")
            c_wk = c_q.get("claude-weekly", {}).get("remainingPct", "N/A")
            writer.writerow([
                acc.get("id"),
                acc.get("name"),
                acc.get("email"),
                acc.get("usability", {}).get("code", "UNKNOWN"),
                g_5h,
                g_wk,
                c_wk,
                acc.get("stats_5h", {}).get("requests", 0),
                acc.get("stats_5h", {}).get("total_tokens", 0),
                acc.get("stats_5h", {}).get("cached_tokens", 0),
                acc.get("stats_5h", {}).get("thinking_tokens", 0),
                acc.get("stats_7d", {}).get("requests", 0),
                acc.get("stats_7d", {}).get("total_tokens", 0),
                acc.get("stats_7d", {}).get("cached_tokens", 0),
                acc.get("stats_7d", {}).get("thinking_tokens", 0),
                acc.get("stats_all", {}).get("requests", 0),
                acc.get("stats_all", {}).get("total_tokens", 0),
                acc.get("stats_all", {}).get("cached_tokens", 0),
                acc.get("stats_all", {}).get("thinking_tokens", 0),
                ";".join(map(str, acc.get("active_pids", []))),
            ])
        return 0

    html_path = getattr(args, "html", None)
    if html_path is not None:
        target_path = Path(html_path) if html_path != "" else None
        saved = save_html_dashboard(manager, target_path)
        print(f"\n{GREEN}✓ Usage HTML Dashboard saved to: {BOLD}{saved}{RESET}\n")
        return 0

    # Output formatted terminal overview and automatically update usage_dashboard.html
    saved_dashboard = save_html_dashboard(manager)

    print(f"\n{BOLD}{CYAN}=== Antigravity Multi-Account Quota & Usage ==={RESET}\n")
    headers = ["ID", "Name", "Usability", "Gemini 5H Rem", "Gemini Wk Rem", "Claude Wk Rem", "5H Tokens", "7D Tokens", "Active PIDs"]
    rows = []
    for a in data["accounts"]:
        s5 = a["stats_5h"]
        s7 = a["stats_7d"]
        pids = ", ".join(map(str, a["active_pids"])) if a["active_pids"] else "idle"
        
        # Usability label with color
        u = a.get("usability", {})
        code = u.get("code", "READY")
        label = u.get("label", "● 可用")
        if code == "READY":
            usability_str = f"{GREEN}{label}{RESET}"
        elif code in ("LOW_WEEKLY", "CLAUDE_EXHAUSTED", "COOLDOWN_5H"):
            usability_str = f"{YELLOW}{label}{RESET}"
        elif code == "WEEKLY_EXHAUSTED":
            usability_str = f"{RED}{label}{RESET}"
        elif code == "REGION_PENDING":
            usability_str = f"{YELLOW}{label}{RESET}"
        elif code == "NOT_AUTH":
            usability_str = f"{RED}{label}{RESET}"
        else:
            usability_str = label

        oq = a.get("official_quota", {})
        g_buckets = oq.get("groups", {}).get("gemini", {}).get("buckets", {})
        c_buckets = oq.get("groups", {}).get("claude_gpt", {}).get("buckets", {})

        g_5h = g_buckets.get("gemini-5h")
        g_wk = g_buckets.get("gemini-weekly")
        c_wk = c_buckets.get("3p-weekly")

        def fmt_rem(b):
            if not b or not oq.get("available"):
                return "-"
            if b.get("disabled"):
                return f"{RED}Disabled{RESET}"
            pct = f"{b.get('remainingPct', 100):.1f}%"
            reset_ts = b.get("resetTs")
            if reset_ts and b.get("remainingFraction", 1.0) < 1.0:
                diff = max(0, int(reset_ts - time.time()))
                if diff > 86400:
                    d = diff // 86400
                    h = (diff % 86400) // 3600
                    return f"{pct} ({d}d {h}h)"
                elif diff > 0:
                    h = diff // 3600
                    m = (diff % 3600) // 60
                    return f"{pct} ({h}h {m}m)"
            return pct

        g_5h_str = fmt_rem(g_5h)
        g_wk_str = fmt_rem(g_wk)
        c_wk_str = fmt_rem(c_wk)

        rows.append([
            a["id"],
            a["name"],
            usability_str,
            g_5h_str,
            g_wk_str,
            c_wk_str,
            f"{s5['total_tokens']:,}",
            f"{s7['total_tokens']:,}",
            pids
        ])
    print(format_table(rows, headers))

    tot_5 = data["totals"]["stats_5h"]
    tot_7 = data["totals"]["stats_7d"]
    tot_a = data["totals"]["stats_all"]
    sep_len = 80
    print("-" * sep_len)
    print(f"{BOLD}TOTALS:  5H: {tot_5['requests']} reqs ({tot_5['total_tokens']:,} tok) | 7D: {tot_7['requests']} reqs ({tot_7['total_tokens']:,} tok) | All: {tot_a['total_tokens']:,} tok{RESET}\n")
    print(f"{GREEN}✓ Interactive HTML Dashboard updated: {BOLD}{saved_dashboard}{RESET}\n")
    return 0


def cmd_config(manager: ProfileManager, args: argparse.Namespace) -> int:
    updated = False
    kwargs = {}

    if getattr(args, "min_buffer", None) is not None:
        try:
            val = float(args.min_buffer)
            if val < 0 or val > 90:
                print(f"{RED}Error: min-buffer must be between 0% and 90%.{RESET}")
                return 1
            kwargs["min_buffer_pct"] = val
            updated = True
        except ValueError:
            print(f"{RED}Error: invalid number for min-buffer.{RESET}")
            return 1

    if getattr(args, "on_no_target", None) is not None:
        kwargs["on_no_target"] = args.on_no_target
        updated = True

    if getattr(args, "auto_relay", None) is not None:
        kwargs["auto_relay"] = args.auto_relay
        updated = True

    if updated:
        cfg = manager.update_config(**kwargs)
        print(f"\n{BOLD}{GREEN}✓ Configuration updated successfully!{RESET}")
        print(f"  {BOLD}Minimum Quota Buffer (保底余量):{RESET} {CYAN}{cfg.get('min_buffer_pct', 0.0)}%{RESET}")
        auto_str = f"{GREEN}Enabled (开启){RESET}" if cfg.get("auto_relay", True) else f"{YELLOW}Disabled (关闭 - 仅暂停不切换){RESET}"
        print(f"  {BOLD}Auto Takeover (自动接管):{RESET} {auto_str}")
        print(f"  {BOLD}No Relay Target Policy (无接力兜底策略):{RESET} {CYAN}{cfg.get('on_no_target', 'pause')}{RESET}\n")
        return 0
    else:
        cfg = manager.get_config()
        print(f"\n{BOLD}{CYAN}=== agy-multi Configuration ==={RESET}\n")
        print(f"  {BOLD}Minimum Quota Buffer (保底余量):{RESET} {GREEN}{cfg.get('min_buffer_pct', 0.0)}%{RESET}")
        auto_str = f"{GREEN}Enabled (开启){RESET}" if cfg.get("auto_relay", True) else f"{YELLOW}Disabled (关闭 - 仅暂停不切换){RESET}"
        print(f"  {BOLD}Auto Takeover (自动接管):{RESET} {auto_str}")
        print(f"  {BOLD}No Relay Target Policy (无接力兜底策略):{RESET} {GREEN}{cfg.get('on_no_target', 'pause')}{RESET}")
        print(f"\nTo update settings:")
        print(f"  {CYAN}agy-multi config --min-buffer 5{RESET}           (keep 5% reserve buffer)")
        print(f"  {CYAN}agy-multi config --auto-relay{RESET}             (enable auto takeover)")
        print(f"  {CYAN}agy-multi config --no-auto-relay{RESET}          (disable auto takeover, pause only)")
        print(f"  {CYAN}agy-multi config --on-no-target pause{RESET}     (pause & countdown if no account)")
        print(f"  {CYAN}agy-multi config --on-no-target burn_buffer{RESET} (burn buffer to 0% if no account)\n")
        return 0


def cmd_relay(manager: ProfileManager, args: argparse.Namespace, remaining_args: List[str]) -> int:
    from .usage import get_profile_usage

    min_buffer = getattr(args, "min_buffer", None)
    if min_buffer is None:
        min_buffer = float(manager.get_config().get("min_buffer_pct", 0.0))

    # Case 1: List candidates
    if getattr(args, "list_candidates", False):
        src_profile = None
        if getattr(args, "from_profile", None):
            src_profile = manager.find_profile(args.from_profile)
        else:
            src_profile = manager.get_active_or_recent_profile()

        best_cand = manager.find_best_relay_candidate(
            src_profile["name"] if src_profile else None,
            min_buffer_pct=min_buffer
        )
        best_name = best_cand["name"] if best_cand else None

        profiles = manager.list_profiles()
        headers = ["ID", "Name", "Target Email", "Status", "Gemini 5H Rem", "Gemini Wk Rem", "Claude Wk Rem", "PIDs", "Relay Suitability"]
        rows = []
        for p in profiles:
            if src_profile and p["name"] == src_profile["name"]:
                continue
            is_valid = p.get("auth", {}).get("is_valid", False)
            if not is_valid:
                rows.append([p["id"], p["name"], p["email"], f"{RED}○ Not Auth{RESET}", "-", "-", "-", "idle", f"{RED}Ineligible (Not logged in){RESET}"])
                continue

            u = get_profile_usage(p, min_buffer_pct=min_buffer)
            usability = u.get("usability", {})
            u_label = usability.get("label", "● 可用")
            u_code = usability.get("code", "")

            oq = u.get("official_quota", {})
            g_5h = oq.get("groups", {}).get("gemini", {}).get("buckets", {}).get("gemini-5h")
            g_wk = oq.get("groups", {}).get("gemini", {}).get("buckets", {}).get("gemini-weekly")
            c_wk = oq.get("groups", {}).get("claude_gpt", {}).get("buckets", {}).get("3p-weekly")

            g_5h_pct = float(g_5h.get("remainingPct", 100)) if g_5h else 100.0
            g_wk_pct = float(g_wk.get("remainingPct", 100)) if g_wk else 100.0
            g_5h_str = f"{g_5h_pct:.1f}%" if g_5h else "-"
            g_wk_str = f"{g_wk_pct:.1f}%" if g_wk else "-"
            c_wk_str = f"{c_wk.get('remainingPct', 100):.1f}%" if c_wk else "-"
            pids_str = ", ".join(map(str, p.get("active_pids", []))) if p.get("active_pids") else "idle"

            if p["name"] == best_name:
                suitability = f"{BOLD}{GREEN}★ Recommended (Best Pick){RESET}"
            elif usability.get("is_target_eligible") is False:
                reason = usability.get("target_ineligible_reason") or u_label
                suitability = f"{RED}✗ Ineligible ({reason}){RESET}"
            elif g_5h_pct <= min_buffer and min_buffer > 0:
                suitability = f"{YELLOW}⏳ Reserved (5H <= {min_buffer}%){RESET}"
            elif u_code in ("READY", "LOW_WEEKLY"):
                suitability = f"{GREEN}✓ Ready{RESET}" if u_code == "READY" else f"{YELLOW}⚠ Ready (Weekly low){RESET}"
            elif u_code == "COOLDOWN_5H":
                suitability = f"{YELLOW}⏳ Cooldown (Gemini 5H low){RESET}"
            elif u_code == "CLAUDE_EXHAUSTED":
                suitability = f"{YELLOW}✓ Gemini Ready (Claude exhausted){RESET}"
            else:
                suitability = f"{RED}✗ {u_label}{RESET}"

            rows.append([
                p["id"],
                p["name"],
                p["email"],
                f"{GREEN}● Logged In{RESET}",
                g_5h_str,
                g_wk_str,
                c_wk_str,
                pids_str,
                suitability
            ])

        src_str = f" [excluding source: {src_profile['name']}]" if src_profile else ""
        buffer_str = f" [min reserve buffer: {min_buffer}%]" if min_buffer > 0 else ""
        print(f"\n{BOLD}{CYAN}=== Relay Candidates Assessment{src_str}{buffer_str} ==={RESET}\n")
        if rows:
            print(format_table(rows, headers))
        else:
            print(f"{YELLOW}No other accounts configured. Add another profile with 'agy-multi add'.{RESET}")
        print()
        return 0

    # Case 2: Perform relay
    # 1. Resolve source profile
    src_profile = None
    if getattr(args, "from_profile", None):
        src_profile = manager.find_profile(args.from_profile)
        if not src_profile:
            print(f"{RED}Error: Source profile '{args.from_profile}' not found.{RESET}")
            return 1
    else:
        src_profile = manager.get_active_or_recent_profile()
        if not src_profile:
            print(f"{RED}Error: No active or configured profile found.{RESET}")
            return 1

    # 2. Resolve conversation ID
    cid = getattr(args, "conversation_id", None)
    if not cid:
        recent = manager.get_most_recent_conversation(src_profile["name"])
        if not recent:
            print(f"{RED}Error: No conversation found in source profile '{src_profile['name']}'.{RESET}")
            print("Specify a conversation ID explicitly: agy-multi relay <conversation_id>")
            return 1
        cid = recent["id"]
        convo_title = recent.get("title", f"Conversation {cid[:8]}")
    else:
        convo_title = f"Conversation {cid[:8]}"

    # 3. Resolve target profile
    dst_profile = None
    if getattr(args, "to_profile", None):
        dst_profile = manager.find_profile(args.to_profile)
        if not dst_profile:
            print(f"{RED}Error: Target profile '{args.to_profile}' not found.{RESET}")
            return 1
        if not dst_profile.get("auth", {}).get("is_valid", False):
            print(f"{RED}Error: Target profile '{dst_profile['name']}' is not authenticated.{RESET}")
            print(f"Run first: agy-multi login {dst_profile['id']}")
            return 1
    else:
        dst_profile = manager.find_best_relay_candidate(src_profile["name"], min_buffer_pct=min_buffer)
        if not dst_profile:
            print(f"{RED}Error: No eligible target profile available with ready quota (buffer: {min_buffer}%).{RESET}")
            print("Check candidate status with: agy-multi relay --list-candidates")
            return 1

    # 4. Perform relay
    try:
        res = manager.relay_conversation(src_profile["name"], dst_profile["name"], cid, sync_brain=True)
    except Exception as e:
        print(f"{RED}Error executing conversation relay: {e}{RESET}")
        return 1

    print(f"\n{BOLD}{GREEN}✓ Quota Relay Handover Successful!{RESET}\n")
    print(f"  Conversation : {BOLD}{res['conversation_id']}{RESET} ({res.get('title', convo_title)})")
    print(f"  Relayed From : [{src_profile['id']}] {src_profile['name']} ({src_profile['email']})")
    print(f"  Relayed To   : {BOLD}[{dst_profile['id']}] {dst_profile['name']}{RESET} ({dst_profile['email']})")
    print()

    no_exec = getattr(args, "no_exec", False)
    if no_exec:
        print(f"Session data and artifacts synchronized. To continue this conversation:")
        print(f"  {CYAN}{BOLD}agy-{dst_profile['id']} --conversation {cid}{RESET}")
        print(f"  or: {CYAN}agy-multi run {dst_profile['id']} --conversation {cid}{RESET}\n")
        return 0

    print(f"🚀 {BOLD}Resuming conversation with Profile {dst_profile['id']} ({dst_profile['name']})...{RESET}\n")
    cmd_args = ["--conversation", cid] + remaining_args
    return manager.run_profile(dst_profile["name"], cmd_args, exec_replace=True)


def cmd_run(manager: ProfileManager, args: argparse.Namespace, remaining_args: List[str]) -> int:
    identifier = getattr(args, "identifier", None)
    profile = None
    if identifier:
        profile = manager.find_profile(identifier)
        if not profile:
            print(f"{RED}Error: Profile '{identifier}' not found.{RESET}")
            print("Run `agy-multi list` to see available profiles.")
            return 1

        if not profile["auth"]["is_valid"]:
            print(f"{YELLOW}Warning: Profile '{profile['name']}' is not logged in yet.{RESET}")
            print(f"Please run first: {BOLD}{CYAN}agy-multi login {profile['id']}{RESET}")
            return 1

    # If --direct is specified, bypass supervisor and execvpe directly
    if getattr(args, "direct", False) or "--direct" in remaining_args:
        clean_args = [a for a in remaining_args if a != "--direct"]
        fallback_p = manager.get_active_or_recent_profile() or (manager.list_profiles()[0] if manager.list_profiles() else None)
        target_name = profile["name"] if profile else (fallback_p["name"] if fallback_p else "")
        return manager.run_profile(target_name, clean_args, exec_replace=True)

    # Otherwise run with SessionRunner (includes quota buffer watchdog and auto-relay)
    from .runner import SessionRunner
    runner = SessionRunner(
        manager=manager,
        profile_identifier=profile["name"] if profile else None,
        extra_args=remaining_args,
        on_no_target=getattr(args, "on_no_target", None)
    )
    return runner.run()


def extract_credentials_from_binary() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Inspects installed Antigravity CLI binary to extract built-in Google OAuth credentials."""
    bin_path = shutil.which("agy") or shutil.which("antigravity")
    if not bin_path or not os.path.isfile(bin_path):
        return None, None, None
    try:
        with open(bin_path, "rb") as bf:
            data = bf.read()
        cid_match = re.search(rb"(107[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com)", data)
        sec_match = re.search(rb"(GOCSPX-[A-Za-z0-9_-]{28})", data)
        cid = cid_match.group(1).decode("utf-8") if cid_match else None
        sec = sec_match.group(1).decode("utf-8") if sec_match else None
        return bin_path, cid, sec
    except Exception:
        return bin_path, None, None


def cmd_creds(manager: ProfileManager, args: argparse.Namespace) -> int:
    env_cid = os.environ.get("AGY_OAUTH_CLIENT_ID") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    env_sec = os.environ.get("AGY_OAUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")

    print(f"\n{BOLD}{CYAN}=== Google OAuth Credentials Helper (24/7 Background Refresh) ==={RESET}\n")

    bin_path, bin_cid, bin_sec = extract_credentials_from_binary()
    active_cid = bin_cid or env_cid
    active_sec = bin_sec or env_sec

    if not getattr(args, "save", False):
        if env_cid and env_sec:
            print(f"{GREEN}✓ Active OAuth client credentials detected in environment:{RESET}")
            print(f"  • Client ID    : {env_cid[:15]}...{env_cid[-12:]}")
            print(f"  • Client Secret: {env_sec[:8]}... (Configured)")
            print(f"\nBackground token refresh is {BOLD}{GREEN}Active & Healthy{RESET}.\n")
            return 0

        print(f"{YELLOW}⚠️  No OAuth client credentials found in current environment.{RESET}")
        print("   Background dashboard cannot automatically refresh expired tokens for idle accounts.")
        print("\nAttempting zero-config auto-discovery from local Antigravity binary...")

        if not bin_path:
            print(f"{RED}✗ Could not locate 'agy' or 'antigravity' binary on PATH.{RESET}")
            print("  Please manually set AGY_OAUTH_CLIENT_ID and AGY_OAUTH_CLIENT_SECRET.")
            return 1

        if not bin_cid or not bin_sec:
            print(f"{RED}✗ Located binary at {bin_path}, but could not auto-extract credentials.{RESET}")
            print("  Please manually set AGY_OAUTH_CLIENT_ID and AGY_OAUTH_CLIENT_SECRET.")
            return 1

        print(f"{GREEN}✓ Found installed binary at:{RESET} {bin_path}")
        print(f"{GREEN}✓ Successfully discovered Google Antigravity OAuth client credentials!{RESET}")

        export_block = (
            f'\n# Google Antigravity OAuth Client Credentials (auto-discovered by agy-multi)\n'
            f'export AGY_OAUTH_CLIENT_ID="{bin_cid}"\n'
            f'export AGY_OAUTH_CLIENT_SECRET="{bin_sec}"\n'
        )
        print("\nTo automatically save credentials to your config and shell, run:")
        print(f"{BOLD}{GREEN}  agy-multi creds --save{RESET}")
        print("\nOr manually add the following exports to your ~/.bashrc / service environment:")
        print(f"{CYAN}{export_block}{RESET}")
        return 0

    # Handle --save
    if not active_cid or not active_sec:
        print(f"{RED}✗ Could not locate or extract OAuth client credentials to save.{RESET}")
        print("  Please make sure 'agy' is installed, or set AGY_OAUTH_CLIENT_ID and AGY_OAUTH_CLIENT_SECRET manually.")
        return 1

    if bin_path and bin_cid and bin_sec:
        print(f"{GREEN}✓ Discovered credentials from installed binary:{RESET} {bin_path}")
    else:
        print(f"{GREEN}✓ Using existing environment credentials.{RESET}")

    # Save to ~/.config/agy-multi/env and update ~/.bashrc
    target_homes = [manager.real_home] if manager else [detect_real_home()]
    current_home = Path.home().resolve()
    if current_home not in target_homes:
        target_homes.append(current_home)

    for h in target_homes:
        env_dir = h / ".config" / "agy-multi"
        env_dir.mkdir(parents=True, exist_ok=True)
        env_file = env_dir / "env"
        try:
            env_content = (
                f"# Google Antigravity OAuth Client Credentials for agy-multi\n"
                f'AGY_OAUTH_CLIENT_ID="{active_cid}"\n'
                f'AGY_OAUTH_CLIENT_SECRET="{active_sec}"\n'
            )
            env_file.write_text(env_content, encoding="utf-8")
            env_file.chmod(0o600)
            print(f"{GREEN}✓ Saved credentials to {env_file} (mode 0600){RESET}")
        except Exception as e:
            print(f"{RED}Failed to write {env_file}: {e}{RESET}")

        rc_path = h / ".bashrc"
        try:
            existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
            if "AGY_OAUTH_CLIENT_ID" in existing:
                print(f"{GREEN}✓ Credentials already present in {rc_path}{RESET}")
            else:
                export_block = (
                    f'\n# Google Antigravity OAuth Client Credentials (auto-discovered by agy-multi)\n'
                    f'export AGY_OAUTH_CLIENT_ID="{active_cid}"\n'
                    f'export AGY_OAUTH_CLIENT_SECRET="{active_sec}"\n'
                )
                with open(rc_path, "a", encoding="utf-8") as f:
                    f.write(export_block)
                print(f"{GREEN}✓ Appended credentials export to {rc_path}{RESET}")
        except Exception as e:
            print(f"{RED}Failed to update {rc_path}: {e}{RESET}")

    os.environ["AGY_OAUTH_CLIENT_ID"] = active_cid
    os.environ["AGY_OAUTH_CLIENT_SECRET"] = active_sec
    print(f"\n{BOLD}{GREEN}✓ Configuration saved successfully! Background token refresh is active.{RESET}\n")
    return 0




def cmd_login(manager: ProfileManager, args: argparse.Namespace) -> int:
    profile = manager.find_profile(args.identifier)
    if not profile:
        print(f"{RED}Error: Profile '{args.identifier}' not found.{RESET}")
        return 1

    success = manager.login_profile(args.identifier)
    return 0 if success else 1


def cmd_add(manager: ProfileManager, args: argparse.Namespace) -> int:
    try:
        inherit_from = None
        if getattr(args, "inherit", False):
            inherit_from = "host"
        elif getattr(args, "inherit_from", None):
            inherit_from = args.inherit_from

        new_p = manager.add_profile(
            name=args.name,
            email=args.email,
            description=args.description or "",
            custom_id=getattr(args, "custom_id", None) or getattr(args, "id", None),
            inherit_from=inherit_from,
            inherit_mcp=not getattr(args, "no_mcp", False),
            inherit_skills=not getattr(args, "no_skills", False),
            inherit_plugins=not getattr(args, "no_plugins", False),
            inherit_settings=not getattr(args, "no_settings", False),
            inherit_hooks=not getattr(args, "no_hooks", False),
            copy_mode=getattr(args, "copy", False),
        )
        print(f"{GREEN}✓ Profile added successfully!{RESET}")
        print(f"  ID: {BOLD}{new_p['id']}{RESET}")
        print(f"  Name: {BOLD}{new_p['name']}{RESET}")
        print(f"  Email: {BOLD}{new_p['email']}{RESET}")
        if inherit_from:
            report = new_p.get("inheritance_report", {})
            skills_mark = f"{GREEN}✓{RESET}" if report.get("skills_inherited") else f"{YELLOW}none{RESET}"
            mcp_mark = f"{GREEN}✓{RESET}" if report.get("mcp_inherited") else f"{YELLOW}none{RESET}"
            plugins_mark = f"{GREEN}✓{RESET}" if report.get("plugins_inherited") else f"{YELLOW}none{RESET}"
            settings_mark = f"{GREEN}✓{RESET}" if report.get("settings_inherited") else f"{YELLOW}none{RESET}"
            hooks_mark = f"{GREEN}✓{RESET}" if report.get("hooks_inherited") else f"{YELLOW}none{RESET}"
            print(f"  Inherited: {CYAN}from '{inherit_from}'{RESET} (Skills: {skills_mark}, MCP: {mcp_mark}, Plugins: {plugins_mark}, Settings: {settings_mark}, Hooks: {hooks_mark})")
        print(f"\nTo authenticate this profile, run:")
        print(f"  {CYAN}{BOLD}agy-multi login {new_p['id']}{RESET}\n")
        return 0
    except Exception as e:
        print(f"{RED}Error adding profile: {e}{RESET}")
        return 1


def cmd_clone(manager: ProfileManager, args: argparse.Namespace) -> int:
    try:
        new_p = manager.clone_profile(
            source_identifier=args.source,
            new_name=args.name,
            new_email=args.email,
            description=args.description or "",
            custom_id=getattr(args, "custom_id", None) or getattr(args, "id", None),
            inherit_mcp=not getattr(args, "no_mcp", False),
            inherit_skills=not getattr(args, "no_skills", False),
            inherit_plugins=not getattr(args, "no_plugins", False),
            inherit_settings=not getattr(args, "no_settings", False),
            inherit_hooks=not getattr(args, "no_hooks", False),
            copy_mode=not getattr(args, "link", False),
        )
        print(f"{GREEN}✓ Profile cloned successfully from '{args.source}'!{RESET}")
        print(f"  ID: {BOLD}{new_p['id']}{RESET}")
        print(f"  Name: {BOLD}{new_p['name']}{RESET}")
        print(f"  Email: {BOLD}{new_p['email']}{RESET}")
        report = new_p.get("inheritance_report", {})
        skills_mark = f"{GREEN}✓{RESET}" if report.get("skills_inherited") else f"{YELLOW}none{RESET}"
        mcp_mark = f"{GREEN}✓{RESET}" if report.get("mcp_inherited") else f"{YELLOW}none{RESET}"
        plugins_mark = f"{GREEN}✓{RESET}" if report.get("plugins_inherited") else f"{YELLOW}none{RESET}"
        settings_mark = f"{GREEN}✓{RESET}" if report.get("settings_inherited") else f"{YELLOW}none{RESET}"
        hooks_mark = f"{GREEN}✓{RESET}" if report.get("hooks_inherited") else f"{YELLOW}none{RESET}"
        print(f"  Cloned Items: (Skills: {skills_mark}, MCP: {mcp_mark}, Plugins: {plugins_mark}, Settings: {settings_mark}, Hooks: {hooks_mark})")
        print(f"\nTo authenticate this profile, run:")
        print(f"  {CYAN}{BOLD}agy-multi login {new_p['id']}{RESET}\n")
        return 0
    except Exception as e:
        print(f"{RED}Error cloning profile: {e}{RESET}")
        return 1


def cmd_inherit(manager: ProfileManager, args: argparse.Namespace) -> int:
    try:
        report = manager.inherit_profile_config(
            target_name_or_id=args.target,
            source=args.source or "host",
            inherit_mcp=not getattr(args, "no_mcp", False),
            inherit_skills=not getattr(args, "no_skills", False),
            inherit_plugins=not getattr(args, "no_plugins", False),
            inherit_settings=not getattr(args, "no_settings", False),
            inherit_hooks=not getattr(args, "no_hooks", False),
            copy_mode=getattr(args, "copy", False),
        )
        print(f"{GREEN}✓ Configuration inherited successfully for profile '{args.target}'!{RESET}")
        skills_mark = f"{GREEN}✓{RESET}" if report.get("skills_inherited") else f"{YELLOW}none{RESET}"
        mcp_mark = f"{GREEN}✓{RESET}" if report.get("mcp_inherited") else f"{YELLOW}none{RESET}"
        plugins_mark = f"{GREEN}✓{RESET}" if report.get("plugins_inherited") else f"{YELLOW}none{RESET}"
        settings_mark = f"{GREEN}✓{RESET}" if report.get("settings_inherited") else f"{YELLOW}none{RESET}"
        hooks_mark = f"{GREEN}✓{RESET}" if report.get("hooks_inherited") else f"{YELLOW}none{RESET}"
        print(f"  Source: {CYAN}{report.get('source')}{RESET}")
        print(f"  Items: (Skills: {skills_mark}, MCP: {mcp_mark}, Plugins: {plugins_mark}, Settings: {settings_mark}, Hooks: {hooks_mark})")
        if report.get("copied_items"):
            print(f"  Copied : {', '.join(report['copied_items'])}")
        if report.get("linked_items"):
            print(f"  Linked : {', '.join(report['linked_items'])}")
        return 0
    except Exception as e:
        print(f"{RED}Error inheriting configuration: {e}{RESET}")
        return 1


def cmd_edit(manager: ProfileManager, args: argparse.Namespace) -> int:
    try:
        region_restricted = None
        if getattr(args, "region_restricted", False):
            region_restricted = True
        elif getattr(args, "no_region_restricted", False):
            region_restricted = False

        updated = manager.update_profile(
            identifier=args.identifier,
            new_name=args.name,
            new_email=args.email,
            new_description=args.description,
            region_restricted=region_restricted
        )
        print(f"{GREEN}✓ Profile updated successfully!{RESET}")
        print(f"  ID: {BOLD}{updated['id']}{RESET}")
        print(f"  Name: {BOLD}{updated['name']}{RESET}")
        print(f"  Email: {BOLD}{updated['email']}{RESET}")
        if updated.get("region_restricted"):
            print(f"  Region Status: {YELLOW}⚠️ Region Restricted (Excluded from auto-relay){RESET}")
        else:
            print(f"  Region Status: {GREEN}✓ Normal{RESET}")
        # Reinstall shortcuts
        cmd_install_helpers(manager, args)
        return 0
    except Exception as e:
        print(f"{RED}Error updating profile: {e}{RESET}")
        return 1


def cmd_install_helpers(manager: ProfileManager, args: argparse.Namespace) -> int:
    """Installs agy-multi and shortcut scripts to ~/.local/bin/"""
    local_bin = manager.real_home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)

    # Clean up any existing agy-* shortcuts that are managed by agy-multi
    for f in local_bin.glob("agy-*"):
        if f.is_file():
            try:
                with open(f, "r", encoding="utf-8") as s:
                    content = s.read()
                    if "agy-multi" in content or "gemini-switch" in content:
                        f.unlink()
            except Exception:
                pass

    # Purge legacy gemini-switch binary if present
    legacy_bin = local_bin / "gemini-switch"
    if legacy_bin.exists():
        try:
            legacy_bin.unlink()
            print(f"{GREEN}✓ Removed deprecated command: {legacy_bin}{RESET}")
        except Exception:
            pass

    # 1. Install main agy-multi binary wrapper
    main_bin = local_bin / "agy-multi"
    package_dir = Path(__file__).resolve().parent.parent
    
    script_content = f"""#!/usr/bin/env bash
if [ -f "$HOME/.config/agy-multi/env" ]; then
    set -a
    source "$HOME/.config/agy-multi/env"
    set +a
fi
PYTHONPATH="{package_dir}:$PYTHONPATH" python3 -m agy_multi.cli "$@"
"""
    with open(main_bin, "w", encoding="utf-8") as f:
        f.write(script_content)
    main_bin.chmod(0o755)
    print(f"{GREEN}✓ Installed: {main_bin}{RESET}")

    # 2. Install agy-auto (auto-picks best idle profile, runs with watchdog)
    shortcut_auto = local_bin / "agy-auto"
    with open(shortcut_auto, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env bash
exec "{main_bin}" run "$@"
""")
    shortcut_auto.chmod(0o755)
    print(f"{GREEN}✓ Installed shortcut: {shortcut_auto} (Auto-selects best idle profile & relay watchdog){RESET}")

    # 3. Install shortcuts for each profile (e.g. agy-1, agy-2, agy-work, etc.)
    profiles = manager.list_profiles()
    for p in profiles:
        # ID-based shortcut (agy-1, agy-2)
        shortcut_id = local_bin / f"agy-{p['id']}"
        with open(shortcut_id, "w", encoding="utf-8") as f:
            f.write(f"""#!/usr/bin/env bash
exec "{main_bin}" run "{p['id']}" "$@"
""")
        shortcut_id.chmod(0o755)
        print(f"{GREEN}✓ Installed shortcut: {shortcut_id} -> {p['email']}{RESET}")

        # Name-based shortcut (agy-work, agy-personal)
        shortcut_name = local_bin / f"agy-{p['name']}"
        with open(shortcut_name, "w", encoding="utf-8") as f:
            f.write(f"""#!/usr/bin/env bash
exec "{main_bin}" run "{p['name']}" "$@"
""")
        shortcut_name.chmod(0o755)
        print(f"{GREEN}✓ Installed shortcut: {shortcut_name} -> {p['email']}{RESET}")

    print(f"\n{BOLD}{CYAN}All shortcuts are available in your PATH!{RESET}")
    print("You can run them in any terminal or herdr pane directly.")
    return 0


def cmd_serve(manager, args):
    from .server import start_server
    host = "0.0.0.0" if getattr(args, "lan", False) else args.host
    start_server(port=args.port, host=host, token=getattr(args, "token", None))
    return 0


def cmd_init(manager, args):
    print(f"\n{BOLD}{CYAN}======================================================{RESET}")
    print(f"{BOLD}{CYAN}   🚀 Welcome to agy-multi Setup Wizard              {RESET}")
    print(f"{BOLD}{CYAN}======================================================{RESET}\n")
    print("This wizard will help you configure your Google AI Pro accounts")
    print("for seamless multi-account concurrency and usage monitoring.\n")

    # 1. Check existing Antigravity installation
    default_agy_dir = manager.real_home / ".gemini" / "antigravity-cli"
    default_token = default_agy_dir / "antigravity-oauth-token"
    existing_profiles = manager.list_profiles()

    if not existing_profiles and default_token.is_file():
        print(f"{GREEN}✓ Detected existing Antigravity login at {default_agy_dir}{RESET}")
        try:
            choice = input(f"Import existing login as Profile 1 ('main')? [Y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.")
            return 1
        if choice in ("", "y", "yes"):
            auth_info = inspect_token_file(default_token)
            email = auth_info.get("email") or "primary@developer"
            manager.add_profile(name="main", email=email, description="Default imported profile", custom_id="1")
            print(f"{GREEN}✓ Imported Profile 1 (main) for {email}{RESET}\n")

    # 2. Interactive account addition loop
    while True:
        profiles = manager.list_profiles()
        print(f"\n{BOLD}Current Profiles ({len(profiles)}):{RESET}")
        if profiles:
            rows = []
            for p in profiles:
                status = f"{GREEN}● Logged In{RESET}" if p.get("auth", {}).get("is_valid") else f"{YELLOW}○ Pending{RESET}"
                rows.append([p["id"], p["name"], p["email"], status, p.get("description", "")])
            print(format_table(rows, ["ID", "Name", "Email", "Status", "Description"]))
        else:
            print("  (No profiles configured yet)")

        print("\nOptions:")
        print("  [a] Add another Google account profile")
        print("  [d] Done / Proceed to install shortcuts")
        print("  [q] Quit")
        try:
            action = input("\nChoose an option [a/d/q] (default: d): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.")
            return 1

        if action in ("d", ""):
            break
        elif action == "q":
            print("Setup exited.")
            return 0
        elif action == "a":
            try:
                name = input("Enter profile short name (e.g. coder, reviewer, backup): ").strip()
                if not name:
                    print(f"{RED}Profile name cannot be empty.{RESET}")
                    continue
                email = input("Enter Google account email: ").strip()
                if not email:
                    print(f"{RED}Email cannot be empty.{RESET}")
                    continue
                desc = input("Enter optional description: ").strip()
                manager.add_profile(name=name, email=email, description=desc)
                print(f"{GREEN}✓ Profile '{name}' added successfully!{RESET}")
            except Exception as e:
                print(f"{RED}Error: {e}{RESET}")
        else:
            print(f"{YELLOW}Invalid option.{RESET}")

    # 3. Generate shortcuts
    profiles = manager.list_profiles()
    if profiles:
        print(f"\n{CYAN}Installing global shortcuts to ~/.local/bin...{RESET}")
        cmd_install_helpers(manager, args)

        print(f"\n{BOLD}{GREEN}🎉 Setup Complete!{RESET}")
        print("Next steps:")
        print(f"  1. Run {BOLD}agy-multi list{RESET} to view all configured profiles.")
        unauthed = [p for p in profiles if not p.get("auth", {}).get("is_valid")]
        if unauthed:
            print(f"  2. Authenticate newly added profiles:")
            for p in unauthed:
                print(f"     {CYAN}agy-multi login {p['id']}{RESET}  ({p['email']})")
        print(f"  3. Run {BOLD}agy-auto{RESET} (auto-picks best idle account) or {BOLD}agy-1{RESET}, {BOLD}agy-2{RESET}, etc. in any terminal or tmux/herdr pane.")
        print(f"  4. Run {BOLD}agy-multi serve{RESET} to launch the real-time web dashboard.")
    else:
        print(f"\n{YELLOW}No profiles configured yet. Run 'agy-multi init' anytime to get started.{RESET}")

    return 0


def main():
    load_env_config()
    parser = argparse.ArgumentParser(
        prog="agy-multi",
        description="Google AI Pro multi-account manager and runner for Antigravity (agy)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    # init / setup
    subparsers.add_parser("init", aliases=["setup"], help="Interactive setup wizard to initialize profiles and shortcuts")

    # list / ls
    subparsers.add_parser("list", aliases=["ls"], help="List all configured profiles and their runtime status")

    # status
    subparsers.add_parser("status", help="Show current profile, quota health, and explainable relay decisions")

    # run
    p_run = subparsers.add_parser("run", help="Run agy with a specific profile or auto-pick best")
    p_run.add_argument("identifier", nargs="?", default=None, help="Profile ID, name, or email (optional)")
    p_run.add_argument("--direct", action="store_true", help="Bypass supervisor and directly execvpe into agy")
    p_run.add_argument("--on-no-target", choices=["pause", "burn_buffer"], default=None, help="Fallback behavior when no relay targets are available")

    # login
    p_login = subparsers.add_parser("login", aliases=["auth"], help="Trigger Google OAuth authentication for a profile")
    p_login.add_argument("identifier", help="Profile ID, name, or email")

    # add
    p_add = subparsers.add_parser("add", help="Add a new profile")
    p_add.add_argument("name", help="Profile short name (e.g. coder, reviewer)")
    p_add.add_argument("email", help="Google account email")
    p_add.add_argument("-d", "--description", default="", help="Profile description or purpose")
    p_add.add_argument("--id", dest="custom_id", default=None, help="Custom numeric or short ID")
    p_add.add_argument("--inherit", action="store_true", help="Inherit non-credential configs, skills, and MCP from host")
    p_add.add_argument("--inherit-from", default=None, help="Inherit non-credential configs from specified profile or 'host'")
    p_add.add_argument("--no-mcp", action="store_true", help="Exclude MCP tool configuration when inheriting")
    p_add.add_argument("--no-skills", action="store_true", help="Exclude Agent skills when inheriting")
    p_add.add_argument("--no-plugins", action="store_true", help="Exclude Agent plugins when inheriting")
    p_add.add_argument("--no-settings", action="store_true", help="Exclude settings.json when inheriting")
    p_add.add_argument("--copy", action="store_true", help="Deep-copy inherited configs instead of symlinking")

    # clone
    p_clone = subparsers.add_parser("clone", help="Clone non-credential configs, skills, and MCP from an existing profile into a new profile")
    p_clone.add_argument("source", help="Source profile ID or name to clone from")
    p_clone.add_argument("name", help="New profile short name")
    p_clone.add_argument("email", help="New Google account email")
    p_clone.add_argument("-d", "--description", default="", help="Profile description or purpose")
    p_clone.add_argument("--id", dest="custom_id", default=None, help="Custom numeric or short ID")
    p_clone.add_argument("--no-mcp", action="store_true", help="Exclude MCP tool configuration")
    p_clone.add_argument("--no-skills", action="store_true", help="Exclude Agent skills")
    p_clone.add_argument("--no-plugins", action="store_true", help="Exclude Agent plugins")
    p_clone.add_argument("--no-settings", action="store_true", help="Exclude settings.json")
    p_clone.add_argument("--link", action="store_true", help="Symlink configs instead of deep-copying")

    # inherit / sync-config
    p_inherit = subparsers.add_parser("inherit", aliases=["sync-config"], help="Inherit or sync non-credential configs, skills, or MCP into an existing profile")
    p_inherit.add_argument("target", help="Target profile ID or name")
    p_inherit.add_argument("--from", dest="source", default="host", help="Source profile or 'host' (default: host)")
    p_inherit.add_argument("--no-mcp", action="store_true", help="Exclude MCP tool configuration")
    p_inherit.add_argument("--no-skills", action="store_true", help="Exclude Agent skills")
    p_inherit.add_argument("--no-plugins", action="store_true", help="Exclude Agent plugins")
    p_inherit.add_argument("--no-settings", action="store_true", help="Exclude settings.json")
    p_inherit.add_argument("--copy", action="store_true", help="Deep-copy inherited configs instead of symlinking")

    # edit / update
    p_edit = subparsers.add_parser("edit", aliases=["update"], help="Edit an existing profile")
    p_edit.add_argument("identifier", help="Profile ID, name, or email")
    p_edit.add_argument("--name", help="New profile short name")
    p_edit.add_argument("--email", help="New Google account email")
    p_edit.add_argument("-d", "--description", help="New description or notes")
    p_edit.add_argument("--region-restricted", action="store_true", help="Mark profile as region-restricted (excluded from auto-relay)")
    p_edit.add_argument("--no-region-restricted", action="store_true", help="Clear region-restricted mark")

    # install
    subparsers.add_parser("install", help="Install global commands and agy-N shortcuts")

    # usage / stats / dashboard
    p_usage = subparsers.add_parser("usage", aliases=["stats", "dashboard"], help="Show usage statistics and generate HTML dashboard")
    p_usage.add_argument("--html", nargs="?", const="", help="Specify path to output HTML dashboard")
    p_usage.add_argument("--json", action="store_true", help="Output raw JSON data")
    p_usage.add_argument("--csv", action="store_true", help="Output raw CSV data")

    # serve
    p_serve = subparsers.add_parser("serve", help="Run real-time usage dashboard web server")
    p_serve.add_argument("--port", type=int, default=8989, help="Server port (default: 8989)")
    p_serve.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    p_serve.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 for LAN access (generates or requires --token)")
    p_serve.add_argument("--token", default=None, help="Authentication token for mutating API endpoints")

    # config
    p_config = subparsers.add_parser("config", help="View or modify global configuration (e.g. reserve quota buffer)")
    p_config.add_argument("--min-buffer", type=float, help="Set minimum quota buffer percentage (e.g. 5 for 5%)")
    p_config.add_argument("--auto-relay", dest="auto_relay", action="store_true", default=None, help="Enable automatic quota takeover")
    p_config.add_argument("--no-auto-relay", dest="auto_relay", action="store_false", help="Disable automatic quota takeover (pause only)")
    p_config.add_argument("--on-no-target", choices=["pause", "burn_buffer"], help="Set fallback policy when no relay target is available")

    # relay
    p_relay = subparsers.add_parser("relay", help="Relay an active conversation / quota to another profile")
    p_relay.add_argument("conversation_id", nargs="?", default=None, help="Conversation ID to relay (default: most recent conversation)")
    p_relay.add_argument("--from", dest="from_profile", help="Source profile ID or name (default: auto-detect active)")
    p_relay.add_argument("--to", dest="to_profile", help="Target profile ID or name (default: auto-select best candidate)")
    p_relay.add_argument("--no-exec", action="store_true", help="Sync conversation session without launching agy")
    p_relay.add_argument("--list-candidates", action="store_true", help="List eligible target profiles and their quota scores")
    p_relay.add_argument("--min-buffer", type=float, help="Override minimum quota buffer percentage for this relay (e.g. 5 for 5%)")

    # creds / auth-helper
    p_creds = subparsers.add_parser("creds", help="Inspect or auto-discover OAuth client credentials for 24/7 background token refresh")
    p_creds.add_argument("--save", action="store_true", help="Automatically discover credentials from local agy binary and append to ~/.bashrc")

    # Parse known args so trailing args can be forwarded to agy in `run` and `relay`
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        if "-h" in sys.argv or "--help" in sys.argv:
            p_run.print_help()
            sys.exit(0)
        # Handle `agy-multi run [id] [extra args...]`
        target_id = None
        remaining = []
        if len(sys.argv) > 2:
            if sys.argv[2].startswith("-"):
                target_id = None
                remaining = sys.argv[2:]
            else:
                target_id = sys.argv[2]
                remaining = sys.argv[3:]
        manager = ProfileManager()
        args = argparse.Namespace(command="run", identifier=target_id)
        sys.exit(cmd_run(manager, args, remaining))
    elif len(sys.argv) > 1 and sys.argv[1] == "relay":
        # Handle `agy-multi relay [cid] [--from ...] [--to ...] [--no-exec] [--list-candidates] [extra agy args...]`
        parsed_args, remaining = p_relay.parse_known_args(sys.argv[2:])
        manager = ProfileManager()
        sys.exit(cmd_relay(manager, parsed_args, remaining))

    args = parser.parse_args()
    manager = ProfileManager()

    if args.command in ("list", "ls", None):
        if args.command is None:
            cmd_list(manager, args)
            parser.print_help()
            sys.exit(0)
        sys.exit(cmd_list(manager, args))
    elif args.command == "status":
        sys.exit(cmd_status(manager, args))
    elif args.command in ("init", "setup"):
        sys.exit(cmd_init(manager, args))
    elif args.command in ("usage", "stats", "dashboard"):
        sys.exit(cmd_usage(manager, args))
    elif args.command == "serve":
        sys.exit(cmd_serve(manager, args))
    elif args.command == "config":
        sys.exit(cmd_config(manager, args))
    elif args.command == "relay":
        sys.exit(cmd_relay(manager, args, []))
    elif args.command == "login":
        sys.exit(cmd_login(manager, args))
    elif args.command == "add":
        sys.exit(cmd_add(manager, args))
    elif args.command == "clone":
        sys.exit(cmd_clone(manager, args))
    elif args.command in ("inherit", "sync-config"):
        sys.exit(cmd_inherit(manager, args))
    elif args.command in ("edit", "update"):
        sys.exit(cmd_edit(manager, args))
    elif args.command == "creds":
        sys.exit(cmd_creds(manager, args))
    elif args.command == "install":
        sys.exit(cmd_install_helpers(manager, args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
