"""
Unit tests for agy_multi.server security and API endpoints.
"""

import json
import urllib.error
import urllib.request
import threading
from http.server import ThreadingHTTPServer
import pytest
from agy_multi.manager import ProfileManager
from agy_multi.server import UsageDashboardHandler


@pytest.fixture
def test_server(tmp_path):
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    UsageDashboardHandler.manager = mgr
    UsageDashboardHandler.auth_token = "secret-token-123"
    UsageDashboardHandler.allow_remote = False

    # Bind to an ephemeral port on 127.0.0.1
    server = ThreadingHTTPServer(("127.0.0.1", 0), UsageDashboardHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://{host}:{port}"

    server.shutdown()
    server.server_close()


@pytest.fixture
def open_loopback_server(tmp_path):
    """Loopback bind with no server token — local convenience for GET."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    UsageDashboardHandler.manager = mgr
    UsageDashboardHandler.auth_token = None
    UsageDashboardHandler.allow_remote = False

    server = ThreadingHTTPServer(("127.0.0.1", 0), UsageDashboardHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://{host}:{port}"

    server.shutdown()
    server.server_close()


@pytest.fixture
def non_loopback_style_server(tmp_path):
    """Simulate non-loopback exposure: token required + allow_remote=True."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    base_dir = tmp_path / "profiles"

    mgr = ProfileManager(base_dir=base_dir, real_home=mock_home)
    UsageDashboardHandler.manager = mgr
    UsageDashboardHandler.auth_token = "lan-token-456"
    UsageDashboardHandler.allow_remote = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), UsageDashboardHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://{host}:{port}"

    server.shutdown()
    server.server_close()


def test_server_get_config_and_cors(test_server):
    # When a server token is configured, GET also requires auth
    req = urllib.request.Request(
        f"{test_server}/api/config",
        headers={
            "Origin": "https://evil.com",
            "Authorization": "Bearer secret-token-123",
        },
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        # Evil origin must NOT be reflected in Access-Control-Allow-Origin
        assert resp.headers.get("Access-Control-Allow-Origin") is None

    req_local = urllib.request.Request(
        f"{test_server}/api/config",
        headers={
            "Origin": "http://localhost:3000",
            "Authorization": "Bearer secret-token-123",
        },
    )
    with urllib.request.urlopen(req_local) as resp:
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


def test_server_post_auth_protection(test_server):
    # POST without token should be 401 Unauthorized
    post_data = json.dumps({"min_buffer_pct": 10.0}).encode("utf-8")
    req = urllib.request.Request(
        f"{test_server}/api/config",
        data=post_data,
        headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401

    # POST with valid Bearer token should succeed
    req_auth = urllib.request.Request(
        f"{test_server}/api/config",
        data=post_data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token-123"
        }
    )
    with urllib.request.urlopen(req_auth) as resp:
        assert resp.status == 200
        res_data = json.loads(resp.read().decode("utf-8"))
        assert res_data["success"] is True
        assert res_data["config"]["min_buffer_pct"] == 10.0


def test_get_requires_auth_when_token_configured(test_server):
    """Configured server token → GET /api/* and HTML dashboard need auth."""
    for path in ("/api/config", "/api/usage", "/", "/dashboard"):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{test_server}{path}")
        assert exc_info.value.code == 401, path

    req = urllib.request.Request(
        f"{test_server}/api/config",
        headers={"X-API-Token": "secret-token-123"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200


def test_get_open_on_loopback_without_token(open_loopback_server):
    """Loopback + no token keeps local GET convenience."""
    with urllib.request.urlopen(f"{open_loopback_server}/api/config") as resp:
        assert resp.status == 200
    with urllib.request.urlopen(f"{open_loopback_server}/") as resp:
        assert resp.status == 200


def test_non_loopback_get_without_token_401(non_loopback_style_server):
    """Non-loopback-style (allow_remote + token): GET without token → 401."""
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{non_loopback_style_server}/api/usage")
    assert exc_info.value.code == 401

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{non_loopback_style_server}/")
    assert exc_info.value.code == 401

    req = urllib.request.Request(
        f"{non_loopback_style_server}/api/usage",
        headers={"Authorization": "Bearer lan-token-456"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    # Query param ?token=... should also authenticate
    req_query = urllib.request.Request(
        f"{non_loopback_style_server}/api/usage?token=lan-token-456"
    )
    with urllib.request.urlopen(req_query) as resp:
        assert resp.status == 200

    # Cookie agy_token=... should also authenticate
    req_cookie = urllib.request.Request(
        f"{non_loopback_style_server}/api/usage",
        headers={"Cookie": "agy_token=lan-token-456; other=123"},
    )
    with urllib.request.urlopen(req_cookie) as resp:
        assert resp.status == 200


def test_server_post_relay_auto_switched(test_server, monkeypatch):
    mgr = UsageDashboardHandler.manager
    mgr.add_profile("src_acc", "src@example.com", custom_id="1")
    mgr.add_profile("dst_acc", "dst@example.com", custom_id="2")

    cid = "cid-server-auto-relay"
    src_cli = mgr.get_profile_dir("src_acc") / ".gemini" / "antigravity-cli"
    convos = src_cli / "conversations"
    convos.mkdir(parents=True, exist_ok=True)
    (convos / f"{cid}.db").touch()

    curr_pid = 777666
    kill_signals = []

    def mock_kill(pid, sig):
        kill_signals.append((pid, sig))
        return 0

    monkeypatch.setattr("os.kill", mock_kill)

    # Register active supervisor
    mgr.register_active_supervisor(
        pid=curr_pid,
        profile_name="src_acc",
        profile_id=1,
        conversation_id=cid,
        pane_info={"herdr_pane_id": "wC:p2"}
    )

    try:
        post_data = json.dumps({
            "from": "src_acc",
            "to": "dst_acc",
            "conversation_id": cid,
            "sync_brain": False
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{test_server}/api/relay",
            data=post_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer secret-token-123"
            }
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["auto_switched"] is True
            assert data["supervisor"]["pid"] == curr_pid
            assert data["supervisor"]["pane_info"]["herdr_pane_id"] == "wC:p2"
            import signal
            if hasattr(signal, "SIGUSR1"):
                assert (curr_pid, signal.SIGUSR1) in kill_signals
    finally:
        mgr.unregister_active_supervisor(curr_pid)


def test_server_post_relay_multiplexer_fallback(test_server, monkeypatch):
    """Verifies /api/relay falls back to multiplexer injection when no supervisor exists."""
    mgr = UsageDashboardHandler.manager
    mgr.add_profile("src_acc2", "src2@example.com")
    mgr.add_profile("dst_acc2", "dst2@example.com")

    cid = "fallback-cid-44556677"
    src_cli = mgr.get_profile_dir("src_acc2") / ".gemini" / "antigravity-cli"
    convos = src_cli / "conversations"
    convos.mkdir(parents=True, exist_ok=True)
    (convos / f"{cid}.db").touch()

    # Mock dispatch_relay_to_multiplexer returning injected pane info
    def mock_dispatch_mux(from_identifier, target_profile, conversation_id):
        return {
            "multiplexer": "herdr",
            "pane_id": "wC:p8",
            "command": f"agy-2 --conversation {conversation_id}",
            "title": f"agy: dst_acc2 [P2] • {conversation_id[:8]}"
        }

    monkeypatch.setattr(mgr, "dispatch_relay_to_multiplexer", mock_dispatch_mux)

    post_data = json.dumps({
        "from": "src_acc2",
        "to": "dst_acc2",
        "conversation_id": cid,
        "sync_brain": False
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{test_server}/api/relay",
        data=post_data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token-123"
        }
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["success"] is True
        assert data["auto_switched"] is True
        assert data["supervisor"]["type"] == "multiplexer"
        assert data["supervisor"]["multiplexer"] == "herdr"
        assert data["supervisor"]["pane_id"] == "wC:p8"


def test_server_relay_candidates_excludes_hidden_accounts(test_server, monkeypatch):
    """Verifies /api/relay/candidates excludes accounts marked show_on_dashboard=False."""
    mgr = UsageDashboardHandler.manager
    mgr.add_profile("board_acc", "board@example.com")
    mgr.add_profile("hidden_acc", "hidden@example.com")
    mgr.set_show_on_dashboard("hidden_acc", False)

    # Mock auth for both so they would be eligible if not hidden
    token_board = mgr.get_token_path("board_acc")
    token_board.write_text("token", encoding="utf-8")
    token_hidden = mgr.get_token_path("hidden_acc")
    token_hidden.write_text("token", encoding="utf-8")

    mock_auth = lambda p, **kwargs: {"is_valid": True, "email": "x@x.com", "expired": False}
    monkeypatch.setattr("agy_multi.manager.inspect_token_file", mock_auth)
    monkeypatch.setattr("agy_multi.usage.inspect_token_file", mock_auth)

    req = urllib.request.Request(
        f"{test_server}/api/relay/candidates",
        headers={"Authorization": "Bearer secret-token-123"}
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        candidate_names = [c["name"] for c in data.get("candidates", [])]
        assert "board_acc" in candidate_names
        assert "hidden_acc" not in candidate_names


def test_cli_server_alias(monkeypatch):
    """Verifies that 'server' and 'web' command aliases map to cmd_serve."""
    import sys
    from agy_multi.cli import main

    called = []
    monkeypatch.setattr("agy_multi.cli.cmd_serve", lambda mgr, args: called.append(args.command) or 0)

    for alias in ["serve", "server", "web"]:
        called.clear()
        monkeypatch.setattr(sys, "argv", ["agy-multi", alias])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert len(called) == 1


def test_cmd_serve_daemon_status_stop(tmp_path, monkeypatch):
    """Verifies that cmd_serve handles --daemon, --status, and --stop correctly."""
    import argparse
    from agy_multi.cli import cmd_serve

    base_dir = tmp_path / "profiles"
    mgr = ProfileManager(base_dir=base_dir, real_home=tmp_path)
    pid_file = base_dir / "dashboard_server.pid"

    # 1. Daemon launch
    class MockPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 98765
        def poll(self):
            return None

    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: MockPopen())
    monkeypatch.setattr("agy_multi.cli.is_process_alive", lambda pid: True)
    interrupted_pids = []
    monkeypatch.setattr("agy_multi.cli.interrupt_process", lambda pid: interrupted_pids.append(pid) or True)

    args_daemon = argparse.Namespace(
        daemon=True, stop=False, status=False, host="127.0.0.1", port=8989, lan=False, token=None
    )
    res = cmd_serve(mgr, args_daemon)
    assert res == 0
    assert pid_file.is_file()
    assert pid_file.read_text(encoding="utf-8").strip() == "98765"

    # 2. Status
    args_status = argparse.Namespace(
        daemon=False, stop=False, status=True, host="127.0.0.1", port=8989, lan=False, token=None
    )
    res_status = cmd_serve(mgr, args_status)
    assert res_status == 0

    # 3. Stop
    args_stop = argparse.Namespace(
        daemon=False, stop=True, status=False, host="127.0.0.1", port=8989, lan=False, token=None
    )
    res_stop = cmd_serve(mgr, args_stop)
    assert res_stop == 0
    assert not pid_file.exists()
    assert 98765 in interrupted_pids





