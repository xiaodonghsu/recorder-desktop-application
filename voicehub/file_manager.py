"""Recording card and local transcript management panel."""

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .audio import check_models
from .library import Library


class FileManager(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.entries = []
        self.current_id = None
        self.loading = False
        self.setWindowTitle("录音卡文件管理")
        self.resize(920, 720)
        self.settings = QSettings("VoiceHub", "Desktop")
        self.root = Path(self.settings.value("sounds_directory", str(Path.cwd() / "Sounds")))
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("本地保存目录"))
        self.directory = QLineEdit(str(self.root))
        self.directory.setReadOnly(True)
        row.addWidget(self.directory, 1)
        self.choose_button = QPushButton("选择目录")
        self.choose_button.clicked.connect(self.choose_directory)
        row.addWidget(self.choose_button)
        folder = QPushButton("打开目录")
        folder.clicked.connect(self.open_directory)
        row.addWidget(folder)
        layout.addLayout(row)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)
        sources = QWidget()
        source_layout = QVBoxLayout(sources)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self.source = QComboBox()
        self.source.addItems(["本地录音", "卡端文件", "原始录音恢复"])
        source_layout.addWidget(self.source)
        self.tabs = QStackedWidget()
        source_layout.addWidget(self.tabs)
        splitter.addWidget(sources)
        self.card_list, self.local_list = QListWidget(), QListWidget()
        for widget in (self.card_list, self.local_list):
            widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            widget.itemSelectionChanged.connect(self.update_controls)
        self.card_buttons = []
        self.local_buttons = []
        for title, widget, actions, buttons in (
            (
                "录音卡文件",
                self.card_list,
                [
                    ("读取文件列表", self.refresh_card),
                    ("保存所选到本地", self.download),
                    ("下载并 ASR", self.download_asr),
                    ("删除卡端所选文件", self.delete_card),
                ],
                self.card_buttons,
            ),
            (
                "本地录音与转写",
                self.local_list,
                [
                    ("刷新本地", self.refresh_local),
                    ("ASR 所选文件", self.asr),
                    ("删除本地所选文件", self.delete_local),
                ],
                self.local_buttons,
            ),
        ):
            page = QWidget()
            box = QVBoxLayout(page)
            box.addWidget(widget)
            row = QGridLayout()
            for index, (label, handler) in enumerate(actions):
                button = QPushButton(label)
                button.clicked.connect(handler)
                row.addWidget(button, index // 2, index % 2)
                buttons.append(button)
            box.addLayout(row)
            self.tabs.addWidget(page)
        recovery = QWidget()
        recovery_layout = QVBoxLayout(recovery)
        hint = QLabel("选择实时录音保存的 .vhrec 文件，使用当前模型和分包设置重新解码、转写。")
        hint.setWordWrap(True)
        recovery_layout.addWidget(hint)
        self.retry_button = QPushButton("选择原始录音重新转写")
        self.retry_button.clicked.connect(self.owner.retry)
        recovery_layout.addWidget(self.retry_button)
        folder = QPushButton("打开实时录音目录")
        folder.clicked.connect(self.owner.open_recordings)
        recovery_layout.addWidget(folder)
        recovery_layout.addStretch()
        self.tabs.addWidget(recovery)
        self.tabs.setCurrentIndex(1)
        self.details = QStackedWidget()
        splitter.addWidget(self.details)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.details.addWidget(detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([440, 440])
        self.selection = QLabel("选择本地录音查看转写与解析")
        self.selection.setWordWrap(True)
        detail_layout.addWidget(self.selection)
        detail_layout.addWidget(QLabel("转写文本 · 修改自动保存"))
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("先下载录音并执行 ASR；已有转写可直接编辑。")
        self.editor.textChanged.connect(self.save_text)
        detail_layout.addWidget(self.editor, 3)
        row = QHBoxLayout()
        copy = QPushButton("复制文本")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.editor.toPlainText()))
        self.export_button = QPushButton("导出解析输入 JSON")
        self.export_button.clicked.connect(self.export)
        row.addWidget(copy)
        row.addWidget(self.export_button)
        detail_layout.addLayout(row)
        detail_layout.addWidget(QLabel("已有解析结果"))
        self.analysis = QPlainTextEdit()
        self.analysis.setReadOnly(True)
        self.analysis.setPlaceholderText(
            "暂无解析结果。当前支持导出解析输入 JSON，尚未接入自动总结模型。"
        )
        detail_layout.addWidget(self.analysis, 1)
        recovery_detail = QWidget()
        recovery_detail_layout = QVBoxLayout(recovery_detail)
        recovery_detail_layout.setContentsMargins(0, 0, 0, 0)
        self.recovery_title = QLabel("原始录音转写结果")
        self.recovery_title.setWordWrap(True)
        recovery_detail_layout.addWidget(self.recovery_title)
        self.recovery_text = QPlainTextEdit()
        self.recovery_text.setPlaceholderText(
            "选择 .vhrec 文件后，转写结果显示在这里，可编辑、复制。"
        )
        recovery_detail_layout.addWidget(self.recovery_text, 1)
        copy_recovery = QPushButton("复制转写结果")
        copy_recovery.clicked.connect(
            lambda: QApplication.clipboard().setText(self.recovery_text.toPlainText())
        )
        recovery_detail_layout.addWidget(copy_recovery)
        self.details.addWidget(recovery_detail)
        self.source.currentIndexChanged.connect(self.select_source)
        self.status = QLabel(
            "本地录音无需连接设备；卡端操作需先连接并验证。支持 Ctrl / Shift 多选。"
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.activity_hint = QLabel()
        self.activity_hint.setWordWrap(True)
        layout.addWidget(self.activity_hint)
        self.local_list.currentItemChanged.connect(self.show_text)
        self.refresh_local()

    def select_source(self, index):
        self.tabs.setCurrentIndex((1, 0, 2)[index])
        self.details.setCurrentIndex(1 if index == 2 else 0)

    @property
    def library(self):
        return Library(self.root)

    def selected_card(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.card_list.selectedItems()]

    def selected_local(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.local_list.selectedItems()]

    def available(self):
        owner = self.owner
        return not (owner.busy or owner.processing or owner.recording or owner.quitting)

    def update_controls(self):
        idle = self.available()
        card = idle and self.owner.authorized
        for i, button in enumerate(self.card_buttons):
            button.setEnabled(card and (i == 0 or bool(self.card_list.selectedItems())))
        for i, button in enumerate(self.local_buttons):
            button.setEnabled(idle and (i == 0 or bool(self.local_list.selectedItems())))
        self.retry_button.setEnabled(idle)
        self.choose_button.setEnabled(idle)
        self.editor.setReadOnly(not idle or self.current_id is None)
        self.export_button.setEnabled(idle and self.current_id is not None)
        if self.owner.recording:
            self.activity_hint.setText("正在录音：可浏览文件，停止录音后可下载、删除或转写。")
        elif not idle:
            self.activity_hint.setText("任务处理中：请等待当前操作完成后再修改或处理其他文件。")
        else:
            self.activity_hint.setText("")

    def run_job(self, name, *args):
        if not self.available():
            return
        self.status.setText("正在处理所选文件…")
        self.owner.status.setText("正在处理文件，可切换页面查看其他内容…")
        self.owner.job(name, *args)
        self.update_controls()

    def refresh_card(self):
        self.run_job("file_list")

    def set_card(self, entries):
        self.entries = entries
        self.card_list.clear()
        for entry in entries:
            item = QListWidgetItem(
                f"{entry['name']}    {entry['duration']} 秒    {entry['size']:,} 字节"
            )
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.card_list.addItem(item)
        self.status.setText(f"录音卡列表读取完成：{len(entries)} 个文件")
        self.update_controls()

    def download(self):
        self.run_job("file_download", self.root, self.selected_card())

    def models_ready(self):
        try:
            check_models(self.owner.models)
            return True
        except FileNotFoundError as exc:
            QMessageBox.warning(self, "请准备离线模型", str(exc))
            return False

    def download_asr(self):
        if self.models_ready():
            self.run_job("file_download_asr", self.root, self.selected_card(), self.owner.models)

    def asr(self):
        if not self.models_ready():
            return
        ids = self.selected_local()
        if any(self.library.read(key).get("transcript") for key in ids) and (
            QMessageBox.question(
                self, "重新转写", "重新 ASR 会替换所选文件已有的转写文本，是否继续？"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.run_job("file_asr", self.root, ids, self.owner.models)

    def delete_card(self):
        entries = self.selected_card()
        if (
            QMessageBox.question(
                self, "删除卡端文件", f"永久删除录音卡上的 {len(entries)} 个文件？本地副本保留。"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.run_job("file_delete", entries)

    def delete_local(self):
        ids = self.selected_local()
        if (
            QMessageBox.question(
                self,
                "删除本地文件",
                f"删除本地所选 {len(ids)} 个录音及其转写、分析数据？卡端文件保留。",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            for key in ids:
                self.library.delete(key)
            self.status.setText(f"已删除 {len(ids)} 个本地录音")
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
        self.refresh_local()

    def refresh_local(self):
        previous = self.current_id
        self.current_id = None
        self.local_list.clear()
        for record in self.library.records():
            transcript = record.get("transcript")
            state = "已转写" if transcript is not None else "未转写"
            if record.get("asr", {}).get("status") == "failed":
                state += " · 上次 ASR 失败"
            if record["download_status"] != "complete":
                state = "下载未完成"
            item = QListWidgetItem(
                f"{record['source']['name']}    {state}    {record['created_at']}"
            )
            item.setData(Qt.ItemDataRole.UserRole, record["id"])
            self.local_list.addItem(item)
            if record["id"] == previous:
                self.local_list.setCurrentItem(item)
        self.show_text(self.local_list.currentItem(), None)
        self.update_controls()

    def show_text(self, item, previous):
        self.loading = True
        self.current_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        try:
            record = self.library.read(self.current_id) if self.current_id else {}
            self.selection.setText(
                record.get("source", {}).get("name", "选择本地录音查看转写与解析")
            )
            self.editor.setPlainText((record.get("transcript") or {}).get("text", ""))
            self.analysis.setPlainText(
                json.dumps(record["analyses"], ensure_ascii=False, indent=2)
                if record.get("analyses")
                else ""
            )
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            self.current_id = None
            self.editor.clear()
            self.analysis.clear()
        finally:
            self.loading = False
        self.update_controls()

    def save_text(self):
        if self.loading or not self.current_id:
            return
        try:
            self.library.save_transcript(self.current_id, self.editor.toPlainText(), edited=True)
            self.status.setText("文本修改已保存")
        except (OSError, ValueError) as exc:
            self.status.setText(f"文本保存失败：{exc}")

    def choose_directory(self):
        selected = QFileDialog.getExistingDirectory(self, "选择录音卡文件保存目录", str(self.root))
        if selected:
            self.root = Path(selected).resolve()
            self.settings.setValue("sounds_directory", str(self.root))
            self.directory.setText(str(self.root))
            self.current_id = None
            self.refresh_local()

    def open_directory(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root)))
        except OSError as exc:
            self.status.setText(str(exc))

    def export(self):
        try:
            data = self.library.analysis_input(self.current_id)
            selected, _ = QFileDialog.getSaveFileName(
                self,
                "导出模型输入",
                str(self.library.folder(self.current_id) / "analysis-input.json"),
                "JSON (*.json)",
            )
            if selected:
                Path(selected).write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self.status.setText(f"已导出：{selected}")
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
