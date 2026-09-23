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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

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


def detect_real_home() -> Path:
    """Detect real user HOME even when running inside a .gemini-profiles isolation container."""
    if os.environ.get("AGY_REAL_HOME"):
        return Path(os.environ["AGY_REAL_HOME"]).resolve()
    home = Path(os.path.expanduser("~")).resolve()
    parts = home.parts
    if ".gemini-profiles" in parts:
        idx = parts.index(".gemini-profiles")
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
                if not dst_skills.exists():
                    if copy_mode or src_skills.is_symlink():
                        shutil.copytree(src_skills.resolve(), dst_skills, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
                        report["copied_items"].append("skills")
                    else:
                        try:
                            dst_skills.symlink_to(src_skills.resolve())
                            report["linked_items"].append("skills")
                        except OSError:
                            shutil.copytree(src_skills.resolve(), dst_skills, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
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
                if not dst_plugins.exists():
                    if copy_mode or src_plugins.is_symlink():
                        shutil.copytree(src_plugins.resolve(), dst_plugins, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
                        report["copied_items"].append("plugins")
                    else:
                        try:
                            dst_plugins.symlink_to(src_plugins.resolve())
                            report["linked_items"].append("plugins")
                        except OSError:
                            shutil.copytree(src_plugins.resolve(), dst_plugins, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
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
            if not dst_hooks.exists():
                if copy_mode or src_hooks.is_symlink():
                    try:
                        shutil.copytree(src_hooks.resolve(), dst_hooks, symlinks=True)
                        report["copied_items"].append("hooks")
                        report["hooks_inherited"] = True
                    except OSError:
                        pass
                else:
                    try:
                        dst_hooks.symlink_to(src_hooks.resolve())
                        report["linked_items"].append("hooks")
                        report["hooks_inherited"] = True
                    except OSError:
                        pass
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

    # 1. Symlink host items (excluding internal profiles and sensitive paths)
    for entry in real_home.iterdir():
        if entry.name in SENSITIVE_SKIP_NAMES:
            continue
        dest = profile_dir / entry.name
        if not dest.exists() and not dest.is_symlink():
            try:
                dest.symlink_to(entry)
            except OSError:
                pass

    # 2. Inherit ~/.gemini/config/hooks if present (essential for herdr integration)
    real_hooks = real_home / ".gemini" / "config" / "hooks"
    dest_hooks = gemini_cfg_dir / "hooks"
    if real_hooks.exists() and not dest_hooks.exists() and not dest_hooks.is_symlink():
        try:
            dest_hooks.symlink_to(real_hooks)
        except OSError:
            pass

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

    # Scan /proc for Linux
    proc = Path("/proc")
    if not proc.is_dir():
        return pids

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
