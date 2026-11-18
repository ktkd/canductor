"""canductor.core HTTP microservice (stdlib asyncio, no deps).

Endpoints:
  GET  /health
  GET  /devices
  GET  /devices/<name>/status
  POST /devices/<name>/open          {config?}
  POST /devices/<name>/close
  POST /devices/<name>/reconfigure   {config}
  POST /devices/<name>/send          {id, data, ext, remote}
  POST /devices/<name>/send_raw      {hex}
  GET  /devices/<name>/stream        SSE: data: {frame} + keepalive comments
      (?throttle=<ms> — display view: one event per window,
       {"total": <frames in window>, "frames": [changed frames only]})
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import queue
import threading
import time
from pathlib import Path
from typing import Any
from urllib import parse as urlparse

from .device import Config, DeviceError, UsbCanDevice, make_device
from ..worker.runner import Runner, SessionOpts

HEARTBEAT_S = 15.0


def _throttle_ms(path: str) -> int:
    """Parse ?throttle=<ms> from the stream URL (0 = raw per-frame stream)."""
    parts = path.split("?", 1)
    if len(parts) < 2:
        return 0
    from urllib import parse as urlparse
    raw = (urlparse.parse_qs(parts[1]).get("throttle") or ["0"])[0]
    try:
        return max(0, min(1000, int(raw)))
    except ValueError:
        return 0


def _frame_json(f, ts: float) -> dict:
    return {
        "id": f.id,
        "id_hex": f"{f.id:03X}" if not f.extended and f.id <= 0x7FF else f"{f.id:08X}",
        "ext": f.extended,
        "remote": f.remote,
        "dlc": f.dlc,
        "data": f.data.hex(),
        "ts": ts,
    }


class Service:
    def __init__(self, devices: dict[str, str], host: str = "127.0.0.1", port: int = 8111,
                 out_root: str | Path = "logs", scenarios_dir: str | Path | None = None) -> None:
        from ..worker.webcontrol import WebHub

        self.host = host
        self.port = port
        self._devices: dict[str, UsbCanDevice] = {}
        for alias, path in devices.items():
            cfg = Config.hs() if alias == "hs" else (Config.ms() if alias == "ms" else None)
            self._devices[alias] = make_device(alias, path, cfg)
        self._server: asyncio.Server | None = None
        self._started_at = time.time()

        # web/session state
        self.hub = WebHub()
        self.out_root = Path(out_root)
        self.out_root.mkdir(parents=True, exist_ok=True)
        if scenarios_dir:
            sd = Path(scenarios_dir)
        else:
            from ..worker.scenario import scenario_roots
            sd = next(iter(scenario_roots()), Path(__file__).parent.parent.parent / "scenarios")
        self.scen_dir = sd
        self.scenarios = sorted(x.stem for x in sd.glob("*.json")) if sd.exists() else []
        self._worker: Worker | None = None
        self._worker_lock = threading.Lock()

    @property
    def devices(self) -> dict[str, UsbCanDevice]:
        return self._devices

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        try:
            self.port = self._server.sockets[0].getsockname()[1]
        except (AttributeError, IndexError):
            pass

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for d in self._devices.values():
            if d.state.value != "init":
                try:
                    await d.close()
                except Exception:
                    pass

    def list_devices(self) -> dict:
        out = {}
        for name, d in self._devices.items():
            out[name] = {
                "path": d.path,
                "state": d.state.value,
                "config": d.defn,
                "rx": d.rx_count,
                "tx": d.tx_count,
                "rx_queue": d.pending_frames(),
                "last_error": d.last_error,
            }
        return out

    # --- HTTP plumbing ----------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await self._read_request(reader)
            if req is None:
                return
            p = urlparse.unquote(req["path"].split("?")[0]).rstrip("/") or "/"
            parts = p.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "devices" and parts[2] == "stream":
                await self._serve_stream(self._get(parts[1]), writer, _throttle_ms(req["path"]))
                return
            if p == "/" and req["method"] == "GET":
                await self._serve_page(writer)
                return
            if p == "/api/session/stream" and req["method"] == "GET":
                await self._serve_worker_stream(writer)
                return
            if p.startswith("/api/"):
                writer.write(await self._api(req))
                await writer.drain()
                return
            body = await self._route(req)
            writer.write(body)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001
            if os.environ.get("CANDUCTOR_DBG"):
                import traceback
                traceback.print_exc(file=sys.stderr)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_request(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(8192)
            if not chunk:
                return None
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            return None
        headers: dict[str, str] = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            if k:
                headers[k.strip().lower()] = v.strip()
        try:
            n = int(headers.get("content-length", "0") or 0)
        except ValueError:
            return None
        if n > 16 * 1024 * 1024:  # sane cap for the JSON/SSE-control API (localhost)
            return None
        body = rest
        while len(body) < n:
            chunk = await reader.read(n - len(body))
            if not chunk:
                return None
            body += chunk
        return {"method": method.upper(), "path": path, "headers": headers, "body": body[:n]}

    async def _serve_stream(self, dev: UsbCanDevice, writer: asyncio.StreamWriter,
                            throttle_ms: int = 0) -> None:
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode()
        writer.write(head)
        await writer.drain()
        _dbg = os.environ.get("CANDUCTOR_DBG")
        q = dev.subscribe()
        try:
            if throttle_ms <= 0:
                await self._stream_raw(dev, q, writer, _dbg)
            else:
                await self._stream_view(dev, q, writer, throttle_ms, _dbg)
        except (ConnectionResetError, BrokenPipeError):
            if _dbg:
                print(f"SSE {dev.name}: conn closed", file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            if _dbg:
                print(f"SSE {dev.name}: EXC {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            raise
        finally:
            dev.unsubscribe(q)

    async def _stream_raw(self, dev: UsbCanDevice, q, writer, _dbg) -> None:
        """Legacy per-frame stream — every frame, as before (recorder consumers)."""
        while True:
            try:
                frame = await asyncio.wait_for(q.get(), HEARTBEAT_S)
            except asyncio.TimeoutError:
                try:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                except Exception:
                    break
                continue
            if frame is None:
                continue
            ev = json.dumps({**_frame_json(frame, time.time()), "bus": dev.name}, ensure_ascii=False)
            writer.write(f"data: {ev}\n\n".encode())
            await writer.drain()
            if _dbg:
                print(f"SSE {dev.name}: sent frame {frame.id:#x}", file=sys.stderr, flush=True)

    async def _stream_view(self, dev: UsbCanDevice, q, writer, throttle_ms: int, _dbg) -> None:
        """Display stream (web UI), throttled to one event per window of
        `throttle` ms: `total` = all frames in the window (exact, for the
        UI counters) + `frames` = the changed ones only (latest per id,
        unchanged data is skipped). This turns a live car bus (~100-400
        msg/s, 400 KB/30 s) into a steady 10 msg/s display flow; the
        recorder keeps its own raw subscription."""
        window = throttle_ms / 1000.0
        last: dict[tuple, bytes] = {}     # (ext, id) -> data last sent (change detection)
        last_beat = time.monotonic()
        while True:
            w0 = time.monotonic()
            rows: dict[tuple, tuple] = {}
            n = 0
            while True:
                remain = window - (time.monotonic() - w0)
                if remain <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(q.get(), min(remain, 0.05))
                except asyncio.TimeoutError:
                    break
                if frame is None:
                    continue
                n += 1
                rows[(bool(frame.extended), frame.id)] = (frame, time.time())
            if n:
                frames = []
                for key, (frame, ts) in rows.items():
                    if last.get(key) == frame.data:
                        continue    # repeated data — counted in `total` only
                    last[key] = frame.data
                    frames.append(dict(_frame_json(frame, ts), bus=dev.name))
                ev = json.dumps({"total": n, "frames": frames}, ensure_ascii=False)
                writer.write(f"data: {ev}\n\n".encode())
                last_beat = time.monotonic()
                await writer.drain()
                if _dbg:
                    print(f"SSE {dev.name}: view total={n} sent={len(frames)}", file=sys.stderr, flush=True)
            elif time.monotonic() - last_beat >= HEARTBEAT_S:
                try:
                    writer.write(b": keepalive\n\n")
                    last_beat = time.monotonic()
                    await writer.drain()
                except Exception:
                    break

# ======================================================================
    # WEB / API
    # ======================================================================

    def _web_dir(self) -> Path:
        return Path(__file__).parent.parent / "web"

    async def _serve_page(self, writer: asyncio.StreamWriter) -> None:
        idx = self._web_dir() / "index.html"
        if idx.exists():
            body = idx.read_bytes()
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(head.encode() + body)
        else:
            writer.write(self._json({"error": "index.html missing"}, 404))
        await writer.drain()

    async def _serve_worker_stream(self, writer: asyncio.StreamWriter) -> None:
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode()
        writer.write(head)
        await writer.drain()
        q = self.hub.subscribe()
        loop = asyncio.get_running_loop()

        def _qget():
            try:
                return q.get(True, HEARTBEAT_S)
            except queue.Empty:
                return None

        # send current status immediately so client syncs
        try:
            writer.write(("data: " + json.dumps({"type": "status", "state": self.hub.status()}, ensure_ascii=False) + "\n\n").encode())
            await writer.drain()
            while True:
                ev = await loop.run_in_executor(None, _qget)
                if ev is None:
                    try:
                        writer.write(b": keepalive\n\n")
                        await writer.drain()
                    except Exception:
                        break
                    continue
                writer.write((f"data: {json.dumps(ev, ensure_ascii=False)}\n\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.hub.unsubscribe(q)

    def _json_bytes(self, obj, code=200) -> bytes:
        return self._json(obj, code)

    async def _api(self, req: dict) -> bytes:
        p = req["path"].split("?")[0].rstrip("/")
        m = req["method"]
        query = {}
        if "?" in req["path"]:
            from urllib import parse as urlparse
            query = {k: v[0] for k, v in urlparse.parse_qs(req["path"].split("?", 1)[1]).items()}
        body = {}
        if req["body"]:
            try:
                body = json.loads(req["body"].decode() or "{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
        try:
            if m == "GET" and p == "/api/scenario":
                name = query.get("name")
                if name:
                    sc = self._load_scen(name)
                    if sc is None:
                        return self._json({"error": f"scenario {name} not found"}, 404)
                    return self._json(_scenario_view(sc))
                return self._json({"scenarios": self.scenarios})
            if m == "GET" and p == "/api/session/status":
                meta = self.hub.status()
                meta["devices"] = self.list_devices()
                return self._json(meta)
            if m == "POST" and p == "/api/session/start":
                return await self._api_session_start(body)
            if m == "POST" and p == "/api/session/cmd":
                return self._api_session_cmd(body)
            if m == "GET" and p == "/api/logs":
                return self._json(self._logs_listing())
            if m == "GET" and p == "/api/logs/file":
                return self._logs_file(query.get("path", ""), query.get("dl", "") == "1")
        except KeyError as e:
            return self._json({"error": str(e)}, 404)
        except Exception as e:  # noqa: BLE001
            from ..worker.scenario import ScenarioError
            if isinstance(e, ScenarioError):
                return self._json({"error": f"scenario error: {e}"}, 400)
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"error": f"no api endpoint {m} {p}"}, 404)

    def _load_scen(self, name: str):
        """Load a scenario; None if the name/path does not exist.

        A found-but-invalid scenario raises ScenarioError (→ 400 in _api):
        'broken scenario' must not look like 'not found'."""
        from ..worker.scenario import find_scenario_file, load_scenario
        path = find_scenario_file(name)
        if path is None:
            return None
        return load_scenario(str(path))

    async def _api_session_start(self, body: dict) -> bytes:
        with self._worker_lock:
            w = self._worker
            active = w is not None and w.thread is not None and w.thread.is_alive()
            if active:
                return self._json({"error": "session already running"}, 409)
        name = body.get("scenario", self.scenarios[0] if self.scenarios else "mazda_swap")
        sc = self._load_scen(name)
        if sc is None:
            return self._json({"error": f"scenario '{name}' not found"}, 404)
        from ..worker.runner import SessionOpts
        from ..worker.webcontrol import WebControl
        from ..worker.channel import CoreClient
        opts = SessionOpts(
            scenario=sc,
            out_root=self.out_root,
            core=CoreClient(self.base_url),
            device_aliases={a: d.path for a, d in self._devices.items()},
            repeats=int(body.get("repeats", 1)),
            only=[x.strip() for x in (body.get("only") or "").split(",") if x.strip()],
            cars=[x.strip() for x in (body.get("cars") or "").split(",") if x.strip()],
            buses=[x.strip() for x in (body.get("buses") or "").split(",") if x.strip()],
            scale=float(body.get("scale", 1.0)),
            auto_confirm=bool(body.get("auto", False)),
            control=WebControl(self.hub),
        )
        w = Worker(self, opts)
        with self._worker_lock:
            if self._worker is not None:
                tw = self._worker
                if tw.thread is not None and tw.thread.is_alive():
                    return self._json({"error": "session already running"}, 409)
                # previous session finished — reset stale hub state
                self.hub.update(session_state="idle", card=None, phase=None,
                                done=[], plan=[], last_error="")
            self._worker = w
        # drop UI commands left over from a finished session (they would be
        # consumed by the first test card of the new one)
        while True:
            try:
                self.hub.cmd_q.get_nowait()
            except queue.Empty:
                break
        w.start()
        return self._json({"ok": True, "scenario": name})

    def _api_session_cmd(self, body: dict) -> bytes:
        action = body.get("action", "start")
        note = body.get("note") or {}
        self.hub.cmd_q.put({"action": action, "note": note})
        return self._json({"ok": True, "action": action})

    def _logs_listing(self) -> dict:
        out = {"root": str(self.out_root), "sessions": []}
        if not self.out_root.exists():
            return out
        sess = []
        for d in self.out_root.iterdir():
            if d.is_dir() and d.name.startswith("session_"):
                tests = []
                for td in sorted(d.iterdir()):
                    if td.is_dir():
                        files = {}
                        for f in td.iterdir():
                            if f.is_file():
                                files[f.name] = f.stat().st_size
                        if files:
                            tests.append({"name": td.name, "files": files})
                sess.append({"name": d.name, "tests": tests})
        sess.sort(key=lambda x: x["name"], reverse=True)
        out["sessions"] = sess
        return out

    def _logs_file(self, rel: str, download: bool) -> bytes:
        try:
            p = (self.out_root / rel).resolve()
            root = self.out_root.resolve()
            if not p.is_file() or not p.is_relative_to(root):
                return self._json({"error": "not found"}, 404)
            data = p.read_bytes()
            reason = "200 OK"
            extra = ""
            if download:
                extra = f"Content-Disposition: attachment; filename=\"{p.name}\"\r\n"
            head = (
                f"HTTP/1.1 {reason}\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                + extra
                + f"Content-Length: {len(data)}\r\n"
                "Connection: close\r\n\r\n"
            )
            return head.encode() + data
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 404)

    async def _route(self, req: dict) -> bytes:
        p = req["path"].split("?")[0].rstrip("/")
        m = req["method"]
        parts = p.strip("/").split("/")
        try:
            if m == "GET" and p == "/health":
                return self._json({"ok": True, "devices": len(self._devices),
                                   "uptime": round(time.time() - self._started_at, 1)})
            if m == "GET" and p == "/devices":
                return self._json(self.list_devices())
            if len(parts) >= 2 and parts[0] == "devices":
                dev = self._get(parts[1])
                if m == "GET" and len(parts) == 3 and parts[2] == "status":
                    return self._json(dev.status())
                if m == "POST" and len(parts) == 3 and parts[2] == "open":
                    cfg = self._config_from_body(req)
                    if cfg is not None:
                        dev.cfg = cfg
                    await dev.open()
                    return self._json(dev.status())
                if m == "POST" and len(parts) == 3 and parts[2] == "close":
                    await dev.close()
                    return self._json(dev.status())
                if m == "POST" and len(parts) == 3 and parts[2] == "reconfigure":
                    cfg = self._config_from_body(req)
                    if cfg is None:
                        raise ValueError("no config in body")
                    await dev.reconfigure(cfg)
                    return self._json(dev.status())
                if m == "POST" and len(parts) == 3 and parts[2] == "send":
                    body = json.loads(req["body"].decode() or "{}")
                    if body.get("id") is None:
                        raise ValueError("'id' required (int CAN id)")
                    fr = await dev.send_frame(body)
                    return self._json(_frame_json(fr, time.time()))
                if m == "POST" and len(parts) == 3 and parts[2] == "send_raw":
                    body = json.loads(req["body"].decode() or "{}")
                    await dev.send_raw(bytes.fromhex(body.get("hex", "")))
                    return self._json({"ok": True})
        except KeyError as e:
            return self._json({"error": f"unknown device '{e}'"}, 404)
        except (ValueError, json.JSONDecodeError) as e:
            return self._json({"error": str(e)}, 400)
        except DeviceError as e:
            return self._json({"error": str(e)}, 503)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"error": f"no endpoint {m} {p}"}, 404)

    def _get(self, name: str) -> UsbCanDevice:
        try:
            return self._devices[name]
        except KeyError:
            raise KeyError(name) from None

    @staticmethod
    def _config_from_body(req: dict) -> Config | None:
        if not req.get("body"):
            return None
        body = json.loads(req["body"].decode() or "{}")
        if not body:
            return None
        f = body.get("id_filter") or {}
        return Config(
            can_baud=int(body.get("can_baud", 500_000)),
            standard_only=bool(body.get("standard_only", True)),
            variable_encoding=bool(body.get("variable", False)),
            loopback=bool(body.get("loopback", False)),
            silent=bool(body.get("silent", False)),
            retransmit=bool(body.get("retransmit", True)),
            filter_id=int(body.get("filter", 0)),
            mask_id=int(body.get("mask", 0)),
            serial_baud=int(body.get("serial_baud", 2_000_000)),
            id_filter_mode=f.get("mode"),
            id_filter_ids=[int(x) for x in f.get("ids", [])],
        )

    @staticmethod
    def _json(obj: dict, code: int = 200) -> bytes:
        data = json.dumps(obj, ensure_ascii=False).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 409: "Conflict",
                  503: "Service Unavailable"}.get(code, "Error")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n"
        )
        return head.encode() + data


# ============================================================================
# WEB: session API + static page
# ============================================================================

class Worker:
    """Worker-session as a thread: Runner + WebControl."""

    def __init__(self, svc: "Service", opts: SessionOpts) -> None:
        self.svc = svc
        self.opts = opts
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        def _go() -> None:
            try:
                Runner(self.opts).run()
            except Exception as e:  # noqa: BLE001
                # leave the session in the 'error' state (the web panel shows the
                # message + the start form); a done-state would hide the failure
                self.opts.control.error(f"worker: {type(e).__name__}: {e}")

        self.thread = threading.Thread(target=_go, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        pass


def _scenario_view(scen) -> dict:
    """Scenario → UI JSON (tests' metadata)."""
    return {
        "name": scen.name,
        "title": scen.title,
        "version": scen.version,
        "cars": scen.cars,
        "buses": scen.buses,
        "rules": scen.rules,
        "warnings": scen.warnings,
        "stages": [
            {
                "id": st.id,
                "title": st.title,
                "warn": st.warn,
                "checklist": st.checklist,
                "tests": [
                    {
                        "id": t.id, "title": t.title, "do": t.do, "warn": t.warn,
                        "car": t.car, "bus": t.bus, "kind": t.kind,
                        "duration": t.duration, "file": t.file, "checks": t.checks,
                        "note": t.note,
                    }
                    for t in st.tests
                ],
            }
            for st in scen.stages
        ],
    }


async def run_service(devices: dict[str, str], host: str = "127.0.0.1", port: int = 8111,
                      out_root: str = "logs", scenarios_dir: str | None = None) -> Service:
    svc = Service(devices, host=host, port=port, out_root=out_root, scenarios_dir=scenarios_dir)
    await svc.start()
    return svc