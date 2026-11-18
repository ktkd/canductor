"""Worker: the interactive "prepare → confirm → record" loop.

For each scenario test the runner:
  * builds the recording on the required buses (HS and/or MS) simultaneously
    through core;
  * asks the operator for prep (preparation) with confirmation;
  * drives the phases: quiet (silence) → action (target action) →
    after (post-action);
  * writes per-bus .log (candump-style with timestamps) + report.txt +
    merged.log;
  * applies the rules: 3 repeats, pause ≥ 10 s between tests,
    preparation/confirm at every step.

The link with core is over HTTP (Channel); scenario bus names ("hs"/"ms")
are mapped to device aliases in core.
"""
from __future__ import annotations

import json
import queue
import sys
import time
import threading
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path

from ..common import C, ask, confirm, err, hr, say
from .channel import CoreClient, CoreError, SSEConsumer
from .report import BusStat, fmt_id, render_report
from .control import LocalControl
from .scenario import Scenario, Stage, TestSpec

QUIET_DEFAULT = 5.0
AFTER_DEFAULT = 5.0
PAUSE_DEFAULT = 10.0
REPEATS_DEFAULT = 3


@dataclass
class SessionOpts:
    scenario: Scenario
    out_root: Path
    core: CoreClient
    device_aliases: dict[str, str] = field(default_factory=dict)  # bus→device name in core
    repeats: int = REPEATS_DEFAULT
    quiet: float = QUIET_DEFAULT
    after: float = AFTER_DEFAULT
    pause: float = PAUSE_DEFAULT
    scale: float = 1.0
    only: list[str] = field(default_factory=list)   # test ids (or prefixes) to run
    skip: list[str] = field(default_factory=list)   # test ids to skip
    cars: list[str] = field(default_factory=list)   # only these cars
    buses: list[str] = field(default_factory=list)  # only these buses
    auto_confirm: bool = False
    control: "Any" = None
    inject_ids: dict[str, list[int]] = field(default_factory=dict)  # bus->ids to inject (for bench)
    dry_run: bool = False
    verbose: bool = True

    def s(self, x: float) -> float:
        return x * self.scale


@dataclass
class SpecRun:
    stage: Stage
    test: TestSpec
    car: str
    rep: int  # 1-based


class Recorder:
    """Record frames from N buses simultaneously into log files + stats."""

    def __init__(self, core: CoreClient, devices: list[str], out_dir: Path,
                 inject: dict[str, list[dict]] | None = None,
                 hz: dict[str, float] | float = 10.0, auto: bool = False, verbose: bool = True) -> None:
        self.core = core
        self.devices = devices
        self.out_dir = out_dir
        self.inject = inject or {}
        self.hz = hz
        self.auto = auto
        self.verbose = verbose
        self.start_ts = 0.0
        self._consumers: dict[str, SSEConsumer] = {}
        self._stats: dict[str, BusStat] = {}
        self._files: dict[str, object] = {}
        self._queues: dict[str, queue.Queue] = {}
        self._writers: dict[str, threading.Thread] = {}
        self._phase = "idle"
        self._phase_ends: list[float] = []   # cumulative ends of the phases
        self._stop = threading.Event()
        self._inject_stop = threading.Event()
        self._injecters: dict[str, threading.Thread] = {}
        self._merged: list[tuple[float, str, str]] = []

    def open(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.start_ts = time.monotonic()
        for dev in self.devices:
            q: queue.Queue = queue.Queue()
            self._queues[dev] = q
            self._stats[dev] = BusStat(name=dev)
            logf = open(self.out_dir / f"{dev}.log", "a", buffering=1, encoding="utf-8")
            self._files[dev] = logf
            c = SSEConsumer(self.core.base, dev)
            c.q = q
            c.start()
            self._consumers[dev] = c
            t = threading.Thread(target=self._writer, args=(dev,), daemon=True)
            t.start()
            self._writers[dev] = t
            if self.inject.get(dev):
                it = threading.Thread(target=self._inject_loop, args=(dev,), daemon=True)
                it.start()
                self._injecters[dev] = it

    def _writer(self, dev: str) -> None:
        st = self._stats[dev]
        f = self._files[dev]
        q = self._queues[dev]
        while True:
            try:
                ev = q.get(timeout=0.05)
            except queue.Empty:
                # stop only after the queue is drained — no frame lost
                if self._stop.is_set():
                    break
                continue
            t = time.monotonic() - self.start_ts
            if "__error__" in ev:
                line = f"{t:.6f} {dev.lower()} !ERROR {ev['__error__']}"
                f.write(line + "\n")
                st.frames.append({"__error__": ev["__error__"], "t": t})
                continue
            fid = ev["id"]
            ext = bool(ev.get("ext"))
            remote = bool(ev.get("remote"))
            dlc = int(ev.get("dlc", 0))
            data = ev.get("data", "")
            idtag = f"{fid:03X}" if not ext else f"{fid:08X}"
            kind = "r" if remote else "d"
            line = f"{t:.6f} {dev.lower()} {idtag} Rx {kind} {dlc}"
            if data:
                line += " " + " ".join(bytes.fromhex(data)[i:i+2].hex() for i in range(0, len(data), 2))
            f.write(line + "\n")
            frame = {"id": fid, "ext": ext, "remote": remote, "dlc": dlc, "data": data, "t": t}
            st.frames.append(frame)
            self._merged.append((t, dev.lower(), line))

    def _inject_loop(self, dev: str) -> None:
        """Send periodic frames into a bus (for isolated bench pairs).

        inject[dev] = [{'id': int, 'data': hex-str, 'ext': bool?}, ...]
        """
        frames = self.inject.get(dev, [])
        if not frames:
            return
        hz_v = self.hz.get(dev, 10.0) if isinstance(self.hz, dict) else self.hz
        hz_v = max(0.5, float(hz_v))
        i = 0
        while not self._inject_stop.is_set():
            fr = frames[i % len(frames)]
            i += 1
            try:
                d = dict(fr)
                self.core.send(dev, d)
            except CoreError:
                pass
            self._inject_stop.wait(1.0 / hz_v)

    def phase(self, name: str, dur: float, label: str | None = None) -> None:
        """Run a phase (duration is already scaled) with countdown."""
        self._phase = name
        t0 = time.monotonic()
        say(f"  {C.CYAN}▶ {label or name} {int(dur)}″{C.RESET}")
        deadline = t0 + dur
        last_shown = -1
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - t0
            if int(elapsed) != last_shown and sys.stdout.isatty():
                last_shown = int(elapsed)
                say(f"   {C.DIM}… {elapsed:4.1f}/{dur:.0f}″{C.RESET}")
            time.sleep(0.2)
        # phase span, cumulative from the recording start (shared time base)
        t0off = self._phase_ends[-1] if self._phase_ends else 0.0
        t1off = time.monotonic() - self.start_ts
        self._phase_ends.append(t1off)
        for st in self._stats.values():
            st.register_phase(name, t0off, t1off)

    def stop(self) -> None:
        # 1) close the SSE sources, 2) let the writers drain their queues,
        # 3) only then close the files and build merged.log — otherwise a
        #    writer can race the closed file / the merged sort and lose frames.
        self._inject_stop.set()
        for c in self._consumers.values():
            c.stop()
        self._stop.set()
        for w in self._writers.values():
            w.join(2.0)
        for th in self._injecters.values():
            th.join(1.0)
        for f in self._files.values():
            try:
                f.close()
            except OSError:
                pass
        self._merged.sort(key=lambda x: x[0])
        with open(self.out_dir / "merged.log", "w", encoding="utf-8") as mf:
            for _, _, line in self._merged:
                mf.write(line + "\n")

    def stats(self) -> dict[str, BusStat]:
        return self._stats

    def last_phase_end(self) -> float:
        for st in self._stats.values():
            if st.phases:
                return st.phases[-1][2]
        return 0.0

    def phase_spans(self) -> list[tuple[str, float, float]]:
        for st in self._stats.values():
            return list(st.phases)
        return []

# --- selection ----------------------------------------------------------


def _id_match(test_id: str, key: str) -> bool:
    """Exact id or a dot-boundary prefix ('5.1' → '5.1.x', not '5.10.x')."""
    return test_id == key or test_id.startswith(key + ".")


class Runner:
    def __init__(self, opts: SessionOpts) -> None:
        self.opts = opts

    # -- selection ----------------------------------------------------------

    def selected(self) -> list[SpecRun]:
        o = self.opts
        runs: list[SpecRun] = []
        for st in o.scenario.stages:
            for t in st.tests:
                if t.kind == "setup":
                    continue
                if o.only and not any(_id_match(t.id, k) for k in o.only):
                    continue
                if any(_id_match(t.id, k) for k in o.skip):
                    continue
                for car in t.car:
                    if o.cars and car not in o.cars:
                        continue
                    ok_bus = True
                    if o.buses:
                        ok_bus = any(b in o.buses for b in t.bus)
                    if not ok_bus:
                        continue
                    for rep in range(1, o.repeats + 1):
                        runs.append(SpecRun(stage=st, test=t, car=car, rep=rep))
        return runs

    # -- one session --------------------------------------------------------------

    def _resolve_pacing(self) -> None:
        """Fill None pacing fields from scenario rules (rules are the single
        source of truth; explicit CLI/UI values win)."""
        o = self.opts
        r = o.scenario.rules
        o.repeats = o.repeats if o.repeats is not None else int(r.get("repeats", REPEATS_DEFAULT))
        o.quiet = o.quiet if o.quiet is not None else float(r.get("quiet_s", QUIET_DEFAULT))
        o.after = o.after if o.after is not None else float(r.get("after_s", AFTER_DEFAULT))
        o.pause = o.pause if o.pause is not None else float(r.get("pause_s", PAUSE_DEFAULT))
        self._action_min = float(r.get("action_min_s", 5.0))
        self._action_max = float(r.get("action_max_s", 15.0))
        self._state_max = float(r.get("state_max_s", 120.0))

    def _test_duration(self, t) -> tuple[float, str]:
        """(seconds, why) for the recording phase of a test, rules-clamped:
        action bounded by action_min_s/action_max_s; state capped by
        state_max_s; open-ended resolves to the state_max_s cap."""
        if t.duration is None:
            base, why = self._state_max, "open-ended (capped at state max)"
        elif t.kind == "state":
            base, why = t.duration, "state (the condition itself; duration = how long it holds)"
        else:
            base, why = t.duration, "action (target action)"
        if t.kind == "state":
            act = min(base, self._state_max)
        else:
            act = min(max(base, self._action_min), self._action_max)
        return max(0.5, self.opts.s(act)), why

    def run(self) -> int:
        o = self.opts
        self._resolve_pacing()
        runs = self.selected()
        say(C.BOLD + f"Plan: {len(runs)} recordings{C.RESET}")
        if o.dry_run:
            for r in runs:
                say(f"  {r.test.id} / {r.car} / rep {r.rep} / {r.stage.title}")
            return 0
        session_dir = o.out_root / f"session_{time.strftime('%Y%m%d_%H%M%S')}"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "scenario.json").write_text(
            json.dumps({"title": o.scenario.title, "version": o.scenario.version, "no": o.scenario.name}, ensure_ascii=False),
            encoding="utf-8")

        self._control().session_start(
            [{"id": r.test.id, "car": r.car, "rep": r.rep, "stage": r.stage.title} for r in runs],
            {"scenario": o.scenario.name, "out": str(session_dir)})

        first = True
        seen_stages: set[str] = set()
        for r in runs:
            if r.stage.id not in seen_stages:
                seen_stages.add(r.stage.id)
                self._stage_header(r.stage)
            if not first:
                self._pause()
            first = False
            ok = self._do_one(r, session_dir)
            if not ok:
                break
        self._summary(session_dir)
        self._control().session_done(str(session_dir))
        return 0

    def _stage_header(self, st: Stage) -> None:
        say("")
        hr()
        say(C.BOLD + C.BLUE + f"STAGE {st.id} · {st.title}{C.RESET}{C.RESET}")
        if st.warn:
            say(C.YELLOW + f"  ⚠ {st.warn}{C.RESET}")
        if st.checklist:
            for i, item in enumerate(st.checklist, 1):
                say(f"  {i}. {item}")
        hr()

    def _build_inject(self, t, o) -> dict:
        """Builds the TX patterns (frame injection) for the buses (bench ECU)."""
        out: dict[str, list[dict]] = {}
        for p in t.tx:
            if p.bus not in t.bus:
                continue
            frames = []
            for f in p.frames:
                idv = f.get("id")
                if isinstance(idv, str):
                    idv = int(idv, 0)
                fr = {"id": idv}
                if f.get("data") is not None:
                    fr["data"] = f["data"] if isinstance(f["data"], str) else bytes(f["data"]).hex()
                if f.get("ext"):
                    fr["ext"] = True
                if f.get("remote"):
                    fr["remote"] = True
                frames.append(fr)
            out.setdefault(p.bus, []).extend(frames)
        for b, ids in o.inject_ids.items():
            out.setdefault(b, []).extend({"id": i} for i in ids)
        return out

    def _ensure_bus(self, name: str, bs_cfg: dict) -> None:
        """Core: open + configuration in case of rate change (low-cost call).

        Fails cleanly (single message) when the device is not connected.
        """
        try:
            cur = self.opts.core.status(name)
        except CoreError:
            cur = {}
        want_rate = int(bs_cfg.get("can_baud", 500_000))
        std_only = bool(bs_cfg.get("std_only", True))
        variable = bool(bs_cfg.get("variable", False))
        cfg = {"can_baud": want_rate, "standard_only": std_only, "variable": variable}
        try:
            if cur.get("state") != "configured":
                self.opts.core.open(name, cfg)
            else:
                c = cur.get("config") or {}
                if c.get("can_baud") != want_rate or c.get("standard_only") != std_only or bool(c.get("variable_encoding", False)) != variable:
                    self.opts.core.reconfigure(name, cfg)
        except CoreError as e:
            raise CoreError(f"bus '{name}': {e} (device not connected or not openable?)") from e

    def _control(self):
        if self.opts.control is None:
            if not hasattr(self, "_lc"):
                self._lc = LocalControl(auto=self.opts.auto_confirm)
            return self._lc
        return self.opts.control

    def _pause(self) -> None:
        p = self.opts.s(self.opts.pause)
        self._control().pause(p)

    def _do_one(self, r: SpecRun, session_dir: Path) -> bool:
        o = self.opts
        t = r.test
        act, why = self._test_duration(t)
        say("")
        hr()
        say(C.BOLD + f"STAGE {t.stage_id} · {t.stage_title}{C.RESET}")
        say(C.BOLD + f"Test {t.id} — {t.title}{C.RESET}" + (f"  ({r.car})" if len(t.car) > 1 else ""))
        if t.warn:
            say(C.YELLOW + f"  ⚠ {t.warn}{C.RESET}")
        if t.do:
            say(f"  action: {t.do}")
        say(f"  buses: {', '.join(t.bus)} · record ~{int(act)}s · rep {r.rep}/{o.repeats}")
        if o.verbose and t.checks:
            say(f"  expect ID: {', '.join(t.checks)}")
        hr()

        # test card: preparation + confirmation via control
        date = time.strftime("%Y-%m-%d")
        file_preview = (f"{r.car}_{t.file}_{date}.log" if t.file else f"{t.id}_{r.car}_{date}.log")
        note, action = self._control().test_card(
            t.stage_id, t.stage_title, t, r.car, r.rep, o.repeats, file_preview)
        if action == "quit":
            say(C.DIM + "\u2014 quit" + C.RESET)
            return False
        if action == "skip":
            return True
        note.update({"car": r.car, "rep": r.rep, "file": file_preview})

        # prepare devices: ensure each bus is in the same speed the test needs
        for b in t.bus:
            bs_cfg = dict(self.opts.scenario.buses[b])
            if getattr(t, "bus_override", None):
                bs_cfg.update(t.bus_override)
            if t.variable:
                bs_cfg["variable"] = True
            self._ensure_bus(b, bs_cfg)
        tot = 0.0
        inject = self._build_inject(t, o)
        hz: dict[str, float] = {}
        for p in t.tx:
            hz[p.bus] = p.hz
        rec = Recorder(o.core, t.bus, session_dir / f"{t.id}_{r.car}_rep{r.rep}",
                       inject=inject, hz=hz, auto=o.auto_confirm)
        rec.open()
        ct = self._control()
        try:
            ct.phase(t.id, r.car, "quiet", o.s(o.quiet))
            rec.phase("quiet", o.s(o.quiet), label="quiet (stable)")
            ct.phase(t.id, r.car, "action", act)
            rec.phase("action", act, label=why)
            ct.phase(t.id, r.car, "after", o.s(o.after))
            rec.phase("after", o.s(o.after), label="post-action")
        except KeyboardInterrupt:
            say(C.YELLOW + "— interrupted" + C.RESET)
        rec.stop()
        tot = rec.last_phase_end()

        st = rec.stats()
        report = render_report(test=t, rep=r.rep, repeat_total=o.repeats, car=r.car,
                               buses=st, notes=note,
                               started_at=time.time() - tot if tot else time.time(),
                               phases_duration=rec.phase_spans())
        td = session_dir / f"{t.id}_{r.car}_rep{r.rep}"
        rp = td / "report.txt"
        rp.write_text(report, encoding="utf-8")
        try:
            (td / "notes.json").write_text(json.dumps(note, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        self._control().test_done({
            "test": t.id, "car": r.car, "rep": r.rep,
            "dir": str(td), "rel": f"{session_dir.name}/{td.name}",
            "report": str(rp), "session": str(session_dir),
        })
        return True

    def _summary(self, session_dir: Path) -> None:
        say("")
        hr()
        say(C.BOLD + f"Session finished: {session_dir}{C.RESET}")
        hr()
