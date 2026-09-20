"""
agy_multi.usage
Collects, aggregates, and renders usage statistics (tokens, requests, 5h rolling window,
weekly usage, calendar heatmap) across all Antigravity profiles.
"""

import os
import json
import sqlite3
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional


def decode_varint(data: bytes, pos: int):
    val = 0
    shift = 0
    while True:
        if pos >= len(data):
            return val, pos
        b = data[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, pos


def get_next_monday_utc(current_ts: float) -> float:
    """Calculates the UNIX timestamp for the next Monday at 00:00 UTC."""
    now_dt = datetime.fromtimestamp(current_ts, tz=timezone.utc)
    days_ahead = (7 - now_dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_monday = (now_dt + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_monday.timestamp()


def parse_step_metadata(data: bytes) -> Dict[str, Any]:
    """Parses protobuf metadata blob from the steps table in conversation db."""
    res = {}
    pos = 0
    while pos < len(data):
        key, pos = decode_varint(data, pos)
        field_num = key >> 3
        wire_type = key & 0x7
        if wire_type == 0:
            val, pos = decode_varint(data, pos)
        elif wire_type == 2:
            length, pos = decode_varint(data, pos)
            val = data[pos:pos+length]
            pos += length
            if field_num == 1:  # Timestamp container
                sub_pos = 0
                while sub_pos < len(val):
                    sub_k, sub_pos = decode_varint(val, sub_pos)
                    fn = sub_k >> 3
                    wt = sub_k & 0x7
                    if wt == 0:
                        v, sub_pos = decode_varint(val, sub_pos)
                        if fn == 1:
                            res["timestamp"] = v
                    elif wt == 2:
                        l, sub_pos = decode_varint(val, sub_pos)
                        sub_pos += l
            elif field_num == 9:  # Token usage container
                sub_pos = 0
                while sub_pos < len(val):
                    sub_k, sub_pos = decode_varint(val, sub_pos)
                    fn = sub_k >> 3
                    wt = sub_k & 0x7
                    if wt == 0:
                        v, sub_pos = decode_varint(val, sub_pos)
                        if fn == 1:
                            res["system_tokens"] = v
                        elif fn == 2:
                            res["prompt_tokens"] = v
                        elif fn == 3:
                            res["candidate_tokens"] = v
                        elif fn == 5:
                            res["cached_tokens"] = v
                        elif fn == 9:
                            res["thinking_tokens"] = v
                        elif fn == 10:
                            res["output_tokens"] = v
                    elif wt == 2:
                        l, sub_pos = decode_varint(val, sub_pos)
                        sub_pos += l
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            break
    return res


def get_oauth_client_credentials() -> tuple[str, str]:
    """Read OAuth client credentials from environment only (no built-in secrets).

    Supported env vars:
      - AGY_OAUTH_CLIENT_ID / AGY_OAUTH_CLIENT_SECRET (preferred)
      - GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET (aliases)
    """
    client_id = (
        os.environ.get("AGY_OAUTH_CLIENT_ID")
        or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        or ""
    ).strip()
    client_secret = (
        os.environ.get("AGY_OAUTH_CLIENT_SECRET")
        or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
        or ""
    ).strip()
    return client_id, client_secret


# Back-compat aliases: always empty at import; prefer get_oauth_client_credentials().
CLIENT_ID = ""
CLIENT_SECRET = ""

_MISSING_OAUTH_CREDS_REASON = (
    "OAuth client credentials missing; cannot refresh token. "
    "Set AGY_OAUTH_CLIENT_ID / AGY_OAUTH_CLIENT_SECRET "
    "(or GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET), "
    "or re-login with `agy` / `agy-multi login`. | "
    "缺少 OAuth 客户端凭证，无法刷新 Token。"
    "请设置 AGY_OAUTH_CLIENT_ID / AGY_OAUTH_CLIENT_SECRET"
    "（或 GOOGLE_OAUTH_* 别名），或重新执行 `agy` / `agy-multi login` 登录。"
)


def fetch_official_quota(profile_dir: Path) -> Dict[str, Any]:
    """
    Fetches real-time official model quota from Google Antigravity prediction service:
    POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary
    Automatically refreshes expired access tokens using the refresh_token if client credentials are provided.
    """
    token_file = profile_dir / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if not token_file.is_file():
        return {"available": False, "reason": "未登录 (无 Token)"}

    try:
        with open(token_file, "r", encoding="utf-8") as f:
            auth_data = json.load(f)
    except Exception as e:
        return {"available": False, "reason": f"Token 读取失败: {e}"}

    token_obj = auth_data.get("token", {})
    access_token = token_obj.get("access_token")
    refresh_token = token_obj.get("refresh_token")

    def call_api(tok: str) -> Optional[Dict[str, Any]]:
        # daily-cloudcode-pa.googleapis.com is the prediction backend used by Antigravity CLI
        # that tracks real-time 5H and weekly usage buckets.
        for host in ["daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com"]:
            try:
                req = urllib.request.Request(
                    f"https://{host}/v1internal:retrieveUserQuotaSummary",
                    data=b"{}",
                    headers={
                        "Authorization": f"Bearer {tok}",
                        "Content-Type": "application/json",
                        "User-Agent": "antigravity-cli",
                    },
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    return None
                continue
            except Exception:
                continue
        return None

    is_token_expired = False
    if token_obj.get("expiry"):
        try:
            exp_str = str(token_obj["expiry"]).replace("Z", "+00:00")
            dt = datetime.fromisoformat(exp_str)
            is_token_expired = dt.timestamp() < time.time()
        except Exception:
            pass

    data = None
    if access_token and not is_token_expired:
        try:
            data = call_api(access_token)
        except Exception:
            pass

    # If access token was expired or missing, try refresh if credentials are configured
    if (is_token_expired or not access_token) and refresh_token:
        client_id, client_secret = get_oauth_client_credentials()
        if not client_id or not client_secret:
            return {
                "available": False,
                "reason": _MISSING_OAUTH_CREDS_REASON,
            }
        post_data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        r_req = urllib.request.Request("https://oauth2.googleapis.com/token", data=post_data)
        try:
            with urllib.request.urlopen(r_req, timeout=8) as r_resp:
                new_token_info = json.loads(r_resp.read().decode("utf-8"))
                new_access_token = new_token_info.get("access_token")
                if new_access_token:
                    token_obj["access_token"] = new_access_token
                    if "expires_in" in new_token_info:
                        token_obj["expiry"] = datetime.fromtimestamp(
                            time.time() + new_token_info["expires_in"], tz=timezone.utc
                        ).isoformat()
                    auth_data["token"] = token_obj
                    try:
                        with open(token_file, "w", encoding="utf-8") as f:
                            json.dump(auth_data, f, indent=2)
                        try:
                            token_file.chmod(0o600)
                        except OSError:
                            pass
                    except Exception:
                        pass
                    data = call_api(new_access_token)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            if "invalid_grant" in err_body.lower() or e.code == 400:
                return {"available": False, "reason": "Refresh Token 已失效，需重新登录 (agy-multi login)"}
            return {"available": False, "reason": f"Token 刷新失败 (HTTP {e.code})"}
        except Exception as e:
            return {"available": False, "reason": f"Token 刷新失败: {e}"}

    if not data:
        return {"available": False, "reason": "无法获取配额数据"}

    res = {"available": True, "raw": data, "groups": {}}
    for g in data.get("groups", []):
        g_name = g.get("displayName", "")
        g_key = "gemini" if "Gemini" in g_name else "claude_gpt"
        g_info = {
            "displayName": g_name,
            "description": g.get("description", ""),
            "buckets": {},
        }
        for b in g.get("buckets", []):
            b_id = b.get("bucketId", "")
            rem = b.get("remainingFraction", 1.0)
            reset_time = b.get("resetTime")
            reset_ts = None
            if reset_time:
                try:
                    dt = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
                    reset_ts = dt.timestamp()
                except Exception:
                    pass
            g_info["buckets"][b_id] = {
                "bucketId": b_id,
                "displayName": b.get("displayName", ""),
                "window": b.get("window", ""),
                "remainingFraction": rem,
                "remainingPct": round(rem * 100, 2),
                "resetTime": reset_time,
                "resetTs": reset_ts,
                "disabled": b.get("disabled", False),
                "description": b.get("description", ""),
            }
        res["groups"][g_key] = g_info
    return res


def get_profile_usage(profile: Dict[str, Any], current_ts: Optional[float] = None, min_buffer_pct: float = 0.0) -> Dict[str, Any]:
    """Extracts usage statistics for a single profile including calendar heatmap and usability assessment."""
    if current_ts is None:
        current_ts = time.time()

    pdir = Path(profile["profile_dir"])
    cli_dir = pdir / ".gemini" / "antigravity-cli"
    convos_dir = cli_dir / "conversations"
    summaries_db = cli_dir / "conversation_summaries.db"

    # 1. Map conversation summaries
    convo_titles = {}
    if summaries_db.is_file():
        try:
            conn = sqlite3.connect(f"file:{summaries_db.resolve()}?mode=ro", uri=True, timeout=5.0)
            cur = conn.cursor()
            cur.execute("SELECT conversation_id, title, last_modified_time FROM conversation_summaries;")
            for cid, title, last_mod in cur.fetchall():
                convo_titles[cid] = {
                    "title": title or "Untitled Conversation",
                    "last_modified": last_mod or ""
                }
            conn.close()
        except Exception:
            pass

    # 2. Iterate conversations
    window_5h_threshold = current_ts - (5 * 3600)
    window_7d_threshold = current_ts - (7 * 86400)

    stats_5h = {"requests": 0, "prompt_tokens": 0, "candidate_tokens": 0, "cached_tokens": 0, "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    stats_7d = {"requests": 0, "prompt_tokens": 0, "candidate_tokens": 0, "cached_tokens": 0, "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    stats_all = {"requests": 0, "prompt_tokens": 0, "candidate_tokens": 0, "cached_tokens": 0, "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    timestamps_5h = []
    timestamps_7d = []

    # Daily breakdown for past 7 days: [date_str] -> dict
    daily_stats = {}
    for i in range(7):
        d_str = (datetime.fromtimestamp(current_ts) - timedelta(days=6 - i)).strftime("%Y-%m-%d")
        daily_stats[d_str] = {"requests": 0, "prompt_tokens": 0, "candidate_tokens": 0, "cached_tokens": 0, "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    # Calendar Heatmap breakdown (Starting from 2026-09-01)
    start_dt = datetime(2026, 9, 1).date()
    end_dt = datetime.fromtimestamp(current_ts).date()
    calendar_days_count = max(1, (end_dt - start_dt).days + 1)
    calendar_stats = {}
    for i in range(calendar_days_count):
        d_str = (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        calendar_stats[d_str] = {
            "date": d_str,
            "requests": 0,
            "prompt_tokens": 0,
            "candidate_tokens": 0,
            "cached_tokens": 0,
            "thinking_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    conversations_list = []

    if convos_dir.is_dir():
        for db_path in convos_dir.glob("*.db"):
            cid = db_path.stem
            meta_info = convo_titles.get(cid, {"title": f"Conversation {cid[:8]}", "last_modified": ""})

            convo_reqs = 0
            convo_prompt = 0
            convo_candidate = 0
            convo_cached = 0
            convo_thinking = 0
            convo_output = 0
            convo_steps = []

            try:
                conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=5.0)
                cur = conn.cursor()
                cur.execute("SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx ASC;")
                for idx, raw_meta in cur.fetchall():
                    usage = parse_step_metadata(raw_meta)
                    if not usage or ("prompt_tokens" not in usage and "cached_tokens" not in usage):
                        continue

                    ts = usage.get("timestamp", 0)
                    p_tok = usage.get("prompt_tokens", 0)
                    c_tok = usage.get("candidate_tokens", 0)
                    cached_tok = usage.get("cached_tokens", 0)
                    th_tok = usage.get("thinking_tokens", 0)
                    out_tok = usage.get("output_tokens", 0)
                    tot_tok = p_tok + c_tok + cached_tok

                    convo_reqs += 1
                    convo_prompt += p_tok
                    convo_candidate += c_tok
                    convo_cached += cached_tok
                    convo_thinking += th_tok
                    convo_output += out_tok

                    dt_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
                    d_key = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

                    step_info = {
                        "step_idx": idx,
                        "timestamp": ts,
                        "time_str": dt_str,
                        "prompt_tokens": p_tok,
                        "candidate_tokens": c_tok,
                        "cached_tokens": cached_tok,
                        "thinking_tokens": th_tok,
                        "output_tokens": out_tok,
                        "total_tokens": tot_tok,
                    }
                    convo_steps.append(step_info)

                    # Accumulate All Time
                    stats_all["requests"] += 1
                    stats_all["prompt_tokens"] += p_tok
                    stats_all["candidate_tokens"] += c_tok
                    stats_all["cached_tokens"] += cached_tok
                    stats_all["thinking_tokens"] += th_tok
                    stats_all["output_tokens"] += out_tok
                    stats_all["total_tokens"] += tot_tok

                    # Accumulate Calendar Stats
                    if d_key in calendar_stats:
                        calendar_stats[d_key]["requests"] += 1
                        calendar_stats[d_key]["prompt_tokens"] += p_tok
                        calendar_stats[d_key]["candidate_tokens"] += c_tok
                        calendar_stats[d_key]["cached_tokens"] += cached_tok
                        calendar_stats[d_key]["thinking_tokens"] += th_tok
                        calendar_stats[d_key]["output_tokens"] += out_tok
                        calendar_stats[d_key]["total_tokens"] += tot_tok

                    # Accumulate 7 Days
                    if ts >= window_7d_threshold:
                        stats_7d["requests"] += 1
                        stats_7d["prompt_tokens"] += p_tok
                        stats_7d["candidate_tokens"] += c_tok
                        stats_7d["cached_tokens"] += cached_tok
                        stats_7d["thinking_tokens"] += th_tok
                        stats_7d["output_tokens"] += out_tok
                        stats_7d["total_tokens"] += tot_tok
                        timestamps_7d.append(ts)
                        if d_key in daily_stats:
                            daily_stats[d_key]["requests"] += 1
                            daily_stats[d_key]["prompt_tokens"] += p_tok
                            daily_stats[d_key]["candidate_tokens"] += c_tok
                            daily_stats[d_key]["cached_tokens"] += cached_tok
                            daily_stats[d_key]["thinking_tokens"] += th_tok
                            daily_stats[d_key]["output_tokens"] += out_tok
                            daily_stats[d_key]["total_tokens"] += tot_tok

                    # Accumulate 5 Hours
                    if ts >= window_5h_threshold:
                        stats_5h["requests"] += 1
                        stats_5h["prompt_tokens"] += p_tok
                        stats_5h["candidate_tokens"] += c_tok
                        stats_5h["cached_tokens"] += cached_tok
                        stats_5h["thinking_tokens"] += th_tok
                        stats_5h["output_tokens"] += out_tok
                        stats_5h["total_tokens"] += tot_tok
                        timestamps_5h.append(ts)

                conn.close()

                if convo_reqs > 0:
                    conversations_list.append({
                        "id": cid,
                        "title": meta_info["title"],
                        "last_modified": meta_info["last_modified"],
                        "requests": convo_reqs,
                        "prompt_tokens": convo_prompt,
                        "candidate_tokens": convo_candidate,
                        "cached_tokens": convo_cached,
                        "thinking_tokens": convo_thinking,
                        "output_tokens": convo_output,
                        "total_tokens": convo_prompt + convo_candidate + convo_cached,
                        "steps": convo_steps[-20:]  # keep recent 20 steps for detail
                    })

            except Exception:
                pass

    conversations_list.sort(key=lambda c: c["last_modified"], reverse=True)

    # 5H Reset Calculations
    if timestamps_5h:
        earliest_5h = min(timestamps_5h)
        latest_5h = max(timestamps_5h)
        stats_5h["earliest_ts"] = earliest_5h
        stats_5h["latest_ts"] = latest_5h
        stats_5h["next_reset_ts"] = earliest_5h + (5 * 3600)
        stats_5h["full_reset_ts"] = latest_5h + (5 * 3600)
        stats_5h["next_reset_seconds"] = max(0, int(stats_5h["next_reset_ts"] - current_ts))
        stats_5h["full_reset_seconds"] = max(0, int(stats_5h["full_reset_ts"] - current_ts))
    else:
        stats_5h["earliest_ts"] = None
        stats_5h["latest_ts"] = None
        stats_5h["next_reset_ts"] = None
        stats_5h["full_reset_ts"] = None
        stats_5h["next_reset_seconds"] = 0
        stats_5h["full_reset_seconds"] = 0

    # Weekly Reset Calculations (7-day rolling window)
    if timestamps_7d:
        earliest_7d = min(timestamps_7d)
        latest_7d = max(timestamps_7d)
        stats_7d["earliest_ts"] = earliest_7d
        stats_7d["latest_ts"] = latest_7d
        stats_7d["next_reset_ts"] = earliest_7d + (7 * 86400)
        stats_7d["full_reset_ts"] = latest_7d + (7 * 86400)
        stats_7d["next_reset_seconds"] = max(0, int(stats_7d["next_reset_ts"] - current_ts))
        stats_7d["full_reset_seconds"] = max(0, int(stats_7d["full_reset_ts"] - current_ts))
    else:
        stats_7d["earliest_ts"] = None
        stats_7d["latest_ts"] = None
        stats_7d["next_reset_ts"] = None
        stats_7d["full_reset_ts"] = None
        stats_7d["next_reset_seconds"] = 0
        stats_7d["full_reset_seconds"] = 0

    # Fetch Official Quota from Google Antigravity Prediction Service
    official_quota = fetch_official_quota(pdir)

    gemini_q = official_quota.get("groups", {}).get("gemini", {}).get("buckets", {})
    claude_q = official_quota.get("groups", {}).get("claude_gpt", {}).get("buckets", {})

    g_5h = gemini_q.get("gemini-5h")
    g_wk = gemini_q.get("gemini-weekly")
    c_5h = claude_q.get("3p-5h")
    c_wk = claude_q.get("3p-weekly")

    # Comprehensive Usability Assessment
    g_exhausted = bool(g_wk and g_wk.get("remainingFraction", 1.0) == 0)
    c_exhausted = bool(c_wk and c_wk.get("remainingFraction", 1.0) == 0)
    if official_quota.get("available"):
        weekly_exhausted = g_exhausted
    else:
        weekly_exhausted = bool(profile.get("weekly_quota_exhausted", False))

    g_5h_disabled = bool(g_5h and g_5h.get("disabled", False))
    g_5h_rem = g_5h.get("remainingPct", 100) if g_5h else 100.0

    auth_valid = bool(profile.get("auth", {}).get("is_valid", False))
    p_name = profile.get("name", "")

    # Check token expiry directly from token file as well
    token_file = pdir / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token_is_expired = False
    if token_file.is_file():
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                t_data = json.load(f)
            t_exp = t_data.get("token", {}).get("expiry")
            if t_exp:
                exp_str = str(t_exp).replace("Z", "+00:00")
                token_is_expired = datetime.fromisoformat(exp_str).timestamp() < time.time()
        except Exception:
            pass

    oq_reason = str(official_quota.get("reason", ""))
    oq_reason_lower = oq_reason.lower()
    is_region = (
        profile.get("region_restricted")
        or "地区" in profile.get("description", "")
        or "region" in oq_reason_lower
        or "403" in oq_reason
    )

    is_target_eligible = True
    target_ineligible_reason = ""

    if not auth_valid:
        usability = {
            "code": "NOT_AUTH",
            "label": "○ 未登录",
            "badge_class": "status-not-auth",
            "is_usable": False,
            "reason": "未登录 Google 账号，需执行 login",
        }
        is_target_eligible = False
        target_ineligible_reason = "未登录 Google 账号"
    elif is_region:
        usability = {
            "code": "REGION_PENDING",
            "label": "⚠️ 地区受限",
            "badge_class": "status-pending",
            "is_usable": False,
            "reason": "Google 账号地区受限",
        }
        is_target_eligible = False
        target_ineligible_reason = "Google 账号地区受限"
    elif token_is_expired or (not official_quota.get("available") and ("过期" in oq_reason or "expired" in oq_reason_lower or "invalid_grant" in oq_reason_lower)):
        usability = {
            "code": "TOKEN_EXPIRED",
            "label": "⚠️ 凭证已过期",
            "badge_class": "status-pending",
            "is_usable": False,
            "reason": oq_reason or "Access Token 已过期，请重新登录或刷新凭证",
        }
        is_target_eligible = False
        target_ineligible_reason = "凭证已过期 (需刷新或登录)"
    elif g_exhausted and c_exhausted:
        usability = {
            "code": "WEEKLY_EXHAUSTED",
            "label": "🚫 周额度已耗尽",
            "badge_class": "status-exhausted",
            "is_usable": False,
            "reason": "Gemini 与 Claude/GPT 周配额均已耗尽",
        }
        is_target_eligible = False
        target_ineligible_reason = "周配额已耗尽"
    elif g_exhausted:
        usability = {
            "code": "WEEKLY_EXHAUSTED",
            "label": "🚫 Gemini周额耗尽",
            "badge_class": "status-exhausted",
            "is_usable": False,
            "reason": "Gemini 周配额已达上限",
        }
        is_target_eligible = False
        target_ineligible_reason = "Gemini 周配额已耗尽"
    elif g_5h_disabled:
        usability = {
            "code": "COOLDOWN_5H",
            "label": "⏳ 5H限额停用",
            "badge_class": "status-cooldown",
            "is_usable": False,
            "reason": "周配额已耗尽，Gemini 5小时限额停用",
        }
        is_target_eligible = False
        target_ineligible_reason = "5小时配额已停用"
    elif g_5h and g_5h.get("remainingFraction", 1.0) == 0:
        usability = {
            "code": "COOLDOWN_5H",
            "label": "⏳ 5H已耗尽 (0%)",
            "badge_class": "status-cooldown",
            "is_usable": False,
            "reason": "Gemini 5小时配额已完全耗尽，等待窗口重置",
        }
        is_target_eligible = False
        target_ineligible_reason = "5小时配额已完全耗尽 (0%)"
    elif g_5h and min_buffer_pct > 0 and g_5h_rem <= min_buffer_pct:
        usability = {
            "code": "COOLDOWN_5H",
            "label": f"⏳ 触碰保底 ({g_5h_rem:.1f}%)",
            "badge_class": "status-cooldown",
            "is_usable": False,
            "reason": f"Gemini 5小时配额触碰预设保底余量 ({min_buffer_pct}%)",
        }
        is_target_eligible = False
        target_ineligible_reason = f"配额低于保底余量 ({g_5h_rem:.1f}% <= {min_buffer_pct}%)"
    elif c_exhausted and not g_exhausted:
        usability = {
            "code": "CLAUDE_EXHAUSTED",
            "label": "⚠️ Claude已耗尽",
            "badge_class": "status-cooldown",
            "is_usable": True,
            "reason": "Claude/GPT 周配额已耗尽，Gemini 可用",
        }
        is_target_eligible = True
        target_ineligible_reason = ""
    elif g_5h and g_5h.get("remainingFraction", 1.0) < 0.1:
        usability = {
            "code": "COOLDOWN_5H",
            "label": f"⏳ 5H余量低 ({g_5h_rem:.1f}%)",
            "badge_class": "status-cooldown",
            "is_usable": False,
            "reason": "Gemini 5小时配额余量不足 10%",
        }
        is_target_eligible = False
        target_ineligible_reason = f"5小时配额不足 10% ({g_5h_rem:.1f}%)"
    elif (g_wk and g_wk.get("remainingFraction", 1.0) < 1.0) or (g_5h and g_5h.get("remainingFraction", 1.0) < 1.0):
        g_rem_wk = g_wk.get("remainingPct", 100) if g_wk else 100.0
        usability = {
            "code": "READY",
            "label": f"● 可用 (周: {g_rem_wk:.1f}% | 5H: {g_5h_rem:.1f}%)",
            "badge_class": "status-ready",
            "is_usable": True,
            "reason": f"Gemini 周度剩余 {g_rem_wk:.1f}%, 5小时剩余 {g_5h_rem:.1f}%",
        }
        is_target_eligible = True
        target_ineligible_reason = ""
    elif not official_quota.get("available"):
        usability = {
            "code": "READY",
            "label": "● 可用",
            "badge_class": "status-ready",
            "is_usable": True,
            "reason": "凭证有效 (未获取官方配额)",
        }
        is_target_eligible = True
        target_ineligible_reason = ""
    elif stats_5h["requests"] > 0:
        usability = {
            "code": "READY",
            "label": "● 可用",
            "badge_class": "status-ready",
            "is_usable": True,
            "reason": "配额充裕",
        }
        is_target_eligible = True
        target_ineligible_reason = ""
    else:
        usability = {
            "code": "READY",
            "label": "● 满额可用",
            "badge_class": "status-ready",
            "is_usable": True,
            "reason": "配额充裕 (100%)",
        }
        is_target_eligible = True
        target_ineligible_reason = ""

    usability["is_target_eligible"] = is_target_eligible
    usability["target_ineligible_reason"] = target_ineligible_reason

    return {
        "id": profile["id"],
        "name": profile["name"],
        "email": profile["email"],
        "auth": profile["auth"],
        "active_pids": profile["active_pids"],
        "weekly_quota_exhausted": weekly_exhausted,
        "usability": usability,
        "official_quota": official_quota,
        "stats_5h": stats_5h,
        "stats_7d": stats_7d,
        "stats_all": stats_all,
        "daily_stats": daily_stats,
        "calendar_stats": calendar_stats,
        "today_stats": calendar_stats.get(datetime.fromtimestamp(current_ts).strftime("%Y-%m-%d"), {
            "date": datetime.fromtimestamp(current_ts).strftime("%Y-%m-%d"),
            "requests": 0,
            "prompt_tokens": 0,
            "candidate_tokens": 0,
            "cached_tokens": 0,
            "thinking_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }),
        "conversations": conversations_list,
    }


def get_all_usage(manager) -> Dict[str, Any]:
    """Collects usage across all configured profiles."""
    profiles = manager.list_profiles()
    current_ts = time.time()
    config = manager.get_config() if hasattr(manager, "get_config") else {"min_buffer_pct": 0.0}
    min_buffer_pct = float(config.get("min_buffer_pct", 0.0))

    accounts_usage = []
    totals = {
        "stats_5h": {"requests": 0, "prompt_tokens": 0, "candidate_tokens": 0, "cached_tokens": 0, "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "stats_7d": {"requests": 0, "prompt_tokens": 0, "candidate_tokens": 0, "cached_tokens": 0, "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "stats_all": {"requests": 0, "prompt_tokens": 0, "candidate_tokens": 0, "cached_tokens": 0, "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "usable_accounts": 0,
        "exhausted_accounts": 0,
    }

    # Aggregate calendar stats for totals (Starting from 2026-09-01)
    start_dt = datetime(2026, 9, 1).date()
    end_dt = datetime.fromtimestamp(current_ts).date()
    calendar_days_count = max(1, (end_dt - start_dt).days + 1)
    totals_calendar = {}
    for i in range(calendar_days_count):
        d_str = (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        totals_calendar[d_str] = {
            "date": d_str,
            "requests": 0,
            "prompt_tokens": 0,
            "candidate_tokens": 0,
            "cached_tokens": 0,
            "thinking_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    for p in profiles:
        u = get_profile_usage(p, current_ts, min_buffer_pct=min_buffer_pct)
        accounts_usage.append(u)

        if u["usability"]["is_usable"]:
            totals["usable_accounts"] += 1
        if u["usability"]["code"] == "WEEKLY_EXHAUSTED":
            totals["exhausted_accounts"] += 1

        for scope in ["stats_5h", "stats_7d", "stats_all"]:
            for k in totals[scope]:
                totals[scope][k] += u[scope].get(k, 0)

        for d_str, d_stat in u.get("calendar_stats", {}).items():
            if d_str in totals_calendar:
                for k in ["requests", "prompt_tokens", "candidate_tokens", "cached_tokens", "thinking_tokens", "output_tokens", "total_tokens"]:
                    totals_calendar[d_str][k] += d_stat.get(k, 0)

    today_str = datetime.fromtimestamp(current_ts).strftime("%Y-%m-%d")
    today_totals = totals_calendar.get(today_str, {
        "date": today_str,
        "requests": 0,
        "prompt_tokens": 0,
        "candidate_tokens": 0,
        "cached_tokens": 0,
        "thinking_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    })
    totals["today"] = today_totals

    return {
        "generated_at": datetime.fromtimestamp(current_ts).strftime("%Y-%m-%d %H:%M:%S"),
        "today_date": today_str,
        "timestamp": current_ts,
        "accounts": accounts_usage,
        "totals": totals,
        "totals_calendar": totals_calendar,
        "config": config,
    }


def render_html_dashboard(usage_data: Dict[str, Any]) -> str:
    """Renders a modern, responsive single-page HTML usage dashboard with calendar heatmap and dual quota gauges."""
    json_data = json.dumps(usage_data, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Antigravity Multi-Account Usage Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #090d16;
      --card-bg: rgba(18, 26, 43, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --card-hover: rgba(255, 255, 255, 0.14);
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --accent-blue: #38bdf8;
      --accent-cyan: #06b6d4;
      --accent-purple: #a855f7;
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-rose: #f43f5e;
      --glow-blue: rgba(56, 189, 248, 0.15);
    }}

    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    body {{
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      background-color: var(--bg);
      background-image: 
        radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.08) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(168, 85, 247, 0.08) 0px, transparent 50%),
        radial-gradient(at 50% 50%, rgba(15, 23, 42, 0.5) 0px, transparent 100%);
      color: var(--text-main);
      min-height: 100vh;
      padding: 2rem 1.5rem;
      line-height: 1.5;
      overflow-x: hidden;
    }}

    .container {{
      max-width: 1440px;
      margin: 0 auto;
    }}

    /* Header */
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 2rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--card-border);
      flex-wrap: wrap;
      gap: 1rem;
    }}

    .logo-area {{
      display: flex;
      align-items: center;
      gap: 0.85rem;
      min-width: 0;
    }}

    .logo-badge {{
      width: 44px;
      height: 44px;
      min-width: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, #0284c7, #8b5cf6);
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 0 20px rgba(56, 189, 248, 0.35);
    }}

    .logo-badge svg {{
      width: 24px;
      height: 24px;
      fill: #fff;
    }}

    .brand-info {{
      display: flex;
      flex-direction: column;
      gap: 0.2rem;
      min-width: 0;
    }}

    .brand-row {{
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
      flex-wrap: wrap;
    }}

    .brand-name {{
      font-size: 1.4rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      font-family: 'JetBrains Mono', monospace;
      background: linear-gradient(135deg, #38bdf8 0%, #818cf8 50%, #c084fc 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      line-height: 1.2;
    }}

    .brand-sep {{
      color: rgba(148, 163, 184, 0.4);
      font-weight: 300;
      font-size: 1.15rem;
      user-select: none;
    }}

    .brand-title {{
      font-size: 1.15rem;
      font-weight: 600;
      color: #f1f5f9;
      margin: 0;
      letter-spacing: -0.01em;
      line-height: 1.2;
      white-space: nowrap;
    }}

    .subtitle {{
      font-size: 0.8125rem;
      color: var(--text-muted);
      margin: 0;
      line-height: 1.3;
    }}

    .header-actions {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}

    .btn {{
      padding: 0.5rem 1rem;
      border-radius: 8px;
      font-size: 0.8125rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      transition: all 0.2s;
      border: 1px solid var(--card-border);
      background: var(--card-bg);
      color: var(--text-main);
      backdrop-filter: blur(12px);
    }}

    .btn:hover {{
      border-color: var(--card-hover);
      background: rgba(255, 255, 255, 0.06);
      transform: translateY(-1px);
    }}

    .btn-primary {{
      background: linear-gradient(135deg, #0284c7, #2563eb);
      border: none;
      color: #fff;
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
    }}

    .btn-primary:hover {{
      background: linear-gradient(135deg, #0369a1, #1d4ed8);
    }}

    .badge-live {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 0.35rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.1);
      color: var(--accent-green);
      border: 1px solid rgba(16, 185, 129, 0.2);
    }}

    .badge-live .dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent-green);
      animation: pulse 2s infinite;
    }}

    @keyframes pulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
      70% {{ box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
    }}

    /* Global Ribbon */
    .ribbon {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }}

    .stat-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 1.25rem;
      backdrop-filter: blur(12px);
      position: relative;
      overflow: hidden;
    }}

    .stat-card::after {{
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 2px;
      background: linear-gradient(90deg, transparent, var(--accent-blue), transparent);
      opacity: 0.4;
    }}

    .stat-label {{
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-bottom: 0.35rem;
    }}

    .stat-value {{
      font-size: 1.75rem;
      font-weight: 800;
      letter-spacing: -0.03em;
      color: #fff;
      display: flex;
      align-items: baseline;
      gap: 0.35rem;
    }}

    .stat-unit {{
      font-size: 0.8125rem;
      font-weight: 500;
      color: var(--text-muted);
    }}

    .stat-sub {{
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-top: 0.5rem;
      display: flex;
      justify-content: space-between;
      gap: 0.4rem;
      flex-wrap: wrap;
    }}

    .stat-sub-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 0.35rem 0.5rem;
      font-size: 0.715rem;
    }}

    .stat-sub-grid span {{
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    /* Accounts Grid */
    .accounts-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 1.25rem;
      margin-bottom: 2.5rem;
    }}

    .account-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.5rem;
      backdrop-filter: blur(12px);
      transition: all 0.25s ease;
      display: flex;
      flex-direction: column;
      position: relative;
    }}

    .account-card.card-exhausted {{
      border-color: rgba(244, 63, 94, 0.3);
      background: linear-gradient(180deg, rgba(244, 63, 94, 0.05) 0%, rgba(18, 26, 43, 0.85) 100%);
    }}

    .account-card:hover {{
      border-color: var(--card-hover);
      box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
      transform: translateY(-2px);
    }}

    .acc-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.65rem;
      gap: 0.5rem;
      min-width: 0;
    }}

    .acc-title-area {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      min-width: 0;
      flex: 1;
      overflow: hidden;
    }}

    .acc-id-badge {{
      width: 34px;
      height: 34px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid var(--card-border);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 0.875rem;
      color: var(--accent-blue);
      font-family: 'JetBrains Mono', monospace;
      flex-shrink: 0;
    }}

    .acc-name {{
      font-size: 1.125rem;
      font-weight: 700;
      color: #fff;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .acc-email {{
      font-size: 0.75rem;
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 100%;
    }}

    .acc-status-row {{
      display: flex;
      align-items: center;
      margin-bottom: 1rem;
      min-width: 0;
    }}

    .btn-relay-card {{
      background: rgba(56, 189, 248, 0.12);
      border: 1px solid rgba(56, 189, 248, 0.3);
      color: var(--accent-blue);
      border-radius: 6px;
      padding: 0.22rem 0.55rem;
      font-size: 0.6875rem;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      transition: all 0.2s ease;
      white-space: nowrap;
      flex-shrink: 0;
    }}
    .btn-relay-card:hover {{
      background: rgba(56, 189, 248, 0.25);
      border-color: rgba(56, 189, 248, 0.5);
      color: #fff;
      transform: translateY(-1px);
    }}
    .btn-relay-card.disabled {{
      background: rgba(255, 255, 255, 0.04) !important;
      border: 1px solid rgba(255, 255, 255, 0.1) !important;
      color: var(--text-muted) !important;
      opacity: 0.4 !important;
      cursor: not-allowed !important;
      pointer-events: auto !important;
      box-shadow: none !important;
      transform: none !important;
    }}
    .btn-relay-card.disabled:hover {{
      background: rgba(255, 255, 255, 0.04) !important;
      border-color: rgba(255, 255, 255, 0.1) !important;
      color: var(--text-muted) !important;
      transform: none !important;
    }}

    /* Usability Badges */
    .status-tag {{
      font-size: 0.6875rem;
      font-weight: 700;
      padding: 0.28rem 0.55rem;
      border-radius: 9999px;
      letter-spacing: 0.03em;
      white-space: nowrap;
      flex-shrink: 0;
    }}

    .status-tag.status-ready {{
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }}

    .status-tag.status-cooldown {{
      background: rgba(6, 182, 212, 0.15);
      color: #22d3ee;
      border: 1px solid rgba(6, 182, 212, 0.3);
    }}

    .status-tag.status-exhausted {{
      background: rgba(244, 63, 94, 0.18);
      color: #fb7185;
      border: 1px solid rgba(244, 63, 94, 0.35);
    }}

    .status-tag.status-pending {{
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.3);
    }}

    .status-tag.status-not-auth {{
      background: rgba(148, 163, 184, 0.15);
      color: #cbd5e1;
      border: 1px solid rgba(148, 163, 184, 0.3);
    }}

    /* Warning Banner for Exhausted Quota */
    .quota-exhausted-banner {{
      background: rgba(244, 63, 94, 0.12);
      border: 1px solid rgba(244, 63, 94, 0.25);
      border-radius: 8px;
      padding: 0.5rem 0.75rem;
      margin-bottom: 0.75rem;
      font-size: 0.75rem;
      color: #fecdd3;
      display: flex;
      align-items: flex-start;
      gap: 0.5rem;
    }}

    /* Metrics Section */
    .metric-block {{
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.04);
      border-radius: 12px;
      padding: 1rem;
      margin-bottom: 0.875rem;
    }}

    .metric-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.5rem;
    }}

    .metric-title {{
      font-size: 0.8125rem;
      font-weight: 600;
      color: #cbd5e1;
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }}

    .metric-badge {{
      font-size: 0.6875rem;
      padding: 0.15rem 0.4rem;
      border-radius: 4px;
      font-weight: 700;
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent-blue);
    }}

    .metric-val {{
      font-size: 1.125rem;
      font-weight: 700;
      font-family: 'JetBrains Mono', monospace;
      color: #fff;
    }}

    .progress-bar-wrapper {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-top: 0.5rem;
      margin-bottom: 0.5rem;
    }}

    .progress-bar-container {{
      flex: 1;
      height: 6px;
      background: rgba(255, 255, 255, 0.06);
      border-radius: 9999px;
      overflow: hidden;
      margin: 0;
    }}

    .progress-bar {{
      height: 100%;
      border-radius: 9999px;
      background: linear-gradient(90deg, #38bdf8, #818cf8);
      transition: width 0.6s ease;
    }}

    .progress-bar.bar-exhausted {{
      background: linear-gradient(90deg, #f43f5e, #fb7185);
    }}

    .progress-bar.bar-warning {{
      background: linear-gradient(90deg, #f59e0b, #fbbf24);
    }}

    .progress-pct {{
      font-size: 0.75rem;
      font-weight: 700;
      font-family: 'JetBrains Mono', monospace;
      color: var(--accent-blue);
      min-width: 36px;
      text-align: right;
    }}

    .progress-pct.pct-exhausted {{
      color: var(--accent-rose);
    }}

    .progress-pct.pct-warning {{
      color: var(--accent-amber);
    }}

    .breakdown-chips {{
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
      margin-top: 0.5rem;
    }}

    .chip {{
      font-size: 0.6875rem;
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.04);
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
      display: flex;
      align-items: center;
      gap: 0.3rem;
    }}

    .chip strong {{
      color: #e2e8f0;
    }}

    .reset-countdown {{
      margin-top: 0.65rem;
      padding: 0.45rem 0.65rem;
      border-radius: 8px;
      background: rgba(56, 189, 248, 0.08);
      border: 1px solid rgba(56, 189, 248, 0.2);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.75rem;
    }}

    .reset-countdown.ready {{
      background: rgba(16, 185, 129, 0.08);
      border-color: rgba(16, 185, 129, 0.2);
    }}

    .countdown-time {{
      font-weight: 700;
      color: var(--accent-cyan);
      font-family: 'JetBrains Mono', monospace;
    }}

    /* Sparkline Bar Chart */
    .chart-container {{
      margin-top: 0.75rem;
      display: flex;
      align-items: flex-end;
      gap: 4px;
      height: 48px;
      padding-top: 4px;
    }}

    .bar-col {{
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      height: 100%;
      justify-content: flex-end;
      position: relative;
    }}

    .bar {{
      width: 100%;
      background: rgba(56, 189, 248, 0.35);
      border-radius: 3px 3px 0 0;
      transition: all 0.3s ease;
      min-height: 2px;
    }}

    .bar-col:hover .bar {{
      background: var(--accent-blue);
      box-shadow: 0 0 8px rgba(56, 189, 248, 0.6);
    }}

    .bar-date {{
      font-size: 0.5625rem;
      color: #64748b;
      margin-top: 4px;
      font-family: 'JetBrains Mono', monospace;
    }}

    .acc-footer {{
      margin-top: auto;
      padding-top: 0.75rem;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: nowrap;
    }}

    .pid-tag {{
      font-size: 0.6875rem;
      font-family: 'JetBrains Mono', monospace;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .pid-tag.active {{
      color: var(--accent-green);
    }}

    .alias-pill {{
      font-size: 0.6875rem;
      font-family: 'JetBrains Mono', monospace;
      color: var(--accent-blue);
      background: rgba(56, 189, 248, 0.08);
      border: 1px solid rgba(56, 189, 248, 0.2);
      border-radius: 4px;
      padding: 2px 6px;
      white-space: nowrap;
      flex-shrink: 0;
    }}

    /* Activity & Trend Combined Section */
    .activity-section {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.5rem;
      backdrop-filter: blur(12px);
      margin-bottom: 2.5rem;
    }}

    .activity-top-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
      flex-wrap: wrap;
      gap: 1rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      padding-bottom: 1rem;
    }}

    .activity-split-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1.25rem;
      align-items: stretch;
      min-width: 0;
    }}

    @media (max-width: 1024px) {{
      .activity-split-grid {{
        grid-template-columns: 1fr;
      }}
    }}

    .activity-pane {{
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 12px;
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 295px;
      min-width: 0;
      overflow: hidden;
    }}

    .pane-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
      flex-wrap: wrap;
      gap: 0.5rem;
    }}

    .pane-title {{
      font-size: 0.875rem;
      font-weight: 700;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}

    /* Range Selector Buttons */
    .range-selector {{
      display: flex;
      background: rgba(0, 0, 0, 0.35);
      border-radius: 8px;
      padding: 2px;
      gap: 2px;
      border: 1px solid var(--card-border);
    }}

    .range-btn {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 0.6875rem;
      font-weight: 600;
      padding: 0.25rem 0.6rem;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.2s ease;
    }}

    .range-btn:hover {{
      color: #fff;
    }}

    .range-btn.active {{
      background: var(--accent-blue);
      color: #090d16;
      font-weight: 700;
    }}

    /* Heatmap Layout */
    .heatmap-scroll-wrapper {{
      height: 160px;
      min-height: 160px;
      max-height: 160px;
      display: flex;
      align-items: center;
      overflow-x: auto;
      padding: 0.25rem 0;
    }}

    .heatmap-grid-layout {{
      display: inline-flex;
      flex-direction: column;
      gap: 6px;
    }}

    .heatmap-body {{
      display: flex;
      gap: 6px;
    }}

    .heatmap-days-labels {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 0.625rem;
      color: #64748b;
      padding-top: 1px;
      width: 20px;
    }}

    .heatmap-days-labels span {{
      height: 14px;
      line-height: 14px;
      text-align: right;
    }}

    .heatmap-weeks-container {{
      display: flex;
      gap: 4px;
    }}

    .heatmap-week-col {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}

    .heatmap-cell {{
      width: 14px;
      height: 14px;
      border-radius: 3px;
      background: #1e293b;
      cursor: pointer;
      transition: transform 0.15s ease, box-shadow 0.15s ease;
      position: relative;
    }}

    .heatmap-cell:hover {{
      transform: scale(1.35);
      z-index: 10;
      box-shadow: 0 0 10px rgba(56, 189, 248, 0.6);
    }}

    .heatmap-cell.level-0 {{ background: #182234; }}
    .heatmap-cell.level-1 {{ background: #064e3b; }}
    .heatmap-cell.level-2 {{ background: #047857; }}
    .heatmap-cell.level-3 {{ background: #10b981; }}
    .heatmap-cell.level-4 {{ background: #34d399; box-shadow: 0 0 6px rgba(52, 211, 153, 0.5); }}

    .heatmap-cell.today-cell {{
      outline: 2px solid var(--accent-cyan);
      outline-offset: 1px;
      box-shadow: 0 0 8px rgba(6, 182, 212, 0.85);
      z-index: 5;
    }}

    /* Trend Curve Chart */
    .trend-chart-wrapper {{
      position: relative;
      height: 160px;
      min-height: 160px;
      max-height: 160px;
      width: 100%;
      display: flex;
      align-items: center;
    }}

    .trend-chart-svg {{
      width: 100%;
      height: 100%;
      display: block;
      overflow: visible;
    }}

    .trend-point {{
      cursor: pointer;
      transition: r 0.2s ease, fill 0.2s ease;
    }}

    .trend-point:hover {{
      r: 6;
      fill: #fff;
      stroke: var(--accent-blue);
      stroke-width: 3;
    }}

    /* Unified Bottom Metrics Row & Chips for Both Panes */
    .pane-metrics-row {{
      display: flex;
      gap: 0.4rem;
      flex-wrap: wrap;
      margin-top: auto;
      padding-top: 0.85rem;
      border-top: 1px solid rgba(255, 255, 255, 0.06);
      align-items: center;
      overflow-x: auto;
    }}

    .metric-chip {{
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 7px;
      padding: 0.32rem 0.6rem;
      font-size: 0.6875rem;
      color: var(--text-muted);
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      transition: all 0.2s ease;
      white-space: nowrap;
    }}

    .metric-chip:hover {{
      background: rgba(255, 255, 255, 0.06);
      border-color: rgba(255, 255, 255, 0.16);
    }}

    .metric-chip strong {{
      color: #fff;
      font-family: 'JetBrains Mono', monospace;
      font-weight: 600;
    }}

    .metric-chip.highlight-cyan {{
      background: rgba(6, 182, 212, 0.12);
      border-color: rgba(6, 182, 212, 0.35);
      color: #e0f2fe;
    }}

    .metric-chip.highlight-cyan strong {{
      color: var(--accent-cyan);
      font-weight: 700;
    }}

    .metric-chip .chip-sub {{
      font-size: 0.625rem;
      color: var(--text-muted);
    }}

    .metric-chip.highlight-cyan .chip-sub {{
      color: var(--accent-cyan);
      opacity: 0.85;
    }}

    /* Floating Tooltip */
    #heatmap-tooltip {{
      position: fixed;
      display: none;
      background: rgba(15, 23, 42, 0.95);
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 10px;
      padding: 0.75rem 1rem;
      color: #fff;
      font-size: 0.75rem;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
      pointer-events: none;
      z-index: 1000;
      backdrop-filter: blur(12px);
      min-width: 190px;
    }}

    /* Detailed Table Section */
    .section-title {{
      font-size: 1.25rem;
      font-weight: 700;
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}

    .table-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      overflow-x: auto;
      backdrop-filter: blur(12px);
      margin-bottom: 3rem;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.8125rem;
    }}

    th {{
      background: rgba(255, 255, 255, 0.03);
      padding: 0.875rem 1rem;
      font-weight: 600;
      color: var(--text-muted);
      border-bottom: 1px solid var(--card-border);
      text-transform: uppercase;
      font-size: 0.6875rem;
      letter-spacing: 0.05em;
    }}

    td {{
      padding: 0.875rem 1rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      color: #cbd5e1;
    }}

    tr:hover td {{
      background: rgba(255, 255, 255, 0.02);
    }}

    .mono {{
      font-family: 'JetBrains Mono', monospace;
    }}

    /* Filter tabs */
    .tabs {{
      display: flex;
      gap: 0.5rem;
      margin-bottom: 1rem;
      overflow-x: auto;
      padding-bottom: 0.5rem;
    }}

    .tab-btn {{
      padding: 0.4rem 0.85rem;
      border-radius: 8px;
      font-size: 0.75rem;
      font-weight: 600;
      border: 1px solid var(--card-border);
      background: var(--card-bg);
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.2s;
    }}

    .tab-btn.active {{
      background: rgba(56, 189, 248, 0.15);
      border-color: rgba(56, 189, 248, 0.3);
      color: var(--accent-blue);
    }}

    /* Relay Button & Modal */
    .btn-relay {{
      background: linear-gradient(135deg, #0ea5e9, #0284c7);
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 0.3rem 0.65rem;
      font-size: 0.75rem;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      transition: all 0.2s ease;
      box-shadow: 0 2px 6px rgba(14, 165, 233, 0.3);
      white-space: nowrap;
    }}
    .btn-relay:hover {{
      background: linear-gradient(135deg, #38bdf8, #0ea5e9);
      box-shadow: 0 4px 12px rgba(56, 189, 248, 0.4);
      transform: translateY(-1px);
    }}
    .btn-relay-quick {{
      background: rgba(234, 179, 8, 0.2);
      border: 1px solid rgba(234, 179, 8, 0.4);
      color: #fef08a;
      border-radius: 4px;
      padding: 0.2rem 0.5rem;
      font-size: 0.6875rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      white-space: nowrap;
    }}
    .btn-relay-quick:hover {{
      background: rgba(234, 179, 8, 0.35);
      color: #fff;
    }}
    .modal-overlay {{
      position: fixed;
      top: 0;
      left: 0;
      width: 100vw;
      height: 100vh;
      background: rgba(9, 13, 22, 0.85);
      backdrop-filter: blur(8px);
      z-index: 1000;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }}
    .modal-card {{
      background: #0f172a;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 16px;
      width: 100%;
      max-width: 580px;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.75);
      padding: 1.75rem;
      position: relative;
    }}
    .modal-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
      padding-bottom: 0.75rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }}
    .modal-title {{
      font-size: 1.1rem;
      font-weight: 700;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}
    .modal-close-btn {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 1.25rem;
      cursor: pointer;
      line-height: 1;
    }}
    .modal-close-btn:hover {{
      color: #fff;
    }}
    .relay-item {{
      padding: 0.75rem 1rem;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 8px;
      margin-bottom: 0.6rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      cursor: pointer;
      transition: all 0.2s ease;
    }}
    .relay-item:hover, .relay-item.selected {{
      background: rgba(56, 189, 248, 0.08);
      border-color: rgba(56, 189, 248, 0.4);
    }}
    .relay-item.disabled {{
      opacity: 0.45 !important;
      cursor: not-allowed !important;
      border: 1px dashed rgba(239, 68, 68, 0.3) !important;
      background: rgba(239, 68, 68, 0.03) !important;
      box-shadow: none !important;
      transform: none !important;
    }}
    .relay-item.disabled:hover {{
      border-color: rgba(239, 68, 68, 0.3) !important;
      background: rgba(239, 68, 68, 0.03) !important;
      transform: none !important;
    }}
    .relay-cmd-box {{
      background: #020617;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 8px;
      padding: 0.75rem 1rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.8125rem;
      color: #38bdf8;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 1rem;
    }}
    .btn-copy-cmd {{
      background: rgba(255, 255, 255, 0.1);
      border: none;
      color: #fff;
      border-radius: 4px;
      padding: 0.25rem 0.5rem;
      font-size: 0.75rem;
      cursor: pointer;
    }}
    .btn-copy-cmd:hover {{
      background: var(--accent-blue);
      color: #090d16;
    }}

    /* Switch Toggle */
    .switch {{
      position: relative;
      display: inline-block;
      width: 44px;
      height: 24px;
    }}
    .switch input {{
      opacity: 0;
      width: 0;
      height: 0;
    }}
    .toggle-slider {{
      position: absolute;
      cursor: pointer;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background-color: #334155;
      transition: .3s;
      border-radius: 24px;
    }}
    .toggle-slider:before {{
      position: absolute;
      content: "";
      height: 18px;
      width: 18px;
      left: 3px;
      bottom: 3px;
      background-color: white;
      transition: .3s;
      border-radius: 50%;
    }}
    input:checked + .toggle-slider {{
      background-color: var(--accent-green);
    }}
    input:checked + .toggle-slider:before {{
      transform: translateX(20px);
    }}

    /* Modal Large */
    .modal-card.modal-lg {{
      max-width: 840px;
      max-height: 88vh;
      overflow-y: auto;
    }}

    /* Policy Guide Flow */
    .policy-flow {{
      display: flex;
      flex-direction: column;
      gap: 0.85rem;
      margin-top: 1rem;
    }}
    .policy-card {{
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 1.1rem 1.25rem;
      position: relative;
      transition: all 0.2s ease;
    }}
    .policy-card:hover {{
      border-color: rgba(56, 189, 248, 0.35);
      background: rgba(255, 255, 255, 0.05);
    }}
    .policy-card-header {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 0.5rem;
    }}
    .policy-badge {{
      font-size: 0.6875rem;
      font-weight: 800;
      padding: 0.2rem 0.55rem;
      border-radius: 6px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    .badge-p1 {{ background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }}
    .badge-p2 {{ background: rgba(168, 85, 247, 0.2); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.3); }}
    .badge-p3 {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }}
    .badge-p4 {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }}
    .badge-p5 {{ background: rgba(244, 63, 94, 0.2); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.3); }}
    .policy-title {{
      font-size: 0.9375rem;
      font-weight: 700;
      color: #fff;
    }}
    .policy-desc {{
      font-size: 0.8125rem;
      color: #cbd5e1;
      line-height: 1.6;
    }}
    .policy-desc ul {{
      margin-left: 1.25rem;
      margin-top: 0.4rem;
    }}
    .policy-desc li {{
      margin-bottom: 0.3rem;
    }}
    .policy-arrow {{
      text-align: center;
      color: var(--accent-blue);
      font-size: 1.25rem;
      line-height: 1;
      opacity: 0.7;
    }}
  </style>
</head>
<body>
  <div class="container">
    <!-- Tooltip element -->
    <div id="heatmap-tooltip"></div>

    <!-- Header -->
    <header>
      <div class="logo-area">
        <div class="logo-badge">
          <svg viewBox="0 0 24 24">
            <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
          </svg>
        </div>
        <div class="brand-info">
          <div class="brand-row">
            <span class="brand-name mono">agy-multi</span>
            <span class="brand-sep">/</span>
            <h1 class="brand-title" id="dash-title">多账号监控看板</h1>
          </div>
          <p class="subtitle" id="dash-subtitle">Google AI Pro 多环境并发隔离 • 配额智能接管与 Token 分析</p>
        </div>
      </div>
      <div class="header-actions">
        <div class="badge-live" id="live-badge" title="Click to pause/resume auto-refresh" onclick="toggleAutoRefresh()" style="cursor:pointer;user-select:none">
          <span class="dot"></span>
          <span id="txt-live">实时监控中</span>
          <span id="txt-refresh-timer" class="mono" style="font-size:0.6875rem;opacity:0.8;margin-left:2px">(15s)</span>
        </div>
        <button class="btn" id="lang-btn" onclick="toggleLang()" style="display:inline-flex;align-items:center;gap:6px">
          <span>🌐</span>
          <span id="lang-btn-text">English</span>
        </button>
        <button class="btn" onclick="openPolicyModal()" style="display:inline-flex;align-items:center;gap:6px">
          <span>📖</span>
          <span id="txt-policy-btn">接管策略</span>
        </button>
        <button class="btn" onclick="openConfigModal()" style="display:inline-flex;align-items:center;gap:6px">
          <span>⚙️</span>
          <span id="txt-config-btn">接管设置</span>
        </button>
        <button class="btn" onclick="location.reload()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/>
          </svg>
          <span id="txt-refresh">刷新</span>
        </button>
        <button class="btn" onclick="exportData()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
          </svg>
          <span id="txt-export">导出 JSON</span>
        </button>
      </div>
    </header>

    <!-- Global Ribbon -->
    <div class="ribbon">
      <div class="stat-card">
        <div class="stat-label" id="lbl-stat-accounts">总监控账号状态</div>
        <div class="stat-value" id="total-accounts">4 <span class="stat-unit" id="unit-profiles">个 Profile</span></div>
        <div class="stat-sub stat-sub-grid">
          <span><span id="lbl-ready">满额就绪:</span> <strong style="color:var(--accent-green)" id="usable-count">0</strong></span>
          <span><span id="lbl-exhausted">周额耗尽:</span> <strong style="color:var(--accent-rose)" id="exhausted-count">0</strong></span>
          <span><span id="lbl-buffer">保底余量:</span> <strong style="color:var(--accent-cyan);cursor:pointer;" id="buffer-pct" onclick="openConfigModal()" title="点击修改保底余量">0%</strong></span>
          <span><span id="lbl-auto-relay-stat">自动接管:</span> <strong style="color:var(--accent-green);cursor:pointer;" id="auto-relay-stat" onclick="openConfigModal()" title="点击切换接管设置">开启</strong></span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-label" id="lbl-stat-5h">近 5 小时全账号消耗</div>
        <div class="stat-value" id="total-5h-tokens">-- <span class="stat-unit">Tokens</span></div>
        <div class="stat-sub">
          <span><span id="lbl-5h-reqs">总请求:</span> <strong id="total-5h-reqs">--</strong> <span class="unit-times">次</span></span>
          <span><span id="lbl-5h-thinking">思维:</span> <strong id="total-5h-thinking">--</strong></span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-label" id="lbl-stat-7d">近 7 天全账号消耗</div>
        <div class="stat-value" id="total-7d-tokens">-- <span class="stat-unit">Tokens</span></div>
        <div class="stat-sub">
          <span><span id="lbl-7d-reqs">总请求:</span> <strong id="total-7d-reqs">--</strong> <span class="unit-times">次</span></span>
          <span><span id="lbl-7d-window-desc">统计窗口:</span> <strong style="color:var(--accent-cyan)" id="lbl-7d-window">7天持续滑动</strong></span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-label" id="lbl-stat-all">全周期总请求与 Token</div>
        <div class="stat-value" id="total-all-tokens">-- <span class="stat-unit">Tokens</span></div>
        <div class="stat-sub">
          <span><span id="lbl-all-reqs">总模型调用:</span> <strong id="total-all-reqs">--</strong> <span class="unit-times">次</span></span>
          <span><span id="lbl-all-updated">更新时间:</span> <span id="gen-time">--</span></span>
        </div>
      </div>
    </div>

    <!-- Accounts Grid -->
    <div class="accounts-grid" id="accounts-container">
      <!-- Injected by JavaScript -->
    </div>

    <!-- Activity Heatmap & Trend Chart Section -->
    <div class="activity-section">
      <div class="activity-top-bar">
        <div class="section-title" style="margin-bottom:0">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
            <line x1="16" y1="2" x2="16" y2="6"></line>
            <line x1="8" y1="2" x2="8" y2="6"></line>
            <line x1="3" y1="10" x2="21" y2="10"></line>
          </svg>
          <span id="txt-act-title">每日 Token 活跃度与用量走势</span>
        </div>

        <div class="tabs heatmap-tabs" id="heatmap-tabs" style="margin-bottom:0">
          <button class="tab-btn active" id="btn-tab-all" onclick="switchAccountView('all', this)">全部账号合并</button>
          <!-- Account tab buttons injected by JS -->
        </div>
      </div>

      <!-- Split Grid: Left = Calendar Heatmap, Right = Trend Curve Chart -->
      <div class="activity-split-grid">
        <!-- Left: Heatmap -->
        <div class="activity-pane heatmap-pane">
          <div class="pane-header">
            <span class="pane-title" id="txt-cal-title">📅 活跃日历</span>
            <div class="range-selector" id="heatmap-range-selector">
              <button class="range-btn" id="btn-range-current" onclick="setHeatmapRange('current', this)">本周 (当前列)</button>
              <button class="range-btn" id="btn-range-4w" onclick="setHeatmapRange('4w', this)">近4周</button>
              <button class="range-btn active" id="btn-range-all" onclick="setHeatmapRange('all', this)">全部</button>
            </div>
          </div>
          <div class="heatmap-scroll-wrapper">
            <div class="heatmap-grid-layout" id="heatmap-grid-container">
              <!-- Injected by JavaScript -->
            </div>
          </div>
          <div class="pane-metrics-row" id="heatmap-summary-chips">
            <!-- Summary chips injected by JS -->
          </div>
        </div>

        <!-- Right: Trend Curve Chart -->
        <div class="activity-pane trend-pane">
          <div class="pane-header" style="flex-wrap:wrap;gap:0.4rem">
            <span class="pane-title" id="txt-trend-title">📈 Token 消耗走势曲线</span>
            <div style="display:flex;gap:0.35rem;align-items:center;flex-wrap:wrap">
              <div class="range-selector" id="dimension-selector">
                <button class="range-btn active" id="btn-dim-total" onclick="setTrendDimension('total', this)">总计</button>
                <button class="range-btn" id="btn-dim-rw" onclick="setTrendDimension('breakdown', this)">读/写/缓存</button>
                <button class="range-btn" id="btn-dim-thinking" onclick="setTrendDimension('thinking', this)">思考/输出</button>
              </div>
              <div class="range-selector" id="scale-selector">
                <button class="range-btn active" id="btn-scale-linear" onclick="setTrendScale('linear', this)">线性</button>
                <button class="range-btn" id="btn-scale-log" onclick="setTrendScale('log', this)">对数</button>
              </div>
              <div class="range-selector" id="trend-range-selector">
                <button class="range-btn" id="btn-trend-7d" onclick="setTrendRange('7d', this)">近7天</button>
                <button class="range-btn" id="btn-trend-14d" onclick="setTrendRange('14d', this)">近14天</button>
                <button class="range-btn active" id="btn-trend-sep" onclick="setTrendRange('sep', this)">9月至今</button>
                <button class="range-btn" id="btn-trend-all" onclick="setTrendRange('all', this)">全部</button>
              </div>
            </div>
          </div>

          <div class="trend-chart-wrapper" id="trend-chart-wrapper">
            <!-- Injected by JavaScript -->
          </div>

          <div class="pane-metrics-row" id="trend-stats-footer">
            <!-- Injected by JavaScript -->
          </div>
        </div>
      </div>
    </div>

    <!-- Detailed Conversations Table -->
    <div class="section-title" style="margin-top: 1rem;">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
        <line x1="16" y1="13" x2="8" y2="13"/>
        <line x1="16" y1="17" x2="8" y2="17"/>
      </svg>
      <span id="txt-detail-title">近期会话与 Token 详细明细</span>
    </div>

    <div class="tabs" id="account-tabs">
      <button class="tab-btn active" id="btn-tab-table-all" onclick="filterTable('all')">全部账号</button>
      <!-- Injected by JavaScript -->
    </div>

    <div class="table-card">
      <table>
        <thead>
          <tr>
            <th id="th-acc">账号</th>
            <th id="th-sess">会话 ID / 标题</th>
            <th id="th-active">最后活跃时间</th>
            <th id="th-reqs">请求次数</th>
            <th id="th-prompt">输入 (Prompt)</th>
            <th id="th-cached">缓存 (Cached)</th>
            <th id="th-think">思维 (Thinking)</th>
            <th id="th-out">生成 (Output)</th>
            <th id="th-tot">总 Token 消耗</th>
          </tr>
        </thead>
        <tbody id="detailed-table-body">
          <!-- Injected by JavaScript -->
        </tbody>
      </table>
    </div>
  </div>

  <script>
    const data = {json_data};

    const I18N = {{
      zh: {{
        title: "多账号监控看板",
        subtitle: "Google AI Pro 多环境并发隔离 • 配额智能接管与 Token 分析",
        live: "实时监控中",
        langBtn: "English",
        refresh: "刷新",
        export: "导出 JSON",
        totalAccounts: "总监控账号状态",
        unitProfiles: "个 Profile",
        readyCount: "满额就绪:",
        exhaustedCount: "周额耗尽:",
        lblBuffer: "保底余量:",
        activePids: "活跃进程:",
        stat5h: "近 5 小时全账号消耗",
        stat7d: "近 7 天全账号消耗",
        statAll: "全周期总请求与 Token",
        requests: "总请求:",
        unitTimes: " 次",
        thinking: "思维:",
        rolling7dDesc: "统计窗口:",
        rolling7d: "7天持续滑动",
        allCalls: "总模型调用:",
        updated: "更新时间:",
        allCombined: "全部账号合并",
        accTabPrefix: "账号",
        actTitle: "每日 Token 活跃度与用量走势",
        calTitle: "📅 活跃日历",
        rangeCurrent: "本周 (当前列)",
        range4w: "近4周",
        rangeAll: "全部",
        range7d: "近7天",
        range14d: "近14天",
        rangeSep: "9月至今",
        scaleLinear: "线性",
        scaleLog: "对数",
        trendTitle: "📈 Token 消耗走势曲线",
        dimTotal: "总计",
        dimRW: "读/写/缓存",
        dimThinking: "思考/输出",
        dayMon: "一", dayWed: "三", dayFri: "五", daySun: "日",
        todayUsage: "🌟 今日消耗:",
        activeDays: "活跃天数:",
        unitDays: "天",
        rangeTotal: "区间总量:",
        totalCalls: "总调用:",
        peakDay: "最高单日:",
        dailyAvg: "日均消耗:",
        detailTitle: "近期会话与 Token 详细明细",
        allAccTab: "全部账号",
        thAccount: "账号",
        thSession: "会话 ID / 标题",
        thLastActive: "最后活跃时间",
        thRequests: "请求次数",
        thPrompt: "输入 (Prompt)",
        thCached: "缓存 (Cached)",
        thThinking: "思维 (Thinking)",
        thOutput: "生成 (Output)",
        thTotal: "总 Token 消耗",
        thActions: "操作",
        btnRelay: "🚀 接力",
        btnRelayAccount: "⚡ 账号接力",
        accountRelayTip: "将此账号当前任务无缝接续至其他账号",
        relayModalTitle: "⚡ 账号算力接力",
        relayFromLabel: "源账号:",
        relayConvoLabel: "接力会话:",
        relayTargetLabel: "选择接力目标账号:",
        relayBtnSubmit: "⚡ 确认接力",
        relayClose: "关闭",
        relaySuccess: "接力成功！",
        relayResumePrompt: "请在终端运行以下命令继续会话:",
        relayAutoPick: "★ 智能推荐 (算力最充裕)",
        relayCooldownHint: "5H配额告急，建议接力到空闲账号",
        relayAutoRefreshTitle: "自动刷新",
        relayAutoRefreshPaused: "已暂停",
        gModelsTitle: "♊ GEMINI MODELS (Flash, Pro)",
        officialQuota: "官方配额余量",
        limit5hRem: "5 小时配额余量",
        limitWkRem: "周度配额余量",
        labelWeekly: "周",
        countdown5h: "⏳ 5H 刷新倒计时:",
        countdownWk: "⏳ 周刷新倒计时:",
        ready5h: "✅ 5小时配额:",
        readyWk: "✅ 周度配额:",
        ready100: "100% 满额可用",
        pendingAuth: "待认证",
        disabledStatus: "已停用",
        disabledWeeklyLimit: "周配额耗尽，5H限额停用",
        statusDisabled: "🚫 周配额耗尽 (5H停用)",
        claudeTitle: "🧠 Claude & GPT",
        claudeWk: "周配额",
        claudeRem: "周度余量",
        claudeCd: "⏳ 刷新倒计时:",
        claudeStatus: "○ 状态:",
        claudeQuota: "✅ 配额:",
        claude100: "100% 充裕",
        needLogin: "需登录",
        localHistory: "📊 本地 Token 历史消耗",
        totalTokens: "总Tokens",
        chip5hTok: "5H消耗:",
        chip5hReq: "5H请求:",
        chip7dTok: "7天消耗:",
        chip7dReq: "7天请求:",
        statusReady: "● 可用",
        statusReady100: "● 满额可用",
        statusExhausted: "🚫 周额度已耗尽",
        statusCooldown: "⏳ 5H冷却中",
        statusNotAuth: "○ 未登录",
        statusRegion: "⚠️ 地区受限",
        statusClaudeEx: "⚠️ Claude已耗尽",
        pidRunning: "● 运行中",
        pidSingle: "● PID:",
        pidIdle: "○ 空闲",
        aliasTitle: "终端别名: ",
        noRecords: "暂无会话或尚未产生 Token 消耗记录",
        released: "已释放 (可恢复)",
        releasedExact: "已释放",
        noPending: "无待释放请求",
        exactReleaseTime: "预计精确释放时间: ",
        localTimeSuffix: " (本地时间)",
        todayDot: "今日: ",
        dayUnit: "天 ",
        todaySubLabel: "今日",
        policyBtn: "接管策略",
        configBtn: "接管设置",
        policyModalTitle: "📖 多账号接管策略与生命周期流程图",
        configModalTitle: "⚙️ 策略与接管设置",
        lblCfgBuffer: "5H 配额保底余量比例",
        descCfgBuffer: "当账号 5H 剩余配额低于此阈值时，自动触发接力迁移或保护性挂起，防止把当前账号额度完全打光。",
        lblCfgAutoRelay: "自动接管开关 (Auto Takeover)",
        descCfgAutoRelay: "When enabled: automatically migrates to an idle ready account. When disabled: gracefully suspends current session without switching accounts.",
        lblCfgFallback: "无替代账号可用时策略",
        optPause: "挂起倒计时等待（推荐，等账号配额刷新自动唤醒）",
        optBurn: "继续消耗保底余量（直至 0% 彻底耗尽）",
        btnSaveConfig: "💾 保存设置",
        saving: "保存中...",
        configSavedToast: "✓ 设置已保存并在所有终端实时生效！",
        statusOn: "开启",
        statusOff: "关闭",
        lblAutoRelayStat: "自动接管:",
        statusExpired: "⚠️ 凭证已过期",
        statusQuotaUnavail: "⚠️ 配额不可用",
        relayDisabledNotAuth: "未登录，无法发起接力",
        relayDisabledExpired: "凭证已过期，无法发起接力",
        relayDisabledRegion: "地区受限，无法发起接力",
        relayDisabledNoConvo: "无活跃会话记录，无法接力",
        relayNoTargetBtn: "无可接力目标",
        relayNoTargetTitle: "当前无可用的接力目标账号",
        relayNoTargetMsg: "所有其他候选账号均未登录、地区受限、凭证已过期或配额已耗尽。请先登录或刷新目标账号凭证。",
        relayReasonNotAuth: "未登录 (需 login)",
        relayReasonExpired: "凭证已过期 (需刷新)",
        relayReasonRegion: "地区受限",
        relayReasonCooldown: "5H配额耗尽/冷却中",
        relayReasonBuffer: "配额触碰保底",
        relayReasonWeekly: "周配额已耗尽",
        relayReasonQuotaUnavail: "配额不可用",
        relayReasonIneligible: "账号不可用",
        noOtherAccounts: "未配置其他账号",
      }},
      en: {{
        title: "Multi-Account Dashboard",
        subtitle: "Google AI Pro Environment Isolation • Smart Relay & Token Analytics",
        live: "Live",
        langBtn: "中文",
        refresh: "Refresh",
        export: "Export JSON",
        totalAccounts: "Monitored Profiles",
        unitProfiles: "Profiles",
        readyCount: "Ready:",
        exhaustedCount: "Exhausted:",
        lblBuffer: "Reserve Buffer:",
        activePids: "Active PIDs:",
        stat5h: "5H Token Usage",
        stat7d: "7D Token Usage",
        statAll: "All-Time Calls & Tokens",
        requests: "Requests:",
        unitTimes: " calls",
        thinking: "Thinking:",
        rolling7dDesc: "Window:",
        rolling7d: "7D Rolling",
        allCalls: "Total Calls:",
        updated: "Updated:",
        allCombined: "All Accounts",
        accTabPrefix: "Account",
        actTitle: "Daily Token Activity & Usage Trend",
        calTitle: "📅 Activity Calendar",
        rangeCurrent: "Week",
        range4w: "4W",
        rangeAll: "All",
        range7d: "7D",
        range14d: "14D",
        rangeSep: "Sep",
        scaleLinear: "Linear",
        scaleLog: "Log",
        trendTitle: "📈 Token Trend",
        dimTotal: "Total",
        dimRW: "Read/Write/Cache",
        dimThinking: "Thinking/Output",
        dayMon: "M", dayWed: "W", dayFri: "F", daySun: "S",
        todayUsage: "🌟 Today:",
        activeDays: "Active:",
        unitDays: "Days",
        rangeTotal: "Total:",
        totalCalls: "Calls:",
        peakDay: "Peak:",
        dailyAvg: "Daily Avg:",
        detailTitle: "Recent Conversations & Detailed Token Usage",
        allAccTab: "All Accounts",
        thAccount: "Account",
        thSession: "Session ID / Title",
        thLastActive: "Last Active",
        thRequests: "Requests",
        thPrompt: "Prompt",
        thCached: "Cached",
        thThinking: "Thinking",
        thOutput: "Output",
        thTotal: "Total Tokens",
        thActions: "Actions",
        btnRelay: "🚀 Relay",
        btnRelayAccount: "⚡ Relay",
        accountRelayTip: "Relay this account's active task to another profile",
        relayModalTitle: "⚡ Account Quota Relay",
        relayFromLabel: "Source Profile:",
        relayConvoLabel: "Active Task:",
        relayTargetLabel: "Select Target Profile:",
        relayBtnSubmit: "⚡ Execute Relay",
        relayClose: "Close",
        relaySuccess: "Relay Successful!",
        relayResumePrompt: "Run the following command in terminal to continue:",
        relayAutoPick: "★ Auto-Recommended (Highest Quota)",
        relayCooldownHint: "5H Low • Ready to Relay",
        relayAutoRefreshTitle: "Auto-refresh",
        relayAutoRefreshPaused: "Paused",
        gModelsTitle: "♊ GEMINI MODELS (Flash, Pro)",
        officialQuota: "Official Quota",
        limit5hRem: "5-Hour Limit Remaining",
        limitWkRem: "Weekly Limit Remaining",
        labelWeekly: "Wk",
        countdown5h: "⏳ 5H Reset In:",
        countdownWk: "⏳ Weekly Reset In:",
        ready5h: "✅ 5-Hour Quota:",
        readyWk: "✅ Weekly Quota:",
        ready100: "100% Available",
        pendingAuth: "Pending Auth",
        disabledStatus: "Disabled",
        disabledWeeklyLimit: "Weekly limit reached, 5H disabled",
        statusDisabled: "🚫 Weekly Limit Reached (5H Disabled)",
        claudeTitle: "🧠 Claude & GPT",
        claudeWk: "Weekly Quota",
        claudeRem: "Weekly Remaining",
        claudeCd: "⏳ Reset In:",
        claudeStatus: "○ Status:",
        claudeQuota: "✅ Quota:",
        claude100: "100% Available",
        needLogin: "Login Required",
        localHistory: "📊 Local Token History",
        totalTokens: "Total Tokens",
        chip5hTok: "5H Tokens:",
        chip5hReq: "5H Reqs:",
        chip7dTok: "7D Tokens:",
        chip7dReq: "7D Reqs:",
        statusReady: "● Ready",
        statusReady100: "● Ready (100%)",
        statusExhausted: "🚫 Weekly Limit Reached",
        statusCooldown: "⏳ 5H Cooldown",
        statusNotAuth: "○ Not Logged In",
        statusRegion: "⚠️ Region Restricted",
        statusClaudeEx: "⚠️ Claude Exhausted",
        statusExpired: "⚠️ Token Expired",
        statusQuotaUnavail: "⚠️ Quota Unavailable",
        relayDisabledNotAuth: "Not logged in, cannot relay",
        relayDisabledExpired: "Token expired, cannot relay",
        relayDisabledRegion: "Region restricted, cannot relay",
        relayDisabledNoConvo: "No active conversations to relay",
        relayNoTargetBtn: "No Target Available",
        relayNoTargetTitle: "No Eligible Target Accounts",
        relayNoTargetMsg: "All other candidate accounts are not logged in, region restricted, token expired, or exhausted. Please log in or refresh credentials first.",
        relayReasonNotAuth: "Not logged in",
        relayReasonExpired: "Token expired",
        relayReasonRegion: "Region restricted",
        relayReasonCooldown: "5H cooldown/exhausted",
        relayReasonBuffer: "Below reserve buffer",
        relayReasonWeekly: "Weekly limit reached",
        relayReasonQuotaUnavail: "Quota unavailable",
        relayReasonIneligible: "Ineligible",
        noOtherAccounts: "No other profiles configured",
        pidRunning: "● Running",
        pidSingle: "● PID:",
        pidIdle: "○ Idle",
        aliasTitle: "Terminal Alias: ",
        noRecords: "No conversations or token records found",
        released: "Reset (Available)",
        releasedExact: "Reset",
        noPending: "No pending requests",
        exactReleaseTime: "Estimated Exact Reset Time: ",
        localTimeSuffix: " (Local Time)",
        todayDot: "Today: ",
        dayUnit: "d ",
        todaySubLabel: "Today",
        policyBtn: "Policy Guide",
        configBtn: "Settings",
        policyModalTitle: "📖 Multi-Account Relay & Lifecycle Policy",
        configModalTitle: "⚙️ Policy & Relay Settings",
        lblCfgBuffer: "5H Reserve Buffer Ratio",
        descCfgBuffer: "When 5H remaining quota drops below this threshold, automatically triggers relay or graceful suspension.",
        lblCfgAutoRelay: "Automatic Takeover (Auto Relay)",
        descCfgAutoRelay: "When enabled: automatically migrates to an idle ready account. When disabled: gracefully suspends current session without switching accounts.",
        lblCfgFallback: "When No Alternative Account Available",
        optPause: "Suspend with Countdown (Recommended: auto-wakes on quota reset)",
        optBurn: "Burn Buffer (Keep running until 0%)",
        btnSaveConfig: "💾 Save Settings",
        saving: "Saving...",
        configSavedToast: "✓ Settings saved and active across all sessions!",
        statusOn: "ON",
        statusOff: "OFF",
        lblAutoRelayStat: "Auto Relay:",
      }}
    }};

    let currentLang = localStorage.getItem('agy_dashboard_lang') || (navigator.language && !navigator.language.startsWith('zh') ? 'en' : 'zh');

    function t(key) {{
      if (I18N[currentLang] && Object.prototype.hasOwnProperty.call(I18N[currentLang], key)) {{
        return I18N[currentLang][key];
      }}
      if (I18N['zh'] && Object.prototype.hasOwnProperty.call(I18N['zh'], key)) {{
        return I18N['zh'][key];
      }}
      return key;
    }}

    function updateStaticTexts() {{
      const setEl = (id, val) => {{ const el = document.getElementById(id); if (el) el.innerText = val; }};

      setEl('dash-title', t('title'));
      setEl('dash-subtitle', t('subtitle'));
      setEl('txt-live', t('live'));
      setEl('lang-btn-text', t('langBtn'));
      setEl('txt-policy-btn', t('policyBtn'));
      setEl('txt-config-btn', t('configBtn'));
      setEl('txt-refresh', t('refresh'));
      setEl('txt-export', t('export'));

      setEl('lbl-stat-accounts', t('totalAccounts'));
      setEl('unit-profiles', t('unitProfiles'));
      setEl('lbl-ready', t('readyCount'));
      setEl('lbl-exhausted', t('exhaustedCount'));
      setEl('lbl-buffer', t('lblBuffer'));
      setEl('lbl-auto-relay-stat', t('lblAutoRelayStat'));

      setEl('lbl-stat-5h', t('stat5h'));
      setEl('lbl-5h-reqs', t('requests'));
      setEl('lbl-5h-thinking', t('thinking'));

      setEl('lbl-stat-7d', t('stat7d'));
      setEl('lbl-7d-reqs', t('requests'));
      setEl('lbl-7d-window-desc', t('rolling7dDesc'));
      setEl('lbl-7d-window', t('rolling7d'));

      setEl('lbl-stat-all', t('statAll'));
      setEl('lbl-all-reqs', t('allCalls'));
      setEl('lbl-all-updated', t('updated'));

      document.querySelectorAll('.unit-times').forEach(el => el.innerText = (currentLang === 'zh' ? '次' : ''));

      setEl('txt-act-title', t('actTitle'));
      setEl('btn-tab-all', t('allCombined'));
      setEl('txt-cal-title', t('calTitle'));
      setEl('btn-range-current', t('rangeCurrent'));
      setEl('btn-range-4w', t('range4w'));
      setEl('btn-range-all', t('rangeAll'));

      setEl('txt-trend-title', t('trendTitle'));
      setEl('btn-trend-7d', t('range7d'));
      setEl('btn-trend-14d', t('range14d'));
      setEl('btn-trend-sep', t('rangeSep'));
      setEl('btn-trend-all', t('rangeAll'));
      setEl('btn-scale-linear', t('scaleLinear'));
      setEl('btn-scale-log', t('scaleLog'));
      setEl('btn-dim-total', t('dimTotal'));
      setEl('btn-dim-rw', t('dimRW'));
      setEl('btn-dim-thinking', t('dimThinking'));

      setEl('txt-detail-title', t('detailTitle'));
      setEl('btn-tab-table-all', t('allAccTab'));

      setEl('th-acc', t('thAccount'));
      setEl('th-sess', t('thSession'));
      setEl('th-active', t('thLastActive'));
      setEl('th-reqs', t('thRequests'));
      setEl('th-prompt', t('thPrompt'));
      setEl('th-cached', t('thCached'));
      setEl('th-think', t('thThinking'));
      setEl('th-out', t('thOutput'));
      setEl('th-tot', t('thTotal'));

      setEl('txt-relay-modal-title', t('relayModalTitle'));
      setEl('lbl-relay-from', t('relayFromLabel'));
      setEl('lbl-relay-convo', t('relayConvoLabel'));
      setEl('lbl-relay-target', t('relayTargetLabel'));
      setEl('btn-relay-submit', t('relayBtnSubmit'));
      setEl('btn-relay-cancel', t('relayClose'));

      setEl('txt-policy-modal-title', t('policyModalTitle'));
      setEl('txt-config-modal-title', t('configModalTitle'));
      setEl('lbl-cfg-buffer', t('lblCfgBuffer'));
      setEl('desc-cfg-buffer', t('descCfgBuffer'));
      setEl('lbl-cfg-auto-relay', t('lblCfgAutoRelay'));
      setEl('desc-cfg-auto-relay', t('descCfgAutoRelay'));
      setEl('lbl-cfg-fallback', t('lblCfgFallback'));
      setEl('opt-pause', t('optPause'));
      setEl('opt-burn', t('optBurn'));
      setEl('btn-cfg-save', t('btnSaveConfig'));
    }}

    function toggleLang() {{
      currentLang = (currentLang === 'zh' ? 'en' : 'zh');
      localStorage.setItem('agy_dashboard_lang', currentLang);
      updateStaticTexts();
      initDashboard();
      updateCountdowns();
    }}

    function formatNumber(num) {{
      return (num || 0).toLocaleString();
    }}

    function exportData() {{
      const blob = new Blob([JSON.stringify(data, null, 2)], {{ type: 'application/json' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `antigravity_usage_${{new Date().toISOString().slice(0, 10)}}.json`;
      a.click();
    }}

    // ==========================================
    // Combined Activity & Trend Controllers
    // ==========================================
    let currentAccount = 'all';
    let currentTableAccount = 'all';
    let currentTrendRange = 'sep';
    let currentTrendScale = 'linear';
    let currentHeatmapRange = 'all';

    function initDashboard() {{
      updateStaticTexts();
      document.getElementById('gen-time').innerText = data.generated_at;

      // Update totals
      document.getElementById('total-accounts').innerHTML = `${{data.accounts.length}} <span class="stat-unit">${{t('unitProfiles')}}</span>`;
      document.getElementById('usable-count').innerText = data.totals.usable_accounts || 0;
      document.getElementById('exhausted-count').innerText = data.totals.exhausted_accounts || 0;
      if (document.getElementById('buffer-pct')) {{
        document.getElementById('buffer-pct').innerText = `${{data.config ? (data.config.min_buffer_pct || 0) : 0}}%`;
      }}
      if (document.getElementById('auto-relay-stat')) {{
        const isAuto = data.config ? (data.config.auto_relay !== false) : true;
        const autoEl = document.getElementById('auto-relay-stat');
        autoEl.innerText = isAuto ? t('statusOn') : t('statusOff');
        autoEl.style.color = isAuto ? 'var(--accent-green)' : 'var(--accent-amber)';
      }}

      const activePidsCount = data.accounts.reduce((sum, a) => sum + (a.active_pids ? a.active_pids.length : 0), 0);
      const activePidsEl = document.getElementById('active-pids-count');
      if (activePidsEl) {{
        activePidsEl.innerText = activePidsCount;
      }}

      document.getElementById('total-5h-tokens').innerHTML = `${{formatNumber(data.totals.stats_5h.total_tokens)}} <span class="stat-unit">Tokens</span>`;
      document.getElementById('total-5h-reqs').innerText = formatNumber(data.totals.stats_5h.requests);
      document.getElementById('total-5h-thinking').innerText = formatNumber(data.totals.stats_5h.thinking_tokens);

      document.getElementById('total-7d-tokens').innerHTML = `${{formatNumber(data.totals.stats_7d.total_tokens)}} <span class="stat-unit">Tokens</span>`;
      document.getElementById('total-7d-reqs').innerText = formatNumber(data.totals.stats_7d.requests);

      document.getElementById('total-all-tokens').innerHTML = `${{formatNumber(data.totals.stats_all.total_tokens)}} <span class="stat-unit">Tokens</span>`;
      document.getElementById('total-all-reqs').innerText = formatNumber(data.totals.stats_all.requests);

      // Render Accounts Grid
      const container = document.getElementById('accounts-container');
      container.innerHTML = '';

      data.accounts.forEach(acc => {{
        const usability = acc.usability || {{}};
        const isExhausted = usability.code === 'WEEKLY_EXHAUSTED';
        const oq = acc.official_quota || {{}};
        const gGroup = (oq.groups && oq.groups.gemini) ? oq.groups.gemini : null;
        const cGroup = (oq.groups && oq.groups.claude_gpt) ? oq.groups.claude_gpt : null;

        const g5h = gGroup && gGroup.buckets ? gGroup.buckets['gemini-5h'] : null;
        const gWk = gGroup && gGroup.buckets ? gGroup.buckets['gemini-weekly'] : null;
        const cWk = cGroup && cGroup.buckets ? cGroup.buckets['3p-weekly'] : null;
        const c5h = cGroup && cGroup.buckets ? cGroup.buckets['3p-5h'] : null;

        // Calculate max daily token for sparkline scale
        const dailyVals = Object.values(acc.daily_stats || {{}}).map(d => d.total_tokens);
        const maxDaily = Math.max(...dailyVals, 1000);

        let sparklineBars = '';
        Object.entries(acc.daily_stats || {{}}).forEach(([dStr, dStat]) => {{
          const heightPct = Math.max(4, Math.round((dStat.total_tokens / maxDaily) * 100));
          const dayLabel = dStr.slice(5); // MM-DD
          sparklineBars += `
            <div class="bar-col" title="${{dStr}}: ${{formatNumber(dStat.total_tokens)}} Tokens (${{dStat.requests}}${{t('unitTimes')}})">
              <div class="bar" style="height: ${{heightPct}}%"></div>
              <div class="bar-date">${{dayLabel}}</div>
            </div>
          `;
        }});

        // Gemini 5H Limit Remaining
        const is5hDisabled = g5h && g5h.disabled;
        const pct5h = g5h ? g5h.remainingPct : 100;
        const bar5hClass = is5hDisabled ? 'bar-exhausted' : (pct5h <= 15 ? 'bar-exhausted' : (pct5h <= 40 ? 'bar-warning' : ''));
        const pct5hClass = is5hDisabled ? 'pct-exhausted' : (pct5h <= 15 ? 'pct-exhausted' : (pct5h <= 40 ? 'pct-warning' : ''));

        // Gemini Weekly Limit Remaining
        const pctWk = gWk ? gWk.remainingPct : 100;
        const barWkClass = pctWk <= 15 ? 'bar-exhausted' : (pctWk <= 40 ? 'bar-warning' : '');
        const pctWkClass = pctWk <= 15 ? 'pct-exhausted' : (pctWk <= 40 ? 'pct-warning' : '');

        // Claude & GPT Weekly Limit Remaining
        const isClaudeExhausted = cWk && cWk.remainingFraction === 0;
        const pctClaudeWk = cWk ? cWk.remainingPct : 100;

        // Status tag label localization
        let statusLabel = usability.label || t('statusReady');
        if (usability.code === 'NOT_AUTH') statusLabel = t('statusNotAuth');
        else if (usability.code === 'REGION_PENDING') statusLabel = t('statusRegion');
        else if (usability.code === 'WEEKLY_EXHAUSTED') statusLabel = t('statusExhausted');
        else if (usability.code === 'CLAUDE_EXHAUSTED') statusLabel = t('statusClaudeEx');
        else if (usability.code === 'COOLDOWN_5H') {{
          if (is5hDisabled) statusLabel = t('disabledStatus');
          else statusLabel = `${{t('statusCooldown')}} (${{g5h ? g5h.remainingPct : 0}}%)`;
        }}
        else if (usability.code === 'READY') {{
          const g5hRem = g5h ? g5h.remainingPct : 100;
          const gWkRem = gWk ? gWk.remainingPct : 100;
          if (gWkRem < 100 || g5hRem < 100) {{
            statusLabel = `${{t('statusReady')}} (${{t('labelWeekly') || '周'}}: ${{gWkRem}}% | 5H: ${{g5hRem}}%)`;
          }} else {{
            statusLabel = t('statusReady100');
          }}
        }}

        // PID label localization
        let pidText = t('pidIdle');
        if (acc.active_pids && acc.active_pids.length > 1) {{
          pidText = `${{t('pidRunning')}} (${{acc.active_pids.length}})`;
        }} else if (acc.active_pids && acc.active_pids.length === 1) {{
          pidText = `${{t('pidSingle')}} ${{acc.active_pids[0]}}`;
        }}

        let canInitiateRelay = true;
        let relayDisabledReason = '';

        if (!acc.auth || !acc.auth.is_valid) {{
          canInitiateRelay = false;
          relayDisabledReason = t('relayDisabledNotAuth');
        }} else if (usability.code === 'TOKEN_EXPIRED') {{
          canInitiateRelay = false;
          relayDisabledReason = t('relayDisabledExpired');
        }} else if (usability.code === 'REGION_PENDING') {{
          canInitiateRelay = false;
          relayDisabledReason = t('relayDisabledRegion');
        }} else if (!acc.conversations || acc.conversations.length === 0) {{
          canInitiateRelay = false;
          relayDisabledReason = t('relayDisabledNoConvo');
        }}

        const relayCardBtnHtml = canInitiateRelay
          ? `<button class="btn-relay-card" onclick="openAccountRelay('${{acc.name}}')" title="${{t('accountRelayTip')}}">${{t('btnRelayAccount')}}</button>`
          : `<button class="btn-relay-card disabled" disabled title="${{relayDisabledReason}}">${{t('btnRelayAccount')}}</button>`;

        const card = document.createElement('div');
        card.className = `account-card ${{isExhausted ? 'card-exhausted' : ''}}`;
        card.innerHTML = `
          <div class="acc-header">
            <div class="acc-title-area">
              <div class="acc-id-badge">${{acc.id}}</div>
              <div style="min-width:0;flex:1">
                <div class="acc-name">${{acc.name}}</div>
                <div class="acc-email" title="${{acc.email}}">${{acc.email}}</div>
              </div>
            </div>
          </div>
          <div class="acc-status-row">
            <span class="status-tag ${{usability.badge_class || 'status-ready'}}" title="${{usability.reason || ''}}">${{statusLabel}}</span>
          </div>

          <!-- Official Quota: Gemini Models -->
          <div class="metric-block">
            <div style="font-size:0.75rem;font-weight:700;color:var(--accent-blue);margin-bottom:0.75rem;display:flex;align-items:center;justify-content:space-between">
              <span>${{t('gModelsTitle')}}</span>
              <span style="font-size:0.6875rem;color:var(--text-muted)">${{t('officialQuota')}}</span>
            </div>

            <!-- Gemini Weekly Limit Remaining -->
            <div class="metric-row" style="margin-bottom:0.25rem">
              <span class="metric-title" style="font-size:0.75rem">${{t('limitWkRem')}}</span>
              <span class="progress-pct ${{pctWkClass}}" title="Gemini Weekly: ${{pctWk}}%">${{pctWk}}%</span>
            </div>
            <div class="progress-bar-wrapper" style="margin-top:0.25rem;margin-bottom:0.4rem">
              <div class="progress-bar ${{barWkClass}}" style="width: ${{pctWk}}%"></div>
            </div>
            ${{gWk && gWk.resetTs && pctWk < 100 ? `
            <div class="reset-countdown" style="margin-top:0.35rem;padding:0.35rem 0.55rem;font-size:0.6875rem" title="${{t('exactReleaseTime')}}${{formatExactTime(gWk.resetTs)}}${{t('localTimeSuffix')}}">
              <span style="color:#94a3b8;display:flex;align-items:center;gap:4px">
                ${{t('countdownWk')}}
              </span>
              <strong class="countdown-time mono" data-reset-ts="${{gWk.resetTs || 0}}">...</strong>
            </div>
            ` : `
            <div class="reset-countdown ready" style="margin-top:0.35rem;padding:0.35rem 0.55rem;font-size:0.6875rem">
              <span style="color:#94a3b8;display:flex;align-items:center;gap:4px">
                ${{t('readyWk')}}
              </span>
              <strong class="mono" style="color:var(--accent-green)">${{oq.available ? t('ready100') : t('pendingAuth')}}</strong>
            </div>
            `}}

            <!-- Gemini 5H Limit Remaining -->
            <div class="metric-row" style="margin-top:0.75rem;margin-bottom:0.25rem">
              <span class="metric-title" style="font-size:0.75rem">${{t('limit5hRem')}}</span>
              <span class="progress-pct ${{pct5hClass}}" title="${{is5hDisabled ? (g5h.description || t('disabledWeeklyLimit')) : 'Gemini 5h: ' + pct5h + '%'}}">${{is5hDisabled ? t('disabledStatus') : pct5h + '%'}}</span>
            </div>
            <div class="progress-bar-wrapper" style="margin-top:0.25rem;margin-bottom:0.4rem">
              <div class="progress-bar ${{bar5hClass}}" style="width: ${{is5hDisabled ? 0 : pct5h}}%"></div>
            </div>
            ${{is5hDisabled ? `
            <div class="reset-countdown" style="margin-top:0.35rem;padding:0.35rem 0.55rem;font-size:0.6875rem;border-color:rgba(244,63,94,0.3);background:rgba(244,63,94,0.08)" title="${{g5h.description || t('disabledWeeklyLimit')}}">
              <span style="color:#fb7185;font-weight:600;display:flex;align-items:center;gap:4px">
                ${{t('statusDisabled')}}
              </span>
            </div>
            ` : (g5h && g5h.resetTs && pct5h < 100 ? `
            <div class="reset-countdown" style="margin-top:0.35rem;padding:0.35rem 0.55rem;font-size:0.6875rem" title="${{t('exactReleaseTime')}}${{formatExactTime(g5h.resetTs)}}${{t('localTimeSuffix')}}">
              <span style="color:#94a3b8;display:flex;align-items:center;gap:4px">
                ${{t('countdown5h')}}
              </span>
              <strong class="countdown-time mono" data-reset-ts="${{g5h.resetTs || 0}}">...</strong>
            </div>
            ` : `
            <div class="reset-countdown ready" style="margin-top:0.35rem;padding:0.35rem 0.55rem;font-size:0.6875rem">
              <span style="color:#94a3b8;display:flex;align-items:center;gap:4px">
                ${{t('ready5h')}}
              </span>
              <strong class="mono" style="color:var(--accent-green)">${{oq.available ? t('ready100') : t('pendingAuth')}}</strong>
            </div>
            `)}}
          </div>

          <!-- Official Quota: Claude & GPT -->
          <div class="metric-block">
            <div style="font-size:0.75rem;font-weight:700;color:var(--accent-purple);margin-bottom:0.75rem;display:flex;align-items:center;justify-content:space-between">
              <span>${{t('claudeTitle')}}</span>
              <span style="font-size:0.6875rem;color:var(--text-muted)">${{t('claudeWk')}}</span>
            </div>
            <div class="metric-row" style="margin-bottom:0.25rem">
              <span class="metric-title" style="font-size:0.75rem">${{t('claudeRem')}}</span>
              <span class="progress-pct ${{isClaudeExhausted ? 'pct-exhausted' : ''}}">${{isClaudeExhausted ? '0.00%' : (cWk ? cWk.remainingPct + '%' : (oq.available ? '100%' : '-'))}}</span>
            </div>
            <div class="progress-bar-wrapper" style="margin-top:0.25rem;margin-bottom:0.4rem">
              <div class="progress-bar ${{isClaudeExhausted ? 'bar-exhausted' : ''}}" style="width: ${{isClaudeExhausted ? 0 : (cWk ? cWk.remainingPct : 100)}}%"></div>
            </div>
            ${{isClaudeExhausted ? `
            <div class="reset-countdown" style="margin-top:0.35rem;padding:0.35rem 0.55rem;font-size:0.6875rem;border-color:rgba(244,63,94,0.3);background:rgba(244,63,94,0.08)" title="${{t('exactReleaseTime')}}${{formatExactTime(cWk.resetTs)}}${{t('localTimeSuffix')}}">
              <span style="color:#fb7185;display:flex;align-items:center;gap:4px">
                ${{t('claudeCd')}}
              </span>
              <strong class="countdown-time mono" style="color:#fb7185" data-reset-ts="${{cWk.resetTs || 0}}">...</strong>
            </div>
            ` : `
            <div class="reset-countdown ready" style="margin-top:0.35rem;padding:0.35rem 0.55rem;font-size:0.6875rem">
              <span style="color:#94a3b8;display:flex;align-items:center;gap:4px">
                ${{oq.available ? t('claudeQuota') : t('claudeStatus')}}
              </span>
              <strong class="mono" style="color:${{oq.available ? 'var(--accent-green)' : 'var(--text-muted)'}}">${{oq.available ? t('claude100') : t('needLogin')}}</strong>
            </div>
            `}}
          </div>

          <!-- Local Token Stats Block -->
          <div class="metric-block">
            <div style="font-size:0.75rem;font-weight:700;color:#cbd5e1;margin-bottom:0.5rem;display:flex;align-items:center;justify-content:space-between">
              <span>${{t('localHistory')}}</span>
              <span class="mono" style="font-size:0.75rem;color:#fff">${{formatNumber(acc.stats_all.total_tokens)}} <span style="font-size:0.6875rem;color:var(--text-muted)">${{t('totalTokens')}}</span></span>
            </div>
            <div class="breakdown-chips">
              <div class="chip">${{t('chip5hTok')}} <strong>${{formatNumber(acc.stats_5h.total_tokens)}}</strong></div>
              <div class="chip">${{t('chip5hReq')}} <strong>${{acc.stats_5h.requests}}</strong></div>
              <div class="chip">${{t('chip7dTok')}} <strong>${{formatNumber(acc.stats_7d.total_tokens)}}</strong></div>
              <div class="chip">${{t('chip7dReq')}} <strong>${{acc.stats_7d.requests}}</strong></div>
            </div>
            <div class="chart-container" style="height:36px;margin-top:0.5rem">
              ${{sparklineBars}}
            </div>
          </div>

          ${{usability.code === 'COOLDOWN_5H' ? `
          <div style="margin-top:0.6rem;padding:0.45rem 0.65rem;background:rgba(234,179,8,0.08);border:1px dashed rgba(234,179,8,0.3);border-radius:8px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.4rem">
            <span style="font-size:0.6875rem;color:#fef08a;display:flex;align-items:center;gap:4px">
              <span>⚡</span> <span>${{t('relayCooldownHint')}}</span>
            </span>
            <button class="btn-relay-quick" onclick="openAccountRelay('${{acc.name}}')">${{t('btnRelay')}} ➔</button>
          </div>
          ` : ''}}

          <!-- Footer with PID, Shortcut, and Account Relay -->
          <div class="acc-footer">
            <div style="display:flex;align-items:center;gap:0.4rem;min-width:0;overflow:hidden">
              <span class="pid-tag ${{acc.active_pids && acc.active_pids.length ? 'active' : ''}}" title="${{acc.active_pids && acc.active_pids.length ? 'PID: ' + acc.active_pids.join(', ') : ''}}">
                ${{pidText}}
              </span>
              <span class="alias-pill mono" title="${{t('aliasTitle')}}agy-${{acc.id}} / agy-${{acc.name}}">agy-${{acc.id}}</span>
            </div>
            ${{relayCardBtnHtml}}
          </div>
        `;
        container.appendChild(card);
      }});

      // Setup Combined Activity Tabs (Heatmap + Trend Chart)
      const heatmapTabs = document.getElementById('heatmap-tabs');
      heatmapTabs.innerHTML = `<button class="tab-btn active" id="btn-tab-all" onclick="switchAccountView('all', this)">${{t('allCombined')}}</button>`;
      data.accounts.forEach(acc => {{
        const btn = document.createElement('button');
        btn.className = 'tab-btn';
        btn.innerText = `${{t('accTabPrefix')}} ${{acc.id}} (${{acc.name}})`;
        btn.onclick = () => switchAccountView(acc.name, btn);
        heatmapTabs.appendChild(btn);
      }});

      // Render Initial Activity Views (Heatmap & Trend Chart)
      switchAccountView(currentAccount || 'all');

      // Setup Table Tabs
      const tabsContainer = document.getElementById('account-tabs');
      tabsContainer.innerHTML = `<button class="tab-btn active" id="btn-tab-table-all" onclick="filterTable('all', this)">${{t('allAccTab')}}</button>`;
      data.accounts.forEach(acc => {{
        const btn = document.createElement('button');
        btn.className = 'tab-btn';
        btn.innerText = `${{t('accTabPrefix')}} ${{acc.id}} (${{acc.name}})`;
        btn.onclick = () => filterTable(acc.name, btn);
        tabsContainer.appendChild(btn);
      }});

      // Render Detailed Table
      renderTable(currentTableAccount || 'all');
    }}

    let currentTrendDimension = 'total';

    function setTrendDimension(dim, clickedBtn) {{
      currentTrendDimension = dim;
      document.querySelectorAll('#dimension-selector .range-btn').forEach(b => b.classList.remove('active'));
      if (clickedBtn) {{
        clickedBtn.classList.add('active');
      }}
      renderTrendChart(currentAccount, currentTrendRange);
    }}

    function setTrendScale(scale, clickedBtn) {{
      currentTrendScale = scale;
      document.querySelectorAll('#scale-selector .range-btn').forEach(b => b.classList.remove('active'));
      if (clickedBtn) {{
        clickedBtn.classList.add('active');
      }}
      renderTrendChart(currentAccount, currentTrendRange);
    }}

    function switchAccountView(accountName, clickedBtn) {{
      currentAccount = accountName;
      document.querySelectorAll('#heatmap-tabs .tab-btn').forEach(b => b.classList.remove('active'));
      if (clickedBtn) {{
        clickedBtn.classList.add('active');
      }} else {{
        const firstTab = document.querySelector('#heatmap-tabs .tab-btn');
        if (firstTab) firstTab.classList.add('active');
      }}
      renderHeatmap(accountName, currentHeatmapRange);
      renderTrendChart(accountName, currentTrendRange);
    }}

    function setTrendRange(rangeKey, clickedBtn) {{
      currentTrendRange = rangeKey;
      document.querySelectorAll('#trend-range-selector .range-btn').forEach(b => b.classList.remove('active'));
      if (clickedBtn) {{
        clickedBtn.classList.add('active');
      }}
      renderTrendChart(currentAccount, rangeKey);
    }}

    function setHeatmapRange(rangeKey, clickedBtn) {{
      currentHeatmapRange = rangeKey;
      document.querySelectorAll('#heatmap-range-selector .range-btn').forEach(b => b.classList.remove('active'));
      if (clickedBtn) {{
        clickedBtn.classList.add('active');
      }}
      renderHeatmap(currentAccount, rangeKey);
    }}

    function renderHeatmap(accountName, rangeKey) {{
      if (!rangeKey) rangeKey = currentHeatmapRange;
      let sourceCalendar = {{}};
      let todayStat = {{ total_tokens: 0, requests: 0 }};
      const todayDate = data.today_date || new Date().toISOString().slice(0, 10);

      if (accountName === 'all') {{
        sourceCalendar = data.totals_calendar || {{}};
        todayStat = (data.totals && data.totals.today) ? data.totals.today : (sourceCalendar[todayDate] || todayStat);
      }} else {{
        const acc = data.accounts.find(a => a.name === accountName);
        sourceCalendar = acc ? (acc.calendar_stats || {{}}) : {{}};
        todayStat = (acc && acc.today_stats) ? acc.today_stats : (sourceCalendar[todayDate] || todayStat);
      }}

      const allDays = Object.values(sourceCalendar);
      if (!allDays.length) return;

      const firstDate = new Date(allDays[0].date);
      let firstDow = (firstDate.getUTCDay() + 6) % 7; // Mon=0, Sun=6

      let currentWeek = new Array(firstDow).fill(null);
      const allWeeks = [];

      allDays.forEach(d => {{
        currentWeek.push(d);
        if (currentWeek.length === 7) {{
          allWeeks.push(currentWeek);
          currentWeek = [];
        }}
      }});
      if (currentWeek.length > 0) {{
        while (currentWeek.length < 7) {{
          currentWeek.push(null);
        }}
        allWeeks.push(currentWeek);
      }}

      let weeks = allWeeks;
      if (rangeKey === 'current') {{
        weeks = allWeeks.slice(-1);
      }} else if (rangeKey === '4w') {{
        weeks = allWeeks.slice(-4);
      }}

      const displayedDays = weeks.flat().filter(d => d !== null);
      const activeDays = displayedDays.filter(d => d.total_tokens > 0);
      const totalTokens = displayedDays.reduce((sum, d) => sum + d.total_tokens, 0);
      const totalRequests = displayedDays.reduce((sum, d) => sum + d.requests, 0);
      const maxDay = displayedDays.reduce((prev, curr) => (curr.total_tokens > prev.total_tokens ? curr : prev), {{ total_tokens: 0, date: '-' }});

      const summaryContainer = document.getElementById('heatmap-summary-chips');
      if (summaryContainer) {{
        summaryContainer.innerHTML = `
          <div class="metric-chip highlight-cyan">${{t('todayUsage')}} <strong>${{formatNumber(todayStat.total_tokens)}}</strong> <span class="chip-sub">(${{todayStat.requests}}${{t('unitTimes')}})</span></div>
          <div class="metric-chip">${{t('activeDays')}} <strong>${{activeDays.length}} / ${{displayedDays.length}} ${{t('unitDays')}}</strong></div>
          <div class="metric-chip">${{t('rangeTotal')}} <strong>${{formatNumber(totalTokens)}}</strong></div>
          <div class="metric-chip">${{t('totalCalls')}} <strong>${{formatNumber(totalRequests)}}</strong></div>
          <div class="metric-chip">${{t('peakDay')}} <strong>${{formatNumber(maxDay.total_tokens)}}</strong> <span class="chip-sub">(${{maxDay.date}})</span></div>
        `;
      }}

      const nonZeroVals = activeDays.map(d => d.total_tokens).sort((a, b) => a - b);
      const maxTok = nonZeroVals.length ? nonZeroVals[nonZeroVals.length - 1] : 0;
      let q1, q2, q3;
      if (nonZeroVals.length >= 4) {{
        q1 = nonZeroVals[Math.floor(nonZeroVals.length * 0.25)];
        q2 = nonZeroVals[Math.floor(nonZeroVals.length * 0.5)];
        q3 = nonZeroVals[Math.floor(nonZeroVals.length * 0.75)];
      }} else if (maxTok > 0) {{
        q1 = Math.round(maxTok * 0.15);
        q2 = Math.round(maxTok * 0.40);
        q3 = Math.round(maxTok * 0.75);
      }} else {{
        q1 = 10000; q2 = 50000; q3 = 200000;
      }}

      function getLevel(tok) {{
        if (!tok || tok <= 0) return 0;
        if (tok <= q1) return 1;
        if (tok <= q2) return 2;
        if (tok <= q3) return 3;
        return 4;
      }}

      const gridContainer = document.getElementById('heatmap-grid-container');
      if (gridContainer) {{
        gridContainer.innerHTML = '';

        let bodyHtml = `
          <div class="heatmap-body">
            <div class="heatmap-days-labels">
              <span>${{t('dayMon')}}</span>
              <span></span>
              <span>${{t('dayWed')}}</span>
              <span></span>
              <span>${{t('dayFri')}}</span>
              <span></span>
              <span>${{t('daySun')}}</span>
            </div>
            <div class="heatmap-weeks-container">
        `;

        weeks.forEach((week, wIdx) => {{
          bodyHtml += `<div class="heatmap-week-col">`;
          week.forEach((day, dIdx) => {{
            if (!day) {{
              bodyHtml += `<div class="heatmap-cell level-0" style="opacity:0.2;pointer-events:none"></div>`;
            }} else {{
              const isToday = (day.date === todayDate);
              const lvl = getLevel(day.total_tokens);
              bodyHtml += `
                <div class="heatmap-cell level-${{lvl}} ${{isToday ? 'today-cell' : ''}}" 
                     data-date="${{day.date}}" 
                     data-tokens="${{day.total_tokens}}"
                     data-prompt="${{day.prompt_tokens}}"
                     data-cached="${{day.cached_tokens || 0}}"
                     data-candidate="${{day.candidate_tokens || 0}}"
                     data-thinking="${{day.thinking_tokens}}"
                     data-output="${{day.output_tokens}}"
                     data-reqs="${{day.requests}}"
                     data-istoday="${{isToday ? '1' : '0'}}"
                     onmouseenter="showHeatmapTooltip(event, this)"
                     onmousemove="moveHeatmapTooltip(event)"
                     onmouseleave="hideHeatmapTooltip()">
                </div>
              `;
            }}
          }});
          bodyHtml += `</div>`;
        }});

        bodyHtml += `</div></div>`;
        gridContainer.innerHTML = bodyHtml;
      }}
    }}

    // ==========================================
    // Token Trend Curve Chart Renderer
    // ==========================================
    function renderTrendChart(accountName, rangeKey) {{
      let sourceCalendar = {{}};
      let todayStat = {{ total_tokens: 0, requests: 0 }};
      const todayDate = data.today_date || new Date().toISOString().slice(0, 10);

      if (accountName === 'all') {{
        sourceCalendar = data.totals_calendar || {{}};
        todayStat = (data.totals && data.totals.today) ? data.totals.today : (sourceCalendar[todayDate] || todayStat);
      }} else {{
        const acc = data.accounts.find(a => a.name === accountName);
        sourceCalendar = acc ? (acc.calendar_stats || {{}}) : {{}};
        todayStat = (acc && acc.today_stats) ? acc.today_stats : (sourceCalendar[todayDate] || todayStat);
      }}

      const allDays = Object.values(sourceCalendar);
      if (!allDays.length) return;

      let chartDays = allDays;
      if (rangeKey === '7d') {{
        chartDays = allDays.slice(-7);
      }} else if (rangeKey === '14d') {{
        chartDays = allDays.slice(-14);
      }} else if (rangeKey === 'sep') {{
        chartDays = allDays.filter(d => d.date >= '2026-09-01');
      }}

      const totalTok = chartDays.reduce((sum, d) => sum + d.total_tokens, 0);
      const totalReq = chartDays.reduce((sum, d) => sum + d.requests, 0);
      const avgTok = chartDays.length ? Math.round(totalTok / chartDays.length) : 0;
      const maxDay = chartDays.reduce((prev, curr) => (curr.total_tokens > prev.total_tokens ? curr : prev), {{ total_tokens: 0, date: '-' }});

      // Update Trend Footer Stats
      const footer = document.getElementById('trend-stats-footer') || document.getElementById('trend-summary-chips');
      if (footer) {{
        const todayPrompt = todayStat.prompt_tokens || 0;
        const todayCached = todayStat.cached_tokens || 0;
        const todayOut = (todayStat.candidate_tokens || 0) || ((todayStat.thinking_tokens || 0) + (todayStat.output_tokens || 0));
        footer.innerHTML = `
          <div class="metric-chip highlight-cyan">${{t('todayUsage')}} <strong>${{formatNumber(todayStat.total_tokens)}}</strong> <span class="chip-sub">(${{todayStat.requests}}${{t('unitTimes')}})</span></div>
          <div class="metric-chip" style="border-color:rgba(56,189,248,0.35)">读 <strong>${{formatNumber(todayPrompt)}}</strong></div>
          <div class="metric-chip" style="border-color:rgba(168,85,247,0.35)">缓存 <strong>${{formatNumber(todayCached)}}</strong></div>
          <div class="metric-chip" style="border-color:rgba(52,211,153,0.35)">写 <strong>${{formatNumber(todayOut)}}</strong></div>
          <div class="metric-chip">${{t('rangeTotal')}} <strong>${{formatNumber(totalTok)}}</strong></div>
          <div class="metric-chip">${{t('dailyAvg')}} <strong>${{formatNumber(avgTok)}}</strong></div>
        `;
      }}

      // Render SVG Chart
      const wrapper = document.getElementById('trend-chart-wrapper') || document.getElementById('trend-chart-container');
      if (!wrapper) return;

      const svgW = 540;
      const svgH = 160;
      const padL = 48;
      const padR = 15;
      const padT = 18;
      const padB = 25;
      const plotW = svgW - padL - padR;
      const plotH = svgH - padT - padB;

      let maxVal = 1000;
      if (currentTrendDimension === 'total') {{
        maxVal = Math.max(...chartDays.map(d => d.total_tokens || 0), 1000);
      }} else if (currentTrendDimension === 'breakdown') {{
        maxVal = Math.max(...chartDays.map(d => Math.max(d.total_tokens || 0, d.prompt_tokens || 0, d.cached_tokens || 0, d.candidate_tokens || 0)), 1000);
      }} else {{
        maxVal = Math.max(...chartDays.map(d => Math.max(d.prompt_tokens || 0, d.thinking_tokens || 0, d.output_tokens || 0)), 1000);
      }}
      const yMax = Math.ceil(maxVal * 1.15);
      const minLift = 8; // minimum visual height in px for days with tokens > 0

      function getY(val) {{
        if (!val || val <= 0) return padT + plotH;
        if (currentTrendScale === 'log') {{
          const logVal = Math.log10(Math.max(1, val));
          const logMax = Math.log10(Math.max(10, yMax));
          const ratio = Math.max(0, Math.min(1, logVal / logMax));
          return padT + plotH - (minLift + (plotH - minLift) * ratio);
        }} else {{
          const ratio = val / yMax;
          return padT + plotH - (minLift + (plotH - minLift) * ratio);
        }}
      }}

      const points = chartDays.map((d, i) => {{
        const x = chartDays.length > 1 ? padL + (i * (plotW / (chartDays.length - 1))) : padL + plotW / 2;
        const yTotal = getY(d.total_tokens || 0);
        const yPrompt = getY(d.prompt_tokens || 0);
        const yCached = getY(d.cached_tokens || 0);
        const yCandidate = getY((d.candidate_tokens || 0) || ((d.thinking_tokens || 0) + (d.output_tokens || 0)));
        const yThinking = getY(d.thinking_tokens || 0);
        const yOutput = getY(d.output_tokens || 0);
        return {{ x, yTotal, yPrompt, yCached, yCandidate, yThinking, yOutput, d }};
      }});

      window._trendPoints = points;

      function formatYLabel(num) {{
        if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
        if (num >= 1000) return (num / 1000).toFixed(0) + 'k';
        return String(Math.round(num));
      }}

      function buildSmoothPath(pts, yKey) {{
        if (!pts || !pts.length) return '';
        if (pts.length === 1) return `M ${{pts[0].x}} ${{pts[0][yKey]}}`;
        let path = `M ${{pts[0].x}} ${{pts[0][yKey]}}`;
        for (let i = 0; i < pts.length - 1; i++) {{
          const p0 = pts[i];
          const p1 = pts[i + 1];
          const cpX = (p0.x + p1.x) / 2;
          path += ` C ${{cpX}} ${{p0[yKey]}}, ${{cpX}} ${{p1[yKey]}}, ${{p1.x}} ${{p1[yKey]}}`;
        }}
        return path;
      }}

      let linesSvg = '';
      let legendSvg = '';

      if (currentTrendDimension === 'total') {{
        const pathTotal = buildSmoothPath(points, 'yTotal');
        const areaTotal = points.length ? `${{pathTotal}} L ${{points[points.length - 1].x}} ${{padT + plotH}} L ${{points[0].x}} ${{padT + plotH}} Z` : '';
        linesSvg = `
          ${{areaTotal ? `<path d="${{areaTotal}}" fill="url(#trendAreaGrad)"/>` : ''}}
          ${{pathTotal ? `<path d="${{pathTotal}}" fill="none" stroke="#38bdf8" stroke-width="2.5" stroke-linecap="round" filter="url(#glow)"/>` : ''}}
        `;
        legendSvg = `
          <g class="chart-legend" transform="translate(${{padL + 6}}, ${{padT - 6}})">
            <circle cx="4" cy="4" r="3.5" fill="#38bdf8"/>
            <text x="12" y="7" fill="#94a3b8" font-size="9" font-family="'JetBrains Mono', monospace">${{t('dimTotal')}} (Total)</text>
          </g>
        `;
      }} else if (currentTrendDimension === 'breakdown') {{
        const pathPrompt = buildSmoothPath(points, 'yPrompt');
        const pathCached = buildSmoothPath(points, 'yCached');
        const pathCandidate = buildSmoothPath(points, 'yCandidate');
        const pathTotal = buildSmoothPath(points, 'yTotal');
        linesSvg = `
          ${{pathTotal ? `<path d="${{pathTotal}}" fill="none" stroke="#64748b" stroke-width="1.2" stroke-dasharray="3,3"/>` : ''}}
          ${{pathPrompt ? `<path d="${{pathPrompt}}" fill="none" stroke="#38bdf8" stroke-width="2.2" stroke-linecap="round" filter="url(#glow)"/>` : ''}}
          ${{pathCached ? `<path d="${{pathCached}}" fill="none" stroke="#a855f7" stroke-width="2.2" stroke-linecap="round" filter="url(#glow)"/>` : ''}}
          ${{pathCandidate ? `<path d="${{pathCandidate}}" fill="none" stroke="#34d399" stroke-width="2.2" stroke-linecap="round" filter="url(#glow)"/>` : ''}}
        `;
        legendSvg = `
          <g class="chart-legend" transform="translate(${{padL + 6}}, ${{padT - 6}})">
            <circle cx="4" cy="4" r="3" fill="#38bdf8"/>
            <text x="11" y="7" fill="#38bdf8" font-size="8.5" font-family="'JetBrains Mono', monospace">读 Prompt</text>
            <circle cx="76" cy="4" r="3" fill="#a855f7"/>
            <text x="83" y="7" fill="#c084fc" font-size="8.5" font-family="'JetBrains Mono', monospace">缓存 Cache</text>
            <circle cx="152" cy="4" r="3" fill="#34d399"/>
            <text x="159" y="7" fill="#34d399" font-size="8.5" font-family="'JetBrains Mono', monospace">写 Output</text>
            <line x1="225" y1="4" x2="237" y2="4" stroke="#64748b" stroke-dasharray="2,2"/>
            <text x="241" y="7" fill="#94a3b8" font-size="8.5" font-family="'JetBrains Mono', monospace">总计 Total</text>
          </g>
        `;
      }} else {{
        const pathPrompt = buildSmoothPath(points, 'yPrompt');
        const pathThinking = buildSmoothPath(points, 'yThinking');
        const pathOutput = buildSmoothPath(points, 'yOutput');
        linesSvg = `
          ${{pathPrompt ? `<path d="${{pathPrompt}}" fill="none" stroke="#38bdf8" stroke-width="1.5" stroke-dasharray="2,2"/>` : ''}}
          ${{pathThinking ? `<path d="${{pathThinking}}" fill="none" stroke="#c084fc" stroke-width="2.2" stroke-linecap="round" filter="url(#glow)"/>` : ''}}
          ${{pathOutput ? `<path d="${{pathOutput}}" fill="none" stroke="#34d399" stroke-width="2.2" stroke-linecap="round" filter="url(#glow)"/>` : ''}}
        `;
        legendSvg = `
          <g class="chart-legend" transform="translate(${{padL + 6}}, ${{padT - 6}})">
            <circle cx="4" cy="4" r="3" fill="#c084fc"/>
            <text x="11" y="7" fill="#c084fc" font-size="8.5" font-family="'JetBrains Mono', monospace">思考 Thinking</text>
            <circle cx="92" cy="4" r="3" fill="#34d399"/>
            <text x="99" y="7" fill="#34d399" font-size="8.5" font-family="'JetBrains Mono', monospace">纯输出 Output</text>
            <line x1="185" y1="4" x2="197" y2="4" stroke="#38bdf8" stroke-dasharray="2,2"/>
            <text x="201" y="7" fill="#38bdf8" font-size="8.5" font-family="'JetBrains Mono', monospace">输入 Prompt</text>
          </g>
        `;
      }}

      const yMid = padT + plotH / 2;
      const yBottom = padT + plotH;
      const yTop = padT;

      let xLabelsSvg = '';
      const step = Math.max(1, Math.floor(chartDays.length / 5));
      chartDays.forEach((d, i) => {{
        const isToday = (d.date === todayDate);
        if (isToday) {{
          const pt = points[i];
          const label = d.date.slice(5); // MM-DD
          xLabelsSvg += `<text x="${{pt.x}}" y="${{yBottom + 16}}" fill="#06b6d4" font-weight="700" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="middle">${{label}}(${{t('todaySubLabel')}})</text>`;
        }} else if (i % step === 0 || i === chartDays.length - 1) {{
          const pt = points[i];
          const label = d.date.slice(5); // MM-DD
          xLabelsSvg += `<text x="${{pt.x}}" y="${{yBottom + 16}}" fill="#64748b" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="middle">${{label}}</text>`;
        }}
      }});

      let yLabelsSvg = '';
      if (currentTrendScale === 'log') {{
        yLabelsSvg = `
          <text x="${{padL - 8}}" y="${{yTop + 4}}" fill="#64748b" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="end">${{formatYLabel(yMax)}}</text>
          <text x="${{padL - 8}}" y="${{padT + plotH * 0.45 + 3}}" fill="#64748b" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="end">100k</text>
          <text x="${{padL - 8}}" y="${{padT + plotH * 0.75 + 3}}" fill="#64748b" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="end">1k</text>
          <text x="${{padL - 8}}" y="${{yBottom + 3}}" fill="#64748b" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="end">0</text>
        `;
      }} else {{
        yLabelsSvg = `
          <text x="${{padL - 8}}" y="${{yTop + 4}}" fill="#64748b" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="end">${{formatYLabel(yMax)}}</text>
          <text x="${{padL - 8}}" y="${{yMid + 3}}" fill="#64748b" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="end">${{formatYLabel(yMax / 2)}}</text>
          <text x="${{padL - 8}}" y="${{yBottom + 3}}" fill="#64748b" font-size="9" font-family="'JetBrains Mono', monospace" text-anchor="end">0</text>
        `;
      }}

      let circlesSvg = '';
      let todayBadgeSvg = '';
      points.forEach(p => {{
        const isToday = (p.d.date === todayDate);
        const hasData = (p.d.total_tokens > 0);
        if (isToday) {{
          circlesSvg += `
            <circle cx="${{p.x}}" cy="${{p.yTotal}}" r="5" fill="#06b6d4" stroke="#ffffff" stroke-width="2" class="trend-point today-point"
                    data-date="${{p.d.date}}"
                    data-tokens="${{p.d.total_tokens}}"
                    data-reqs="${{p.d.requests}}"
                    data-prompt="${{p.d.prompt_tokens}}"
                    data-cached="${{p.d.cached_tokens || 0}}"
                    data-candidate="${{p.d.candidate_tokens || 0}}"
                    data-thinking="${{p.d.thinking_tokens}}"
                    data-output="${{p.d.output_tokens}}"
                    data-istoday="1"
                    onmouseenter="showHeatmapTooltip(event, this)"
                    onmousemove="moveHeatmapTooltip(event)"
                    onmouseleave="hideHeatmapTooltip()">
            </circle>
          `;
          const badgeX = Math.min(svgW - 25, Math.max(50, p.x));
          const textAnchor = p.x > (svgW - 70) ? 'end' : (p.x < 70 ? 'start' : 'middle');
          const badgeY = Math.max(14, p.yTotal - 10);
          todayBadgeSvg = `
            <text x="${{badgeX}}" y="${{badgeY}}" fill="#06b6d4" font-size="10" font-weight="700" font-family="'JetBrains Mono', monospace" text-anchor="${{textAnchor}}" filter="url(#glow)">${{t('todayDot')}}${{formatNumber(p.d.total_tokens)}}</text>
          `;
        }} else if (hasData) {{
          circlesSvg += `
            <circle cx="${{p.x}}" cy="${{p.yTotal}}" r="4" fill="#38bdf8" stroke="#090d16" stroke-width="2" class="trend-point has-data"
                    data-date="${{p.d.date}}"
                    data-tokens="${{p.d.total_tokens}}"
                    data-reqs="${{p.d.requests}}"
                    data-prompt="${{p.d.prompt_tokens}}"
                    data-cached="${{p.d.cached_tokens || 0}}"
                    data-candidate="${{p.d.candidate_tokens || 0}}"
                    data-thinking="${{p.d.thinking_tokens}}"
                    data-output="${{p.d.output_tokens}}"
                    data-istoday="0"
                    onmouseenter="showHeatmapTooltip(event, this)"
                    onmousemove="moveHeatmapTooltip(event)"
                    onmouseleave="hideHeatmapTooltip()">
            </circle>
          `;
        }} else {{
          circlesSvg += `
            <circle cx="${{p.x}}" cy="${{p.yTotal}}" r="2" fill="rgba(148, 163, 184, 0.35)" stroke="#090d16" stroke-width="1" class="trend-point no-data"
                    data-date="${{p.d.date}}"
                    data-tokens="${{p.d.total_tokens}}"
                    data-reqs="${{p.d.requests}}"
                    data-prompt="${{p.d.prompt_tokens}}"
                    data-cached="${{p.d.cached_tokens || 0}}"
                    data-candidate="${{p.d.candidate_tokens || 0}}"
                    data-thinking="${{p.d.thinking_tokens}}"
                    data-output="${{p.d.output_tokens}}"
                    data-istoday="0"
                    onmouseenter="showHeatmapTooltip(event, this)"
                    onmousemove="moveHeatmapTooltip(event)"
                    onmouseleave="hideHeatmapTooltip()">
            </circle>
          `;
        }}
      }});

      wrapper.innerHTML = `
        <svg class="trend-chart-svg" viewBox="0 0 ${{svgW}} ${{svgH}}" preserveAspectRatio="none"
             onmousemove="handleTrendChartHover(event)" onmouseleave="handleTrendChartLeave()">
          <defs>
            <linearGradient id="trendAreaGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.3"/>
              <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.0"/>
            </linearGradient>
            <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
              <feGaussianBlur stdDeviation="3" result="coloredBlur"/>
              <feMerge>
                <feMergeNode in="coloredBlur"/>
                <feMergeNode in="SourceGraphic"/>
              </feMerge>
            </filter>
          </defs>

          <!-- Grid Lines -->
          <line x1="${{padL}}" y1="${{yTop}}" x2="${{padL + plotW}}" y2="${{yTop}}" stroke="rgba(255,255,255,0.06)" stroke-dasharray="3,3"/>
          <line x1="${{padL}}" y1="${{yMid}}" x2="${{padL + plotW}}" y2="${{yMid}}" stroke="rgba(255,255,255,0.06)" stroke-dasharray="3,3"/>
          <line x1="${{padL}}" y1="${{yBottom}}" x2="${{padL + plotW}}" y2="${{yBottom}}" stroke="rgba(255,255,255,0.1)"/>

          <!-- Y-Axis Labels -->
          ${{yLabelsSvg}}

          <!-- X-Axis Labels -->
          ${{xLabelsSvg}}

          <!-- Legend -->
          ${{legendSvg}}

          <!-- Area & Line Curves -->
          ${{linesSvg}}

          <!-- Vertical Hover Tracking Guide Line -->
          <line id="trend-hover-line" x1="0" y1="${{padT}}" x2="0" y2="${{padT + plotH}}" stroke="rgba(56, 189, 248, 0.5)" stroke-width="1.5" stroke-dasharray="2,2" style="opacity:0;pointer-events:none;transition:opacity 0.15s ease"/>

          <!-- Data Points & Today Badge -->
          ${{circlesSvg}}
          ${{todayBadgeSvg}}
        </svg>
      `;
    }}

    function handleTrendChartHover(event) {{
      if (!window._trendPoints || !window._trendPoints.length) return;
      const svg = event.currentTarget;
      const rect = svg.getBoundingClientRect();
      const mouseX = ((event.clientX - rect.left) / rect.width) * 540;
      let closest = window._trendPoints[0];
      let minDiff = Math.abs(mouseX - closest.x);
      for (let i = 1; i < window._trendPoints.length; i++) {{
        const diff = Math.abs(mouseX - window._trendPoints[i].x);
        if (diff < minDiff) {{
          minDiff = diff;
          closest = window._trendPoints[i];
        }}
      }}
      const hoverLine = document.getElementById('trend-hover-line');
      if (hoverLine) {{
        hoverLine.setAttribute('x1', closest.x);
        hoverLine.setAttribute('x2', closest.x);
        hoverLine.style.opacity = '1';
      }}
      const pt = document.querySelector(`.trend-point[data-date="${{closest.d.date}}"]`);
      if (pt) {{
        showHeatmapTooltip(event, pt);
      }}
    }}

    function handleTrendChartLeave() {{
      const hoverLine = document.getElementById('trend-hover-line');
      if (hoverLine) hoverLine.style.opacity = '0';
      hideHeatmapTooltip();
    }}

    // Heatmap Tooltip
    const tooltip = document.getElementById('heatmap-tooltip');
    function showHeatmapTooltip(event, el) {{
      const d = el.getAttribute('data-date');
      const isToday = el.getAttribute('data-istoday') === '1';
      const dateHeader = isToday ? `${{d}} <span style="color:var(--accent-cyan);font-weight:700">(${{t('todaySubLabel')}})</span>` : d;
      const tok = parseInt(el.getAttribute('data-tokens') || '0', 10);
      const req = parseInt(el.getAttribute('data-reqs') || '0', 10);
      const prompt = parseInt(el.getAttribute('data-prompt') || '0', 10);
      const cached = parseInt(el.getAttribute('data-cached') || '0', 10);
      const candidate = parseInt(el.getAttribute('data-candidate') || '0', 10);
      const think = parseInt(el.getAttribute('data-thinking') || '0', 10);
      const out = parseInt(el.getAttribute('data-output') || '0', 10);

      tooltip.innerHTML = `
        <div style="font-weight:700;color:#fff;margin-bottom:0.35rem;border-bottom:1px solid rgba(255,255,255,0.1);padding-bottom:0.25rem">${{dateHeader}}</div>
        <div style="display:flex;justify-content:space-between;gap:1.2rem;margin-bottom:2px">
          <span style="color:var(--text-muted)">${{t('thTotal')}}:</span>
          <strong style="color:var(--accent-blue)">${{formatNumber(tok)}}</strong>
        </div>
        <div style="display:flex;justify-content:space-between;gap:1.2rem;margin-bottom:2px">
          <span style="color:var(--text-muted)">${{t('thRequests')}}:</span>
          <strong>${{req}}</strong>
        </div>
        <div style="display:flex;justify-content:space-between;gap:1.2rem;margin-bottom:2px">
          <span style="color:var(--text-muted);display:flex;align-items:center;gap:4px"><span style="width:6px;height:6px;border-radius:50%;background:#38bdf8;display:inline-block"></span>${{t('thPrompt')}}:</span>
          <span>${{formatNumber(prompt)}}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:1.2rem;margin-bottom:2px">
          <span style="color:var(--text-muted);display:flex;align-items:center;gap:4px"><span style="width:6px;height:6px;border-radius:50%;background:#a855f7;display:inline-block"></span>${{t('thCached')}}:</span>
          <span style="color:#c084fc;font-weight:600">${{formatNumber(cached)}}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:1.2rem;margin-bottom:2px">
          <span style="color:var(--text-muted);display:flex;align-items:center;gap:4px"><span style="width:6px;height:6px;border-radius:50%;background:#34d399;display:inline-block"></span>${{t('thOutput')}}:</span>
          <span>${{formatNumber(candidate || (think + out))}}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:1.2rem;margin-bottom:2px;padding-left:10px">
          <span style="color:var(--text-muted)">└ ${{t('thThinking')}}:</span>
          <span style="color:#c084fc">${{formatNumber(think)}}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:1.2rem;padding-left:10px">
          <span style="color:var(--text-muted)">└ 纯输出 (Out):</span>
          <span>${{formatNumber(out)}}</span>
        </div>
      `;
      tooltip.style.display = 'block';
      moveHeatmapTooltip(event);
    }}

    function moveHeatmapTooltip(event) {{
      const pad = 14;
      const tipWidth = tooltip.offsetWidth || 230;
      const tipHeight = tooltip.offsetHeight || 200;

      let left = event.clientX + pad;
      let top = event.clientY + pad;

      // Flip horizontally if overflowing right edge
      if (left + tipWidth > window.innerWidth - 12) {{
        left = event.clientX - tipWidth - pad;
      }}
      // Clamp to left screen edge
      if (left < 12) {{
        left = 12;
      }}

      // Flip vertically if overflowing bottom edge
      if (top + tipHeight > window.innerHeight - 12) {{
        top = event.clientY - tipHeight - pad;
      }}
      // Clamp to top screen edge
      if (top < 12) {{
        top = 12;
      }}

      tooltip.style.left = left + 'px';
      tooltip.style.top = top + 'px';
    }}

    function hideHeatmapTooltip() {{
      tooltip.style.display = 'none';
    }}

    // ==========================================
    // Table Filtering
    // ==========================================
    function filterTable(filterAcc, clickedBtn) {{
      currentTableAccount = filterAcc;
      document.querySelectorAll('#account-tabs .tab-btn').forEach(b => b.classList.remove('active'));
      if (clickedBtn) {{
        clickedBtn.classList.add('active');
      }} else {{
        const firstTab = document.querySelector('#account-tabs .tab-btn');
        if (firstTab) firstTab.classList.add('active');
      }}
      renderTable(filterAcc);
    }}

    function renderTable(filterAcc) {{
      const tbody = document.getElementById('detailed-table-body');
      if (!tbody) return;
      tbody.innerHTML = '';

      let rowsCount = 0;
      data.accounts.forEach(acc => {{
        if (filterAcc !== 'all' && acc.name !== filterAcc) return;

        acc.conversations.forEach(c => {{
          rowsCount++;
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><strong style="color:var(--accent-blue)">${{acc.name}}</strong> <span style="font-size:0.75rem;color:var(--text-muted)">(ID:${{acc.id}})</span></td>
            <td>
              <div style="font-weight:600;color:#fff">${{c.title}}</div>
              <div class="mono" style="font-size:0.6875rem;color:var(--text-muted)">${{c.id}}</div>
            </td>
            <td class="mono" style="font-size:0.75rem">${{c.last_modified ? c.last_modified.slice(0, 19).replace('T', ' ') : '-'}}</td>
            <td class="mono"><strong>${{c.requests}}</strong></td>
            <td class="mono">${{formatNumber(c.prompt_tokens)}}</td>
            <td class="mono" style="color:#c084fc">${{formatNumber(c.cached_tokens || 0)}}</td>
            <td class="mono" style="color:var(--accent-purple)">${{formatNumber(c.thinking_tokens)}}</td>
            <td class="mono" style="color:var(--accent-green)">${{formatNumber(c.output_tokens)}}</td>
            <td class="mono"><strong style="color:#fff">${{formatNumber(c.total_tokens)}}</strong></td>
          `;
          tbody.appendChild(tr);
        }});
      }});

      if (rowsCount === 0) {{
        const tr = document.createElement('tr');
        tr.innerHTML = `<td colspan="9" style="text-align:center;padding:2rem;color:var(--text-muted)">${{t('noRecords')}}</td>`;
        tbody.appendChild(tr);
      }}
    }}

    // ==========================================
    // Real-Time Countdowns Ticker
    // ==========================================
    function formatExactTime(ts) {{
      if (!ts || ts <= 0) return t('noPending');
      const d = new Date(ts * 1000);
      const Y = d.getFullYear();
      const M = String(d.getMonth() + 1).padStart(2, '0');
      const D = String(d.getDate()).padStart(2, '0');
      const h = String(d.getHours()).padStart(2, '0');
      const m = String(d.getMinutes()).padStart(2, '0');
      const s = String(d.getSeconds()).padStart(2, '0');
      return `${{Y}}-${{M}}-${{D}} ${{h}}:${{m}}:${{s}}`;
    }}

    function formatCountdown(totalSecs) {{
      if (totalSecs <= 0) return t('released');
      const d = Math.floor(totalSecs / 86400);
      const h = Math.floor((totalSecs % 86400) / 3600);
      const m = Math.floor((totalSecs % 3600) / 60);
      const s = totalSecs % 60;
      if (d > 0) {{
        return `${{d}}${{t('dayUnit')}}${{String(h).padStart(2, '0')}}:${{String(m).padStart(2, '0')}}:${{String(s).padStart(2, '0')}}`;
      }}
      return `${{String(h).padStart(2, '0')}}:${{String(m).padStart(2, '0')}}:${{String(s).padStart(2, '0')}}`;
    }}

    function updateCountdowns() {{
      const now = Math.floor(Date.now() / 1000);
      document.querySelectorAll('.countdown-time[data-reset-ts]').forEach(el => {{
        const resetTs = parseInt(el.getAttribute('data-reset-ts'), 10);
        if (!resetTs || resetTs <= 0) {{
          el.innerText = t('releasedExact');
          el.title = t('noPending');
          return;
        }}
        const diff = resetTs - now;
        const exactTimeStr = formatExactTime(resetTs);
        const titleText = `${{t('exactReleaseTime')}}${{exactTimeStr}}${{t('localTimeSuffix')}}`;
        el.title = titleText;
        if (el.parentElement && el.parentElement.classList.contains('reset-countdown')) {{
          el.parentElement.title = titleText;
        }}
        if (diff <= 0) {{
          el.innerText = `00:00:00 (${{t('releasedExact')}})`;
          el.style.color = "var(--accent-green)";
        }} else {{
          el.innerText = formatCountdown(diff);
        }}
      }});
    }}

    // ==========================================
    // Quota Relay Controller
    // ==========================================
    let currentRelayFrom = '';
    let currentRelayCid = '';
    let selectedTargetName = '';
    let selectedTargetId = '';

    function openRelayModal(srcName, cid, encodedTitle) {{
      currentRelayFrom = srcName;
      currentRelayCid = cid;
      const title = decodeURIComponent(encodedTitle || '');
      document.getElementById('modal-relay-from-text').innerText = srcName;
      document.getElementById('modal-relay-convo-text').innerText = `${{title}} (${{cid.slice(0, 8)}}...)`;
      document.getElementById('relay-result-box').style.display = 'none';
      const submitBtn = document.getElementById('btn-relay-submit');
      submitBtn.style.display = 'inline-block';

      // Populate candidate accounts
      const listContainer = document.getElementById('modal-relay-candidates-list');
      listContainer.innerHTML = '';

      const candidates = data.accounts.filter(a => a.name !== srcName);
      if (!candidates.length) {{
        listContainer.innerHTML = `<div style="padding:1.5rem;text-align:center;color:var(--text-muted);font-size:0.8rem">${{t('noOtherAccounts')}}</div>`;
        submitBtn.disabled = true;
        submitBtn.style.opacity = '0.4';
        submitBtn.style.cursor = 'not-allowed';
        submitBtn.innerText = t('relayNoTargetBtn');
        document.getElementById('relay-modal').style.display = 'flex';
        return;
      }}

      // Check eligibility for each candidate
      function checkEligibility(a) {{
        if (!a.auth || !a.auth.is_valid) {{
          return {{ eligible: false, reason: t('relayReasonNotAuth') }};
        }}
        const u = a.usability || {{}};
        if (u.code === 'TOKEN_EXPIRED') {{
          return {{ eligible: false, reason: t('relayReasonExpired') }};
        }}
        if (u.code === 'REGION_PENDING') {{
          return {{ eligible: false, reason: t('relayReasonRegion') }};
        }}
        if (u.is_target_eligible === false) {{
          return {{ eligible: false, reason: u.target_ineligible_reason || u.reason || t('relayReasonIneligible') }};
        }}
        if (!u.is_usable) {{
          return {{ eligible: false, reason: u.reason || t('relayReasonIneligible') }};
        }}
        const oq = a.official_quota || {{}};
        if (oq.available === false) {{
          return {{ eligible: false, reason: oq.reason || t('relayReasonQuotaUnavail') }};
        }}
        const g5h = oq.groups && oq.groups.gemini && oq.groups.gemini.buckets ? oq.groups.gemini.buckets['gemini-5h'] : null;
        if (g5h && g5h.disabled) {{
          return {{ eligible: false, reason: t('relayReasonCooldown') }};
        }}
        if (g5h && g5h.remainingFraction === 0) {{
          return {{ eligible: false, reason: t('relayReasonCooldown') }};
        }}
        return {{ eligible: true, reason: '' }};
      }}

      // Pick best candidate among ELIGIBLE candidates only
      let bestCandidate = null;
      let bestScore = -999;
      candidates.forEach(a => {{
        const check = checkEligibility(a);
        if (!check.eligible) return;

        let score = 0;
        const uCode = a.usability ? a.usability.code : '';
        if (uCode === 'READY') score += 500;
        else if (uCode === 'COOLDOWN_5H') score -= 300;
        const oq = a.official_quota || {{}};
        const g5h = oq.groups && oq.groups.gemini && oq.groups.gemini.buckets ? oq.groups.gemini.buckets['gemini-5h'] : null;
        const gWk = oq.groups && oq.groups.gemini && oq.groups.gemini.buckets ? oq.groups.gemini.buckets['gemini-weekly'] : null;
        if (gWk) score += (gWk.remainingPct || 0) * 2;
        if (g5h) score += (g5h.remainingPct || 0);
        if (!a.active_pids || a.active_pids.length === 0) score += 100;
        if (score > bestScore) {{
          bestScore = score;
          bestCandidate = a;
        }}
      }});

      if (bestCandidate) {{
        selectedTargetName = bestCandidate.name;
        selectedTargetId = bestCandidate.id;
        submitBtn.disabled = false;
        submitBtn.style.opacity = '1';
        submitBtn.style.cursor = 'pointer';
        submitBtn.innerText = t('relayBtnSubmit');
      }} else {{
        selectedTargetName = '';
        selectedTargetId = '';
        submitBtn.disabled = true;
        submitBtn.style.opacity = '0.4';
        submitBtn.style.cursor = 'not-allowed';
        submitBtn.innerText = t('relayNoTargetBtn');

        // Render prominent warning banner
        const banner = document.createElement('div');
        banner.className = 'relay-warning-banner';
        banner.style = 'margin-bottom:0.8rem;padding:0.65rem 0.85rem;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:8px;font-size:0.75rem;color:#fca5a5;line-height:1.4;';
        banner.innerHTML = `⚠️ <strong>${{t('relayNoTargetTitle')}}</strong><br><span style="color:#e2e8f0;font-size:0.7rem">${{t('relayNoTargetMsg')}}</span>`;
        listContainer.appendChild(banner);
      }}

      candidates.forEach(a => {{
        const check = checkEligibility(a);
        const isEligible = check.eligible;
        const isBest = (bestCandidate && a.name === bestCandidate.name);
        const item = document.createElement('div');
        item.className = `relay-item ${{isEligible ? (isBest ? 'selected' : '') : 'disabled'}}`;
        item.id = `relay-item-${{a.name}}`;

        if (isEligible) {{
          item.onclick = () => selectRelayTarget(a.name, a.id);
        }} else {{
          item.title = `${{t('relayIneligiblePrefix')}}${{check.reason}}`;
        }}

        const oq = a.official_quota || {{}};
        const g5h = oq.groups && oq.groups.gemini && oq.groups.gemini.buckets ? oq.groups.gemini.buckets['gemini-5h'] : null;
        const gWk = oq.groups && oq.groups.gemini && oq.groups.gemini.buckets ? oq.groups.gemini.buckets['gemini-weekly'] : null;
        const pct5h = (oq.available && g5h) ? `${{g5h.remainingPct}}%` : '-';
        const pctWk = (oq.available && gWk) ? `${{gWk.remainingPct}}%` : '-';

        let badgeHtml = '';
        if (isEligible) {{
          if (isBest) {{
            badgeHtml = `<span style="font-size:0.6875rem;color:var(--accent-green);font-weight:600">★ ${{t('relayAutoPick')}}</span>`;
          }}
        }} else {{
          badgeHtml = `<span style="font-size:0.6875rem;color:#f87171;font-weight:600;background:rgba(239,68,68,0.15);padding:2px 6px;border-radius:4px;border:1px solid rgba(239,68,68,0.3)">🚫 ${{check.reason}}</span>`;
        }}

        item.innerHTML = `
          <div>
            <div style="font-weight:700;color:#fff;display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap">
              <span style="${{!isEligible ? 'color:#94a3b8' : ''}}">[${{a.id}}] ${{a.name}}</span>
              ${{badgeHtml}}
            </div>
            <div style="font-size:0.6875rem;color:var(--text-muted);margin-top:2px">${{a.email || '-'}}</div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:0.75rem;font-weight:700;color:${{isEligible ? 'var(--accent-blue)' : 'var(--text-muted)'}}">${{t('labelWeekly') || '周'}}: ${{pctWk}}</div>
            <div style="font-size:0.6875rem;color:var(--text-muted)">5H: ${{pct5h}}</div>
          </div>
        `;
        listContainer.appendChild(item);
      }});

      document.getElementById('relay-modal').style.display = 'flex';
    }}

    function selectRelayTarget(targetName, targetId) {{
      selectedTargetName = targetName;
      selectedTargetId = targetId;
      document.querySelectorAll('.relay-item').forEach(el => el.classList.remove('selected'));
      const activeEl = document.getElementById(`relay-item-${{targetName}}`);
      if (activeEl) activeEl.classList.add('selected');
    }}

    function closeRelayModal() {{
      document.getElementById('relay-modal').style.display = 'none';
    }}

    function openAccountRelay(srcName) {{
      const acc = data.accounts.find(a => a.name === srcName);
      const convos = acc ? acc.conversations : [];
      const cid = convos.length ? convos[0].id : '';
      const title = convos.length ? (convos[0].title || 'Active Session') : 'Current Task';
      openRelayModal(srcName, cid, encodeURIComponent(title));
    }}

    function openQuickRelay(srcName) {{
      openAccountRelay(srcName);
    }}

    async function executeRelay() {{
      if (!selectedTargetName) return;
      const submitBtn = document.getElementById('btn-relay-submit');
      submitBtn.disabled = true;
      submitBtn.innerText = 'Relaying...';

      try {{
        const resp = await fetch('/api/relay', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            from: currentRelayFrom,
            to: selectedTargetName,
            conversation_id: currentRelayCid,
            sync_brain: true
          }})
        }});
        const json = await resp.json();
        if (json.success) {{
          const resBox = document.getElementById('relay-result-box');
          resBox.style.display = 'block';
          const cmdText = `agy-${{selectedTargetId}} --conversation ${{currentRelayCid}}`;
          document.getElementById('relay-cmd-text').innerText = cmdText;
          document.getElementById('relay-status-msg').innerHTML = `✓ ${{t('relaySuccess')}}<br><span style="color:#cbd5e1">${{t('relayResumePrompt')}}</span>`;
          submitBtn.style.display = 'none';
        }} else {{
          alert('Relay failed: ' + (json.error || 'Unknown error'));
          submitBtn.disabled = false;
          submitBtn.innerText = t('relayBtnSubmit');
        }}
      }} catch (err) {{
        // Fallback in static view: generate command
        const resBox = document.getElementById('relay-result-box');
        resBox.style.display = 'block';
        const cmdText = `agy-multi relay ${{currentRelayCid}} --from ${{currentRelayFrom}} --to ${{selectedTargetName}}`;
        document.getElementById('relay-cmd-text').innerText = cmdText;
        document.getElementById('relay-status-msg').innerHTML = `ℹ️ ${{t('relayResumePrompt')}}`;
        submitBtn.style.display = 'none';
      }}
    }}

    function copyRelayCmd() {{
      const text = document.getElementById('relay-cmd-text').innerText;
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {{
        const btn = document.getElementById('btn-copy-text');
        btn.innerText = 'Copied!';
        setTimeout(() => {{ btn.innerText = (currentLang === 'zh' ? '复制' : 'Copy'); }}, 1500);
      }});
    }}

    // ==========================================
    // Real-Time Auto-Refresh Poller
    // ==========================================
    let autoRefreshTimer = null;
    let autoRefreshCountdown = 15;
    let isAutoRefreshEnabled = true;

    async function fetchUsageUpdate() {{
      try {{
        const resp = await fetch('/api/usage', {{ cache: 'no-store' }});
        if (!resp.ok) return;
        const freshData = await resp.json();
        Object.assign(data, freshData);
        updateStaticTexts();
        initDashboard();
        updateCountdowns();
      }} catch (err) {{
        // Silent catch during background poll
      }}
    }}

    function startAutoRefresh() {{
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      autoRefreshCountdown = 15;
      autoRefreshTimer = setInterval(() => {{
        if (!isAutoRefreshEnabled) return;
        autoRefreshCountdown--;
        const timerEl = document.getElementById('txt-refresh-timer');
        if (timerEl) timerEl.innerText = `(${{autoRefreshCountdown}}s)`;
        if (autoRefreshCountdown <= 0) {{
          autoRefreshCountdown = 15;
          fetchUsageUpdate();
        }}
      }}, 1000);
    }}

    function toggleAutoRefresh() {{
      isAutoRefreshEnabled = !isAutoRefreshEnabled;
      const dot = document.querySelector('.badge-live .dot');
      const timerEl = document.getElementById('txt-refresh-timer');
      if (!isAutoRefreshEnabled) {{
        if (dot) dot.style.background = 'var(--text-muted)';
        if (timerEl) timerEl.innerText = `(${{t('relayAutoRefreshPaused')}})`;
      }} else {{
        if (dot) dot.style.background = 'var(--accent-green)';
        autoRefreshCountdown = 15;
        if (timerEl) timerEl.innerText = '(15s)';
      }}
    }}

    document.addEventListener('visibilitychange', () => {{
      if (document.visibilityState === 'visible' && isAutoRefreshEnabled) {{
        fetchUsageUpdate();
        autoRefreshCountdown = 15;
      }}
    }});

    function openConfigModal() {{
      const cfg = data.config || {{}};
      const bufVal = cfg.min_buffer_pct !== undefined ? cfg.min_buffer_pct : 5.0;
      const isAuto = cfg.auto_relay !== false;
      const fallback = cfg.on_no_target || 'pause';

      document.getElementById('cfg-buffer-slider').value = bufVal;
      document.getElementById('cfg-buffer-display').innerText = `${{parseFloat(bufVal).toFixed(1)}}%`;
      document.getElementById('cfg-auto-relay-checkbox').checked = isAuto;
      document.getElementById('cfg-fallback-select').value = fallback;
      document.getElementById('cfg-save-toast').style.display = 'none';

      document.getElementById('config-modal').style.display = 'flex';
    }}

    function closeConfigModal() {{
      document.getElementById('config-modal').style.display = 'none';
    }}

    function updateBufferSlider(val) {{
      document.getElementById('cfg-buffer-display').innerText = `${{parseFloat(val).toFixed(1)}}%`;
    }}

    async function saveDashboardConfig() {{
      const bufVal = parseFloat(document.getElementById('cfg-buffer-slider').value);
      const isAuto = document.getElementById('cfg-auto-relay-checkbox').checked;
      const fallback = document.getElementById('cfg-fallback-select').value;
      const btn = document.getElementById('btn-cfg-save');

      btn.disabled = true;
      btn.innerText = t('saving');

      try {{
        const resp = await fetch('/api/config', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            min_buffer_pct: bufVal,
            auto_relay: isAuto,
            on_no_target: fallback
          }})
        }});
        const res = await resp.json();
        if (res.success) {{
          data.config = res.config;
          // Refresh usage to update UI with latest usability ratings under new buffer threshold
          await fetchUsageUpdate();
          const toast = document.getElementById('cfg-save-toast');
          if (toast) {{
            toast.innerText = t('configSavedToast');
            toast.style.display = 'block';
          }}
          setTimeout(() => {{
            closeConfigModal();
          }}, 1200);
        }} else {{
          alert('保存失败: ' + (res.error || '未知错误'));
        }}
      }} catch (e) {{
        alert('请求失败: ' + e);
      }} finally {{
        btn.disabled = false;
        btn.innerText = t('btnSaveConfig');
      }}
    }}

    function openPolicyModal() {{
      document.getElementById('policy-modal').style.display = 'flex';
    }}

    function closePolicyModal() {{
      document.getElementById('policy-modal').style.display = 'none';
    }}

    window.onload = () => {{
      initDashboard();
      updateCountdowns();
      setInterval(updateCountdowns, 1000);
      startAutoRefresh();
    }};
  </script>

  <!-- Quota Relay Handover Modal -->
  <div id="relay-modal" class="modal-overlay" onclick="if(event.target === this) closeRelayModal()">
    <div class="modal-card">
      <div class="modal-header">
        <div class="modal-title">
          <span>🚀</span>
          <span id="txt-relay-modal-title">会话流量/算力接力</span>
        </div>
        <button class="modal-close-btn" onclick="closeRelayModal()">&times;</button>
      </div>

      <div style="margin-bottom: 1.25rem; font-size: 0.8125rem;">
        <div style="margin-bottom: 0.5rem; display: flex; gap: 0.5rem;">
          <span style="color: var(--text-muted);" id="lbl-relay-from">源账号:</span>
          <strong id="modal-relay-from-text" style="color: var(--accent-blue);">--</strong>
        </div>
        <div style="display: flex; gap: 0.5rem;">
          <span style="color: var(--text-muted);" id="lbl-relay-convo">接力会话:</span>
          <span id="modal-relay-convo-text" class="mono" style="color: #fff; word-break: break-all;">--</span>
        </div>
      </div>

      <div style="font-size: 0.75rem; font-weight: 700; color: #cbd5e1; margin-bottom: 0.5rem;" id="lbl-relay-target">
        选择接力目标账号:
      </div>
      <div id="modal-relay-candidates-list" style="max-height: 220px; overflow-y: auto; margin-bottom: 1rem;">
        <!-- Injected by JS -->
      </div>

      <div id="relay-result-box" style="display: none; margin-bottom: 1rem;">
        <div style="padding: 0.75rem; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 8px; font-size: 0.75rem; color: var(--accent-green); margin-bottom: 0.5rem;" id="relay-status-msg">
          ✓ 接力成功！
        </div>
        <div class="relay-cmd-box">
          <span id="relay-cmd-text" class="mono"></span>
          <button class="btn-copy-cmd" onclick="copyRelayCmd()" id="btn-copy-text">复制</button>
        </div>
      </div>

      <div style="display: flex; justify-content: flex-end; gap: 0.75rem; margin-top: 1.25rem;">
        <button class="btn" onclick="closeRelayModal()" id="btn-relay-cancel">关闭</button>
        <button class="btn btn-primary" id="btn-relay-submit" onclick="executeRelay()">⚡ 确认接力</button>
      </div>
    </div>
  </div>

  <!-- Settings & Buffer Modal -->
  <div id="config-modal" class="modal-overlay" onclick="if(event.target === this) closeConfigModal()">
    <div class="modal-card" style="max-width: 520px;">
      <div class="modal-header">
        <div class="modal-title">
          <span>⚙️</span>
          <span id="txt-config-modal-title">策略与接管设置</span>
        </div>
        <button class="modal-close-btn" onclick="closeConfigModal()">&times;</button>
      </div>

      <!-- 1. Buffer pct slider -->
      <div style="margin-bottom: 1.5rem;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
          <label style="font-weight: 700; color: #fff; font-size: 0.875rem;" id="lbl-cfg-buffer">5H 配额保底余量比例</label>
          <span id="cfg-buffer-display" class="mono" style="color: var(--accent-cyan); font-weight: 800; font-size: 1.125rem;">5.0%</span>
        </div>
        <input type="range" id="cfg-buffer-slider" min="0" max="50" step="0.5" value="5.0" style="width: 100%; accent-color: var(--accent-cyan); cursor: pointer;" oninput="updateBufferSlider(this.value)">
        <p style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.35rem;" id="desc-cfg-buffer">
          当账号 5H 剩余配额低于此阈值时，自动触发接力迁移或保护性挂起，防止把当前账号额度完全打光。
        </p>
      </div>

      <!-- 2. Auto Relay switch -->
      <div style="margin-bottom: 1.5rem; padding: 1rem; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 10px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <div>
            <div style="font-weight: 700; color: #fff; font-size: 0.875rem;" id="lbl-cfg-auto-relay">自动接管开关 (Auto Takeover)</div>
            <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem;" id="desc-cfg-auto-relay">
              开启时额度不足自动迁移至其他可用账号；关闭时仅温和暂停现场，不自动切换账号。
            </div>
          </div>
          <label class="switch" style="margin-left: 1rem; flex-shrink: 0;">
            <input type="checkbox" id="cfg-auto-relay-checkbox" checked>
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>

      <!-- 3. Fallback action -->
      <div style="margin-bottom: 1.5rem;">
        <label style="font-weight: 700; color: #fff; font-size: 0.875rem; display: block; margin-bottom: 0.5rem;" id="lbl-cfg-fallback">无替代账号可用时策略</label>
        <select id="cfg-fallback-select" style="width: 100%; padding: 0.6rem 0.75rem; background: #020617; border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; color: #fff; font-size: 0.8125rem;">
          <option value="pause" id="opt-pause">挂起倒计时等待（推荐，等账号配额刷新自动唤醒）</option>
          <option value="burn_buffer" id="opt-burn">继续消耗保底余量（直至 0% 彻底耗尽）</option>
        </select>
      </div>

      <!-- Save Toast -->
      <div id="cfg-save-toast" style="display: none; padding: 0.6rem; background: rgba(16,185,129,0.15); border: 1px solid var(--accent-green); border-radius: 8px; color: var(--accent-green); font-size: 0.75rem; margin-bottom: 1rem; text-align: center;">
        ✓ 设置已保存并在所有终端实时生效！
      </div>

      <div style="display: flex; justify-content: flex-end; gap: 0.75rem;">
        <button class="btn" onclick="closeConfigModal()" id="btn-cfg-cancel">取消</button>
        <button class="btn btn-primary" onclick="saveDashboardConfig()" id="btn-cfg-save">💾 保存设置</button>
      </div>
    </div>
  </div>

  <!-- Policy Guide & Flowchart Modal -->
  <div id="policy-modal" class="modal-overlay" onclick="if(event.target === this) closePolicyModal()">
    <div class="modal-card modal-lg">
      <div class="modal-header">
        <div class="modal-title">
          <span>📖</span>
          <span id="txt-policy-modal-title">多账号接管策略与生命周期流程图</span>
        </div>
        <button class="modal-close-btn" onclick="closePolicyModal()">&times;</button>
      </div>

      <div style="font-size: 0.8125rem; color: var(--text-muted); margin-bottom: 1rem; line-height: 1.5;">
        agy-multi 采用“保底余量守护 + 温和存盘迁移 + 智能挂起唤醒 + 防并发主动退场”全生命周期策略，确保多账号无缝切换与任务连续性。
      </div>

      <div class="policy-flow">
        <!-- Phase 1 -->
        <div class="policy-card">
          <div class="policy-card-header">
            <span class="policy-badge badge-p1">Phase 1</span>
            <span class="policy-title">会话启动与就绪选优 (Launch & Readiness Check)</span>
          </div>
          <div class="policy-desc">
            启动会话时自动检测当前账号的 5H 剩余配额与周配额：
            <ul>
              <li><strong>5H 配额 &gt; 保底阈值</strong>：直接以该账号启动 Antigravity 会话。</li>
              <li><strong>5H 配额 &le; 保底阈值</strong>：
                <ul>
                  <li><strong>开启自动接管</strong>：智能推荐并无感切换至配额最高且空闲（无运行进程）的账号启动。</li>
                  <li><strong>关闭自动接管</strong>：停止切换，提示额度触碰保底并进入当前账号保护性挂起。</li>
                </ul>
              </li>
            </ul>
          </div>
        </div>

        <div class="policy-arrow">↓</div>

        <!-- Phase 2 -->
        <div class="policy-card">
          <div class="policy-card-header">
            <span class="policy-badge badge-p2">Phase 2</span>
            <span class="policy-title">运行期后台静默守护 (Runtime Watchdog Monitoring)</span>
          </div>
          <div class="policy-desc">
            会话运行期间，后台守护线程以 10 秒为周期静默监听官方 Quota API：
            <ul>
              <li><strong>余量充裕</strong>：静默伴随，终端前台完全无干扰。</li>
              <li><strong>触碰保底</strong>（例如余量 &le; 5%）：守护线程介入，向前台发送一次温和中断信号（<code>SIGINT</code>），触发 Antigravity CLI 正常结算当前轮次回合，安全存盘。</li>
            </ul>
          </div>
        </div>

        <div class="policy-arrow">↓</div>

        <!-- Phase 3 -->
        <div class="policy-card">
          <div class="policy-card-header">
            <span class="policy-badge badge-p3">Phase 3</span>
            <span class="policy-title">温和存盘与事务级接力 (Graceful Save & SQLite WAL Backup)</span>
          </div>
          <div class="policy-desc">
            前台 CLI 退出并恢复标准终端模式后，执行接力迁移：
            <ul>
              <li><strong>开启自动接管</strong>：
                <ul>
                  <li>通过 SQLite Online Backup API 对会话数据库（<code>conversations/&lt;cid&gt;.db</code>）进行事务级热备份，WAL 零损坏。</li>
                  <li>同步 <code>conversation_summaries.db</code> 会话元数据与 <code>brain/&lt;cid&gt;</code> 记忆/工件现场。</li>
                  <li>原地唤起目标账号 <code>agy --conversation &lt;cid&gt;</code>，实现<strong>无缝就地接续</strong>！</li>
                </ul>
              </li>
              <li><strong>关闭自动接管</strong>：不寻找新账号，保留当前会话现场，直接进入安全挂起等待。</li>
            </ul>
          </div>
        </div>

        <div class="policy-arrow">↓</div>

        <!-- Phase 4 -->
        <div class="policy-card">
          <div class="policy-card-header">
            <span class="policy-badge badge-p4">Phase 4</span>
            <span class="policy-title">保护性挂起与动态唤醒 (Suspend & Smart Auto-Wakeup)</span>
          </div>
          <div class="policy-desc">
            当暂无可用空闲账号时，任务进入静默倒计时挂起，无需按 Ctrl+C：
            <ul>
              <li><strong>动态就绪监听</strong>：每 5 秒轮询，若有其他账号完成任务变为空闲且配额充足，立即自动唤醒接管！</li>
              <li><strong>配额刷新唤醒</strong>：倒计时归零时（最早解冻账号配额释放），自动唤醒并重新拉起任务。</li>
            </ul>
          </div>
        </div>

        <div class="policy-arrow">↓</div>

        <!-- Phase 5 -->
        <div class="policy-card">
          <div class="policy-card-header">
            <span class="policy-badge badge-p5">Phase 5</span>
            <span class="policy-title">并发防护与主动退场 (Anti-Conflict & Safe Auto-Exit)</span>
          </div>
          <div class="policy-desc">
            防止外部接管后原挂起进程几小时后唤醒造成“脑裂双跑”：
            <ul>
              <li>挂起等待轮询期间，实时扫描系统底层进程与文件描述符（FD）。</li>
              <li>一旦检测到当前会话已在其他终端或账号中运行，本挂起进程<strong>立即自动安全退出（退出码 0）</strong>，彻底杜绝额度恢复后的冲突重试！</li>
            </ul>
          </div>
        </div>
      </div>

      <div style="display: flex; justify-content: flex-end; margin-top: 1.25rem;">
        <button class="btn btn-primary" onclick="closePolicyModal()">了解并关闭</button>
      </div>
    </div>
  </div>
</body>
</html>
"""
    return html


def save_html_dashboard(manager, output_file: Optional[Path] = None) -> Path:
    """Collects usage and writes the HTML dashboard to file."""
    if output_file is None:
        output_file = Path.cwd() / "usage_dashboard.html"
    usage_data = get_all_usage(manager)
    html_content = render_html_dashboard(usage_data)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    return output_file
