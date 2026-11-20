import threading
import time
from types import SimpleNamespace

import av
import numpy as np
import pytest

from voicehub.output_audio import PcmOutput, list_output_devices


@pytest.fixture
def backend(monkeypatch):
    import voicehub.output_audio as module

    class Stream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.active = False
            self.closed = False

        def start(self):
            self.active = True

        def stop(self):
            self.active = False

        abort = stop

        def close(self):
            self.closed = True

    devices = [
        {"name": "Input only", "hostapi": 0, "max_output_channels": 0},
        {
            "name": "CABLE Input",
            "hostapi": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000,
        },
    ]
    monkeypatch.setattr(module.sd, "query_hostapis", lambda: [{"name": "Windows WASAPI"}])
    monkeypatch.setattr(
        module.sd,
        "query_devices",
        lambda device=None, kind=None: devices if device is None else devices[device],
    )
    monkeypatch.setattr(module.sd, "check_output_settings", lambda **kwargs: None)
    monkeypatch.setattr(module.sd, "OutputStream", Stream)
    return module


def pull(output, size):
    data = np.full((size, output.channels), np.nan, dtype=np.float32)
    output._callback(data, size, None, SimpleNamespace(output_underflow=False))
    return data


def test_output_devices_and_no_default_fallback(backend):
    assert list_output_devices() == [
        {
            "id": 1,
            "name": "CABLE Input",
            "hostapi": "Windows WASAPI",
            "default_samplerate": 48000,
        }
    ]
    with pytest.raises(ValueError, match="明确选择"):
        PcmOutput(None)


def test_resamples_and_buffers_live_pcm_then_rebuffers_after_gap(backend):
    output = PcmOutput(1)
    try:
        output.write(np.full(320, 8192, dtype="<i2").tobytes())
        assert np.all(pull(output, 480) == 0)  # Wait for the 60 ms jitter buffer.
        output.write(np.full(1280, 8192, dtype="<i2").tobytes())
        samples = pull(output, 480)
        assert np.allclose(samples, 0.25, atol=0.01)
        assert output.stream.kwargs["device"] == 1
        assert output.rate == 48000
        tail = pull(output, 6000)
        assert np.isfinite(tail).all()
        assert np.all(tail[-100:] == 0)
        assert output.underruns == 1
        output.write(np.full(320, 8192, dtype="<i2").tobytes())
        assert np.all(pull(output, 480) == 0)
    finally:
        output.close(discard=True)
    assert output.stream.closed
    assert not output.stream.active


def test_overflow_keeps_recent_audio_and_never_grows_buffer(backend):
    output = PcmOutput(1)
    try:

        def frame(value, count):
            return av.AudioFrame.from_ndarray(
                np.full((1, count), value, dtype=np.float32), format="flt", layout="mono"
            )

        output._enqueue([frame(0.1, output.capacity)])
        output._enqueue([frame(0.8, output.capacity + 100)])
        assert output.count == output.capacity
        assert output.overflows == 1
        assert np.allclose(pull(output, 480), 0.8)
        output.close(discard=True)
        assert output.count == 0
        output.close()  # Idempotent cleanup after a failure.
    finally:
        output.close(discard=True)


def test_stereo_fallback_duplicates_mono(backend, monkeypatch):
    def check(**kwargs):
        if kwargs["channels"] == 1:
            raise backend.sd.PortAudioError("mono unsupported")

    monkeypatch.setattr(backend.sd, "check_output_settings", check)
    output = PcmOutput(1)
    try:
        output.write(np.full(1600, 8192, dtype="<i2").tobytes())
        samples = pull(output, 480)
        assert output.channels == 2
        assert np.allclose(samples[:, 0], samples[:, 1])
        assert np.allclose(samples, 0.25, atol=0.01)
    finally:
        output.close(discard=True)


def test_close_drains_a_short_recording_below_prebuffer(backend):
    output = PcmOutput(1)
    output.write(np.full(320, 8192, dtype="<i2").tobytes())
    received = []

    def audio_callback_thread():
        while not output.draining and not output.closed:
            time.sleep(0.001)
        # close() marks closed before flushing the final resampler frames.
        while not output.draining:
            time.sleep(0.001)
        while output.count:
            received.append(pull(output, 480))

    consumer = threading.Thread(target=audio_callback_thread)
    consumer.start()
    output.close()
    consumer.join(2)
    assert not consumer.is_alive()
    assert sum(np.count_nonzero(block) for block in received) > 900
    assert output.count == 0
    assert output.stream.closed


def test_device_stopped_raises_for_caller_to_report(backend):
    output = PcmOutput(1)
    output.stream.active = False
    try:
        with pytest.raises(RuntimeError, match="已停止"):
            output.write(bytes(640))
    finally:
        output.close(discard=True)
