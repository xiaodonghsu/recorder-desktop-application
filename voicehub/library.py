"""Card file wire layouts and a portable, versioned local recording library."""

import hashlib
import json
import struct
import uuid
from datetime import UTC, datetime
from pathlib import Path


def parse_file_list(body):
    if len(body) < 4:
        raise ValueError("文件列表缺少条目数")
    count = int.from_bytes(body[:4], "big")
    if not count:
        if len(body) != 4:
            raise ValueError("空文件列表长度错误")
        return []
    width, remainder = divmod(len(body) - 4, count)
    if remainder or width not in (28, 33):
        raise ValueError("不支持的文件列表条目长度")
    result = []
    for pos in range(4, len(body), width):
        raw = body[pos : pos + width]
        duration, size = struct.unpack_from(">II", raw)
        name = raw[8:].split(b"\0", 1)[0].decode("utf-8", errors="strict")
        if not name:
            raise ValueError("设备返回空文件名")
        result.append({"name": name, "duration": duration, "size": size, "raw": raw.hex()})
    return result


def download_names(name):
    base = name.rstrip(".")
    for suffix in (".opus", ".wav", ".mp3"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    return list(dict.fromkeys((base + ".opus", base + ".wav", name)))


def timestamp():
    return datetime.now(UTC).isoformat()


class Library:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def folder(self, record_id):
        if len(record_id) != 32 or any(c not in "0123456789abcdef" for c in record_id):
            raise ValueError("无效的本地录音 ID")
        folder = self.root / record_id
        if folder.is_symlink():
            raise ValueError("录音目录不能是符号链接")
        return folder

    def read(self, record_id):
        return json.loads((self.folder(record_id) / "record.json").read_text(encoding="utf-8"))

    def write(self, record):
        folder = self.folder(record["id"])
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / "record.json.part"
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(folder / "record.json")

    def records(self):
        records = []
        if self.root.exists():
            for path in self.root.glob("*/record.json"):
                try:
                    record = self.read(path.parent.name)
                    if record["id"] != path.parent.name:
                        continue
                    records.append(record)
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(records, key=lambda r: r["created_at"], reverse=True)

    def create(self, device, entry):
        record = {
            "schema_version": 1,
            "id": uuid.uuid4().hex,
            "device": device,
            "source": entry,
            "created_at": timestamp(),
            "download_status": "pending",
            "transcript": None,
            "analyses": [],
        }
        self.write(record)
        return record

    def audio_path(self, record_id):
        folder = self.folder(record_id)
        return folder / "audio.opus"

    @staticmethod
    def _digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def complete(self, record_id):
        record = self.read(record_id)
        path = self.audio_path(record_id)
        record.update(
            download_status="complete",
            bytes=path.stat().st_size,
            sha256=self._digest(path),
        )
        self.write(record)
        return record

    def save_transcript(self, record_id, text, segments=None, edited=False):
        record = self.read(record_id)
        previous = record.get("transcript") or {}
        record["transcript"] = {
            "text": text,
            "segments": segments if segments is not None else previous.get("segments", []),
            "engine": previous.get("engine", "manual") if edited else "SenseVoice",
            "segments_match_text": not edited,
            "updated_at": timestamp(),
            "edited": edited,
            "revision": previous.get("revision", 0) + 1,
        }
        self.write(record)
        (self.folder(record_id) / "transcript.txt").write_text(text, encoding="utf-8")
        return record

    def analysis_input(self, record_id):
        """Stable JSON input for future summarizers/parsers; no model dependency."""
        record = self.read(record_id)
        if not record.get("transcript"):
            raise ValueError("请先完成 ASR 转写")
        return {
            "schema_version": 1,
            "recording_id": record_id,
            "source": record["source"],
            "audio_sha256": record.get("sha256"),
            "transcript": record["transcript"],
        }

    def save_analysis(self, record_id, kind, model, result, transcript_revision):
        record = self.read(record_id)
        record["analyses"].append(
            {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "model": model,
                "result": result,
                "transcript_revision": transcript_revision,
                "created_at": timestamp(),
            }
        )
        self.write(record)

    def delete(self, record_id):
        # Delete only known application files, never arbitrary paths from a manifest.
        folder = self.folder(record_id)
        for name in (
            "audio.opus",
            "audio.opus.part",
            "audio.wav",
            "audio.wav.part",
            "audio.txt",
            "audio.segments.json",
            "transcript.txt",
            "analysis-input.json",
            "record.json.part",
        ):
            (folder / name).unlink(missing_ok=True)
        (folder / "record.json").unlink()
        if not any(folder.iterdir()):
            folder.rmdir()
