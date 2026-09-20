"""
agy_multi.manager
ProfileManager handles lifecycle, authentication status, and execution of isolated agy profiles.
"""

import os
import sys
import json
import time
import fcntl
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from .utils import (
    sync_profile_environment,
    inspect_token_file,
    get_profile_active_pids,
    BOLD, GREEN, YELLOW, RED, CYAN, MAGENTA, RESET
)


class ProfileManager:
    ALLOWED_CONFIG_KEYS = {
        "min_buffer_pct": (int, float),
        "on_no_target": str,
        "auto_relay": bool,
    }
    VALID_ON_NO_TARGET = {"pause", "burn_buffer"}

    def __init__(self, base_dir: Optional[Path] = None, real_home: Optional[Path] = None):
        self.real_home = real_home or self._detect_real_home()
        self.base_dir = base_dir or (self.real_home / ".gemini-profiles")
        self.registry_file = self.base_dir / "accounts.json"
        self._ensure_initialized()

    @staticmethod
    def _detect_real_home() -> Path:
        if os.environ.get("AGY_REAL_HOME"):
            return Path(os.environ["AGY_REAL_HOME"]).resolve()
        home = Path(os.path.expanduser("~")).resolve()
        parts = home.parts
        if ".gemini-profiles" in parts:
            idx = parts.index(".gemini-profiles")
            return Path(*parts[:idx])
        return home

    def _ensure_initialized(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.base_dir.chmod(0o700)
        except OSError:
            pass
        if not self.registry_file.exists():
            self._save_registry({"profiles": []})

    def _load_registry(self) -> Dict[str, Any]:
        try:
            with open(self.registry_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"profiles": []}

    def _save_registry(self, data: Dict[str, Any]) -> None:
        with open(self.registry_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        try:
            self.registry_file.chmod(0o600)
        except OSError:
            pass

    def get_config(self) -> Dict[str, Any]:
        registry = self._load_registry()
        defaults = {
            "min_buffer_pct": 0.0,
            "on_no_target": "pause",
            "auto_relay": True,
        }
        cfg = registry.get("config", {})
        defaults.update(cfg)
        return defaults

    def update_config(self, **kwargs) -> Dict[str, Any]:
        registry = self._load_registry()
        cfg = registry.get("config", {})
        for k, v in kwargs.items():
            if k not in self.ALLOWED_CONFIG_KEYS:
                raise ValueError(f"Unknown or unauthorized configuration key: '{k}'")
            if v is not None:
                expected_types = self.ALLOWED_CONFIG_KEYS[k]
                if not isinstance(v, expected_types):
                    if k == "min_buffer_pct" and isinstance(v, (int, float, str)):
                        try:
                            v = float(v)
                        except (ValueError, TypeError):
                            raise ValueError(f"Invalid type for min_buffer_pct: expected numeric, got {type(v).__name__}")
                    elif k == "auto_relay" and isinstance(v, str):
                        v = v.lower() in ("true", "1", "yes")
                    else:
                        raise ValueError(f"Invalid type for '{k}': expected {expected_types}, got {type(v).__name__}")

                if k == "min_buffer_pct" and not (0.0 <= float(v) <= 100.0):
                    raise ValueError(f"min_buffer_pct must be between 0.0 and 100.0, got {v}")
                if k == "on_no_target" and v not in self.VALID_ON_NO_TARGET:
                    raise ValueError(f"on_no_target must be one of {self.VALID_ON_NO_TARGET}, got '{v}'")

                cfg[k] = v
        registry["config"] = cfg
        self._save_registry(registry)
        return self.get_config()

    def get_profile_dir(self, profile_name: str) -> Path:
        return self.base_dir / profile_name

    def get_token_path(self, profile_name: str) -> Path:
        return self.get_profile_dir(profile_name) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

    def list_profiles(self) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        results = []
        for p in registry.get("profiles", []):
            name = p.get("name")
            pdir = self.get_profile_dir(name)
            token_file = self.get_token_path(name)
            auth_info = inspect_token_file(token_file)
            pids = get_profile_active_pids(pdir)

            item = dict(p)
            item["profile_dir"] = str(pdir)
            item["auth"] = auth_info
            item["active_pids"] = pids
            results.append(item)
        return results

    def find_profile(self, identifier: str) -> Optional[Dict[str, Any]]:
        identifier = str(identifier).strip().lower()
        profiles = self.list_profiles()
        for p in profiles:
            if str(p.get("id")).lower() == identifier:
                return p
            if p.get("name", "").lower() == identifier:
                return p
            if p.get("email", "").lower() == identifier:
                return p
        return None

    def add_profile(
        self,
        name: str,
        email: str,
        description: str = "",
        custom_id: Optional[str] = None
    ) -> Dict[str, Any]:
        registry = self._load_registry()
        profiles = registry.get("profiles", [])

        # Validate unique name
        for p in profiles:
            if p.get("name") == name:
                raise ValueError(f"Profile with name '{name}' already exists.")

        # Determine ID
        if custom_id is not None:
            new_id = str(custom_id)
        else:
            existing_ids = [int(p["id"]) for p in profiles if str(p.get("id", "")).isdigit()]
            new_id = str(max(existing_ids, default=0) + 1)

        new_profile = {
            "id": new_id,
            "name": name,
            "email": email.strip(),
            "description": description.strip(),
            "created_at": datetime.now().isoformat(),
        }

        # Initialize profile directory & symlinks
        pdir = self.get_profile_dir(name)
        sync_profile_environment(pdir, self.real_home)

        profiles.append(new_profile)
        registry["profiles"] = profiles
        self._save_registry(registry)
        return new_profile

    def update_profile(
        self,
        identifier: str,
        new_name: Optional[str] = None,
        new_email: Optional[str] = None,
        new_description: Optional[str] = None,
        region_restricted: Optional[bool] = None
    ) -> Dict[str, Any]:
        registry = self._load_registry()
        profiles = registry.get("profiles", [])
        target = None
        target_idx = -1

        identifier = str(identifier).strip().lower()
        for idx, p in enumerate(profiles):
            if str(p.get("id")).lower() == identifier or p.get("name", "").lower() == identifier:
                target = p
                target_idx = idx
                break

        if target is None:
            raise ValueError(f"Profile '{identifier}' not found.")

        old_name = target["name"]

        if new_name and new_name != old_name:
            # Check unique
            for p in profiles:
                if p != target and p.get("name") == new_name:
                    raise ValueError(f"Profile with name '{new_name}' already exists.")
            old_dir = self.get_profile_dir(old_name)
            new_dir = self.get_profile_dir(new_name)
            if old_dir.exists():
                old_dir.rename(new_dir)
            target["name"] = new_name
            # re-sync environment for new dir
            sync_profile_environment(new_dir, self.real_home)

        if new_email is not None:
            target["email"] = new_email.strip()

        if new_description is not None:
            target["description"] = new_description.strip()

        if region_restricted is not None:
            target["region_restricted"] = bool(region_restricted)

        profiles[target_idx] = target
        registry["profiles"] = profiles
        self._save_registry(registry)
        return target

    def set_quota_status(self, identifier: str, weekly_exhausted: bool = True) -> Dict[str, Any]:
        """Sets the weekly quota exhausted flag for a profile."""
        registry = self._load_registry()
        profiles = registry.get("profiles", [])
        target = None
        target_idx = -1
        identifier = str(identifier).strip().lower()
        for idx, p in enumerate(profiles):
            if str(p.get("id")).lower() == identifier or p.get("name", "").lower() == identifier:
                target = p
                target_idx = idx
                break
        if target is None:
            raise ValueError(f"Profile '{identifier}' not found.")
        target["weekly_quota_exhausted"] = bool(weekly_exhausted)
        profiles[target_idx] = target
        registry["profiles"] = profiles
        self._save_registry(registry)
        return target


    def import_existing_token(self, profile_identifier: str) -> bool:
        """Imports host's active token into the specified profile if valid."""
        p = self.find_profile(profile_identifier)
        if not p:
            raise ValueError(f"Profile '{profile_identifier}' not found.")

        host_token_file = self.real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        if not host_token_file.is_file():
            return False

        host_auth = inspect_token_file(host_token_file)
        if not host_auth["is_valid"]:
            return False

        target_token_file = self.get_token_path(p["name"])
        target_token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Tighten oauth token directory tree (profile + .gemini + antigravity-cli)
            pdir = self.get_profile_dir(p["name"])
            pdir.mkdir(parents=True, exist_ok=True)
            pdir.chmod(0o700)
            for d in (target_token_file.parent, target_token_file.parent.parent):
                if d.exists():
                    d.chmod(0o700)
        except OSError:
            pass
        shutil.copy2(host_token_file, target_token_file)
        try:
            target_token_file.chmod(0o600)
        except OSError:
            pass
        return True

    def run_profile(self, identifier: str, agy_args: List[str], exec_replace: bool = True) -> int:
        """Runs agy with the given profile's isolated environment."""
        p = self.find_profile(identifier)
        if not p:
            raise ValueError(f"Profile '{identifier}' not found. Use `agy-multi list` to view profiles.")

        pdir = self.get_profile_dir(p["name"])
        # Fast sync symlinks in case new tools/configs were added to real_home
        sync_profile_environment(pdir, self.real_home)

        env = os.environ.copy()
        env["HOME"] = str(pdir)
        env["AGY_REAL_HOME"] = str(self.real_home)
        env["AGY_PROFILE_NAME"] = p["name"]
        env["AGY_PROFILE_ID"] = p["id"]
        env["AGY_PROFILE_EMAIL"] = p["email"]

        agy_binary = shutil.which("agy") or str(self.real_home / ".local" / "bin" / "agy")
        cmd = [agy_binary] + agy_args

        if exec_replace:
            # Replaces the current process (standard for interactive TUI)
            sys.stdout.flush()
            sys.stderr.flush()
            os.execvpe(agy_binary, cmd, env)
            return 0
        else:
            proc = subprocess.run(cmd, env=env)
            return proc.returncode

    def login_profile(self, identifier: str) -> bool:
        """Starts agy in interactive login mode for the specified profile."""
        p = self.find_profile(identifier)
        if not p:
            raise ValueError(f"Profile '{identifier}' not found.")

        pdir = self.get_profile_dir(p["name"])
        sync_profile_environment(pdir, self.real_home)

        token_file = self.get_token_path(p["name"])
        if token_file.exists():
            auth = inspect_token_file(token_file)
            print(f"{YELLOW}Warning: Profile '{p['name']}' already has credentials (Email: {auth.get('email')}).{RESET}")
            ans = input(f"Do you want to re-authenticate with {p['email']}? [y/N]: ").strip().lower()
            if ans != "y":
                print("Login cancelled.")
                return False
            # Remove existing token to force OAuth prompt
            token_file.unlink()

        print(f"\n{CYAN}{BOLD}=== Initiating Google OAuth Login for [{p['name']}] ==={RESET}")
        print(f"Target Account Email: {BOLD}{p['email']}{RESET}\n")
        print("A Google authorization link will be displayed below.")
        print("Please log in with the corresponding Google AI Pro account in your browser.\n")

        env = os.environ.copy()
        env["HOME"] = str(pdir)
        agy_binary = shutil.which("agy") or str(self.real_home / ".local" / "bin" / "agy")

        # Launch agy in interactive turn to trigger authentication
        cmd = [agy_binary, "-p", "ping"]
        subprocess.run(cmd, env=env)

        # Post-check token
        auth_after = inspect_token_file(token_file)
        if auth_after["is_valid"]:
            logged_email = auth_after.get("email")
            print(f"\n{GREEN}{BOLD}✓ Authentication Successful!{RESET}")
            print(f"Logged-in Email: {BOLD}{logged_email}{RESET}")
            if logged_email and p["email"] and logged_email.lower() != p["email"].lower():
                print(f"{YELLOW}Note: Logged-in email ({logged_email}) differs from registered target ({p['email']}).{RESET}")
            return True
        else:
            print(f"\n{RED}✗ Authentication was not completed or failed.{RESET}")
            return False

    def get_most_recent_conversation(self, profile_name: str) -> Optional[Dict[str, Any]]:
        """Retrieves the most recently modified conversation in the profile."""
        pdir = self.get_profile_dir(profile_name)
        cli_dir = pdir / ".gemini" / "antigravity-cli"
        summaries_db = cli_dir / "conversation_summaries.db"
        convos_dir = cli_dir / "conversations"

        # 1. Try querying conversation_summaries.db
        if summaries_db.is_file():
            try:
                conn = sqlite3.connect(f"file:{summaries_db.resolve()}?mode=ro", uri=True, timeout=5.0)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    "SELECT conversation_id, title, last_modified_time, workspace_uris FROM conversation_summaries "
                    "WHERE conversation_id IS NOT NULL AND conversation_id != '' "
                    "ORDER BY last_modified_time DESC LIMIT 1;"
                )
                row = cur.fetchone()
                conn.close()
                if row:
                    return {
                        "id": row["conversation_id"],
                        "title": row["title"] or f"Conversation {row['conversation_id'][:8]}",
                        "last_modified": row["last_modified_time"],
                        "workspace_uris": row["workspace_uris"],
                    }
            except Exception:
                pass

        # 2. Fallback: check conversations directory
        if convos_dir.is_dir():
            db_files = sorted(convos_dir.glob("*.db"), key=lambda f: f.stat().st_mtime, reverse=True)
            if db_files:
                target_file = db_files[0]
                cid = target_file.stem
                mtime_str = datetime.fromtimestamp(target_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                return {
                    "id": cid,
                    "title": f"Conversation {cid[:8]}",
                    "last_modified": mtime_str,
                    "workspace_uris": "",
                }

        return None

    def get_active_or_recent_profile(self) -> Optional[Dict[str, Any]]:
        """Finds the currently running or most recently active profile."""
        profiles = self.list_profiles()
        if not profiles:
            return None

        # 1. Check environment variable
        env_profile = os.environ.get("AGY_PROFILE_NAME")
        if env_profile:
            p = self.find_profile(env_profile)
            if p:
                return p

        # 2. Check active PIDs
        for p in profiles:
            if p.get("active_pids"):
                return p

        # 3. Check most recent conversation across all profiles
        best_p = None
        latest_mtime = ""
        for p in profiles:
            convo = self.get_most_recent_conversation(p["name"])
            if convo and str(convo.get("last_modified", "")) > latest_mtime:
                latest_mtime = str(convo["last_modified"])
                best_p = p

        return best_p or profiles[0]

    def get_best_relay_target(
        self,
        exclude_identifier: Optional[str] = None,
        min_buffer_pct: Optional[float] = None,
        require_idle: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Intelligently finds the best target profile to relay a conversation to:
        1. Must be authenticated (valid token).
        2. Must not be weekly exhausted.
        3. Excludes exclude_identifier (source profile).
        4. Excludes profiles where 5H quota <= min_buffer_pct (reserve quota threshold).
        5. If require_idle=True, excludes profiles with active_pids > 0.
        6. Prioritizes profiles with READY status and lowest active load / highest quota.
        """
        if min_buffer_pct is None:
            min_buffer_pct = float(self.get_config().get("min_buffer_pct", 0.0))

        profiles = self.list_profiles()
        exclude_p = self.find_profile(exclude_identifier) if exclude_identifier else None
        exclude_name = exclude_p["name"] if exclude_p else None

        from .usage import get_profile_usage

        candidates = []
        for p in profiles:
            if exclude_name and p["name"] == exclude_name:
                continue
            if not p.get("auth", {}).get("is_valid", False):
                continue

            # Check active PIDs if require_idle is set
            active_pids_count = len(p.get("active_pids", []))
            if require_idle and active_pids_count > 0:
                continue

            usage = get_profile_usage(p, min_buffer_pct=min_buffer_pct)
            usability = usage.get("usability", {})
            u_code = usability.get("code", "")
            is_usable = usability.get("is_usable", True)
            is_target_eligible = usability.get("is_target_eligible", True)

            if not is_usable or not is_target_eligible:
                continue

            # Check if 5H quota meets minimum reserve buffer
            oq = usage.get("official_quota", {})
            gemini_q = oq.get("groups", {}).get("gemini", {}).get("buckets", {})
            g_5h = gemini_q.get("gemini-5h")
            g_wk = gemini_q.get("gemini-weekly")
            if (g_5h and g_5h.get("disabled")) or (g_wk and g_wk.get("remainingFraction", 1.0) == 0):
                continue
            g_5h_rem = float(g_5h.get("remainingPct", 100)) if g_5h else 100.0
            if g_5h_rem <= min_buffer_pct:
                continue

            score = 1000
            if u_code == "READY":
                score += 500
            elif u_code == "COOLDOWN_5H":
                score -= 600

            # Bonus for idle profile (no active processes)
            if active_pids_count == 0:
                score += 100
            else:
                score -= (active_pids_count * 50)

            # Bonus for remaining weekly quota (heavier weight, as weekly quota is true global budget)
            if g_wk and not g_wk.get("disabled"):
                score += int(g_wk.get("remainingPct", 100)) * 2

            # Bonus for remaining 5h quota
            if g_5h and not g_5h.get("disabled"):
                score += int(g_5h.get("remainingPct", 100))

            candidates.append((score, p, usage))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        best_candidate = candidates[0][1]
        best_candidate["relay_usage"] = candidates[0][2]
        return best_candidate

    find_best_relay_candidate = get_best_relay_target

    def get_earliest_cooldown_reset(self) -> Optional[Dict[str, Any]]:
        """
        Finds the profile that will reset its 5H quota earliest in the future.
        Returns dict with profile name, id, reset_ts, and remaining_seconds.
        """
        from .usage import get_profile_usage
        profiles = self.list_profiles()
        now = time.time()
        earliest = None
        for p in profiles:
            usage = get_profile_usage(p)
            oq = usage.get("official_quota", {})
            gemini_q = oq.get("groups", {}).get("gemini", {}).get("buckets", {})
            g_5h = gemini_q.get("gemini-5h")
            if g_5h and g_5h.get("resetTs") and g_5h["resetTs"] > now:
                reset_ts = float(g_5h["resetTs"])
                diff = reset_ts - now
                if earliest is None or diff < earliest["remaining_seconds"]:
                    earliest = {
                        "name": p["name"],
                        "id": p["id"],
                        "reset_ts": reset_ts,
                        "remaining_seconds": int(diff),
                    }
        return earliest

    def relay_conversation(
        self,
        from_identifier: str,
        to_identifier: str,
        conversation_id: Optional[str] = None,
        sync_brain: bool = True
    ) -> Dict[str, Any]:
        """
        Transfers an active conversation from one profile to another:
        1. Uses SQLite Online Backup API for atomic, WAL-safe copy of conversations/<cid>.db.
        2. Syncs conversation_summaries.db row.
        3. Optionally syncs brain/<cid> directory (artifacts, scratch files).
        4. Protected by flock to prevent concurrent relay race conditions.
        """
        src_profile = self.find_profile(from_identifier)
        if not src_profile:
            raise ValueError(f"Source profile '{from_identifier}' not found.")

        dst_profile = self.find_profile(to_identifier)
        if not dst_profile:
            raise ValueError(f"Target profile '{to_identifier}' not found.")

        if src_profile["name"] == dst_profile["name"]:
            raise ValueError("Source and target profiles must be different.")

        src_cli = Path(src_profile["profile_dir"]) / ".gemini" / "antigravity-cli"
        dst_cli = Path(dst_profile["profile_dir"]) / ".gemini" / "antigravity-cli"

        # 1. Determine conversation ID
        if not conversation_id:
            recent = self.get_most_recent_conversation(src_profile["name"])
            if not recent:
                raise ValueError(f"No conversation found in source profile [{src_profile['name']}].")
            conversation_id = recent["id"]
            convo_title = recent.get("title", f"Conversation {conversation_id[:8]}")
        else:
            convo_title = f"Conversation {conversation_id[:8]}"

        src_convo_db = src_cli / "conversations" / f"{conversation_id}.db"
        if not src_convo_db.is_file():
            raise FileNotFoundError(f"Conversation database not found: {src_convo_db}")

        # 2. Ensure destination directories exist
        dst_convos_dir = dst_cli / "conversations"
        dst_convos_dir.mkdir(parents=True, exist_ok=True)
        dst_convo_db = dst_convos_dir / f"{conversation_id}.db"

        # Acquire lock to prevent parallel relays from racing
        lock_file = self.base_dir / "relay.lock"
        with open(lock_file, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                # 3. Copy conversation database safely using SQLite Backup API with fallback
                try:
                    src_conn = sqlite3.connect(f"file:{src_convo_db.resolve()}?mode=ro", uri=True, timeout=10.0)
                    dst_conn = sqlite3.connect(dst_convo_db, timeout=10.0)
                    src_conn.backup(dst_conn)
                    src_conn.close()
                    dst_conn.close()
                except Exception:
                    shutil.copy2(src_convo_db, dst_convo_db)

                # 4. Sync conversation_summaries.db metadata
                src_summaries_db = src_cli / "conversation_summaries.db"
                dst_summaries_db = dst_cli / "conversation_summaries.db"
                if src_summaries_db.is_file():
                    try:
                        src_conn = sqlite3.connect(f"file:{src_summaries_db.resolve()}?mode=ro", uri=True, timeout=5.0)
                        src_conn.row_factory = sqlite3.Row
                        src_cur = src_conn.cursor()
                        src_cur.execute("SELECT * FROM conversation_summaries WHERE conversation_id = ?;", (conversation_id,))
                        row = src_cur.fetchone()
                        src_conn.close()

                        if row:
                            row_dict = dict(row)
                            if row_dict.get("title"):
                                convo_title = row_dict["title"]

                            dst_conn = sqlite3.connect(dst_summaries_db, timeout=10.0)
                            dst_cur = dst_conn.cursor()
                            dst_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_summaries';")
                            if not dst_cur.fetchone():
                                # Create schema from source
                                src_conn2 = sqlite3.connect(f"file:{src_summaries_db.resolve()}?mode=ro", uri=True, timeout=5.0)
                                cur2 = src_conn2.cursor()
                                cur2.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_summaries';")
                                create_sql = cur2.fetchone()[0]
                                src_conn2.close()
                                dst_cur.execute(create_sql)

                            cols = list(row_dict.keys())
                            placeholders = ", ".join(["?"] * len(cols))
                            col_names = ", ".join([f"`{c}`" for c in cols])
                            values = [row_dict[c] for c in cols]
                            dst_cur.execute(f"INSERT OR REPLACE INTO conversation_summaries ({col_names}) VALUES ({placeholders});", values)
                            dst_conn.commit()
                            dst_conn.close()
                    except Exception:
                        pass

                # 5. Sync brain/<cid> artifacts and scratch files
                if sync_brain:
                    src_brain = src_cli / "brain" / conversation_id
                    dst_brain = dst_cli / "brain" / conversation_id
                    if src_brain.is_dir():
                        dst_brain.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(src_brain, dst_brain, dirs_exist_ok=True)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)

        # Record relay event in registry directory for supervisor coordination
        try:
            relay_record = {
                "conversation_id": conversation_id,
                "from_profile": src_profile["name"],
                "to_profile": dst_profile["name"],
                "timestamp": time.time(),
            }
            relay_file = self.base_dir / "last_relay.json"
            with open(relay_file, "w", encoding="utf-8") as rf:
                json.dump(relay_record, rf, ensure_ascii=False)
        except Exception:
            pass

        return {
            "success": True,
            "conversation_id": conversation_id,
            "title": convo_title,
            "from_profile": src_profile["name"],
            "from_id": src_profile["id"],
            "to_profile": dst_profile["name"],
            "to_id": dst_profile["id"],
            "resume_command": f"agy --conversation {conversation_id}",
            "shortcut_command": f"agy-{dst_profile['id']} --conversation {conversation_id}",
        }

    def get_last_relay_info(self, conversation_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Returns the most recent relay event info, optionally filtered by conversation_id."""
        relay_file = self.base_dir / "last_relay.json"
        if not relay_file.is_file():
            return None
        try:
            with open(relay_file, "r", encoding="utf-8") as rf:
                data = json.load(rf)
            if conversation_id:
                if str(data.get("conversation_id")) != str(conversation_id):
                    return None
            return data
        except Exception:
            return None

    def run_relay(
        self,
        from_identifier: Optional[str] = None,
        to_identifier: Optional[str] = None,
        conversation_id: Optional[str] = None,
        agy_args: Optional[List[str]] = None,
        exec_replace: bool = True,
        min_buffer_pct: Optional[float] = None
    ) -> int:
        """Executes conversation handover and launches agy in target profile."""
        # 1. Resolve source profile
        if from_identifier:
            src_profile = self.find_profile(from_identifier)
            if not src_profile:
                raise ValueError(f"Source profile '{from_identifier}' not found.")
        else:
            src_profile = self.get_active_or_recent_profile()
            if not src_profile:
                raise ValueError("No active or configured profile found.")

        # 2. Resolve target profile
        if to_identifier:
            dst_profile = self.find_profile(to_identifier)
            if not dst_profile:
                raise ValueError(f"Target profile '{to_identifier}' not found.")
        else:
            dst_profile = self.find_best_relay_candidate(src_profile["name"], min_buffer_pct=min_buffer_pct)
            if not dst_profile:
                raise ValueError("No eligible target profile available with ready quota.")

        # 3. Perform relay
        info = self.relay_conversation(src_profile["name"], dst_profile["name"], conversation_id)

        # 4. Launch in target profile
        args = ["--conversation", info["conversation_id"]] + (agy_args or [])
        return self.run_profile(dst_profile["name"], args, exec_replace=exec_replace)

