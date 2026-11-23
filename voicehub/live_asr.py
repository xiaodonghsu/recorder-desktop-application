"""Short-phrase recognition on a separate thread, fed by decoded 16kHz PCM.

SenseVoice is an offline recognizer. Silero closes a phrase after a short pause
or roughly three seconds of continuous speech, so committed text can be emitted
while recording continues without rewriting text in another application.
"""

import json
import threading
from collections import deque
from concurrent.futures import Future
from pathlib import Path

import numpy as np

from .audio import Transcriber


class LiveTranscribeSession:
    def __init__(self, source: Path, root: Path, event=lambda *_: None, max_pending_seconds=15):
        self.source = Path(source)
        self.root = Path(root)
        self.event = event
        self.max_pending_bytes = max(2, int(max_pending_seconds * 16000) * 2)
        self.pending_bytes = 0
        self.pending = deque()
        self.condition = threading.Condition()
        self.closed = False
        self.failure = None
        self.segments = []
        self.future = Future()
        self.thread = threading.Thread(target=self._run, name="voicehub-asr", daemon=False)
        self.thread.start()

    def push(self, pcm: bytes):
        """Queue PCM without waiting for recognition; failures only affect ASR."""
        if not pcm:
            return
        with self.condition:
            if self.closed or self.future.done():
                return
            if len(pcm) % 2:
                self.failure = ValueError("实时识别收到不完整的 PCM16 样本")
            elif self.pending_bytes + len(pcm) > self.max_pending_bytes:
                self.failure = RuntimeError("实时识别队列积压，已停止识别；请停止后重新转写录音")
            if self.failure:
                self.closed = True
                self.pending.clear()
                self.pending_bytes = 0
            else:
                self.pending.append(bytes(pcm))
                self.pending_bytes += len(pcm)
            self.condition.notify()

    def close(self):
        """Drain all queued PCM and flush the unfinished phrase."""
        with self.condition:
            self.closed = True
            self.condition.notify()

    def result(self):
        try:
            return self.future.result()
        finally:
            self.thread.join()

    def _check_failure(self):
        with self.condition:
            if self.failure:
                raise self.failure

    def _take(self):
        with self.condition:
            self.condition.wait_for(lambda: self.pending or self.closed or self.failure)
            if self.failure:
                raise self.failure
            if not self.pending:
                return None
            pcm = self.pending.popleft()
            self.pending_bytes -= len(pcm)
            return pcm

    def _save(self):
        text = "\n".join(item["text"] for item in self.segments)
        self.source.with_suffix(".txt").write_text(text, encoding="utf-8")
        self.source.with_suffix(".segments.json").write_text(
            json.dumps(self.segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return text

    def _run(self):
        error = None
        try:
            self.event("live_asr_status", "正在加载实时识别模型…")
            model = Transcriber(self.root)
            self._check_failure()
            model.config.silero_vad.min_silence_duration = 0.4
            model.config.silero_vad.max_speech_duration = 3.0
            vad = model.sherpa.VoiceActivityDetector(model.config, buffer_size_in_seconds=30)
            window = model.config.silero_vad.window_size
            remainder = np.empty(0, dtype=np.float32)
            total_samples = 0
            processed_samples = 0
            previous_complete = True
            previous_end = None

            def drain(flushing=False):
                nonlocal previous_complete, previous_end
                while not vad.empty():
                    self._check_failure()
                    speech = vad.front
                    # The final VAD frame is zero-padded, but timestamps refer only
                    # to PCM actually received from the recorder.
                    start = max(0, speech.start)
                    end = min(start + len(speech.samples), total_samples)
                    samples = speech.samples[: max(0, end - start)]
                    # Long utterances may use a shorter VAD silence threshold.
                    # Only a real 0.4s boundary on both sides permits a command;
                    # stopping the recorder alone does not confirm a command.
                    complete = not flushing and processed_samples - end >= round(0.4 * 16000)
                    separated = previous_complete or (
                        previous_end is not None and start - previous_end >= round(0.4 * 16000)
                    )
                    command_allowed = separated and complete
                    previous_complete = complete
                    previous_end = end
                    if len(samples):
                        stream = model.recognizer.create_stream()
                        stream.accept_waveform(16000, samples)
                        model.recognizer.decode_stream(stream)
                        text = stream.result.text.strip()
                    else:
                        text = ""
                    vad.pop()
                    self._check_failure()
                    if text:
                        self.segments.append(
                            {"start": start / 16000, "end": end / 16000, "text": text}
                        )
                        self._save()
                        self.event(
                            "live_segment", {"text": text, "command_allowed": command_allowed}
                        )

            self.event("live_asr_status", "实时识别已就绪；短暂停顿或连续说话约 3 秒后输出文字")
            while (pcm := self._take()) is not None:
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                total_samples += len(samples)
                samples = np.concatenate((remainder, samples))
                complete = len(samples) // window * window
                for offset in range(0, complete, window):
                    self._check_failure()
                    vad.accept_waveform(samples[offset : offset + window])
                    processed_samples += window
                    drain()
                remainder = samples[complete:].copy()
            if len(remainder):
                vad.accept_waveform(np.pad(remainder, (0, window - len(remainder))))
                drain(flushing=True)
            vad.flush()
            drain(flushing=True)
        except Exception as exc:  # noqa: BLE001 - ASR errors must not stop PCM archival
            error = exc
        finally:
            with self.condition:
                self.closed = True
                self.pending.clear()
                self.pending_bytes = 0
            try:
                text = self._save()
            except Exception as exc:  # noqa: BLE001 - propagate persistence failures to the UI
                error = error or exc
            if error:
                self.event("live_asr_failed", f"实时识别已停止：{error}；已确认文字和录音仍保留")
                self.future.set_exception(error)
            else:
                self.event("live_asr_status", "实时识别已结束")
                self.future.set_result(text)
