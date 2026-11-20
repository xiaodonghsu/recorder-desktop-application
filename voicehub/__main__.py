import argparse
import sys
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .ui import Window


def main():
    parser = argparse.ArgumentParser(description="VoiceHub CAC10 离线语音输入")
    parser.add_argument("--models", type=Path, default=Path("models"))
    parser.add_argument("--recordings", type=Path, default=Path("recordings"))
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("VoiceHub Microphone")
    if sys.platform == "win32":
        app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setQuitOnLastWindowClosed(False)
    window = Window(args.models.resolve(), args.recordings.resolve())
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
