"""AE20 protocol; command numbers are decimal, CRC covers LEN + DATA."""

import binascii
import re
import struct
from dataclasses import dataclass

SERVICE, WRITE, MAIN, KEY = (f"0000ae{n:02x}-0000-1000-8000-00805f9b34fb" for n in (32, 33, 34, 35))


def auth_key(value: str) -> bytes:
    value = value.strip()
    if not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{64}", value):
        raise ValueError("AuthKey 必须为 64 位十六进制字符，可带 0x 前缀")
    return bytes.fromhex(value.removeprefix("0x").removeprefix("0X"))


def encode(seq: int, kind: int, cmd: int, body: bytes = b"") -> bytes:
    data = bytes((kind, cmd)) + body
    if not 1 <= len(data) <= 8192:
        raise ValueError("协议 DATA 长度超限")
    tail = struct.pack("<H", len(data)) + data
    return bytes((0x5A, seq & 255)) + struct.pack("<H", binascii.crc_hqx(tail, 0)) + tail


@dataclass(frozen=True)
class Frame:
    seq: int
    kind: int
    cmd: int | None
    body: bytes


class Parser:
    def __init__(self):
        self.buffer = bytearray()
        self.crc_errors = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        self.buffer.extend(chunk)
        result = []
        while self.buffer:
            start = self.buffer.find(b"\x5a")
            if start < 0:
                self.buffer.clear()
                break
            del self.buffer[:start]
            if len(self.buffer) < 6:
                break
            crc, size = struct.unpack_from("<HH", self.buffer, 2)
            if not 1 <= size <= 8192:
                del self.buffer[0]
                continue
            if len(self.buffer) < 6 + size:
                break
            if binascii.crc_hqx(self.buffer[4 : 6 + size], 0) != crc:
                self.crc_errors += 1
                del self.buffer[0]
                continue
            data = self.buffer[6 : 6 + size]
            result.append(
                Frame(self.buffer[1], data[0], data[1] if size > 1 else None, bytes(data[2:]))
            )
            del self.buffer[: 6 + size]
        return result
