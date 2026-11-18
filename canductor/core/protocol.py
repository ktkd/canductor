"""Waveshare USB-CAN-A protocol (byte-compatible with waveshare-usb-can-a/lib/src/proto.rs).

Messages:
  * fixed — 20 bytes, header [0]=0xAA [1]=0x55, checksum as the **last**
    byte = sum(bytes[2:-1]) & 0xFF;
  * variable (frames) — [0]=0xAA [1]=0xC0|ext|remote|dlc, id (2/4 bytes LE),
    data, final 0x55 (terminator, NOT a checksum).

The golden vectors come from the Rust crate's unit tests and were verified
on live hardware (cross-linked pair of adapters).
"""
from __future__ import annotations

from dataclasses import dataclass

# --- protocol constants (1:1 with proto.rs) ----------------------------------
HEADER = 0xAA
HEADER_TYPE_FIXED = 0x55

TYPE_FRAME = 0x01
TYPE_CFG = 0x02
TYPE_CFG_SET_SERIAL_BAUD = 0x04
TYPE_CFG_SET_VARIABLE = 0x10
TYPE_CFG_SET_ID_FILTER = 0x10
TYPE_CFG_SET_ID_FILTER_BLOCKLIST = 0x01

ANY_STANDARD = 0x01
ANY_EXTENDED = 0x02

FRAME_DATA = 0x01
FRAME_REMOTE = 0x02

VAR_BASE = 0xC0
VAR_EXT = 0x20
VAR_REMOTE = 0x10
VAR_DLC_MASK = 0x0F
VAR_END = 0x55

MODE_LOOPBACK = 0x01
MODE_SILENT = 0x02

RETRAN_ENABLED = 0x00
RETRAN_DISABLED = 0x01

MAX_FIXED = 20
MAX_DATA = 8
SYNC_ATTEMPTS_MAX = MAX_FIXED - 1  # as in Rust: MAX_MESSAGE_SIZE - 1

# CAN rates (code -> bit/s), ToProtocolValue for CanBaudRate
CAN_BAUD_BY_CODE = {
    0x0C: 5_000,
    0x0B: 10_000,
    0x0A: 20_000,
    0x09: 50_000,
    0x08: 100_000,
    0x07: 125_000,
    0x06: 200_000,
    0x05: 250_000,
    0x04: 400_000,
    0x03: 500_000,
    0x02: 800_000,
    0x01: 1_000_000,
}
CODE_BY_CAN_BAUD = {v: k for k, v in CAN_BAUD_BY_CODE.items()}

# Serial rates (code -> bit/s)
SERIAL_BAUD_BY_CODE = {0x05: 9600, 0x04: 19200, 0x03: 38400, 0x02: 115200, 0x01: 1_228_800, 0x00: 2_000_000}
CODE_BY_SERIAL_BAUD = {v: k for k, v in SERIAL_BAUD_BY_CODE.items()}

CONFIGURATION_DELAY_MS = 100  # crate: CONFIGURATION_DELAY
BLINK_DELAY_MS = 700          # crate: BLINK_DELAY


class ProtocolError(ValueError):
    """Message format error."""


# --- helpers ----------------------------------------------------------------

def checksum(msg: bytes) -> int:
    """Checksum: sum(bytes[2:-1]) & 0xFF (fixed fill_checksum)."""
    return sum(msg[2 : len(msg) - 1]) & 0xFF


def can_baud_code(rate: int) -> int:
    try:
        return CODE_BY_CAN_BAUD[rate]
    except KeyError:
        raise ProtocolError(f"unsupported CAN rate {rate}") from None


def serial_baud_code(rate: int) -> int:
    try:
        return CODE_BY_SERIAL_BAUD[rate]
    except KeyError:
        raise ProtocolError(f"unsupported serial baud {rate}") from None


def _le16(v: int) -> list[int]:
    return [v & 0xFF, (v >> 8) & 0xFF]


def _le32(v: int) -> list[int]:
    return [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]


def _id_slots(can_id: int, extended: bool) -> list[int]:
    """4 ID slots (as in id_to_bytes): std -> [lo, hi, 0, 0], ext -> 4 bytes."""
    return _le32(can_id) if extended else (_le16(can_id) + [0, 0])


# --- coders ------------------------------------------------------------------

def encode_config(
    can_baud: int = 500_000,
    standard_only: bool = True,
    variable: bool = False,
    filter_id: int = 0,
    mask_id: int = 0,
    loopback: bool = False,
    silent: bool = False,
    retransmit: bool = True,
) -> bytes:
    """20-byte configuration message (per impl ToFixedMessage for Usb2CanConfiguration)."""
    if (filter_id or mask_id) and (loopback or silent):
        # crate: filter/mask and mode are exclusive in one config message
        raise ProtocolError("filter/mask not compatible with loopback/silent mode")
    byts = [
        HEADER,
        HEADER_TYPE_FIXED,
        TYPE_CFG | (TYPE_CFG_SET_VARIABLE if variable else 0),
        can_baud_code(can_baud),
        ANY_STANDARD if standard_only else ANY_EXTENDED,
        *_id_slots(filter_id, False),
        *_id_slots(mask_id, False),
        (MODE_LOOPBACK if loopback else 0) | (MODE_SILENT if silent else 0),
        RETRAN_ENABLED if retransmit else RETRAN_DISABLED,
        0, 0, 0, 0,  # reserved
        0,           # checksum placeholder
    ]
    assert len(byts) == MAX_FIXED
    out = bytearray(byts)
    out[-1] = checksum(bytes(out))
    return bytes(out)


def encode_serial_baud(rate: int) -> bytes:
    """20-byte "set serial baud rate" message (impl ToFixedMessage for SerialBaudRate)."""
    byts = [
        HEADER,
        HEADER_TYPE_FIXED,
        TYPE_CFG | TYPE_CFG_SET_SERIAL_BAUD,
        serial_baud_code(rate),
        0,           # frame type placeholder
        0, 0, 0, 0,  # filter
        0, 0, 0, 0,  # mask
        0,           # mode
        RETRAN_ENABLED,
        0, 0, 0, 0,
        0,
    ]
    assert len(byts) == MAX_FIXED
    out = bytearray(byts)
    out[-1] = checksum(bytes(out))
    return bytes(out)


def encode_id_filter(mode: str, ids: list[int]) -> bytes:
    """Stored ID filter (allow/disabled/block) — variable-length message.

    mode: 'disabled' | 'allow' | 'block'; ids — list of CAN ids (std<=0x7FF or ext)
    """
    if mode == "disabled":
        t = TYPE_CFG_SET_ID_FILTER | TYPE_CFG_SET_ID_FILTER_BLOCKLIST
        ids = []
    elif mode == "block":
        if not ids:
            raise ProtocolError("blocklist requires at least one id")
        t = TYPE_CFG_SET_ID_FILTER | TYPE_CFG_SET_ID_FILTER_BLOCKLIST
    elif mode == "allow":
        if not ids:
            raise ProtocolError("allowlist requires at least one id")
        t = TYPE_CFG_SET_ID_FILTER
    else:
        raise ProtocolError(f"unknown filter mode: {mode}")
    byts = [HEADER, HEADER_TYPE_FIXED, t, len(ids)]
    for i in ids:
        byts.extend(_id_slots(i, i > 0x7FF))
    byts.append(0)  # checksum placeholder
    out = bytearray(byts)
    out[-1] = checksum(bytes(out))
    return bytes(out)


def encode_frame(
    can_id: int,
    data: bytes | list[int],
    extended: bool = False,
    remote: bool = False,
    variable: bool = False,
) -> bytes:
    """Frame message, fixed (20B) or variable (variable length).

    remote frame: data may be empty (the crate fills dummy bytes in fixed —
    in the variable case there is no data, only the dlc bits in the type byte).
    """
    data = bytes(data)
    if remote and len(data) > MAX_DATA:
        raise ProtocolError("overlong remote data")
    if len(data) > MAX_DATA:
        raise ProtocolError(f"too long data: {len(data)}")
    if not extended and can_id > 0x7FF:
        raise ProtocolError(f"id {can_id:#x} > 0x7FF, extended needed")

    if variable:
        t = VAR_BASE | (VAR_EXT if extended else 0) | (VAR_REMOTE if remote else 0) | len(data)
        if remote:
            payload: list[int] = []  # in variable mode, remote frames carry no data
        else:
            payload = list(data)
        byts = [HEADER, t, *_id_slots(can_id, extended)[: (4 if extended else 2)], *payload, VAR_END]
        return bytes(byts)

    # fixed layout (impl ToFixedMessage for Frame)
    dlc = len(data)
    pad = list(data) + [0] * (MAX_DATA - dlc)
    byts = [
        HEADER,
        HEADER_TYPE_FIXED,
        TYPE_FRAME,
        ANY_EXTENDED if extended else ANY_STANDARD,
        FRAME_REMOTE if remote else FRAME_DATA,
        *_id_slots(can_id, extended),
        dlc,
        *pad,
        0,         # reserved
        0,         # checksum placeholder
    ]
    assert len(byts) == MAX_FIXED
    out = bytearray(byts)
    out[-1] = checksum(bytes(out))
    return bytes(out)


@dataclass
class FrameStore:
    """Decoded CAN frame (the receiver-side analogue of embedded_can::Frame)."""

    id: int
    extended: bool = False
    remote: bool = False
    dlc: int = 0
    data: bytes = b""

    @property
    def head(self) -> int:
        return f"{self.id:03X}" if self.id <= 0x7FF and not self.ext else f"{self.id:08X}"


# --- decoders (with resync, as ReceiveBuffer in the crate) -------------------

class ProtocolDecoder:
    """Takes a byte stream → FrameStore. Mode: 'fixed' or 'variable'.

    Resync logic — 1:1 with ReceiveBuffer: on error we drop **one** byte
    and start over; the error counter resets on resync.
    """

    def __init__(self, mode: str = "fixed"):
        self.mode = mode
        self._buf = bytearray()
        self._errors_left = SYNC_ATTEMPTS_MAX

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def frames(self) -> list[FrameStore]:
        """Reads all complete frames from the buffer (the buffer is consumed).

        Resync logic — like ReceiveBuffer::receive_frame in Rust:
        error on a byte → skip that byte and try again;
        the error counter is reset once it is exhausted (this is not a
        buffer reset, just a guard against an infinite loop).
        """
        out: list[FrameStore] = []
        while True:
            try:
                f, eat = self._parse_at(self._buf)
            except ProtocolError:
                # error: skip one byte and retry (resync search)
                if self._buf:
                    self._buf = self._buf[1:]
                if self._errors_left <= 0:
                    self._errors_left = SYNC_ATTEMPTS_MAX
                else:
                    self._errors_left -= 1
                continue
            if f is None:
                break  # not enough data yet
            out.append(f)
            self._buf = self._buf[eat:]
            self._errors_left = SYNC_ATTEMPTS_MAX
            if eat == 0:  # safety: do not loop
                break
        return out

    def _parse_at(self, buf: bytes) -> tuple[FrameStore | None, int]:
        """Try parse one frame at buf[0]. Return (None, 0) if not enough data
        yet, or (frame, eaten) on success. Raise ProtocolError on error."""
        if self.mode == "fixed":
            return self._fixed_at(buf)
        return self._var_at(buf)

    def _fixed_at(self, buf: bytes) -> tuple[FrameStore | None, int]:
        if len(buf) < MAX_FIXED:
            return None, 0
        seg = bytes(buf[:MAX_FIXED])
        if seg[0] != HEADER:
            raise ProtocolError("expected 0xAA header")
        if seg[1] != HEADER_TYPE_FIXED:
            raise ProtocolError("expected 0x55")
        if seg[2] != TYPE_FRAME:
            raise ProtocolError("not a frame message")
        if seg[3] not in (ANY_STANDARD, ANY_EXTENDED):
            raise ProtocolError("bad any flag")
        if seg[4] not in (FRAME_DATA, FRAME_REMOTE):
            raise ProtocolError("bad data/remote flag")
        ext = seg[3] == ANY_EXTENDED
        remote = seg[4] == FRAME_REMOTE
        can_id = 0
        for i in range(4):
            can_id |= seg[5 + i] << (8 * i)
        if not ext and can_id > 0x7FF:
            raise ProtocolError(f"overlong standard id {can_id:#x}")
        dlc = seg[9]
        if dlc > MAX_DATA:
            raise ProtocolError(f"bad dlc {dlc}")
        if seg[19] != checksum(seg):
            raise ProtocolError("checksum mismatch")
        data = bytes(seg[10 : 10 + dlc])
        return FrameStore(id=can_id, extended=ext, remote=remote, dlc=dlc, data=data), MAX_FIXED

    def _var_at(self, buf: bytes) -> tuple[FrameStore | None, int]:
        n = len(buf)
        if n < 2:
            return None, 0
        if buf[0] != HEADER:
            raise ProtocolError("expected 0xAA")
        t = buf[1]
        if t & VAR_BASE != VAR_BASE:
            raise ProtocolError("expected 0xC0 var flag")
        dlc = t & VAR_DLC_MASK
        ext = bool(t & VAR_EXT)
        remote = bool(t & VAR_REMOTE)
        idn = 4 if ext else 2
        need = 2 + idn + dlc + 1
        if dlc > MAX_DATA:
            raise ProtocolError(f"bad dlc {dlc}")
        if n < need:
            return None, 0
        if buf[need - 1] != VAR_END:
            raise ProtocolError(f"bad end marker {buf[need - 1]:#04x} (0x55)")
        can_id = 0
        for i in range(idn):
            can_id |= buf[2 + i] << (8 * i)
        if not ext and can_id > 0x7FF:
            raise ProtocolError(f"overlong standard id {can_id:#x}")
        data = b"" if remote else bytes(buf[2 + idn : 2 + idn + dlc])
        return FrameStore(id=can_id, extended=ext, remote=remote, dlc=dlc, data=data), need

    def reset(self) -> None:
        self._buf.clear()
        self._errors_left = SYNC_ATTEMPTS_MAX