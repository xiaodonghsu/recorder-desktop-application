import threading
import wave

import av
import numpy as np
import pytest

from voicehub.audio import decode_capture
from voicehub.capture import Capture
from voicehub.live_audio import LiveDecodeSession


def speech_packets():
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate = 16000
    encoder.layout = "mono"
    encoder.format = "s16"
    for i in range(25):
        samples = (np.sin(np.arange(320) * 2 * np.pi * 440 / 16000) * 10000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 16000
        frame.pts = i * 320
        for packet in encoder.encode(frame):
            yield bytes(packet)
    for packet in encoder.encode(None):
        yield bytes(packet)


def test_live_decodes_before_stop_and_matches_offline_pcm(tmp_path):
    capture = Capture(tmp_path)
    progress = threading.Event()
    session = LiveDecodeSession(
        capture.path, event=lambda name, seconds: progress.set() if seconds > 0 else None
    )
    try:
        for payload in speech_packets():
            capture.push(payload)
            session.push(payload)
        # Decode progress is delivered while the recording is still open.
        assert progress.wait(3)
        assert not session.future.done()
        assert not capture.closed
    finally:
        capture.close()
        session.close()
    wav = session.result()
    live_pcm = wav.read_bytes()
    assert decode_capture(capture.path).read_bytes() == live_pcm
    with wave.open(str(wav)) as output:
        assert output.getframerate() == 16000
        assert output.getnframes() > 7900
    assert not session.thread.is_alive()


def test_decode_error_does_not_stop_raw_capture(tmp_path):
    capture = Capture(tmp_path)
    session = LiveDecodeSession(capture.path, mode="fixed40")
    capture.push(b"bad")
    session.push(b"bad")
    session.close()
    with pytest.raises(ValueError, match="40"):
        session.result()
    capture.push(b"still receiving raw audio")
    capture.close()
    assert capture.packets == 2
    assert not capture.path.with_suffix(".wav.part").exists()


def test_queue_overflow_reports_error_without_blocking(tmp_path, monkeypatch):
    ready = threading.Event()
    release = threading.Event()

    class SlowDecoder:
        def __init__(self, *args):
            ready.set()
            assert release.wait(3)

        def abort(self):
            pass

    monkeypatch.setattr("voicehub.live_audio.OpusDecoder", SlowDecoder)
    session = LiveDecodeSession(tmp_path / "overflow.vhrec", max_packets=1)
    try:
        assert ready.wait(3)
        session.push(b"one")
        session.push(b"two")
        assert session.overloaded.is_set()
    finally:
        release.set()
        session.close()
    with pytest.raises(ValueError, match="积压"):
        session.result()


def test_worker_reuses_live_pcm_for_asr(tmp_path, monkeypatch):
    from voicehub.worker import Worker

    class FakeTranscriber:
        def __init__(self, root):
            pass

        def transcribe(self, wav, progress):
            assert wav.is_file()
            return "recognized from live PCM"

    def forbid_offline_decode(*args):
        pytest.fail("The captured stream must not be decoded a second time")

    monkeypatch.setattr("voicehub.worker.Transcriber", FakeTranscriber)
    monkeypatch.setattr("voicehub.worker.decode_capture", forbid_offline_decode)
    worker = Worker()
    capture = Capture(tmp_path)
    try:
        worker._recorder_event("capture_started", str(capture.path))
        for payload in speech_packets():
            capture.push(payload)
            worker._recorder_event("audio", payload)
        capture.close()
        worker._recorder_event("capture_closed", str(capture.path))
        assert worker.transcribe(capture.path, tmp_path, "packet") == "recognized from live PCM"
        assert worker.live_decoder is None
    finally:
        if worker.live_decoder:
            worker.live_decoder.close()
            worker.live_decoder.thread.join(3)
        worker.loop.close()


@pytest.mark.parametrize("failure", ["open", "write", None])
def test_output_receives_live_pcm_and_failure_preserves_wav(tmp_path, monkeypatch, failure):
    received = []
    closed = []
    events = []

    class FakeOutput:
        def __init__(self, device, event):
            assert device == 7
            if failure == "open":
                raise OSError("missing device")

        def write(self, data):
            if failure == "write":
                raise OSError("device removed")
            received.append(data)

        def close(self, discard=False):
            closed.append(discard)

    monkeypatch.setattr("voicehub.live_audio.PcmOutput", FakeOutput)
    capture = Capture(tmp_path)
    session = LiveDecodeSession(
        capture.path, event=lambda *args: events.append(args), output_device=7
    )
    for payload in speech_packets():
        capture.push(payload)
        session.push(payload)
    capture.close()
    session.close()
    wav = session.result()
    with wave.open(str(wav)) as output:
        pcm = output.readframes(output.getnframes())
    assert len(pcm) > 15000
    if failure:
        assert any(
            name == "output_status"
            and "失败" in message
            or name == "output_status"
            and "已停止" in message
            for name, message in events
        )
    else:
        assert b"".join(received) == pcm
        assert closed == [False]


def test_worker_live_asr_gets_all_pcm_including_tail_without_second_decode(tmp_path, monkeypatch):
    from voicehub.worker import Worker

    calls = []

    class FakeAsr:
        def __init__(self, source, root, event):
            self.source = source
            self.pcm = []
            self.closed = False
            self.thread = self
            calls.append(self)

        def push(self, pcm):
            assert not self.closed
            self.pcm.append(pcm)

        def close(self):
            self.closed = True

        def result(self):
            assert self.closed
            return "实时结果"

        def join(self):
            pass

    monkeypatch.setattr("voicehub.worker.LiveTranscribeSession", FakeAsr)
    worker = Worker()
    worker.live_asr_root = tmp_path
    capture = Capture(tmp_path)
    try:
        worker._recorder_event("capture_started", str(capture.path))
        for payload in speech_packets():
            capture.push(payload)
            worker._recorder_event("audio", payload)
        capture.close()
        worker._recorder_event("capture_closed", str(capture.path))
        assert worker.finish_live(capture.path) == "实时结果"
        with wave.open(str(capture.path.with_suffix(".wav"))) as wav:
            assert b"".join(calls[0].pcm) == wav.readframes(wav.getnframes())
        assert worker.live_asr is None
        assert worker.live_decoder is None
    finally:
        if worker.live_decoder:
            worker.live_decoder.close()
            worker.live_decoder.thread.join(3)
        worker.loop.close()
