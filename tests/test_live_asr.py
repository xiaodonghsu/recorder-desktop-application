import json
import threading
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

from voicehub.live_asr import LiveTranscribeSession


@pytest.fixture
def fake_model(monkeypatch):
    models = []

    class FakeVad:
        def __init__(self, config, buffer_size_in_seconds):
            self.config = config
            self.samples = []
            self.position = 0
            self.segments = deque()

        def accept_waveform(self, samples):
            assert len(samples) == 4
            self.samples.extend(samples)
            if len(self.samples) == 8:
                self.flush()

        def flush(self):
            if any(self.samples):
                self.segments.append(
                    SimpleNamespace(start=self.position, samples=np.array(self.samples))
                )
            self.position += len(self.samples)
            self.samples = []

        def empty(self):
            return not self.segments

        @property
        def front(self):
            return self.segments[0]

        def pop(self):
            self.segments.popleft()

    class FakeRecognizer:
        def __init__(self):
            self.calls = []
            self.fail_on = None

        def create_stream(self):
            stream = SimpleNamespace(result=SimpleNamespace(text=""))
            stream.accept_waveform = lambda rate, data: setattr(stream, "samples", list(data))
            return stream

        def decode_stream(self, stream):
            self.calls.append(stream.samples)
            if len(self.calls) == self.fail_on:
                raise RuntimeError("model failure")
            stream.result.text = f"文字{len(self.calls)}"

    class FakeModel:
        def __init__(self, root):
            self.config = SimpleNamespace(silero_vad=SimpleNamespace(window_size=4))
            self.sherpa = SimpleNamespace(VoiceActivityDetector=FakeVad)
            self.recognizer = FakeRecognizer()
            models.append(self)

    monkeypatch.setattr("voicehub.live_asr.Transcriber", FakeModel)
    return models, FakeModel


def test_phrases_emit_before_stop_and_arbitrary_chunks_preserve_tail(tmp_path, fake_model):
    models, _ = fake_model
    emitted = threading.Event()
    events = []

    def event(name, value):
        events.append((name, value))
        if name == "live_segment":
            emitted.set()

    source = tmp_path / "capture.vhrec"
    session = LiveTranscribeSession(source, tmp_path, event)
    pcm = np.arange(1, 20, dtype="<i2").tobytes()
    try:
        # Boundaries differ from the VAD's four-sample window.
        session.push(pcm[:6])
        session.push(pcm[6:18])
        assert emitted.wait(3)
        assert not session.future.done()
        session.push(pcm[18:])
    finally:
        session.close()
    assert session.result() == "文字1\n文字2\n文字3"
    recognized = np.concatenate(models[0].recognizer.calls)
    np.testing.assert_array_equal(recognized, np.arange(1, 20) / 32768)
    assert [text for name, text in events if name == "live_segment"] == ["文字1", "文字2", "文字3"]
    segments = json.loads(source.with_suffix(".segments.json").read_text(encoding="utf-8"))
    assert segments[-1]["end"] == 19 / 16000
    assert models[0].config.silero_vad.max_speech_duration == 3
    assert models[0].config.silero_vad.min_silence_duration == 0.4
    assert not session.thread.is_alive()


def test_silence_and_empty_recording_do_not_create_text(tmp_path, fake_model):
    for index, pcm in enumerate((b"", bytes(34))):
        session = LiveTranscribeSession(tmp_path / f"{index}.vhrec", tmp_path)
        session.push(pcm)
        session.close()
        assert session.result() == ""
        assert session.segments == []


def test_byte_limit_reports_overflow_without_waiting_for_model(tmp_path, fake_model, monkeypatch):
    _, fake = fake_model
    ready = threading.Event()
    release = threading.Event()

    def slow_model(root):
        ready.set()
        assert release.wait(3)
        return fake(root)

    monkeypatch.setattr("voicehub.live_asr.Transcriber", slow_model)
    events = []
    session = LiveTranscribeSession(
        tmp_path / "overflow.vhrec",
        tmp_path,
        event=lambda *args: events.append(args),
        max_pending_seconds=4 / 16000,
    )
    try:
        assert ready.wait(3)
        session.push(bytes(8))
        assert session.pending_bytes == 8
        session.push(bytes(2))
        assert session.pending_bytes == 0
        assert session.closed
    finally:
        release.set()
        session.close()
    with pytest.raises(RuntimeError, match="积压"):
        session.result()
    assert any(name == "live_asr_failed" for name, _ in events)
    assert not session.thread.is_alive()


def test_recognition_failure_keeps_committed_text(tmp_path, fake_model, monkeypatch):
    _, fake = fake_model

    def failing_model(root):
        model = fake(root)
        model.recognizer.fail_on = 2
        return model

    monkeypatch.setattr("voicehub.live_asr.Transcriber", failing_model)
    source = tmp_path / "error.vhrec"
    session = LiveTranscribeSession(source, tmp_path)
    session.push(np.ones(16, dtype="<i2").tobytes())
    session.close()
    with pytest.raises(RuntimeError, match="model failure"):
        session.result()
    assert source.with_suffix(".txt").read_text(encoding="utf-8") == "文字1"
    assert len(json.loads(source.with_suffix(".segments.json").read_text(encoding="utf-8"))) == 1


def test_incomplete_pcm_sample_is_explicit_failure(tmp_path, fake_model):
    session = LiveTranscribeSession(tmp_path / "odd.vhrec", tmp_path)
    session.push(b"x")
    session.close()
    with pytest.raises(ValueError, match="PCM16"):
        session.result()
