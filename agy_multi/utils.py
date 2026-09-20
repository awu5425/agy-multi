"""
agy_multi.utils
Utility functions for token inspection, symlink synchronization, and process detection.
"""

import os
import sys
import json
import base64
import time
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


def inspect_token_file(token_file: Path) -> Dict[str, Any]:
    """Inspects an antigravity-oauth-token file and returns its auth details."""
    if not token_file.is_file():
        return {
            "status": "NOT_LOGGED_IN",
            "email": None,
            "expiry": None,
            "is_valid": False,
        }

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

        return {
            "status": "VALID" if (email or has_refresh) else "INCOMPLETE",
            "email": email,
            "expiry": expiry,
            "has_refresh_token": has_refresh,
            "is_valid": bool(email or has_refresh),
        }
    except Exception as e:
        return {
            "status": f"ERROR: {e}",
            "email": None,
            "expiry": None,
            "is_valid": False,
        }


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
