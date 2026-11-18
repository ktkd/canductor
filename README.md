# CANDuctor

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/deps-none-orange)

CAN data collection for Waveshare USB-CAN-A adapters (CH340) — built for the
Mazda SWAP workflow: record **HS-CAN (500 kbit/s)** and **MS-CAN
(125 kbit/s)** simultaneously, with a structured test protocol — quiet →
action → after phases, operator confirmation, per-test reports — plus a live
web console.

Pure Python stdlib (≥ 3.10), zero runtime dependencies.

---

## What's inside

| Part | What it does |
|---|---|
| **core** | HTTP microservice over the Waveshare binary protocol (byte-compatible with the Rust [waveshare-usb-can-a](https://github.com/im-0/rust-waveshare-usb-can-a) crate). Open / reconfigure (incl. adaptive serial rate), TX, RX fan-out as SSE streams. |
| **worker** | Interactive test session: the operator prepares and confirms each test, the recording runs three phases; per-test `report.txt` (protocol form + statistics) and per-bus `.log` files. |
| **web UI** | Browser console over the same core: session control, test cards (coolant / air / engine / comment), live frames per bus, manual TX with saved presets, collected-data browser with previews and downloads. |

## How it fits together

```
 browser (web UI)               worker (threads)
   │ UI + SSE (10 Hz view)      │ dialog, phases, confirms
   │                     └─────┐│ SSE consumers (raw frames → logs)
   ▼                           ▼▼
         core · HTTP service (stdlib asyncio)
                  │
                  └─ UsbCanDevice · termios CH340
                      2M/8N2 raw (BOTHER baud), fixed/variable protocol,
                      live resync
```

`run`, `link`, `probe` use the same HTTP API — by default an in-process core
on a random port, or `--core http://host:port` for a standalone
`canductor core` (one core, any number of workers / browsers).

---

## Quick start

Requirements: Linux with the `ch341` kernel module (USB-CAN-A: VID `1a86`,
PID `7523` → `/dev/ttyUSB*`), Python ≥ 3.10.

```bash
git clone https://github.com/ktkd/canductor
cd canductor
pip install .            # or pip install -e . in the source tree
```

`canductor` is now on the PATH (everything works identically as `python3 -m canductor …`).

### 1 · See the adapters

```bash
$ canductor devices
/dev/ttyUSB0   ttyUSB0
/dev/ttyUSB1   ttyUSB1
```

### 2 · Self-test a pair of adapters (bench: looped with a CAN lead)

```bash
$ canductor probe /dev/ttyUSB0 /dev/ttyUSB1
────────────────────────────────────────────────────────────────
probe A=ttyUSB0 B=ttyUSB1 core=http://127.0.0.1:46625
A→B @ 500000 fixed:   OK
B→A @ 500000 fixed:   OK
A→B @ 500000 variable:OK
A→B @ 125000 fixed:    OK
```

TX in both directions, fixed 20-byte layout, variable-length encoding,
the drop to 125 kbit/s. Exit code 0 only when all four pass; a missing
adapter gives a polite per-device error, no traceback.

### 3 · Live dump one bus (ad-hoc listening)

```bash
$ canductor link /dev/ttyUSB1
────────────────────────────────────────────────────────────────
link → /dev/ttyUSB1 (ttyUSB1) rate=500000 var=False core=http://127.0.0.1:35759
opened configured cfg={'can_baud': 500000, 'standard_only': True, …}
90362.379 40A 8 bf16902465000000
90362.387 477 8 0000000000000000
90362.389 47B 8 0000000000000000
^C
464 frames
```

Options: `--rate 125000`, `--var` (variable encoding), `-t 30` (stop after
30 s), `-s 0x123 01 02 03` (send one frame before the dump). 

### 4 · Web console (the way to run a real session)

```bash
$ canductor web --dev hs=/dev/ttyUSB2 --dev ms=/dev/ttyUSB3
CANDuctor web — UI: http://127.0.0.1:8111
devices: {"hs": "/dev/ttyUSB2", "ms": "/dev/ttyUSB3"}  out: logs
```

Open `http://127.0.0.1:8111`. On the page:

* **scenario panel** — step list with progress: ✓ done, — skipped, ● current;
* **test card** — title, action, expected IDs; the operator confirms with the
  protocol fields: coolant °C, air °C, engine state (cold / warm), comment —
  they land in `notes.json` and in the report form;
* **HS / MS panels** — live frames (time / ID / DLC / data), exact counters
  (`frames · ID · fps`), row cap, pause, manual TX line (ID + data, up to 8
  saved presets in the browser);
* **collected data** — session tree, file preview in a modal, download.

The live stream is decimated server-side per connection: one event per
100 ms window carrying the latest *changed* value per ID (`total` keeps the
exact frame count). A 300 msg/s bus becomes a steady 10 msg/s for the UI and
the tab stays responsive. The recorder uses a separate raw stream — no frame
is ever lost to a file.

Everything the UI does is a plain HTTP API — the same session via `curl`:

```bash
$ curl -s -X POST localhost:8111/api/session/start \
    -d '{"scenario":"mazda_swap","repeats":1,"scale":0.5,"only":"1.1","cars":"rx8"}'
{"ok": true, "scenario": "mazda_swap"}

# the test card is waiting — confirmation with the protocol fields
$ curl -s -X POST localhost:8111/api/session/cmd \
    -d '{"action":"start","note":{"coolant":90,"air":25,"engine":"cold","comment":"idle 5 min after start"}}'
{"ok": true, "action": "start"}

$ curl -s localhost:8111/api/session/status | head -c 110
{"session_state": "recording", "phase": {"test_id": "1.1", "car": "rx8",…
```

### 5 · Terminal session (no browser)

```bash
canductor run -s mazda_swap --dev hs=/dev/ttyUSB2 --dev ms=/dev/ttyUSB3
```

Preview the plan without touching the hardware:

```bash
$ canductor run -s bench_quick --dry-run --dev hs=/dev/ttyUSB0 --dev ms=/dev/ttyUSB1
Plan: 9 recordings
  1.1 / dummy / rep 1 / Echo pair: basic recording (500 kbit/s)
  1.1 / dummy / rep 2 / Echo pair: basic recording (500 kbit/s)
  …
```

For each planned recording (test × car × repeat) the session does:

1. **card** — operator confirmation (skip / quit available); in the web UI
   this is the card with the protocol fields;
2. **configure** — the buses switch to the test rate (only when it differs;
   reconfigure + LED blink ~700 ms);
3. **phases** — `quiet` (stable state) → `action` (target action, duration
   from the scenario) → `after`, with a countdown shown in the UI;
4. **pause** (default 10 s) — then the next card.

---

## CLI

```bash
canductor devices                                           # list /dev/ttyUSB*
canductor core --host H --port P --dev alias=/dev/ttyUSBn   # standalone core
canductor web  --host H --port P -o DIR --dev alias=…       # web console
canductor link <dev> [--rate B] [--var] [-t S] [-s ID [bytes…]]
canductor run  -s SCENARIO [filters] [--core URL]           # test session
canductor probe <A> [B] [--rate B]                           # pair self-test
```

`run` filters:

| flag | meaning | default |
|---|---|---|
| `-o DIR` | output root | `logs` |
| `--repeats N` | repeats per test | scenario rule (3) |
| `--quiet / --after / --pause S` | phase durations (s) | scenario rule (5/5/10) |
| `--scale F` | multiplier for all durations | 1.0 |
| `--only "1.1,2"` | test / stage id filter: exact or dot-boundary prefix (`5.1` → `5.1.x`, `5.1.1` does **not** pull `5.1.10`) | all |
| `--skip "3.4"` | tests to exclude | — |
| `--cars "rx8"` · `--buses "hs"` | restrict | all |
| `--auto` | no interactive prompts | interactive |
| `--dry-run` | print the plan only | — |
| `--core URL` | use a running core | in-process, random port |

Without `--dev`, devices are auto-mapped to `hs=/dev/ttyUSB0`, `ms=/dev/ttyUSB1`.

Scenario names resolve against `$CANDUCTOR_SCENARIOS`, then `<project>/scenarios/`,
then `./scenarios/` (cwd); an explicit path (with or without `.json`) always wins.
After a plain `pip install .` (not `-e`), run from a checkout or export
`CANDUCTOR_SCENARIOS=/path/to/canductor/scenarios` — the wheel does not ship scenarios.

## Core HTTP API

```
GET  /health
GET  /devices                                    # all adapters: state rx tx
GET  /devices/<n>/status                         # state, config, counters
POST /devices/<n>/open      {config?}            # no-op if already configured
POST /devices/<n>/close
POST /devices/<n>/reconfigure {config}           # rate change → blink
POST /devices/<n>/send       {id, data, ext, remote}
POST /devices/<n>/send_raw   {hex}               # raw protocol bytes
GET  /devices/<n>/stream                           SSE, raw: one event per frame
GET  /devices/<n>/stream?throttle=<ms>             SSE, display: one per window
```

`config` — `can_baud` (5k…1M), `standard_only`, `variable`, `loopback`,
`silent`, `retransmit`, `filter` / `mask`, `serial_baud` (9600…2M — sent at
the old rate, then the port reopens at the new one), `id_filter {mode, ids}`.

The display stream (consumed by the web UI) sends
`{"total": <frames in the window>, "frames": [latest changed value per id]}`;
everything the recorder needs stays on the raw stream. Session API of the
web console (details in `service.py` docstring):

```
GET  /                                  # the UI page
GET  /api/scenario[?name=] · GET /api/session/status
POST /api/session/start  {scenario, repeats, scale, only, cars, buses}
POST /api/session/cmd    {action: start|skip|quit, note}
GET  /api/session/stream   # SSE: status · card · phase · pause · done · …
GET  /api/logs · /api/logs/file?path=…[&dl=1]
```

## Protocol (ground truth: Rust crate + live hardware)

Waveshare USB-CAN-A speaks binary over 2M/8N2 raw (custom baud via BOTHER),
byte-compatible with the `waveshare-usb-can-a` crate:

* **fixed** 20-byte message: `[0]=0xAA [1]=0x55`, last byte is the checksum
  `sum(bytes[2:-1]) & 0xFF`;
* **frame, fixed**: `[2]=01 [3]=std/ext [4]=data/remote [5..8]=ID LE
  (std = lo, hi, 0, 0) [9]=DLC [10..17]=data [18]=rsvd`;
* **frame, variable**: `[0]=0xAA [1]=0xC0|ext(0x20)|remote(0x10)|DLC`, id
  2/4 bytes LE, data, terminating `0x55` (no checksum);
* **config**: `[2]=02|10(var) [3]=baud code [4]=std/ext [5..8]=filter
  [9..12]=mask [13]=mode [14]=retrans`;
* baud codes: CAN `1M=01 … 500k=03 … 125k=07`; serial `2M=00 … 9600=05`;
* after a config — LED blink ~700 ms (data only).

The decoder resyncs live (one byte at a time, as `ReceiveBuffer` in the
crate). Parity is covered by golden vectors in `tests/` and by
`canductor probe` on live hardware; the crate itself has 39 unit tests
(`cargo test`).

## Scenarios

`scenarios/*.json` is a complete test protocol (format v1, validated at
load):

* **`mazda_swap.json`** — 13 stages / 72 tests for Mazda SWAP: passive
  listening, engine & transmission, ABS/DSC, steering, body, HVAC (MS-CAN),
  PCM-off, stress, bench ECU (TX injection), verification. Each test
carries: car, bus, kind (`state` / `action`), duration (number / `{min,max}` /
  open-ended), `file`, `checks` (expected ids), `tx` (injection pattern),
  `bus_override` (per-test rate / encoding), `variable`;
* **`mazda_nd_dual_can.json`** — dual-bus MX-5 ND campaign (HS + MS captured
  simultaneously, shared timebase): DSC/TCS button, cruise control and ASL
  limiter stages; `tx` may be a bare id list (`["0x7DF"]` = inject an empty
  frame of the test's own bus at 10 Hz);
* **`bench_quick.json`** — no-car check of the record/run/report mechanics:
  echo pair, 500k→125k, variable, injection.

A session writes:

```
<out>/session_20251007_120000/
  scenario.json
  1.1_rx8_rep1/
    hs.log             # one line per frame:  t bus ID Rx R|D DLC data
    ms.log
    merged.log
    report.txt         # protocol form (Appendix C) + per-phase statistics
    notes.json         # coolant / air / engine / comment
```

`report.txt` — the form (date / car / bus / stage / test / action / coolant /
air / engine / filename / repeat) plus per-phase statistics: frames / fps in
quiet / action / after, unique ids, top-20, ids appearing in action /
disappearing after it, ids whose data changed, presence of the expected ids.

Scale on a real car (engine on, idle) — one 30 s window:

```
hs: 3479 frames  (116 msg/s, 13 unique ids)
ms: 6694 frames  (223 msg/s)
```

## Tests

```bash
python3 tests/test_protocol.py        # plain script, no pytest required
```

Protocol golden vectors (byte-level vs the crate), decoder resync / split,
in-process service round-trip, raw and throttled SSE streams.

## Layout

```
canductor/
  core/     service · device · protocol · serial       # HTTP + USB-CAN
  worker/   runner · scenario · report · channel · control · webcontrol
  web/      index.html                                 # the whole UI, one file
  cli.py
scenarios/  mazda_swap.json · mazda_nd_dual_can.json · bench_quick.json · …
tests/      test_protocol.py
bin/        canductor · canductor-core · -link · -run · -probe · -bench (bash)
```

## License

MIT — see [LICENSE](LICENSE).

## Contact

* Website: <https://ktkd.ru> · E-mail: <ktk@ktkd.ru>
