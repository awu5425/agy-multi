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
