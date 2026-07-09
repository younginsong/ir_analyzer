"""
right_panel.py - 오른쪽 패널: Settings + Peaks + Summary
"""

from __future__ import annotations

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QPushButton, QDoubleSpinBox, QSpinBox, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QLabel, QCheckBox, QStackedWidget, QScrollArea
)
from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from core.peak_finder import PeakGuess
from core.fitter import FitResult

PEAK_COLORS = ['#89b4fa', '#fab387', '#cba6f7', '#94e2d5',
               '#f9e2af', '#a6e3a1', '#89dceb', '#f38ba8']
CO_ASSIGNMENTS = ["Unassigned", "CO_L", "CO_B", "OH bending", "Other"]


class _PeakTableWidget(QTableWidget):
    delete_pressed = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_pressed.emit()
            return
        super().keyPressEvent(event)


class RightPanel(QWidget):
    fit_requested              = pyqtSignal()
    auto_detect_requested      = pyqtSignal()
    auto_detect_co_b_requested = pyqtSignal()
    co_analyze_all_requested   = pyqtSignal()
    peak_params_changed   = pyqtSignal(list)          # list[PeakGuess]
    locks_changed         = pyqtSignal(list)          # list[dict]
    peaks_cleared         = pyqtSignal()
    peak_rows_deleted     = pyqtSignal(list)
    co_peak_params_changed = pyqtSignal(list)
    co_locks_changed       = pyqtSignal(list)
    co_peaks_cleared       = pyqtSignal()
    co_peak_rows_deleted   = pyqtSignal(list)
    co_peak_add_mode_toggled = pyqtSignal(bool)
    region_changed        = pyqtSignal(float, float)
    baseline_mode_toggled = pyqtSignal(bool)           # Edit Baseline ON/OFF
    baseline_apply        = pyqtSignal(str, dict)      # (algo, params)
    baseline_undo         = pyqtSignal()
    baseline_clear        = pyqtSignal()
    baseline_point_mode_changed = pyqtSignal(str)
    plot_auto_range       = pyqtSignal()
    plot_export           = pyqtSignal()
    plot_x_auto           = pyqtSignal(bool)
    plot_y_auto           = pyqtSignal(bool)
    export_requested      = pyqtSignal()
    batch_requested       = pyqtSignal()
    stark_calculate_requested = pyqtSignal()
    stark_plot_requested  = pyqtSignal(int)
    auto_fit_requested    = pyqtSignal()
    mode_changed          = pyqtSignal(str)            # 'Total' | 'OH' | 'CO' | 'SiO'
    total_view_changed    = pyqtSignal(str)            # 'overlay' | 'stack'
    total_shift_toggled   = pyqtSignal(bool)
    total_probe_toggled   = pyqtSignal(bool)
    total_reset_shifts    = pyqtSignal()
    oh_overlay_intensity_changed = pyqtSignal()
    snapshot_save_requested = pyqtSignal()
    snapshot_restore_requested = pyqtSignal(int)
    snapshot_delete_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(310)
        self._guesses: list[PeakGuess] = []
        self._co_guesses: list[PeakGuess] = []
        self._current_mode = 'OH'
        self._mode_regions = {
            'Total': (1500.0, 4000.0),
            'OH': (3000.0, 3990.0),
            'CO': (1400.0, 2230.0),
            'SiO': (1100.0, 1300.0),
        }
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── 분석 모드 선택 ─────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        self.btn_mode_total = QPushButton("Total")
        self.btn_mode_oh  = QPushButton("OH")
        self.btn_mode_co  = QPushButton("CO")
        self.btn_mode_sio = QPushButton("Si-O")
        for btn, mode in [(self.btn_mode_total, 'Total'),
                          (self.btn_mode_oh, 'OH'),
                          (self.btn_mode_co, 'CO'),
                          (self.btn_mode_sio, 'SiO')]:
            btn.setCheckable(True)
            btn.setObjectName("btn_flat")
            btn.clicked.connect(lambda _, m=mode: self._set_mode(m))
            mode_row.addWidget(btn)
        self.btn_mode_oh.setChecked(True)
        layout.addLayout(mode_row)

        layout.addWidget(self._build_settings_peaks_tab())

        # 하단 버튼 (Export / Batch)
        btn_row2 = QHBoxLayout()
        btn_export = QPushButton("↓ Export")
        btn_export.setObjectName("btn_flat")
        btn_export.clicked.connect(self.export_requested.emit)
        btn_batch = QPushButton("⚡ Batch")
        btn_batch.setObjectName("btn_flat")
        btn_batch.clicked.connect(self.batch_requested.emit)
        btn_row2.addWidget(btn_export)
        btn_row2.addWidget(btn_batch)

        layout.addLayout(btn_row2)

    # ── Settings & Peaks 탭 (통합) ───────────────────────────

    def _build_settings_peaks_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # 분석 영역
        region_grp = QGroupBox("Analysis Region")
        self._region_group = region_grp
        form = QFormLayout()
        form.setSpacing(6)

        self.spin_wn_min = QDoubleSpinBox()
        self.spin_wn_min.setRange(0, 10000)
        self.spin_wn_min.setValue(3000)
        self.spin_wn_min.setSuffix("  cm⁻¹")

        self.spin_wn_max = QDoubleSpinBox()
        self.spin_wn_max.setRange(0, 10000)
        self.spin_wn_max.setValue(3990)
        self.spin_wn_max.setSuffix("  cm⁻¹")

        self.spin_tolerance = QDoubleSpinBox()
        self.spin_tolerance.setRange(1, 200)
        self.spin_tolerance.setValue(30)
        self.spin_tolerance.setSuffix("  cm⁻¹")
        self.spin_tolerance.setToolTip("피크 중심 이동 허용 범위")

        form.addRow("Min:", self.spin_wn_min)
        form.addRow("Max:", self.spin_wn_max)
        form.addRow("Tolerance:", self.spin_tolerance)

        btn_apply = QPushButton("Apply")
        btn_apply.clicked.connect(
            lambda: self.region_changed.emit(
                self.spin_wn_min.value(), self.spin_wn_max.value()))
        form.addRow(btn_apply)
        region_grp.setLayout(form)

        # 베이스라인
        bl_grp = QGroupBox("Baseline")
        self._baseline_group = bl_grp
        bl_layout = QVBoxLayout()
        bl_layout.setSpacing(6)
        bl_layout.setContentsMargins(6, 6, 6, 6)

        # Edit Baseline 토글 버튼
        self.btn_edit_bl = QPushButton("Edit Baseline")
        self.btn_edit_bl.setCheckable(True)
        self.btn_edit_bl.setObjectName("btn_flat")
        self.btn_edit_bl.toggled.connect(self._on_bl_mode_toggled)
        bl_layout.addWidget(self.btn_edit_bl)

        # 서브 패널 (Edit 활성 시에만 표시)
        self._bl_panel = QWidget()
        bl_form = QFormLayout(self._bl_panel)
        bl_form.setSpacing(6)
        bl_form.setContentsMargins(0, 4, 0, 0)

        self.combo_bl_algo = QComboBox()
        self.combo_bl_algo.addItems(
            ["Manual", "Rubber Band", "ARPLS", "SNIP", "Linear"])
        self.combo_bl_algo.currentTextChanged.connect(self._on_bl_algo_changed)
        bl_form.addRow("Algorithm:", self.combo_bl_algo)

        self.combo_bl_point_mode = QComboBox()
        self.combo_bl_point_mode.addItems(["Follow Spectrum", "Free"])
        self.combo_bl_point_mode.currentTextChanged.connect(
            lambda txt: self.baseline_point_mode_changed.emit(
                "free" if txt == "Free" else "follow"
            )
        )
        bl_form.addRow("Point Mode:", self.combo_bl_point_mode)

        # ARPLS λ
        self.lbl_lam = QLabel("λ (smoothness):")
        self.spin_lam = QDoubleSpinBox()
        self.spin_lam.setRange(100, 1e8)
        self.spin_lam.setValue(1e4)
        self.spin_lam.setDecimals(0)
        self.spin_lam.setSingleStep(1000)
        self.spin_lam.valueChanged.connect(self._emit_bl_apply)
        bl_form.addRow(self.lbl_lam, self.spin_lam)

        # SNIP iterations
        self.lbl_iter = QLabel("Iterations:")
        self.spin_iter = QSpinBox()
        self.spin_iter.setRange(5, 300)
        self.spin_iter.setValue(50)
        self.spin_iter.valueChanged.connect(self._emit_bl_apply)
        bl_form.addRow(self.lbl_iter, self.spin_iter)

        # Manual 전용 Undo / Clear
        self._bl_manual_widget = QWidget()
        btn_row = QHBoxLayout(self._bl_manual_widget)
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(4)
        self.btn_bl_undo  = QPushButton("↩ Undo")
        self.btn_bl_clear = QPushButton("✕ Clear")
        self.btn_bl_undo.setObjectName("btn_flat")
        self.btn_bl_clear.setObjectName("btn_flat")
        self.btn_bl_undo.clicked.connect(self.baseline_undo.emit)
        self.btn_bl_clear.clicked.connect(self.baseline_clear.emit)
        btn_row.addWidget(self.btn_bl_undo)
        btn_row.addWidget(self.btn_bl_clear)
        bl_form.addRow(self._bl_manual_widget)

        self._bl_panel.setVisible(False)
        bl_layout.addWidget(self._bl_panel)
        bl_grp.setLayout(bl_layout)

        # 초기 파라미터 위젯 가시성 설정
        self._update_bl_param_visibility("Manual")

        # 플롯 뷰 컨트롤 (기존 우클릭 메뉴 대체)
        view_grp = QGroupBox("Plot View")
        view_layout = QVBoxLayout()
        view_layout.setSpacing(6)
        view_layout.setContentsMargins(6, 6, 6, 6)

        btn_auto_range = QPushButton("↺  View All")
        btn_auto_range.setObjectName("btn_flat")
        btn_auto_range.setToolTip("전체 데이터에 맞게 축 범위 자동 조정")
        btn_auto_range.clicked.connect(self.plot_auto_range.emit)

        btn_save_plot = QPushButton("💾  Save Plot…")
        btn_save_plot.setObjectName("btn_flat")
        btn_save_plot.setToolTip("플롯을 PNG 또는 SVG로 저장")
        btn_save_plot.clicked.connect(self.plot_export.emit)

        axis_row = QHBoxLayout()
        axis_row.setSpacing(4)
        self.cb_x_auto = QCheckBox("X Auto")
        self.cb_x_auto.setChecked(True)
        self.cb_y_auto = QCheckBox("Y Auto")
        self.cb_y_auto.setChecked(True)
        self.cb_x_auto.toggled.connect(self.plot_x_auto.emit)
        self.cb_y_auto.toggled.connect(self.plot_y_auto.emit)
        axis_row.addWidget(self.cb_x_auto)
        axis_row.addWidget(self.cb_y_auto)
        axis_row.addStretch()

        view_layout.addWidget(btn_auto_range)
        view_layout.addWidget(btn_save_plot)
        view_layout.addLayout(axis_row)
        view_grp.setLayout(view_layout)

        # 피크 (모드별 스택)
        self._peaks_stack = QStackedWidget()
        self._peaks_stack.addWidget(self._build_total_page())      # 0
        self._peaks_stack.addWidget(self._build_oh_peaks_page())   # 0
        self._peaks_stack.addWidget(self._build_co_peaks_page())   # 1
        self._peaks_stack.addWidget(self._build_sio_peaks_page())  # 2
        self._peaks_stack.setCurrentIndex(1)

        layout.addWidget(region_grp)
        layout.addWidget(bl_grp)
        layout.addWidget(view_grp)
        layout.addWidget(self._peaks_stack)
        layout.addStretch()

        # 세로 스크롤바가 생겨도 가로폭이 줄어들지 않도록 최소 폭 고정
        w.setMinimumWidth(295)
        scroll.setWidget(w)
        bl_grp.setVisible(self._current_mode == 'Total')
        return scroll

    # ── Baseline 내부 핸들러 ──────────────────────────────────

    def _on_bl_mode_toggled(self, checked: bool):
        if self._current_mode != 'Total':
            self._bl_panel.setVisible(False)
            return
        self._bl_panel.setVisible(checked)
        # 버튼 텍스트로 상태 표시
        self.btn_edit_bl.setText("▣ Editing Baseline…" if checked else "Edit Baseline")
        self.baseline_mode_toggled.emit(checked)

    def _on_bl_algo_changed(self, algo: str):
        self._update_bl_param_visibility(algo)
        self._emit_bl_apply()

    def _update_bl_param_visibility(self, algo: str):
        is_manual = (algo == "Manual")
        is_arpls   = (algo == "ARPLS")
        is_snip    = (algo == "SNIP")
        self._bl_manual_widget.setVisible(is_manual)
        self.combo_bl_point_mode.setVisible(is_manual)
        label = self._bl_panel.layout().labelForField(self.combo_bl_point_mode)
        if label is not None:
            label.setVisible(is_manual)
        self.lbl_lam.setVisible(is_arpls)
        self.spin_lam.setVisible(is_arpls)
        self.lbl_iter.setVisible(is_snip)
        self.spin_iter.setVisible(is_snip)

    def get_baseline_point_mode(self) -> str:
        return "free" if self.combo_bl_point_mode.currentText() == "Free" else "follow"

    def _emit_bl_apply(self):
        if not self.btn_edit_bl.isChecked():
            return
        algo = self.combo_bl_algo.currentText()
        params = {}
        if algo == "ARPLS":
            params['lam'] = self.spin_lam.value()
        elif algo == "SNIP":
            params['n_iter'] = self.spin_iter.value()
        self.baseline_apply.emit(algo, params)

    def _on_oh_overlay_intensity_changed(self):
        enabled = self.combo_oh_overlay_intensity.currentText() == "Normalize"
        self.spin_oh_norm_min.setEnabled(enabled)
        self.spin_oh_norm_max.setEnabled(enabled)
        if hasattr(self, 'cb_total_shift'):
            if enabled:
                self.cb_total_shift.setChecked(False)
            self.cb_total_shift.setEnabled(not enabled)
        self.oh_overlay_intensity_changed.emit()

    def _build_oh_peaks_page(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        cfg_grp = QGroupBox("Peak Config")
        form = QFormLayout()
        form.setSpacing(6)
        self.spin_n_peaks = QSpinBox()
        self.spin_n_peaks.setRange(1, 10)
        self.spin_n_peaks.setValue(4)
        form.addRow("# Peaks:", self.spin_n_peaks)
        cfg_grp.setLayout(form)
        layout.addWidget(cfg_grp)

        btn_row = QHBoxLayout()
        btn_auto = QPushButton("🔍 Auto Detect")
        btn_auto.setObjectName("btn_primary")
        btn_auto.clicked.connect(self.auto_detect_requested.emit)
        btn_clear_peaks = QPushButton("✕ Clear")
        btn_clear_peaks.setObjectName("btn_flat")
        btn_clear_peaks.clicked.connect(self._on_clear_peaks)
        btn_row.addWidget(btn_auto)
        btn_row.addWidget(btn_clear_peaks)
        layout.addLayout(btn_row)
        tip = QLabel("또는 플롯에서  ＋ Add Peak 클릭")
        tip.setStyleSheet("color: #6c7086; font-size: 11px;")
        layout.addWidget(tip)

        self.btn_fit = QPushButton("▶  Run Fit")
        self.btn_fit.setObjectName("btn_success")
        self.btn_fit.clicked.connect(self.fit_requested.emit)
        layout.addWidget(self.btn_fit)

        btn_auto_fit = QPushButton("⚡  Auto Fit")
        btn_auto_fit.setObjectName("btn_primary")
        btn_auto_fit.setToolTip(
            "첫 번째·마지막 스펙트럼 피팅 결과를 보간해\n"
            "중간 스펙트럼을 자동으로 피팅합니다.")
        btn_auto_fit.clicked.connect(self.auto_fit_requested.emit)
        layout.addWidget(btn_auto_fit)

        grp = QGroupBox("Initial Parameters")
        grp_layout = QVBoxLayout()
        lock_hint = QLabel("Lock columns freeze Center, Amp, or Sigma during fit.")
        lock_hint.setStyleSheet("color: #6c7086; font-size: 11px;")
        grp_layout.addWidget(lock_hint)
        self.init_table = _PeakTableWidget(0, 8)
        self.init_table.setHorizontalHeaderLabels(
            ["", "Shape", "Center", "Amp", "Sigma", "C", "A", "S"])
        hh = self.init_table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignCenter)
        hh.setFixedHeight(38)
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.Fixed)
        hh.setSectionResizeMode(6, QHeaderView.Fixed)
        hh.setSectionResizeMode(7, QHeaderView.Fixed)
        self.init_table.setColumnWidth(0, 24)
        self.init_table.setColumnWidth(1, 128)
        self.init_table.setColumnWidth(5, 44)
        self.init_table.setColumnWidth(6, 44)
        self.init_table.setColumnWidth(7, 44)
        self.init_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.init_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.init_table.verticalHeader().setVisible(False)
        self.init_table.verticalHeader().setDefaultSectionSize(40)
        self.init_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.init_table.itemChanged.connect(self._on_table_changed)
        self.init_table.delete_pressed.connect(self._delete_selected_peaks)
        lock_tips = {
            5: "Lock Center",
            6: "Lock Amplitude",
            7: "Lock Sigma",
        }
        for col, tip in lock_tips.items():
            self.init_table.setHorizontalHeaderItem(
                col, self._make_lock_header_item(self.init_table.horizontalHeaderItem(col).text(), tip)
            )
        grp_layout.addWidget(self.init_table)
        grp.setLayout(grp_layout)
        layout.addWidget(grp)

        snap_grp = QGroupBox("Snapshots")
        snap_layout = QVBoxLayout()
        snap_layout.setContentsMargins(6, 6, 6, 6)
        snap_layout.setSpacing(6)

        self.combo_snapshots = QComboBox()
        self.combo_snapshots.setObjectName("table_combo")
        self.combo_snapshots.setEnabled(False)
        snap_layout.addWidget(self.combo_snapshots)

        snap_btn_row = QHBoxLayout()
        self.btn_snapshot_save = QPushButton("Save")
        self.btn_snapshot_save.setObjectName("btn_flat")
        self.btn_snapshot_save.clicked.connect(self.snapshot_save_requested.emit)
        self.btn_snapshot_save.setEnabled(False)
        self.btn_snapshot_restore = QPushButton("Restore")
        self.btn_snapshot_restore.setObjectName("btn_flat")
        self.btn_snapshot_restore.clicked.connect(self._emit_snapshot_restore)
        self.btn_snapshot_delete = QPushButton("Delete")
        self.btn_snapshot_delete.setObjectName("btn_flat")
        self.btn_snapshot_delete.clicked.connect(self._emit_snapshot_delete)
        self.btn_snapshot_restore.setEnabled(False)
        self.btn_snapshot_delete.setEnabled(False)
        snap_btn_row.addWidget(self.btn_snapshot_save)
        snap_btn_row.addWidget(self.btn_snapshot_restore)
        snap_btn_row.addWidget(self.btn_snapshot_delete)
        snap_layout.addLayout(snap_btn_row)

        snap_tip = QLabel("현재 OH 상태를 저장하고 나중에 복원합니다.")
        snap_tip.setStyleSheet("color: #6c7086; font-size: 11px;")
        snap_layout.addWidget(snap_tip)
        snap_grp.setLayout(snap_layout)
        layout.addWidget(snap_grp)

        layout.addWidget(self._build_summary_group())
        layout.addStretch()
        return w

    def _build_total_page(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(10)

        info = QLabel(
            "왼쪽 목록에서 선택한 스펙트럼만 비교합니다.\n"
            "Baseline과 normalize는 선택된 스펙트럼에 적용됩니다."
        )
        info.setStyleSheet("color: #a6adc8; font-size: 11px;")
        layout.addWidget(info)

        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("View:"))
        self.combo_total_view = QComboBox()
        self.combo_total_view.addItems(["Overlay", "Stack"])
        self.combo_total_view.currentTextChanged.connect(
            lambda txt: self.total_view_changed.emit(txt.lower()))
        view_row.addWidget(self.combo_total_view)
        layout.addLayout(view_row)

        self.cb_total_shift = QCheckBox("Shift Mode")
        self.cb_total_shift.setToolTip("스펙트럼을 클릭 후 위/아래로 드래그해 y-shift를 조절")
        self.cb_total_shift.toggled.connect(self.total_shift_toggled.emit)
        layout.addWidget(self.cb_total_shift)

        self.cb_total_probe = QCheckBox("Coordinate Probe")
        self.cb_total_probe.setToolTip("마우스 위치의 (x, y) 좌표를 플롯 우측 상단에 표시")
        self.cb_total_probe.toggled.connect(self.total_probe_toggled.emit)
        layout.addWidget(self.cb_total_probe)

        btn_reset = QPushButton("Reset Shifts")
        btn_reset.setObjectName("btn_flat")
        btn_reset.clicked.connect(self.total_reset_shifts.emit)
        layout.addWidget(btn_reset)

        intensity_grp = QGroupBox("Overlay Intensity")
        intensity_layout = QVBoxLayout()
        intensity_layout.setContentsMargins(6, 6, 6, 6)
        intensity_layout.setSpacing(6)

        intensity_form = QFormLayout()
        intensity_form.setSpacing(6)
        self.combo_oh_overlay_intensity = QComboBox()
        self.combo_oh_overlay_intensity.addItems(["Original", "Normalize"])
        self.combo_oh_overlay_intensity.currentTextChanged.connect(
            lambda _txt: self._on_oh_overlay_intensity_changed())
        intensity_form.addRow("Mode:", self.combo_oh_overlay_intensity)

        self.spin_oh_norm_min = QDoubleSpinBox()
        self.spin_oh_norm_min.setRange(0, 10000)
        self.spin_oh_norm_min.setValue(3000)
        self.spin_oh_norm_min.setSuffix("  cm⁻¹")
        self.spin_oh_norm_min.valueChanged.connect(
            lambda _value: self.oh_overlay_intensity_changed.emit())

        self.spin_oh_norm_max = QDoubleSpinBox()
        self.spin_oh_norm_max.setRange(0, 10000)
        self.spin_oh_norm_max.setValue(3990)
        self.spin_oh_norm_max.setSuffix("  cm⁻¹")
        self.spin_oh_norm_max.valueChanged.connect(
            lambda _value: self.oh_overlay_intensity_changed.emit())

        intensity_form.addRow("Min:", self.spin_oh_norm_min)
        intensity_form.addRow("Max:", self.spin_oh_norm_max)
        intensity_layout.addLayout(intensity_form)
        intensity_grp.setLayout(intensity_layout)
        layout.addWidget(intensity_grp)
        self._on_oh_overlay_intensity_changed()

        tip = QLabel("색상은 Potential 값 기준이며, 값이 없으면 목록 색상을 사용합니다.")
        tip.setStyleSheet("color: #6c7086; font-size: 11px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addStretch()
        return w

    def _make_lock_header_item(self, label: str, tooltip: str) -> QTableWidgetItem:
        pixmap = QPixmap(12, 12)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        lock_color = QColor("#f9e2af")
        pen = QPen(lock_color)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(lock_color)
        painter.drawRoundedRect(3, 5, 6, 5, 1.2, 1.2)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(2, 1, 8, 8, 35 * 16, 110 * 16)
        painter.end()

        item = QTableWidgetItem(label)
        item.setIcon(QIcon(pixmap))
        item.setTextAlignment(Qt.AlignCenter)
        item.setToolTip(tooltip)
        return item

    def _build_summary_group(self):
        grp = QGroupBox("Current Summary")
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        def _make_value_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet("color: #cdd6f4; font-size: 12px;")
            lbl.setWordWrap(True)
            return lbl

        self._summary_fit_widget = QWidget()
        fit_layout = QVBoxLayout(self._summary_fit_widget)
        fit_layout.setContentsMargins(0, 0, 0, 0)
        fit_layout.setSpacing(4)
        self.summary_r2_label = _make_value_label("Fit R²:  —")
        self.summary_peak_label = _make_value_label("Peaks:  —")
        self.summary_fwhm_label = _make_value_label("FWHM:  —")
        fit_layout.addWidget(self.summary_r2_label)
        fit_layout.addWidget(self.summary_peak_label)
        fit_layout.addWidget(self.summary_fwhm_label)
        layout.addWidget(self._summary_fit_widget)

        self._summary_oh_widget = QWidget()
        oh_layout = QVBoxLayout(self._summary_oh_widget)
        oh_layout.setContentsMargins(0, 0, 0, 0)
        oh_layout.setSpacing(4)
        self.oh_total_area_label = _make_value_label("OH Total Area:  —")
        self.oh_norm_label = _make_value_label("OH / Si-O:  —")
        oh_layout.addWidget(self.oh_total_area_label)
        oh_layout.addWidget(self.oh_norm_label)
        layout.addWidget(self._summary_oh_widget)

        self._summary_co_widget = QWidget()
        co_layout = QVBoxLayout(self._summary_co_widget)
        co_layout.setContentsMargins(0, 0, 0, 0)
        co_layout.setSpacing(4)
        self.co_l_summary_label = _make_value_label("CO_L:  —")
        self.co_b_summary_label = _make_value_label("CO_B:  —")
        self.co_ratio_summary_label = _make_value_label("CO_L / CO_B:  —")
        co_layout.addWidget(self.co_l_summary_label)
        co_layout.addWidget(self.co_b_summary_label)
        co_layout.addWidget(self.co_ratio_summary_label)
        layout.addWidget(self._summary_co_widget)

        self._summary_sio_widget = QWidget()
        sio_layout = QVBoxLayout(self._summary_sio_widget)
        sio_layout.setContentsMargins(0, 0, 0, 0)
        sio_layout.setSpacing(4)
        self.sio_area_label = _make_value_label("Si-O Area:  —")
        sio_layout.addWidget(self.sio_area_label)
        layout.addWidget(self._summary_sio_widget)

        self._summary_stark_widget = QWidget()
        stark_layout = QVBoxLayout(self._summary_stark_widget)
        stark_layout.setContentsMargins(0, 4, 0, 0)
        stark_layout.setSpacing(6)
        self.btn_calc_stark_summary = QPushButton("Calculate Stark Slopes")
        self.btn_calc_stark_summary.setObjectName("btn_primary")
        self.btn_calc_stark_summary.clicked.connect(self.stark_calculate_requested.emit)
        self.stark_summary_label = _make_value_label("Stark slopes:  not calculated")
        stark_layout.addWidget(self.btn_calc_stark_summary)
        stark_layout.addWidget(self.stark_summary_label)
        layout.addWidget(self._summary_stark_widget)

        grp.setLayout(layout)
        self._sync_summary_visibility()
        return grp

    def _sync_summary_visibility(self):
        mode = getattr(self, '_current_mode', 'OH')
        self._summary_fit_widget.setVisible(mode == 'OH')
        self._summary_oh_widget.setVisible(mode == 'OH')
        self._summary_stark_widget.setVisible(mode in ('OH', 'CO'))
        self.btn_calc_stark_summary.setVisible(False)
        self._summary_co_widget.setVisible(mode == 'CO')
        self._summary_sio_widget.setVisible(mode == 'SiO')

    def _build_co_peaks_page(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(10)

        info = QLabel(
            "CO view는 선택한 spectrum 전체를 보여줍니다.\n"
            "Total corrected spectrum이 없으면 raw data를 그대로 사용합니다.\n"
            "Fit 후 각 피크의 Assign 값을 지정하면 CO_L/CO_B summary에 반영됩니다."
        )
        info.setStyleSheet("color: #a6adc8; font-size: 11px;")
        layout.addWidget(info)

        fit_mode_row = QHBoxLayout()
        self.lbl_co_b_mode = QLabel("CO_B Handling:")
        fit_mode_row.addWidget(self.lbl_co_b_mode)
        self.combo_co_b_mode = QComboBox()
        self.combo_co_b_mode.addItems(["Manual Assignment"])
        self.combo_co_b_mode.setCurrentIndex(0)
        self.combo_co_b_mode.currentTextChanged.connect(self._on_co_b_mode_changed)
        fit_mode_row.addWidget(self.combo_co_b_mode)
        layout.addLayout(fit_mode_row)
        self.lbl_co_b_mode.setVisible(False)
        self.combo_co_b_mode.setVisible(False)

        self._co_b_deconv_grp = QWidget()
        deconv_layout = QVBoxLayout(self._co_b_deconv_grp)
        deconv_layout.setContentsMargins(0, 0, 0, 0)
        deconv_layout.setSpacing(6)

        shape_row = QHBoxLayout()
        shape_row.addWidget(QLabel("Shape:"))
        self.combo_shape_co = QComboBox()
        self.combo_shape_co.addItems(['Gaussian', 'Lorentzian', 'Voigt'])
        shape_row.addWidget(self.combo_shape_co)
        deconv_layout.addLayout(shape_row)

        btn_detect_co_b = QPushButton("🔍  Auto Guess Peaks")
        btn_detect_co_b.setObjectName("btn_primary")
        btn_detect_co_b.clicked.connect(self.auto_detect_co_b_requested.emit)
        deconv_layout.addWidget(btn_detect_co_b)

        self.btn_co_add_peak = QPushButton("＋  Add Peak Mode")
        self.btn_co_add_peak.setCheckable(True)
        self.btn_co_add_peak.setObjectName("btn_flat")
        self.btn_co_add_peak.setToolTip("플롯에서 클릭 또는 우클릭+드래그로 CO 피크를 수동 생성")
        self.btn_co_add_peak.toggled.connect(self.co_peak_add_mode_toggled.emit)
        deconv_layout.addWidget(self.btn_co_add_peak)

        co_grp = QGroupBox("CO Peaks")
        co_grp_layout = QVBoxLayout()
        co_grp_layout.setContentsMargins(6, 6, 6, 6)
        co_grp_layout.setSpacing(6)
        lock_hint = QLabel("Assign labels after fitting; summary uses assigned CO_L/CO_B only.")
        lock_hint.setStyleSheet("color: #6c7086; font-size: 11px;")
        lock_hint.setWordWrap(True)
        co_grp_layout.addWidget(lock_hint)

        self.co_init_table = _PeakTableWidget(0, 9)
        self.co_init_table.setHorizontalHeaderLabels(
            ["", "Assign", "Shape", "Center", "Amp", "Sigma", "C", "A", "S"])
        hh = self.co_init_table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignCenter)
        hh.setFixedHeight(38)
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        hh.setSectionResizeMode(6, QHeaderView.Fixed)
        hh.setSectionResizeMode(7, QHeaderView.Fixed)
        hh.setSectionResizeMode(8, QHeaderView.Fixed)
        self.co_init_table.setColumnWidth(0, 24)
        self.co_init_table.setColumnWidth(1, 112)
        self.co_init_table.setColumnWidth(2, 112)
        self.co_init_table.setColumnWidth(6, 44)
        self.co_init_table.setColumnWidth(7, 44)
        self.co_init_table.setColumnWidth(8, 44)
        self.co_init_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.co_init_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.co_init_table.verticalHeader().setVisible(False)
        self.co_init_table.verticalHeader().setDefaultSectionSize(40)
        self.co_init_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.co_init_table.itemChanged.connect(self._on_co_table_changed)
        self.co_init_table.delete_pressed.connect(self._delete_selected_co_peaks)
        lock_tips = {
            6: "Lock Center",
            7: "Lock Amplitude",
            8: "Lock Sigma",
        }
        for col, tip in lock_tips.items():
            self.co_init_table.setHorizontalHeaderItem(
                col, self._make_lock_header_item(self.co_init_table.horizontalHeaderItem(col).text(), tip)
            )
        co_grp_layout.addWidget(self.co_init_table)

        co_btn_row = QHBoxLayout()
        btn_clear_co_peaks = QPushButton("✕ Clear CO Peaks")
        btn_clear_co_peaks.setObjectName("btn_flat")
        btn_clear_co_peaks.clicked.connect(self._on_clear_co_peaks)
        co_btn_row.addWidget(btn_clear_co_peaks)
        co_grp_layout.addLayout(co_btn_row)
        co_grp.setLayout(co_grp_layout)
        deconv_layout.addWidget(co_grp)

        layout.addWidget(self._co_b_deconv_grp)

        btn_fit_all_co = QPushButton("⚡  Fit All CO")
        btn_fit_all_co.setObjectName("btn_primary")
        btn_fit_all_co.clicked.connect(self.co_analyze_all_requested.emit)
        layout.addWidget(btn_fit_all_co)

        btn_fit_co = QPushButton("▶  Fit Current")
        btn_fit_co.setObjectName("btn_success")
        btn_fit_co.clicked.connect(self.fit_requested.emit)
        layout.addWidget(btn_fit_co)

        self._on_co_b_mode_changed(self.combo_co_b_mode.currentText())
        layout.addStretch()
        return w

    def _on_co_b_mode_changed(self, mode_text: str):
        self._co_b_deconv_grp.setVisible(True)

    def set_co_b_fit_mode(self, mode: str):
        self.combo_co_b_mode.blockSignals(True)
        self.combo_co_b_mode.setCurrentText("Manual Assignment")
        self.combo_co_b_mode.blockSignals(False)
        self._on_co_b_mode_changed("Manual Assignment")

    def get_co_b_fit_mode(self) -> str:
        return "auto"

    def _build_sio_peaks_page(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(10)

        info = QLabel(
            "Si-O area는 Analysis Region 범위를 적분합니다.\n"
            "Baseline은 Total tab에서 잡은 corrected spectrum을 사용합니다.\n"
            "Total corrected가 없으면 raw data를 그대로 사용합니다.\n\n"
            "면적 = |∫(analysis input) dν|"
        )
        info.setStyleSheet("color: #a6adc8; font-size: 11px;")
        layout.addWidget(info)

        self.sio_mode_area_label = QLabel("Current Si-O Area:  —")
        self.sio_mode_area_label.setObjectName("result_value")
        self.sio_mode_area_label.setWordWrap(True)
        layout.addWidget(self.sio_mode_area_label)

        btn_sio = QPushButton("▶  Calculate Si-O Area")
        btn_sio.setObjectName("btn_success")
        btn_sio.clicked.connect(self.fit_requested.emit)
        layout.addWidget(btn_sio)

        layout.addStretch()
        return w

    # ── Results 탭 ────────────────────────────────────────────

    def _build_results_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        self.r2_label = QLabel("R²  —")
        self.r2_label.setObjectName("result_value")
        layout.addWidget(self.r2_label)

        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["Peak", "Center", "Area", "Area %"])
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._prepare_results_table(self.result_table)
        layout.addWidget(self.result_table)

        # FWHM 서브 테이블
        fwhm_grp = QGroupBox("FWHM")
        fwhm_layout = QVBoxLayout()
        self.fwhm_table = QTableWidget(0, 2)
        self.fwhm_table.setHorizontalHeaderLabels(["Peak", "FWHM (cm⁻¹)"])
        self.fwhm_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.fwhm_table.verticalHeader().setVisible(False)
        self.fwhm_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._prepare_results_table(self.fwhm_table)
        fwhm_layout.addWidget(self.fwhm_table)
        fwhm_grp.setLayout(fwhm_layout)
        layout.addWidget(fwhm_grp)

        # Stark Tuning
        stark_grp = QGroupBox("⚡ Stark Tuning Slopes")
        stark_layout = QVBoxLayout()
        stark_layout.setSpacing(6)
        stark_layout.setContentsMargins(6, 6, 6, 6)

        btn_calc_stark = QPushButton("Calculate Slopes")
        btn_calc_stark.setObjectName("btn_primary")
        btn_calc_stark.clicked.connect(self.stark_calculate_requested.emit)
        stark_layout.addWidget(btn_calc_stark)

        self.stark_table = QTableWidget(0, 5)
        self.stark_table.setHorizontalHeaderLabels(["Peak", "Slope\n(cm⁻¹/V)", "R²", "N", "Plot"])
        self.stark_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.stark_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.stark_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.stark_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.stark_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.stark_table.verticalHeader().setVisible(False)
        self.stark_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._prepare_results_table(self.stark_table)
        stark_layout.addWidget(self.stark_table)
        stark_grp.setLayout(stark_layout)

        layout.addWidget(stark_grp)

        # ── OH Total Area ──────────────────────────────────────
        oh_area_grp = QGroupBox("OH Total Area")
        oh_area_layout = QVBoxLayout()
        oh_area_layout.setSpacing(4)
        oh_area_layout.setContentsMargins(6, 6, 6, 6)
        self.oh_total_area_label = QLabel("OH Total Area:  —")
        self.oh_norm_label       = QLabel("OH / Si-O:  —")
        for lbl in [self.oh_total_area_label, self.oh_norm_label]:
            lbl.setStyleSheet("color: #cdd6f4; font-size: 12px;")
        oh_area_layout.addWidget(self.oh_total_area_label)
        oh_area_layout.addWidget(self.oh_norm_label)
        oh_area_grp.setLayout(oh_area_layout)
        layout.addWidget(oh_area_grp)

        # ── CO Analysis ───────────────────────────────────────
        co_grp = QGroupBox("CO Analysis")
        co_layout = QVBoxLayout()
        co_layout.setSpacing(4)
        co_layout.setContentsMargins(6, 6, 6, 6)
        self.co_result_table = QTableWidget(3, 3)
        self.co_result_table.setHorizontalHeaderLabels(
            ["", "Center (cm⁻¹)", "Area"])
        self.co_result_table.setVerticalHeaderLabels(["CO_L", "CO_B", "Ratio (L/B)"])
        self.co_result_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.co_result_table.verticalHeader().setVisible(True)
        self.co_result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._prepare_results_table(self.co_result_table)
        co_layout.addWidget(self.co_result_table)
        co_grp.setLayout(co_layout)
        layout.addWidget(co_grp)

        # ── Si-O Area ─────────────────────────────────────────
        sio_grp = QGroupBox("Si-O Area")
        sio_layout = QVBoxLayout()
        sio_layout.setSpacing(4)
        sio_layout.setContentsMargins(6, 6, 6, 6)
        self.sio_area_label = QLabel("Si-O Area:  —")
        self.sio_area_label.setStyleSheet("color: #cdd6f4; font-size: 12px;")
        sio_layout.addWidget(self.sio_area_label)
        sio_grp.setLayout(sio_layout)
        layout.addWidget(sio_grp)

        layout.addStretch()
        scroll.setWidget(w)
        self._results_scroll = scroll
        self._results_tables = [
            self.result_table,
            self.fwhm_table,
            self.stark_table,
            self.co_result_table,
        ]
        self._refresh_results_tables()
        return scroll

    def _prepare_results_table(self, table: QTableWidget):
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setSizeAdjustPolicy(QTableWidget.AdjustToContents)

    def _table_height(self, table: QTableWidget, min_rows: int = 1) -> int:
        header_h = table.horizontalHeader().height() if table.horizontalHeader().isVisible() else 0
        row_count = max(table.rowCount(), min_rows)
        if table.rowCount() > 0:
            row_h = sum(table.rowHeight(row) for row in range(table.rowCount()))
        else:
            row_h = table.verticalHeader().defaultSectionSize() * row_count
        frame = table.frameWidth() * 2
        return header_h + row_h + frame + 2

    def _refresh_results_tables(self):
        self.result_table.setFixedHeight(self._table_height(self.result_table))
        self.fwhm_table.setFixedHeight(self._table_height(self.fwhm_table))
        self.stark_table.setFixedHeight(self._table_height(self.stark_table))
        self.co_result_table.setFixedHeight(self._table_height(self.co_result_table, min_rows=3))

    # ── 모드 전환 ─────────────────────────────────────────────

    def _set_mode(self, mode: str):
        if hasattr(self, 'spin_wn_min') and hasattr(self, 'spin_wn_max'):
            self._mode_regions[self._current_mode] = (
                float(self.spin_wn_min.value()),
                float(self.spin_wn_max.value()),
            )

        self._current_mode = mode
        mode_map = {'Total': 0, 'OH': 1, 'CO': 2, 'SiO': 3}
        self._peaks_stack.setCurrentIndex(mode_map.get(mode, 0))
        for btn, m in [(self.btn_mode_total, 'Total'),
                       (self.btn_mode_oh, 'OH'),
                       (self.btn_mode_co, 'CO'),
                       (self.btn_mode_sio, 'SiO')]:
            btn.setChecked(m == mode)

        if hasattr(self, 'spin_wn_min') and hasattr(self, 'spin_wn_max'):
            wn_min, wn_max = self._mode_regions.get(mode, self._mode_regions['OH'])
            self.spin_wn_min.blockSignals(True)
            self.spin_wn_max.blockSignals(True)
            self.spin_wn_min.setValue(wn_min)
            self.spin_wn_max.setValue(wn_max)
            self.spin_wn_min.blockSignals(False)
            self.spin_wn_max.blockSignals(False)

        if hasattr(self, '_baseline_group'):
            is_total = mode == 'Total'
            self._baseline_group.setVisible(is_total)
            self._bl_panel.setVisible(is_total and self.btn_edit_bl.isChecked())

        if hasattr(self, '_region_group'):
            self._region_group.setVisible(mode != 'CO')

        self._sync_summary_visibility()
        self.mode_changed.emit(mode)

    def get_mode(self) -> str:
        return self._current_mode

    def set_mode(self, mode: str):
        self._set_mode(mode)

    def set_total_shift_checked(self, checked: bool):
        self.cb_total_shift.blockSignals(True)
        self.cb_total_shift.setChecked(bool(checked))
        self.cb_total_shift.blockSignals(False)

    def is_total_shift_enabled(self) -> bool:
        return bool(self.cb_total_shift.isChecked() and self.cb_total_shift.isEnabled())

    def set_total_probe_checked(self, checked: bool):
        self.cb_total_probe.blockSignals(True)
        self.cb_total_probe.setChecked(bool(checked))
        self.cb_total_probe.blockSignals(False)

    def set_total_view_mode(self, view_mode: str):
        txt = "Stack" if view_mode == 'stack' else "Overlay"
        self.combo_total_view.blockSignals(True)
        self.combo_total_view.setCurrentText(txt)
        self.combo_total_view.blockSignals(False)

    # ── Public API ────────────────────────────────────────────

    def set_wavenumber_range(self, wn_min, wn_max):
        self.spin_wn_min.setRange(wn_min, wn_max)
        self.spin_wn_max.setRange(wn_min, wn_max)
        current_min = min(max(self.spin_wn_min.value(), wn_min), wn_max)
        current_max = min(max(self.spin_wn_max.value(), wn_min), wn_max)
        self.spin_wn_min.setValue(current_min)
        self.spin_wn_max.setValue(current_max)
        if hasattr(self, 'spin_oh_norm_min') and hasattr(self, 'spin_oh_norm_max'):
            self.spin_oh_norm_min.setRange(wn_min, wn_max)
            self.spin_oh_norm_max.setRange(wn_min, wn_max)
            norm_min = min(max(self.spin_oh_norm_min.value(), wn_min), wn_max)
            norm_max = min(max(self.spin_oh_norm_max.value(), wn_min), wn_max)
            self.spin_oh_norm_min.setValue(norm_min)
            self.spin_oh_norm_max.setValue(norm_max)
        self._mode_regions[self._current_mode] = (float(current_min), float(current_max))

    def set_region_values(self, wn_min: float, wn_max: float):
        self.spin_wn_min.blockSignals(True)
        self.spin_wn_max.blockSignals(True)
        self.spin_wn_min.setValue(float(wn_min))
        self.spin_wn_max.setValue(float(wn_max))
        self.spin_wn_min.blockSignals(False)
        self.spin_wn_max.blockSignals(False)
        self._mode_regions[self._current_mode] = (float(wn_min), float(wn_max))

    def get_config(self) -> dict:
        algo = self.combo_bl_algo.currentText()
        params = {}
        if algo == "ARPLS":
            params['lam'] = self.spin_lam.value()
        elif algo == "SNIP":
            params['n_iter'] = self.spin_iter.value()
        return {
            'wn_min': self.spin_wn_min.value(),
            'wn_max': self.spin_wn_max.value(),
            'center_tolerance': self.spin_tolerance.value(),
            'baseline_algo': algo,
            'baseline_params': params,
        }

    def get_oh_overlay_intensity_config(self) -> dict:
        mode = self.combo_oh_overlay_intensity.currentText().lower()
        wn_min = float(self.spin_oh_norm_min.value())
        wn_max = float(self.spin_oh_norm_max.value())
        return {
            'mode': 'normalize' if mode == 'normalize' else 'original',
            'wn_min': min(wn_min, wn_max),
            'wn_max': max(wn_min, wn_max),
        }

    def get_n_peaks(self) -> int:
        return self.spin_n_peaks.value()

    def get_peak_shape(self) -> str:
        return 'gaussian'

    def set_oh_snapshot_config(
        self,
        config: dict,
        n_peaks: int | None = None,
        baseline_edit_enabled: bool | None = None,
    ):
        widgets = [
            self.spin_wn_min,
            self.spin_wn_max,
            self.spin_tolerance,
            self.combo_bl_algo,
            self.spin_lam,
            self.spin_iter,
            self.spin_n_peaks,
            self.btn_edit_bl,
        ]
        for widget in widgets:
            widget.blockSignals(True)

        if n_peaks is not None:
            self.spin_n_peaks.setValue(int(n_peaks))

        self.spin_wn_min.setValue(config.get('wn_min', self.spin_wn_min.value()))
        self.spin_wn_max.setValue(config.get('wn_max', self.spin_wn_max.value()))
        self.spin_tolerance.setValue(config.get('center_tolerance', self.spin_tolerance.value()))

        algo = config.get('baseline_algo', self.combo_bl_algo.currentText())
        if algo == 'OH Auto Baseline':
            algo = 'Manual'
        self.combo_bl_algo.setCurrentText(algo)
        params = config.get('baseline_params', {})
        if 'lam' in params:
            self.spin_lam.setValue(params['lam'])
        if 'n_iter' in params:
            self.spin_iter.setValue(int(params['n_iter']))

        if baseline_edit_enabled is not None:
            self.btn_edit_bl.setChecked(bool(baseline_edit_enabled))
            self._bl_panel.setVisible(bool(baseline_edit_enabled))
            self.btn_edit_bl.setText(
                "▣ Editing Baseline…" if baseline_edit_enabled else "Edit Baseline"
            )

        for widget in widgets:
            widget.blockSignals(False)

        self._update_bl_param_visibility(self.combo_bl_algo.currentText())

    def get_locks(self) -> list:
        locks = []
        for i in range(self.init_table.rowCount()):
            ci = self.init_table.item(i, 5)
            ai = self.init_table.item(i, 6)
            si = self.init_table.item(i, 7)
            locks.append({
                'center': ci is not None and ci.checkState() == Qt.Checked,
                'amplitude': ai is not None and ai.checkState() == Qt.Checked,
                'sigma':  si is not None and si.checkState() == Qt.Checked,
            })
        return locks

    def set_guesses(self, guesses: list, locks: list | None = None):
        if locks is None:
            locks = [{'center': False, 'amplitude': False, 'sigma': False}
                     for _ in guesses]
        self._guesses = list(guesses)
        self.init_table.blockSignals(True)
        self.init_table.setRowCount(len(guesses))
        for i, g in enumerate(guesses):
            dot = QTableWidgetItem("●")
            dot.setForeground(QColor(PEAK_COLORS[i % len(PEAK_COLORS)]))
            dot.setFlags(dot.flags() & ~Qt.ItemIsEditable)
            dot.setTextAlignment(Qt.AlignCenter)

            shape_combo = QComboBox()
            shape_combo.addItems(['Gaussian', 'Lorentzian', 'Voigt'])
            shape_combo.setCurrentText(getattr(g, 'shape', 'gaussian').capitalize())
            shape_combo.setFocusPolicy(Qt.NoFocus)
            shape_combo.setObjectName("table_combo")
            shape_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            shape_combo.setMinimumContentsLength(8)

            center_item = QTableWidgetItem(f"{g.center:.1f}")
            amp_item    = QTableWidgetItem(f"{getattr(g, 'amplitude', 0.1):.4f}")
            sigma_item  = QTableWidgetItem(f"{g.sigma:.1f}")

            old = locks[i] if i < len(locks) else {}

            def _chk(locked: bool) -> QTableWidgetItem:
                item = QTableWidgetItem()
                item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                item.setCheckState(Qt.Checked if locked else Qt.Unchecked)
                item.setTextAlignment(Qt.AlignCenter)
                return item

            self.init_table.setItem(i, 0, dot)
            self.init_table.setCellWidget(i, 1, shape_combo)
            self.init_table.setItem(i, 2, center_item)
            self.init_table.setItem(i, 3, amp_item)
            self.init_table.setItem(i, 4, sigma_item)
            self.init_table.setItem(i, 5, _chk(old.get('center', False)))
            self.init_table.setItem(i, 6, _chk(old.get('amplitude', False)))
            self.init_table.setItem(i, 7, _chk(old.get('sigma', False)))
            
            shape_combo.currentTextChanged.connect(self._on_shape_combo_changed)
        self.init_table.blockSignals(False)

        # 행 수에 맞게 높이 조정 (내부 스크롤바 없이 전체 표시)
        header_h = self.init_table.horizontalHeader().height()
        row_h    = self.init_table.verticalHeader().defaultSectionSize()
        self.init_table.setFixedHeight(header_h + row_h * len(guesses) + 2)
        self.locks_changed.emit(self.get_locks())

    def _on_shape_combo_changed(self, txt):
        if getattr(self, 'init_table', None) and self.init_table.item(0, 0): self._on_table_changed(self.init_table.item(0, 0))

    def add_peak_guess(self, center: float, amplitude: float = 0.1, sigma: float = 30.0):
        """플롯 클릭 또는 우클릭 드래그로 피크 추가.
        amplitude: 피크 높이 (absorbance 단위)
        sigma: Gaussian sigma (cm⁻¹)
        """
        from core.peak_finder import PeakGuess
        locks = self.get_locks()
        new_guess = PeakGuess(center=center, amplitude=max(amplitude, 1e-6),
                              sigma=max(sigma, 1.0), index=len(self._guesses))
        self._guesses.append(new_guess)
        locks.append({'center': False, 'amplitude': False, 'sigma': False})
        self.set_guesses(self._guesses, locks=locks)
        self.peak_params_changed.emit(self._guesses)

    def update_peak_center(self, idx: int, center: float):
        """드래그 후 테이블 업데이트"""
        if idx < len(self._guesses):
            self._guesses[idx].center = center
            self.init_table.blockSignals(True)
            item = self.init_table.item(idx, 2)
            if item:
                item.setText(f"{center:.1f}")
            self.init_table.blockSignals(False)

    def get_guesses(self) -> list:
        guesses = []
        for i in range(self.init_table.rowCount()):
            try:
                combo = self.init_table.cellWidget(i, 1)
                shape = combo.currentText().lower() if combo else 'gaussian'
                center = float(self.init_table.item(i, 2).text())
                amp = float(self.init_table.item(i, 3).text())
                sigma  = float(self.init_table.item(i, 4).text())
                from core.peak_finder import PeakGuess
                guesses.append(PeakGuess(center=center, amplitude=amp, sigma=sigma, index=i, shape=shape))
            except (AttributeError, ValueError):
                pass
        return guesses

    def _on_clear_peaks(self):
        self._guesses = []
        self.init_table.setRowCount(0)
        self.summary_r2_label.setText("Fit R²:  —")
        self.summary_peak_label.setText("Peaks:  —")
        self.summary_fwhm_label.setText("FWHM:  —")
        self.peaks_cleared.emit()

    def _delete_selected_peaks(self):
        rows = sorted({idx.row() for idx in self.init_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        locks = self.get_locks()

        for row in rows:
            if 0 <= row < len(self._guesses):
                self._guesses.pop(row)
            if 0 <= row < len(locks):
                locks.pop(row)

        for i, g in enumerate(self._guesses):
            g.index = i

        self.set_guesses(self._guesses, locks=locks)
        if self._guesses:
            self.peak_rows_deleted.emit(self._guesses)
        else:
            self.peaks_cleared.emit()

    def _on_table_changed(self, item):
        if item is not None and item.column() >= 5:   # lock 체크박스
            self.locks_changed.emit(self.get_locks())
            return
        guesses = self.get_guesses()
        if guesses:
            self._guesses = guesses
            self.peak_params_changed.emit(guesses)

    def update_results(self, result: FitResult):
        self.summary_r2_label.setText(f"Fit R²:  {result.r_squared:.6f}")
        self.summary_peak_label.setText(f"Peaks:  {len(result.peaks)}")
        if result.peaks:
            main_peak = max(result.peaks, key=lambda p: getattr(p, 'area_fraction', 0.0))
            self.summary_peak_label.setText(
                f"Peaks:  {len(result.peaks)}  |  Main P{main_peak.index + 1}: "
                f"{main_peak.center:.1f} cm⁻¹ ({main_peak.area_fraction:.1f}%)"
            )
            fwhm_vals = [p.fwhm for p in result.peaks if getattr(p, 'fwhm', None) is not None]
            if fwhm_vals:
                self.summary_fwhm_label.setText(
                    f"FWHM:  {min(fwhm_vals):.1f} – {max(fwhm_vals):.1f} cm⁻¹"
                )
            else:
                self.summary_fwhm_label.setText("FWHM:  —")
        else:
            self.summary_fwhm_label.setText("FWHM:  —")

    def clear_results(self):
        """현재 스펙트럼의 fit summary 초기화"""
        self.summary_r2_label.setText("Fit R²:  —")
        self.summary_peak_label.setText("Peaks:  —")
        self.summary_fwhm_label.setText("FWHM:  —")

    def clear_current_summary(self):
        """현재 표시 중인 스펙트럼이 없을 때 summary 전체 초기화"""
        self.clear_results()
        self.oh_total_area_label.setText("OH Total Area:  —")
        self.oh_norm_label.setText("OH / Si-O:  —")
        self.co_l_summary_label.setText("CO_L:  —")
        self.co_b_summary_label.setText("CO_B:  —")
        self.co_ratio_summary_label.setText("CO_L / CO_B:  —")
        self.sio_area_label.setText("Si-O Area:  —")
        self.sio_mode_area_label.setText("Current Si-O Area:  —")
        self.stark_summary_label.setText("Stark slopes:  not calculated")

    def set_snapshot_names(self, names: list[str], selected_index: int = -1):
        self.combo_snapshots.blockSignals(True)
        self.combo_snapshots.clear()
        self.combo_snapshots.addItems(names)
        has_items = bool(names)
        self.combo_snapshots.setEnabled(has_items)
        self.btn_snapshot_restore.setEnabled(has_items)
        self.btn_snapshot_delete.setEnabled(has_items)
        if has_items:
            if selected_index < 0 or selected_index >= len(names):
                selected_index = len(names) - 1
            self.combo_snapshots.setCurrentIndex(selected_index)
        self.combo_snapshots.blockSignals(False)

    def _emit_snapshot_restore(self):
        idx = self.combo_snapshots.currentIndex()
        if idx >= 0:
            self.snapshot_restore_requested.emit(idx)

    def _emit_snapshot_delete(self):
        idx = self.combo_snapshots.currentIndex()
        if idx >= 0:
            self.snapshot_delete_requested.emit(idx)

    def update_peak_amplitude(self, idx: int, amp: float):
        """amplitude = 피크 높이 (absorbance 단위)."""
        if idx < self.init_table.rowCount():
            self.init_table.blockSignals(True)
            item = self.init_table.item(idx, 3)
            if item:
                item.setText(f"{amp:.4f}")
            self.init_table.blockSignals(False)
        if idx < len(self._guesses):
            self._guesses[idx].amplitude = amp
                
    def update_peak_sigma(self, idx: int, sigma: float):
        if idx < self.init_table.rowCount():
            self.init_table.blockSignals(True)
            item = self.init_table.item(idx, 4)
            if item:
                item.setText(f"{sigma:.1f}")
            self.init_table.blockSignals(False)
            if idx < len(self._guesses):
                self._guesses[idx].sigma = sigma

    def get_peak_shape_co(self) -> str:
        return self.combo_shape_co.currentText().lower()

    def get_co_locks(self) -> list:
        locks = []
        for i in range(self.co_init_table.rowCount()):
            ci = self.co_init_table.item(i, 6)
            ai = self.co_init_table.item(i, 7)
            si = self.co_init_table.item(i, 8)
            locks.append({
                'center': ci is not None and ci.checkState() == Qt.Checked,
                'amplitude': ai is not None and ai.checkState() == Qt.Checked,
                'sigma': si is not None and si.checkState() == Qt.Checked,
            })
        return locks

    def set_co_guesses(self, guesses: list, locks: list | None = None):
        if locks is None:
            locks = [{'center': False, 'amplitude': False, 'sigma': False}
                     for _ in guesses]
        self._co_guesses = list(guesses)
        self.co_init_table.blockSignals(True)
        self.co_init_table.setRowCount(len(guesses))
        for i, g in enumerate(guesses):
            dot = QTableWidgetItem("●")
            dot.setForeground(QColor(PEAK_COLORS[i % len(PEAK_COLORS)]))
            dot.setFlags(dot.flags() & ~Qt.ItemIsEditable)
            dot.setTextAlignment(Qt.AlignCenter)

            assign_combo = QComboBox()
            assign_combo.addItems(CO_ASSIGNMENTS)
            assignment = getattr(g, 'assignment', 'Unassigned')
            if assignment not in CO_ASSIGNMENTS:
                assignment = 'Unassigned'
            assign_combo.setCurrentText(assignment)
            assign_combo.setFocusPolicy(Qt.NoFocus)
            assign_combo.setObjectName("table_combo")
            assign_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            assign_combo.setMinimumContentsLength(9)

            shape_combo = QComboBox()
            shape_combo.addItems(['Gaussian', 'Lorentzian', 'Voigt'])
            shape_combo.setCurrentText(getattr(g, 'shape', self.get_peak_shape_co()).capitalize())
            shape_combo.setFocusPolicy(Qt.NoFocus)
            shape_combo.setObjectName("table_combo")
            shape_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            shape_combo.setMinimumContentsLength(8)

            center_item = QTableWidgetItem(f"{g.center:.1f}")
            amp_item = QTableWidgetItem(f"{getattr(g, 'amplitude', 0.1):.4f}")
            sigma_item = QTableWidgetItem(f"{g.sigma:.1f}")

            old = locks[i] if i < len(locks) else {}

            def _chk(locked: bool) -> QTableWidgetItem:
                item = QTableWidgetItem()
                item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                item.setCheckState(Qt.Checked if locked else Qt.Unchecked)
                item.setTextAlignment(Qt.AlignCenter)
                return item

            self.co_init_table.setItem(i, 0, dot)
            self.co_init_table.setCellWidget(i, 1, assign_combo)
            self.co_init_table.setCellWidget(i, 2, shape_combo)
            self.co_init_table.setItem(i, 3, center_item)
            self.co_init_table.setItem(i, 4, amp_item)
            self.co_init_table.setItem(i, 5, sigma_item)
            self.co_init_table.setItem(i, 6, _chk(old.get('center', False)))
            self.co_init_table.setItem(i, 7, _chk(old.get('amplitude', False)))
            self.co_init_table.setItem(i, 8, _chk(old.get('sigma', False)))
            assign_combo.currentTextChanged.connect(self._on_co_shape_combo_changed)
            shape_combo.currentTextChanged.connect(self._on_co_shape_combo_changed)
        self.co_init_table.blockSignals(False)

        header_h = self.co_init_table.horizontalHeader().height()
        row_h = self.co_init_table.verticalHeader().defaultSectionSize()
        self.co_init_table.setFixedHeight(header_h + row_h * max(len(guesses), 1) + 2)
        self.co_locks_changed.emit(self.get_co_locks())

    def get_co_guesses(self) -> list:
        guesses = []
        for i in range(self.co_init_table.rowCount()):
            try:
                assign_combo = self.co_init_table.cellWidget(i, 1)
                assignment = assign_combo.currentText() if assign_combo else 'Unassigned'
                shape_combo = self.co_init_table.cellWidget(i, 2)
                shape = shape_combo.currentText().lower() if shape_combo else self.get_peak_shape_co()
                center = float(self.co_init_table.item(i, 3).text())
                amp = float(self.co_init_table.item(i, 4).text())
                sigma = float(self.co_init_table.item(i, 5).text())
                guess = PeakGuess(
                    center=center, amplitude=amp, sigma=sigma, index=i, shape=shape)
                guess.assignment = assignment
                guesses.append(guess)
            except (AttributeError, ValueError):
                pass
        return guesses

    def _on_co_shape_combo_changed(self, txt):
        if getattr(self, 'co_init_table', None) and self.co_init_table.item(0, 0):
            self._on_co_table_changed(self.co_init_table.item(0, 0))

    def _on_co_table_changed(self, item):
        if item is not None and item.column() >= 6:
            self.co_locks_changed.emit(self.get_co_locks())
            return
        guesses = self.get_co_guesses()
        self._co_guesses = guesses
        self.co_peak_params_changed.emit(guesses)

    def _on_clear_co_peaks(self):
        self._co_guesses = []
        self.co_init_table.setRowCount(0)
        self.co_peaks_cleared.emit()

    def _delete_selected_co_peaks(self):
        rows = sorted({idx.row() for idx in self.co_init_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        locks = self.get_co_locks()
        for row in rows:
            if 0 <= row < len(self._co_guesses):
                self._co_guesses.pop(row)
            if 0 <= row < len(locks):
                locks.pop(row)
        for i, g in enumerate(self._co_guesses):
            g.index = i
        self.set_co_guesses(self._co_guesses, locks=locks)
        if self._co_guesses:
            self.co_peak_rows_deleted.emit(self._co_guesses)
        else:
            self.co_peaks_cleared.emit()

    def update_co_peak_center(self, idx: int, center: float):
        if idx < len(self._co_guesses):
            self._co_guesses[idx].center = center
        if idx < self.co_init_table.rowCount():
            self.co_init_table.blockSignals(True)
            item = self.co_init_table.item(idx, 3)
            if item:
                item.setText(f"{center:.1f}")
            self.co_init_table.blockSignals(False)

    def update_co_peak_amplitude(self, idx: int, amp: float):
        if idx < len(self._co_guesses):
            self._co_guesses[idx].amplitude = amp
        if idx < self.co_init_table.rowCount():
            self.co_init_table.blockSignals(True)
            item = self.co_init_table.item(idx, 4)
            if item:
                item.setText(f"{amp:.4f}")
            self.co_init_table.blockSignals(False)

    def update_co_peak_sigma(self, idx: int, sigma: float):
        if idx < len(self._co_guesses):
            self._co_guesses[idx].sigma = sigma
        if idx < self.co_init_table.rowCount():
            self.co_init_table.blockSignals(True)
            item = self.co_init_table.item(idx, 5)
            if item:
                item.setText(f"{sigma:.1f}")
            self.co_init_table.blockSignals(False)

    def update_oh_total_area(self, area: float, sio_area: float = None):
        self.oh_total_area_label.setText(f"OH Total Area:  {area:.4f}")
        if sio_area is not None and sio_area > 0:
            self.oh_norm_label.setText(f"OH / Si-O:  {area / sio_area:.4f}")
        else:
            self.oh_norm_label.setText("OH / Si-O:  —")

    def update_co_results(self, co_l_result, co_b_result):
        """CO_L, CO_B FitResult 요약 업데이트"""
        if co_l_result and co_l_result.success and co_l_result.peaks:
            p = co_l_result.peaks[0]
            self.co_l_summary_label.setText(
                f"CO_L:  {p.center:.1f} cm⁻¹  |  area {p.area:.4f}"
            )
        else:
            self.co_l_summary_label.setText("CO_L:  —")

        if co_b_result and co_b_result.success and co_b_result.peaks:
            p = co_b_result.peaks[0]
            self.co_b_summary_label.setText(
                f"CO_B:  {p.center:.1f} cm⁻¹  |  area {p.area:.4f}"
            )
        else:
            self.co_b_summary_label.setText("CO_B:  —")

        if (co_l_result and co_l_result.success and co_l_result.peaks and
                co_b_result and co_b_result.success and co_b_result.peaks
                and co_b_result.peaks[0].area > 0):
            ratio = co_l_result.peaks[0].area / co_b_result.peaks[0].area
            self.co_ratio_summary_label.setText(f"CO_L / CO_B:  {ratio:.3f}")
        else:
            self.co_ratio_summary_label.setText("CO_L / CO_B:  —")

    def update_sio_area(self, area):
        if area is None:
            self.sio_area_label.setText("Si-O Area:  —")
            self.sio_mode_area_label.setText("Current Si-O Area:  —")
            return
        self.sio_area_label.setText(f"Si-O Area:  {area:.4f}")
        self.sio_mode_area_label.setText(f"Current Si-O Area:  {area:.4f}")

    def update_stark_results(self, results):
        if not results:
            self.stark_summary_label.setText("Stark slopes:  waiting for at least 2 spectra with potentials")
            return
        max_result = max(results, key=lambda r: abs(r.slope))
        self.stark_summary_label.setText(
            f"Stark slopes:  {len(results)} peaks  |  max |slope| P{max_result.peak_index + 1} "
            f"= {max_result.slope:.2f} cm⁻¹/V"
        )

    def clear_stark_results(self):
        self.stark_summary_label.setText("Stark slopes:  waiting for at least 2 spectra with potentials")

    def update_co_stark_results(self, results):
        if not results:
            self.stark_summary_label.setText("CO Stark slopes:  waiting for at least 2 spectra with potentials")
            return
        parts = [
            f"{r.series_name} {r.slope:.2f} cm⁻¹/V (R²={r.r_squared:.3f})"
            for r in results
        ]
        self.stark_summary_label.setText("CO Stark slopes:  " + "  |  ".join(parts))
