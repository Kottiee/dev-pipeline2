from __future__ import annotations

import argparse
import hmac
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

        def do_GET(self):
            if not self._authorize():
                return
            path = urlparse(self.path).path
            try:
                if path in STATIC:
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
