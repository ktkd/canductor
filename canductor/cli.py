"""CLI canductor."""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

from . import __version__
from .common import C, err, hr, say
from .core.service import run_service
from .worker.channel import CoreClient, CoreError
from .worker.scenario import find_scenario_file, load_scenario, scenario_roots


# --- helpers ----------------------------------------------------------------


def list_usb_devices() -> list[str]:
    return sorted(glob.glob("/dev/ttyUSB*"))


def parse_devices(specs: list[str]) -> dict[str, str]:
    """['hs=/dev/ttyUSB0','ms=/dev/ttyUSB1'] -> {'hs': path, 'ms': path}."""
    out: dict[str, str] = {}
    for sp in specs:
        if "=" in sp:
            k, v = sp.split("=", 1)
        else:
            k, v = os.path.basename(sp), sp
        out[k.strip()] = v
    return out


def cmd_devices(args: argparse.Namespace) -> int:
    devs = list_usb_devices()
    if not devs:
        say("No /dev/ttyUSB* devices found")
    for i, d in enumerate(devs):
        say(f"{d}   {C.DIM}ttyUSB{i}{C.RESET}")
    return 0


def _default_devices() -> dict[str, str]:
    d = list_usb_devices()
    out: dict[str, str] = {}
    if len(d) >= 2:
        out = {"hs": d[0], "ms": d[1]}
        say(C.DIM + f"auto: hs={d[0]} ms={d[1]}{C.RESET}")
    return out


def _core_or_inproc(args, aliases: dict[str, str] | None = None) -> tuple[CoreClient, dict[str, str]]:
    """Returns (client, aliases). If --core URL — remote, otherwise in-process."""
    aliases = aliases or {}
    if getattr(args, "core", None):
        return CoreClient(args.core), aliases
    if not aliases:
        aliases = _default_devices()
    # start in-process service on random port
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    ev = threading.Event()
    holder: dict = {}

    def _run() -> None:
        asyncio.set_event_loop(loop)

        async def _go() -> None:
            svc = await run_service(aliases, host="127.0.0.1", port=0)
            holder["base"] = svc.base_url
            ev.set()
            while True:
                await asyncio.sleep(3600)

        try:
            loop.run_until_complete(_go())
            loop.run_forever()
        except Exception as e:      # startup failed — hand the real error over
            holder["err"] = e
            ev.set()

    threading.Thread(target=_run, daemon=True).start()
    if not ev.wait(10):
        raise RuntimeError("in-process core did not start in time (check the device aliases)")
    if "err" in holder:
        raise RuntimeError(f"in-process core did not start: {holder['err']}")
    if "base" not in holder:
        raise RuntimeError("in-process core did not start (check the device aliases)")
    return CoreClient(holder["base"]), aliases


def cmd_web(args: argparse.Namespace) -> int:
    import asyncio

    from .core.service import run_service

    dev_map = _raw_map(args)

    async def _main() -> None:
        svc = await run_service(dev_map, host=args.host, port=args.port, out_root=args.out)
        say(f"{C.BOLD}CANDuctor web{C.RESET} — UI: {svc.base_url}")
        say(f"devices: {json.dumps(dev_map, ensure_ascii=False)}  out: {args.out}")
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        say("\ndone")
    return 0


def cmd_core(args: argparse.Namespace) -> int:
    import asyncio

    dev_map = _raw_map(args)

    async def _main() -> None:
        svc = await run_service(dev_map, host=args.host, port=args.port)
        say(f"{C.BOLD}canductor.core{C.RESET} listening {svc.base_url}")
        say(f"devices: {json.dumps(dev_map, ensure_ascii=False)}")
        say(C.DIM + "endpoints: /health /devices /devices/<n>/{status,open,close,reconfigure,send,send_raw,stream}{C.RESET}")
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        say("\ndone")
    return 0


def _raw_map(args) -> dict:
    return parse_devices(args.dev) or _default_devices()


def cmd_link(args: argparse.Namespace) -> int:
    """Ad-hoc: open one device, live-dump frames; optional --send."""
    import threading

    from .worker.channel import SSEConsumer

    dev = os.path.basename(args.name)
    aliases = {dev: args.name}
    client, _ = _core_or_inproc(args, aliases)
    say(f"link → {args.name} ({dev}) rate={args.rate} var={args.var} core={client.base}")
    try:
        stat = client.open(dev, {"can_baud": args.rate, "variable": args.var, "standard_only": True})
    except CoreError as e:
        err(f"{C.RED}! link: open {dev} failed: {e}{C.RESET}")
        if os.path.exists(args.name):
            err(C.DIM + "  the device file is there — try canductor devices / replugging the adapter" + C.RESET)
        else:
            err(C.DIM + "  device file is absent — is the USB adapter connected? (canductor devices)" + C.RESET)
        return 1
    say(f"opened {stat.get('state')} cfg={stat.get('config')}")
    if args.send:
        _do_send(client, dev, args.send)

    stop = threading.Event()
    n = [0]
    cons = SSEConsumer(client.base, dev)
    cons.start()

    def _dump() -> None:
        while not stop.is_set():
            try:
                ev = cons.q.get(timeout=0.2)
            except Exception:
                continue
            if "__error__" in ev:
                err(f"! {ev['__error__']}")
                continue
            ts = time.time()
            idh = f"{ev['id']:03X}" if not ev["ext"] else f"{ev['id']:08X}"
            data = ev.get("data", "")
            print(f"{ts % 100000:8.3f} {idh} {ev.get('dlc', 0)} {data}", flush=True)
            n[0] += 1

    t = threading.Thread(target=_dump, daemon=True)
    t.start()
    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        cons.stop()
        try:
            client.close(dev)
        except CoreError:
            pass
        say(f"\n{C.GREEN}{n[0]} frames{C.RESET}")
    return 0


def _do_send(client: CoreClient, dev: str, send: list[str]) -> None:
    # send = [ID, data-bytes(hex)]
    idstr = send[0]
    can_id = int(idstr, 0) if idstr.lower().startswith("0x") else int(idstr, 16)
    data = bytes(int(x, 0) if x.lower().startswith("0x") else int(x, 16) for x in send[1:])
    r = client.send(dev, {"id": can_id, "data": data.hex(), "ext": can_id > 0x7FF})
    say(C.DIM + f"TX {r.get('id_hex')} dlc{r.get('dlc')}{C.RESET}")


def cmd_run(args: argparse.Namespace) -> int:
    sc_path = find_scenario_file(args.scenario)
    if sc_path is None:
        err(f"scenario not found: {args.scenario}")
        roots = scenario_roots()
        if roots:
            err("looked in: " + "  ".join(str(r) for r in roots))
        err("pass a path to the .json, put the scenarios there, "
            "or set CANDUCTOR_SCENARIOS")
        return 2
    scenario = load_scenario(str(sc_path))
    aliases = _raw_map(args)
    client, aliases = _core_or_inproc(args, aliases)
    say(f"{C.BOLD}Scenario: {scenario.title}{C.RESET} v{scenario.version}")
    say(f"devices: {json.dumps(aliases, ensure_ascii=False)}  core={client.base}")

    from .worker.runner import Runner, SessionOpts
    import pathlib

    opts = SessionOpts(
        scenario=scenario,
        out_root=pathlib.Path(args.out),
        core=client,
        device_aliases=aliases,
        repeats=args.repeats,
        quiet=args.quiet,
        after=args.after,
        pause=args.pause,
        scale=args.scale,
        only=[x.strip() for x in (args.only or "").split(",") if x.strip()],
        skip=[x.strip() for x in (args.skip or "").split(",") if x.strip()],
        cars=[x.strip() for x in (args.cars or "").split(",") if x.strip()],
        buses=[x.strip() for x in (args.buses or "").split(",") if x.strip()],
        auto_confirm=args.auto,
        dry_run=args.dry_run,
        verbose=not args.no_verbose,
    )
    try:
        return Runner(opts).run()
    except KeyboardInterrupt:
        say("\n" + C.YELLOW + "interrupted" + C.RESET)
        return 130
    except CoreError as e:
        err(f"{C.RED}! session: {e}{C.RESET}")
        return 1


def cmd_probe(args: argparse.Namespace) -> int:
    """Isolated bench test: two devices A/B, frame A→B / B→A / variable / ms-rate."""
    import threading

    if len(args.devices) >= 2:
        a, b = args.devices[0], args.devices[1]
    else:
        a = b = args.devices[0]
    aliases = parse_devices([a, b])
    client, _ = _core_or_inproc(args, aliases)
    a, b = a.rsplit("/", 1)[-1], b.rsplit("/", 1)[-1]

    def _ensure(dev: str, rate: int, var: bool) -> bool:
        cfg = {"can_baud": rate, "variable": var, "standard_only": True}
        try:
            st = client.status(dev)
            if st.get("state") == "configured":
                client.reconfigure(dev, cfg)
            else:
                st = client.open(dev, cfg)
                if st.get("state") != "configured":
                    raise CoreError(
                        f"{dev}: state '{st.get('state')}', last_error={st.get('last_error') or '?'}")
        except CoreError as e:
            err(f"{C.RED}! {dev}: {e}{C.RESET}")
            return False
        return True

    def try_cross(dev_from: str, dev_to: str, fr: dict, rate: int, var: bool) -> bool:
        if not (_ensure(dev_from, rate, var) and _ensure(dev_to, rate, var)):
            return False
        cons = __import__("canductor.worker.channel", fromlist=["SSEConsumer"]).SSEConsumer(client.base, dev_to)
        cons.start()
        time.sleep(0.2)
        client.send(dev_from, fr)
        ok = False
        t0 = time.time()
        while time.time() - t0 < 3.0:
            try:
                ev = cons.q.get(timeout=0.05)
                if "__error__" not in ev and ev.get("id") == fr["id"]:
                    ok = True
                    break
            except Exception:
                continue
        cons.stop()
        return ok

    say(f"probe A={a} B={b} core={client.base}")
    ok1 = try_cross(a, b, {"id": 0x123, "data": "0102030405060708"}, args.rate, False)
    ok2 = try_cross(b, a, {"id": 0x778, "data": "ab" * 8}, args.rate, False)
    ok3 = try_cross(a, b, {"id": 0x345, "data": "0001020304050607"}, args.rate, True)
    ok4 = try_cross(a, b, {"id": 0x10, "data": "0102"}, 125000, False)
    say(f"A→B @ {args.rate} fixed:   {C.GREEN if ok1 else C.RED}{'OK' if ok1 else 'FAIL'}{C.RESET}")
    say(f"B→A @ {args.rate} fixed:   {C.GREEN if ok2 else C.RED}{'OK' if ok2 else 'FAIL'}{C.RESET}")
    say(f"A→B @ {args.rate} variable:{C.GREEN if ok3 else C.RED}{'OK' if ok3 else 'FAIL'}{C.RESET}")
    say(f"A→B @ 125000 fixed:  {C.GREEN if ok4 else C.RED}{'OK' if ok4 else 'FAIL'}{C.RESET}")
    client.close(a)
    client.close(b)
    return 0 if all([ok1, ok2, ok3, ok4]) else 1


def _add_device_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dev", action="append", default=[], help="alias=/dev/ttyUSBn (multiple ok)")
    p.add_argument("--core", default=None, help="remote core URL (else in-process)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="CANDuctor", description="CAN data collection microservice (Waveshare USB-CAN-A)")
    ap.add_argument("--version", action="version", version=f"CANDuctor {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("devices", help="list /dev/ttyUSB*")
    p.set_defaults(fn=cmd_devices)

    p = sub.add_parser("core", help="start HTTP microservice")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8111)
    p.add_argument("--dev", action="append", default=[], metavar="alias=/dev/ttyUSBn",
                   help="bus (two: --dev hs=... --dev ms=...); without --dev: auto hs=/dev/ttyUSB0 ms=/dev/ttyUSB1")
    p.set_defaults(fn=cmd_core)

    p = sub.add_parser("link", help="live dump one device")
    p.add_argument("name", help="device alias or path")
    p.add_argument("--rate", type=int, default=500000)
    p.add_argument("--var", action="store_true")
    p.add_argument("-t", "--seconds", type=float, default=None)
    p.add_argument("-s", "--send", nargs="+", metavar="HDR", help="TX id + data bytes (hex)\n")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("run", help="run worker scenario")
    p.add_argument("-s", "--scenario", default="mazda_swap")
    p.add_argument("-o", "--out", default="logs")
    p.add_argument("--repeats", type=int, default=None, help="repeats per test (default: scenario rules.repeats)")
    p.add_argument("--quiet", type=float, default=None, help="quiet phase seconds (default: scenario rules)")
    p.add_argument("--after", type=float, default=None, help="after phase seconds (default: scenario rules)")
    p.add_argument("--pause", type=float, default=None, help="pause between tests (default: scenario rules)")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--only", default="", help="test id filter, comma-separated")
    p.add_argument("--skip", default="")
    p.add_argument("--cars", default="")
    p.add_argument("--buses", default="")
    p.add_argument("--auto", action="store_true", help="no interactive confirm")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-verbose", action="store_true")
    _add_device_args(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("web", help="web UI (core + scenario console + live)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8111)
    p.add_argument("--out", default="logs")
    p.add_argument("--dev", action="append", default=[], metavar="ALIAS=/DEV/TTYUSBN",
                   help="bus (two: --dev hs=... --dev ms=...); without --dev: auto hs=/dev/ttyUSB0 ms=/dev/ttyUSB1")
    p.set_defaults(fn=cmd_web)

    p = sub.add_parser("probe", help="cross-device bench test")
    p.add_argument("devices", nargs="+", help="dev_from dev_to")
    p.add_argument("--rate", type=int, default=500000)
    p.set_defaults(fn=cmd_probe)

    args = ap.parse_args(argv)
    hr()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
