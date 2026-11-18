"""canductor.core — Waveshare USB-CAN-A USB device handling.

The protocol is byte-compatible with the Rust crate waveshare-usb-can-a
(lib/src/proto.rs) and therefore with mazda-canbus, which "by a happy
accident" works with this hardware.

Modules:
  protocol  — message encode/decode for the bus;
  serial    — raw termios connection (8N2, custom baud via BOTHER);
  device    — async device wrapper (config / tx / rx / stats);
  service   — HTTP microservice on top of device (stdlib asyncio).
"""

from .protocol import (
    FrameStore,
    ProtocolDecoder,
    encode_config,
    encode_serial_baud,
    encode_id_filter,
    encode_frame,
    checksum,
    can_baud_code,
    serial_baud_code,
)
from .serial import SerialPort
from .device import UsbCanDevice, DeviceError, Config

__all__ = [
    "FrameStore",
    "ProtocolDecoder",
    "encode_config",
    "encode_serial_baud",
    "encode_id_filter",
    "encode_frame",
    "checksum",
    "can_baud_code",
    "serial_baud_code",
    "SerialPort",
    "UsbCanDevice",
    "DeviceError",
    "Config",
]