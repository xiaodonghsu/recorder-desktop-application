"""Explicit one-time download; recognition itself never needs the network."""

import argparse
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
MODEL = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"


def download(url, path):
    print(f"下载 {url}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "VoiceHub/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def install(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        temp = Path(directory)
        if not (root / "silero_vad.onnx").is_file():
            download(BASE + "silero_vad.onnx", temp / "vad")
            (temp / "vad").replace(root / "silero_vad.onnx")
        target = root / "sense-voice"
        target.mkdir(exist_ok=True)
        if not all((target / name).is_file() for name in ("model.int8.onnx", "tokens.txt")):
            archive = temp / "model.tar.bz2"
            download(BASE + MODEL + ".tar.bz2", archive)
            # Extract only named regular files; never trust archive paths or symlinks.
            with tarfile.open(archive, "r:bz2") as tar:
                for name in ("model.int8.onnx", "tokens.txt"):
                    member = tar.getmember(f"{MODEL}/{name}")
                    if not member.isfile():
                        raise ValueError("模型压缩包内容无效")
                    with tar.extractfile(member) as src, (temp / name).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                for name in ("model.int8.onnx", "tokens.txt"):
                    (temp / name).replace(target / name)
    print(f"离线模型已准备好：{root.resolve()}")


def main():
    parser = argparse.ArgumentParser(description="下载 Silero VAD 和 SenseVoice 多语言离线模型")
    parser.add_argument("--dir", type=Path, default=Path("models"))
    install(parser.parse_args().dir)


if __name__ == "__main__":
    main()
