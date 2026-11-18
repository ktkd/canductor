"""Report: frame statistics and the test protocol form (Appendix C)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from .scenario import TestSpec


@dataclass
class BusStat:
    """Statistics for a single bus."""

    name: str
    frames: list[dict] = field(default_factory=list)   # SSE-events (s "t" = offset c)
    phases: list[tuple[str, float, float]] = field(default_factory=list)  # (phase, t0, t1)
    phase_registered: bool = False

    def _phase_span(self, phase: str) -> tuple[float, float]:
        for p, a, b in self.phases:
            if p == phase:
                return a, b
        if self.phases:
            return self.phases[0][1], self.phases[-1][2] if self.phases[-1] else 0.0
        raise KeyError(phase)

    def _in(self, e: dict, phase: str) -> bool:
        if not self.phases:
            return True
        a, b = self._phase_span(phase)
        t = e.get("t")
        return t is not None and a <= t < b and "__error__" not in e

    # --- aggregate ---------------------------------------------------------------

    def per_phase(self, phase: str) -> tuple[int, float]:
        n = sum(1 for e in self.frames if self._in(e, phase))
        a, b = self._phase_span(phase)
        dur = max(1e-6, b - a)
        return n, n / dur

    def unique_ids(self, phase: str | None = None) -> dict[int, int]:
        out: dict[int, int] = {}
        for e in self.frames:
            if e.get("id") is None or "__error__" in e:
                continue
            if phase is None or self._in(e, phase):
                out[e["id"]] = out.get(e["id"], 0) + 1
        return out

    def changed_ids(self) -> tuple[list[int], list[int]]:
        """(appeared in action, disappeared) — versus the quiet phase."""
        quiet = self._counts("quiet")
        act = self._counts("action")
        appeared = sorted(set(act) - set(quiet))
        dropped = sorted(set(quiet) - set(act))
        return appeared, dropped

    def _counts(self, phase: str) -> dict[int, int]:
        out: dict[int, int] = {}
        for e in self.frames:
            if self._in(e, phase) and e.get("id") is not None:
                out[e["id"]] = out.get(e["id"], 0) + 1
        return out

    def data_changed_ids(self) -> list[int]:
        """IDs whose data changes in action (vs quiet) — candidate signal carriers."""
        def groups(phase: str) -> dict[int, set[int]]:
            g: dict[int, set[int]] = {}
            for e in self.frames:
                if self._in(e, phase) and e.get("id") is not None:
                    g.setdefault(e["id"], set()).add(decode_hex(e.get("data", "")))
            return g
        quiet = groups("quiet")
        act = groups("action")
        return sorted(i for i in set(act) | set(quiet) if act.get(i) != quiet.get(i))

    def total_fps(self) -> float:
        ts = [e["t"] for e in self.frames if isinstance(e.get("t"), (int, float))]
        if len(ts) < 2:
            return 0.0
        span = ts[-1] - ts[0]
        return (len(ts) - 1) / span if span > 0 else 0.0

    def summary(self) -> dict:
        return {
            "frames": sum(1 for e in self.frames if "__error__" not in e),
            "errors": sum(1 for e in self.frames if "__error__" in e),
            "total_fps": round(self.total_fps(), 2),
            "unique_ids_total": len(self._all_ids()),
        }

    def _all_ids(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for e in self.frames:
            if e.get("id") is not None:
                out[e["id"]] = out.get(e["id"], 0) + 1
        return out

    def register_phase(self, phase: str, t0: float, t1: float) -> None:
        self.phases.append((phase, t0, t1))
        self.phase_registered = True


def decode_hex(h: str) -> int:
    h = (h or "").replace(" ", "")
    return int(h, 16) if h else 0


def fmt_id(i: int) -> str:
    return f"0x{i:03X}" if i <= 0x7FF else f"0x{i:08X}"


def render_report(
    *,
    test: TestSpec,
    rep: int,
    repeat_total: int,
    car: str,
    buses: dict[str, BusStat],
    notes: dict,
    started_at: float,
    phases_duration: list[tuple[str, float, float]] | None = None,
) -> str:
    now = datetime.now()
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"Test protocol {test.id} · repeat {rep}/{repeat_total} · {car.upper()}")
    lines.append("=" * 72)
    rows = [
        ("Date/time", now.strftime("%Y-%m-%d %H:%M")),
        ("Car", car),
        ("Bus", " + ".join(b.upper() for b in buses)),
        ("Scenario stage", f"{test.stage_id} {test.stage_title}"),
        ("Test id/title", f"{test.id} {test.title}"),
        ("Action", test.do or test.title),
        ("Coolant \u00b0C", notes.get("coolant", "\u2014")),
        ("Ambient \u00b0C", notes.get("air", "\u2014")),
        ("Engine state", notes.get("engine", "\u2014")),
        ("Comment", notes.get("comment", "\u2014")),
        ("Log file", notes.get("file", "\u2014")),
        ("Repeat", f"{rep}/{repeat_total}"),
    ]
    for k, v in rows:
        lines.append(f"{k:30s}: {v}")
    phases = phases_duration or (list(first_bus.phases) if (first_bus := next(iter(buses.values()), None)) else [])
    for (ph, t0, t1) in phases:
        lines.append(f"  phase {ph:6s}: {int(t1 - t0)}s (from {int(t0)}s to {int(t1)}s)")

    for bname, st in buses.items():
        sb = st.summary()
        lines.append("")
        lines.append(f"--- {bname.upper()} ---")
        lines.append(f"frames: {sb['frames']}, fps: {sb['total_fps']}, errors: {sb['errors']}, unique IDs: {sb['unique_ids_total']}")
        for ph in ("quiet", "action", "after"):
            try:
                n, fps = st.per_phase(ph)
            except KeyError:
                n, fps = 0, 0.0
            lines.append(f"  {ph:6s}: {n:6d} frames \u00b7 {fps:7.1f} fps")
        q = st._counts("quiet")
        a = st._counts("action")
        top = sorted(st._all_ids().items(), key=lambda kv: -kv[1])[:20]
        lines.append("  top ID: " + (", ".join(f"{fmt_id(i)}×{n}" for i, n in top) if top else "—"))
        appeared, dropped = st.changed_ids()
        lines.append(f"  appeared in action: " + (", ".join(fmt_id(i) for i in appeared) if appeared else "\u2014"))
        lines.append(f"  dropped in action : " + (", ".join(fmt_id(i) for i in dropped) if dropped else "\u2014"))
        dc = st.data_changed_ids()
        if dc:
            lines.append("  data changed (ID) : " + ", ".join(fmt_id(i) for i in dc))
        if test.checks:
            allids = st._all_ids()
            ok = []
            missing = []
            for c in test.checks:
                try:
                    cid = int(c, 16)
                except (TypeError, ValueError):
                    continue    # malformed id — validated at load, defensive here
                (ok if cid in allids else missing).append(c)
            got = "OK: " + ", ".join(ok) if ok else "\u2014"
            lines.append(f"  expected IDs: {', '.join(test.checks)} -> {got}"
                         + (f" \u00b7 not captured: {', '.join(missing)}" if missing else ""))
    dur = time.time() - started_at
    lines.append("")
    lines.append(f"recording duration: {int(dur)}s \u00b7 finished {now.strftime('%H:%M:%S')}")
    return "\n".join(lines) + "\n"