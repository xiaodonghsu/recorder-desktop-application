import asyncio
import json
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .audio import Transcriber, decode_capture, decode_card_file
from .device import Recorder, scan
from .library import Library, timestamp
from .live_asr import LiveTranscribeSession
from .live_audio import LiveDecodeSession


class Worker(QThread):
    event = Signal(str, object)
    finished_job = Signal(str, object)
    failed_job = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.recorder = Recorder(self._recorder_event)
        self.transcriber = None
        self.model_root = None
        self.live_decoder = None
        self.live_mode = "packet"
        self.output_device = None
        self.live_asr_root = None
        self.live_asr = None

    def _recorder_event(self, name, data):
        if name == "capture_started":
            self.live_asr = (
                LiveTranscribeSession(Path(data), self.live_asr_root, self.event.emit)
                if self.live_asr_root
                else None
            )
            self.live_decoder = LiveDecodeSession(
                Path(data),
                self.live_mode,
                self.event.emit,
                output_device=self.output_device,
                pcm_sink=self.live_asr.push if self.live_asr else None,
                pcm_done=self.live_asr.close if self.live_asr else None,
            )
        elif name == "audio":
            if self.live_decoder:
                self.live_decoder.push(data)
            return  # Do not send every audio packet through the Qt GUI event queue.
        elif name == "capture_closed" and self.live_decoder:
            self.live_decoder.close()
        self.event.emit(name, data)

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()

    def submit(self, name, *args):
        async def execute():
            try:
                if name.startswith("file_"):
                    result = await self.file_job(name, *args)
                elif name == "scan":
                    result = await scan()
                elif name == "transcribe":
                    result = await asyncio.to_thread(self.transcribe, *args)
                elif name == "finalize":
                    result = await asyncio.to_thread(self.finalize, *args)
                elif name == "live_finalize":
                    result = await asyncio.to_thread(self.finish_live, *args)
                elif name == "start":
                    self.live_mode = args[1] if len(args) > 1 else "packet"
                    self.output_device = args[2] if len(args) > 2 else None
                    self.live_asr_root = Path(args[3]) if len(args) > 3 else None
                    result = await self.recorder.start(args[0])
                else:
                    result = await getattr(self.recorder, name)(*args)
                self.finished_job.emit(name, result)
            except Exception as exc:  # noqa: BLE001 - marshal worker failures to the Qt UI
                self.failed_job.emit(name, str(exc))

        asyncio.run_coroutine_threadsafe(execute(), self.loop)

    async def file_job(self, name, *args):
        if name == "file_list":
            return await self.recorder.list_files()
        if name == "file_delete":
            deleted = []
            try:
                for entry in args[0]:
                    await self.recorder.delete_file(entry)
                    deleted.append(entry["raw"])
            finally:
                self.event.emit("files_deleted", deleted)
            return deleted
        downloading = name in ("file_download", "file_download_asr")
        if downloading:
            self.recorder._check_files_idle()
            if not self.recorder.sn or not self.recorder.sn.strip():
                raise ValueError("未读取到设备 SN，请刷新设备信息后再下载录音")
        library = Library(args[0])
        results = []
        high_speed = downloading and len(args) > 3 and args[3]
        prepared = False
        for item in args[1]:
            if name in ("file_download", "file_download_asr"):
                record = library.create(
                    str(self.recorder.client.address), item, sn=self.recorder.sn
                )
                record_id = record["id"]
            else:
                record_id = item
            try:
                if name in ("file_download", "file_download_asr"):
                    path = library.audio_path(record_id)
                    if not path.exists():
                        if not prepared:
                            if high_speed:
                                await self.recorder.prepare_high_speed(args[4])
                            else:
                                await self.recorder.use_bluetooth_transfer()
                            prepared = True
                        # A retry starts from the beginning in the same record.
                        path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)
                        record.update(download_status="pending")
                        record.pop("download_error", None)
                        library.write(record)
                        await self.recorder.download_file(item, path, high_speed=high_speed)
                        record = library.complete(record_id)
                    elif record["download_status"] != "complete":
                        # Download atomically finalized before a previous metadata save failed.
                        record = library.complete(record_id)
                    else:
                        self.event.emit("file_progress", f"已存在，复用本地录音：{item['name']}")
                    if name == "file_download_asr" and record.get("transcript") is None:
                        await asyncio.to_thread(
                            self.transcribe_library, library, record_id, args[2]
                        )
                elif name == "file_asr":
                    await asyncio.to_thread(self.transcribe_library, library, record_id, args[2])
                else:
                    raise ValueError("未知文件操作")
                results.append(record_id)
                self.event.emit("library_changed", record_id)
            except Exception as exc:
                if name in ("file_download", "file_download_asr"):
                    record = library.read(record_id)
                    if record["download_status"] != "complete":
                        record.update(download_status="failed", download_error=str(exc))
                    library.write(record)
                self.event.emit("library_changed", record_id)
                raise RuntimeError(f"已完成 {len(results)} 个文件；当前文件失败：{exc}") from exc
        return results

    def transcribe_library(self, library, record_id, root):
        record = library.read(record_id)
        if record["download_status"] != "complete":
            raise ValueError("录音未完整下载，请重新下载后再转写")
        record["asr"] = {"status": "running", "engine": "SenseVoice", "started_at": timestamp()}
        library.write(record)
        try:
            self.event.emit("file_progress", f"正在解码和转写：{record['source']['name']}")
            wav = decode_card_file(library.audio_path(record_id))
            if self.transcriber is None or self.model_root != root:
                self.transcriber = Transcriber(root)
                self.model_root = root
            text = self.transcriber.transcribe(wav)
            segments = json.loads(wav.with_suffix(".segments.json").read_text(encoding="utf-8"))
            library.save_transcript(record_id, text, segments)
        except Exception as exc:
            record = library.read(record_id)
            record["asr"].update(status="failed", error=str(exc), finished_at=timestamp())
            library.write(record)
            raise
        record = library.read(record_id)
        record["asr"].update(status="complete", finished_at=timestamp())
        library.write(record)

    def finalize(self, source):
        live = self.live_decoder
        if live and live.source == source:
            try:
                return live.result()
            finally:
                self.live_decoder = None
        return Path(source).with_suffix(".wav")

    def finish_live(self, source):
        live_asr = self.live_asr
        try:
            self.finalize(source)
            if live_asr and live_asr.source == source:
                return live_asr.result()
            raise RuntimeError("未找到本次实时识别会话，请重新转写原始录音")
        finally:
            if live_asr and live_asr.source == source:
                live_asr.close()
                live_asr.thread.join()
                self.live_asr = None

    def transcribe(self, source: Path, root: Path, mode):
        live = self.live_decoder
        if live and live.source == source and live.mode == mode:
            self.event.emit("processing", "正在完成实时解码尾部音频…")
            try:
                wav = live.result()
            finally:
                self.live_decoder = None
        else:
            self.event.emit("processing", "正在离线解码 Opus…")
            wav = decode_capture(source, mode)
        if self.transcriber is None or self.model_root != root:
            self.event.emit("processing", "正在加载离线模型…")
            self.transcriber = Transcriber(root)
            self.model_root = root
        self.event.emit("processing", "正在进行 VAD 分段和离线识别…")
        return self.transcriber.transcribe(wav, lambda text: self.event.emit("segment", text))

    def shutdown(self):
        async def cleanup():
            try:
                try:
                    if self.recorder.capture:
                        await self.recorder.stop()
                finally:
                    await self.recorder.disconnect()
            finally:
                if self.live_decoder:
                    self.live_decoder.close()
                    await asyncio.to_thread(self.live_decoder.thread.join)
                if self.live_asr:
                    self.live_asr.close()
                    await asyncio.to_thread(self.live_asr.thread.join)
                self.loop.call_soon(self.loop.stop)

        asyncio.run_coroutine_threadsafe(cleanup(), self.loop)
