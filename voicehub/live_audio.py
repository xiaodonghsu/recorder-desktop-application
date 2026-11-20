"""Non-blocking BLE producer, bounded queue, single persistent Opus decoding thread."""

import queue
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from .audio import OpusDecoder
from .output_audio import PcmOutput


class LiveDecodeSession:
    def __init__(
        self,
        source: Path,
        mode="packet",
        event=lambda *_: None,
        max_packets=256,
        output_device=None,
        pcm_sink=None,
        pcm_done=None,
    ):
        self.source = source
        self.mode = mode
        self.event = event
        self.output_device = output_device
        self.output = None
        self.pcm_sink = pcm_sink
        self.pcm_done = pcm_done
        self.queue = queue.Queue(maxsize=max_packets)
        self.closed = threading.Event()
        self.overloaded = threading.Event()
        self.future = Future()
        self.thread = threading.Thread(target=self._run, name="voicehub-opus")
        self.thread.start()

    def push(self, payload: bytes):
        if self.closed.is_set() or self.future.done():
            return
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            self.overloaded.set()
            self.closed.set()

    def close(self):
        # No sentinel is needed: a full queue must never block the BLE event loop.
        self.closed.set()

    def result(self):
        try:
            return self.future.result()
        finally:
            self.thread.join()

    def _run(self):
        decoder = None
        last_progress = 0
        try:
            if self.output_device is not None:
                try:
                    self.output = PcmOutput(self.output_device, self.event)
                except Exception as exc:  # noqa: BLE001 - output must not break archival decoding
                    self.event("output_status", f"音频输出失败：{exc}；录音继续保存")
            if self.output or self.pcm_sink:
                decoder = OpusDecoder(self.source, self.mode, pcm_sink=self._output_pcm)
            else:
                decoder = OpusDecoder(self.source, self.mode)
            while True:
                if self.overloaded.is_set():
                    raise RuntimeError("实时解码队列积压，已停止解码，原始录音继续保存")
                try:
                    payload = self.queue.get(timeout=0.05)
                except queue.Empty:
                    if self.closed.is_set():
                        break
                    continue
                decoder.push(payload)
                now = time.monotonic()
                if now - last_progress >= 0.25:
                    self.event("pcm_progress", decoder.samples / 16000)
                    last_progress = now
            path = decoder.finish()
            self.event("pcm_progress", decoder.samples / 16000)
            self.future.set_result(path)
        except Exception as exc:  # noqa: BLE001 - report audio thread failures to the UI
            # A decode failure must not break BLE receipt or lose the original packets.
            if decoder:
                decoder.abort()
            message = f"实时 Opus 解码失败：{exc}；原始包已保留，可调整分包模式后重试"
            self.event("decode_failed", message)
            self.future.set_exception(ValueError(message))
        finally:
            self._close_output(discard=bool(self.future.exception()))
            if self.pcm_done:
                self.pcm_done()

    def _close_output(self, discard=False):
        output, self.output = self.output, None
        if output:
            try:
                output.close(discard=discard)
            except Exception as exc:  # noqa: BLE001 - contain independent output failures
                self.event("output_status", f"音频输出关闭失败：{exc}")
            else:
                self.event("output_status", "音频输出已停止")

    def _output_pcm(self, data):
        if self.pcm_sink:
            try:
                self.pcm_sink(data)
            except Exception as exc:  # noqa: BLE001 - recognition must not interrupt recording
                self.pcm_sink = None
                self.event("live_asr_failed", f"实时识别输入失败：{exc}；录音继续保存")
        if self.output:
            try:
                self.output.write(data)
            except Exception as exc:  # noqa: BLE001 - preserve WAV when output device disappears
                self._close_output(discard=True)
                self.event("output_status", f"音频输出已停止：{exc}；录音继续保存")
