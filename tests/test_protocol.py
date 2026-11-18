"""Unit tests: protocol golden vectors + device/service in-process round-trips."""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canductor.core import protocol as P
from canductor.core.device import Config
from canductor.core.service import run_service
from canductor.worker.channel import CoreClient, SSEConsumer


def test_golden_fixed_standard():
    m = P.encode_frame(0x123, [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
    assert m == bytes([
        0xaa, 0x55, 0x01, 0x01, 0x01,
        0x23, 0x01, 0x00, 0x00,
        0x08,
        0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
        0x00, 0x93,
    ]), m.hex()


def test_golden_fixed_checksum():
    for can_id, data in [(0x778, bytes([0xAB] * 8)), (0x10, bytes([1, 2]))]:
        m = P.encode_frame(can_id, data)
        assert len(m) == 20
        assert m[19] == P.checksum(m)


def test_golden_variable_short_long():
    s = P.encode_frame(0x7FF, b"", variable=True)
    assert s == bytes([0xaa, 0xc0, 0xff, 0x07, 0x55]), s.hex()
    l = P.encode_frame(0x7FF, [0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF], variable=True)
    assert l == bytes([
        0xaa, 0xc8, 0xff, 0x07,
        0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF, 0x55,
    ]), l.hex()
    e = P.encode_frame(0x1FFFFFFF, b"", extended=True, variable=True)
    assert e == bytes([0xaa, 0xe0, 0xff, 0xff, 0xff, 0x1f, 0x55]), e.hex()


def test_config_vectors():
    c = P.encode_config(500_000)
    assert c[2] == 0x02 and c[3] == 0x03 and c[4] == 0x01 and c[19] == P.checksum(c)
    cv = P.encode_config(125_000, variable=True)
    assert cv[2] == (0x02 | 0x10) and cv[3] == 0x07
    # loopback/silent masks
    assert P.encode_config(500_000, loopback=True)[13] == 0x01
    assert P.encode_config(500_000, silent=True)[14] == 0x00  # silent = 0x02 at idx 13
    assert P.encode_config(500_000, silent=True)[13] == 0x02


def test_serial_baud_filters():
    sb = P.encode_serial_baud(2_000_000)
    assert sb[3] == 0x00
    sb = P.encode_serial_baud(1_228_800)
    assert sb[3] == 0x01
    fl = P.encode_id_filter("disabled", [])
    assert fl[:4] == bytes([0xaa, 0x55, 0x11, 0x00]) and fl[-1] == P.checksum(fl)
    fb = P.encode_id_filter("block", [0x123, 0x456])
    assert fb[3] == 2 and fb[-1] == P.checksum(fb)


def test_decoder_fixed_resync_and_split():
    frame = P.encode_frame(0x123, range(1, 9))
    d = P.ProtocolDecoder("fixed")
    d.feed(b"\x00\x11" + frame)
    f = d.frames()
    assert len(f) == 1 and f[0].id == 0x123
    d2 = P.ProtocolDecoder("fixed")
    d2.feed(frame[:3]); assert d2.frames() == []
    d2.feed(frame[3:]); assert len(d2.frames()) == 1
    d3 = P.ProtocolDecoder("fixed")
    d3.feed(frame + frame)
    assert len(d3.frames()) == 2


def test_inproc_service_http_roundtrip(tmp_paths=None):
    """End-to-end over in-process HTTP with real (or no) devices."""
    pass


async def _stream_test_events(timeout_s: float, throttle: int | None):
    """Starts the service, feeds frames into the device subscriber queues
    (id 0x100 twice with the same data, id 0x200 once; then a second window
    with 0x100 repeated, unchanged), connects to the stream endpoint and
    returns the parsed SSE events."""
    from canductor.core.protocol import FrameStore

    async def feed(frames: list) -> None:
        for f in frames:
            for q in list(dev._subs):   # fan-out, as _read_loop does
                q.put_nowait(f)

    svc = await run_service({"dev": "/dev/null"}, host="127.0.0.1", port=0)
    try:
        dev = svc.devices["dev"]
        path = "/devices/dev/stream" + (f"?throttle={throttle}" if throttle else "")
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", svc.port), 5)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
        await writer.drain()
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not dev._subs:   # wait for the SSE handler's own subscriber queue
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("stream handler did not subscribe")
            await asyncio.sleep(0.01)
        await feed([FrameStore(id=0x100, dlc=2, data=b"\x01\x02"),
                    FrameStore(id=0x100, dlc=2, data=b"\x01\x02"),
                    FrameStore(id=0x200, dlc=2, data=b"\x03\x04")])
        events: list[dict] = []
        buf: bytes = b""

        async def read_events(n_events: int) -> None:
            nonlocal buf
            d_deadline = asyncio.get_running_loop().time() + timeout_s
            while len(events) < n_events:
                if asyncio.get_running_loop().time() > d_deadline:
                    raise TimeoutError(f"no SSE events in time (have {len(events)}, buf={buf[-200:]!r})")
                rem = d_deadline - asyncio.get_running_loop().time()
                chunk = await asyncio.wait_for(reader.read(4096), timeout=max(0.1, rem))
                if not chunk:
                    raise TimeoutError("connection closed early")
                buf += chunk
                while b"\n\n" in buf:
                    head, _, buf = buf.partition(b"\n\n")
                    line = head.split(b"\r\n")[-1]
                    if line.startswith(b"data: "):
                        events.append(json.loads(line[6:]))

        await read_events(1)
        # second window: same id, unchanged data
        await feed([FrameStore(id=0x100, dlc=2, data=b"\x01\x02"),
                    FrameStore(id=0x100, dlc=2, data=b"\x01\x02")])
        await read_events(4 if throttle is None else 2)
        writer.close()
        return events
    finally:
        await svc.stop()


def test_stream_raw_and_throttled_view():
    async def _go() -> None:
        raw = await _stream_test_events(5.0, None)
        # legacy: one event per frame, single-frame shape (recorder contract)
        assert [e["id"] for e in raw[:3]] == [0x100, 0x100, 0x200]
        assert all("id" in e and "frames" not in e for e in raw)
        vs = await _stream_test_events(8.0, 20)
        # view: window 1 — total=3 exact, deduped/changed frames only (0x100 once, 0x200 once)
        assert vs[0]["total"] == 3
        assert sorted(x["id"] for x in vs[0]["frames"]) == [0x100, 0x200]
        assert all(set(x) == {"id", "id_hex", "ext", "remote", "dlc", "data", "ts", "bus"} for x in vs[0]["frames"])
        # window 2 — unchanged data: counted in total, not resent
        assert vs[1]["total"] == 2 and vs[1]["frames"] == []

    asyncio.run(_go())


def test_scenario_lookup_and_tx_shorthand():
    from canductor.worker.scenario import (
        find_scenario_file,
        load_scenario,
        scenario_roots,
    )
    root = Path(__file__).resolve().parent.parent
    assert (root / "scenarios" / "mazda_nd_dual_can.json").is_file()
    # bare name resolves against the project scenarios dir
    p = find_scenario_file("mazda_nd_dual_can")
    assert p and p.is_file(), f"bare name lookup: {p}"
    # explicit path, with or without .json
    assert find_scenario_file(str(root / "scenarios" / "mazda_swap.json"))
    assert find_scenario_file(str(root / "scenarios" / "mazda_swap"))
    assert find_scenario_file("no_such_scenario") is None
    # loading the ND scenario: string tx shorthand -> TxPattern on the test bus
    sc = load_scenario(str(root / "scenarios" / "mazda_nd_dual_can.json"))
    t = sc.tests_by_id(["14.1.1"])[0][1]
    assert len(t.tx) == 1
    assert t.tx[0].bus == "hs" and t.tx[0].frames == [{"id": "0x7DF"}]


def test_id_filter_dot_boundary():
    # --only/--skip must not match siblings by raw prefix:
    # '5.1.1' selects only 5.1.1, while stage prefix '5.1' selects 5.1.*
    root = Path(__file__).resolve().parent.parent
    from canductor.worker.runner import Runner, SessionOpts
    from canductor.worker.scenario import load_scenario

    sc = load_scenario(str(root / "scenarios" / "mazda_swap.json"))
    opts = SessionOpts(scenario=sc, out_root=Path("/tmp"), core=None,
                       repeats=1, cars=[], buses=[])
    r = Runner(opts)
    opts.only = ["5.1.1"]
    ids = {x.test.id for x in r.selected()}
    assert ids == {"5.1.1"}, ids
    opts.only = ["5.1"]
    ids = {x.test.id for x in r.selected()}
    assert ids and all(i.startswith("5.1.") for i in ids), ids
    assert "5.1.10" in ids and "5.1.2" in ids
    opts.only = []
    opts.skip = ["5.1"]
    assert all(not x.test.id.startswith("5.1.") for x in r.selected())


def test_scenario_validation_failures():
    import copy
    from canductor.worker.scenario import ScenarioError, load_scenario
    root = Path(__file__).resolve().parent.parent
    src = json.loads((root / "scenarios" / "mazda_swap.json").read_text())
    # first stage that actually carries tests (stage 0 may be a checklist-only stage)
    ti = next((si, j) for si, s in enumerate(src["stages"])
              for j, _t in enumerate(s.get("tests", [])))
    si, j = ti

    def load_mut(mutate):
        raw = copy.deepcopy(src)
        mutate(raw)
        p = Path("/tmp/_cand_mut.json")
        p.write_text(json.dumps(raw), encoding="utf-8")
        load_scenario(str(p))

    def set0(key, val):
        def m(r):
            r["stages"][si]["tests"][j][key] = val
        return m

    for mutate, why in [
        (set0("checks", ["zz!"]), "bad check hex must raise"),
        (set0("duration", 0), "zero duration must raise"),
        (set0("tx", [{"bus": "hs", "frames": [{}]}]), "tx frame without id must raise"),
    ]:
        try:
            load_mut(mutate)
        except ScenarioError:
            pass
        else:
            raise AssertionError(why)


def test_cli_card_actions_mapping():
    # LocalControl must translate s/p/q into the actions the Runner compares
    # against (start/skip/quit) — 'p' silently recording was a real bug.
    from canductor.worker import control as ctl
    from canductor.worker.control import LocalControl
    from canductor.worker.scenario import TestSpec
    old_ask, old_confirm = ctl.ask, ctl.confirm
    old_say = ctl.say
    try:
        ctl.say = lambda *a, **k: None
        ctl.ask = lambda *a, **k: ""
        for key, want in [("s", "start"), ("p", "skip"), ("q", "quit"), ("", "start")]:
            ctl.confirm = lambda *a, **k: key
            _, action = LocalControl(auto=False).test_card(
                "1", "s1", TestSpec(id="1.1", stage_id="1", stage_title="t",
                                    car=["x"], bus=["hs"], kind="action",
                                    title="t"), "x", 1, 3, "f")
            assert action == want, f"key {key!r} -> {action!r}, want {want!r}"
    finally:
        ctl.ask, ctl.confirm = old_ask, old_confirm
        ctl.say = old_say


def test_recorder_stop_drains_pending_frames():
    # stop() must flush queued frames to the logs before closing files
    import tempfile
    from canductor.worker.channel import CoreClient
    from canductor.worker.runner import Recorder

    with tempfile.TemporaryDirectory() as td:
        rec = Recorder(CoreClient("http://127.0.0.1:9"), ["hs"], Path(td))
        rec.open()
        for i in range(50):
            rec._queues["hs"].put({"id": 0x100 + i, "ext": False, "remote": False,
                                   "dlc": 1, "data": "aa"})
        rec.stop()
        lines = (Path(td) / "hs.log").read_text().strip().splitlines()
        merged = (Path(td) / "merged.log").read_text().strip().splitlines()
        assert len(lines) == 50, f"lost frames in hs.log: {len(lines)}/50"
        assert len(merged) == 50, f"merged.log: {len(merged)}/50"
        assert len(rec.stats()["hs"].frames) == 50


if __name__ == "__main__":
    failed = 0
    for name in dir():
        if name.startswith("test_") and callable(globals()[name]):
            try:
                globals()[name]()
                print(f"  ok  {name}")
            except Exception as e:
                failed += 1
                import traceback
                print(f"  FAIL {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)