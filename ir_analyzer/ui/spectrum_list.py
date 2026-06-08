"""
spectrum_list.py - 왼쪽 사이드바: 스펙트럼 파일 목록 관리
드래그 앤 드롭으로 여러 파일 추가 지원
"""

import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QLabel, QFileDialog, QAbstractItemView,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QApplication,
    QScrollArea, QButtonGroup, QFrame, QMenu
)
from PyQt5.QtCore import pyqtSignal, Qt, QSize
from PyQt5.QtGui import QColor, QIcon, QFont, QKeySequence, QCursor

from core.loader import load_spectrum


@dataclass
class SpectrumEntry:
    filepath: str
    name: str
    wavenumber: np.ndarray
    absorbance: np.ndarray
    color: str = "#89b4fa"
    fit_done: bool = False
    original_name: str = ""
    source_session_label: str = ""
    source_session_path: str = ""
    source_spectrum_path: str = ""


# 스펙트럼별 색상 순환
SPECTRUM_COLORS = [
    "#89b4fa",  # blue
    "#a6e3a1",  # green
    "#fab387",  # peach
    "#f38ba8",  # red
    "#cba6f7",  # mauve
    "#94e2d5",  # teal
    "#f9e2af",  # yellow
    "#89dceb",  # sky
]


class _InnerListWidget(QListWidget):
    """키보드 단축키와 내부 드래그 재정렬을 처리하는 스펙트럼 리스트"""
    delete_requested = pyqtSignal()
    order_changed = pyqtSignal()
    files_dropped = pyqtSignal(list, list)  # (spectrum_paths, session_paths)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos = None
        self._press_row = -1
        self._manual_dragging = False
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragDropOverwriteMode(False)

    def keyPressEvent(self, event):
        # Cmd+A (macOS) / Ctrl+A (Windows·Linux) → 전체 선택
        if event.key() == Qt.Key_A and event.modifiers() & Qt.ControlModifier:
            self.selectAll()
            return
        # Del 또는 Cmd+Backspace → 선택 항목 삭제
        if event.key() == Qt.Key_Delete:
            self.delete_requested.emit()
            return
        if event.key() == Qt.Key_Backspace and event.modifiers() & Qt.ControlModifier:
            self.delete_requested.emit()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            spectrum_paths = []
            session_paths = []
            for url in event.mimeData().urls():
                fp = url.toLocalFile()
                lower = fp.lower()
                if lower.endswith('.irsession'):
                    session_paths.append(fp)
                elif lower.endswith(('.csv', '.txt', '.asc', '.dpt')):
                    spectrum_paths.append(fp)
            if spectrum_paths or session_paths:
                self.files_dropped.emit(spectrum_paths, session_paths)
                event.acceptProposedAction()
                return

        super().dropEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
            item = self.itemAt(event.pos())
            self._press_row = self.row(item) if item is not None else -1
            self._manual_dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self._press_pos is not None
            and self._press_row >= 0
            and event.buttons() & Qt.LeftButton
        ):
            distance = (event.pos() - self._press_pos).manhattanLength()
            if distance >= QApplication.startDragDistance():
                self._manual_dragging = True
                self.setCursor(QCursor(Qt.ClosedHandCursor))
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._manual_dragging:
            source_row = self._press_row
            target_row = self._drop_row_from_pos(event.pos())
            self._reset_manual_drag_state()

            if source_row >= 0 and target_row >= 0:
                if target_row > source_row:
                    target_row -= 1
                if target_row != source_row:
                    item = self.takeItem(source_row)
                    self.insertItem(target_row, item)
                    self.setCurrentItem(item)
                    item.setSelected(True)
                    self.order_changed.emit()
            event.accept()
            return

        self._reset_manual_drag_state()
        super().mouseReleaseEvent(event)

    def _drop_row_from_pos(self, pos) -> int:
        item = self.itemAt(pos)
        if item is None:
            visible_rows = [
                row for row in range(self.count())
                if not self.item(row).isHidden()
            ]
            if not visible_rows:
                return -1
            return visible_rows[-1] + 1 if pos.y() >= 0 else visible_rows[0]

        row = self.row(item)
        rect = self.visualItemRect(item)
        if pos.y() > rect.center().y():
            row += 1
        return row

    def _reset_manual_drag_state(self):
        self._press_pos = None
        self._press_row = -1
        self._manual_dragging = False
        self.unsetCursor()


class _SessionFilterButton(QPushButton):
    """Session tab button with a reliable right-click context signal."""
    context_requested = pyqtSignal(object)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.context_requested.emit(event.globalPos())
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        self.context_requested.emit(event.globalPos())
        event.accept()


class SpectrumListWidget(QWidget):
    """파일 목록 사이드바 — 드래그 앤 드롭으로 CSV 추가"""
    LOOSE_FILES_KEY = "__loose__"

    spectrum_selected = pyqtSignal(object)      # SpectrumEntry
    selection_changed = pyqtSignal(list)        # list[SpectrumEntry]
    spectrum_added    = pyqtSignal(object)      # SpectrumEntry (newly added)
    spectra_added     = pyqtSignal(list)        # list[SpectrumEntry] added in one batch
    spectrum_removed  = pyqtSignal(int, str, str)    # (index, filepath, name)
    spectra_reordered = pyqtSignal()
    session_dropped   = pyqtSignal(list)             # list[irsession filepath]
    potential_assignments_changed = pyqtSignal()
    session_filter_changed = pyqtSignal(str)
    workspace_created = pyqtSignal(str)
    session_save_requested = pyqtSignal(str)
    session_close_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAcceptDrops(True)
        self.setMinimumWidth(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._entries: list[SpectrumEntry] = []
        self._potentials: dict[str, float] = {}
        self._session_filter = self.LOOSE_FILES_KEY
        self._last_selected_paths: dict[str, str] = {}
        self._session_buttons: dict[str, QPushButton] = {}
        self._workspace_keys: list[str] = []
        self._bulk_update_depth = 0
        self._bulk_filter_refresh_pending = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(6)

        # 헤더
        header = QLabel("SPECTRA")
        header.setObjectName("section_label")
        layout.addWidget(header)

        self._session_filter_scroll = QScrollArea()
        self._session_filter_scroll.setWidgetResizable(True)
        self._session_filter_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._session_filter_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._session_filter_scroll.setFrameShape(QFrame.NoFrame)
        self._session_filter_scroll.setFixedHeight(34)

        self._session_filter_host = QWidget()
        self._session_filter_layout = QHBoxLayout(self._session_filter_host)
        self._session_filter_layout.setContentsMargins(0, 0, 0, 0)
        self._session_filter_layout.setSpacing(4)
        self._session_filter_scroll.setWidget(self._session_filter_host)
        layout.addWidget(self._session_filter_scroll)

        self._session_button_group = QButtonGroup(self)
        self._session_button_group.setExclusive(True)

        # 버튼 행
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        self.btn_add = QPushButton("+ Add")
        self.btn_add.setObjectName("btn_flat")
        self.btn_add.setToolTip("CSV 파일 추가 (Ctrl+O)")
        self.btn_add.clicked.connect(self._add_files_dialog)

        self.btn_remove = QPushButton("✕ Remove")
        self.btn_remove.setObjectName("btn_flat")
        self.btn_remove.setToolTip("선택된 스펙트럼 제거")
        self.btn_remove.clicked.connect(self._remove_selected)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # 파일 목록
        self.list_widget = _InnerListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.delete_requested.connect(self._remove_selected)
        self.list_widget.order_changed.connect(self._on_list_order_changed)
        self.list_widget.files_dropped.connect(self._on_list_files_dropped)
        layout.addWidget(self.list_widget, 1)

        # 드롭 힌트 레이블
        self.hint_label = QLabel("CSV 파일을 여기에\n드래그하세요")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setStyleSheet("color: #45475a; font-size: 12px;")
        layout.addWidget(self.hint_label)

        pot_header = QLabel("POTENTIAL ASSIGNMENTS")
        pot_header.setObjectName("section_label")
        layout.addWidget(pot_header)

        self.potential_table = QTableWidget(0, 2)
        self.potential_table.setObjectName("spectra_potential_table")
        self.potential_table.setHorizontalHeaderLabels(["Potential (V)", "Spectrum"])
        self.potential_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.potential_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.potential_table.verticalHeader().setVisible(False)
        self.potential_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.potential_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.potential_table.setFixedHeight(170)
        self.potential_table.itemChanged.connect(self._on_potential_table_changed)
        self.potential_table.cellClicked.connect(self._on_potential_row_clicked)
        layout.addWidget(self.potential_table)

        self._rebuild_session_filter_buttons()

    def _session_key_for_entry(self, entry: SpectrumEntry) -> str:
        label = (entry.source_session_label or "").strip()
        return label if label else self.LOOSE_FILES_KEY

    def _session_label_for_key(self, session_key: str) -> str:
        if session_key == self.LOOSE_FILES_KEY:
            return "Loose Files"
        return session_key

    def _session_keys_in_order(self) -> list[str]:
        keys = list(self._workspace_keys)
        for entry in self._entries:
            key = self._session_key_for_entry(entry)
            if key not in keys:
                keys.append(key)
        return keys

    def get_session_keys(self) -> list[str]:
        return self._session_keys_in_order()

    def _make_unique_workspace_key(self) -> str:
        existing = set(self._session_keys_in_order())
        n = 1
        while True:
            candidate = f"Workspace {n}"
            if candidate not in existing:
                return candidate
            n += 1

    def create_workspace(self) -> str:
        session_key = self._make_unique_workspace_key()
        self._workspace_keys.append(session_key)
        self._rebuild_session_filter_buttons()
        self.set_session_filter(session_key)
        self.workspace_created.emit(session_key)
        return session_key

    def _entry_matches_filter(self, entry: SpectrumEntry, session_key: Optional[str] = None) -> bool:
        target_key = self._session_filter if session_key is None else session_key
        return self._session_key_for_entry(entry) == target_key

    def _rebuild_session_filter_buttons(self):
        if self._bulk_update_depth > 0:
            self._bulk_filter_refresh_pending = True
            return

        while self._session_filter_layout.count():
            item = self._session_filter_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                self._session_button_group.removeButton(widget)
                widget.deleteLater()

        self._session_buttons.clear()
        session_keys = self._session_keys_in_order()
        if self._session_filter not in session_keys:
            self._session_filter = session_keys[0] if session_keys else self.LOOSE_FILES_KEY

        for session_key in session_keys:
            btn = _SessionFilterButton(self._session_label_for_key(session_key))
            btn.setObjectName("btn_flat")
            btn.setCheckable(True)
            btn.setChecked(session_key == self._session_filter)
            btn.clicked.connect(lambda checked, key=session_key: checked and self.set_session_filter(key))
            btn.context_requested.connect(
                lambda global_pos, key=session_key:
                self._show_session_context_menu(key, global_pos)
            )
            self._session_button_group.addButton(btn)
            self._session_filter_layout.addWidget(btn)
            self._session_buttons[session_key] = btn

        btn_new = QPushButton("+")
        btn_new.setObjectName("btn_flat")
        btn_new.setFixedWidth(28)
        btn_new.setToolTip("New workspace")
        btn_new.clicked.connect(self.create_workspace)
        self._session_filter_layout.addWidget(btn_new)

        self._session_filter_layout.addStretch(1)
        self._session_filter_scroll.setVisible(True)
        self._apply_filter_views(emit_signal=False)

    def _show_session_context_menu(self, session_key: str, global_pos):
        menu = QMenu(self)
        save_action = menu.addAction("Save This Session As…")
        close_action = menu.addAction("Close This Session")
        action = menu.exec_(global_pos)
        if action == save_action:
            self.session_save_requested.emit(session_key)
        elif action == close_action:
            self.session_close_requested.emit(session_key)

    def begin_bulk_update(self):
        """Defer expensive list/table refreshes while many spectra are added."""
        self._bulk_update_depth += 1
        if self._bulk_update_depth == 1:
            self.setUpdatesEnabled(False)
            self.list_widget.setUpdatesEnabled(False)
            self.potential_table.setUpdatesEnabled(False)

    def end_bulk_update(self):
        if self._bulk_update_depth <= 0:
            return
        self._bulk_update_depth -= 1
        if self._bulk_update_depth > 0:
            return

        try:
            if self._bulk_filter_refresh_pending:
                self._bulk_filter_refresh_pending = False
                self._rebuild_session_filter_buttons()
        finally:
            self.potential_table.setUpdatesEnabled(True)
            self.list_widget.setUpdatesEnabled(True)
            self.setUpdatesEnabled(True)

    def set_session_filter(self, session_key: str, emit_signal: bool = True):
        session_keys = self._session_keys_in_order()
        if session_key not in session_keys:
            session_key = session_keys[0] if session_keys else self.LOOSE_FILES_KEY
        if self._session_filter == session_key:
            if emit_signal:
                self.session_filter_changed.emit(session_key)
            return

        self._remember_current_selection()
        self._session_filter = session_key
        btn = self._session_buttons.get(session_key)
        if btn is not None:
            btn.setChecked(True)
        self._apply_filter_views(emit_signal=emit_signal)

    def get_current_session_filter(self) -> str:
        return self._session_filter

    def get_session_key_for_entry(self, entry: SpectrumEntry) -> str:
        return self._session_key_for_entry(entry)

    def get_session_label_for_key(self, session_key: str) -> str:
        return self._session_label_for_key(session_key)

    def get_visible_entries(self) -> list[SpectrumEntry]:
        return [entry for entry in self._entries if self._entry_matches_filter(entry)]

    def entry_belongs_to_current_filter(self, entry: SpectrumEntry) -> bool:
        return self._entry_matches_filter(entry)

    def name_belongs_to_current_filter(self, spectrum_name: str) -> bool:
        entry = next((entry for entry in self._entries if entry.name == spectrum_name), None)
        return bool(entry) and self._entry_matches_filter(entry)

    def _apply_filter_views(self, emit_signal: bool = True):
        self._refresh_list_visibility()
        self._refresh_potential_table()
        if emit_signal:
            self.session_filter_changed.emit(self._session_filter)

    def _remember_current_selection(self, session_key: Optional[str] = None):
        current_row = self.list_widget.currentRow()
        if current_row < 0:
            return
        entry = self.get_entry(current_row)
        if entry is None:
            return
        key = session_key or self._session_key_for_entry(entry)
        self._last_selected_paths[key] = entry.filepath

    def _last_selected_visible_row(self, visible_rows: list[int]) -> int:
        last_path = self._last_selected_paths.get(self._session_filter)
        if not last_path:
            return -1
        for row in visible_rows:
            entry = self.get_entry(row)
            if entry is not None and entry.filepath == last_path:
                return row
        return -1

    def _refresh_list_visibility(self):
        visible_rows = []
        for row, entry in enumerate(self._entries):
            item = self.list_widget.item(row)
            is_visible = self._entry_matches_filter(entry)
            if item is not None:
                item.setHidden(not is_visible)
                if not is_visible and item.isSelected():
                    item.setSelected(False)
            if is_visible:
                visible_rows.append(row)

        current_row = self.list_widget.currentRow()
        if current_row >= 0:
            current_entry = self.get_entry(current_row)
            if current_entry is None or not self._entry_matches_filter(current_entry):
                current_row = -1

        if visible_rows:
            self.hint_label.setVisible(False)
            self.hint_label.setText("CSV 파일을 여기에\n드래그하세요")
            if current_row < 0:
                restore_row = self._last_selected_visible_row(visible_rows)
                self.list_widget.setCurrentRow(
                    restore_row if restore_row >= 0 else visible_rows[0])
        else:
            self.hint_label.setText(
                "CSV 파일을 여기에\n드래그하세요"
                if not self._entries else
                "현재 세션에 표시할\n스펙트럼이 없습니다"
            )
            self.hint_label.setVisible(True)
            self.list_widget.clearSelection()
            self.list_widget.setCurrentRow(-1)

    def _refresh_potential_table(self):
        visible_entries = self.get_visible_entries()
        self.potential_table.blockSignals(True)
        self.potential_table.setRowCount(0)

        for row, entry in enumerate(visible_entries):
            potential = self._potentials.get(entry.name)
            self.potential_table.insertRow(row)

            pot_text = "" if potential is None else f"{potential:.2f}"
            pot_item = QTableWidgetItem(pot_text)
            pot_item.setData(Qt.UserRole, entry.name)
            pot_item.setTextAlignment(Qt.AlignCenter)

            spec_item = QTableWidgetItem(entry.name)
            spec_item.setData(Qt.UserRole, entry.name)
            spec_item.setFlags(spec_item.flags() & ~Qt.ItemIsEditable)
            spec_item.setToolTip(entry.name)

            self.potential_table.setItem(row, 0, pot_item)
            self.potential_table.setItem(row, 1, spec_item)
            self.potential_table.setRowHeight(row, 28)

        self.potential_table.blockSignals(False)

        current_row = self.list_widget.currentRow()
        current_entry = self.get_entry(current_row) if current_row >= 0 else None
        if current_entry is not None:
            self._select_potential_row_for_name(current_entry.name)
        else:
            self.potential_table.clearSelection()

    def _select_potential_row_for_name(self, spectrum_name: str):
        self.potential_table.blockSignals(True)
        for row in range(self.potential_table.rowCount()):
            item = self.potential_table.item(row, 1)
            if item is not None and item.data(Qt.UserRole) == spectrum_name:
                self.potential_table.selectRow(row)
                self.potential_table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                self.potential_table.blockSignals(False)
                return
        self.potential_table.clearSelection()
        self.potential_table.blockSignals(False)

    # ── 파일 추가 ─────────────────────────────────────────────

    def _add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Open IR Spectra", "",
            "IR Spectra (*.csv *.txt *.asc *.dpt);;All Files (*)"
        )
        self.add_files(files)

    def add_files(self, filepaths: list[str]) -> list[SpectrumEntry]:
        """여러 raw spectrum 파일을 한 번에 추가하고 UI 갱신은 마지막에 수행."""
        added: list[SpectrumEntry] = []
        if not filepaths:
            return added

        self.begin_bulk_update()
        try:
            for filepath in filepaths:
                entry = self.add_file(filepath, select=False, emit_signal=False)
                if entry is not None:
                    added.append(entry)
        finally:
            self.end_bulk_update()

        if added:
            self.spectra_added.emit(added)
            last_entry = added[-1]
            if self._entry_matches_filter(last_entry):
                last_row = len(self._entries) - 1
                self.list_widget.setCurrentRow(last_row)
        return added

    def add_file(self, filepath: str, select: bool = True,
                 emit_signal: bool = True) -> Optional[SpectrumEntry]:
        """파일을 로드해서 목록에 추가. 중복 무시."""
        # macOS AppleDouble 메타데이터 파일 무시
        if Path(filepath).name.startswith('._'):
            return None
        if any(e.filepath == filepath for e in self._entries):
            return None
        try:
            wn, ab = load_spectrum(filepath)
        except Exception as e:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(None, "파일 로드 실패",
                                f"{Path(filepath).name}\n\n{e}")
            return None

        session_key = self._session_filter
        session_label = "" if session_key == self.LOOSE_FILES_KEY else self._session_label_for_key(session_key)
        original_name = Path(filepath).name
        base_name = f"{session_label} :: {original_name}" if session_label else original_name
        existing_names = {entry.name for entry in self._entries}
        display_name = base_name
        suffix = 2
        while display_name in existing_names:
            display_name = f"{base_name} [{suffix}]"
            suffix += 1

        color = SPECTRUM_COLORS[len(self._entries) % len(SPECTRUM_COLORS)]
        entry = SpectrumEntry(
            filepath=filepath,
            name=display_name,
            wavenumber=wn,
            absorbance=ab,
            color=color,
            original_name=original_name,
            source_session_label=session_label,
            source_spectrum_path=filepath,
        )
        self._entries.append(entry)
        self._add_list_item(entry)
        self._rebuild_session_filter_buttons()
        self.hint_label.setVisible(False)
        if emit_signal:
            self.spectrum_added.emit(entry)

        # 추가된 항목 자동 선택
        if select and self._entry_matches_filter(entry):
            self.list_widget.setCurrentRow(len(self._entries) - 1)
        return entry

    def _add_list_item(self, entry: SpectrumEntry):
        item = QListWidgetItem()
        item.setText(entry.name)
        item.setData(Qt.UserRole, entry.filepath)
        tooltip_lines = [entry.name]
        if entry.source_session_label:
            tooltip_lines.append(f"Session: {entry.source_session_label}")
        if entry.source_session_path:
            tooltip_lines.append(f"Session file: {entry.source_session_path}")
        tooltip_lines.append(f"Source: {entry.source_spectrum_path or entry.filepath}")
        item.setToolTip("\n".join(tooltip_lines))
        # 색상 점 표시
        item.setForeground(QColor(entry.color))
        self.list_widget.addItem(item)

    def _on_list_order_changed(self):
        entries_by_path = {entry.filepath: entry for entry in self._entries}
        reordered = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            filepath = item.data(Qt.UserRole) if item is not None else None
            entry = entries_by_path.get(filepath)
            if entry is not None:
                reordered.append(entry)

        if len(reordered) != len(self._entries):
            return

        selected_paths = {
            item.data(Qt.UserRole)
            for item in self.list_widget.selectedItems()
            if item is not None
        }
        current_item = self.list_widget.currentItem()
        current_path = current_item.data(Qt.UserRole) if current_item is not None else None

        self._entries = reordered
        self._refresh_potential_table()
        self._rebuild_session_filter_buttons()

        self.list_widget.blockSignals(True)
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            path = item.data(Qt.UserRole) if item is not None else None
            if item is not None:
                item.setSelected(path in selected_paths)
            if path == current_path:
                self.list_widget.setCurrentRow(row)
        self.list_widget.blockSignals(False)

        self._on_selection_changed()
        self.potential_assignments_changed.emit()
        self.spectra_reordered.emit()

    def _on_list_files_dropped(self, spectrum_paths: list, session_paths: list):
        if spectrum_paths:
            self.add_files(spectrum_paths)
        if session_paths:
            self.session_dropped.emit(session_paths)

    def _remove_selected(self):
        # 선택된 행을 높은 인덱스 순으로 처리 (인덱스 시프트 방지)
        selected_rows = sorted(
            {self.list_widget.row(item) for item in self.list_widget.selectedItems()},
            reverse=True
        )
        if not selected_rows:
            return
        for row in selected_rows:
            if 0 <= row < len(self._entries):
                entry = self._entries[row]
                self.list_widget.takeItem(row)
                self._entries.pop(row)
                self._remove_last_selection_for_entry(entry)
                self.spectrum_removed.emit(row, entry.filepath, entry.name)
        self._rebuild_session_filter_buttons()
        if not self._entries:
            self.hint_label.setVisible(True)

    def _remove_last_selection_for_entry(self, entry: SpectrumEntry):
        key = self._session_key_for_entry(entry)
        if self._last_selected_paths.get(key) == entry.filepath:
            self._last_selected_paths.pop(key, None)

    def remove_session(self, session_key: str) -> list[SpectrumEntry]:
        """Remove every spectrum belonging to one sidebar session filter."""
        if session_key in self._workspace_keys:
            self._workspace_keys.remove(session_key)
        self._last_selected_paths.pop(session_key, None)
        rows = [
            row for row, entry in enumerate(self._entries)
            if self._session_key_for_entry(entry) == session_key
        ]
        removed: list[SpectrumEntry] = []
        for row in reversed(rows):
            entry = self._entries[row]
            self.list_widget.takeItem(row)
            self._entries.pop(row)
            removed.append(entry)
            self.spectrum_removed.emit(row, entry.filepath, entry.name)
        self._rebuild_session_filter_buttons()
        if not self._entries:
            self.hint_label.setVisible(True)
        return list(reversed(removed))

    def get_entry(self, index: int) -> Optional[SpectrumEntry]:
        if 0 <= index < len(self._entries):
            return self._entries[index]
        return None

    def mark_fit_done(self, index: int):
        if 0 <= index < len(self._entries):
            self._entries[index].fit_done = True
            item = self.list_widget.item(index)
            if item:
                item.setText(f"✓ {self._entries[index].name}")

    def clear_fit_done(self, index: int):
        if 0 <= index < len(self._entries):
            self._entries[index].fit_done = False
            item = self.list_widget.item(index)
            if item:
                item.setText(self._entries[index].name)

    def get_all_entries(self) -> list:
        """모든 SpectrumEntry 반환"""
        return list(self._entries)

    def get_selected_entries(self) -> list[SpectrumEntry]:
        rows = sorted({
            self.list_widget.row(item)
            for item in self.list_widget.selectedItems()
        })
        return [
            self._entries[row]
            for row in rows
            if 0 <= row < len(self._entries)
        ]

    def add_entry(self, entry: 'SpectrumEntry', select: bool = True,
                  emit_signal: bool = True) -> 'Optional[SpectrumEntry]':
        """
        Pre-built SpectrumEntry를 직접 추가 (세션 복원용).
        add_file() 과 달리 디스크에서 파일을 읽지 않음.
        """
        if any(e.filepath == entry.filepath for e in self._entries):
            return None
        if not entry.original_name:
            entry.original_name = entry.name
        if not entry.source_spectrum_path:
            entry.source_spectrum_path = entry.filepath
        self._entries.append(entry)
        self._add_list_item(entry)
        self._rebuild_session_filter_buttons()
        self.hint_label.setVisible(False)
        if emit_signal:
            self.spectrum_added.emit(entry)
        if select:
            if self._entry_matches_filter(entry):
                self.list_widget.setCurrentRow(len(self._entries) - 1)
        return entry

    def clear_all(self):
        """모든 스펙트럼 제거 (세션 복원 시 초기화용)"""
        self._entries.clear()
        self._potentials.clear()
        self._last_selected_paths.clear()
        self._workspace_keys.clear()
        self.list_widget.clear()
        self.potential_table.blockSignals(True)
        self.potential_table.setRowCount(0)
        self.potential_table.blockSignals(False)
        self._rebuild_session_filter_buttons()
        self.hint_label.setVisible(True)

    def _on_row_changed(self, row: int):
        if row >= 0 and row < len(self._entries):
            entry = self._entries[row]
            self._remember_current_selection()
            self._select_potential_row_for_name(entry.name)
            self.spectrum_selected.emit(entry)

    def _on_selection_changed(self):
        self.selection_changed.emit(self.get_selected_entries())

    # ── Potential Assignments ─────────────────────────────────

    def add_spectrum_potential(self, name: str, potential: float):
        """스펙트럼 추가 시 Potential 상태를 저장하고 현재 필터 뷰를 갱신"""
        self._potentials[name] = potential
        self._refresh_potential_table()
        self.potential_assignments_changed.emit()

    def remove_spectrum_potential(self, spectrum_name: str, emit_changed: bool = True):
        """스펙트럼 제거 시 Potential 상태도 함께 정리"""
        self._potentials.pop(spectrum_name, None)
        self._refresh_potential_table()
        if emit_changed:
            self.potential_assignments_changed.emit()

    def _on_potential_table_changed(self, item):
        if item is not None and item.column() == 0:
            spectrum_name = item.data(Qt.UserRole)
            if spectrum_name:
                try:
                    self._potentials[spectrum_name] = float(item.text())
                except ValueError:
                    pass
            self.potential_assignments_changed.emit()

    def _on_potential_row_clicked(self, row: int, column: int):
        item = self.potential_table.item(row, 1)
        spectrum_name = item.data(Qt.UserRole) if item is not None else None
        if not spectrum_name:
            return
        for idx, entry in enumerate(self._entries):
            if entry.name == spectrum_name:
                if self.list_widget.currentRow() != idx:
                    self.list_widget.setCurrentRow(idx)
                break

    def get_potentials(self, visible_only: bool = False) -> dict:
        """{spectrum_name: potential_V} 딕셔너리 반환"""
        if not visible_only:
            return dict(self._potentials)
        visible_names = {entry.name for entry in self.get_visible_entries()}
        return {
            name: potential
            for name, potential in self._potentials.items()
            if name in visible_names
        }

    def set_potentials(self, spectra_names: list, potentials: dict,
                       emit_changed: bool = True):
        """Potential 상태를 spectra_names 순서 기준으로 재설정"""
        ordered = {}
        for row, name in enumerate(spectra_names):
            ordered[name] = potentials.get(name, -0.1 * (row + 1))
        self._potentials = ordered
        if self._bulk_update_depth > 0:
            self._bulk_filter_refresh_pending = True
        else:
            self._refresh_potential_table()
        if emit_changed:
            self.potential_assignments_changed.emit()

    # ── 드래그 앤 드롭 ────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            valid = any(
                u.toLocalFile().lower().endswith(
                    ('.csv', '.txt', '.asc', '.dpt', '.irsession'))
                for u in event.mimeData().urls()
            )
            if valid:
                event.acceptProposedAction()
                self.setStyleSheet(self.styleSheet() +
                    "QWidget#sidebar { border: 2px dashed #89b4fa; }")
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        # 하이라이트 제거
        self.setStyleSheet("")

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.setStyleSheet("")
        session_paths = []
        spectrum_paths = []
        for url in event.mimeData().urls():
            fp = url.toLocalFile()
            if fp.lower().endswith('.irsession'):
                session_paths.append(fp)
            elif fp.lower().endswith(('.csv', '.txt', '.asc', '.dpt')):
                spectrum_paths.append(fp)
        if spectrum_paths:
            self.add_files(spectrum_paths)
        if session_paths:
            self.session_dropped.emit(session_paths)
        event.acceptProposedAction()
