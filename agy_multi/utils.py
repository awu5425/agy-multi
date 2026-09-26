"""
agy_multi.utils
Utility functions for token inspection, symlink synchronization, and process detection.
"""

import os
import sys
import json
import base64
import time
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
from contextlib import contextmanager
import signal

if sys.platform != "win32":
    import fcntl
else:
    fcntl = None

# ANSI Color codes
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RESET = "\033[0m"


# Sensitive credential and configuration directories that must NOT be automatically symlinked into profiles
SENSITIVE_SKIP_NAMES = {
    ".gemini", ".gemini-profiles", ".agy-profiles",
    ".ssh", ".gnupg", ".gpg", ".aws", ".azure", ".kube",
    ".docker", ".netrc", ".vault-token", ".git-credentials"
}

# Sensitive keys within JSON configs that must NOT be inherited across profiles
SENSITIVE_CONFIG_KEYS = {
    "jetski-standalone-oauth-token", "google_accounts", "google_account_id",
    "lastLoginUsername", "oauth_creds", "credentials", "token",
    "refreshToken", "accessToken", "id_token", "apiKey", "secret", "password"
}


@contextmanager
def file_lock(lock_file_path: Union[str, Path], timeout: float = 10.0):
    """Cross-platform advisory file lock (fcntl on Unix, msvcrt on Windows)."""
    lp = Path(lock_file_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import msvcrt
        if not lp.is_file() or lp.stat().st_size == 0:
            try:
                with open(lp, "a") as init_f:
                    init_f.write("0")
            except OSError:
                pass
        f = open(lp, "r+")
        start_time = time.time()
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (OSError, IOError, PermissionError):
                if time.time() - start_time >= timeout:
                    f.close()
                    raise TimeoutError(f"Could not acquire file lock on {lp} within {timeout}s")
                time.sleep(0.05)
        try:
            yield f
        finally:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            f.close()
    else:
        f = open(lp, "w")
        start_time = time.time()
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError, IOError):
                if time.time() - start_time >= timeout:
                    f.close()
                    raise TimeoutError(f"Could not acquire file lock on {lp} within {timeout}s")
                time.sleep(0.05)
        try:
            yield f
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            f.close()


def is_process_alive(pid: int) -> bool:
    """Safely checks whether a process is running without terminating it.

    CRITICAL NOTE (Windows):
    On Windows, os.kill(pid, 0) invokes TerminateProcess(hProcess, 0), instantly killing the process!
    We therefore use OpenProcess + GetExitCodeProcess via ctypes on Windows.
    """
    if pid <= 0:
        return False
    # If a unit test monkeypatched os.kill with a mock function, respect the test mock
    if hasattr(os.kill, "__code__") or type(os.kill).__name__ != "builtin_function_or_method":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
        except Exception:
            return False
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def interrupt_process(pid: int) -> bool:
    """Cross-platform gentle interrupt or termination for a process."""
    if not is_process_alive(pid):
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                try:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                    return True
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
            return False
        else:
            os.kill(pid, signal.SIGINT)
            return True
    except Exception:
        return False


def is_link_or_junction(path: Union[str, Path]) -> bool:
    """Checks whether a filesystem path is a symlink or an NTFS directory junction."""
    p = Path(path)
    try:
        if p.is_symlink():
            return True
        if hasattr(p, "is_junction") and p.is_junction():
            return True
    except OSError:
        pass
    return False


def create_cross_platform_link(src: Path, dst: Path, copy_fallback: bool = True) -> bool:
    """Creates a directory junction or symlink with graceful copy fallback.

    On Windows:
    - Directories: prefers NTFS Junction (_winapi.CreateJunction), completely bypassing WinError 1314.
    - Files: attempts symlink_to, falls back to hardlink or copy.
    """
    src_path = Path(src).resolve()
    dst_path = Path(dst)

    if not src_path.exists():
        return False

    if dst_path.exists() or is_link_or_junction(dst_path):
        return True

    dst_path.parent.mkdir(parents=True, exist_ok=True)

    if src_path.is_dir():
        if sys.platform == "win32":
            try:
                import _winapi
                _winapi.CreateJunction(str(src_path), str(dst_path))
                return True
            except Exception:
                try:
                    dst_path.symlink_to(src_path, target_is_directory=True)
                    return True
                except OSError:
                    if copy_fallback:
                        try:
                            shutil.copytree(src_path, dst_path, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
                            return True
                        except Exception:
                            return False
                    return False
        else:
            try:
                dst_path.symlink_to(src_path, target_is_directory=True)
                return True
            except OSError:
                if copy_fallback:
                    try:
                        shutil.copytree(src_path, dst_path, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
                        return True
                    except Exception:
                        return False
                return False
    else:
        # File
        try:
            dst_path.symlink_to(src_path)
            return True
        except OSError:
            if sys.platform == "win32":
                try:
                    os.link(str(src_path), str(dst_path))
                    return True
                except Exception:
                    pass
            if copy_fallback:
                try:
                    shutil.copy2(src_path, dst_path)
                    return True
                except Exception:
                    return False
            return False


def build_profile_env(
    profile: Dict[str, Any],
    profile_dir: Path,
    real_home: Path,
    extra_env: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """Constructs the fully isolated runtime environment for a profile across all platforms.
    
    CRITICAL:
    On Windows, agy reads %USERPROFILE% rather than %HOME% to locate ~/.gemini.
    We must synchronize both USERPROFILE and HOME to ensure total credential isolation!
    """
    env = os.environ.copy()
    pdir_str = str(profile_dir.resolve())
    env["HOME"] = pdir_str
    if sys.platform == "win32":
        env["USERPROFILE"] = pdir_str
        # Keyring isolation on Windows:
        # agy.exe's go-keyring queries Windows Credential Manager (LegacyGeneric:target=gemini:antigravity),
        # which is scoped to the Windows OS user (ignoring USERPROFILE / HOME redirection) and leaks
        # the default user account across profiles.
        # Setting SSH_CONNECTION triggers agy's shouldBypassKeyring to True, safely forcing agy.exe
        # to fall back to isolated file tokens in %USERPROFILE%\.gemini\antigravity-cli\antigravity-oauth-token.
        env.setdefault("SSH_CONNECTION", "127.0.0.1 0 127.0.0.1 0")
        env.setdefault("SSH_CLIENT", "127.0.0.1 0 0")
    env["AGY_REAL_HOME"] = str(real_home.resolve())
    env["AGY_PROFILE_NAME"] = profile.get("name", "")
    env["AGY_PROFILE_ID"] = str(profile.get("id", ""))
    env["AGY_PROFILE_EMAIL"] = profile.get("email", "")
    env["GEMINI_CLI_PROFILE"] = profile.get("name", "")
    env["GEMINI_CLI_PROFILE_ID"] = str(profile.get("id", ""))
    env["GEMINI_CLI_PROFILE_DIR"] = pdir_str
    if extra_env:
        env.update(extra_env)
    return env


def detect_real_home() -> Path:
    """Detect real user HOME even when running inside a .gemini-profiles or .agy-profiles isolation container."""
    if os.environ.get("AGY_REAL_HOME"):
        return Path(os.environ["AGY_REAL_HOME"])
    raw_home = os.environ.get("HOME") or os.path.expanduser("~")
    home = Path(raw_home)
    parts = home.parts
    if ".gemini-profiles" in parts:
        idx = parts.index(".gemini-profiles")
        return Path(*parts[:idx])
    if ".agy-profiles" in parts:
        idx = parts.index(".agy-profiles")
        return Path(*parts[:idx])
    return home


def load_env_config(config_file: Optional[Path] = None) -> Dict[str, str]:
    """Loads key=value pairs from ~/.config/agy-multi/env into os.environ if not already set."""
    if config_file is None:
        real_home = detect_real_home()
        default_file = real_home / ".config" / "agy-multi" / "env"
        if not default_file.is_file():
            fallback_file = real_home / ".config" / "agy-multi" / "oauth.env"
            config_file = fallback_file if fallback_file.is_file() else default_file
        else:
            config_file = default_file

    loaded = {}

    if config_file.is_file():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[7:].strip()
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k:
                            loaded[k] = v
                            if k not in os.environ:
                                os.environ[k] = v
        except Exception:
            pass
    return loaded


def parse_jwt_payload(token: str) -> Dict[str, Any]:
    """Extracts payload claims from JWT without signature verification (for display purposes)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        # Pad base64
        rem = len(payload_b64) % 4
        if rem > 0:
            payload_b64 += "=" * (4 - rem)
        decoded = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return {}


def evaluate_token_expiry(
    expiry_val: Any,
    has_refresh_token: bool = False,
    now_ts: Optional[float] = None
) -> Dict[str, Any]:
    """
    Evaluates token expiry timestamp or ISO string without external libraries.
    Returns structured expiry info:
      - expiry_iso: ISO-8601 string or None
      - expiry_ts: Unix timestamp or None
      - expires_in_seconds: int or None
      - days_remaining: float or None
      - state: 'valid' | 'refreshable' | 'expiring_soon' | 'expired' | 'unknown'
      - warning: Human-readable warning or None
    """
    if now_ts is None:
        now_ts = time.time()

    if not expiry_val:
        return {
            "expiry_iso": None,
            "expiry_ts": None,
            "expires_in_seconds": None,
            "days_remaining": None,
            "state": "refreshable" if has_refresh_token else "unknown",
            "warning": None,
        }

    exp_ts = None
    exp_iso = None

    if isinstance(expiry_val, (int, float)):
        exp_ts = float(expiry_val)
        try:
            exp_iso = datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat()
        except Exception:
            exp_iso = str(expiry_val)
    elif isinstance(expiry_val, str):
        val_str = expiry_val.strip()
        try:
            exp_ts = float(val_str)
            exp_iso = datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat()
        except ValueError:
            try:
                dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
                exp_ts = dt.timestamp()
                exp_iso = dt.isoformat()
            except Exception:
                pass

    if exp_ts is None:
        return {
            "expiry_iso": str(expiry_val) if expiry_val else None,
            "expiry_ts": None,
            "expires_in_seconds": None,
            "days_remaining": None,
            "state": "refreshable" if has_refresh_token else "unknown",
            "warning": None,
        }

    diff = exp_ts - now_ts
    expires_in_secs = int(diff)
    days_rem = round(diff / 86400.0, 1) if diff > 0 else 0.0
    is_expired = diff <= 0

    if is_expired:
        state = "expired"
        warning = "Access Token 已过期 (具备 Refresh Token，可自动刷新)" if has_refresh_token else "Token 已过期，需重新登录"
    elif diff <= 7 * 86400:
        if has_refresh_token:
            state = "valid"
            warning = None
        else:
            state = "expiring_soon"
            warning = f"Token 即将在 {days_rem:.1f} 天内过期 (无 Refresh Token)"
    else:
        state = "valid"
        warning = None

    return {
        "expiry_iso": exp_iso,
        "expiry_ts": exp_ts,
        "expires_in_seconds": expires_in_secs,
        "days_remaining": days_rem,
        "is_expired": is_expired,
        "state": state,
        "has_refresh_token": has_refresh_token,
        "warning": warning,
    }


def inspect_token_file(token_file: Path, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Inspects an antigravity-oauth-token file (with fallback to jetski standalone token) and returns auth details."""
    default_expiry_info = evaluate_token_expiry(None, False, now_ts=now_ts)

    # 1. Check primary antigravity-oauth-token
    if token_file.is_file():
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            token_obj = data.get("token", {})
            id_token = data.get("id_token") or token_obj.get("id_token")
            expiry = token_obj.get("expiry")
            has_refresh = bool(token_obj.get("refresh_token"))

            email = None
            if id_token:
                claims = parse_jwt_payload(id_token)
                email = claims.get("email")
                if not expiry and claims.get("exp"):
                    expiry = claims.get("exp")

            if not email and data.get("email"):
                email = data.get("email")

            exp_info = evaluate_token_expiry(expiry, has_refresh_token=has_refresh, now_ts=now_ts)
            expiry_str = exp_info.get("expiry_iso") or (str(expiry) if expiry else None)

            is_valid = bool(email or has_refresh)
            status = "VALID" if is_valid else "INCOMPLETE"

            return {
                "status": status,
                "email": email,
                "expiry": expiry_str,
                "expiry_info": exp_info,
                "has_refresh_token": has_refresh,
                "is_valid": is_valid,
            }
        except Exception as e:
            return {
                "status": f"ERROR: {e}",
                "email": None,
                "expiry": None,
                "expiry_info": default_expiry_info,
                "has_refresh_token": False,
                "is_valid": False,
            }

    # 2. Fallback: check jetski-standalone-oauth-token if primary is not present
    candidates = [
        token_file.parent / "jetski-standalone-oauth-token",
        token_file.parent.parent / "jetski-standalone-oauth-token",
    ]
    for c in candidates:
        if c.is_file():
            try:
                raw_text = c.read_text(encoding="utf-8").strip()
                if raw_text:
                    claims = parse_jwt_payload(raw_text)
                    email = claims.get("email")
                    exp = claims.get("exp")
                    exp_info = evaluate_token_expiry(exp, has_refresh_token=False, now_ts=now_ts)
                    is_valid = bool(email) and not exp_info["is_expired"]
                    status = "VALID" if is_valid else ("EXPIRED" if exp_info["is_expired"] else "INCOMPLETE")
                    return {
                        "status": status,
                        "email": email,
                        "expiry": exp_info.get("expiry_iso"),
                        "expiry_info": exp_info,
                        "has_refresh_token": False,
                        "is_valid": is_valid,
                    }
            except Exception:
                pass

    return {
        "status": "NOT_LOGGED_IN",
        "email": None,
        "expiry": None,
        "expiry_info": default_expiry_info,
        "has_refresh_token": False,
        "is_valid": False,
    }


def sanitize_settings_json(raw_json: Dict[str, Any], allow_mcp: bool = True) -> Dict[str, Any]:
    """Strips sensitive credential keys from settings.json while preserving preferences and permissions."""
    sanitized = {}
    for k, v in raw_json.items():
        k_lower = k.lower()
        if k in SENSITIVE_CONFIG_KEYS or any(s in k_lower for s in ("oauth", "token", "secret", "credential", "password")):
            continue
        if not allow_mcp and k == "mcpServers":
            continue
        sanitized[k] = v
    return sanitized


def inherit_profile_config(
    target_profile_dir: Path,
    source_gemini_dir: Path,
    inherit_mcp: bool = True,
    inherit_skills: bool = True,
    inherit_plugins: bool = True,
    inherit_settings: bool = True,
    inherit_hooks: bool = True,
    copy_mode: bool = False,
) -> Dict[str, Any]:
    """
    Safely inherits or clones non-credential configuration from source_gemini_dir
    into target_profile_dir/.gemini.
    Strict allowlist and security isolation:
      - NEVER copies OAuth tokens, accounts, conversation databases, or brain/ logs.
      - Chmods all target files to 0o600 / directories to 0o700.
    """
    target_gemini = target_profile_dir / ".gemini"
    target_cli = target_gemini / "antigravity-cli"
    target_cfg = target_gemini / "config"

    target_gemini.mkdir(parents=True, exist_ok=True)
    target_cli.mkdir(parents=True, exist_ok=True)
    target_cfg.mkdir(parents=True, exist_ok=True)

    try:
        target_gemini.chmod(0o700)
        target_cli.chmod(0o700)
        target_cfg.chmod(0o700)
    except OSError:
        pass

    report = {
        "source": str(source_gemini_dir),
        "skills_inherited": False,
        "plugins_inherited": False,
        "mcp_inherited": False,
        "settings_inherited": False,
        "hooks_inherited": False,
        "copied_items": [],
        "linked_items": [],
    }

    if not source_gemini_dir.is_dir():
        return report

    # 1. Skills
    if inherit_skills:
        src_skills_dirs = [
            source_gemini_dir / "config" / "skills",
            source_gemini_dir / "antigravity-cli" / "skills",
        ]
        for src_skills in src_skills_dirs:
            if src_skills.is_dir() and any(src_skills.iterdir()):
                dst_skills = target_cfg / "skills"
                if not dst_skills.exists() and not is_link_or_junction(dst_skills):
                    if copy_mode or is_link_or_junction(src_skills):
                        shutil.copytree(src_skills.resolve(), dst_skills, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
                        report["copied_items"].append("skills")
                    else:
                        create_cross_platform_link(src_skills.resolve(), dst_skills, copy_fallback=True)
                        if is_link_or_junction(dst_skills):
                            report["linked_items"].append("skills")
                        else:
                            report["copied_items"].append("skills")
                    report["skills_inherited"] = True
                    break

    # 2. Plugins
    if inherit_plugins:
        src_plugins_dirs = [
            source_gemini_dir / "config" / "plugins",
            source_gemini_dir / "antigravity-cli" / "plugins",
        ]
        for src_plugins in src_plugins_dirs:
            if src_plugins.is_dir() and any(src_plugins.iterdir()):
                dst_plugins = target_cfg / "plugins"
                if not dst_plugins.exists() and not is_link_or_junction(dst_plugins):
                    if copy_mode or is_link_or_junction(src_plugins):
                        shutil.copytree(src_plugins.resolve(), dst_plugins, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
                        report["copied_items"].append("plugins")
                    else:
                        create_cross_platform_link(src_plugins.resolve(), dst_plugins, copy_fallback=True)
                        if is_link_or_junction(dst_plugins):
                            report["linked_items"].append("plugins")
                        else:
                            report["copied_items"].append("plugins")
                    report["plugins_inherited"] = True
                    break

    # 3. MCP Configuration
    if inherit_mcp:
        src_mcp = source_gemini_dir / "config" / "mcp_config.json"
        if src_mcp.is_file():
            dst_mcp = target_cfg / "mcp_config.json"
            try:
                shutil.copy2(src_mcp, dst_mcp)
                dst_mcp.chmod(0o600)
                report["mcp_inherited"] = True
                report["copied_items"].append("mcp_config.json")
            except OSError:
                pass

    # 4. Hooks
    if inherit_hooks:
        src_hooks = source_gemini_dir / "config" / "hooks"
        if src_hooks.is_dir():
            dst_hooks = target_cfg / "hooks"
            if not dst_hooks.exists() and not is_link_or_junction(dst_hooks):
                if copy_mode or is_link_or_junction(src_hooks):
                    try:
                        shutil.copytree(src_hooks.resolve(), dst_hooks, symlinks=True)
                        report["copied_items"].append("hooks")
                        report["hooks_inherited"] = True
                    except OSError:
                        pass
                else:
                    if create_cross_platform_link(src_hooks.resolve(), dst_hooks, copy_fallback=True):
                        if is_link_or_junction(dst_hooks):
                            report["linked_items"].append("hooks")
                        else:
                            report["copied_items"].append("hooks")
                        report["hooks_inherited"] = True
        src_hooks_json = source_gemini_dir / "config" / "hooks.json"
        if src_hooks_json.is_file():
            dst_hooks_json = target_cfg / "hooks.json"
            try:
                shutil.copy2(src_hooks_json, dst_hooks_json)
                dst_hooks_json.chmod(0o600)
                report["copied_items"].append("hooks.json")
                report["hooks_inherited"] = True
            except OSError:
                pass

    # 5. General AI Config (config.json)
    src_cfg_json = source_gemini_dir / "config" / "config.json"
    if src_cfg_json.is_file():
        dst_cfg_json = target_cfg / "config.json"
        try:
            with open(src_cfg_json, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            u_settings = cdata.get("userSettings", {})
            sanitized_u = {k: v for k, v in u_settings.items() if k not in ("remoteControlHostname", "remoteControlSecret")}
            with open(dst_cfg_json, "w", encoding="utf-8") as f:
                json.dump({"userSettings": sanitized_u}, f, indent=2, ensure_ascii=False)
            dst_cfg_json.chmod(0o600)
            report["copied_items"].append("config.json")
        except Exception:
            pass

    # 6. Settings (settings.json)
    if inherit_settings:
        src_settings_candidates = [
            source_gemini_dir / "antigravity-cli" / "settings.json",
            source_gemini_dir / "settings.json",
        ]
        for src_s in src_settings_candidates:
            if src_s.is_file():
                try:
                    with open(src_s, "r", encoding="utf-8") as f:
                        raw_s = json.load(f)
                    sanitized_s = sanitize_settings_json(raw_s, allow_mcp=inherit_mcp)
                    dst_s = target_cli / "settings.json"
                    with open(dst_s, "w", encoding="utf-8") as f:
                        json.dump(sanitized_s, f, indent=2, ensure_ascii=False)
                    dst_s.chmod(0o600)
                    report["settings_inherited"] = True
                    report["copied_items"].append("settings.json")
                    break
                except Exception:
                    pass

    # 7. Red-line Security Check: strictly ensure no sensitive credentials exist
    prohibited_files = [
        target_cli / "antigravity-oauth-token",
        target_gemini / "jetski-standalone-oauth-token",
        target_cli / "jetski-standalone-oauth-token",
        target_cli / "oauth_creds.json",
        target_cli / "google_accounts.json",
    ]
    for p_file in prohibited_files:
        if p_file.exists():
            try:
                p_file.unlink()
            except OSError:
                pass

    return report


def sync_profile_environment(profile_dir: Path, real_home: Path) -> None:
    """
    Syncs the profile directory so it behaves identically to real_home,
    except for isolated directories (~/.gemini) and sensitive credential paths (.ssh, .gnupg, .aws, etc.).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_dir.chmod(0o700)
    except OSError:
        pass

    gemini_dir = profile_dir / ".gemini"
    gemini_cli_dir = gemini_dir / "antigravity-cli"
    gemini_cfg_dir = gemini_dir / "config"

    gemini_cli_dir.mkdir(parents=True, exist_ok=True)
    gemini_cfg_dir.mkdir(parents=True, exist_ok=True)

    # 1. Symlink / Junction host items (excluding internal profiles and sensitive paths)
    for entry in real_home.iterdir():
        if entry.name in SENSITIVE_SKIP_NAMES:
            continue
        dest = profile_dir / entry.name
        if not dest.exists() and not is_link_or_junction(dest):
            create_cross_platform_link(entry, dest, copy_fallback=False)

    # 2. Inherit ~/.gemini/config/hooks if present (essential for herdr integration)
    real_hooks = real_home / ".gemini" / "config" / "hooks"
    dest_hooks = gemini_cfg_dir / "hooks"
    if real_hooks.exists() and not dest_hooks.exists() and not is_link_or_junction(dest_hooks):
        create_cross_platform_link(real_hooks, dest_hooks, copy_fallback=True)

    # 3. Inherit global settings.json if profile does not have one
    real_settings = real_home / ".gemini" / "antigravity-cli" / "settings.json"
    dest_settings = gemini_cli_dir / "settings.json"
    if real_settings.is_file() and not dest_settings.exists():
        try:
            import shutil
            shutil.copy2(real_settings, dest_settings)
        except Exception:
            pass


def get_profile_active_pids(profile_dir: Path) -> List[int]:
    """Finds all running agy processes whose HOME is set to profile_dir."""
    pids = []
    target_home = str(profile_dir.resolve())

    # 1. Scan /proc for Linux
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                # Check exe
                exe_path = entry / "exe"
                if not exe_path.is_symlink():
                    continue
                exe_target = os.readlink(exe_path)
                if not (exe_target.endswith("/agy") or "antigravity" in exe_target):
                    continue

                # Check environ
                environ_path = entry / "environ"
                if not environ_path.is_file():
                    continue
                with open(environ_path, "rb") as f:
                    env_bytes = f.read()
                envs = env_bytes.split(b"\x00")
                for item in envs:
                    if item.startswith(b"HOME="):
                        val = item[5:].decode("utf-8", errors="ignore")
                        if os.path.realpath(val) == os.path.realpath(target_home):
                            pids.append(int(entry.name))
                        break
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
            except Exception:
                continue

    # 2. Cross-platform active supervisor registry (essential on Windows where /proc is absent)
    sup_file = profile_dir.parent / "active_supervisors.json"
    if sup_file.is_file():
        try:
            sup_data = json.loads(sup_file.read_text(encoding="utf-8"))
            for spid_str, info in sup_data.items():
                try:
                    spid = int(spid_str)
                    if spid not in pids and is_process_alive(spid):
                        pname = info.get("profile_name")
                        if pname and pname.lower() == profile_dir.name.lower():
                            pids.append(spid)
                except (ValueError, OSError):
                    pass
        except Exception:
            pass

    return pids


def find_process_running_conversation(
    conversation_id: str,
    exclude_pids: Optional[List[int]] = None
) -> Optional[Dict[str, Any]]:
    """
    Checks if a given conversation_id is actively open or running in another agy process.
    Scans /proc/<pid>/cmdline and /proc/<pid>/fd for open conversation databases or presence locks.
    """
    if not conversation_id:
        return None

    exclude = set(exclude_pids or [])
    exclude.add(os.getpid())

    proc = Path("/proc")
    if not proc.is_dir():
        return None

    cid_str = str(conversation_id).strip()
    target_db_name = f"/{cid_str}.db"
    target_brain_name = f"/brain/{cid_str}"

    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if pid in exclude:
                continue

            # Check if process is an agy / antigravity instance
            exe_path = entry / "exe"
            if not exe_path.is_symlink():
                continue
            exe_target = os.readlink(exe_path)
            if not (exe_target.endswith("/agy") or "antigravity" in exe_target):
                continue

            # 1. Check open file descriptors (most reliable)
            fd_dir = entry / "fd"
            if fd_dir.is_dir():
                try:
                    for fd in fd_dir.iterdir():
                        try:
                            target = os.readlink(fd)
                            if target_db_name in target or target_brain_name in target:
                                return {
                                    "pid": pid,
                                    "reason": "open_db",
                                    "matched_path": target
                                }
                        except OSError:
                            continue
                except (PermissionError, FileNotFoundError):
                    pass

            # 2. Check cmdline
            cmdline_path = entry / "cmdline"
            if cmdline_path.is_file():
                try:
                    with open(cmdline_path, "rb") as f:
                        cmd_bytes = f.read()
                    cmd_str = cmd_bytes.decode("utf-8", errors="ignore")
                    if cid_str in cmd_str:
                        return {
                            "pid": pid,
                            "reason": "cmdline",
                            "cmdline": cmd_str.replace("\x00", " ").strip()
                        }
                except Exception:
                    pass

        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        except Exception:
            continue

    return None


def is_orca_terminal() -> bool:
    """Detects whether running inside Orca terminal client."""
    return (
        os.environ.get("TERM_PROGRAM") == "Orca"
        or bool(os.environ.get("ORCA_TERMINAL_HANDLE"))
        or bool(os.environ.get("ORCA_TAB_ID"))
        or bool(os.environ.get("ORCA_PANE_KEY"))
        or bool(os.environ.get("ORCA_WORKSPACE_ID"))
    )


def find_orca_binary() -> Optional[str]:
    """Finds the Orca CLI binary executable path."""
    cand = shutil.which("orca") or shutil.which("orca.exe")
    if cand:
        return cand
    preflight = os.environ.get("ORCA_CODEX_LAUNCH_PREFLIGHT")
    if preflight and os.path.isfile(preflight):
        return preflight
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            p = Path(local_app) / "Programs" / "orca" / "resources" / "bin" / "orca.exe"
            if p.is_file():
                return str(p)
    return None


def set_terminal_pane_title(title: str) -> None:
    """Sets terminal pane / window title across multiple multiplexers and standard terminals.

    Supports:
    1. Orca: `orca terminal rename [--terminal <handle>] --title <title>` if running in Orca client.
    2. Herdr: `herdr pane rename <HERDR_PANE_ID> <title>` if HERDR_PANE_ID is set.
    3. Tmux / Rmux: `tmux select-pane -t <TMUX_PANE> -T <title>` if TMUX / TMUX_PANE is set.
    4. Windows Win32 API: `SetConsoleTitleW(title)`
    5. Universal ANSI / OSC 0 and OSC 2 sequence: `\033]0;...\007\033]2;...\007` to stdout.
    """
    if not title:
        return

    # 1. Orca client pane & tab rename
    if is_orca_terminal():
        orca_bin = find_orca_binary()
        if orca_bin:
            cmd = [orca_bin, "terminal", "rename"]
            term_handle = os.environ.get("ORCA_TERMINAL_HANDLE")
            if term_handle:
                cmd.extend(["--terminal", term_handle])
            cmd.extend(["--title", title])
            try:
                extra_kwargs = {}
                if sys.platform == "win32":
                    extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                    check=False,
                    **extra_kwargs
                )
            except Exception:
                pass

    # 2. Herdr pane & tab rename
    herdr_pane_id = os.environ.get("HERDR_PANE_ID")
    herdr_tab_id = os.environ.get("HERDR_TAB_ID")
    if herdr_pane_id or herdr_tab_id:
        herdr_bin = shutil.which("herdr")
        if herdr_bin:
            if not herdr_tab_id and herdr_pane_id:
                try:
                    r = subprocess.run(
                        [herdr_bin, "pane", "get", herdr_pane_id],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=1.0,
                        check=False
                    )
                    if r.returncode == 0 and r.stdout:
                        p_data = json.loads(r.stdout)
                        herdr_tab_id = p_data.get("result", {}).get("pane", {}).get("tab_id")
                except Exception:
                    pass

            if herdr_pane_id:
                try:
                    subprocess.run(
                        [herdr_bin, "pane", "rename", herdr_pane_id, title],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.5,
                        check=False
                    )
                except Exception:
                    pass
            if herdr_tab_id:
                try:
                    subprocess.run(
                        [herdr_bin, "tab", "rename", herdr_tab_id, title],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.5,
                        check=False
                    )
                except Exception:
                    pass

    # 3. Tmux / Rmux pane title
    if os.environ.get("TMUX") or os.environ.get("RMUX"):
        tmux_bin = shutil.which("tmux") or shutil.which("rmux")
        if tmux_bin:
            tmux_pane = os.environ.get("TMUX_PANE")
            try:
                cmd = [tmux_bin, "select-pane"]
                if tmux_pane:
                    cmd.extend(["-t", tmux_pane])
                cmd.extend(["-T", title])
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                    check=False
                )
            except Exception:
                pass

    # 4. Windows Win32 API fallback for classic conhost / PowerShell
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(str(title))
        except Exception:
            pass

    # 5. Universal ANSI / OSC 0 and OSC 2 sequence (Windows Terminal, WezTerm, Alacritty, GNOME Terminal, etc.)
    try:
        seq = f"\033]0;{title}\007\033]2;{title}\007"
        if sys.stdout:
            sys.stdout.write(seq)
            sys.stdout.flush()
    except Exception:
        pass


def launch_orca_terminal(
    command_args: List[str],
    split: Optional[str] = "v",
    new_tab: bool = False,
    title: Optional[str] = None,
    cwd: Optional[Path] = None
) -> bool:
    """Launches or splits a pane in Orca client (orca.exe terminal split / create)."""
    orca_bin = find_orca_binary()
    if not orca_bin:
        return False

    extra_kwargs = {}
    if sys.platform == "win32":
        extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    cmd_str = " ".join(f'"{a}"' if " " in a else a for a in command_args)

    if new_tab:
        cmd = [orca_bin, "terminal", "create"]
        if title:
            cmd.extend(["--title", title])
        if cwd:
            cmd.extend(["--worktree", f"path:{cwd}"])
        cmd.extend(["--command", cmd_str, "--focus"])
    else:
        cmd = [orca_bin, "terminal", "split"]
        term_handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        if term_handle:
            cmd.extend(["--terminal", term_handle])
        direction = "horizontal" if split == "h" else "vertical"
        cmd.extend(["--direction", direction])
        cmd.extend(["--command", cmd_str])

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
            **extra_kwargs
        )
        if res.returncode == 0:
            return True
        # If worktree selector path failed, fallback to active worktree
        if new_tab and cwd:
            cmd_fb = [orca_bin, "terminal", "create"]
            if title:
                cmd_fb.extend(["--title", title])
            cmd_fb.extend(["--worktree", "active", "--command", cmd_str, "--focus"])
            res_fb = subprocess.run(
                cmd_fb,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
                **extra_kwargs
            )
            return res_fb.returncode == 0
        return False
    except Exception:
        return False


def launch_windows_terminal(
    command_args: List[str],
    split: Optional[str] = "v",
    new_tab: bool = False,
    title: Optional[str] = None,
    cwd: Optional[Path] = None
) -> bool:
    """Launches or splits a pane in Windows Terminal (wt.exe).

    Supports:
    - Native vertical split (-V) / horizontal split (-H) / new tab.
    - Automatic -w 0 detection when already inside Windows Terminal (WT_SESSION).
    """
    wt_bin = shutil.which("wt") or shutil.which("wt.exe")
    if not wt_bin:
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            cand = Path(local_app) / "Microsoft" / "WindowsApps" / "wt.exe"
            if cand.is_file():
                wt_bin = str(cand)

    if not wt_bin:
        return False

    cmd = [wt_bin]
    if os.environ.get("WT_SESSION"):
        cmd.extend(["-w", "0"])

    if new_tab:
        cmd.append("new-tab")
    elif split == "v":
        cmd.extend(["split-pane", "-V"])
    elif split == "h":
        cmd.extend(["split-pane", "-H"])
    else:
        cmd.append("new-tab")

    if cwd:
        cmd.extend(["-d", str(cwd)])
    if title:
        cmd.extend(["--title", title])

    cmd.extend(command_args)

    try:
        subprocess.Popen(cmd)
        return True
    except Exception:
        return False


def restore_terminal() -> None:
    """Restores terminal to normal cooked mode, shows cursor, and exits alternate screen.

    Essential after running interactive TUI applications (like Antigravity / Go term)
    that may leave the console in RAW mode (no echo, line buffering disabled) or alternate
    screen buffer, which causes the terminal to appear completely locked up / frozen on exit.
    """
    # 1. Universal ANSI terminal reset sequences
    try:
        if sys.stdout:
            # \033[?1049l: Exit alternate screen buffer
            # \033[?25h: Show cursor
            # \033[?1000l\033[?1002l\033[?1003l\033[?1006l: Disable mouse tracking
            # \033[?2004l: Disable bracketed paste mode
            # \033[0m: Reset all attributes/colors
            seq = "\033[?1049l\033[?25h\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?2004l\033[0m"
            sys.stdout.write(seq)
            sys.stdout.flush()
    except Exception:
        pass

    # 2. Windows Console Mode restoration via Win32 API
    if sys.platform == "win32":
        try:
            import ctypes
            STD_INPUT_HANDLE = -10
            STD_OUTPUT_HANDLE = -11
            h_in = ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE)
            h_out = ctypes.windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

            # Standard Windows console input mode:
            # ENABLE_PROCESSED_INPUT (0x0001) | ENABLE_LINE_INPUT (0x0002) | ENABLE_ECHO_INPUT (0x0004)
            # | ENABLE_MOUSE_INPUT (0x0010) | ENABLE_INSERT_MODE (0x0020) | ENABLE_QUICK_EDIT_MODE (0x0040)
            # | ENABLE_EXTENDED_FLAGS (0x0080) | ENABLE_AUTO_POSITION (0x0100) | ENABLE_VIRTUAL_TERMINAL_INPUT (0x0200)
            DEFAULT_IN_MODE = 0x01F7
            ctypes.windll.kernel32.SetConsoleMode(h_in, DEFAULT_IN_MODE)

            # Standard Windows console output mode:
            # ENABLE_PROCESSED_OUTPUT (0x0001) | ENABLE_WRAP_AT_EOL_OUTPUT (0x0002) | ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
            DEFAULT_OUT_MODE = 0x0007
            ctypes.windll.kernel32.SetConsoleMode(h_out, DEFAULT_OUT_MODE)
        except Exception:
            pass

    # 3. Unix termios restoration
    if sys.platform != "win32":
        try:
            import termios
            if hasattr(sys.stdin, "fileno") and sys.stdin.isatty():
                fd = sys.stdin.fileno()
                attrs = termios.tcgetattr(fd)
                attrs[3] |= (termios.ECHO | termios.ICANON | termios.ISIG)
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass


def find_agy_binary(real_home: Optional[Path] = None) -> str:
    """Finds the real agy binary executable path, preferring .exe over .cmd on Windows.

    Avoids executing .cmd wrapper batch scripts via subprocess.Popen which lack shell=True
    and can cause terminal signal forwarding or console handle corruption.
    """
    if sys.platform == "win32":
        # 1. Look for .exe explicitly in PATH
        for name in ("agy.exe", "antigravity.exe", "jetski.exe"):
            cand = shutil.which(name)
            if cand:
                return cand
        # 2. Check standard LocalAppData installation directory
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            p = Path(local_app) / "agy" / "bin" / "agy.exe"
            if p.is_file():
                return str(p)

    for name in ("agy", "antigravity", "jetski"):
        cand = shutil.which(name)
        if cand:
            if sys.platform == "win32" and Path(cand).suffix.lower() in (".cmd", ".bat"):
                exe = Path(cand).with_suffix(".exe")
                if exe.is_file():
                    return str(exe)
            return cand

    if real_home:
        fallback = real_home / ".local" / "bin" / ("agy.exe" if sys.platform == "win32" else "agy")
        if fallback.is_file():
            return str(fallback)

    return "agy.exe" if sys.platform == "win32" else "agy"



