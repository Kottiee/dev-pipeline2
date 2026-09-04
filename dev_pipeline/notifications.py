"""Optional Web Push delivery. Pipeline execution never depends on push availability."""
from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .store import WorkStore


class Notifications:
    def __init__(self, directory: Path, subject: str | None = None):
        self.directory = directory
        self.store = WorkStore(directory, "")
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.public_key = None
        self.sender = None
        self.subject = subject
        self.reason = "Web Pushを使うには .[web] をインストールし、--push-subject に連絡先を設定してください。"
        try:
            self.data = self.store.read_json("notifications.json")
        except FileNotFoundError:
            self.data = {"subscriptions": [], "pending": [], "last_error": None}
        if subject:
            if not subject.startswith(("mailto:", "https://")):
                raise ValueError("--push-subject は mailto:メールアドレス または https://URL で指定してください")
            try:
                from py_vapid import Vapid
                from pywebpush import webpush
                from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            except ImportError:
                pass
            else:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                key_path = directory / "vapid.pem"
                if not key_path.exists():
                    key = Vapid()
                    key.generate_keys()
                    key.save_key(str(key_path))
                    key_path.chmod(0o600)
                key = Vapid.from_file(str(key_path))
                self.public_key = base64.urlsafe_b64encode(key.public_key.public_bytes(
                    Encoding.X962, PublicFormat.UncompressedPoint)).rstrip(b"=").decode()
                self.sender = webpush
                self.reason = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="web-push")

    def start(self):
        self.thread.start()

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)

    def _save(self):
        self.store.write_json("notifications.json", self.data)

    def status(self):
        with self.lock:
            return {"available": self.sender is not None, "public_key": self.public_key,
                    "reason": self.reason, "last_error": self.data.get("last_error"),
                    "pending": len(self.data["pending"])}

    def subscribe(self, subscription: dict):
        if self.sender is None:
            raise ValueError(self.reason)
        endpoint = subscription.get("endpoint", "")
        url = urlparse(endpoint)
        host = url.hostname or ""
        # A subscription must not turn this local service into an arbitrary HTTP client.
        allowed = (host == "fcm.googleapis.com" or host == "web.push.apple.com"
                   or host.endswith(".push.apple.com")
                   or host == "updates.push.services.mozilla.com"
                   or host.endswith(".push.services.mozilla.com"))
        keys = subscription.get("keys", {})
        if (url.scheme != "https" or not allowed or url.username or url.password
                or url.port not in (None, 443) or len(endpoint) > 4096
                or not isinstance(keys, dict)
                or not all(isinstance(keys.get(k), str) and 1 <= len(keys[k]) <= 512
                           for k in ("auth", "p256dh"))):
            raise ValueError("有効なブラウザーの通知購読情報が必要です")
        clean = {"endpoint": endpoint, "keys": {k: keys[k] for k in ("auth", "p256dh")}}
        with self.lock:
            self.data["subscriptions"] = [s for s in self.data["subscriptions"]
                                          if s["endpoint"] != endpoint] + [clean]
            self._save()

    def unsubscribe(self, endpoint: str):
        with self.lock:
            self.data["subscriptions"] = [s for s in self.data["subscriptions"]
                                          if s["endpoint"] != endpoint]
            self._save()

    def notify(self, title: str, body: str, initiative_id: str | None = None):
        if not self.sender:
            return
        with self.lock:
            if not self.data["subscriptions"]:
                return
            self.data["pending"].append({
                "id": uuid.uuid4().hex, "title": title, "body": body,
                "url": "/#" + (initiative_id or ""), "created": time.time(),
                "remaining": [s["endpoint"] for s in self.data["subscriptions"]],
            })
            self._save()
        self.wake.set()

    def _loop(self):
        while not self.closed.is_set():
            self.wake.wait(30)
            self.wake.clear()
            if not self.closed.is_set():
                self.deliver_pending()

    def deliver_pending(self):
        if not self.sender:
            return
        with self.lock:
            events = list(self.data["pending"])
            subscriptions = {s["endpoint"]: s for s in self.data["subscriptions"]}
        for event in events:
            for endpoint in list(event["remaining"]):
                if self.closed.is_set():
                    return
                expired = time.time() - event["created"] > 86400
                remove = expired or endpoint not in subscriptions
                try:
                    if not remove:
                        self.sender(subscription_info=subscriptions[endpoint],
                                    data=json.dumps({k: event[k] for k in ("id", "title", "body", "url")}),
                                    vapid_private_key=str(self.directory / "vapid.pem"),
                                    vapid_claims={"sub": self.subject}, ttl=3600, timeout=10)
                        remove = True
                        with self.lock:
                            self.data["last_error"] = None
                except Exception as error:
                    response = getattr(error, "response", None)
                    if response is not None and response.status_code in (404, 410):
                        self.unsubscribe(endpoint)
                        remove = True
                    else:
                        with self.lock:
                            # Avoid persisting subscriptions / provider response bodies in UI errors.
                            self.data["last_error"] = "通知の配信に失敗しました。自動再試行します。"
                with self.lock:
                    if remove and endpoint in event["remaining"]:
                        event["remaining"].remove(endpoint)
                    self.data["pending"] = [e for e in self.data["pending"] if e["remaining"]]
                    self._save()
