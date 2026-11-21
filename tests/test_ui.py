import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from voicehub.ui import Window


def test_credentials_saved_only_after_auth_and_selected_by_mac(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from voicehub.credentials import DeviceCredentials

    app = QApplication.instance() or QApplication([])
    saved = []
    credentials = SimpleNamespace(
        devices=dict,
        identity=DeviceCredentials.identity,
        key_for=lambda address: "ab" * 32 if address == "AA:BB" else "",
        remember=lambda *args: saved.append(args),
    )
    window = Window(tmp_path, tmp_path, auto_scan=False, credentials=credentials)
    monkeypatch.setattr(window.worker, "submit", lambda *args: None)
    monkeypatch.setattr(window, "background", lambda: None)
    monkeypatch.setattr("voicehub.ui.QMessageBox.critical", lambda *args: None)
    try:
        devices = [
            (SimpleNamespace(name="cached name", address="AA:BB"),
             SimpleNamespace(local_name="CAC10 advertised", rssi=-50)),
            (SimpleNamespace(name="CAC10 other", address="CC:DD"),
             SimpleNamespace(local_name=None, rssi=-60)),
        ]
        window.on_finished("scan", devices)
        assert window.key.text() == "ab" * 32
        window.device_list.setCurrentRow(1)
        assert window.key.text() == ""
        window.device_list.setCurrentRow(0)
        window.connect_device()
        assert not saved
        window.on_failed("connect", "验证失败")
        assert not saved
        assert window.pending_credentials is None
        window.select_device(0)
        window.connect_device()
        # Successful auth is enough, even if later optional information fails.
        window.on_event("authorized", "cached name")
        assert saved == [("CAC10 advertised", "AA:BB", "ab" * 32)]
        window.on_event("info", {"sn": "SN123"})
        assert "SN SN123" in window.info.text()
        window.on_event("disconnected", None)
        assert "SN123" not in window.info.text()
    finally:
        window.busy = False
        window.quit()
        assert window.worker.wait(5000)
        app.processEvents()
        window.hide()


def test_window_and_worker_lifecycle(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path, tmp_path, auto_scan=False)
    window.show()
    app.processEvents()
    assert not window.record_button.isEnabled()
    window.on_event("authorized", "CAC10")
    assert window.record_button.isEnabled()
    window.on_event("info", {"battery": 110, "gain": 3, "capacity": (10, 20)})
    assert "充电中" in window.info.text()
    window.on_event("disconnected", None)
    assert not window.record_button.isEnabled()
    loop = QEventLoop()
    window.worker.finished.connect(loop.quit)
    window.quit()
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert window.worker.wait(1000)
    window.hide()


def test_hardware_key_starts_once_and_waits_for_connection_refresh(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path, tmp_path, auto_scan=False)
    commands = []
    monkeypatch.setattr(window.worker, "submit", lambda *args: commands.append(args))
    monkeypatch.setattr("voicehub.ui.check_models", lambda _: None)
    monkeypatch.setattr(window, "background", lambda: None)
    try:
        window.hide()  # Hardware triggers must work while the main window is hidden.
        window.on_event("key_event", 1)
        assert commands == []  # No recording before authentication.
        window.busy = True
        window.active_job = "connect"
        window.on_event("authorized", "CAC10")
        window.on_event("key_event", 1)
        assert commands == []
        window.on_finished("connect", None)
        assert commands == [("start", tmp_path, "packet")]
        window.on_event("recording", "test.vhrec")
        window.on_finished("start", None)
        window.on_event("pcm_progress", 0.5)
        assert "0.50" in window.status.text()
        window.on_event("key_event", 1)
        window.on_event("key_event", 2)
        assert len(commands) == 2
        assert commands[-1] == ("stop",)
        window.on_finished("stop", tmp_path / "test.vhrec")
        assert commands[-1][0] == "transcribe"
        count = len(commands)
        window.on_event("key_event", 1)
        assert len(commands) == count  # Do not start a second session during ASR.
    finally:
        window.processing = window.busy = window.recording = False
        window.quit()
        assert window.worker.wait(5000)
        app.processEvents()
        window.hide()


@pytest.fixture
def hardware_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(tmp_path, tmp_path, auto_scan=False)
    commands = []
    monkeypatch.setattr(window.worker, "submit", lambda *args: commands.append(args))
    monkeypatch.setattr("voicehub.ui.check_models", lambda _: None)
    monkeypatch.setattr(window, "background", lambda: None)
    window.on_event("authorized", "CAC10")
    window.hide()
    yield window, commands
    window.processing = window.busy = window.recording = False
    window.quit()
    assert window.worker.wait(5000)
    app.processEvents()
    window.hide()


@pytest.mark.parametrize("stop_key", [1, 3])
def test_second_press_stops_active_recording(hardware_window, tmp_path, stop_key):
    window, commands = hardware_window
    window.on_event("key_event", 1)
    window.on_event("recording", "test.vhrec")
    window.on_finished("start", None)
    window.on_event("key_event", stop_key)
    window.on_event("key_event", stop_key)  # Ignore extra notifications while stopping.
    assert commands == [("start", tmp_path, "packet"), ("stop",)]
    window.on_finished("stop", tmp_path / "test.vhrec")
    assert not window.recording
    assert window.processing
    assert commands[-1][0] == "transcribe"
    window.on_event("key_event", 3)
    assert len(commands) == 3


@pytest.mark.parametrize("stop_key", [1, 3])
def test_quick_second_press_waits_for_start_then_stops(hardware_window, stop_key):
    window, commands = hardware_window
    window.on_event("key_event", 1)
    window.on_event("key_event", stop_key)  # Device is still acknowledging stream start.
    assert len(commands) == 1
    window.on_event("recording", "test.vhrec")
    assert len(commands) == 1
    window.on_finished("start", None)
    assert commands[-1] == ("stop",)
    assert len(commands) == 2


def test_stop_event_cancels_deferred_start_and_never_starts_idle(hardware_window):
    window, commands = hardware_window
    window.on_event("key_event", 3)
    assert commands == []
    window.busy = True
    window.active_job = "connect"
    window.on_event("key_event", 1)
    assert window.pending_key_start
    window.on_event("key_event", 3)
    window.on_finished("connect", None)
    assert commands == []


def test_device_stopped_notification_and_key_stop_transcribe_once(hardware_window, tmp_path):
    window, commands = hardware_window
    path = tmp_path / "test.vhrec"
    window.on_event("key_event", 1)
    window.on_event("recording", str(path))
    window.on_finished("start", None)
    window.on_event("key_event", 3)
    window.on_event("interrupted", str(path))
    window.on_finished("stop", path)
    assert len([cmd for cmd in commands if cmd[0] == "transcribe"]) == 1
    assert not window.recording
    assert window.processing


def test_microphone_mode_skips_models_and_finalizes_without_asr(
    hardware_window, tmp_path, monkeypatch
):
    window, commands = hardware_window
    window.auto_transcribe.setChecked(False)
    window.output_device.addItem("Test CABLE Input", 7)
    window.output_device.setCurrentIndex(window.output_device.count() - 1)

    def forbid_models(_):
        pytest.fail("Microphone-only mode must not require ASR models")

    monkeypatch.setattr("voicehub.ui.check_models", forbid_models)
    window.toggle_record()
    assert commands == [("start", tmp_path, "packet", 7)]
    window.on_event("recording", "test.vhrec")
    window.on_finished("start", None)
    assert not window.output_device.isEnabled()
    window.toggle_record()
    window.on_finished("stop", tmp_path / "test.vhrec")
    assert commands[-1] == ("finalize", tmp_path / "test.vhrec")
    window.on_finished("finalize", tmp_path / "test.wav")
    assert not window.processing
    assert window.output_device.isEnabled()


def test_live_input_before_stop_and_tail_never_reinserted(hardware_window, tmp_path, monkeypatch):
    window, commands = hardware_window
    sent = []
    target = object()

    class FakeInjector:
        def capture_target(self):
            return target

        def send(self, text, selected):
            assert selected is target
            sent.append(text)

    monkeypatch.setattr("voicehub.ui.TextInjector", FakeInjector)
    monkeypatch.setattr(window, "reveal", lambda: pytest.fail("Must not steal input focus"))
    window.live_transcribe.setChecked(True)
    window.type_current.setChecked(True)
    window.toggle_record()
    assert commands == [("start", tmp_path, "packet", None, tmp_path)]
    window.on_event("recording", "test.vhrec")
    window.on_finished("start", None)
    window.on_event("live_segment", "你好")
    assert window.recording
    assert sent == ["你好"]
    window.toggle_record()
    window.on_finished("stop", tmp_path / "test.vhrec")
    assert "你好" in window.text.toPlainText()
    assert commands[-1] == ("live_finalize", tmp_path / "test.vhrec")
    window.on_event("live_segment", "世界\n下一句")
    window.on_finished("live_finalize", "你好\n世界\n下一句")
    assert sent == ["你好", "世界 下一句"]
    assert not window.processing
    assert not window.live_session


def test_changed_input_target_pauses_for_rest_of_session(hardware_window, monkeypatch):
    window, _ = hardware_window
    calls = []

    class FakeInjector:
        def capture_target(self):
            return object()

        def send(self, text, target):
            calls.append(text)
            raise RuntimeError("窗口已切换")

    monkeypatch.setattr("voicehub.ui.TextInjector", FakeInjector)
    window.live_transcribe.setChecked(True)
    window.type_current.setChecked(True)
    window.toggle_record()
    window.on_event("live_segment", "第一段")
    window.on_event("live_segment", "第二段")
    assert calls == ["第一段"]
    assert window.input_paused
    assert "第二段" in window.text.toPlainText()
    assert "暂停" in window.input_status.text()


def test_live_input_binds_first_external_window_and_separates_english(hardware_window, monkeypatch):
    window, _ = hardware_window
    target = object()
    sent = []

    class FakeInjector:
        captures = 0

        def capture_target(self):
            self.captures += 1
            return None if self.captures == 1 else target

        def send(self, text, selected):
            assert selected is target
            sent.append(text)

    monkeypatch.setattr("voicehub.ui.TextInjector", FakeInjector)
    window.live_transcribe.setChecked(True)
    window.type_current.setChecked(True)
    window.toggle_record()
    assert window.input_target is None
    window.on_event("live_segment", "hello")
    window.on_event("live_segment", "world")
    assert sent == ["hello", " world"]


def test_live_finalize_error_preserves_text_without_stealing_focus(hardware_window, monkeypatch):
    window, _ = hardware_window
    window.live_transcribe.setChecked(True)
    window.type_current.setChecked(True)
    window.live_session = True
    window.processing = True
    window.text.setPlainText("已确认的文字")
    monkeypatch.setattr(window, "reveal", lambda: pytest.fail("Must not take input focus"))
    window.on_failed("live_finalize", "测试识别失败")
    assert window.text.toPlainText() == "已确认的文字"
    assert not window.processing
    assert not window.live_session


def test_live_preview_never_types_without_input_option(hardware_window, monkeypatch):
    window, _ = hardware_window
    monkeypatch.setattr("voicehub.ui.TextInjector", lambda: pytest.fail("input not enabled"))
    window.live_transcribe.setChecked(True)
    window.toggle_record()
    window.on_event("live_segment", "只在应用中显示")
    assert window.text.toPlainText() == "只在应用中显示"
