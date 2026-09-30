"""
agy_multi.server
Lightweight HTTP server for real-time usage dashboard and JSON API.
"""

import os
import json
import secrets
import urllib.parse
import ipaddress
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, List

try:
    from .manager import ProfileManager
    from .usage import get_all_usage, get_profile_usage, render_html_dashboard, profile_on_dashboard
    from .utils import load_env_config
except (ImportError, ValueError):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agy_multi.manager import ProfileManager
    from agy_multi.usage import get_all_usage, get_profile_usage, render_html_dashboard, profile_on_dashboard
    from agy_multi.utils import load_env_config


class UsageDashboardHandler(BaseHTTPRequestHandler):
    manager = None
    auth_token: Optional[str] = None
    allow_remote: bool = False
    MAX_BODY_BYTES = 1 << 20  # 1 MiB

    @staticmethod
    def _tailnet_suffix() -> Optional[str]:
        s = os.environ.get("AGY_MULTI_TAILNET", "").strip().lower().lstrip(".")
        return ("." + s) if s else None

    @staticmethod
    def _is_loopback_ip(ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
            if getattr(a, "ipv4_mapped", None):
                a = a.ipv4_mapped
            return a.is_loopback
        except ValueError:
            return False

    @staticmethod
    def _is_tailscale_ip(ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
            if getattr(a, "ipv4_mapped", None):
                a = a.ipv4_mapped
            return a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            return False

    @classmethod
    def _trusted_hosts_list(cls) -> List[str]:
        raw = os.environ.get("AGY_MULTI_TRUSTED_HOSTS", "").strip()
        if not raw:
            return []
        return [h.strip().lower() for h in raw.split(",") if h.strip()]

    @classmethod
    def _is_trusted_hostname(cls, hostname: Optional[str]) -> bool:
        """Hostnames trusted for Origin/Host: localhost, loopback IPs, Tailscale CGNAT IPs,
        the user's own tailnet suffix (AGY_MULTI_TAILNET=xxx.ts.net),
        or explicitly declared trusted hosts in AGY_MULTI_TRUSTED_HOSTS."""
        if not hostname:
            return False
        h = hostname.strip("[]").lower()
        if h == "localhost" or cls._is_loopback_ip(h) or cls._is_tailscale_ip(h):
            return True
        for th in cls._trusted_hosts_list():
            if th.startswith("*.") and h.endswith(th[1:]):
                return True
            if th.startswith(".") and h.endswith(th):
                return True
            if h == th:
                return True
        suffix = cls._tailnet_suffix()
        return bool(suffix and h.endswith(suffix))

    @classmethod
    def _local_hostname(cls) -> str:
        try:
            import socket
            return socket.gethostname().strip().lower()
        except Exception:
            return ""

    @classmethod
    def _is_trusted_host_header(cls, hostname: Optional[str]) -> bool:
        """Hostnames trusted for incoming Host header (DNS-rebinding guard):
        All trusted hostnames, plus:
        - Machine's local hostname (e.g. socket.gethostname())
        - Tailscale MagicDNS (*.ts.net)
        - Cloudflare quick tunnels (*.trycloudflare.com)
        """
        if not hostname:
            return False
        h = hostname.strip("[]").lower()
        if cls._is_trusted_hostname(h):
            return True
        local_h = cls._local_hostname()
        if local_h and h == local_h:
            return True
        if h.endswith(".ts.net") or h == "ts.net":
            return True
        if h.endswith(".trycloudflare.com") or h == "trycloudflare.com":
            return True
        return False

    def _host_header_ok(self) -> bool:
        """DNS-rebinding guard: without a token, Host must be an IP literal, trusted name,
        or the connection originates directly from Tailscale CGNAT."""
        if self.auth_token:
            return True
        client_ip = self.client_address[0] if getattr(self, "client_address", None) else ""
        if client_ip and self._is_tailscale_ip(client_ip):
            return True
        host = self.headers.get("Host", "")
        hostname = urllib.parse.urlsplit("//" + host).hostname if host else None
        if not hostname:
            return False
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            return self._is_trusted_host_header(hostname)

    def _post_csrf_ok(self) -> bool:
        """CSRF guard: JSON content-type forces a CORS preflight; Origin (if sent) must be trusted or same-origin."""
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # non-browser client (curl/CLI); browsers always send Origin on cross-site POST
        try:
            o = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        if o.netloc and o.netloc == self.headers.get("Host", ""):
            return True
        return self._is_trusted_hostname(o.hostname)

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise ValueError("Invalid Content-Length")
        if n < 0 or n > self.MAX_BODY_BYTES:
            raise ValueError("Request body too large")
        body = self.rfile.read(n) if n > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise ValueError("Malformed JSON body")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _send_forbidden(self, reason: str):
        encoded = json.dumps({"success": False, "error": f"Forbidden: {reason}"}).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        # Suppress noisy standard request logs
        pass

    def _get_allowed_origin(self) -> Optional[str]:
        origin = self.headers.get("Origin")
        if not origin:
            return None
        try:
            parsed = urllib.parse.urlparse(origin)
            host_header = self.headers.get("Host", "")
            if parsed.netloc and parsed.netloc == host_header:
                return origin
            if self._is_trusted_hostname(parsed.hostname):
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
            if self._is_loopback_ip(client_ip):
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
        # Drain any unread request body so the socket closes cleanly without TCP RST on Windows (WinError 10053)
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                self.rfile.read(content_length)
        except Exception:
            pass

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
        try:
            self.wfile.flush()
        except Exception:
            pass

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if not self._host_header_ok():
            self._send_forbidden("untrusted Host header")
            return
        if self._requires_read_auth() and not self._check_auth():
            self._send_unauthorized()
            return

        # If token was supplied via query string, set cookie for subsequent fetch calls
        set_cookie_header = None
        has_query_token = False
        remaining_query = ""
        if self.auth_token:
            try:
                parsed = urllib.parse.urlsplit(self.path)
                params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                token_val = None
                kept_params = []
                for k, v in params:
                    if k == "token":
                        token_val = v
                    else:
                        kept_params.append((k, v))
                if token_val and secrets.compare_digest(token_val, self.auth_token):
                    set_cookie_header = f"agy_token={token_val}; Path=/; SameSite=Lax; HttpOnly"
                    has_query_token = True
                    if kept_params:
                        remaining_query = "?" + urllib.parse.urlencode(kept_params)
            except Exception:
                pass

        clean_path = self.path.split("?", 1)[0]
        if clean_path in ("/", "/index.html", "/dashboard"):
            if has_query_token and set_cookie_header:
                self.send_response(302)
                self.send_header("Location", clean_path + remaining_query)
                self.send_header("Set-Cookie", set_cookie_header)
                self.end_headers()
                return
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
            except Exception:
                self.send_error(500, "Error generating dashboard")

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
            except Exception:
                self.send_error(500, "Error fetching usage data")

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
            except Exception:
                self.send_error(500, "Error getting relay candidates")

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
            except Exception:
                self.send_error(500, "Error getting config")
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if not self._host_header_ok():
            self._send_forbidden("untrusted Host header")
            return
        if not self._post_csrf_ok():
            self._send_forbidden("cross-site or non-JSON request")
            return
        if not self._check_auth():
            self._send_unauthorized()
            return

        if self.path == "/api/relay":
            try:
                payload = self._read_json_body()

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
                err_msg = str(e) if isinstance(e, ValueError) else "Invalid relay request"
                err_bytes = json.dumps({"success": False, "error": err_msg}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

        elif self.path == "/api/account-visibility":
            try:
                payload = self._read_json_body()
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
                err_msg = str(e) if isinstance(e, ValueError) else "Invalid visibility request"
                err_bytes = json.dumps({"success": False, "error": err_msg}, ensure_ascii=False).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

        elif self.path == "/api/config":
            try:
                payload = self._read_json_body()
                new_cfg = self.manager.update_config(**payload)
                encoded = json.dumps({"success": True, "config": new_cfg}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                err_msg = str(e) if isinstance(e, ValueError) else "Invalid config request"
                err_bytes = json.dumps({"success": False, "error": err_msg}, ensure_ascii=False).encode("utf-8")
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
        token_file = mgr.base_dir / "server_token"
        try:
            from .utils import secure_write_text
            secure_write_text(token_file, env_token, mode=0o600)
            token_saved_msg = f" (saved to {token_file})"
        except Exception:
            token_saved_msg = ""
        masked_token = env_token[:4] + "****"
        print(f"\n[SECURITY] Non-loopback binding ({host}). Generated admin token for ALL endpoints (GET/POST/HTML):")
        print(f"  Token: {masked_token}{token_saved_msg}")
        print("  Use Header: 'Authorization: Bearer <token>' or 'X-API-Token: <token>' or '?token=<token>' in browser URL\n")
    elif not is_loopback:
        masked_token = env_token[:4] + "****" if len(env_token) >= 4 else "****"
        print(f"\n[SECURITY WARNING] Binding to non-loopback host '{host}'.")
        print(f"  Token: {masked_token}")
        print("  All endpoints require the configured API token.")
        print("  Ensure network firewall / VPC controls access to this port.\n")
    elif env_token:
        masked_token = env_token[:4] + "****" if len(env_token) >= 4 else "****"
        print(f"\n[SECURITY] Server token configured ({masked_token}): GET /api/* and HTML dashboard require authentication.\n")

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
