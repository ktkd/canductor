"""Raw serial port (termios) — the exact recipe that worked on a live CH340:

  O_RDWR | O_NOCTTY | O_NONBLOCK
  iflag:  disable IGNBRK BRKINT PARMRK ISTRIP
          INLCR IGNCR ICRNL IXON
  oflag:  no OPOST (raw)
  lflag:  no ECHO ECHONL ICANON ISIG IEXTEN
  cflag:  CS8, **2 stop bits** (CSTOPB), no parity, CLOCAL CREAD,
          custom baud via BOTHER:
              cflag &= ~CBAUD_MASK (0o10017); cflag |= BOTHER (0o10000)
              ispeed = ospeed = baud
  after setup — TCFLSH/CIOFLUSH.

Identical to the behaviour of patched serialport-rs
(src/posix/termios.rs:216-221), on which the Rust crate's hw tests pass.
"""
from __future__ import annotations

import fcntl
import os
import select
import struct
import termios

CBAUD_MASK = 0o10017
BOTHER = 0o10000


class SerialError(OSError):
    pass


class SerialPort:
    """Minimal raw serial (fd-based). Threading-friendly: all
    blocking/non-blocking operations go through select, no mutexes (one
    thread reads, another writes)."""

    def __init__(self, path: str, baud: int = 2_000_000) -> None:
        self.path = path
        self.baud = baud
        self.fd = -1

    # --- setup --------------------------------------------------------------

    def open(self) -> "SerialPort":
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            raise SerialError(f"failed to open {self.path}: {e}") from e
        try:
            iflag, oflag, cflag, lflag, ispeed, ospeed, ccc = termios.tcgetattr(self.fd)
            iflag &= ~(
                termios.IGNBRK
                | termios.BRKINT
                | termios.PARMRK
                | termios.ISTRIP
                | termios.INLCR
                | termios.IGNCR
                | termios.ICRNL
                | termios.IXON
            )
            oflag &= ~termios.OPOST
            lflag &= ~(
                termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN
            )
            cflag &= ~(termios.CSIZE | termios.PARENB | termios.CRTSCTS | CBAUD_MASK)
            cflag |= termios.CS8 | termios.CSTOPB | termios.CLOCAL | termios.CREAD | BOTHER
            termios.tcsetattr(self.fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, self.baud, self.baud, ccc])
            self.flush()
        except OSError as e:
            # termios/ioctl failure after os.open: close the fd, surface as SerialError
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
            raise SerialError(f"termios setup for {self.path} failed: {e}") from e
        return self

    @property
    def is_open(self) -> bool:
        return self.fd >= 0

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

    # --- io ------------------------------------------------------------------

    def flush(self) -> None:
        """TCFLSH in the TCIOFLUSH mode (single int argument — otherwise ENOTTY/EINVAL on ch341)."""
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, termios.TCFLSH, termios.TCIOFLUSH)
            except OSError as e:
                raise SerialError(f"flush {self.path}: {e}") from e

    def write(self, data: bytes) -> int:
        if self.fd < 0:
            raise SerialError("port closed")
        n = 0
        while n < len(data):
            try:
                n += os.write(self.fd, memoryview(data)[n:])
            except BlockingIOError:
                # wait for the fd to become *writable* (second list!) and retry
                _, w, _ = select.select([], [self.fd], [], 0.05)
                if not w:
                    continue
            except OSError as e:
                raise SerialError(f"write {self.path}: {e}") from e
        return n

    def read(self, timeout_s: float = 0.0) -> bytes:
        """Reads whatever is available (timeout in seconds, 0 = non-blocking)."""
        if self.fd < 0:
            return b""
        r, _, _ = select.select([self.fd], [], [], max(0.0, timeout_s))
        if not r:
            return b""
        try:
            return os.read(self.fd, 65536)
        except BlockingIOError:
            return b""
        except OSError as e:
            raise SerialError(f"read {self.path}: {e}") from e

    def __enter__(self) -> "SerialPort":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()