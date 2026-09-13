from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dev_pipeline.preview import CONFIG_NAME, PreviewConflict, PreviewManager, read_preview_config
from dev_pipeline.web_server import handler_for


PREVIEW_SERVER = r'''
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ('<html><body>preview ok ' + self.path + '</body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Set-Cookie', 'unsafe=preview; Path=/')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(size)
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

print('preview server ready', flush=True)
ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
'''


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.program = self.repository / "preview_server.py"
        self.program.write_text(PREVIEW_SERVER, encoding="utf-8")
        self.manager = PreviewManager(self.root / ".web")

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def configure(self, **extra):
        value = {
            "type": "web",
            "command": [sys.executable, "-u", "preview_server.py"],
            "health_path": "/health",
            "startup_timeout": 5,
            **extra,
        }
        (self.repository / CONFIG_NAME).write_text(json.dumps(value), encoding="utf-8")

    def wait_status(self, key, expected, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.manager.status(key, self.repository)
            if current["status"] == expected:
                return current
            time.sleep(0.05)
        self.fail(f"preview did not become {expected}: {current}")

    def test_config_validation_and_unconfigured_status(self):
        self.assertIsNone(read_preview_config(self.repository))
        self.assertEqual("unsupported", self.manager.status("ISS-1", self.repository)["status"])
        (self.repository / CONFIG_NAME).write_text('{"type":"native","command":["x"]}')
        status = self.manager.status("ISS-1", self.repository)
        self.assertEqual("failed", status["status"])
        self.assertIn("web", status["error"])
        (self.repository / CONFIG_NAME).write_text(
            '{"type":"web","command":["x"],"health_path":"https://evil.example/"}')
        with self.assertRaises(ValueError):
            read_preview_config(self.repository)

    def test_start_distinct_ports_restart_stop_and_logs(self):
        self.configure()
        first = self.manager.start("ISS-1", self.repository)
        second = self.manager.start("ISS-2", self.repository)
        self.assertNotEqual(first["port"], second["port"])
        first = self.wait_status("ISS-1", "running")
        self.wait_status("ISS-2", "running")
        self.assertIn("preview server ready", first["log"])
        old_url = first["url"]
        restarted = self.manager.restart("ISS-1", self.repository)
        self.assertNotEqual(old_url, restarted["url"])
        self.wait_status("ISS-1", "running")
        with self.assertRaises(PreviewConflict):
            self.manager.start("ISS-1", self.repository)
        self.manager.stop("ISS-1")
        self.wait_status("ISS-1", "stopped")
        self.manager.stop("ISS-2")
        self.wait_status("ISS-2", "stopped")

    def test_fixed_port_conflict(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.configure(port=port)
        self.manager.start("ISS-1", self.repository)
        with self.assertRaises(PreviewConflict):
            self.manager.start("ISS-2", self.repository)

    def test_start_failure_is_reported(self):
        (self.repository / CONFIG_NAME).write_text(json.dumps({
            "type": "web", "command": ["definitely-not-an-installed-preview-command"]
        }))
        status = self.manager.start("ISS-1", self.repository)
        self.assertEqual("failed", status["status"])
        self.assertIn("起動できませんでした", status["error"])

    def test_authenticated_proxy_forwards_interaction_and_isolates_preview(self):
        self.configure()
        preview = self.manager.start("ISS-1", self.repository)
        preview = self.wait_status("ISS-1", "running")

        class Service:
            previews = self.manager

        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        origin = f"http://127.0.0.1:{server.server_port}"
        server.RequestHandlerClass = handler_for(Service(), {origin}, ("allowed@example.com",))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, data=None, user="allowed@example.com"):
            headers = {"Tailscale-User-Login": user}
            if data is not None:
                headers["Content-Type"] = "text/plain"
            req = Request(origin + path, data=data, headers=headers)
            try:
                with urlopen(req, timeout=5) as response:
                    return response.status, response.read(), response.headers
            except HTTPError as error:
                with error:
                    return error.code, error.read(), error.headers

        try:
            code, body, headers = request(preview["url"] + "hello?q=1")
            self.assertEqual(200, code)
            self.assertIn(b"preview ok /hello?q=1", body)
            self.assertIn("sandbox", headers["Content-Security-Policy"])
            self.assertNotIn("allow-same-origin", headers["Content-Security-Policy"])
            self.assertEqual("null", headers["Access-Control-Allow-Origin"])
            self.assertIsNone(headers["Set-Cookie"])
            self.assertEqual((200, b"payload"), request(preview["url"] + "submit", b"payload")[:2])
            self.assertEqual(403, request(preview["url"], user="blocked@example.com")[0])
            self.assertEqual(404, request("/preview/not-a-token/")[0])
            self.assertEqual(404, request("/repo/preview_server.py")[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
