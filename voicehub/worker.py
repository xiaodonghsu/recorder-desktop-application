import asyncio
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .audio import Transcriber, decode_capture
from .device import Recorder, scan
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
                if name == "scan":
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
