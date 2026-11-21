from pathlib import Path

from PySide6.QtCore import QSettings, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .audio import check_models
from .credentials import DeviceCredentials
from .file_manager import FileManager
from .output_audio import list_output_devices
from .protocol import auth_key
from .text_input import TextInjector
from .worker import Worker


def microphone_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("#2563eb"))
    painter = QPainter(pixmap)
    painter.setPen(QColor("white"))
    painter.setBrush(QColor("white"))
    painter.drawRoundedRect(25, 10, 14, 29, 7, 7)
    painter.drawArc(18, 20, 28, 27, 180 * 16, 180 * 16)
    painter.drawRect(30, 43, 3, 10)
    painter.drawRect(23, 53, 18, 2)
    painter.end()
    return QIcon(pixmap)


class Window(QMainWindow):
    def __init__(self, models: Path, recordings: Path, auto_scan=True, credentials=None):
        super().__init__()
        self.models, self.recordings = models, recordings
        self.authorized = False
        self.recording = False
        self.busy = False
        self.processing = False
        self.quitting = False
        self.last_capture = None
        self.pending_key_start = False
        self.pending_key_stop = False
        self.active_job = None
        self.devices = []
        self.device_names = []
        self.credentials = credentials if credentials is not None else DeviceCredentials()
        self.pending_credentials = None
        self.injector = None
        self.input_target = None
        self.input_paused = False
        self.last_inserted = ""
        self.file_manager = None
        self.live_session = False
        self.recovering_capture = False
        self.setWindowTitle("VoiceHub · 无线语音输入")
        self.resize(1000, 780)
        self.setMinimumSize(860, 680)
        self.setWindowIcon(microphone_icon())
        self.settings = QSettings("VoiceHub", "Desktop")
        self.device_name = ""
        self.battery_text = "—"
        self.audio_seconds = 0.0
        self.build_interface()
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("VoiceHub · 未连接")
        menu = QMenu(self)
        show = QAction("打开 VoiceHub", self)
        show.triggered.connect(self.reveal)
        self.record_action = QAction("开始录音", self)
        self.record_action.triggered.connect(self.toggle_record)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit)
        menu.addAction(show)
        menu.addAction(self.record_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self.reveal() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
            )
        )
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.worker = Worker()
        self.worker.event.connect(self.on_event)
        self.worker.finished_job.connect(self.on_finished)
        self.worker.failed_job.connect(self.on_failed)
        self.worker.finished.connect(self.on_worker_exit)
        self.worker.start()
        self.refresh_outputs()
        self.update_controls()
        if auto_scan:
            QTimer.singleShot(100, self.start_scan)

    def build_interface(self):
        content = QWidget()
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("VoiceHub")
        title.setStyleSheet("font-size:26px;font-weight:600")
        header.addWidget(title)
        header.addStretch()
        settings_button = QPushButton("高级设置")
        settings_button.clicked.connect(self.open_settings)
        header.addWidget(settings_button)
        hide = QPushButton("进入后台")
        hide.clicked.connect(self.background)
        header.addWidget(hide)
        layout.addLayout(header)

        summary = QHBoxLayout()
        self.connection_status = QLabel("未连接设备")
        self.connection_status.setWordWrap(True)
        summary.addWidget(self.connection_status, 1)
        self.session_status = QLabel("待机")
        summary.addWidget(self.session_status)
        self.stop_button = QPushButton("停止录音")
        self.stop_button.clicked.connect(self.toggle_record)
        summary.addWidget(self.stop_button)
        layout.addLayout(summary)
        self.status = QLabel("就绪，请连接录音卡")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)
        device_page = QWidget()
        device_layout = QVBoxLayout(device_page)
        device_layout.setSpacing(14)
        device_layout.addWidget(QLabel("选择附近的 CAC10 录音卡，验证成功后会记住设备与密钥。"))
        self.device_list = QListWidget()
        self.device_list.setMinimumHeight(150)
        device_layout.addWidget(self.device_list, 1)
        self.scan_button = QPushButton("重新扫描 CAC10")
        self.connect_button = QPushButton("连接并验证")
        self.disconnect_button = QPushButton("断开")
        self.scan_button.clicked.connect(self.start_scan)
        self.connect_button.clicked.connect(self.connect_device)
        self.disconnect_button.clicked.connect(lambda: self.job("disconnect"))
        row = QHBoxLayout()
        for button in (self.scan_button, self.connect_button, self.disconnect_button):
            row.addWidget(button)
        device_layout.addLayout(row)
        form = QFormLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("64 位十六进制 AuthKey，验证成功后自动记住")
        self.device_list.currentRowChanged.connect(self.select_device)
        form.addRow("AuthKey", self.key)
        device_layout.addLayout(form)
        details = QGroupBox("设备信息")
        details_layout = QVBoxLayout(details)
        self.info = QLabel("电量 —    增益 —    容量 —    固件 —    SN —")
        self.info.setWordWrap(True)
        details_layout.addWidget(self.info)
        self.refresh_button = QPushButton("刷新设备信息")
        self.refresh_button.clicked.connect(lambda: self.job("refresh"))
        details_layout.addWidget(self.refresh_button)
        device_layout.addWidget(details)
        self.tabs.addTab(device_page, "设备连接")

        live_page = QWidget()
        live_layout = QVBoxLayout(live_page)
        live_layout.setSpacing(12)
        options = QHBoxLayout()
        speech = QGroupBox("语音转文字")
        speech_layout = QVBoxLayout(speech)
        self.live_transcribe = QCheckBox("边说边转写")
        self.live_transcribe.setToolTip("短暂停顿或约 3 秒后输出一段文字")
        self.live_transcribe.toggled.connect(lambda _: self.update_controls())
        self.type_current = QCheckBox("将实时文字输入到当前窗口")
        self.auto_transcribe = QCheckBox("停止后自动进行离线转写")
        self.auto_transcribe.setChecked(True)
        self.auto_transcribe.toggled.connect(lambda _: self.update_controls())
        for widget in (self.live_transcribe, self.type_current, self.auto_transcribe):
            speech_layout.addWidget(widget)
        self.input_status = QLabel(
            "直接输入：先勾选实时转写，将光标放到目标输入框，再按录音卡按键。"
        )
        self.input_status.setWordWrap(True)
        speech_layout.addWidget(self.input_status)
        speech_layout.addStretch()
        options.addWidget(speech, 1)
        audio = QGroupBox("音频输出 / 虚拟麦克风")
        audio_layout = QVBoxLayout(audio)
        self.output_device = QComboBox()
        self.output_device.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.output_device.setMinimumContentsLength(18)
        audio_layout.addWidget(self.output_device)
        self.output_refresh = QPushButton("刷新输出设备")
        self.output_refresh.clicked.connect(self.refresh_outputs)
        audio_layout.addWidget(self.output_refresh)
        self.output_status = QLabel("未启用实时音频输出")
        self.output_status.setWordWrap(True)
        audio_layout.addWidget(self.output_status)
        hint = QLabel(
            "使用虚拟麦克风需安装 VB-CABLE：此处选 CABLE Input，其他应用的麦克风选 CABLE Output。"
        )
        hint.setWordWrap(True)
        audio_layout.addWidget(hint)
        audio_layout.addStretch()
        options.addWidget(audio, 1)
        live_layout.addLayout(options)
        self.record_button = QPushButton("开始录音")
        self.record_button.setMinimumHeight(48)
        self.record_button.setStyleSheet("background:#2563eb;color:white;font-size:17px")
        self.record_button.clicked.connect(self.toggle_record)
        live_layout.addWidget(self.record_button)
        self.live_hint = QLabel("请先在“设备连接”中连接并验证录音卡。")
        self.live_hint.setWordWrap(True)
        live_layout.addWidget(self.live_hint)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("识别文字显示在这里，可编辑后复制。")
        live_layout.addWidget(self.text, 1)
        copy = QPushButton("复制全部文字")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        live_layout.addWidget(copy)
        self.tabs.addTab(live_page, "实时语音")

        files_page = QWidget()
        files_layout = QVBoxLayout(files_page)
        self.file_manager = FileManager(self)
        files_layout.addWidget(self.file_manager, 1)
        self.retry_button = self.file_manager.retry_button
        self.tabs.addTab(files_page, "录音与解析")

        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle("高级设置")
        self.settings_dialog.resize(660, 440)
        settings_layout = QVBoxLayout(self.settings_dialog)
        settings_layout.addWidget(QLabel("模型与音频分包配置供实时语音和文件转写共用。"))
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("每个实时载荷是一个 Opus 包（默认，需真机确认）", "packet")
        self.mode.addItem("已确认固件：载荷由 40 字节 Opus 包组成", "fixed40")
        form.addRow("音频分包", self.mode)
        self.model_path = QLineEdit(str(self.models))
        self.model_path.setReadOnly(True)
        self.choose_models_button = QPushButton("选择模型目录")
        self.choose_models_button.clicked.connect(self.choose_models)
        row = QHBoxLayout()
        row.addWidget(self.model_path, 1)
        row.addWidget(self.choose_models_button)
        form.addRow("离线模型", row)
        form.addRow("录音文件本地保存目录", self.file_manager.directory_settings)
        settings_layout.addLayout(form)
        settings_layout.addWidget(QLabel("诊断日志"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(150)
        settings_layout.addWidget(self.log, 1)
        close = QPushButton("关闭")
        close.clicked.connect(self.settings_dialog.hide)
        settings_layout.addWidget(close)
        page = self.settings.value("main_tab", 0)
        try:
            self.tabs.setCurrentIndex(max(0, min(2, int(page))))
        except (TypeError, ValueError):
            pass
        self.tabs.currentChanged.connect(lambda index: self.settings.setValue("main_tab", index))

    def open_settings(self):
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def open_files(self):
        self.tabs.setCurrentIndex(2)
        self.file_manager.update_controls()

    def update_controls(self):
        idle = not self.busy and not self.processing and not self.quitting
        if self.file_manager is not None:
            self.file_manager.update_controls()
        self.connection_status.setText(
            f"{self.device_name} 已连接 · 电量 {self.battery_text}"
            if self.authorized
            else "未连接设备"
        )
        self.session_status.setText(
            f"正在录音 · {int(self.audio_seconds) // 60:02d}:{int(self.audio_seconds) % 60:02d}"
            if self.recording
            else ("正在处理…" if self.processing or self.busy else "待机")
        )
        self.stop_button.setVisible(self.recording)
        self.stop_button.setEnabled(idle and self.recording)
        self.live_hint.setText(
            "正在收音；切换页面后录音继续，可随时使用顶部按钮停止。"
            if self.recording
            else (
                "设备已就绪，可点击开始录音或按录音卡按键。"
                if self.authorized
                else "请先在“设备连接”中连接并验证录音卡。"
            )
        )
        self.scan_button.setEnabled(idle and not self.authorized)
        self.device_list.setEnabled(idle and not self.authorized)
        self.key.setEnabled(idle and not self.authorized)
        self.connect_button.setEnabled(idle and not self.authorized)
        self.disconnect_button.setEnabled(idle and self.authorized and not self.recording)
        self.refresh_button.setEnabled(idle and self.authorized and not self.recording)
        enabled = idle and self.authorized
        self.record_button.setEnabled(enabled)
        self.record_action.setEnabled(enabled)
        stop_label = (
            "停止录音并识别"
            if self.auto_transcribe.isChecked() and not self.live_transcribe.isChecked()
            else "停止录音"
        )
        label = stop_label if self.recording else "开始录音"
        self.record_button.setText(label)
        self.record_action.setText(label)
        self.retry_button.setEnabled(idle and not self.recording)
        self.choose_models_button.setEnabled(idle and not self.recording)
        self.mode.setEnabled(idle and not self.recording)
        self.output_device.setEnabled(idle and not self.recording)
        self.output_refresh.setEnabled(idle and not self.recording)
        configurable = idle and not self.recording
        self.auto_transcribe.setEnabled(configurable and not self.live_transcribe.isChecked())
        self.live_transcribe.setEnabled(configurable)
        self.type_current.setEnabled(configurable and self.live_transcribe.isChecked())

    def refresh_outputs(self):
        previous = self.output_device.currentData()
        self.output_device.clear()
        self.output_device.addItem("关闭（仅保存录音 / 转写）", None)
        try:
            for device in list_output_devices():
                self.output_device.addItem(f"{device['name']} · {device['hostapi']}", device["id"])
            index = self.output_device.findData(previous)
            if index >= 0:
                self.output_device.setCurrentIndex(index)
            self.output_status.setText("请选择输出设备，开始录音后生效；默认关闭")
        except Exception as exc:  # noqa: BLE001 - audio device enumeration is optional
            self.output_status.setText(f"无法枚举音频输出设备：{exc}")

    def job(self, name, *args):
        self.active_job = name
        self.busy = True
        self.update_controls()
        self.worker.submit(name, *args)

    def start_scan(self):
        self.device_list.clear()
        self.devices = []
        self.status.setText("正在扫描附近的 CAC10（6 秒）…")
        self.job("scan")

    def select_device(self, index):
        self.key.clear()
        if 0 <= index < len(self.devices):
            try:
                self.key.setText(self.credentials.key_for(self.devices[index].address))
            except Exception:  # noqa: BLE001 - credential backend failures must remain recoverable
                self.log.appendPlainText("无法读取已保存的 AuthKey，请重新输入")

    def connect_device(self):
        index = self.device_list.currentRow()
        if index < 0:
            QMessageBox.information(self, "选择设备", "请从列表中选择录音卡")
            return
        try:
            auth_key(self.key.text())
        except ValueError as exc:
            QMessageBox.warning(self, "AuthKey", str(exc))
            return
        self.status.setText("正在连接、验证并获取设备信息…")
        key = self.key.text()
        device = self.devices[index]
        name = self.device_names[index] if index < len(self.device_names) else device.name
        self.pending_credentials = (name or "CAC10", device.address, key)
        self.key.clear()
        self.job("connect", device, key)

    def toggle_record(self):
        if self.busy or self.processing or not self.authorized:
            return
        if self.recording:
            self.status.setText("正在停止接收，等待设备确认…")
            self.job("stop")
        else:
            try:
                if self.auto_transcribe.isChecked() or self.live_transcribe.isChecked():
                    check_models(self.models)
            except FileNotFoundError as exc:
                self.reveal()
                QMessageBox.warning(self, "请准备离线模型", str(exc))
                return
            self.text.clear()
            self.live_session = self.live_transcribe.isChecked()
            self.input_target = None
            self.input_paused = False
            self.last_inserted = ""
            if self.live_session and self.type_current.isChecked():
                try:
                    self.injector = TextInjector()
                    self.input_target = self.injector.capture_target()
                    self.input_status.setText("实时输入已就绪，请保持目标输入框处于前台")
                except (OSError, RuntimeError) as exc:
                    self.input_paused = True
                    self.input_status.setText(f"直接输入不可用：{exc}；仍会显示识别文字")
            elif self.live_session:
                self.input_status.setText("正在准备实时识别，文字将显示在下方")
            self.status.setText("等待设备推送音频…")
            args = [self.recordings, self.mode.currentData()]
            if self.output_device.currentData() is not None or self.live_session:
                args.append(self.output_device.currentData())
                self.output_status.setText(
                    "正在打开音频输出设备…" if args[-1] is not None else "未启用实时音频输出"
                )
            else:
                self.output_status.setText("未启用实时音频输出")
            if self.live_session:
                args.append(self.models)
            self.job("start", *args)

    @property
    def transcript_editor(self):
        return self.file_manager.recovery_text if self.recovering_capture else self.text

    def process_capture(self, path, force_transcribe=False):
        self.pending_key_start = self.pending_key_stop = False
        self.last_capture = Path(path)
        self.recording = False
        self.processing = True
        self.recovering_capture = force_transcribe
        if not self.live_session or force_transcribe:
            self.transcript_editor.clear()
        self.update_controls()
        if self.live_session and not force_transcribe:
            self.status.setText("正在完成尾部识别并保存录音…")
            self.worker.submit("live_finalize", self.last_capture)
        elif force_transcribe or self.auto_transcribe.isChecked():
            self.worker.submit(
                "transcribe", self.last_capture, self.models, self.mode.currentData()
            )
        else:
            self.status.setText("正在保存录音并结束音频输出…")
            self.worker.submit("finalize", self.last_capture)

    def on_event(self, event, data):
        if self.quitting:
            return
        if event == "file_progress" and self.file_manager is not None:
            self.file_manager.status.setText(str(data))
            self.status.setText(str(data))
        elif event == "library_changed" and self.file_manager is not None:
            self.file_manager.refresh_local()
        elif event == "files_deleted" and self.file_manager is not None:
            self.file_manager.set_card(
                [e for e in self.file_manager.entries if e["raw"] not in data]
            )
        elif event == "warning":
            self.log.appendPlainText(str(data))
        elif event == "output_status":
            self.output_status.setText(str(data))
            self.log.appendPlainText(str(data))
        elif event == "key_event" and data in (1, 3):
            self.handle_hardware_key(data)
        elif event == "authorized":
            if self.pending_credentials is not None:
                credentials, self.pending_credentials = self.pending_credentials, None
                try:
                    self.credentials.remember(*credentials)
                except Exception:  # noqa: BLE001 - do not expose credential backend error details
                    self.log.appendPlainText(
                        "验证成功，但设备凭据保存失败；下次连接需重新输入 AuthKey"
                    )
            if self.file_manager is not None:
                self.file_manager.set_card([])
            self.authorized = True
            self.device_name = str(data)
            self.file_manager.status.setText(
                "录音卡已连接，可切换到卡端文件读取列表；本地录音可直接查看。"
            )
            self.tray.setToolTip(f"VoiceHub · {data} 已连接")
        elif event == "disconnected":
            if self.file_manager is not None:
                self.file_manager.set_card([])
            self.pending_key_start = self.pending_key_stop = False
            self.authorized = False
            self.device_name = ""
            self.battery_text = "—"
            self.recording = False
            self.info.setText("电量 —    增益 —    容量 —    固件 —    SN —")
            self.file_manager.status.setText("设备已断开，本地录音仍可查看、编辑和转写。")
            self.status.setText("蓝牙已断开，请重新扫描连接")
            self.tray.setToolTip("VoiceHub · 已断开")
        elif event == "info":
            battery = data.get("battery")
            battery_text = (
                "充电中"
                if battery == 110
                else (f"{battery}%" if battery is not None and battery <= 100 else "未知")
            )
            self.battery_text = battery_text
            capacity = data.get("capacity")
            storage = (
                (f"{capacity[0] / 1024:.1f} / {capacity[1] / 1024:.1f} MiB（按 1KiB/单位估算）")
                if capacity
                else "未知"
            )
            self.info.setText(
                f"电量 {battery_text}    增益 "
                f"{ {1: '低', 2: '中', 3: '高'}.get(data.get('gain'), '未知') }\n"
                f"剩余/总容量 {storage}    固件 {data.get('firmware', '未知')}\n"
                f"SN {data.get('sn') or '未知（设备未提供或读取失败）'}"
            )
        elif event == "recording":
            self.pending_key_start = False
            self.recording = True
            self.audio_seconds = 0.0
            self.status.setText(
                "正在收音并实时转写…" if self.live_session else "正在接收录音并实时解码…"
            )
            self.tray.setToolTip("VoiceHub · 正在录音")
        elif event == "interrupted":
            self.log.appendPlainText(f"录音中断或设备停止，已保存：{data}")
            if not self.processing:
                self.process_capture(data)
        elif event == "processing":
            self.status.setText(str(data))
        elif event == "segment":
            self.transcript_editor.appendPlainText(str(data))
        elif event == "live_segment":
            self.text.appendPlainText(str(data))
            self.insert_live_text(str(data))
        elif event == "live_asr_status":
            if not self.input_paused:
                self.input_status.setText(str(data))
        elif event == "live_asr_failed":
            self.input_paused = True
            self.input_status.setText(str(data))
            self.log.appendPlainText(str(data))
        elif event == "pcm_progress":
            self.audio_seconds = float(data)
            if not self.processing:
                self.status.setText(f"实时解码：{data:.2f} 秒 · 16kHz 单声道 PCM")
        elif event == "decode_failed":
            self.log.appendPlainText(str(data))
            self.status.setText(str(data))
        self.update_controls()

        self.apply_pending_key()

    def insert_live_text(self, text):
        if not self.live_session or not self.type_current.isChecked() or self.input_paused:
            return
        # Never turn model line breaks into Enter in a chat application.
        text = " ".join(text.split())
        if not text:
            return
        if (
            self.last_inserted
            and self.last_inserted[-1].isascii()
            and self.last_inserted[-1].isalnum()
            and text[0].isascii()
            and text[0].isalnum()
        ):
            text = " " + text
        try:
            if self.input_target is None:
                self.input_target = self.injector.capture_target()
            if self.input_target is None:
                raise RuntimeError("未找到外部输入窗口，请先聚焦输入框再开始录音")
            self.injector.send(text, self.input_target)
            self.last_inserted = text
            self.input_status.setText("正在输入实时文字；前台窗口或系统焦点变化时暂停本次输入")
        except (OSError, RuntimeError, ValueError) as exc:
            self.input_paused = True
            self.input_status.setText(f"实时输入已暂停：{exc}；完整文字保留在下方，可手动复制")
            self.log.appendPlainText(self.input_status.text())

    def handle_hardware_key(self, key):
        if not self.authorized or self.processing:
            return
        if key == 3:
            self.pending_key_start = False
            if self.busy and self.active_job == "start":
                self.pending_key_stop = True
            elif not self.busy and self.recording:
                self.log.appendPlainText("收到录音卡停止事件 3，自动停止收音")
                self.toggle_record()
        elif self.busy:
            if self.active_job == "start":
                self.pending_key_stop = True
            elif self.active_job in ("connect", "refresh"):
                self.pending_key_start = True
        else:
            self.log.appendPlainText(
                "再次按键，自动停止收音" if self.recording else "收到录音卡按键事件 1，自动开始录音"
            )
            self.toggle_record()

    def apply_pending_key(self):
        if self.busy or self.processing:
            return
        if self.pending_key_stop:
            self.pending_key_stop = self.pending_key_start = False
            if self.authorized and self.recording:
                self.toggle_record()
        elif self.pending_key_start:
            self.pending_key_start = False
            if self.authorized and not self.recording:
                self.toggle_record()

    def on_finished(self, name, result):
        if self.quitting:
            return
        if name not in ("transcribe", "finalize", "live_finalize"):
            self.busy = False
            self.active_job = None
        if name == "scan":
            self.device_list.clear()
            self.devices = [device for device, _ in result]
            self.device_names = [adv.local_name or device.name or "CAC10" for device, adv in result]
            remembered = self.credentials.devices()
            for device, adv in result:
                saved = self.credentials.identity(device.address) in remembered
                self.device_list.addItem(
                    f"{adv.local_name or device.name}   {device.address}   {adv.rssi} dBm"
                    + ("   已记住" if saved else "")
                )
            if result:
                self.device_list.setCurrentRow(0)
            self.status.setText(
                f"找到 {len(result)} 台 CAC10，请选择设备；已验证设备自动填入 AuthKey"
                if result
                else "未找到 CAC10，请确认设备已开机且未被其他客户端占用"
            )
        elif name.startswith("file_"):
            self.status.setText(f"文件操作完成：{len(result)} 个文件")
            if self.file_manager is not None:
                if name == "file_list":
                    self.file_manager.set_card(result)
                else:
                    self.file_manager.refresh_local()
                    self.file_manager.status.setText(f"操作完成：{len(result)} 个文件")
        elif name == "connect":
            self.status.setText("鉴权成功，设备信息已读取；可前往“实时语音”开始录音")
        elif name == "stop":
            # A device-stopped notification may already have scheduled this transcript.
            if Path(result) != self.last_capture:
                self.process_capture(result)
        elif name == "transcribe":
            self.processing = False
            self.transcript_editor.setPlainText(result)
            self.recovering_capture = False
            self.status.setText("识别完成，可复制文字" if result else "识别完成，未检测到有效语音")
            self.tray.setToolTip("VoiceHub · 识别完成")
            self.reveal()
        elif name == "finalize":
            self.processing = False
            self.status.setText(f"录音已保存：{result}")
            self.tray.setToolTip("VoiceHub · 已停止录音")
        elif name == "live_finalize":
            self.processing = False
            self.live_session = False
            self.text.setPlainText(result)
            self.status.setText("实时转写完成，录音和文字已保存")
            self.tray.setToolTip("VoiceHub · 实时转写完成")
        self.update_controls()
        self.apply_pending_key()

    def on_failed(self, name, message):
        if name == "connect":
            self.pending_credentials = None
        self.pending_key_start = self.pending_key_stop = False
        if name in ("transcribe", "finalize", "live_finalize"):
            self.processing = False
            self.recovering_capture = False
        else:
            self.busy = False
            self.active_job = None
        if name.startswith("file_") and self.file_manager is not None:
            self.file_manager.status.setText(message)
            self.file_manager.refresh_local()
        self.log.appendPlainText(f"{name}：{message}")
        self.status.setText(message)
        if not (self.live_session and self.type_current.isChecked()):
            self.reveal()
        if name == "live_finalize":
            self.live_session = False
        self.update_controls()

    def choose_models(self):
        if self.busy or self.processing or self.recording:
            return
        selected = QFileDialog.getExistingDirectory(self, "选择离线模型目录", str(self.models))
        if selected:
            self.models = Path(selected)
            self.model_path.setText(selected)

    def retry(self):
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择原始录音重新解码识别", str(self.recordings), "VoiceHub (*.vhrec)"
        )
        if selected:
            self.open_files()
            self.file_manager.source.setCurrentIndex(2)
            self.file_manager.recovery_title.setText(Path(selected).name)
            self.process_capture(selected, force_transcribe=True)

    def open_recordings(self):
        self.recordings.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.recordings)))

    def reveal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def background(self):
        if self.tray.isVisible():
            self.tray.showMessage(
                "VoiceHub 在后台运行", "右键托盘图标可开始/停止录音，双击打开窗口"
            )
            self.hide()
        else:
            self.status.setText(self.status.text() + "；当前系统无托盘，保留窗口")

    def closeEvent(self, event):
        event.ignore()
        if self.tray.isVisible():
            self.background()
        else:
            self.quit()

    def quit(self):
        if self.processing:
            QMessageBox.information(self, "正在识别", "请等待本次离线识别完成后退出")
            return
        if self.busy:
            QMessageBox.information(self, "正在操作", "请等待当前蓝牙操作完成后退出")
            return
        self.quitting = True
        self.status.setText("正在断开设备并退出…")
        self.update_controls()
        self.worker.shutdown()

    def on_worker_exit(self):
        self.tray.hide()
        QApplication.instance().quit()
