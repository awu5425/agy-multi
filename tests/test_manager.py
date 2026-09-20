"""
Unit tests for agy_multi manager and utils.
"""

import os
import tempfile
import pytest
from pathlib import Path
from agy_multi.manager import ProfileManager
from agy_multi.utils import parse_jwt_payload, inspect_token_file


def test_jwt_payload_decode():
    # Header: {"alg":"none"}, Payload: {"email":"test@example.com","sub":"123"}
    # b64: eyJlbWFpbCI6InRlc3RAZXhhbXBsZS5jb20iLCJzdWIiOiIxMjMifQ
    payload_jwt = "eyJhbGciOiJub25lIn0.eyJlbWFpbCI6InRlc3RAZXhhbXBsZS5jb20iLCJzdWIiOiIxMjMifQ."
    claims = parse_jwt_payload(payload_jwt)
    assert claims.get("email") == "test@example.com"
    assert claims.get("sub") == "123"


def test_profile_manager_lifecycle(tmp_path):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    assert len(mgr.list_profiles()) == 0

    # Add profile
    p1 = mgr.add_profile("test1", "test1@gmail.com", "Test Account 1", custom_id="1")
    assert p1["id"] == "1"
    assert p1["name"] == "test1"

    # Find profile
    found = mgr.find_profile("1")
    assert found is not None
    assert found["name"] == "test1"

    found_by_name = mgr.find_profile("test1")
    assert found_by_name is not None

    found_by_email = mgr.find_profile("test1@gmail.com")
    assert found_by_email is not None

    # Check directories
    pdir = mgr.get_profile_dir("test1")
    assert pdir.exists()
    assert (pdir / ".gemini" / "antigravity-cli").exists()

    # Update profile
    updated = mgr.update_profile("1", new_name="test1_renamed", new_email="new@example.com")
    assert updated["name"] == "test1_renamed"
    assert updated["email"] == "new@example.com"
    assert mgr.get_profile_dir("test1_renamed").exists()


def test_detect_real_home(monkeypatch):
    # Case 1: AGY_REAL_HOME environment variable set
    monkeypatch.setenv("AGY_REAL_HOME", "/custom/real/home")
    assert ProfileManager._detect_real_home() == Path("/custom/real/home")

    # Case 2: HOME inside .gemini-profiles
    monkeypatch.delenv("AGY_REAL_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/developer/.gemini-profiles/profile1")
    assert ProfileManager._detect_real_home() == Path("/home/developer")


def test_conversation_relay(tmp_path):
    import sqlite3
    import json

    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    p_src = mgr.add_profile("src_prof", "src@example.com", "Source", custom_id="1")
    p_dst = mgr.add_profile("dst_prof", "dst@example.com", "Target", custom_id="2")

    src_cli = mgr.get_profile_dir("src_prof") / ".gemini" / "antigravity-cli"
    dst_cli = mgr.get_profile_dir("dst_prof") / ".gemini" / "antigravity-cli"

    # Setup source conversation
    cid = "test-convo-uuid-1234"
    src_convos = src_cli / "conversations"
    src_convos.mkdir(parents=True, exist_ok=True)
    convo_db = src_convos / f"{cid}.db"

    # Create dummy conversation db
    conn = sqlite3.connect(convo_db)
    conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata TEXT);")
    conn.execute("INSERT INTO steps VALUES (1, ?);", (json.dumps({"prompt_tokens": 100, "total_tokens": 200, "timestamp": 1726000000}),))
    conn.commit()
    conn.close()

    # Create dummy conversation_summaries.db
    src_summaries_db = src_cli / "conversation_summaries.db"
    conn = sqlite3.connect(src_summaries_db)
    conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, last_modified_time TEXT, workspace_uris TEXT);")
    conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?);", (cid, "Test Conversation Title", "2026-09-20 01:00:00", "/workspace/test"))
    conn.commit()
    conn.close()

    # Create dummy brain artifacts
    src_brain = src_cli / "brain" / cid
    src_brain.mkdir(parents=True, exist_ok=True)
    (src_brain / "artifact.md").write_text("# Hello Relay", encoding="utf-8")

    # Test get_most_recent_conversation
    recent = mgr.get_most_recent_conversation("src_prof")
    assert recent is not None
    assert recent["id"] == cid
    assert recent["title"] == "Test Conversation Title"

    # Execute relay
    result = mgr.relay_conversation("src_prof", "dst_prof", cid, sync_brain=True)
    assert result["success"] is True
    assert result["conversation_id"] == cid
    assert result["from_profile"] == "src_prof"
    assert result["to_profile"] == "dst_prof"

    # Verify destination files
    dst_convo_db = dst_cli / "conversations" / f"{cid}.db"
    assert dst_convo_db.is_file()

    dst_summaries_db = dst_cli / "conversation_summaries.db"
    assert dst_summaries_db.is_file()
    dst_conn = sqlite3.connect(dst_summaries_db)
    cur = dst_conn.cursor()
    cur.execute("SELECT title, workspace_uris FROM conversation_summaries WHERE conversation_id = ?;", (cid,))
    row = cur.fetchone()
    dst_conn.close()
    assert row is not None
    assert row[0] == "Test Conversation Title"
    assert row[1] == "/workspace/test"

    # Verify brain sync
    dst_brain_file = dst_cli / "brain" / cid / "artifact.md"
    assert dst_brain_file.is_file()
    assert dst_brain_file.read_text(encoding="utf-8") == "# Hello Relay"


def test_find_best_relay_candidate(tmp_path):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("src", "src@example.com", custom_id="1")
    mgr.add_profile("target1", "target1@example.com", custom_id="2")

    # target1 not logged in yet -> should be None
    assert mgr.find_best_relay_candidate("src") is None


def test_manager_config(tmp_path):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    cfg = mgr.get_config()
    assert cfg.get("min_buffer_pct") == 0.0

    updated = mgr.update_config(min_buffer_pct=5.0)
    assert updated.get("min_buffer_pct") == 5.0
    assert mgr.get_config().get("min_buffer_pct") == 5.0

    # Whitelist checks
    with pytest.raises(ValueError, match="Unknown or unauthorized configuration key"):
        mgr.update_config(malicious_key="injected_value")

    with pytest.raises(ValueError, match="min_buffer_pct must be between"):
        mgr.update_config(min_buffer_pct=150.0)

    with pytest.raises(ValueError, match="on_no_target must be one of"):
        mgr.update_config(on_no_target="invalid_action")


def test_sensitive_directories_excluded_from_symlink(tmp_path):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    (mock_home / ".ssh").mkdir()
    (mock_home / ".gnupg").mkdir()
    (mock_home / ".aws").mkdir()
    (mock_home / ".gitconfig").write_text("[user]\nname=test\n")

    pdir = tmp_path / "profile_test"
    from agy_multi.utils import sync_profile_environment
    sync_profile_environment(pdir, mock_home)

    # .gitconfig should be symlinked
    assert (pdir / ".gitconfig").exists()
    assert (pdir / ".gitconfig").is_symlink()

    # Sensitive credential dirs must NOT be symlinked
    assert not (pdir / ".ssh").exists()
    assert not (pdir / ".gnupg").exists()
    assert not (pdir / ".aws").exists()


def test_registry_and_profile_permissions(tmp_path):
    import stat
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    # Check directory permissions (0700)
    base_mode = stat.S_IMODE(base_dir.stat().st_mode)
    assert base_mode == 0o700

    # Check accounts.json permissions (0600)
    reg_file = base_dir / "accounts.json"
    assert reg_file.is_file()
    reg_mode = stat.S_IMODE(reg_file.stat().st_mode)
    assert reg_mode == 0o600







def test_import_existing_token_permissions(tmp_path):
    import json
    import stat
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    host_cli = mock_home / ".gemini" / "antigravity-cli"
    host_cli.mkdir(parents=True)
    host_tok = host_cli / "antigravity-oauth-token"
    host_tok.write_text(json.dumps({
        "token": {
            "access_token": "ya29.valid",
            "refresh_token": "1//r",
            "expiry": "2099-01-01T00:00:00Z",
        },
        "email": "user@example.com",
    }), encoding="utf-8")

    base_dir = tmp_path / "profiles"
    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("coder", "user@example.com")
    assert mgr.import_existing_token("coder") is True

    target = mgr.get_token_path("coder")
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(mgr.get_profile_dir("coder").stat().st_mode) == 0o700
