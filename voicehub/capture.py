"""Length-prefixed capture preserves BLE payload boundaries, including unknown firmware."""

import json
import struct
import time
from datetime import datetime
from pathlib import Path

MAGIC = b"VHREC001"
MAX_BYTES = 128 * 1024 * 1024


class Capture:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".vhrec")
        self.file = self.path.open("xb")
        self.file.write(MAGIC)
        self.started = time.monotonic()
        self.packets = 0
        self.size = 0
        self.closed = False

    def push(self, payload: bytes):
        if self.closed:
            return
        if not payload:
            raise ValueError("设备发送了空音频包")
        if self.size + len(payload) > MAX_BYTES or time.monotonic() - self.started > 3600:
            raise ValueError("录音达到 128 MiB 或一小时上限，请停止后重新录音")
        self.file.write(struct.pack("<Id", len(payload), time.monotonic() - self.started))
        self.file.write(payload)
        self.packets += 1
        self.size += len(payload)

    def close(self, reason="stopped") -> Path:
        if not self.closed:
            self.closed = True
            self.file.close()
            self.path.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "packets": self.packets,
                        "bytes": self.size,
                        "reason": reason,
                        "duration_received": time.monotonic() - self.started,
                        "format": "length:uint32 LE, elapsed:float64 LE, BLE audio payload",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return self.path


def packets(path: Path):
    with path.open("rb") as stream:
        if stream.read(8) != MAGIC:
            raise ValueError("不是 VoiceHub 录音文件")
        while header := stream.read(12):
            if len(header) != 12:
                raise ValueError("录音包头被截断")
            size, _ = struct.unpack("<Id", header)
            if not 1 <= size <= 8190:
                raise ValueError("录音包长度无效")
            payload = stream.read(size)
            if len(payload) != size:
                raise ValueError("录音包被截断")
            yield payload
