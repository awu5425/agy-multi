"""
Unit tests for SessionRunner, watchdog intervention, and SQLite backup safety.
"""

import os
import json
import sqlite3
import pytest
from pathlib import Path
from agy_multi.manager import ProfileManager
from agy_multi.runner import SessionRunner


def test_session_runner_extract_cid():
    runner = SessionRunner(manager=None, extra_args=["--conversation", "abc-123", "--model", "pro"])
    assert runner._extract_conversation_id(runner.extra_args) == "abc-123"

    runner2 = SessionRunner(manager=None, extra_args=["-c", "xyz-789"])
    assert runner2._extract_conversation_id(runner2.extra_args) == "xyz-789"

    runner3 = SessionRunner(manager=None, extra_args=["--conversation=foo-bar"])
    assert runner3._extract_conversation_id(runner3.extra_args) == "foo-bar"

    runner4 = SessionRunner(manager=None, extra_args=["--model", "pro"])
    assert runner4._extract_conversation_id(runner4.extra_args) is None


def test_sqlite_online_backup_in_relay(tmp_path):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("src", "src@example.com", custom_id="1")
    mgr.add_profile("dst", "dst@example.com", custom_id="2")

    src_cli = mgr.get_profile_dir("src") / ".gemini" / "antigravity-cli"
    dst_cli = mgr.get_profile_dir("dst") / ".gemini" / "antigravity-cli"

    cid = "cid-sqlite-backup-test"
    src_convos = src_cli / "conversations"
    src_convos.mkdir(parents=True, exist_ok=True)
    src_convo_db = src_convos / f"{cid}.db"

    # Create dummy database and enable WAL mode to simulate active CLI writing
    conn = sqlite3.connect(src_convo_db)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, content TEXT);")
    for i in range(10):
        conn.execute("INSERT INTO steps VALUES (?, ?);", (i, f"Step content {i}"))
    conn.commit()
    # Keep connection open or close it
    conn.close()

    # Perform relay (uses SQLite Online Backup API)
    res = mgr.relay_conversation("src", "dst", cid, sync_brain=False)
    assert res["success"] is True

    # Verify destination database has all 10 steps intact
    dst_convo_db = dst_cli / "conversations" / f"{cid}.db"
    assert dst_convo_db.is_file()

    dst_conn = sqlite3.connect(f"file:{dst_convo_db.resolve()}?mode=ro", uri=True)
    cur = dst_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM steps;")
    count = cur.fetchone()[0]
    dst_conn.close()

    assert count == 10


def test_candidate_selection_require_idle(tmp_path, monkeypatch):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    p_src = mgr.add_profile("src", "src@example.com", custom_id="1")
    p_busy = mgr.add_profile("busy_acc", "busy@example.com", custom_id="2")
    p_idle = mgr.add_profile("idle_acc", "idle@example.com", custom_id="3")

    # Mock token file validity
    for name in ["busy_acc", "idle_acc"]:
        tok_file = mgr.get_token_path(name)
        tok_file.parent.mkdir(parents=True, exist_ok=True)
        tok_file.write_text(json.dumps({
            "token": {
                "access_token": "valid_token",
                "refresh_token": "valid_refresh",
                "expiry": "2099-01-01T00:00:00Z"
            }
        }), encoding="utf-8")

    # Mock get_profile_active_pids: busy_acc has [1234], idle_acc has []
    def mock_active_pids(profile_dir):
        if "busy_acc" in str(profile_dir):
            return [1234]
        return []

    monkeypatch.setattr("agy_multi.manager.get_profile_active_pids", mock_active_pids)

    # When require_idle=True, busy_acc should be skipped, idle_acc selected!
    best = mgr.get_best_relay_target("src", require_idle=True)
    assert best is not None
    assert best["name"] == "idle_acc"

    # When idle_acc is also busy, require_idle=True should return None!
    def mock_all_busy(profile_dir):
        return [9999]

    monkeypatch.setattr("agy_multi.manager.get_profile_active_pids", mock_all_busy)
    best_all_busy = mgr.get_best_relay_target("src", require_idle=True)
    assert best_all_busy is None


def test_find_process_running_conversation_and_relay_record(tmp_path):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("src", "src@example.com", custom_id="1")
    mgr.add_profile("dst", "dst@example.com", custom_id="2")

    src_cli = mgr.get_profile_dir("src") / ".gemini" / "antigravity-cli"
    cid = "cid-takeover-test-999"
    src_convos = src_cli / "conversations"
    src_convos.mkdir(parents=True, exist_ok=True)
    src_convo_db = src_convos / f"{cid}.db"
    src_convo_db.touch()

    # Perform relay and check that last_relay.json is recorded
    res = mgr.relay_conversation("src", "dst", cid, sync_brain=False)
    assert res["success"] is True

    last_relay = mgr.get_last_relay_info(cid)
    assert last_relay is not None
    assert last_relay["conversation_id"] == cid
    assert last_relay["from_profile"] == "src"
    assert last_relay["to_profile"] == "dst"
