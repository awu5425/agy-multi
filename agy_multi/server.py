"""
agy_multi.server
Lightweight HTTP server for real-time usage dashboard and JSON API.
"""

import os
import json
import secrets
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

try:
    from .manager import ProfileManager
    from .usage import get_all_usage, get_profile_usage, render_html_dashboard, save_html_dashboard, profile_on_dashboard
    from .utils import load_env_config
except (ImportError, ValueError):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agy_multi.manager import ProfileManager
    from agy_multi.usage import get_all_usage, get_profile_usage, render_html_dashboard, save_html_dashboard, profile_on_dashboard
    from agy_multi.utils import load_env_config


class UsageDashboardHandler(BaseHTTPRequestHandler):
    manager = None
    auth_token: Optional[str] = None
    allow_remote: bool = False

    def log_message(self, format, *args):
        # Suppress noisy standard request logs
        pass

    def _get_allowed_origin(self) -> Optional[str]:
        origin = self.headers.get("Origin")
        if not origin:
            return None
        try:
            parsed = urllib.parse.urlparse(origin)
            if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
                return origin
            if parsed.hostname and (parsed.hostname.startswith("100.") or parsed.hostname.endswith(".ts.net")):
                return origin
        except Exception:
            pass
        return None

    def _send_cors_headers(self):
        origin = self._get_allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Token")

    def _check_auth(self) -> bool:
        # If no auth_token is configured, allow loopback, Tailscale, or trusted networks
        if not self.auth_token:
            client_ip = self.client_address[0]
            if not self.allow_remote or client_ip in ("127.0.0.1", "::1", "localhost"):
                return True
            # Allow Tailscale CGNAT range (100.64.0.0/10)
            if client_ip.startswith("100."):
                try:
                    import ipaddress
                    if ipaddress.ip_address(client_ip) in ipaddress.ip_network("100.64.0.0/10"):
                        return True
                except Exception:
                    pass
            return False

        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if secrets.compare_digest(token, self.auth_token):
                return True
        api_token_header = self.headers.get("X-API-Token", "").strip()
        if api_token_header and secrets.compare_digest(api_token_header, self.auth_token):
            return True

        # Check URL query parameter: ?token=...
        try:
            parsed = urllib.parse.urlparse(self.path)
            q_token = urllib.parse.parse_qs(parsed.query).get("token", [None])[0]
            if q_token and secrets.compare_digest(q_token, self.auth_token):
                return True
        except Exception:
            pass

        # Check Cookie: agy_token=...
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            for part in cookie_header.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    if k == "agy_token" and secrets.compare_digest(v, self.auth_token):
                        return True
        return False

    def _requires_read_auth(self) -> bool:
        """GET/HTML also require auth when non-loopback or a server token is configured."""
        return bool(self.auth_token) or bool(self.allow_remote)

    def _send_unauthorized(self):
        err_bytes = json.dumps(
            {"success": False, "error": "Unauthorized: Valid API token required"},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(err_bytes)))
        self.end_headers()
        self.wfile.write(err_bytes)

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self._requires_read_auth() and not self._check_auth():
            self._send_unauthorized()
            return

        # If token was supplied via query string, set cookie for subsequent fetch calls
        set_cookie_header = None
        if self.auth_token:
            try:
                parsed = urllib.parse.urlparse(self.path)
                q_token = urllib.parse.parse_qs(parsed.query).get("token", [None])[0]
                if q_token and secrets.compare_digest(q_token, self.auth_token):
                    set_cookie_header = f"agy_token={q_token}; Path=/; SameSite=Lax"
            except Exception:
                pass

        clean_path = self.path.split("?", 1)[0]
        if clean_path in ("/", "/index.html", "/dashboard"):
            try:
                data = get_all_usage(self.manager)
                html = render_html_dashboard(data)
                encoded = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                if set_cookie_header:
                    self.send_header("Set-Cookie", set_cookie_header)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                self.send_error(500, f"Error generating dashboard: {e}")

        elif self.path.startswith("/api/usage"):
            try:
                data = get_all_usage(self.manager)
                encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                self.send_error(500, f"Error fetching usage data: {e}")

        elif self.path.startswith("/api/relay/candidates"):
            try:
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                from_id = params.get("from", [None])[0]

                profiles = self.manager.list_profiles()
                candidates = []
                for p in profiles:
                    if from_id and (p["name"] == from_id or str(p["id"]) == str(from_id)):
                        continue
                    if not profile_on_dashboard(p):
                        continue
                    if not p.get("auth", {}).get("is_valid", False):
                        continue
                    u = get_profile_usage(p)
                    candidates.append({
                        "id": p["id"],
                        "name": p["name"],
                        "email": p["email"],
                        "usability": u.get("usability", {}),
                        "official_quota": u.get("official_quota", {}),
                        "active_pids": p.get("active_pids", []),
                    })
                best = self.manager.find_best_relay_candidate(from_id, require_visible_on_dashboard=True)
                res = {
                    "candidates": candidates,
                    "recommended": best["name"] if best else None,
                    "recommended_id": best["id"] if best else None
                }
                encoded = json.dumps(res, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                self.send_error(500, f"Error getting relay candidates: {e}")

        elif self.path == "/api/config":
            try:
                cfg = self.manager.get_config()
                encoded = json.dumps({"success": True, "config": cfg}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                self.send_error(500, f"Error getting config: {e}")
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if not self._check_auth():
            self._send_unauthorized()
            return

        if self.path == "/api/relay":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))

                from_id = payload.get("from")
                to_id = payload.get("to")
                cid = payload.get("conversation_id")
                sync_brain = payload.get("sync_brain", True)

                if not to_id:
                    best = self.manager.find_best_relay_candidate(from_id, require_visible_on_dashboard=True)
                    if not best:
                        raise ValueError("No eligible target profile available with ready quota.")
                    to_id = best["name"]

                result = self.manager.relay_conversation(
                    from_identifier=from_id,
                    to_identifier=to_id,
                    conversation_id=cid,
                    sync_brain=sync_brain
                )

                # Check if there is an active SessionRunner supervisor running this session or profile
                relayed_cid = result.get("conversation_id")
                target_p_name = result.get("to_profile")
                auto_switched = False
                supervisor_info = None

                active_sup = self.manager.find_active_supervisor(
                    profile_identifier=from_id,
                    conversation_id=relayed_cid
                )
                if active_sup:
                    sup_pid = active_sup.get("pid")
                    if sup_pid and self.manager.dispatch_relay_to_supervisor(
                        target_pid=sup_pid,
                        target_profile=target_p_name,
                        conversation_id=relayed_cid
                    ):
                        auto_switched = True
                        supervisor_info = active_sup

                # Tier 2 Fallback: Multiplexer Terminal Injection
                if not auto_switched:
                    mux_res = self.manager.dispatch_relay_to_multiplexer(
                        from_identifier=from_id,
                        target_profile=target_p_name,
                        conversation_id=relayed_cid
                    )
                    if mux_res:
                        auto_switched = True
                        supervisor_info = {
                            "type": "multiplexer",
                            "multiplexer": mux_res.get("multiplexer"),
                            "pane_id": mux_res.get("pane_id"),
                            "command": mux_res.get("command"),
                            "title": mux_res.get("title"),
                        }

                res_payload = {
                    "success": True,
                    "result": result,
                    "auto_switched": auto_switched,
                    "supervisor": supervisor_info
                }
                encoded = json.dumps(res_payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                err_bytes = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

        elif self.path == "/api/account-visibility":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                identifier = payload.get("name") or payload.get("id")
                if identifier is None or "show_on_dashboard" not in payload:
                    raise ValueError("name and show_on_dashboard are required")
                updated = self.manager.set_show_on_dashboard(identifier, bool(payload.get("show_on_dashboard")))
                encoded = json.dumps(
                    {"success": True, "name": updated.get("name"), "show_on_dashboard": bool(updated.get("show_on_dashboard"))},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                err_bytes = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

        elif self.path == "/api/config":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                new_cfg = self.manager.update_config(**payload)
                encoded = json.dumps({"success": True, "config": new_cfg}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                err_bytes = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)
        else:
            self.send_error(404, "Not Found")


def start_server(
    host: Optional[str] = None,
    port: Optional[int] = None,
    token: Optional[str] = None
):
    load_env_config()
    mgr = ProfileManager()
    UsageDashboardHandler.manager = mgr

    if host is None:
        host = os.environ.get("AGY_MULTI_SERVER_HOST", "127.0.0.1")
    if port is None:
        try:
            port = int(os.environ.get("AGY_MULTI_SERVER_PORT", "8989"))
        except (ValueError, TypeError):
            port = 8989

    is_loopback = host in ("127.0.0.1", "localhost", "::1")
    allow_no_auth = os.environ.get("AGY_MULTI_SERVER_NO_AUTH", "").lower() in ("true", "1", "yes")

    UsageDashboardHandler.allow_remote = (not is_loopback) and (not allow_no_auth)

    env_token = os.environ.get("AGY_MULTI_SERVER_TOKEN") or token
    if allow_no_auth:
        env_token = None
        print(f"\n[INFO] Non-loopback binding ({host}) with NO_AUTH enabled (Tailscale / Trusted Network mode).")
    elif not env_token and not is_loopback:
        env_token = secrets.token_hex(16)
        print(f"\n[SECURITY] Non-loopback binding ({host}). Generated admin token for ALL endpoints (GET/POST/HTML):")
        print(f"  Token: {env_token}")
        print(f"  Use Header: 'Authorization: Bearer {env_token}' or 'X-API-Token: {env_token}' or '?token={env_token}' in browser URL\n")
    elif not is_loopback:
        print(f"\n[SECURITY WARNING] Binding to non-loopback host '{host}'.")
        print("  All endpoints require the configured API token.")
        print("  Ensure network firewall / VPC controls access to this port.\n")
    elif env_token:
        print("\n[SECURITY] Server token configured: GET /api/* and HTML dashboard require authentication.\n")

    UsageDashboardHandler.auth_token = env_token

    server = ThreadingHTTPServer((host, port), UsageDashboardHandler)
    print(f"Usage Dashboard server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


run_server = start_server


if __name__ == "__main__":
    start_server()
