"""Worker channel: HTTP client + SSE consumers of the bus streams.

Pure stdlib: urllib.client for request/response, http.client in a
background thread for SSE (data: {...} events).
"""
from __future__ import annotations

import http.client
import json
import queue
import threading
from urllib import parse, request


class CoreError(RuntimeError):
    pass


class CoreClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    # --- plumbing -------------------------------------------------------------

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 20.0) -> dict:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = request.Request(url, data=data, method=method,
                              headers={"Content-Type": "application/json"} if data else {})
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode())
        except (request.URLError, OSError, TimeoutError) as e:
            # service errors come back as JSON body ({"error": ...}) — surface its text
            detail = None
            if isinstance(e, request.HTTPError):
                try:
                    j = json.loads(e.read().decode("utf-8", "replace") or "{}")
                    detail = j.get("error") or None
                except (ValueError, OSError):
                    detail = None
            suffix = detail or str(getattr(e, "reason", None) or e)
            raise CoreError(f"{method} {path}: {suffix}") from e
        if "error" in out:
            raise CoreError(f"{method} {path}: {out['error']}")
        return out

    # --- API -------------------------------------------------------------------

    def health(self) -> dict:
        return self._req("GET", "/health")

    def devices(self) -> dict:
        return self._req("GET", "/devices")

    def status(self, name: str) -> dict:
        return self._req("GET", f"/devices/{name}/status")

    def open(self, name: str, cfg: dict | None = None) -> dict:
        return self._req("POST", f"/devices/{name}/open", cfg or {})

    def close(self, name: str) -> dict:
        return self._req("POST", f"/devices/{name}/close")

    def reconfigure(self, name: str, cfg: dict) -> dict:
        return self._req("POST", f"/devices/{name}/reconfigure", cfg)

    def send(self, name: str, frame: dict) -> dict:
        return self._req("POST", f"/devices/{name}/send", frame)

    def send_raw(self, name: str, hexstr: str) -> dict:
        return self._req("POST", f"/devices/{name}/send_raw", {"hex": hexstr})


# --- SSE ----------------------------------------------------------------------


class SSEConsumer:
    """Connects to the device SSE stream and puts parsed frames into a
    queue.Queue.

    Background thread; stop() breaks the connection (and the reconnect loop).
    """

    def __init__(self, base_url: str, device: str) -> None:
        self.base = base_url.rstrip("/")
        self.device = device
        self.q: queue.Queue[dict] = queue.Queue()
        self.alive = False
        self.connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, max_retry: float = 5.0) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(max_retry,), daemon=True)
        self._thread.start()
        self.alive = True

    def stop(self) -> None:
        self._stop.set()
        self.alive = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def drain(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return out

    def _run(self, max_retry: float) -> None:
        delay = 0.2
        self._ever = False
        while not self._stop.is_set():
            try:
                parts = parse.urlsplit(self.base)
                conn = http.client.HTTPConnection(parts.netloc, timeout=10)
                conn.request("GET", f"/devices/{self.device}/stream")
                resp = conn.getresponse()
                if resp.status != 200:
                    raise CoreError(f"status {resp.status}")
                self.connected.set()
                self._ever = True
                for raw in resp:
                    if self._stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data: "):
                        try:
                            ev = json.loads(line[6:])
                        except ValueError:
                            continue
                        self.q.put(ev)
                conn.close()
            except (CoreError, OSError, http.client.HTTPException) as e:
                if not self._stop.is_set() and self._ever:
                    self.q.put({"__error__": str(e)})
            self.connected.clear()
            delay = min(delay * 2, max_retry)
            self._stop.wait(delay)
            delay = max(delay, 0.2)

    def qsize(self) -> int:
        return self.q.qsize()