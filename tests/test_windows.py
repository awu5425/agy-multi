"""
Dedicated unit tests for Windows platform support in agy-multi.
Covers:
- Safe process liveness checking (OpenProcess / GetExitCodeProcess, avoiding os.kill(pid, 0))
- NTFS Junction creation and cross-platform link/copy fallbacks
- Windows USERPROFILE & HOME credential isolation environment
- Cross-platform file locking with msvcrt.locking
- Sentinel file IPC relay without POSIX signals
- Windows Terminal (wt.exe) command synthesis and execution
- Windows .cmd shortcut generation
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import argparse
import subprocess
from pathlib import Path
import pytest

from agy_multi.utils import (
    is_process_alive,
    interrupt_process,
    is_link_or_junction,
    create_cross_platform_link,
    build_profile_env,
    file_lock,
    launch_windows_terminal,
    set_terminal_pane_title,
    is_orca_terminal,
    find_orca_binary,
    launch_orca_terminal,
    restore_terminal,
    find_agy_binary,
)
from agy_multi.manager import ProfileManager
from agy_multi.runner import SessionRunner
from agy_multi.cli import cmd_install_helpers, cmd_wt


def test_is_process_alive_windows_native():
    """Verifies is_process_alive operates safely on Windows without terminating target."""
    # Current process MUST be alive
    current_pid = os.getpid()
    assert is_process_alive(current_pid) is True
    # If the bug where os.kill(pid, 0) was called were present, we would have terminated here!
    # Checking again proves current process was not killed
    assert is_process_alive(current_pid) is True

    # Invalid / non-existent PIDs
    assert is_process_alive(0) is False
    assert is_process_alive(-1) is False
    assert is_process_alive(99999999) is False


def test_create_cross_platform_link_junction(tmp_path):
    """Verifies NTFS Junction or link creation for directories."""
    src_dir = tmp_path / "original_dir"
    src_dir.mkdir()
    sample_file = src_dir / "test.txt"
    sample_file.write_text("hello junction", encoding="utf-8")

    dst_dir = tmp_path / "linked_dir"

    ok = create_cross_platform_link(src_dir, dst_dir)
    assert ok is True
    assert dst_dir.exists()
    assert is_link_or_junction(dst_dir) is True

    # Verify content read through link/junction
    assert (dst_dir / "test.txt").read_text(encoding="utf-8") == "hello junction"

    # Verify write through link/junction propagates
    (dst_dir / "created_via_dst.txt").write_text("sync", encoding="utf-8")
    assert (src_dir / "created_via_dst.txt").exists()

    # Re-running on existing dst returns True without error
    assert create_cross_platform_link(src_dir, dst_dir) is True


def test_create_cross_platform_link_file(tmp_path):
    """Verifies file link or copy fallback."""
    src_file = tmp_path / "config.json"
    src_file.write_text('{"key": "value"}', encoding="utf-8")

    dst_file = tmp_path / "sub" / "target.json"
    ok = create_cross_platform_link(src_file, dst_file)
    assert ok is True
    assert dst_file.exists()
    assert dst_file.read_text(encoding="utf-8") == '{"key": "value"}'


def test_build_profile_env():
    """Verifies environment variables set USERPROFILE and HOME to ensure credential isolation."""
    profile = {
        "id": "2",
        "name": "developer",
        "email": "dev@company.com",
    }
    pdir = Path(r"C:\Profiles\dev") if sys.platform == "win32" else Path("/tmp/profiles/dev")
    rhome = Path(r"C:\Users\dev") if sys.platform == "win32" else Path("/home/dev")

    env = build_profile_env(profile, pdir, rhome)

    assert env["HOME"] == str(pdir.resolve())
    if sys.platform == "win32":
        assert env["USERPROFILE"] == str(pdir.resolve())
        assert env["SSH_CONNECTION"] == "127.0.0.1 0 127.0.0.1 0"
        assert env["SSH_CLIENT"] == "127.0.0.1 0 0"
    assert env["AGY_REAL_HOME"] == str(rhome.resolve())
    assert env["AGY_PROFILE_NAME"] == "developer"
    assert env["AGY_PROFILE_ID"] == "2"
    assert env["AGY_PROFILE_EMAIL"] == "dev@company.com"
    assert env["GEMINI_CLI_PROFILE"] == "developer"
    assert env["GEMINI_CLI_PROFILE_ID"] == "2"
    assert env["GEMINI_CLI_PROFILE_DIR"] == str(pdir.resolve())


def test_file_lock_concurrency(tmp_path):
    """Verifies file_lock prevents concurrent access with timeout."""
    lock_file = tmp_path / "test.lock"

    first_acquired = threading.Event()
    second_failed = threading.Event()
    release_first = threading.Event()

    def worker_holder():
        with file_lock(lock_file, timeout=1.0):
            first_acquired.set()
            release_first.wait(timeout=2.0)

    def worker_competitor():
        first_acquired.wait(timeout=2.0)
        try:
            with file_lock(lock_file, timeout=0.1):
                pass
        except TimeoutError:
            second_failed.set()

    t1 = threading.Thread(target=worker_holder)
    t2 = threading.Thread(target=worker_competitor)

    t1.start()
    t2.start()

    t2.join(timeout=3.0)
    release_first.set()
    t1.join(timeout=3.0)

    assert second_failed.is_set() is True


def test_sentinel_ipc_relay_dispatch(tmp_path):
    """Verifies Sentinel file IPC relay mechanism without relying on SIGUSR1."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("src", "src@gmail.com", custom_id="1")
    mgr.add_profile("tgt", "tgt@gmail.com", custom_id="2")

    runner = SessionRunner(
        manager=mgr,
        profile_identifier="src",
        extra_args=[]
    )

    # Interrupted flag
    child_interrupted = []
    runner._interrupt_child = lambda: child_interrupted.append(True)

    # Create sentinel file for current process PID
    pid = os.getpid()
    sentinel_path = base_dir / f"relay_cmd_{pid}.json"
    sentinel_payload = {
        "action": "switch",
        "target_profile": "tgt",
        "conversation_id": "conv-test-999",
        "timestamp": time.time(),
    }
    sentinel_path.write_text(json.dumps(sentinel_payload), encoding="utf-8")
    assert sentinel_path.is_file()

    # Trigger sentinel check
    picked_up = runner._check_relay_sentinel()
    assert picked_up is True
    assert runner._relay_requested is True
    assert runner._next_profile["name"] == "tgt"
    assert runner._external_relay_cid == "conv-test-999"
    assert len(child_interrupted) == 1
    # File should have been consumed
    assert not sentinel_path.exists()


def test_windows_terminal_launcher_args(monkeypatch):
    """Verifies Windows Terminal argument formatting for split panes and tabs."""
    captured_commands = []

    def mock_popen(cmd, *args, **kwargs):
        captured_commands.append(cmd)
        return None

    monkeypatch.setattr("subprocess.Popen", mock_popen)
    monkeypatch.setattr("shutil.which", lambda name: "C:\\Fake\\wt.exe" if "wt" in name else None)

    # 1. Vertical split
    monkeypatch.delenv("WT_SESSION", raising=False)
    success = launch_windows_terminal(
        command_args=["python", "-m", "agy_multi.cli", "run", "1"],
        split="v",
        new_tab=False,
        title="agy: 1",
        cwd=Path(r"C:\test")
    )
    assert success is True
    assert len(captured_commands) == 1
    cmd = captured_commands[0]
    assert cmd[0] == "C:\\Fake\\wt.exe"
    assert "split-pane" in cmd
    assert "-V" in cmd
    assert "--title" in cmd
    assert "agy: 1" in cmd
    assert cmd[-5:] == ["python", "-m", "agy_multi.cli", "run", "1"]

    # 2. Horizontal split inside existing WT_SESSION
    captured_commands.clear()
    monkeypatch.setenv("WT_SESSION", "fake-session-guid")
    success = launch_windows_terminal(
        command_args=["python", "-m", "agy_multi.cli", "run", "2"],
        split="h",
        new_tab=False,
        title="agy: 2",
        cwd=Path(r"C:\test")
    )
    assert success is True
    cmd = captured_commands[0]
    assert "-w" in cmd
    assert "0" in cmd
    assert "split-pane" in cmd
    assert "-H" in cmd

    # 3. New tab
    captured_commands.clear()
    success = launch_windows_terminal(
        command_args=["python", "-m", "agy_multi.cli", "run", "3"],
        new_tab=True,
        title="agy: 3"
    )
    assert success is True
    cmd = captured_commands[0]
    assert "new-tab" in cmd


def test_install_helpers_windows(tmp_path, monkeypatch):
    """Verifies cmd_install_helpers writes .cmd batch files on Windows."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("work", "work@gmail.com", custom_id="1")
    mgr.add_profile("personal", "personal@gmail.com", custom_id="2")

    monkeypatch.setattr(sys, "platform", "win32")

    args = argparse.Namespace()
    ret = cmd_install_helpers(mgr, args)
    assert ret == 0

    local_bin = mock_home / ".local" / "bin"
    assert (local_bin / "agy-multi").exists()
    assert (local_bin / "agy-multi.cmd").exists()
    assert (local_bin / "agy-auto.cmd").exists()
    assert (local_bin / "agy-1.cmd").exists()
    assert (local_bin / "agy-work.cmd").exists()
    assert (local_bin / "agy-2.cmd").exists()
    assert (local_bin / "agy-personal.cmd").exists()

    content = (local_bin / "agy-1.cmd").read_text(encoding="utf-8")
    assert "@echo off" in content
    assert "python -m agy_multi.cli run 1" in content


def test_cmd_wt(tmp_path, monkeypatch):
    """Verifies cmd_wt CLI command integrates with Windows Terminal."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("main", "main@gmail.com", custom_id="1")

    wt_calls = []

    def mock_launch_wt(**kwargs):
        wt_calls.append(kwargs)
        return True

    monkeypatch.setattr("agy_multi.cli.is_orca_terminal", lambda: False)
    monkeypatch.setattr("agy_multi.cli.launch_windows_terminal", mock_launch_wt)

    args = argparse.Namespace(identifier="1", split="v", tab=False)
    ret = cmd_wt(mgr, args)
    assert ret == 0
    assert len(wt_calls) == 1
    assert wt_calls[0]["split"] == "v"
    assert wt_calls[0]["new_tab"] is False
    assert "main" in wt_calls[0]["title"]


def test_cmd_tab(tmp_path, monkeypatch):
    """Verifies cmd_wt behaves as a new tab command when invoked via tab/new-tab."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("main", "main@gmail.com", custom_id="1")

    orca_calls = []

    def mock_launch_orca(**kwargs):
        orca_calls.append(kwargs)
        return True

    monkeypatch.setattr("agy_multi.cli.is_orca_terminal", lambda: True)
    monkeypatch.setattr("agy_multi.cli.launch_orca_terminal", mock_launch_orca)

    project_dir = tmp_path / "my_project"
    project_dir.mkdir()

    args = argparse.Namespace(command="tab", identifier="1", project=str(project_dir), split="v")
    ret = cmd_wt(mgr, args)
    assert ret == 0
    assert len(orca_calls) == 1
    assert orca_calls[0]["new_tab"] is True
    assert orca_calls[0]["cwd"] == project_dir
    assert "main" in orca_calls[0]["title"]


def test_set_terminal_pane_title():
    """Verifies set_terminal_pane_title executes safely on Windows."""
    set_terminal_pane_title("agy: test-title")


def test_orca_terminal_detection(monkeypatch):
    """Verifies Orca terminal environment detection."""
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.delenv("ORCA_TAB_ID", raising=False)
    monkeypatch.delenv("ORCA_PANE_KEY", raising=False)
    monkeypatch.delenv("ORCA_WORKSPACE_ID", raising=False)
    assert is_orca_terminal() is False

    monkeypatch.setenv("TERM_PROGRAM", "Orca")
    assert is_orca_terminal() is True

    monkeypatch.delenv("TERM_PROGRAM")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_123")
    assert is_orca_terminal() is True


def test_set_terminal_pane_title_orca(monkeypatch):
    """Verifies set_terminal_pane_title triggers orca terminal rename in Orca."""
    orca_calls = []

    def mock_run(cmd, *args, **kwargs):
        orca_calls.append(cmd)
        class Dummy:
            returncode = 0
            stdout = ""
        return Dummy()

    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("agy_multi.utils.find_orca_binary", lambda: "C:\\Fake\\orca.exe")
    monkeypatch.setenv("TERM_PROGRAM", "Orca")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_abc999")

    set_terminal_pane_title("agy: work [P1] • 12345678")
    assert len(orca_calls) >= 1
    rename_call = next((c for c in orca_calls if "rename" in c), None)
    assert rename_call is not None
    assert "terminal" in rename_call
    assert "--terminal" in rename_call
    assert "term_abc999" in rename_call
    assert "--title" in rename_call
    assert "agy: work [P1] • 12345678" in rename_call


def test_launch_orca_terminal(monkeypatch):
    """Verifies launch_orca_terminal formats commands for split and new-tab."""
    orca_calls = []

    def mock_run(cmd, *args, **kwargs):
        orca_calls.append(cmd)
        class Dummy:
            returncode = 0
        return Dummy()

    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("agy_multi.utils.find_orca_binary", lambda: "C:\\Fake\\orca.exe")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_split_1")

    # 1. Vertical split
    ok = launch_orca_terminal(["python", "-m", "agy_multi.cli", "run", "1"], split="v", new_tab=False)
    assert ok is True
    call = orca_calls[0]
    assert call[0] == "C:\\Fake\\orca.exe"
    assert "terminal" in call
    assert "split" in call
    assert "--direction" in call
    assert "vertical" in call

    # 2. Horizontal split
    orca_calls.clear()
    ok = launch_orca_terminal(["python", "-m", "agy_multi.cli", "run", "2"], split="h", new_tab=False)
    assert ok is True
    call = orca_calls[0]
    assert "horizontal" in call

    # 3. New tab with project worktree
    orca_calls.clear()
    fake_proj = Path("D:/Fake/Project")
    ok = launch_orca_terminal(["python", "-m", "agy_multi.cli", "run", "3"], new_tab=True, title="agy: 3", cwd=fake_proj)
    assert ok is True
    call = orca_calls[0]
    assert "create" in call
    assert "--title" in call
    assert "agy: 3" in call
    assert "--worktree" in call
    assert f"path:{fake_proj}" in call

    # 4. New tab fallback to active worktree if path selector fails
    orca_calls.clear()
    def mock_run_fail_then_succeed(cmd, *args, **kwargs):
        orca_calls.append(cmd)
        class Dummy:
            returncode = 1 if f"path:{fake_proj}" in cmd else 0
        return Dummy()

    monkeypatch.setattr("subprocess.run", mock_run_fail_then_succeed)
    ok = launch_orca_terminal(["python", "-m", "agy_multi.cli", "run", "3"], new_tab=True, title="agy: 3", cwd=fake_proj)
    assert ok is True
    assert len(orca_calls) == 2
    assert "active" in orca_calls[1]


def test_dispatch_relay_to_multiplexer_orca(tmp_path, monkeypatch):
    """Verifies dispatch_relay_to_multiplexer interacts with Orca terminal list and send."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("src", "src@gmail.com", custom_id="1")
    mgr.add_profile("tgt", "tgt@gmail.com", custom_id="2")

    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("agy_multi.manager.find_orca_binary", lambda: "C:\\Fake\\orca.exe")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_active")

    sent_commands = []

    def mock_run(cmd, *args, **kwargs):
        sent_commands.append(cmd)
        class Dummy:
            returncode = 0
            stdout = json.dumps({
                "ok": True,
                "result": {
                    "terminals": [
                        {
                            "handle": "term_active",
                            "connected": True,
                            "title": "agy: src [P1] • 11223344",
                            "worktreePath": str(tmp_path)
                        }
                    ]
                }
            })
        return Dummy()

    monkeypatch.setattr("subprocess.run", mock_run)

    res = mgr.dispatch_relay_to_multiplexer(
        from_identifier="1",
        target_profile="2",
        conversation_id="11223344-5566-7788-9900-aabbccddeeff"
    )

    assert res is not None
    assert res["multiplexer"] == "orca"
    assert res["pane_id"] == "term_active"


def test_get_profile_active_pids_windows_supervisor(tmp_path, monkeypatch):
    """Verifies get_profile_active_pids detects active sessions via active_supervisors.json on Windows."""
    from agy_multi.utils import get_profile_active_pids

    base_dir = tmp_path / "profiles"
    pdir = base_dir / "my_profile"
    pdir.mkdir(parents=True, exist_ok=True)

    curr_pid = os.getpid()

    sup_file = base_dir / "active_supervisors.json"
    sup_file.write_text(json.dumps({
        str(curr_pid): {
            "pid": curr_pid,
            "profile_name": "my_profile",
            "profile_id": "1",
            "started_at": time.time()
        },
        "999999": {
            "pid": 999999,
            "profile_name": "my_profile",
            "profile_id": "1",
            "started_at": time.time()
        }
    }), encoding="utf-8")

    monkeypatch.setattr("agy_multi.utils.is_process_alive", lambda pid: pid == curr_pid)

    active_pids = get_profile_active_pids(pdir)
    assert curr_pid in active_pids
    assert 999999 not in active_pids


def test_restore_terminal(monkeypatch):
    """Verifies restore_terminal outputs ANSI reset sequences and invokes Win32 SetConsoleMode."""
    written_data = []

    class DummyStdout:
        def write(self, s):
            written_data.append(s)
        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", DummyStdout())

    set_modes = []
    if sys.platform == "win32":
        try:
            import ctypes
            monkeypatch.setattr(ctypes.windll.kernel32, "SetConsoleMode", lambda handle, mode: set_modes.append(mode) or 1)
        except Exception:
            pass

    restore_terminal()

    assert any("\033[?1049l" in s for s in written_data)
    assert any("\033[?25h" in s for s in written_data)
    assert any("\033[?2004l" in s for s in written_data)

    if sys.platform == "win32" and set_modes:
        assert 0x01F7 in set_modes
        assert 0x0007 in set_modes


def test_find_agy_binary_prefers_exe(tmp_path, monkeypatch):
    """Verifies find_agy_binary resolves to .exe on Windows and avoids .cmd wrappers."""
    fake_exe = tmp_path / "agy.exe"
    fake_cmd = tmp_path / "agy.cmd"
    fake_exe.write_text("binary", encoding="utf-8")
    fake_cmd.write_text("script", encoding="utf-8")

    monkeypatch.setattr(shutil, "which", lambda name: str(fake_exe) if "exe" in name else str(fake_cmd))

    bin_path = find_agy_binary(real_home=tmp_path)
    if sys.platform == "win32":
        assert bin_path.endswith(".exe")
    else:
        assert bin_path is not None


def test_session_runner_ctrl_c_exit(tmp_path, monkeypatch):
    """Verifies SessionRunner exits cleanly with 130 on user interrupt (Ctrl+C) without 429 quota loop."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    mgr.add_profile("main", "main@gmail.com", custom_id="1")

    runner = SessionRunner(manager=mgr, profile_identifier="1")

    class DummyProc:
        pid = 12345
        def wait(self):
            # Simulate Windows STATUS_CONTROL_C_EXIT
            return 3221225786
        def poll(self):
            return 3221225786

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: DummyProc())
    terminal_restored = False
    monkeypatch.setattr("agy_multi.runner.restore_terminal", lambda: None)

    exit_code = runner.run()
    assert exit_code == 130
    assert runner._relay_requested is False
    assert runner._next_profile is None


def test_dash_cli_alias(tmp_path, monkeypatch):
    """Verifies 'agy-multi dash' is recognized as a valid CLI subcommand alias."""
    from agy_multi.cli import main

    monkeypatch.setattr(sys, "argv", ["agy-multi", "dash", "--json"])
    called = []
    monkeypatch.setattr("agy_multi.cli.cmd_usage", lambda mgr, args: called.append(True) or 0)
    monkeypatch.setattr(sys, "exit", lambda code: called.append(code))

    main()
    assert called == [True, 0]



