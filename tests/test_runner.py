"""
Unit tests for SessionRunner, watchdog intervention, and SQLite backup safety.
"""

import os
import sys
import json
import sqlite3
import pytest

from pathlib import Path
from agy_multi.manager import ProfileManager
from agy_multi.runner import SessionRunner
from agy_multi.utils import set_terminal_pane_title



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


def test_set_terminal_pane_title_herdr_tmux_ansi(monkeypatch, capsys):
    calls = []

    def mock_run(cmd, check=False, stdout=None, stderr=None, timeout=None):
        calls.append(cmd)

    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    # 1. Herdr environment
    monkeypatch.setenv("HERDR_PANE_ID", "wC:p2")
    monkeypatch.setenv("HERDR_TAB_ID", "wC:t2")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    set_terminal_pane_title("test-title-herdr")
    assert ["/usr/bin/herdr", "pane", "rename", "wC:p2", "test-title-herdr"] in calls
    assert ["/usr/bin/herdr", "tab", "rename", "wC:t2", "test-title-herdr"] in calls

    # 2. Tmux environment
    calls.clear()
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setenv("TMUX", "1")
    monkeypatch.setenv("TMUX_PANE", "%5")
    set_terminal_pane_title("test-title-tmux")
    assert ["/usr/bin/tmux", "select-pane", "-t", "%5", "-T", "test-title-tmux"] in calls

    # 3. ANSI OSC 2 escape sequence
    calls.clear()
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    set_terminal_pane_title("test-title-ansi")
    captured = capsys.readouterr()
    assert "\033]2;test-title-ansi\007" in captured.out


def test_cmd_use_and_login_pane_title(tmp_path, monkeypatch):
    import argparse
    from agy_multi.cli import cmd_use, cmd_login

    base_dir = tmp_path / "profiles"
    mgr = ProfileManager(base_dir=base_dir, real_home=tmp_path)
    mgr.add_profile("test_user", "test@example.com", custom_id="3")

    titles_set = []
    monkeypatch.setattr("agy_multi.cli.set_terminal_pane_title", lambda t: titles_set.append(t))
    monkeypatch.setattr("agy_multi.manager.set_terminal_pane_title", lambda t: titles_set.append(t))

    # Test cmd_use
    args = argparse.Namespace(identifier="3")
    ret = cmd_use(mgr, args)
    assert ret == 0
    assert any("agy: test_user [P3]" in t for t in titles_set)

    # Test cmd_login title flow (mock login_profile returning True)
    titles_set.clear()
    monkeypatch.setattr(mgr, "login_profile", lambda ident: True)
    args_login = argparse.Namespace(identifier="test_user")
    ret_login = cmd_login(mgr, args_login)
    assert ret_login == 0
    assert "agy: test_user [P3] (authenticating)" in titles_set
    assert "agy: test_user [P3]" in titles_set


def test_supervisor_registry_and_ipc_dispatch(tmp_path, monkeypatch):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("src", "src@example.com", custom_id="1")
    mgr.add_profile("dst", "dst@example.com", custom_id="2")

    current_pid = 999888
    cid = "cid-super-test-123"

    kill_signals = []
    def mock_kill(pid, sig):
        kill_signals.append((pid, sig))
        return 0

    monkeypatch.setattr("os.kill", mock_kill)

    # Register supervisor
    mgr.register_active_supervisor(
        pid=current_pid,
        profile_name="src",
        profile_id=1,
        conversation_id=cid,
        pane_info={"herdr_pane_id": "wC:p2"}
    )

    # Find active supervisor by cid
    sup = mgr.find_active_supervisor(conversation_id=cid)
    assert sup is not None
    assert sup["pid"] == current_pid
    assert sup["profile_name"] == "src"
    assert sup["pane_info"]["herdr_pane_id"] == "wC:p2"

    # Find active supervisor by profile
    sup2 = mgr.find_active_supervisor(profile_identifier="src")
    assert sup2 is not None
    assert sup2["pid"] == current_pid

    # Dispatch relay to supervisor
    import signal
    ok = mgr.dispatch_relay_to_supervisor(
        target_pid=current_pid,
        target_profile="dst",
        conversation_id=cid
    )
    assert ok is True
    if hasattr(signal, "SIGUSR1"):
        assert (current_pid, signal.SIGUSR1) in kill_signals

    cmd_file = mgr.base_dir / f"relay_cmd_{current_pid}.json"
    assert cmd_file.is_file()
    cmd_data = json.loads(cmd_file.read_text(encoding="utf-8"))
    assert cmd_data["target_profile"] == "dst"
    assert cmd_data["conversation_id"] == cid

    # Unregister supervisor
    mgr.unregister_active_supervisor(current_pid)
    assert mgr.find_active_supervisor(conversation_id=cid) is None
    assert not cmd_file.exists()


def test_session_runner_run_loop_spawn(tmp_path, monkeypatch):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    p = mgr.add_profile("test_user", "user@example.com", custom_id="1")

    runner = SessionRunner(mgr, profile_identifier="1")

    spawned_cmds = []

    class DummyProc:
        def __init__(self, cmd, **kwargs):
            spawned_cmds.append(cmd)
        def wait(self):
            return 0

    monkeypatch.setattr("subprocess.Popen", DummyProc)
    monkeypatch.setattr(runner, "_watchdog_loop", lambda p: None)

    ret = runner._run_loop(p, ["--test-arg"])
    assert ret == 0
    assert any("--test-arg" in cmd for cmd in spawned_cmds)



