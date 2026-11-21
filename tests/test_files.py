import json
import os
import struct
import wave
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox
from test_device import FakeClient

from voicehub.audio import decode_card_file
from voicehub.device import Recorder
from voicehub.library import Library, download_names, parse_file_list
from voicehub.protocol import Parser
from voicehub.ui import Window
from voicehub.worker import Worker


def entry_body(name="note_2026-11-02-07-46-02.", width=25):
    return struct.pack(">II", 20, 80) + name.encode().ljust(width, b"\0")


@pytest.mark.parametrize("width", [20, 25])
def test_list_layout_and_trailing_dot(width):
    raw = entry_body("record.", width)
    entry = parse_file_list(struct.pack(">I", 1) + raw)[0]
    assert entry == {"name": "record.", "size": 80, "duration": 20, "raw": raw.hex()}
    assert download_names(entry["name"]) == ["record.opus", "record.wav", "record."]
    assert download_names("record.opus") == ["record.opus", "record.wav"]


@pytest.mark.parametrize("body", [b"", b"\0\0\0\0x", b"\0\0\0\x01bad"])
def test_malformed_list_rejected(body):
    with pytest.raises(ValueError):
        parse_file_list(body)


class FilesClient(FakeClient):
    address = "device-a"
    wav_fallback = False
    download_code = 0
    disconnect_download = False
    delete_code = 0

    async def write_gatt_char(self, char, data, response):
        await super().write_gatt_char(char, data, response)
        frame = Parser().feed(data)[0]
        if frame.kind != 2:
            return
        if frame.cmd == 0:
            self.emit(2, 1, struct.pack(">I", 1) + entry_body())
            self.emit(2, 1, struct.pack(">I", 1) + entry_body("second.", 20))
            self.emit(2, 18, b"\0")
        elif frame.cmd == 2:
            name = frame.body[4:].rstrip(b"\0")
            assert len(frame.body) == 33
            assert frame.body[:4] == bytes(4)
            if self.wav_fallback and name.endswith(b".opus"):
                self.emit(2, 5, b"\x01")
                return
            self.emit(2, 3, name)
            self.emit(2, 4, b"a" * 40)
            if self.disconnect_download:
                await self.disconnect()
                return
            self.emit(2, 4, b"b" * 40)
            self.emit(2, 5, bytes([self.download_code]))
        elif frame.cmd == 8:
            assert frame.body == entry_body()
            self.emit(2, 13, bytes([self.delete_code]))


async def connected(client=FilesClient):
    recorder = Recorder(client_factory=client)
    await recorder.connect(SimpleNamespace(name="CAC10"), "ab" * 32)
    return recorder


async def test_multi_frame_list_download_and_raw_delete(tmp_path):
    recorder = await connected()
    try:
        entries = await recorder.list_files()
        assert len(entries) == 2
        target = tmp_path / "file.opus"
        await recorder.download_file(entries[0], target)
        assert target.read_bytes() == b"a" * 40 + b"b" * 40
        assert not target.with_suffix(".opus.part").exists()
        assert recorder.client.writes[-1].body[4:].rstrip(b"\0").endswith(b".opus")
        await recorder.delete_file(entries[0])
    finally:
        await recorder.disconnect()


async def test_download_fallback_only_when_not_found(tmp_path):
    class Fallback(FilesClient):
        wav_fallback = True

    recorder = await connected(Fallback)
    try:
        entry = (await recorder.list_files())[0]
        await recorder.download_file(entry, tmp_path / "audio.opus")
        names = [
            f.body[4:].rstrip(b"\0") for f in recorder.client.writes if (f.kind, f.cmd) == (2, 2)
        ]
        assert names == [b"note_2026-11-02-07-46-02.opus", b"note_2026-11-02-07-46-02.wav"]
    finally:
        await recorder.disconnect()


@pytest.mark.parametrize("disconnect", [False, True])
async def test_failed_download_keeps_partial_and_disconnects(tmp_path, disconnect):
    class Failed(FilesClient):
        download_code = 3
        disconnect_download = disconnect

    recorder = await connected(Failed)
    entry = (await recorder.list_files())[0]
    target = tmp_path / "audio.opus"
    with pytest.raises((ValueError, ConnectionError)):
        await recorder.download_file(entry, target)
    assert not target.exists()
    assert target.with_suffix(".opus.part").read_bytes().startswith(b"a" * 40)
    assert not recorder.authorized
    assert recorder.file_handler is None


async def test_delete_failure_is_not_success():
    class Failed(FilesClient):
        delete_code = 1

    recorder = await connected(Failed)
    try:
        with pytest.raises(ValueError, match="未确认删除"):
            await recorder.delete_file((await recorder.list_files())[0])
    finally:
        await recorder.disconnect()


async def test_list_requires_explicit_completion():
    class Incomplete(FilesClient):
        async def write_gatt_char(self, char, data, response):
            frame = Parser().feed(data)[0]
            if (frame.kind, frame.cmd) == (2, 0):
                self.emit(2, 1, struct.pack(">I", 1) + entry_body())
                self.emit(2, 18, b"\x01")
            else:
                await super().write_gatt_char(char, data, response)

    recorder = await connected(Incomplete)
    with pytest.raises(ValueError, match="未确认"):
        await recorder.list_files()
    assert not recorder.authorized


def test_library_transcript_and_analysis_lifecycle(tmp_path):
    library = Library(tmp_path)
    record = library.create(
        "device-a", parse_file_list(struct.pack(">I", 1) + entry_body())[0], sn="SN-A"
    )
    key = record["id"]
    library.audio_path(key).write_bytes(b"audio")
    library.complete(key)
    library.save_transcript(key, "原文", [{"start": 0, "end": 1, "text": "原文"}])
    library.save_transcript(key, "编辑后的文字", edited=True)
    data = Library(tmp_path).analysis_input(key)
    assert data["transcript"]["revision"] == 2
    assert data["transcript"]["text"] == "编辑后的文字"
    assert data["transcript"]["segments"][0]["start"] == 0
    library.save_analysis(key, "summary", "future-model", {"summary": "摘要"}, 2)
    assert library.read(key)["analyses"][0]["transcript_revision"] == 2
    other = library.create("device-b", record["source"], sn="SN-B")
    assert other["id"] != key
    library.delete(key)
    assert [r["id"] for r in library.records()] == [other["id"]]
    with pytest.raises(ValueError):
        library.folder("../bad")


def test_card_wav_content_decoded_independent_of_extension(tmp_path):
    source = tmp_path / "audio.opus"
    with wave.open(str(source), "wb") as output:
        output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(bytes(48000 * 4))
    result = decode_card_file(source)
    with wave.open(str(result)) as output:
        assert (output.getframerate(), output.getnchannels(), output.getsampwidth()) == (
            16000,
            1,
            2,
        )
        assert output.getnframes() == 16000
    source.write_bytes(b"bad raw")
    with pytest.raises(ValueError):
        decode_card_file(source)


async def test_worker_download_asr_keeps_portable_metadata(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    worker = Worker()
    worker.recorder = await connected()
    entry = (await worker.recorder.list_files())[0]

    class FakeTranscriber:
        def __init__(self, root):
            pass

        def transcribe(self, wav):
            wav.with_suffix(".segments.json").write_text(
                json.dumps([{"start": 0, "end": 1, "text": "测试"}])
            )
            return "测试"

    monkeypatch.setattr("voicehub.worker.decode_card_file", lambda path: path.with_suffix(".wav"))
    monkeypatch.setattr("voicehub.worker.Transcriber", FakeTranscriber)
    try:
        ids = await worker.file_job("file_download_asr", tmp_path, [entry], tmp_path)
        assert Library(tmp_path).read(ids[0])["transcript"]["text"] == "测试"
        assert Library(tmp_path).read(ids[0])["download_status"] == "complete"
    finally:
        await worker.recorder.disconnect()
        worker.loop.close()
        app.processEvents()


def test_file_manager_selection_autosave_and_state(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path, tmp_path, auto_scan=False)
    try:
        window.open_files()
        manager = window.file_manager
        manager.root = tmp_path / "Sounds"
        record = manager.library.create(
            "device", parse_file_list(struct.pack(">I", 1) + entry_body())[0], sn="SN-A"
        )
        manager.library.audio_path(record["id"]).write_bytes(b"audio")
        manager.library.complete(record["id"])
        manager.library.save_transcript(record["id"], "原文", [])
        manager.refresh_local()
        manager.local_list.setCurrentRow(0)
        manager.editor.setPlainText("修改")
        assert manager.library.read(record["id"])["transcript"]["text"] == "修改"
        window.on_event("authorized", "CAC10")
        manager.set_card([record["source"]])
        manager.card_list.setCurrentRow(0)
        assert manager.card_buttons[1].isEnabled()
        window.busy = True
        window.update_controls()
        assert not manager.card_buttons[1].isEnabled()
        assert manager.editor.isReadOnly()
        window.busy = False
        window.on_event("disconnected", None)
        assert manager.card_list.count() == 0
        assert manager.local_list.count() == 1
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
        manager.delete_local()
        assert manager.local_list.count() == 0
        assert manager.editor.toPlainText() == ""
    finally:
        window.busy = False
        window.quit()
        assert window.worker.wait(5000)
        app.processEvents()
        window.hide()


def test_raw_card_opus_is_decoded_with_fixed_packet_boundaries(tmp_path):
    source = tmp_path / "audio.opus"
    source.write_bytes((b"\x48" + bytes(39)) * 5)
    with wave.open(str(decode_card_file(source))) as wav:
        assert wav.getnframes() == 1600
        assert wav.getframerate() == 16000
    source.write_bytes(source.read_bytes()[:-1])
    with pytest.raises(ValueError):
        decode_card_file(source)
    assert not source.with_suffix(".wav.part").exists()


async def test_asr_failure_retains_audio_previous_text_and_status(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    worker = Worker()
    library = Library(tmp_path)
    record = library.create(
        "card", parse_file_list(struct.pack(">I", 1) + entry_body())[0], sn="SN-A"
    )
    key = record["id"]
    library.audio_path(key).write_bytes(b"audio")
    library.complete(key)
    library.save_transcript(key, "保留已有文字", [])

    def fail_decode(path):
        raise ValueError("无法解码")

    monkeypatch.setattr("voicehub.worker.decode_card_file", fail_decode)
    try:
        with pytest.raises(RuntimeError, match="无法解码"):
            await worker.file_job("file_asr", tmp_path, [key], tmp_path)
        saved = library.read(key)
        assert saved["download_status"] == "complete"
        assert saved["asr"]["status"] == "failed"
        assert saved["transcript"]["text"] == "保留已有文字"
        assert library.audio_path(key).read_bytes() == b"audio"
    finally:
        worker.loop.close()
        app.processEvents()


def test_directory_choice_is_remembered(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    saved_settings = {}

    class FakeSettings:
        def __init__(self, *args):
            pass

        def value(self, key, default):
            return saved_settings.get(key, default)

        def setValue(self, key, value):
            saved_settings[key] = value

    monkeypatch.setattr("voicehub.file_manager.QSettings", FakeSettings)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "voicehub.file_manager.QFileDialog.getExistingDirectory",
        lambda *args: str(tmp_path / "chosen"),
    )
    window = Window(tmp_path, tmp_path, auto_scan=False)
    try:
        window.open_files()
        manager = window.file_manager
        assert manager.root == tmp_path / "Sounds"
        manager.choose_directory()
        assert manager.root == tmp_path / "chosen"
        from voicehub.file_manager import FileManager

        reopened = FileManager(window)
        assert reopened.root == tmp_path / "chosen"
        reopened.hide()
    finally:
        window.quit()
        assert window.worker.wait(5000)
        app.processEvents()
        window.hide()


def test_sn_and_exact_filename_identify_record_across_addresses(tmp_path):
    library = Library(tmp_path)
    entry = {"name": "录音../note.", "raw": "", "size": 80, "duration": 20}
    record = library.create("old-address", entry, sn="SN-A")
    library.save_transcript(record["id"], "用户编辑的文字", edited=True)
    library.save_analysis(record["id"], "summary", "test", "保留的解析", 1)
    saved = library.read(record["id"])
    assert Library(tmp_path).create("new-address", entry, sn="SN-A") == saved
    assert library.create("old-address", entry, sn="SN-B")["id"] != record["id"]
    assert (
        library.create("old-address", {**entry, "name": "录音../note.opus"}, sn="SN-A")["id"]
        != record["id"]
    )
    assert library.analysis_input(record["id"])["device_sn"] == "SN-A"
    assert len(library.records()) == 3
    assert library.folder(record["id"]).parent == tmp_path


@pytest.mark.parametrize("sn", [None, "", "  "])
def test_library_requires_sn_for_new_record(tmp_path, sn):
    with pytest.raises(ValueError, match="SN"):
        Library(tmp_path).create("address", {"name": "record."}, sn=sn)
    assert not list(tmp_path.iterdir())


async def test_worker_reuses_complete_record_without_overwriting_transcript(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    worker = Worker()
    worker.recorder = await connected()
    entry = (await worker.recorder.list_files())[0]
    library = Library(tmp_path)
    try:
        ids = await worker.file_job("file_download", tmp_path, [entry])
        library.save_transcript(ids[0], "用户修改", edited=True)
        library.save_analysis(ids[0], "summary", "test", "已有解析", 1)
        saved = library.read(ids[0])
        worker.recorder.client.address = "new-address"

        async def forbid_download(*args):
            pytest.fail("Complete recordings must be reused")

        monkeypatch.setattr(worker.recorder, "download_file", forbid_download)
        monkeypatch.setattr(
            worker, "transcribe_library", lambda *args: pytest.fail("Preserve text")
        )
        assert await worker.file_job("file_download_asr", tmp_path, [entry], tmp_path) == ids
        assert library.read(ids[0]) == saved
        assert len(library.records()) == 1
    finally:
        await worker.recorder.disconnect()
        worker.loop.close()
        app.processEvents()


async def test_worker_failed_download_retries_same_record(tmp_path):
    class Failed(FilesClient):
        download_code = 3

    app = QApplication.instance() or QApplication([])
    worker = Worker()
    worker.recorder = await connected(Failed)
    entry = (await worker.recorder.list_files())[0]
    library = Library(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="当前文件失败"):
            await worker.file_job("file_download", tmp_path, [entry])
        failed = library.records()[0]
        assert failed["sn"] == "G1000897"
        assert failed["download_status"] == "failed"
        assert worker.recorder.sn is None
        partial = library.audio_path(failed["id"]).with_suffix(".opus.part")
        assert partial.exists()
        worker.recorder = await connected()
        ids = await worker.file_job("file_download", tmp_path, [entry])
        assert ids == [failed["id"]]
        assert len(library.records()) == 1
        assert library.read(ids[0])["download_status"] == "complete"
        assert "download_error" not in library.read(ids[0])
        assert not partial.exists()
    finally:
        await worker.recorder.disconnect()
        worker.loop.close()
        app.processEvents()


async def test_worker_missing_sn_prevents_download_but_allows_listing(tmp_path):
    class WithoutSerial(FilesClient):
        serial_number = b""

    app = QApplication.instance() or QApplication([])
    worker = Worker()
    worker.recorder = await connected(WithoutSerial)
    try:
        entries = await worker.file_job("file_list")
        assert entries
        with pytest.raises(ValueError, match="SN"):
            await worker.file_job("file_download", tmp_path, entries)
        assert Library(tmp_path).records() == []
        assert worker.recorder.authorized
        assert not any((f.kind, f.cmd) == (2, 2) for f in worker.recorder.client.writes)
    finally:
        await worker.recorder.disconnect()
        worker.loop.close()
        app.processEvents()


def test_legacy_records_without_sn_are_preserved(tmp_path):
    library = Library(tmp_path)
    legacy = {
        "schema_version": 1,
        "id": "a" * 32,
        "device": "address",
        "source": {"name": "record."},
        "created_at": "2026-01-01",
        "download_status": "complete",
        "transcript": {"text": "旧录音文字"},
        "analyses": [],
    }
    library.write(legacy)
    new = library.create("address", legacy["source"], sn="SN-A")
    assert new["id"] != legacy["id"]
    assert library.read(legacy["id"]) == legacy
    assert library.analysis_input(legacy["id"])["device_sn"] is None
    assert len(library.records()) == 2
