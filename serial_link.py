"""Minimal POSIX serial transport for Makeblock-style plotter firmware."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
import select
import struct
import termios
import time


class SerialError(RuntimeError):
    pass


@dataclass
class SerialReply:
    ok: bool
    lines: list[str]
    timed_out: bool = False


_BAUDS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


class PosixSerial:
    def __init__(self) -> None:
        self.fd: int | None = None
        self.port = ""
        self._buffer = bytearray()

    @property
    def is_open(self) -> bool:
        return self.fd is not None

    def open(self, port: str, baud: int = 115200, dtr: bool = False, rts: bool = False) -> None:
        if self.fd is not None:
            self.close()
        if baud not in _BAUDS:
            raise SerialError(f"Unsupported baud rate {baud}")
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.fd = fd
        self.port = port
        self._set_termios(baud)
        self.set_modem_lines(dtr=dtr, rts=rts)
        self.flush()

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _set_termios(self, baud: int) -> None:
        assert self.fd is not None
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = _BAUDS[baud]
        attrs[5] = _BAUDS[baud]
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 1
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def set_modem_lines(self, dtr: bool | None = None, rts: bool | None = None) -> None:
        assert self.fd is not None
        set_bits = 0
        clear_bits = 0
        if dtr is not None:
            bit = getattr(termios, "TIOCM_DTR", 0x002)
            set_bits |= bit if dtr else 0
            clear_bits |= 0 if dtr else bit
        if rts is not None:
            bit = getattr(termios, "TIOCM_RTS", 0x004)
            set_bits |= bit if rts else 0
            clear_bits |= 0 if rts else bit
        if set_bits:
            req = getattr(termios, "TIOCMBIS", 0x5416)
            fcntl.ioctl(self.fd, req, struct.pack("I", set_bits))
        if clear_bits:
            req = getattr(termios, "TIOCMBIC", 0x5417)
            fcntl.ioctl(self.fd, req, struct.pack("I", clear_bits))

    def flush(self) -> None:
        assert self.fd is not None
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self._buffer.clear()

    def write_line(self, line: str) -> None:
        assert self.fd is not None
        data = line.rstrip("\r\n").encode("ascii", "replace") + b"\n"
        os.write(self.fd, data)

    def read_available_lines(self, timeout: float = 0.0) -> list[str]:
        assert self.fd is not None
        end = time.monotonic() + timeout
        lines: list[str] = []
        while True:
            wait = max(0.0, end - time.monotonic()) if timeout else 0.0
            ready, _, _ = select.select([self.fd], [], [], wait)
            if not ready:
                break
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                chunk = b""
            if not chunk:
                break
            self._buffer.extend(chunk)
            while True:
                idx_n = self._buffer.find(b"\n")
                idx_r = self._buffer.find(b"\r")
                candidates = [i for i in (idx_n, idx_r) if i >= 0]
                if not candidates:
                    break
                idx = min(candidates)
                raw = bytes(self._buffer[:idx])
                del self._buffer[: idx + 1]
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    lines.append(text)
            if not timeout:
                break
            if time.monotonic() >= end:
                break
        return lines

    def command(self, line: str, timeout: float = 30.0) -> SerialReply:
        self.write_line(line)
        lines: list[str] = []
        end = time.monotonic() + timeout
        saw_ok = False
        while time.monotonic() < end:
            for reply in self.read_available_lines(timeout=0.1):
                lines.append(reply)
                low = reply.lower()
                if low == "ok" or low.endswith(" ok"):
                    saw_ok = True
                    return SerialReply(True, lines)
                if low.startswith("error") or low.startswith("huh"):
                    return SerialReply(False, lines)
        return SerialReply(saw_ok, lines, timed_out=True)


def list_serial_ports() -> list[str]:
    roots = ["/dev/serial/by-id", "/dev/ttyUSB0", "/dev/ttyACM0"]
    ports: list[str] = []
    by_id = roots[0]
    try:
        for name in sorted(os.listdir(by_id)):
            ports.append(os.path.join(by_id, name))
    except FileNotFoundError:
        pass
    for name in ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0", "/dev/ttyACM1"]:
        if os.path.exists(name):
            ports.append(name)
    unique: list[str] = []
    for port in ports:
        if port not in unique:
            unique.append(port)
    return unique
