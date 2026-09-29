"""Regression tests for the 2026-09 security audit fixes."""
import pytest

from agy_multi.server import UsageDashboardHandler as H
from agy_multi.manager import ProfileManager


def _handler(headers, token=None):
    h = object.__new__(H)
    h.headers = headers
    h.auth_token = token
    return h


@pytest.mark.parametrize("host,ok", [
    ("localhost", True), ("127.0.0.1", True), ("::1", True),
    ("100.101.102.103", True),          # Tailscale CGNAT
    ("100.evil.com", False),            # old startswith("100.") bypass
    ("100.200.1.1", False),             # 100.x but outside 100.64/10
    ("attacker.ts.net", False),         # arbitrary Funnel host
    ("evil.com", False), (None, False),
])
def test_trusted_hostname(host, ok, monkeypatch):
    monkeypatch.delenv("AGY_MULTI_TAILNET", raising=False)
    assert H._is_trusted_hostname(host) is ok


def test_tailnet_suffix_opt_in(monkeypatch):
    monkeypatch.setenv("AGY_MULTI_TAILNET", "my-tail.ts.net")
    assert H._is_trusted_hostname("box.my-tail.ts.net")
    assert not H._is_trusted_hostname("box.other.ts.net")


def test_post_csrf_rejects_text_plain():
    assert not _handler({"Content-Type": "text/plain", "Host": "127.0.0.1:8989"})._post_csrf_ok()


def test_post_csrf_rejects_foreign_origin():
    h = _handler({"Content-Type": "application/json", "Host": "127.0.0.1:8989",
                  "Origin": "https://100.evil.com"})
    assert not h._post_csrf_ok()


def test_post_csrf_allows_same_origin_and_cli():
    same = _handler({"Content-Type": "application/json; charset=utf-8",
                     "Host": "127.0.0.1:8989", "Origin": "http://127.0.0.1:8989"})
    cli = _handler({"Content-Type": "application/json", "Host": "127.0.0.1:8989"})
    assert same._post_csrf_ok() and cli._post_csrf_ok()


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:8989", True), ("localhost:8989", True), ("192.168.1.5:8989", True),
    ("rebind.attacker.com:8989", False), ("", False),
])
def test_host_header_guard_without_token(host, ok):
    assert _handler({"Host": host})._host_header_ok() is ok


def test_host_header_guard_skipped_with_token():
    assert _handler({"Host": "anything.example"}, token="t")._host_header_ok()


@pytest.mark.parametrize("cid", ["../../../etc/passwd", "a; rm -rf ~", "$(id)", "x\ny", ""])
def test_relay_rejects_bad_conversation_id(tmp_path, cid):
    pm = ProfileManager(base_dir=tmp_path / "profiles", real_home=tmp_path)
    pm.add_profile(name="a", email="a@x", description="", custom_id="1")
    pm.add_profile(name="b", email="b@x", description="", custom_id="2")
    with pytest.raises(ValueError):
        pm.relay_conversation(from_identifier="1", to_identifier="2", conversation_id=cid or "..")


def test_cmd_init_import_existing_login(tmp_path, monkeypatch):
    import json
    import argparse
    from agy_multi.cli import cmd_init

    mock_home = tmp_path / "home"
    token_dir = mock_home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / "antigravity-oauth-token"
    token_file.write_text(json.dumps({"email": "imported_dev@google.com", "token": {"access_token": "abc"}}), encoding="utf-8")

    mgr = ProfileManager(base_dir=tmp_path / "profiles", real_home=mock_home)
    inputs = iter(["y", "d"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    args = argparse.Namespace()
    ret = cmd_init(mgr, args)
    assert ret == 0
    profiles = mgr.list_profiles()
    assert len(profiles) == 1
    assert profiles[0]["email"] == "imported_dev@google.com"
    assert profiles[0]["name"] == "main"


def test_cookie_httponly_and_redirect_strip_query_token():
    class DummyHandler:
        def __init__(self, path, token):
            self.path = path
            self.auth_token = token
            self.headers = {"Host": "127.0.0.1:8989"}
            self.responses = []
            self.header_list = []
            self.client_address = ("127.0.0.1", 12345)

        def _host_header_ok(self):
            return True

        def _requires_read_auth(self):
            return True

        def _check_auth(self):
            return True

        def send_response(self, code):
            self.responses.append(code)

        def send_header(self, k, v):
            self.header_list.append((k, v))

        def end_headers(self):
            pass

    h = DummyHandler("/dashboard?token=secret123", "secret123")
    H.do_GET(h)
    assert 302 in h.responses
    headers = dict(h.header_list)
    assert headers["Location"] == "/dashboard"
    assert "HttpOnly" in headers["Set-Cookie"]
    assert "agy_token=secret123" in headers["Set-Cookie"]


def test_secure_write_helpers(tmp_path):
    import sys
    import json
    from agy_multi.utils import secure_write_text, secure_write_json, secure_write_bytes

    f_txt = tmp_path / "test.txt"
    secure_write_text(f_txt, "hello world")
    assert f_txt.read_text(encoding="utf-8") == "hello world"

    f_json = tmp_path / "test.json"
    secure_write_json(f_json, {"key": "val"})
    assert json.loads(f_json.read_text(encoding="utf-8")) == {"key": "val"}

    f_bin = tmp_path / "test.bin"
    secure_write_bytes(f_bin, b"\x00\x01\x02")
    assert f_bin.read_bytes() == b"\x00\x01\x02"

    if sys.platform != "win32":
        assert (f_txt.stat().st_mode & 0o777) == 0o600
        assert (f_json.stat().st_mode & 0o777) == 0o600
        assert (f_bin.stat().st_mode & 0o777) == 0o600


def test_read_json_body_malformed_and_size_limit():
    import io

    h = object.__new__(H)
    h.headers = {"Content-Length": "10"}
    h.rfile = io.BytesIO(b"not a json")
    with pytest.raises(ValueError, match="Malformed JSON body"):
        h._read_json_body()

    h2 = object.__new__(H)
    h2.headers = {"Content-Length": "4"}
    h2.rfile = io.BytesIO(b"[12]")
    with pytest.raises(ValueError, match="JSON body must be an object"):
        h2._read_json_body()

    h3 = object.__new__(H)
    h3.headers = {"Content-Length": str(H.MAX_BODY_BYTES + 10)}
    h3.rfile = io.BytesIO(b"")
    with pytest.raises(ValueError, match="Request body too large"):
        h3._read_json_body()
