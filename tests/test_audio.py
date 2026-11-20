import wave

import av
import numpy as np
import pytest

from voicehub.audio import decode_capture, split_payload
from voicehub.capture import Capture, packets


def test_capture_rejects_truncation(tmp_path):
    capture = Capture(tmp_path)
    capture.push(b"1234")
    path = capture.close()
    assert list(packets(path)) == [b"1234"]
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="截断"):
        list(packets(path))


def test_fixed40_is_explicit_and_checked():
    payload = b"\x48" + bytes(39)
    assert list(split_payload(payload * 2, "fixed40")) == [payload, payload]
    assert list(split_payload(payload * 2, "packet")) == [payload * 2]
    for bad in (b"x", bytes(40)):
        with pytest.raises(ValueError):
            list(split_payload(bad, "fixed40"))


def test_real_opus_decoder_roundtrip(tmp_path):
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate = 48000
    encoder.layout = "mono"
    encoder.format = "s16"
    encoder.bit_rate = 16000
    capture = Capture(tmp_path)
    for i in range(25):
        samples = (np.sin(np.arange(960) * 2 * np.pi * 440 / 48000) * 10000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.pts = i * 960
        for packet in encoder.encode(frame):
            capture.push(bytes(packet))
    for packet in encoder.encode(None):
        capture.push(bytes(packet))
    path = decode_capture(capture.close())
    with wave.open(str(path)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert 7900 <= wav.getnframes() <= 8500
        assert np.abs(np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")).max() > 1000


def test_empty_capture_does_not_produce_successful_wav(tmp_path):
    path = Capture(tmp_path).close()
    with pytest.raises(ValueError, match="没有收到"):
        decode_capture(path)
    assert not path.with_suffix(".wav").exists()
