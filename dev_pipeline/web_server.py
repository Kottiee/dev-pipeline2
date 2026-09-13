from __future__ import annotations

import argparse
import hmac
import http.client
import json
import mimetypes
import os
import secrets
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .web_service import AGENTS, Conflict, WebService, text_field

STATIC = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css",
          "/sw.js": "sw.js", "/manifest.webmanifest": "manifest.webmanifest", "/icon.svg": "icon.svg",
          "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png"}


def handler_for(service, origins, allowed_users=()):
    csrf = secrets.token_urlsafe(32)
    hosts = {urlparse(origin).netloc for origin in origins}
    schemes = {urlparse(origin).netloc: urlparse(origin).scheme for origin in origins}

    class Handler(BaseHTTPRequestHandler):
        server_version = "DevPipeline"

        def log_message(self, format, *args):
            # The URL may contain an initiative ID. Never log request bodies or credentials.
            super().log_message(format, *args)

        def _send(self, code, payload, content_type="application/json; charset=utf-8"):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(payload)

        def _authorize(self, mutation=False):
            if self.headers.get("Host") not in hosts:
                self._send(403, {"error": "接続先が未登録です。サービスの --public-url を確認してください。"})
                return False
            if allowed_users:
                login = self.headers.get("Tailscale-User-Login")
                if login is not None and login not in allowed_users:
                    self._send(403, {"error": "このTailscaleユーザーにはアクセスが許可されていません。"})
                    return False
            if mutation and (self.headers.get("Origin") not in origins
                             or not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf)):
                self._send(403, {"error": "画面を更新してから操作してください。"})
                return False
            return True

        def _preview_parts(self):
            path = urlparse(self.path).path
            if not path.startswith("/preview/"):
                return None
            parts = path.removeprefix("/preview/").split("/", 1)
            if len(parts) != 2 or not parts[0]:
                return None
            return parts[0], "/" + parts[1]

        def _proxy_preview(self):
            parts = self._preview_parts()
            if not parts:
                self._send(404, {"error": "プレビューが見つかりません"})
                return
            token, upstream_path = parts
            target = service.previews.target(token)
            if not target:
                self._send(404, {"error": "プレビューは停止しています"})
                return
            port, status = target
            if status == "starting":
                self._send(503, {"error": "プレビューを起動しています"})
                return
            if self.command == "OPTIONS":
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "null")
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
                requested_headers = self.headers.get("Access-Control-Request-Headers")
                if requested_headers:
                    self.send_header("Access-Control-Allow-Headers", requested_headers)
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            query = urlparse(self.path).query
            if query:
                upstream_path += "?" + query
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, {"error": "プレビューへの入力サイズが不正です"})
                return
            if size < 0 or size > 10 * 1024 * 1024:
                self._send(413, {"error": "プレビューへの入力が大きすぎます"})
                return
            body = self.rfile.read(size) if size else None
            forwarded = {}
            for name in ("Accept", "Accept-Encoding", "Accept-Language", "Content-Type",
                         "If-Modified-Since", "If-None-Match", "Range", "User-Agent"):
                if self.headers.get(name):
                    forwarded[name] = self.headers[name]
            forwarded["Host"] = f"127.0.0.1:{port}"
            if self.headers.get("Origin"):
                forwarded["Origin"] = f"http://127.0.0.1:{port}"
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request(self.command, upstream_path, body=body, headers=forwarded)
                response = connection.getresponse()
                payload = response.read(50 * 1024 * 1024 + 1)
                if len(payload) > 50 * 1024 * 1024:
                    self._send(502, {"error": "プレビューの応答が大きすぎます"})
                    return
                self.send_response(response.status)
                blocked = {"access-control-allow-headers", "access-control-allow-methods",
                           "access-control-allow-origin", "connection", "content-length",
                           "content-security-policy", "keep-alive",
                           "proxy-authenticate", "proxy-authorization", "service-worker-allowed",
                           "set-cookie", "te", "trailer", "transfer-encoding", "upgrade", "x-frame-options"}
                for name, value in response.getheaders():
                    if name.lower() in blocked:
                        continue
                    if name.lower() == "location":
                        location = urlparse(value)
                        if value.startswith("/"):
                            value = f"/preview/{token}{value}"
                        elif location.scheme in {"http", "https"} and location.netloc in {
                                f"127.0.0.1:{port}", f"localhost:{port}"}:
                            value = f"/preview/{token}{location.path or '/'}"
                            if location.query:
                                value += "?" + location.query
                    self.send_header(name, value)
                host = self.headers.get("Host")
                proto = schemes.get(host, "http")
                prefix = f"{proto}://{host}/preview/{token}/"
                websocket = ("wss" if proto == "https" else "ws") + f"://{host}/preview/{token}/"
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "null")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 "sandbox allow-scripts allow-forms allow-modals allow-popups allow-downloads; "
                                 f"default-src {prefix} data: blob: 'unsafe-inline' 'unsafe-eval'; "
                                 f"connect-src {prefix} {websocket} data: blob:; "
                                 f"worker-src {prefix} blob:; form-action {prefix}; "
                                 f"frame-ancestors 'none'; base-uri {prefix}")
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, http.client.HTTPException):
                self._send(502, {"error": "プレビューへ接続できません"})
            finally:
                connection.close()

        def _preview_request(self):
            if not self._preview_parts():
                return False
            if self._authorize():
                self._proxy_preview()
            return True

        def do_GET(self):
            if not self._authorize():
                return
            path = urlparse(self.path).path
            try:
                if self._preview_parts():
                    self._proxy_preview()
                elif path in STATIC:
                    name = STATIC[path]
                    data = (Path(__file__).parent / "webui" / name).read_bytes()
                    mime = {".js": "text/javascript", ".webmanifest": "application/manifest+json"}.get(
                        Path(name).suffix, mimetypes.guess_type(name)[0] or "application/octet-stream")
                    self._send(200, data, mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
                elif path == "/api/config":
                    self._send(200, {"csrf_token": csrf, "agents": AGENTS,
                                     "default_agent": service.default_agent,
                                     "notifications": service.notifications.status()})
                elif path == "/api/projects":
                    self._send(200, {"projects": service.list_projects()})
                elif path.startswith("/api/projects/"):
                    subparts = [unquote(p) for p in path.removeprefix("/api/projects/").split("/") if p]
                    if len(subparts) == 1:
                        self._send(200, service.project_detail(subparts[0]))
                    elif len(subparts) == 2 and subparts[1] == "issues":
                        self._send(200, {"issues": service.list_issues(subparts[0])})
                    elif len(subparts) == 3 and subparts[1] == "issues":
                        self._send(200, service.issue_detail(subparts[0], subparts[2]))
                    else:
                        self._send(404, {"error": "見つかりません"})
                elif path == "/api/initiatives":
                    self._send(200, {"initiatives": service.list_initiatives()})
                elif path.startswith("/api/initiatives/"):
                    initiative_id = unquote(path.removeprefix("/api/initiatives/"))
                    self._send(200, service.detail(initiative_id))
                else:
                    self._send(404, {"error": "見つかりません"})
            except FileNotFoundError as error:
                self._send(404, {"error": str(error)})
            except (ValueError, OSError) as error:
                self._send(400, {"error": str(error)})

        def do_POST(self):
            if self._preview_request():
                return
            if not self._authorize(mutation=True):
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    self._send(413, {"error": "入力が大きすぎるか、空です"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._send(415, {"error": "JSON形式で送信してください"})
                    return
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("JSONオブジェクトが必要です")
                path = urlparse(self.path).path
                if path == "/api/projects":
                    self._send(201, service.create_project(data))
                elif path.startswith("/api/projects/"):
                    subparts = [unquote(p) for p in path.removeprefix("/api/projects/").split("/") if p]
                    if len(subparts) == 2 and subparts[1] == "issues":
                        self._send(202, service.create_issue(subparts[0], data))
                    elif len(subparts) == 4 and subparts[1] == "issues":
                        project_id, issue_id, action = subparts[0], subparts[2], subparts[3]
                        if action == "stop":
                            self._send(200, service.stop_issue(project_id, issue_id))
                        elif action == "configure":
                            self._send(200, service.configure_issue(project_id, issue_id, data))
                        elif action in {"preview-start", "preview-stop", "preview-restart"}:
                            result = service.preview_action(issue_id, action.removeprefix("preview-"), project_id)
                            self._send(202 if action != "preview-stop" else 200, result)
                        else:
                            self._send(202, service.act_issue(project_id, issue_id, action, data))
                    else:
                        self._send(404, {"error": "見つかりません"})
                elif path == "/api/initiatives":
                    self._send(202, service.create(data))
                elif path == "/api/notifications/subscribe":
                    subscription = data.get("subscription")
                    if not isinstance(subscription, dict):
                        raise ValueError("通知購読情報が必要です")
                    service.notifications.subscribe(subscription)
                    self._send(200, {"ok": True})
                elif path == "/api/notifications/unsubscribe":
                    service.notifications.unsubscribe(text_field(data, "endpoint", 4096, True))
                    self._send(200, {"ok": True})
                elif path == "/api/notifications/test":
                    service.notifications.notify("通知の接続テスト", "パイプラインからの通知を受信できています。")
                    self._send(202, {"ok": True})
                elif path.startswith("/api/initiatives/"):
                    parts = path.removeprefix("/api/initiatives/").split("/")
                    if len(parts) != 2:
                        raise ValueError("不明な操作です")
                    initiative_id, action = unquote(parts[0]), parts[1]
                    if action == "stop":
                        self._send(200, service.stop(initiative_id))
                    elif action == "configure":
                        self._send(200, service.configure(initiative_id, data))
                    elif action in {"preview-start", "preview-stop", "preview-restart"}:
                        result = service.preview_action(initiative_id, action.removeprefix("preview-"))
                        self._send(202 if action != "preview-stop" else 200, result)
                    else:
                        self._send(202, service.act(initiative_id, action, data))
                else:
                    self._send(404, {"error": "見つかりません"})
            except Conflict as error:
                self._send(409, {"error": str(error)})
            except FileNotFoundError as error:
                self._send(404, {"error": str(error)})
            except (ValueError, OSError, TypeError) as error:
                self._send(400, {"error": str(error)})

        def do_PUT(self):
            if not self._preview_request():
                self._send(404, {"error": "見つかりません"})

        def do_PATCH(self):
            if not self._preview_request():
                self._send(404, {"error": "見つかりません"})

        def do_DELETE(self):
            if not self._preview_request():
                self._send(404, {"error": "見つかりません"})

        def do_OPTIONS(self):
            if not self._preview_request():
                self._send(404, {"error": "見つかりません"})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="スマホからローカルパイプラインを操作するPWA")
    parser.add_argument("--work-dir", default="work")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--agent", choices=AGENTS, default="codex")
    parser.add_argument("--public-url", help="Tailscale ServeのHTTPS URL（パスなし）")
    parser.add_argument("--allow-user", action="append", default=[], help="許可するTailscaleログイン名")
    parser.add_argument("--push-subject", default=os.environ.get("DEV_PIPELINE_PUSH_SUBJECT"),
                        help="Web Pushの連絡先。例: mailto:you@example.com")
    args = parser.parse_args(argv)
    origins = {f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}"}
    if args.public_url:
        url = urlparse(args.public_url)
        if url.scheme != "https" or not url.netloc or url.path not in ("", "/") or url.query or url.fragment:
            parser.error("--public-url にはパスなしのHTTPS URLを指定してください")
        origins.add(f"https://{url.netloc}")
    service = None
    server = None
    try:
        service = WebService(Path(args.work_dir), args.agent, args.push_subject)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(service, origins, args.allow_user))
        server.daemon_threads = True
        def shutdown(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, shutdown)
        print(f"Pipeline UI: {args.public_url or f'http://127.0.0.1:{args.port}'}", flush=True)
        print("Ctrl+Cで終了。実行中のジョブは停止し、保存済み状態から再開できます。", flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")
    finally:
        if server:
            server.server_close()
        if service:
            service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
