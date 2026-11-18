"""Test scenario model + loading from JSON.

JSON schema (v1):
  {
    "name", "title", "version",
    "cars":    {alias: {label}},
    "buses":   {alias: {label, can_baud, point}},
    "rules":   {quiet_s, after_s, repeats, pause_s, action_min_s, action_max_s},
    "known_ids": {car: [{id, params:[{name, bytes, formula}]}]},
    "warnings": [str],
    "stages": [
      {"id": "1", "title": "...", "warn": "...",
       "tests": [
         {"id": "1.1.1", "car": "rx8" | ["rx8","nd"], "bus": "hs" | ["hs","ms"],
          "kind": "state" | "action", "title": "...", "do": "...",
          "duration": seconds (number | dict{min,max} | string "open-ended"),
          "file": "base name without the date", "checks": ["0x201", ...],
          "tx": [{"bus": "hs", "hz": 10, "frames": [{"id","data"}...],
                  "phases": ["quiet","action","after"]}],
               # shorthand: a bare id string injects an empty frame at 10 Hz
               # on the test's own bus: "tx": ["0x7DF", ...]
          "note": "..."}]
      }
    ]
  }

Scenario lookup: a bare name is resolved against $CANDUCTOR_SCENARIOS,
<project>/scenarios/ (next to the package) and ./scenarios/ (cwd);
a path (absolute, or relative to cwd, with or without .json) is used as-is.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ScenarioError(ValueError):
    pass


# --- location ----------------------------------------------------------------


def scenario_roots() -> list[Path]:
    """Directories that may hold scenario .json files, in precedence order."""
    roots: list[Path] = []
    env = os.environ.get("CANDUCTOR_SCENARIOS")
    if env:
        roots.append(Path(env).expanduser())
    # <project>/scenarios — one level above the package (works in a checkout
    # and in an editable install)
    roots.append(Path(__file__).resolve().parent.parent.parent / "scenarios")
    # ./scenarios — running from inside a checkout
    roots.append(Path.cwd() / "scenarios")
    out: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        r = r.expanduser()
        if r.is_dir() and r not in seen:
            out.append(r)
            seen.add(r)
    return out


def find_scenario_file(name: str) -> Path | None:
    """Resolve an explicit path or a bare scenario name to a .json file."""
    p = Path(name)
    if p.is_file():
        return p
    if os.sep in name or p.suffix == ".json":
        c = p.with_suffix(".json")
        return c if c.is_file() else None
    for root in scenario_roots():
        c = root / f"{name}.json"
        if c.is_file():
            return c
    return None


@dataclass
class KnownId:
    id: str
    params: list[dict] = field(default_factory=list)


@dataclass
class TxPattern:
    bus: str
    hz: float = 10.0
    frames: list[dict] = field(default_factory=list)
    phases: list[str] | None = None  # None = all phases


@dataclass
class TestSpec:
    id: str
    stage_id: str
    stage_title: str
    car: list[str]           # ["rx8"] or ["rx8","nd"] (sequentially)
    bus: list[str]           # ["hs"] or ["hs","ms"] (simultaneously)
    kind: str                # state | action
    title: str
    do: str = ""
    duration: float | None = 10.0  # seconds; None = open-ended (recording capped by rules.state_max_s)
    duration_open: bool = False
    file: str = ""           # log file name base (without the date)
    warn: str = ""
    checks: list[str] = field(default_factory=list)
    tx: list[TxPattern] = field(default_factory=list)
    note: str = ""
    variable: bool = False
    bus_override: dict | None = None  # per-test cfg override for all its buses


@dataclass
class Stage:
    id: str
    title: str
    warn: str = ""
    tests: list[TestSpec] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    title: str
    version: str
    cars: dict[str, dict]
    buses: dict[str, dict]
    rules: dict
    known_ids: dict[str, list[KnownId]]
    warnings: list[str]
    stages: list[Stage]

    # --- convenience ------------------------------------------------------

    @property
    def all_tests(self) -> list[tuple[Stage, TestSpec]]:
        return [(st, t) for st in self.stages for t in st.tests]

    def test_ids(self) -> list[str]:
        return [t.id for _, t in self.all_tests]

    def tests_by_id(self, ids: list[str]) -> list[tuple[Stage, TestSpec]]:
        want: dict[str, bool] = {}
        for i in ids:
            key = i.strip()
            ex = next((x for x in self.test_ids() if _match(x, key)), None)
            if ex is None:
                raise ScenarioError(f"test '{i}' not found")
            want[ex] = True
        res: dict[str, tuple] = {}
        for st, t in self.all_tests:
            if t.id in want:
                res[t.id] = (st, t)
        return list(res.values())

    def activate(self, t: TestSpec) -> list[dict]:
        """Config dicts for the test buses (can_baud from buses)."""
        out = []
        for b in t.bus:
            bs = self.buses.get(b, {})
            if b not in self.buses:
                raise ScenarioError(f"bus {b} not described in scenario buses")
            out.append({"bus": b, "can_baud": int(bs.get("can_baud", 500_000)),
                        "standard_only": bool(bs.get("std_only", True))})
        return out


def _match(test_id: str, key: str) -> bool:
    """Match: exact, prefix (stage.item) or keywords."""
    return test_id == key or test_id.startswith(key + ".") or key in test_id


def load_scenario(path: str) -> Scenario:
    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = json.load(f)
    try:
        rules = raw.get("rules") or {}
        sc = Scenario(
            name=raw.get("name", "scenario"),
            title=raw.get("title", ""),
            version=str(raw.get("version", "")),
            cars=raw.get("cars", {}),
            buses=raw.get("buses", {}),
            rules={
                "quiet_s": float(rules.get("quiet_s", 5)),
                "after_s": float(rules.get("after_s", 5)),
                "repeats": int(rules.get("repeats", 3)),
                "pause_s": float(rules.get("pause_s", 10)),
                "action_min_s": float(rules.get("action_min_s", 5)),
                "action_max_s": float(rules.get("action_max_s", 15)),
                "state_max_s": float(rules.get("state_max_s", 120)),
            },
            known_ids={
                car: [KnownId(id=e.get("id", ""), params=e.get("params", []))
                      for e in lst]
                for car, lst in (raw.get("known_ids") or {}).items()
            },
            warnings=raw.get("warnings", []),
            stages=_stages(raw),
        )
    except ScenarioError:
        raise
    _validate(sc)
    return sc


def _stages(raw: dict) -> list[Stage]:
    stages: list[Stage] = []
    for sr in raw.get("stages", []):
        st = Stage(
            id=str(sr.get("id", "")),
            title=sr.get("title", ""),
            warn=sr.get("warn", ""),
            checklist=sr.get("checklist", []),
        )
        for tr in sr.get("tests", []):
            t = _test(st, tr)
            if t:
                st.tests.append(t)
        stages.append(st)
    return stages


def _test(st: Stage, tr: dict) -> TestSpec | None:
    if not isinstance(tr, dict) or not tr.get("id"):
        return None
    dur = tr.get("duration", 10)
    open_ = False
    if isinstance(dur, dict):
        open_ = bool(dur.get("open"))
        dur = float(dur.get("max", dur.get("min", 10)))
    elif dur == "open-ended":
        open_ = True
        dur = None
    else:
        dur = float(dur)
    bus_raw = tr.get("bus", ["hs"])
    bus_list = bus_raw if isinstance(bus_raw, list) else [bus_raw]
    default_bus = bus_list[0] if bus_list else "hs"
    txs: list[TxPattern] = []
    for x in tr.get("tx", []):
        if isinstance(x, str):  # shorthand: bare id, inject on the test's bus
            x = {"bus": default_bus, "frames": [{"id": x}]}
        txs.append(TxPattern(
            bus=x.get("bus", default_bus), hz=float(x.get("hz", 10)),
            frames=x.get("frames", []), phases=x.get("phases"),
        ))
    return TestSpec(
        id=str(tr["id"]),
        stage_id=st.id,
        stage_title=st.title,
        car=tr.get("car", ["rx8"]) if isinstance(tr.get("car"), list) else [tr.get("car", "rx8")],
        bus=bus_list,
        kind=tr.get("kind", "action"),
        title=tr.get("title", tr["id"]),
        do=tr.get("do", ""),
        duration=dur,
        duration_open=open_,
        file=tr.get("file", ""),
        warn=tr.get("warn", ""),
        checks=tr.get("checks", []),
        tx=txs,
        note=tr.get("note", ""),
        variable=bool(tr.get("variable", False)),
        bus_override=tr.get("bus_override")
    )


def _validate(sc: Scenario) -> None:
    for car in sc.cars:
        if not isinstance(sc.cars[car], dict):
            raise ScenarioError(f"car '{car}' must be an object")
    for b, bs in sc.buses.items():
        if not bs:
            raise ScenarioError(f"bus {b}: empty description")
    seen: set[str] = set()
    for st, t in sc.all_tests:
        if t.id in seen:
            raise ScenarioError(f"duplicate test id {t.id}")
        seen.add(t.id)
        if t.kind not in ("state", "action", "setup"):
            raise ScenarioError(f"test {t.id}: invalid kind '{t.kind}'")
        if not t.car:
            raise ScenarioError(f"test {t.id}: car not set")
        if not t.bus:
            raise ScenarioError(f"test {t.id}: bus not set")
        if t.duration is not None and t.duration <= 0:
            raise ScenarioError(f"test {t.id}: duration must be > 0 (got {t.duration})")
        # checks are parsed with int(x, 16) at report time — fail at load instead
        for c in t.checks:
            try:
                int(c, 16)
            except (TypeError, ValueError):
                raise ScenarioError(f"test {t.id}: check id '{c}' is not a hex id") from None
        for p in t.tx:
            if not p.frames:
                raise ScenarioError(f"test {t.id}: tx pattern without frames")
            for f in p.frames:
                if f.get("id") is None:
                    raise ScenarioError(f"test {t.id}: tx frame without id")
                try:
                    int(f["id"], 0) if isinstance(f["id"], str) else int(f["id"])
                except (TypeError, ValueError):
                    raise ScenarioError(f"test {t.id}: tx frame id '{f['id']}' not numeric") from None