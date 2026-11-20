"""Recording card and local transcript management dialog."""

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .audio import check_models
from .library import Library


class FileManager(QDialog):
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
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
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
            row = QHBoxLayout()
            for label, handler in actions:
                button = QPushButton(label)
                button.clicked.connect(handler)
                row.addWidget(button)
                buttons.append(button)
            box.addLayout(row)
            self.tabs.addTab(page, title)
        layout.addWidget(QLabel("转写文本（点击本地录音查看；修改自动保存）"))
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("先下载录音并执行 ASR；已有转写可直接编辑。")
        self.editor.textChanged.connect(self.save_text)
        layout.addWidget(self.editor, 1)
        row = QHBoxLayout()
        copy = QPushButton("复制文本")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.editor.toPlainText()))
        self.export_button = QPushButton("导出总结 / 解析输入 JSON")
        self.export_button.clicked.connect(self.export)
        row.addWidget(copy)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        self.status = QLabel("连接并验证录音卡后，点击“读取文件列表”。支持 Ctrl / Shift 多选。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.local_list.currentItemChanged.connect(self.show_text)
        self.refresh_local()

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
        self.choose_button.setEnabled(idle)
        self.editor.setReadOnly(not idle or self.current_id is None)
        self.export_button.setEnabled(idle and self.current_id is not None)

    def run_job(self, name, *args):
        if not self.available():
            return
        self.status.setText("正在处理所选文件…")
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
            self.editor.setPlainText((record.get("transcript") or {}).get("text", ""))
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            self.current_id = None
            self.editor.clear()
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
