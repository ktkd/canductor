"""UsbCanDevice — async wrapper for Waveshare USB-CAN-A.

The configuration routine repeats the Rust crate (sync.rs):
  * open → set_configuration → BLINK_DELAY (700 ms) before transmission;
  * reconfigure → CONFIGURATION_DELAY (100 ms) + blink;
  * serial rate change: the “set serial baud” message is sent at the current
    rate, then wait, then reopen the port at the new baud (adaptive reconfigure);
  * the receive buffer is cleared when the encoding mode changes
    (ReceiveBuffer.clear()).
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field, replace
from enum import Enum

from .protocol import (
    BLINK_DELAY_MS,
    CONFIGURATION_DELAY_MS,
    FrameStore,
    ProtocolDecoder,
    ProtocolError,
    encode_config,
    encode_frame,
    encode_id_filter,
    encode_serial_baud,
)
from .serial import SerialError, SerialPort

DEFAULT_SERIAL_BAUD = 2_000_000
READ_CHUNK_S = 0.02
RX_QUEUE_MAX = 8192   # internal receive() queue — bounded, drop on overflow
SUB_QUEUE_MAX = 8192  # per-SSE-connection queue — bounded (a stalled client
                      # must not OOM the service; drop-oldest keeps freshness)


class DeviceState(Enum):
    init = "init"
    setting = "setting"
    configured = "configured"
    configuring = "configuring"
    error = "error"


class DeviceError(Exception):
    pass


@dataclass
class Config:
    can_baud: int = 500_000
    standard_only: bool = True
    variable_encoding: bool = False
    loopback: bool = False
    silent: bool = False
    retransmit: bool = True
    filter_id: int = 0
    mask_id: int = 0
    serial_baud: int = DEFAULT_SERIAL_BAUD
    id_filter_mode: str | None = None  # None | 'disabled' | 'allow' | 'block'
    id_filter_ids: list[int] = field(default_factory=list)

    @classmethod
    def hs(cls) -> "Config":
        """HS-CAN (scenario): 500 kbit/s, std frames only, fixed codec."""
        return cls(can_baud=500_000)

    @classmethod
    def ms(cls) -> "Config":
        """MS-CAN (scenario): 125 kbit/s."""
        return cls(can_baud=125_000)


class UsbCanDevice:
    """isolation one. (a) await dev.open(); (b) await dev.send_frame({...});
    (c) frame = await dev.receive(); (d) await dev.reconfigure(cfg); (e) await dev.close()."""

    def __init__(self, name: str, path: str, cfg: Config | None = None) -> None:
        self.name = name
        self.path = path
        self.cfg = cfg or Config()
        self.state = DeviceState.init
        self._serial: SerialPort | None = None
        self._decoder = ProtocolDecoder("fixed")
        self._rx: asyncio.Queue[FrameStore] = asyncio.Queue(maxsize=RX_QUEUE_MAX)
        self._subs: set = set()  # fan-out: one queue per live subscriber (SSE connection)
        self._alive = False
        self._reader: asyncio.Task | None = None
        self.rx_count = 0
        self.tx_count = 0
        self.rx_dropped = 0    # frames dropped because receive() queue was full
        self.sub_dropped = 0   # frames dropped for a stalled SSE subscriber
        self.last_rx_ts = 0.0
        self.last_config_ts = 0.0
        self.last_error = ""

    # --- lifecycle -------------------------------------------------------------

    async def open(self) -> None:
        if self.state == DeviceState.configured:
            return
        if self.state not in (DeviceState.init, DeviceState.error):
            return    # setting / configuring — in progress
        if self.state == DeviceState.error:
            # retry after a failed open (adapter was plugged in meanwhile)
            await self._cleanup()
        self.state = DeviceState.setting
        try:
            if not os.path.exists(self.path):
                raise SerialError(f"device {self.path} not found (USB adapter not connected?)")
            self._serial = SerialPort(self.path, self.cfg.serial_baud).open()
            await self._apply_config(self.cfg, initial=True)
        except (DeviceError, SerialError, ProtocolError, OSError) as e:
            self.state = DeviceState.error
            self.last_error = str(e)
            await self._cleanup()
            raise DeviceError(f"{self.name} ({self.path}): {e}") from e
        self._alive = True
        self._reader = asyncio.get_running_loop().create_task(self._read_loop())

    async def close(self) -> None:
        await self._cleanup()
        self.state = DeviceState.init

    async def _cleanup(self) -> None:
        self._alive = False
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except BaseException:
                pass
            self._reader = None
        if self._serial:
            self._serial.close()
            self._serial = None
        self._decoder.reset()

    async def __aenter__(self) -> "UsbCanDevice":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # --- configuration ---------------------------------------------------------

    async def reconfigure(self, cfg: Config) -> None:
        if self.state not in (DeviceState.configured,):
            raise DeviceError(f"{self.name}: reconfigure from state {self.state.value}")
        self.state = DeviceState.configuring
        try:
            await self._apply_config(cfg)
        except (DeviceError, SerialError, ProtocolError, OSError) as e:
            self.state = DeviceState.error
            self.last_error = str(e)
            raise DeviceError(f"{self.name}: {e}") from e

    async def _apply_config(self, cfg: Config, initial: bool = False) -> None:
        assert self._serial is not None
        if cfg.serial_baud != self.cfg.serial_baud:
            # Serial baud changes on current speed, then reopen port
            await self._change_serial_baud(cfg.serial_baud)
        self.cfg = replace(self.cfg, **cfg.__dict__)
        self._tx_raw(encode_config(
            can_baud=cfg.can_baud,
            standard_only=cfg.standard_only,
            variable=cfg.variable_encoding,
            filter_id=cfg.filter_id,
            mask_id=cfg.mask_id,
            loopback=cfg.loopback,
            silent=cfg.silent,
            retransmit=cfg.retransmit,
        ))
        if cfg.id_filter_mode is not None:
            self._tx_raw(encode_id_filter(cfg.id_filter_mode, cfg.id_filter_ids))
        # encoding changed -> reset decoder (crate: receiver.clear())
        self._decoder = ProtocolDecoder("variable" if cfg.variable_encoding else "fixed")
        # crate: CONFIGURATION_DELAY + BLINK_DELAY
        await asyncio.sleep((CONFIGURATION_DELAY_MS + BLINK_DELAY_MS) / 1000)
        self.last_config_ts = time.monotonic()
        self.state = DeviceState.configured

    async def _change_serial_baud(self, baud: int) -> None:
        assert self._serial is not None
        self._tx_raw(encode_serial_baud(baud))
        self._serial.flush()
        await asyncio.sleep(0.4)
        self._serial.flush()
        # reopen port on new speed
        self._serial.close()
        self._serial = SerialPort(self.path, baud).open()
        self._serial.flush()
        await asyncio.sleep(0.15)

    def _tx_raw(self, msg: bytes) -> None:
        assert self._serial is not None
        self._serial.write(msg)

    # --- tx / rx ----------------------------------------------------------------

    async def send_frame(self, frame: dict) -> FrameStore:
        """frame: dict(id=int, data=bytes|hex-str, ext=bool, remote=bool).

        If id > 0x7FF — extended is forced.
        """
        if self.state != DeviceState.configured:
            raise DeviceError(f"{self.name}: send in state {self.state.value}")
        can_id = int(frame["id"])
        data = frame.get("data") or b""
        if isinstance(data, str):
            data = bytes.fromhex(data.replace(" ", ""))
        if len(data) > 8:
            raise DeviceError(f"{self.name}: data is {len(data)} bytes, max 8 (fixed CAN frame)")
        data = bytes(bytearray(data)[:8])
        extended = bool(frame.get("ext")) or can_id > 0x7FF
        remote = bool(frame.get("remote"))
        msg = encode_frame(can_id, data, extended=extended, remote=remote,
                          variable=self.cfg.variable_encoding)
        self._tx_raw(msg)
        self.tx_count += 1
        return FrameStore(id=can_id, extended=extended, remote=remote, dlc=len(data), data=data)

    async def send_raw(self, msg: bytes) -> None:
        """Send a ready-made protocol message (cfg/filter/...)."""
        if self._serial is None or self.state != DeviceState.configured:
            raise DeviceError(f"{self.name}: send_raw in state {self.state.value}")
        self._tx_raw(msg)

    async def receive(self) -> FrameStore:
        return await self._rx.get()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUB_QUEUE_MAX)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def wait_frames(self, n: int, timeout: float = 5.0) -> list[FrameStore]:
        out: list[FrameStore] = []
        deadline = time.monotonic() + timeout
        while len(out) < n:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                out.append(await asyncio.wait_for(self._rx.get(), remain))
            except asyncio.TimeoutError:
                break
        return out

    def pending_frames(self) -> int:
        return self._rx.qsize()

    # --- read loop --------------------------------------------------------------

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self._alive and self._serial:
            try:
                data = await loop.run_in_executor(None, self._serial.read, READ_CHUNK_S)
            except (SerialError, OSError):
                self.last_error = "serial read error"
                self.state = DeviceState.error
                break
            if not data:
                continue
            subs = list(self._subs)
            for f in self._feed(data):
                self.rx_count += 1
                self.last_rx_ts = time.monotonic()
                try:
                    self._rx.put_nowait(f)
                except asyncio.QueueFull:
                    self.rx_dropped += 1      # nobody is draining receive()
                for q in subs:
                    self._fanout(q, f)

    def _fanout(self, q: asyncio.Queue, f: FrameStore) -> None:
        """Non-blocking publish to one subscriber; a stalled SSE connection
        loses its oldest frames instead of growing memory without bound."""
        try:
            q.put_nowait(f)
            return
        except asyncio.QueueFull:
            self.sub_dropped += 1
        try:
            q.get_nowait()                    # drop the oldest, keep fresh tail
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(f)
        except asyncio.QueueFull:
            pass

    def _feed(self, data: bytes) -> list[FrameStore]:
        """Feeding of decoder_bytes; return the completed frames (error — resync)."""
        self._decoder.feed(data)
        frames = self._decoder.frames()
        return frames

    # --- introspection ------------------------------------------------------------

    @property
    def defn(self) -> dict:
        """Config definition (for /status)."""
        c = self.cfg
        return {
            "can_baud": c.can_baud,
            "standard_only": c.standard_only,
            "variable_encoding": c.variable_encoding,
            "loopback": c.loopback,
            "silent": c.silent,
            "retransmit": c.retransmit,
            "serial_baud": c.serial_baud,
        }

    def status(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "state": self.state.value,
            "config": self.defn,
            "rx": self.rx_count,
            "tx": self.tx_count,
            "rx_queue": self._rx.qsize(),
            "rx_dropped": self.rx_dropped,
            "sub_dropped": self.sub_dropped,
            "last_error": self.last_error,
            "uptime_since_config": (time.monotonic() - self.last_config_ts) if self.last_config_ts else None,
        }


_devices: dict[str, UsbCanDevice] = {}


def make_device(name: str, path: str, cfg: Config | None = None) -> UsbCanDevice:
    dev = UsbCanDevice(name, path, cfg)
    _devices[name] = dev
    return dev


def get_device(name: str) -> UsbCanDevice:
    return _devices[name]