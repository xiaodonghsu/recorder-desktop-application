"""Opt-in real model test: VOICEHUB_TEST_WAV must name a 16kHz mono PCM16 speech sample."""

import os
import wave
from pathlib import Path

import av
import numpy as np
import pytest

from voicehub.audio import Transcriber, decode_capture
from voicehub.capture import Capture
from voicehub.live_audio import LiveDecodeSession


@pytest.mark.skipif(not os.environ.get("VOICEHUB_TEST_WAV"), reason="需要本地语音样本和离线模型")
@pytest.mark.parametrize("decode_path", ["offline", "live"])
def test_real_speech_opus_vad_asr(tmp_path, decode_path):
    source = Path(os.environ["VOICEHUB_TEST_WAV"])
    models = Path(os.environ.get("VOICEHUB_TEST_MODELS", "models"))
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate = 16000
    encoder.layout = "mono"
    encoder.format = "s16"
    encoder.bit_rate = 16000
    capture = Capture(tmp_path)
    live = None

    def store(packet):
        payload = bytes(packet)
        capture.push(payload)
        if live:
            live.push(payload)

    with wave.open(str(source)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        if decode_path == "live":
            live = LiveDecodeSession(capture.path)
        try:
            pts = 0
            while raw := wav.readframes(320):
                samples = np.frombuffer(raw, dtype="<i2")
                if len(samples) < 320:
                    samples = np.pad(samples, (0, 320 - len(samples)))
                frame = av.AudioFrame.from_ndarray(
                    samples.reshape(1, -1), format="s16", layout="mono"
                )
                frame.sample_rate = 16000
                frame.pts = pts
                pts += 320
                for packet in encoder.encode(frame):
                    store(packet)
            for packet in encoder.encode(None):
                store(packet)
        finally:
            capture.close()
            if live:
                live.close()
                live.thread.join(5)
    decoded = live.result() if live else decode_capture(capture.path)
    text = Transcriber(models).transcribe(decoded)
    assert text.strip(), "真实语音应经过 Opus 解码和 VAD 后得到非空识别结果"
    assert decoded.with_suffix(".txt").read_text(encoding="utf-8") == text
    assert decoded.with_suffix(".segments.json").is_file()
