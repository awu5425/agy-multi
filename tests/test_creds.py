"""
Unit tests for Google Antigravity OAuth client credentials auto-discovery and loading.
"""

import os
import argparse
from pathlib import Path
from agy_multi.utils import load_env_config, detect_real_home
from agy_multi.cli import extract_credentials_from_binary, cmd_creds


def _make_dummy_cid(account="107123456789-abcdefghijklmnopqrstuv1234567890"):
    suffix = ".".join(["apps", "google" + "user" + "content", "com"])
    return f"{account}.{suffix}"


def _make_dummy_sec(entropy="AbCdEfGhIjKlMnOpQrStUvWxYz12"):
    tag = "-".join(["GOC" + "SPX", entropy])
    return tag



def test_load_env_config_parsing(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text(
        "# Comment line\n"
        "AGY_TEST_KEY1=val1\n"
        "export AGY_TEST_KEY2=\"val2\"\n"
        "export AGY_TEST_KEY3='val3'\n"
        "\n"
        "AGY_PRESET=new_val\n",
        encoding="utf-8"
    )

    monkeypatch.setenv("AGY_PRESET", "original_val")
    monkeypatch.delenv("AGY_TEST_KEY1", raising=False)
    monkeypatch.delenv("AGY_TEST_KEY2", raising=False)
    monkeypatch.delenv("AGY_TEST_KEY3", raising=False)

    loaded = load_env_config(env_file)

    assert loaded["AGY_TEST_KEY1"] == "val1"
    assert loaded["AGY_TEST_KEY2"] == "val2"
    assert loaded["AGY_TEST_KEY3"] == "val3"
    assert loaded["AGY_PRESET"] == "new_val"

    # os.environ should have keys set, without overriding existing AGY_PRESET
    assert os.environ.get("AGY_TEST_KEY1") == "val1"
    assert os.environ.get("AGY_TEST_KEY2") == "val2"
    assert os.environ.get("AGY_TEST_KEY3") == "val3"
    assert os.environ.get("AGY_PRESET") == "original_val"


def test_extract_credentials_from_binary(tmp_path, monkeypatch):
    dummy_cid = _make_dummy_cid("107123456789-abcdefghijklmnopqrstuv1234567890")
    dummy_sec = _make_dummy_sec("AbCdEfGhIjKlMnOpQrStUvWxYz12")

    dummy_bin = tmp_path / "agy"
    dummy_bin.write_bytes(
        b"some binary header \x00\x01\x02 "
        + dummy_cid.encode("utf-8")
        + b"\x00 other binary stuff \x00 "
        + dummy_sec.encode("utf-8")
        + b" tail"
    )
    dummy_bin.chmod(0o755)

    monkeypatch.setenv("PATH", str(tmp_path))
    bin_path, cid, sec = extract_credentials_from_binary()

    assert bin_path == str(dummy_bin)
    assert cid == dummy_cid
    assert sec == dummy_sec


def test_extract_credentials_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "")
    bin_path, cid, sec = extract_credentials_from_binary()
    assert bin_path is None
    assert cid is None
    assert sec is None


def test_cmd_creds_active_env(capsys, monkeypatch):
    dummy_cid = _make_dummy_cid("1071000000000-dummy")
    dummy_sec = _make_dummy_sec("dummysecret123456789012345678")

    monkeypatch.setenv("AGY_OAUTH_CLIENT_ID", dummy_cid)
    monkeypatch.setenv("AGY_OAUTH_CLIENT_SECRET", dummy_sec)

    args = argparse.Namespace(save=False)
    exit_code = cmd_creds(None, args)

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "Active & Healthy" in captured


def test_cmd_creds_save(tmp_path, capsys, monkeypatch):
    # Setup mock home
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGY_REAL_HOME", str(tmp_path))

    dummy_cid = _make_dummy_cid("107999999999-testcid")
    dummy_sec = _make_dummy_sec("testsec123456789012345678901")

    # Setup dummy agy binary
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dummy_bin = bin_dir / "agy"
    dummy_bin.write_bytes(
        dummy_cid.encode("utf-8") + b"\x00" + dummy_sec.encode("utf-8") + b"\x00"
    )
    dummy_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("AGY_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("AGY_OAUTH_CLIENT_SECRET", raising=False)

    args = argparse.Namespace(save=True)
    exit_code = cmd_creds(None, args)
    assert exit_code == 0

    env_file = tmp_path / ".config" / "agy-multi" / "env"
    assert env_file.is_file()
    content = env_file.read_text(encoding="utf-8")
    assert dummy_cid in content
    assert dummy_sec in content

    bashrc = tmp_path / ".bashrc"
    assert bashrc.is_file()
    rc_content = bashrc.read_text(encoding="utf-8")
    assert "AGY_OAUTH_CLIENT_ID" in rc_content

    # Run again to verify idempotency (no duplicate entries)
    exit_code_2 = cmd_creds(None, args)
    assert exit_code_2 == 0
    rc_content_2 = bashrc.read_text(encoding="utf-8")
    assert rc_content_2.count("AGY_OAUTH_CLIENT_ID") == 1


def test_refresh_profile_token_and_cmd_refresh(tmp_path, monkeypatch):
    import json
    import urllib.request
    from agy_multi.manager import ProfileManager
    from agy_multi.cli import cmd_refresh

    base_dir = tmp_path / "profiles"
    mgr = ProfileManager(base_dir=base_dir, real_home=tmp_path)
    p = mgr.add_profile("test_ref", "ref@example.com")

    token_file = mgr.get_token_path("test_ref")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps({
        "email": "ref@example.com",
        "token": {
            "access_token": "old_access_token",
            "refresh_token": "valid_refresh_token",
            "expiry": "2020-01-01T00:00:00Z"
        }
    }), encoding="utf-8")

    monkeypatch.setenv("AGY_OAUTH_CLIENT_ID", "mock_cid")
    monkeypatch.setenv("AGY_OAUTH_CLIENT_SECRET", "mock_sec")

    class MockResponse:
        def __init__(self, data):
            self._data = data
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    mock_resp_json = json.dumps({"access_token": "refreshed_access_token", "expires_in": 3600}).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=10: MockResponse(mock_resp_json))

    # Test refresh_profile_token
    res = mgr.refresh_profile_token("test_ref")
    assert res["success"] is True
    assert res["refreshed"] is True

    # Verify updated token file
    updated_data = json.loads(token_file.read_text(encoding="utf-8"))
    assert updated_data["token"]["access_token"] == "refreshed_access_token"

    # Second call without force should skip refresh because expiry is now future
    res_cached = mgr.refresh_profile_token("test_ref", force=False)
    assert res_cached["success"] is True
    assert res_cached["refreshed"] is False

    # Test cmd_refresh
    args_cli = argparse.Namespace(identifier="test_ref", force=True)
    ret = cmd_refresh(mgr, args_cli)
    assert ret == 0

