from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
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
    QVBoxLayout,
    QWidget,
)

from .audio import check_models
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
    def __init__(self, models: Path, recordings: Path, auto_scan=True):
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
        self.injector = None
        self.input_target = None
        self.input_paused = False
        self.last_inserted = ""
        self.live_session = False
        self.setWindowTitle("VoiceHub · 无线语音输入")
        self.resize(760, 760)
        self.setWindowIcon(microphone_icon())
        content = QWidget()
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 22, 24, 22)
        title = QLabel("VoiceHub Microphone")
        title.setStyleSheet("font-size:24px;font-weight:600")
        layout.addWidget(title)
        layout.addWidget(QLabel("CAC10 蓝牙录音卡  ·  本机转写  ·  实时文字输入"))
        self.status = QLabel("就绪，正在准备扫描…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.device_list = QListWidget()
        self.device_list.setMaximumHeight(120)
        layout.addWidget(self.device_list)
        self.scan_button = QPushButton("重新扫描 CAC10")
        self.connect_button = QPushButton("连接并验证")
        self.disconnect_button = QPushButton("断开")
        self.scan_button.clicked.connect(self.start_scan)
        self.connect_button.clicked.connect(self.connect_device)
        self.disconnect_button.clicked.connect(lambda: self.job("disconnect"))
        row = QHBoxLayout()
        for button in (self.scan_button, self.connect_button, self.disconnect_button):
            row.addWidget(button)
        layout.addLayout(row)
        form = QFormLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("64 位十六进制 AuthKey，仅在本次连接使用")
        form.addRow("AuthKey", self.key)
        self.info = QLabel("电量 —    增益 —    容量 —    固件 —")
        self.info.setWordWrap(True)
        form.addRow("设备信息", self.info)
        self.mode = QComboBox()
        self.mode.addItem("每个实时载荷是一个 Opus 包（默认，需真机确认）", "packet")
        self.mode.addItem("已确认固件：载荷由 40 字节 Opus 包组成", "fixed40")
        form.addRow("音频分包", self.mode)
        self.output_device = QComboBox()
        self.output_device.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.output_device.setMinimumContentsLength(24)
        self.output_refresh = QPushButton("刷新输出设备")
        self.output_refresh.clicked.connect(self.refresh_outputs)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_device, 1)
        output_row.addWidget(self.output_refresh)
        form.addRow("实时音频输出", output_row)
        self.output_status = QLabel("未启用实时音频输出")
        self.output_status.setWordWrap(True)
        form.addRow("输出状态", self.output_status)
        hint = QLabel("选择 CABLE Input；其他应用的麦克风选择 CABLE Output。需先安装 VB-CABLE。")
        hint.setWordWrap(True)
        form.addRow("虚拟麦克风", hint)
        self.auto_transcribe = QCheckBox("停止后自动进行离线转写")
        self.auto_transcribe.setChecked(True)
        self.auto_transcribe.toggled.connect(lambda _: self.update_controls())
        form.addRow("转写", self.auto_transcribe)
        self.live_transcribe = QCheckBox("边说边转写（短暂停顿或约 3 秒出一段文字）")
        self.live_transcribe.toggled.connect(lambda _: self.update_controls())
        form.addRow("实时识别", self.live_transcribe)
        self.type_current = QCheckBox("将实时文字输入到当前窗口")
        form.addRow("直接输入", self.type_current)
        self.input_status = QLabel("启用后，将光标放到输入框，再按录音卡按键开始说话。")
        self.input_status.setWordWrap(True)
        form.addRow("识别 / 输入状态", self.input_status)
        self.model_path = QLineEdit(str(models))
        self.model_path.setReadOnly(True)
        choose = QPushButton("选择模型目录")
        choose.clicked.connect(self.choose_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_path)
        model_row.addWidget(choose)
        form.addRow("离线模型", model_row)
        layout.addLayout(form)
        self.record_button = QPushButton("开始录音")
        self.record_button.setMinimumHeight(48)
        self.record_button.setStyleSheet("background:#2563eb;color:white;font-size:17px")
        self.record_button.clicked.connect(self.toggle_record)
        layout.addWidget(self.record_button)
        row = QHBoxLayout()
        self.refresh_button = QPushButton("刷新设备信息")
        self.refresh_button.clicked.connect(lambda: self.job("refresh"))
        self.retry_button = QPushButton("重新转写录音文件")
        self.retry_button.clicked.connect(self.retry)
        hide = QPushButton("进入后台")
        hide.clicked.connect(self.background)
        row.addWidget(self.refresh_button)
        row.addWidget(self.retry_button)
        row.addWidget(hide)
        layout.addLayout(row)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("识别文字显示在这里，可编辑后复制。开启实时转写可边说边显示。")
        layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        copy = QPushButton("复制全部文字")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        folder = QPushButton("打开录音目录")
        folder.clicked.connect(self.open_recordings)
        row.addWidget(copy)
        row.addWidget(folder)
        layout.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(150)
        self.log.setMaximumHeight(90)
        layout.addWidget(self.log)
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

    def update_controls(self):
        idle = not self.busy and not self.processing and not self.quitting
        self.scan_button.setEnabled(idle and not self.authorized)
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
        self.key.clear()
        self.job("connect", self.devices[index], key)

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

    def process_capture(self, path, force_transcribe=False):
        self.pending_key_start = self.pending_key_stop = False
        self.last_capture = Path(path)
        self.recording = False
        self.processing = True
        if not self.live_session or force_transcribe:
            self.text.clear()
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
        if event == "warning":
            self.log.appendPlainText(str(data))
        elif event == "output_status":
            self.output_status.setText(str(data))
            self.log.appendPlainText(str(data))
        elif event == "key_event" and data in (1, 3):
            self.handle_hardware_key(data)
        elif event == "authorized":
            self.authorized = True
            self.tray.setToolTip(f"VoiceHub · {data} 已连接")
        elif event == "disconnected":
            self.pending_key_start = self.pending_key_stop = False
            self.authorized = False
            self.recording = False
            self.info.setText("电量 —    增益 —    容量 —    固件 —")
            self.status.setText("蓝牙已断开，请重新扫描连接")
            self.tray.setToolTip("VoiceHub · 已断开")
        elif event == "info":
            battery = data.get("battery")
            battery_text = (
                "充电中"
                if battery == 110
                else (f"{battery}%" if battery is not None and battery <= 100 else "未知")
            )
            capacity = data.get("capacity")
            storage = (
                (f"{capacity[0] / 1024:.1f} / {capacity[1] / 1024:.1f} MiB（按 1KiB/单位估算）")
                if capacity
                else "未知"
            )
            self.info.setText(
                f"电量 {battery_text}    增益 "
                f"{ {1: '低', 2: '中', 3: '高'}.get(data.get('gain'), '未知') }\n"
                f"剩余/总容量 {storage}    固件 {data.get('firmware', '未知')}"
            )
        elif event == "recording":
            self.pending_key_start = False
            self.recording = True
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
            self.text.appendPlainText(str(data))
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
            self.devices = [device for device, _ in result]
            for device, adv in result:
                self.device_list.addItem(
                    f"{adv.local_name or device.name}   {device.address}   {adv.rssi} dBm"
                )
            if result:
                self.device_list.setCurrentRow(0)
            self.status.setText(
                f"找到 {len(result)} 台 CAC10，请选择并输入 AuthKey"
                if result
                else "未找到 CAC10，请确认设备已开机且未被其他客户端占用"
            )
        elif name == "connect":
            self.status.setText("鉴权成功，设备信息已读取；可从托盘开始录音")
            self.background()
        elif name == "stop":
            # A device-stopped notification may already have scheduled this transcript.
            if Path(result) != self.last_capture:
                self.process_capture(result)
        elif name == "transcribe":
            self.processing = False
            self.text.setPlainText(result)
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
        self.pending_key_start = self.pending_key_stop = False
        if name in ("transcribe", "finalize", "live_finalize"):
            self.processing = False
        else:
            self.busy = False
            self.active_job = None
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
