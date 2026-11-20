"""Incremental Opus -> 16 kHz PCM, followed by offline Silero VAD + SenseVoice."""

import json
import struct
import wave
from pathlib import Path

import av
import numpy as np

from .capture import packets


def split_payload(payload: bytes, mode: str):
    if mode == "packet":
        yield payload
    elif mode == "fixed40":
        if not payload or len(payload) % 40:
            raise ValueError("fixed40 模式要求每个 BLE 载荷为 40 字节的整数倍")
        for i in range(0, len(payload), 40):
            packet = payload[i : i + 40]
            if not (
                packet[0] == 0x48
                or (
                    packet[0] == 0x4B
                    and (packet[1] == 1 or (packet[1] == 0x41 and packet[2] <= 36))
                )
            ):
                raise ValueError("载荷不匹配已知 40 字节 Opus 结构，拒绝猜测包边界")
            yield packet
    else:
        raise ValueError("未知音频分包模式")


class OpusDecoder:
    """One stateful decoder/resampler per recording; used on a dedicated audio thread."""

    def __init__(self, source: Path, mode="packet", pcm_sink=None):
        self.pcm_sink = pcm_sink
        self.mode = mode
        self.codec = av.CodecContext.create("opus", "r")
        # RFC 7845 OpusHead, mono, pre-skip unknown (0); never strip 4B 41.
        self.codec.extradata = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 0, 16000, 0, 0)
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        self.target = source.with_suffix(".wav")
        self.temporary = self.target.with_suffix(".wav.part")
        self.samples = 0
        self.output = wave.open(str(self.temporary), "wb")  # noqa: SIM115 - session owns file
        self.output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))

    def _write_pcm(self, frames):
        for pcm in frames:
            data = pcm.to_ndarray().astype("<i2").tobytes()
            self.output.writeframes(data)
            self.samples += pcm.samples
            if self.pcm_sink:
                self.pcm_sink(data)

    def _write_decoded(self, frames):
        for frame in frames:
            self._write_pcm(self.resampler.resample(frame))

    def push(self, payload: bytes):
        for packet in split_payload(payload, self.mode):
            self._write_decoded(self.codec.decode(av.Packet(packet)))

    def finish(self) -> Path:
        self._write_decoded(self.codec.decode(None))
        self._write_pcm(self.resampler.resample(None))
        self.output.close()
        if not self.samples:
            raise ValueError("没有收到可解码的音频")
        self.temporary.replace(self.target)
        return self.target

    def abort(self):
        self.output.close()
        self.temporary.unlink(missing_ok=True)


def decode_capture(source: Path, mode="packet") -> Path:
    decoder = OpusDecoder(source, mode)
    try:
        for payload in packets(source):
            decoder.push(payload)
        return decoder.finish()
    except Exception as exc:
        decoder.abort()
        raise ValueError(f"Opus 解码失败：{exc}。原始包已保留，请确认固件实时分包模式") from exc


def check_models(root: Path):
    paths = [
        root / "silero_vad.onnx",
        root / "sense-voice" / "model.int8.onnx",
        root / "sense-voice" / "tokens.txt",
    ]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            '缺少离线模型，请先运行 uv run voicehub-models --dir "'
            + str(root)
            + '"\n'
            + "\n".join(missing)
        )
    return paths


class Transcriber:
    def __init__(self, root: Path):
        import sherpa_onnx as sherpa

        vad_path, model_path, tokens = check_models(root)
        self.sherpa = sherpa
        self.recognizer = sherpa.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens),
            num_threads=2,
            use_itn=True,
            provider="cpu",
            language="auto",
        )
        self.config = sherpa.VadModelConfig()
        self.config.silero_vad.model = str(vad_path)
        self.config.silero_vad.min_silence_duration = 0.5
        self.config.silero_vad.min_speech_duration = 0.25
        self.config.silero_vad.max_speech_duration = 20
        self.config.sample_rate = 16000
        self.config.num_threads = 2

    def transcribe(self, wav_path: Path, progress=lambda *_: None):
        vad = self.sherpa.VoiceActivityDetector(self.config, buffer_size_in_seconds=60)
        segments = []

        def drain():
            while not vad.empty():
                speech = vad.front
                start = speech.start / 16000
                duration = len(speech.samples) / 16000
                stream = self.recognizer.create_stream()
                stream.accept_waveform(16000, speech.samples)
                self.recognizer.decode_stream(stream)
                text = stream.result.text.strip()
                vad.pop()
                if text:
                    segments.append({"start": start, "end": start + duration, "text": text})
                    progress(text)

        with wave.open(str(wav_path), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                raise ValueError("ASR 输入必须是 16kHz 单声道 PCM16 WAV")
            window = self.config.silero_vad.window_size
            while raw := audio.readframes(window):
                samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
                if len(samples) < window:
                    samples = np.pad(samples, (0, window - len(samples)))
                vad.accept_waveform(samples)
                drain()
            vad.flush()
            drain()
        text = "\n".join(item["text"] for item in segments)
        wav_path.with_suffix(".txt").write_text(text, encoding="utf-8")
        wav_path.with_suffix(".segments.json").write_text(
            json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return text


def decode_card_file(source: Path) -> Path:
    """Recognize WAV/Ogg by content; bare card Opus uses validated 40-byte packets."""
    with source.open("rb") as stream:
        header = stream.read(12)
    if header.startswith((b"RIFF", b"OggS")):
        target = source.with_suffix(".wav")
        temporary = target.with_suffix(".wav.part")
        try:
            with av.open(str(source)) as container, wave.open(str(temporary), "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
                samples = 0
                for frame in container.decode(audio=0):
                    for pcm in resampler.resample(frame):
                        output.writeframes(pcm.to_ndarray().astype("<i2").tobytes())
                        samples += pcm.samples
                for pcm in resampler.resample(None):
                    output.writeframes(pcm.to_ndarray().astype("<i2").tobytes())
                    samples += pcm.samples
            if not samples:
                raise ValueError("音频中没有可解码采样")
            temporary.replace(target)
            return target
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    decoder = OpusDecoder(source, "fixed40")
    try:
        with source.open("rb") as stream:
            while packet := stream.read(40):
                decoder.push(packet)
        return decoder.finish()
    except BaseException:
        decoder.abort()
        raise
