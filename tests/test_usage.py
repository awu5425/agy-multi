"""
Unit tests for agy_multi.usage module.
"""

import pytest
from pathlib import Path
from agy_multi.usage import decode_varint, parse_step_metadata, render_html_dashboard, get_all_usage
from agy_multi.manager import ProfileManager


def test_decode_varint():
    # 300 in varint is 0xAC 0x02
    data = bytes([0xAC, 0x02])
    val, pos = decode_varint(data, 0)
    assert val == 300
    assert pos == 2


def test_parse_step_metadata_empty():
    assert parse_step_metadata(b"") == {}


def test_render_html_dashboard():
    mock_data = {
        "generated_at": "2026-09-19 12:00:00 UTC",
        "timestamp": 1789800000,
        "accounts": [
            {
                "id": "1",
                "name": "test1",
                "email": "test1@gmail.com",
                "auth": {"is_valid": True},
                "active_pids": [],
                "stats_5h": {"requests": 5, "prompt_tokens": 1000, "candidate_tokens": 100, "thinking_tokens": 80, "output_tokens": 20, "total_tokens": 1100},
                "stats_7d": {"requests": 10, "prompt_tokens": 2000, "candidate_tokens": 200, "thinking_tokens": 160, "output_tokens": 40, "total_tokens": 2200},
                "stats_all": {"requests": 10, "prompt_tokens": 2000, "candidate_tokens": 200, "thinking_tokens": 160, "output_tokens": 40, "total_tokens": 2200},
                "daily_stats": {"2026-09-19": {"requests": 10, "prompt_tokens": 2000, "candidate_tokens": 200, "total_tokens": 2200}},
                "conversations": []
            }
        ],
        "totals": {
            "stats_5h": {"requests": 5, "prompt_tokens": 1000, "candidate_tokens": 100, "thinking_tokens": 80, "output_tokens": 20, "total_tokens": 1100},
            "stats_7d": {"requests": 10, "prompt_tokens": 2000, "candidate_tokens": 200, "thinking_tokens": 160, "output_tokens": 40, "total_tokens": 2200},
            "stats_all": {"requests": 10, "prompt_tokens": 2000, "candidate_tokens": 200, "thinking_tokens": 160, "output_tokens": 40, "total_tokens": 2200},
        }
    }
    html = render_html_dashboard(mock_data)
    assert "<!DOCTYPE html>" in html
    assert "test1@gmail.com" in html
    assert "agy-multi" in html
    assert "多账号监控看板" in html
    assert "dimension-selector" in html
    assert "th-cached" in html
    assert "showClaude5hAsDisabled = isClaude5hDisabled || isClaudeWkExhausted;" in html
    assert "fillClaude5hClass = 'fill-red'" in html
    assert "table-expand-wrapper" in html
    assert "toggleTableExpand" in html
    assert "displayConvos = tableExpanded ? allConvos : allConvos.slice(0, 10);" in html
    assert "showMoreConvos" in html
    assert "relayDisabledIdle" in html
    assert "!acc.active_pids || acc.active_pids.length === 0" in html
    assert "btn-relay-quick.disabled" in html
    assert "relay-anim-container" in html
    assert "relay-beam-track" in html
    assert "relay-beam-spark" in html



def test_parse_step_metadata_with_cached_tokens():
    def encode_varint(val: int) -> bytes:
        res = bytearray()
        while True:
            b = val & 0x7F
            val >>= 7
            if val:
                res.append(b | 0x80)
            else:
                res.append(b)
                break
        return bytes(res)

    def encode_tag(fn: int, wt: int) -> bytes:
        return encode_varint((fn << 3) | wt)

    # Build submessage for field 9 (Token usage)
    sub = bytearray()
    # sfn 1: 1318 (system_tokens)
    sub += encode_tag(1, 0) + encode_varint(1318)
    # sfn 2: 5909 (prompt_tokens)
    sub += encode_tag(2, 0) + encode_varint(5909)
    # sfn 3: 430 (candidate_tokens)
    sub += encode_tag(3, 0) + encode_varint(430)
    # sfn 5: 8127 (cached_tokens)
    sub += encode_tag(5, 0) + encode_varint(8127)
    # sfn 9: 378 (thinking_tokens)
    sub += encode_tag(9, 0) + encode_varint(378)
    # sfn 10: 52 (output_tokens)
    sub += encode_tag(10, 0) + encode_varint(52)

    # Outer message: fn 9, wt 2
    raw = encode_tag(9, 2) + encode_varint(len(sub)) + sub

    parsed = parse_step_metadata(raw)
    assert parsed.get("system_tokens") == 1318
    assert parsed.get("prompt_tokens") == 5909
    assert parsed.get("candidate_tokens") == 430
    assert parsed.get("cached_tokens") == 8127
    assert parsed.get("thinking_tokens") == 378
    assert parsed.get("output_tokens") == 52


def test_usability_and_relay_target_eligibility(tmp_path):
    import json
    from agy_multi.usage import get_profile_usage

    pdir = tmp_path / "test_profile"
    pdir.mkdir(parents=True, exist_ok=True)
    cli_dir = pdir / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True, exist_ok=True)

    # 1. Not authenticated
    p_unauth = {
        "id": "4",
        "name": "unauth_acc",
        "email": "unauth@example.com",
        "auth": {"is_valid": False},
        "active_pids": [],
        "profile_dir": str(pdir),
    }
    u_unauth = get_profile_usage(p_unauth)
    assert u_unauth["usability"]["code"] == "NOT_AUTH"
    assert u_unauth["usability"]["is_usable"] is False
    assert u_unauth["usability"]["is_target_eligible"] is False

    # 2. Expired token
    tok_file = cli_dir / "antigravity-oauth-token"
    tok_file.write_text(json.dumps({
        "token": {
            "access_token": "ya29.expired",
            "refresh_token": "1//refresh",
            "expiry": "2020-01-01T00:00:00Z"
        }
    }), encoding="utf-8")
    p_expired = {
        "id": "1",
        "name": "expired_acc",
        "email": "expired@example.com",
        "auth": {"is_valid": True},
        "active_pids": [],
        "profile_dir": str(pdir),
    }
    u_expired = get_profile_usage(p_expired)
    assert u_expired["usability"]["code"] == "TOKEN_EXPIRED"
    assert u_expired["usability"]["is_usable"] is False
    assert u_expired["usability"]["is_target_eligible"] is False
    assert "过期" in u_expired["usability"]["target_ineligible_reason"]

    # 3. Region restricted
    p_region = {
        "id": "2",
        "name": "region_acc",
        "email": "region@example.com",
        "auth": {"is_valid": True},
        "active_pids": [],
        "profile_dir": str(pdir),
        "region_restricted": True,
    }
    u_region = get_profile_usage(p_region)
    assert u_region["usability"]["code"] == "REGION_PENDING"
    assert u_region["usability"]["is_usable"] is False
    assert u_region["usability"]["is_target_eligible"] is False
    assert "地区受限" in u_region["usability"]["target_ineligible_reason"]


def test_near_zero_weekly_is_not_available(tmp_path, monkeypatch):
    """0.37% weekly still displays as 0% and must not be labeled available."""
    import json
    from agy_multi.usage import get_profile_usage

    pdir = tmp_path / "myway"
    cli_dir = pdir / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text(json.dumps({
        "token": {"access_token": "ya29.ok", "expiry": "2099-01-01T00:00:00Z"}
    }), encoding="utf-8")

    def fake_quota(_pdir):
        return {
            "available": True,
            "groups": {
                "gemini": {"buckets": {
                    "gemini-weekly": {"remainingFraction": 0.0036693, "remainingPct": 0.37, "disabled": False},
                    "gemini-5h": {"remainingFraction": 1.0, "remainingPct": 100.0, "disabled": False},
                }},
                "claude_gpt": {"buckets": {
                    "3p-weekly": {"remainingFraction": 0.000654, "remainingPct": 0.07, "disabled": False},
                    "3p-5h": {"remainingFraction": 0.0, "remainingPct": 0.0, "disabled": False},
                }},
            },
        }

    monkeypatch.setattr("agy_multi.usage.fetch_official_quota", fake_quota)
    usage = get_profile_usage({
        "id": "3",
        "name": "myway",
        "email": "myway@example.com",
        "auth": {"is_valid": True},
        "active_pids": [],
        "profile_dir": str(pdir),
    })
    assert usage["usability"]["code"] == "WEEKLY_EXHAUSTED"
    assert usage["usability"]["is_usable"] is False
    assert usage["usability"]["is_target_eligible"] is False
    assert "可用" not in usage["usability"]["label"]

    def fake_low_but_usable(_pdir):
        data = fake_quota(_pdir)
        data["groups"]["gemini"]["buckets"]["gemini-weekly"] = {
            "remainingFraction": 0.05, "remainingPct": 5.0, "disabled": False,
        }
        data["groups"]["claude_gpt"]["buckets"]["3p-weekly"] = {
            "remainingFraction": 0.5, "remainingPct": 50.0, "disabled": False,
        }
        return data

    monkeypatch.setattr("agy_multi.usage.fetch_official_quota", fake_low_but_usable)
    still_low = get_profile_usage({
        "id": "3",
        "name": "myway",
        "email": "myway@example.com",
        "auth": {"is_valid": True},
        "active_pids": [],
        "profile_dir": str(pdir),
    })
    assert still_low["usability"]["code"] == "LOW_WEEKLY"
    assert still_low["usability"]["is_usable"] is True


def test_subscription_tier_and_dashboard_visibility():
    from agy_multi.usage import profile_on_dashboard, subscription_from_load_code_assist

    assert profile_on_dashboard({}) is True
    assert profile_on_dashboard({"region_restricted": True}) is False
    assert profile_on_dashboard({"region_restricted": True, "show_on_dashboard": True}) is True
    assert profile_on_dashboard({"show_on_dashboard": False}) is False

    assert subscription_from_load_code_assist({
        "paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"},
        "currentTier": {"id": "free-tier"},
    })["tier"] == "PRO"
    assert subscription_from_load_code_assist({
        "currentTier": {"id": "free-tier", "name": "Antigravity Starter Quota"},
    })["tier"] == "FREE"
    assert subscription_from_load_code_assist({
        "paidTier": {"id": "g1-ultra-tier"},
    })["tier"] == "ULTRA"
    assert subscription_from_load_code_assist({})["tier"] == "FREE"
    assert subscription_from_load_code_assist(None)["tier"] is None
    blocked = subscription_from_load_code_assist({
        "currentTier": {"id": "free-tier"},
        "ineligibleTiers": [{"reasonCode": "UNSUPPORTED_LOCATION"}],
    })
    assert "UNSUPPORTED_LOCATION" in blocked["ineligible"]


def test_hidden_account_skipped_in_dashboard_totals(tmp_path, monkeypatch):
    from agy_multi.manager import ProfileManager
    from agy_multi.usage import get_all_usage

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    mgr = ProfileManager(base_dir=tmp_path / "profiles", real_home=mock_home)
    mgr.add_profile("shown", "shown@example.com")
    mgr.add_profile("hidden", "hidden@example.com")
    mgr.set_show_on_dashboard("hidden", False)

    zeros = {
        "requests": 0, "prompt_tokens": 0, "candidate_tokens": 0, "cached_tokens": 0,
        "thinking_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    }

    def fake_usage(profile, current_ts=None, min_buffer_pct=0.0):
        shown = profile.get("show_on_dashboard", True)
        stats = dict(zeros)
        stats["requests"] = 2 if shown else 9
        return {
            "name": profile["name"],
            "usability": {"is_usable": True, "code": "READY"},
            "stats_5h": stats,
            "stats_7d": dict(zeros),
            "stats_all": dict(zeros),
            "calendar_stats": {},
            "show_on_dashboard": shown,
        }

    monkeypatch.setattr("agy_multi.usage.get_profile_usage", fake_usage)
    data = get_all_usage(mgr)
    assert data["totals"]["usable_accounts"] == 1
    assert data["totals"]["stats_5h"]["requests"] == 2
    names = {a["name"]: a["show_on_dashboard"] for a in data["accounts"]}
    assert names == {"shown": True, "hidden": False}


def test_no_builtin_oauth_secrets():
    """Repository must not ship built-in OAuth client id/secret material."""
    import agy_multi.usage as usage_mod

    src = Path(usage_mod.__file__).read_text(encoding="utf-8")
    assert "_B64_CID" not in src
    assert "_B64_SEC" not in src
    assert "DEFAULT_CLIENT_ID" not in src
    assert "DEFAULT_CLIENT_SECRET" not in src
    assert not hasattr(usage_mod, "_B64_CID")
    assert not hasattr(usage_mod, "_B64_SEC")
    assert not hasattr(usage_mod, "DEFAULT_CLIENT_ID")
    assert not hasattr(usage_mod, "DEFAULT_CLIENT_SECRET")

    cid, csec = usage_mod.get_oauth_client_credentials()
    # Without env, credentials must be empty (never a baked-in fallback)
    import os
    if not (os.environ.get("AGY_OAUTH_CLIENT_ID") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")):
        assert cid == ""
    if not (os.environ.get("AGY_OAUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")):
        assert csec == ""


def test_fetch_official_quota_refresh_missing_env(tmp_path, monkeypatch):
    """Expired token + missing OAuth env → clear failure, no crash."""
    import json
    from agy_multi.usage import fetch_official_quota

    for key in (
        "AGY_OAUTH_CLIENT_ID",
        "AGY_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)

    pdir = tmp_path / "prof"
    cli = pdir / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True)
    (cli / "antigravity-oauth-token").write_text(
        json.dumps({
            "token": {
                "access_token": "ya29.expired",
                "refresh_token": "1//refresh-placeholder",
                "expiry": "2020-01-01T00:00:00Z",
            }
        }),
        encoding="utf-8",
    )

    result = fetch_official_quota(pdir)
    assert result["available"] is False
    reason = result.get("reason", "")
    assert "AGY_OAUTH_CLIENT" in reason
    assert "环境变量" in reason or "缺少" in reason


def test_claude_and_gemini_5h_disabled_when_weekly_depleted():
    """Weekly quota depletion (< 1%) must trigger 5H disabled display in both HTML and CLI logic."""
    from agy_multi.usage import bucket_is_depleted, render_html_dashboard

    # Depleted test cases (< 1% threshold or disabled flag)
    assert bucket_is_depleted({"remainingPct": 0.8, "disabled": False}) is True
    assert bucket_is_depleted({"remainingPct": 0.37, "disabled": False}) is True
    assert bucket_is_depleted({"remainingPct": 0.0, "disabled": False}) is True
    assert bucket_is_depleted({"remainingPct": 100.0, "disabled": True}) is True
    assert bucket_is_depleted({"remainingPct": 1.0, "disabled": False}) is False
    assert bucket_is_depleted({"remainingPct": 99.0, "disabled": False}) is False
    assert bucket_is_depleted(None) is False

    # HTML dashboard logic must contain Claude 5H disabling & red fill
    html = render_html_dashboard({"accounts": [], "totals": {"stats_5h": {}, "stats_7d": {}, "stats_all": {}}})
    assert "showClaude5hAsDisabled = isClaude5hDisabled || isClaudeWkExhausted;" in html
    assert "fillClaude5hClass = 'fill-red'" in html
    assert "showClaude5hAsDisabled ? t('disabledStatus')" in html


def test_granular_model_quotas_render_and_groups_synthesis():
    """Verify that specific targeted models (Gemini 4 Argon, Gemini 3.8 Flash, Claude Models)
    are rendered in strict order with compact dual/single capsule meters."""
    from agy_multi.usage import render_html_dashboard

    mock_data = {
        "accounts": [
            {
                "id": "3",
                "name": "martin",
                "email": "martin@example.com",
                "auth": {"is_valid": True},
                "active_pids": [],
                "usability": {"code": "READY", "label": "● 可用", "is_usable": True},
                "official_quota": {
                    "available": True,
                    "models": [
                        {
                            "id": "gemini-4-argon",
                            "displayName": "Gemini 4 Argon",
                            "provider": "Google Gemini",
                            "providerCode": "gemini",
                            "sortOrder": 1,
                            "has5h": False,
                            "hasWeekly": True,
                            "remainingWkPct": 88.7,
                            "resetWkTs": 1793284966.0,
                            "tagTitle": "New",
                            "supportsThinking": True,
                        },
                        {
                            "id": "gemini-3.8-flash",
                            "displayName": "Gemini 3.8 Flash",
                            "provider": "Google Gemini",
                            "providerCode": "gemini",
                            "sortOrder": 2,
                            "has5h": True,
                            "hasWeekly": True,
                            "remaining5hPct": 88.7,
                            "reset5hTs": 1793284966.0,
                            "remainingWkPct": 100.0,
                            "resetWkTs": 1793284966.0,
                            "supportsThinking": True,
                        },
                        {
                            "id": "claude-models-5-5",
                            "displayName": "Claude Models (Opus 5.5 / Sonnet 5.5)",
                            "provider": "Anthropic Claude",
                            "providerCode": "anthropic",
                            "sortOrder": 3,
                            "has5h": True,
                            "hasWeekly": True,
                            "remaining5hPct": 100.0,
                            "reset5hTs": 1793284966.0,
                            "remainingWkPct": 100.0,
                            "resetWkTs": 1793284966.0,
                            "tagTitle": "New",
                            "supportsThinking": True,
                        }
                    ],
                    "groups": {
                        "gemini": {"buckets": {"gemini-5h": {"remainingPct": 88.7}, "gemini-weekly": {"remainingPct": 100.0}}},
                        "claude_gpt": {"buckets": {"3p-5h": {"remainingPct": 100.0}, "3p-weekly": {"remainingPct": 100.0}}},
                    }
                },
                "stats_5h": {"requests": 1, "total_tokens": 1000},
                "stats_7d": {"requests": 2, "total_tokens": 2000},
                "stats_all": {"requests": 2, "total_tokens": 2000},
                "daily_stats": {},
            },
            {
                "id": "1",
                "name": "panya",
                "email": "panya@example.com",
                "auth": {"is_valid": True},
                "active_pids": [],
                "usability": {"code": "READY", "label": "● 可用", "is_usable": True},
                "official_quota": {
                    "available": True,
                    "models": [
                        {
                            "id": "gemini-3.8-flash",
                            "displayName": "Gemini 3.8 Flash",
                            "provider": "Google Gemini",
                            "providerCode": "gemini",
                            "sortOrder": 1,
                            "has5h": True,
                            "hasWeekly": True,
                            "remaining5hPct": 100.0,
                            "reset5hTs": 1793284966.0,
                            "remainingWkPct": 100.0,
                            "resetWkTs": 1793284966.0,
                            "supportsThinking": True,
                        },
                        {
                            "id": "claude-models-4-6",
                            "displayName": "Claude Models (Opus 4.6 / Sonnet 4.6)",
                            "provider": "Anthropic Claude",
                            "providerCode": "anthropic",
                            "sortOrder": 2,
                            "has5h": True,
                            "hasWeekly": True,
                            "remaining5hPct": 100.0,
                            "reset5hTs": 1793284966.0,
                            "remainingWkPct": 100.0,
                            "resetWkTs": 1793284966.0,
                            "tagTitle": "Thinking",
                            "supportsThinking": True,
                        }
                    ],
                    "groups": {
                        "gemini": {"buckets": {"gemini-5h": {"remainingPct": 100.0}, "gemini-weekly": {"remainingPct": 100.0}}},
                        "claude_gpt": {"buckets": {"3p-5h": {"remainingPct": 100.0}, "3p-weekly": {"remainingPct": 100.0}}},
                    }
                },
                "stats_5h": {"requests": 0, "total_tokens": 0},
                "stats_7d": {"requests": 0, "total_tokens": 0},
                "stats_all": {"requests": 0, "total_tokens": 0},
                "daily_stats": {},
            }
        ],
        "totals": {"stats_5h": {}, "stats_7d": {}, "stats_all": {}}
    }

    html = render_html_dashboard(mock_data)
    assert "models-container" in html
    assert "model-quota-block" in html
    assert "model-capsules" in html
    assert "capsule-meter" in html
    assert "Gemini 4 Argon" in html
    assert "Gemini 3.8 Flash" in html
    assert "Claude Models (Opus 5.5 / Sonnet 5.5)" in html
    assert "Claude Models (Opus 4.6 / Sonnet 4.6)" in html
    assert "labelWeekly" in html or "7D" in html


def test_normalize_model_family():
    from agy_multi.usage import normalize_model_family

    assert normalize_model_family("gemini-4-argon") == "Gemini 4 Argon"
    assert normalize_model_family("gemini-4-argon-thinking") == "Gemini 4 Argon"
    assert normalize_model_family("gemini-3.8-flash-high") == "Gemini 3.8 Flash"
    assert normalize_model_family("gemini-3.8-flash") == "Gemini 3.8 Flash"
    assert normalize_model_family("claude-opus-5-5-high") == "Claude Models"
    assert normalize_model_family("claude-sonnet-5-5") == "Claude Models"
    assert normalize_model_family("claude-opus-4-6-thinking") == "Claude Models"
    assert normalize_model_family("gemini-2.5-pro") == "Gemini Pro"
    assert normalize_model_family("gpt-oss-120b") == "GPT Models"
    assert normalize_model_family(None) == "Gemini 3.8 Flash"
    assert normalize_model_family("") == "Gemini 3.8 Flash"
    assert normalize_model_family("custom-unknown-engine") == "Other"


def test_extract_model_resolver_and_token_curve_rendering(tmp_path):
    import sqlite3
    from agy_multi.usage import extract_model_resolver, render_html_dashboard

    # Create a mock sqlite DB with gen_metadata
    db_file = tmp_path / "test_convo.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx));")
    
    # Empty DB fallback
    resolver_empty = extract_model_resolver(conn)
    assert resolver_empty(0) == "Gemini 3.8 Flash"
    conn.close()

    # Test dashboard HTML rendering includes the new 'By Model' dimension & curve assets
    mock_data = {
        "timestamp": 1700000000.0,
        "accounts": [],
        "totals": {
            "stats_5h": {"requests": 10, "total_tokens": 1000, "models": {"Gemini 3.8 Flash": 1000}},
            "stats_7d": {"requests": 50, "total_tokens": 5000, "models": {"Gemini 3.8 Flash": 5000}},
            "stats_all": {"requests": 100, "total_tokens": 10000, "models": {"Gemini 3.8 Flash": 10000}},
        },
        "totals_calendar": {
            "2026-10-29": {
                "date": "2026-10-29",
                "total_tokens": 10000,
                "requests": 5,
                "prompt_tokens": 8000,
                "cached_tokens": 1000,
                "candidate_tokens": 1000,
                "thinking_tokens": 500,
                "output_tokens": 500,
                "models": {"Gemini 4 Argon": 6000, "Gemini 3.8 Flash": 4000},
            }
        }
    }
    html = render_html_dashboard(mock_data)
    assert 'id="btn-dim-models"' in html
    assert 'dimModels' in html
    assert 'setTrendDimension(\'models\'' in html
    assert 'buildSmoothPathForModel' in html
    assert 'MODEL_COLORS' in html
    assert 'data-models=' in html


def test_argon_quota_extraction_and_weekly_exhaustion(tmp_path, monkeypatch):
    """Verify that Gemini 4 Argon extracts true weekly quota, never leaks 5H quotas,
    and correctly marks exhaustion when weekly quota is exhausted."""
    import json
    import io
    import urllib.request
    from agy_multi.usage import fetch_official_quota

    pdir = tmp_path / "martin"
    cli = pdir / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True)
    (cli / "antigravity-oauth-token").write_text(
        json.dumps({
            "token": {
                "access_token": "ya29.valid_token",
                "refresh_token": "1//refresh",
                "expiry": "2099-01-01T00:00:00Z",
            }
        }),
        encoding="utf-8",
    )

    class MockHTTPResponse:
        def __init__(self, data_dict):
            self._data = json.dumps(data_dict).encode("utf-8")
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def mock_urlopen(req, *args, **kwargs):
        url = req.full_url
        if "retrieveUserQuotaSummary" in url:
            return MockHTTPResponse({
                "groups": [
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {
                                "bucketId": "3p-weekly",
                                "window": "weekly",
                                "resetTime": "2026-11-05T17:01:17Z",
                                "remainingFraction": 0,
                            },
                            {
                                "bucketId": "3p-5h",
                                "window": "5h",
                                "resetTime": "2026-10-30T04:35:29Z",
                                "remainingFraction": 0,
                                "disabled": True,
                            }
                        ]
                    }
                ]
            })
        elif "fetchAvailableModels" in url:
            return MockHTTPResponse({
                "models": {
                    "gemini-2.5-pro": {
                        "displayName": "Gemini 2.5 Pro",
                        "quotaInfo": {
                            "remainingFraction": 0.941,
                            "resetTime": "2026-10-30T04:35:29Z",
                        }
                    },
                    "gemini-3.8-flash-tiered": {
                        "displayName": "Gemini 3.8 Flash",
                        "quotaInfo": {
                            "remainingFraction": 0.941,
                            "resetTime": "2026-10-30T04:35:29Z",
                        }
                    },
                    "claude-opus-5-5-high": {
                        "displayName": "Claude Opus 5.5",
                        "quotaInfo": {
                            "resetTime": "2026-11-05T17:01:17Z",
                        }
                    }
                }
            })
        return MockHTTPResponse({})

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    res = fetch_official_quota(pdir)
    assert res["available"] is True
    models = {m["id"]: m for m in res["models"]}
    assert "gemini-4-argon" in models
    assert "gemini-3.8-flash" in models
    assert "claude-models-5-5" in models

    argon = models["gemini-4-argon"]
    assert argon["has5h"] is False
    assert argon["hasWeekly"] is True
    # Must NOT inherit 5h 94.1% or 4h reset time from gemini-2.5-pro
    assert argon["remainingFraction"] == 0.0
    assert argon["remainingPct"] == 0.0
    assert argon["remainingWkPct"] == 0.0
    assert argon["resetTime"] == "2026-11-05T17:01:17Z"

    flash = models["gemini-3.8-flash"]
    assert flash["has5h"] is True
    assert flash["remainingPct"] == 94.1
    assert flash["remaining5hPct"] == 94.1
    assert flash["remainingWkPct"] == 100.0
    assert flash["resetTime"] == "2026-10-30T04:35:29Z"

    # Groups synthesis must NOT make gemini-5h 0% just because Argon is exhausted
    g_5h = res["groups"]["gemini"]["buckets"]["gemini-5h"]
    assert g_5h["remainingPct"] == 94.1





