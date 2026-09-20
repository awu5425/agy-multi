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
