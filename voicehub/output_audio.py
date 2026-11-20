"""Explicit-device PCM playback with a bounded jitter buffer.

The decoder calls write(); PortAudio pulls from the ring on its own callback.
No BLE callback or GUI operation waits for the output device to consume audio.
"""

import threading
import time

import av
import numpy as np
import sounddevice as sd


def list_output_devices():
    hostapis = sd.query_hostapis()
    return [
        {
            "id": index,
            "name": device["name"],
            "hostapi": hostapis[device["hostapi"]]["name"],
            "default_samplerate": device["default_samplerate"],
        }
        for index, device in enumerate(sd.query_devices())
        if device["max_output_channels"] > 0
    ]


class PcmOutput:
    """Own one output stream; all public methods belong to the decoder thread."""

    def __init__(self, device, event=lambda *_: None):
        if device is None or not isinstance(device, int) or device < 0:
            raise ValueError("请明确选择音频输出设备")
        self.event = event
        info = sd.query_devices(device, "output")
        self.rate = int(info["default_samplerate"])
        self.channels = None
        for channels in (1, 2):
            if channels > info["max_output_channels"]:
                continue
            try:
                sd.check_output_settings(
                    device=device, samplerate=self.rate, channels=channels, dtype="float32"
                )
                self.channels = channels
                break
            except sd.PortAudioError:
                continue
        if self.channels is None:
            raise ValueError("所选输出设备不支持单声道或立体声音频")
        self.capacity = max(1, int(self.rate * 0.3))
        self.prebuffer = max(1, int(self.rate * 0.06))
        self.buffer = np.zeros((self.capacity, self.channels), dtype=np.float32)
        self.lock = threading.Lock()
        self.read_pos = 0
        self.count = 0
        self.primed = False
        self.draining = False
        self.closed = False
        self.underruns = 0
        self.overflows = 0
        self.callback_error = None
        self.last_report = 0.0
        self.reported = (0, 0)
        self.pts = 0
        self.resampler = av.AudioResampler(format="flt", layout="mono", rate=self.rate)
        self.stream = sd.OutputStream(
            device=device,
            samplerate=self.rate,
            channels=self.channels,
            dtype="float32",
            blocksize=max(1, int(self.rate * 0.01)),
            latency="low",
            callback=self._callback,
        )
        try:
            self.stream.start()
        except Exception:
            self.stream.close()
            raise
        self.event("output_status", f"输出到 {info['name']} · {self.rate}Hz")

    def _callback(self, outdata, frames, timing, status):
        outdata.fill(0)
        # Never wait for the decoder while servicing a real-time audio request.
        if not self.lock.acquire(blocking=False):
            return
        try:
            if status.output_underflow:
                self.underruns += 1
            if not self.primed:
                if self.count < self.prebuffer and not self.draining:
                    return
                self.primed = True
            size = min(frames, self.count)
            first = min(size, self.capacity - self.read_pos)
            outdata[:first] = self.buffer[self.read_pos : self.read_pos + first]
            if first < size:
                outdata[first:size] = self.buffer[: size - first]
            self.read_pos = (self.read_pos + size) % self.capacity
            self.count -= size
            if size < frames:
                if not self.draining:
                    self.underruns += 1
                self.primed = False
        except Exception as exc:  # noqa: BLE001 - contain errors on the PortAudio callback thread
            outdata.fill(0)
            self.callback_error = str(exc)
        finally:
            self.lock.release()

    def _enqueue(self, frames):
        for frame in frames:
            samples = frame.to_ndarray().reshape(-1, 1)
            if self.channels == 2:
                samples = np.repeat(samples, 2, axis=1)
            with self.lock:
                if len(samples) + self.count > self.capacity:
                    # Keep recent speech and bound latency when the clocks differ.
                    self.overflows += 1
                    drop = min(self.count, len(samples) + self.count - self.capacity)
                    self.read_pos = (self.read_pos + drop) % self.capacity
                    self.count -= drop
                    samples = samples[-self.capacity :]
                position = (self.read_pos + self.count) % self.capacity
                first = min(len(samples), self.capacity - position)
                self.buffer[position : position + first] = samples[:first]
                if first < len(samples):
                    self.buffer[: len(samples) - first] = samples[first:]
                self.count += len(samples)

    def write(self, pcm):
        if self.closed:
            raise RuntimeError("音频输出已关闭")
        if self.callback_error:
            raise RuntimeError(self.callback_error)
        if not self.stream.active:
            raise RuntimeError("音频输出设备已停止，请检查设备连接并重新开始录音")
        if not self.stream.active:
            raise RuntimeError("音频输出设备已停止或断开")
        if len(pcm) % 2:
            raise ValueError("PCM16 字节数必须为偶数")
        if not pcm:
            return
        samples = np.frombuffer(pcm, dtype="<i2").reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = 16000
        frame.pts = self.pts
        self.pts += samples.size
        self._enqueue(self.resampler.resample(frame))
        now = time.monotonic()
        counts = (self.underruns, self.overflows)
        if counts != self.reported and now - self.last_report >= 5:
            self.event(
                "warning",
                f"音频输出缓冲：欠载 {counts[0]} 次，积压丢弃 {counts[1]} 次；录音文件不受影响",
            )
            self.reported = counts
            self.last_report = now

    def close(self, discard=False):
        if self.closed:
            return
        self.closed = True
        try:
            if not discard and self.stream.active:
                self._enqueue(self.resampler.resample(None))
                self.draining = True
                deadline = time.monotonic() + 1
                while self.count and self.stream.active and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.stream.stop()
            else:
                self.stream.abort()
        finally:
            try:
                self.stream.close()
            finally:
                with self.lock:
                    self.count = 0
                    self.primed = False
