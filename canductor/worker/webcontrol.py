"""Web Control: Runner ←→ web UI (events queue + command queue + status)."""
from __future__ import annotations

import copy
import queue
import threading
import time


class WebHub:
    """Thread-safe state hub (control → UI events, UI commands → control)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state: dict = {
            "session_state": "idle",      # idle|running|waiting|paused|done|error
            "plan": [],
            "summary": {},
            "card": None,                 # current test card
            "phase": None,                # {test_id, name, until, dur}
            "done": [],                  # finished recordings
            "last_error": "",
        }
        self.cmd_q: queue.Queue = queue.Queue()
        self._subs: set = set()

    def event(self, kind: str, **kw) -> None:
        ev = {"type": kind, "ts": time.time()}
        ev.update(kw)
        with self.lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except Exception:
                pass

    def update(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)
            snap = copy.deepcopy(self.state)  # callers mutate snapshots freely
        self.event("status", state=snap)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self._subs.discard(q)

    def status(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.state)


class WebControl:
    """Control implementation, powered by WebHub."""

    def __init__(self, hub: WebHub) -> None:
        self.hub = hub

    def session_start(self, plan: list[dict], summary: dict) -> None:
        self.hub.update(session_state="running", plan=plan, summary=summary,
                         done=[], phase=None, card=None)
        self.hub.event("session_start", plan=plan, summary=summary)

    def session_done(self, session_dir: str) -> None:
        self.hub.update(session_state="done", phase=None,
                         session_dir=session_dir)
        self.hub.event("session_done", session_dir=session_dir)

    def error(self, msg: str) -> None:
        self.hub.update(session_state="error", last_error=msg)
        self.hub.event("error", message=msg)

    def log_line(self, text: str) -> None:
        self.hub.event("log", line=text)

    # --- per-test ------------------------------------------------------------

    def test_card(self, stage_id, stage_title, test, car, rep, rep_total, file_preview):
        card = {
            "test_id": test.id, "stage_id": stage_id, "stage_title": stage_title,
            "title": test.title, "do": test.do, "warn": test.warn,
            "kind": test.kind, "duration": test.duration,
            "car": car, "rep": rep, "rep_total": rep_total,
            "buses": test.bus, "checks": test.checks,
            "file_preview": file_preview,
        }
        self.hub.update(session_state="waiting", card=card, phase=None)
        self.hub.event("card", card=card)
        cmd = self.hub.cmd_q.get()
        if cmd.get("action") == "start":
            self.hub.update(session_state="running")
        elif cmd.get("action") == "skip":
            self.hub.update(session_state="idle")
            self.hub.event("skipped", card=card)
        else:
            self.hub.update(session_state="done")
        return (cmd.get("note") or {}), cmd.get("action", "start")

    def phase(self, test_id, car, phase, dur_s) -> None:
        until = time.time() + dur_s
        self.hub.update(session_state="recording",
                        phase={"test_id": test_id, "car": car,
                               "name": phase, "until": until, "dur": dur_s})
        self.hub.event("phase", test_id=test_id, car=car, phase=phase,
                       dur=dur_s, until=until)

    def pause(self, dur_s: float) -> None:
        until = time.time() + max(0.5, dur_s)
        # server-side `until` — the UI countdown shares the server clock (no NTP drift)
        self.hub.update(session_state="paused")
        self.hub.event("pause", dur=dur_s, until=until)
        time.sleep(max(0.5, dur_s))

    def test_done(self, info: dict) -> None:
        with self.hub.lock:
            self.hub.state.setdefault("done", []).append(info)
        self.hub.update(session_state="idle")
        self.hub.event("done", **info)
