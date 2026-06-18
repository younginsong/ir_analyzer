"""
main_window.py - 모던 다크 테마 메인 윈도우
좌: 스펙트럼 목록 사이드바 / 중: 플롯 / 우: 컨트롤 패널
"""

from __future__ import annotations

import sys
import copy
import numpy as np
from pathlib import Path
from datetime import datetime

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter,
    QFileDialog, QMessageBox, QStatusBar, QProgressBar, QLabel, QAction,
    QTabWidget, QMenu, QPushButton, QTabBar, QApplication,
)
from PyQt5.QtCore import Qt, QThread, QTimer, QSettings, pyqtSignal
from PyQt5.QtGui import QKeySequence, QColor

from ui.spectrum_list    import SpectrumListWidget, SpectrumEntry
from ui.plot_widget      import PlotWidget
from ui.right_panel      import RightPanel
from ui.batch_dialog     import BatchDialog
from ui.analysis_widget  import AnalysisWidget

from core.loader      import load_spectrum, crop_region
from core.baseline    import (baseline_rubberband, baseline_from_points,
                               baseline_arpls, baseline_snip, baseline_linear,
                               subtract_baseline, auto_oh_baseline_points,
                               auto_co_baseline_endpoints)
from core.peak_finder import PeakGuess, find_peaks_second_derivative
from core.fitter      import fit_peaks, FitResult
from core.exporter    import (export_single, export_batch, export_all_spectra,
                               export_co_results, export_spectra_excel)
from core.stark_analysis import calculate_stark_slopes, calculate_co_stark_slopes
from core.session     import save_session, load_session
from batch.batch_processor import process_batch, BatchConfig


from dataclasses import dataclass as _dc, field as _field

_trapezoid = np.trapezoid if hasattr(np, 'trapezoid') else np.trapz
MAX_OH_SNAPSHOTS = 5
SETTINGS_ORG = "KIST"
SETTINGS_APP = "In Situ IR Analyzer"
SESSION_DIR_KEY = "paths/session_dir"


@_dc
class _CoPeak:
    center:    float
    area:      float
    amplitude: float


@_dc
class _CoResult:
    success: bool
    peaks:   list


def _make_co_result(center: float, area: float) -> _CoResult:
    """CO 분석 결과를 FitResult 인터페이스와 호환되는 객체로 감쌈 (pickle 가능)"""
    return _CoResult(success=True, peaks=[_CoPeak(center=center, area=area, amplitude=area)])


class BatchWorker(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    error    = pyqtSignal(str)

    def __init__(self, filepaths, config):
        super().__init__()
        self.filepaths = filepaths
        self.config    = config

    def run(self):
        try:
            results = process_batch(
                self.filepaths, self.config,
                progress_callback=lambda c, t, f: self.progress.emit(c, t, f)
            )
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class _DetachableTabBar(QTabBar):
    detach_requested = pyqtSignal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_index = -1
        self._press_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_index = self.tabAt(event.pos())
            self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.LeftButton
                and self._drag_index >= 0
                and self._press_pos is not None):
            if (event.pos() - self._press_pos).manhattanLength() >= QApplication.startDragDistance():
                margin = 24
                if not self.rect().adjusted(-margin, -margin, margin, margin).contains(event.pos()):
                    idx = self._drag_index
                    self._drag_index = -1
                    self._press_pos = None
                    self.detach_requested.emit(idx, event.globalPos())
                    event.accept()
                    return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_index = -1
        self._press_pos = None
        super().mouseReleaseEvent(event)


class _DetachedAnalysisWindow(QMainWindow):
    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._allow_close = False
        self.setWindowTitle("In Situ IR Analyzer - Analysis")
        self.resize(980, 760)

    def allow_close(self):
        self._allow_close = True

    def closeEvent(self, event):
        if self._allow_close:
            super().closeEvent(event)
            return
        self.close_requested.emit()
        event.ignore()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("In Situ IR Analyzer")
        self.setMinimumSize(1280, 780)

        # 현재 활성 스펙트럼 상태
        self._current_entry: SpectrumEntry = None
        self._baseline        = None
        self._ab_corrected    = None
        self._fit_result: FitResult = None
        self._wn_crop         = None
        self._ab_crop         = None
        self._baseline_points: list = []   # Manual 모드 클릭 포인트
        self.batch_results    = []
        self._fit_records: list = []
        self._spectrum_states: dict = {}   # {filepath: OH state dict}
        self._co_states:  dict = {}        # {filepath: {'CO_L': state, 'CO_B': state}}
        self._sio_states: dict = {}        # {filepath: {'ep0', 'ep1'}} — endpoints only
        self._co_fit_records: list = []    # [{filename, CO_L: FitResult, CO_B: FitResult}]
        self._co_stark_results: list = []
        self._co_drag_targets: dict[int, dict] = {}
        self._sio_ref_area: float = None   # 레거시 단일 SiO 참조 면적 fallback
        self._loading_session: bool = False
        self._fit_edit_pending: bool = False
        self._total_shifts: dict[str, dict[str, float]] = {}
        self._total_view_mode: str = 'overlay'
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        # Split view 상태
        self._is_split        = False
        self._inner_splitter  = None
        self._right_tabs      = None
        self._analysis_window = None
        self._analysis_restore_target = 'center'
        self._analysis_window_geometry = None
        self._suppress_analysis_tab_reset = False

        # 피크 조정 후 자동 재피팅 타이머 (400 ms debounce)
        self._refit_timer = QTimer()
        self._refit_timer.setSingleShot(True)
        self._refit_timer.setInterval(400)
        self._refit_timer.timeout.connect(self._auto_refit_current)

        self._build_menu()
        self._build_ui()
        self._build_statusbar()

    def _initial_session_dialog_path(self, suggested_name: str = "") -> str:
        saved_dir = self._settings.value(SESSION_DIR_KEY, "", type=str)
        base_dir = Path(saved_dir).expanduser() if saved_dir else Path.cwd()
        if not base_dir.exists() or not base_dir.is_dir():
            base_dir = Path.home()
        return str(base_dir / suggested_name) if suggested_name else str(base_dir)

    def _remember_session_dialog_path(self, filepath: str):
        if not filepath:
            return
        self._settings.setValue(SESSION_DIR_KEY, str(Path(filepath).expanduser().parent))

    # ── UI 구성 ──────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        # File
        fm = mb.addMenu("File")
        a = QAction("Open CSV…", self); a.setShortcut(QKeySequence.Open)
        a.triggered.connect(self._open_files); fm.addAction(a)
        fm.addSeparator()
        a = QAction("Load Session…", self); a.setShortcut("Ctrl+Shift+O")
        a.triggered.connect(self._load_session); fm.addAction(a)
        a = QAction("Load Multiple Sessions…", self)
        a.triggered.connect(self._import_sessions); fm.addAction(a)
        fm.addSeparator()
        a = QAction("Save Workspace…", self); a.setShortcut("Ctrl+S")
        a.triggered.connect(self._save_session); fm.addAction(a)
        a = QAction("Save Current Session As…", self); a.setShortcut("Ctrl+Shift+S")
        a.triggered.connect(self._save_current_session); fm.addAction(a)
        a = QAction("Close Current Session", self); a.setShortcut("Ctrl+W")
        a.triggered.connect(self._close_current_session); fm.addAction(a)
        fm.addSeparator()
        a = QAction("Export Results (Excel)…", self); a.setShortcut("Ctrl+E")
        a.triggered.connect(self._export); fm.addAction(a)
        a = QAction("Export Spectra (Excel)…", self); a.setShortcut("Ctrl+Shift+E")
        a.triggered.connect(self._export_spectra); fm.addAction(a)
        a = QAction("Batch Process…", self); a.setShortcut("Ctrl+B")
        a.triggered.connect(self._open_batch); fm.addAction(a)

        # Analysis
        am = mb.addMenu("Analysis")
        a = QAction("Auto Detect Peaks", self); a.setShortcut("Ctrl+D")
        a.triggered.connect(self._auto_detect); am.addAction(a)
        a = QAction("Run Fit", self); a.setShortcut("Ctrl+R")
        a.triggered.connect(self._run_fit); am.addAction(a)
        am.addSeparator()
        a = QAction("Zoom to Active Region", self); a.setShortcut("Ctrl+A")
        a.triggered.connect(self._zoom_to_active_region); am.addAction(a)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.main_splitter = QSplitter(Qt.Horizontal)

        # ① 좌측 사이드바 — 스펙트럼 목록
        self.spectrum_list = SpectrumListWidget()
        self.spectrum_list.spectrum_selected.connect(self._on_spectrum_selected)
        self.spectrum_list.selection_changed.connect(self._on_spectrum_selection_changed)
        self.spectrum_list.spectrum_added.connect(self._on_spectrum_added)
        self.spectrum_list.spectra_added.connect(self._on_spectra_added)
        self.spectrum_list.spectrum_removed.connect(self._on_spectrum_removed)
        self.spectrum_list.spectra_reordered.connect(self._on_spectra_reordered)
        self.spectrum_list.session_dropped.connect(self._import_sessions_from_paths)

        # ② 중앙 — Spectrum 탭 + Analysis 탭
        self.plot_widget = PlotWidget()
        self.plot_widget.baseline_point_added.connect(self._on_baseline_point_added)
        self.plot_widget.baseline_point_removed.connect(self._on_baseline_point_removed)
        self.plot_widget.peak_center_dragged.connect(self._on_peak_dragged)
        self.plot_widget.peak_created.connect(self._on_peak_created)

        self.analysis_widget = AnalysisWidget()
        self.analysis_widget.assignment_selected.connect(self._on_analysis_assignment_selected)

        self.center_tabs = QTabWidget()
        self.center_tabs.setObjectName("center_tabs")
        self._setup_detachable_tab_widget(self.center_tabs, self._on_center_tab_context_menu)
        self.center_tabs.addTab(self.plot_widget,     "  Spectrum  ")
        self.center_tabs.addTab(self.analysis_widget, "  Analysis  ")
        self.center_tabs.currentChanged.connect(
            lambda index, tabs=self.center_tabs: self._on_tab_changed(tabs, index)
        )

        # 코너 Split 버튼
        self._btn_split = QPushButton("⊞")
        self._btn_split.setFixedSize(28, 28)
        self._btn_split.setObjectName("btn_flat")
        self._btn_split.clicked.connect(self._toggle_split_view)
        self.center_tabs.setCornerWidget(self._btn_split, Qt.TopRightCorner)
        self._update_split_button()

        # ③ 우측 — 컨트롤 패널
        self.right_panel = RightPanel()
        self.right_panel.region_changed.connect(self._on_region_changed)
        self.right_panel.baseline_mode_toggled.connect(self._on_bl_mode_toggled)
        self.right_panel.baseline_apply.connect(self._on_bl_apply)
        self.right_panel.baseline_undo.connect(self._on_bl_undo)
        self.right_panel.baseline_clear.connect(self._on_bl_clear)
        self.right_panel.fit_requested.connect(self._run_fit)
        self.right_panel.auto_detect_requested.connect(self._auto_detect)
        self.right_panel.peak_params_changed.connect(self._on_peak_params_changed)
        self.right_panel.peak_rows_deleted.connect(self._on_peak_rows_deleted)
        self.right_panel.peaks_cleared.connect(self._on_peaks_cleared)
        self.right_panel.locks_changed.connect(self._on_peak_locks_changed)
        self.right_panel.co_peak_params_changed.connect(self._on_co_peak_params_changed)
        self.right_panel.co_peak_rows_deleted.connect(self._on_co_peak_rows_deleted)
        self.right_panel.co_peaks_cleared.connect(self._on_co_peaks_cleared)
        self.right_panel.co_locks_changed.connect(self._on_co_peak_locks_changed)
        self.right_panel.plot_auto_range.connect(self.plot_widget.do_auto_range)
        self.right_panel.plot_export.connect(self._export_plot_image)
        self.right_panel.plot_x_auto.connect(self.plot_widget.set_x_auto_range)
        self.right_panel.plot_y_auto.connect(self.plot_widget.set_y_auto_range)
        self.right_panel.export_requested.connect(self._export)
        self.right_panel.batch_requested.connect(self._open_batch)
        self.right_panel.stark_calculate_requested.connect(self._calculate_stark)
        self.right_panel.stark_plot_requested.connect(self._show_stark_plot)
        self.right_panel.auto_fit_requested.connect(self._auto_fit)
        self.right_panel.mode_changed.connect(self._on_mode_changed)
        self.right_panel.snapshot_save_requested.connect(self._save_current_snapshot)
        self.right_panel.snapshot_restore_requested.connect(self._restore_snapshot)
        self.right_panel.snapshot_delete_requested.connect(self._delete_snapshot)
        self.right_panel.auto_detect_co_b_requested.connect(self._auto_detect_co_b)
        self.right_panel.co_analyze_all_requested.connect(self._analyze_all_co)
        self.right_panel.total_view_changed.connect(self._on_total_view_changed)
        self.right_panel.total_shift_toggled.connect(self.plot_widget.set_total_shift_mode)
        self.right_panel.total_probe_toggled.connect(self.plot_widget.set_total_probe_mode)
        self.right_panel.total_reset_shifts.connect(self._reset_total_shifts)
        self.spectrum_list.potential_assignments_changed.connect(self._on_potential_assignments_changed)
        self.spectrum_list.session_filter_changed.connect(self._on_session_filter_changed)
        self.spectrum_list.workspace_created.connect(self._on_workspace_created)
        self.spectrum_list.session_save_requested.connect(self._save_session_key)
        self.spectrum_list.session_close_requested.connect(self._close_session_key)
        self.plot_widget.peak_sigma_changed.connect(self._on_peak_sigma_changed)
        self.plot_widget.peak_amplitude_dragged.connect(self._on_peak_amplitude_dragged)
        self.plot_widget.co_endpoint_moved.connect(self._on_co_endpoint_moved)
        self.plot_widget.sio_endpoint_moved.connect(self._on_sio_endpoint_moved)
        self.plot_widget.total_spectrum_selected.connect(self._on_total_spectrum_selected)
        self.plot_widget.total_shift_changed.connect(self._on_total_shift_changed)
        self.plot_widget.total_shift_mode_changed.connect(self.right_panel.set_total_shift_checked)
        self.plot_widget.total_probe_mode_changed.connect(self.right_panel.set_total_probe_checked)

        self.main_splitter.addWidget(self.spectrum_list)
        self.main_splitter.addWidget(self.center_tabs)
        self.main_splitter.addWidget(self.right_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)

        root.addWidget(self.main_splitter)

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_label = QLabel("Ready  —  drag CSV files onto the sidebar to load")
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(180)
        self.progress_bar.setVisible(False)
        sb.addWidget(self.status_label)
        sb.addPermanentWidget(self.progress_bar)

    def _session_labels_in_use(self) -> set[str]:
        labels = {
            entry.source_session_label
            for entry in self.spectrum_list.get_all_entries()
            if getattr(entry, 'source_session_label', '')
        }
        labels.update(
            key for key in self.spectrum_list.get_session_keys()
            if key != self.spectrum_list.LOOSE_FILES_KEY
        )
        return labels

    def _make_unique_session_label(self, base_label: str, labels_in_use: set[str]) -> str:
        label = (base_label or "session").strip()
        if label not in labels_in_use:
            labels_in_use.add(label)
            return label
        n = 2
        while True:
            candidate = f"{label} ({n})"
            if candidate not in labels_in_use:
                labels_in_use.add(candidate)
                return candidate
            n += 1

    def _make_display_name(self, session_label: str, original_name: str) -> str:
        return f"{session_label} :: {original_name}" if session_label else original_name

    def _make_unique_display_name(self, session_label: str, original_name: str,
                                  names_in_use: set[str]) -> str:
        display_name = self._make_display_name(session_label, original_name)
        if display_name not in names_in_use:
            names_in_use.add(display_name)
            return display_name
        n = 2
        while True:
            candidate = f"{display_name} [{n}]"
            if candidate not in names_in_use:
                names_in_use.add(candidate)
                return candidate
            n += 1

    def _make_import_key(self, session_label: str, original_name: str,
                         source_filepath: str, index: int,
                         keys_in_use: set[str]) -> str:
        base = f"session://{session_label}/{index:04d}/{Path(source_filepath).name or original_name}"
        candidate = base
        n = 2
        while candidate in keys_in_use:
            candidate = f"{base}#{n}"
            n += 1
        keys_in_use.add(candidate)
        return candidate

    def _refresh_after_session_merge(self):
        self._refresh_analysis_for_visible_session()
        self.right_panel.update_sio_area(self._get_sio_area_for_entry(self._current_entry))

    def _refresh_analysis_for_visible_session(self):
        current_subtab = self.analysis_widget.get_current_subtab()
        potentials = self._visible_potentials()
        self._sync_analysis_sidebar()

        self._refresh_oh_stark_results(potentials)

        co_fit_records = self._visible_co_fit_records()
        if co_fit_records:
            co_results = calculate_co_stark_slopes(
                co_fit_records,
                potentials=potentials if potentials else None,
            )
            self._co_stark_results = co_results
            if self.right_panel.get_mode() == 'CO' or current_subtab == 'CO':
                self.right_panel.update_co_stark_results(co_results)
            self.analysis_widget.update_co_plots(
                co_fit_records,
                potentials,
                co_results,
            )
        else:
            self._co_stark_results = []
            if self.right_panel.get_mode() == 'CO' or current_subtab == 'CO':
                self.right_panel.update_co_stark_results([])
            self.analysis_widget.update_co_plots([], potentials, [])

        self.analysis_widget.set_current_subtab(current_subtab)

    def _visible_entry_names(self) -> list[str]:
        return [entry.name for entry in self.spectrum_list.get_visible_entries()]

    def _visible_entries(self) -> list[SpectrumEntry]:
        return self.spectrum_list.get_visible_entries()

    def _visible_potentials(self) -> dict:
        return self.spectrum_list.get_potentials(visible_only=True)

    def _visible_fit_records(self) -> list:
        visible_names = set(self._visible_entry_names())
        return [
            record for record in self._fit_records
            if record.get('filename') in visible_names
        ]

    def _visible_co_fit_records(self) -> list:
        visible_names = set(self._visible_entry_names())
        return [
            record for record in self._co_fit_records
            if record.get('filename') in visible_names
        ]

    def _all_session_compare_records(self) -> list:
        potentials = self.spectrum_list.get_potentials()
        fit_record_by_name = {
            record.get('filename'): record
            for record in self._fit_records
        }
        records = []

        for entry in self.spectrum_list.get_all_entries():
            potential = potentials.get(entry.name)
            if potential is None:
                continue

            fit_record = fit_record_by_name.get(entry.name)
            if not fit_record:
                continue
            fit_result = fit_record.get('fit_result')
            if not fit_result or not getattr(fit_result, 'success', False):
                continue

            fractions = {}
            for peak in getattr(fit_result, 'peaks', []) or []:
                try:
                    fractions[int(peak.index)] = float(peak.area_fraction)
                except (TypeError, ValueError):
                    continue
            if not fractions:
                continue

            session_key = self.spectrum_list.get_session_key_for_entry(entry)
            session_label = self.spectrum_list.get_session_label_for_key(session_key)
            records.append({
                'session_key': session_key,
                'session_label': session_label,
                'filename': entry.name,
                'potential': float(potential),
                'area_fractions': fractions,
            })

        return records

    def _refresh_session_compare(self):
        self.analysis_widget.update_compare_plot(
            self._all_session_compare_records()
        )

    def _potential_color(self, entry: SpectrumEntry, potentials: dict,
                         pot_min: float | None, pot_max: float | None) -> str:
        potential = potentials.get(entry.name)
        if potential is None or pot_min is None or pot_max is None or pot_min == pot_max:
            return entry.color
        t = (potential - pot_min) / (pot_max - pot_min)
        t = min(max(t, 0.0), 1.0)
        hue = 0.58 * (1.0 - t)
        return QColor.fromHsvF(hue, 0.62, 0.95).name()

    def _build_total_specs(self) -> list[dict]:
        entries = self._visible_entries()
        potentials = self._visible_potentials()
        session_key = self.spectrum_list.get_current_session_filter()
        session_shifts = self._total_shifts.get(session_key, {})
        pot_values = [potentials[e.name] for e in entries if e.name in potentials]
        pot_min = min(pot_values) if pot_values else None
        pot_max = max(pot_values) if pot_values else None

        ordered = list(entries)
        if pot_values:
            ordered.sort(key=lambda e: potentials.get(e.name, float('inf')))

        ranges = [
            float(np.nanmax(e.absorbance) - np.nanmin(e.absorbance))
            for e in ordered if len(e.absorbance) > 0
        ]
        spacing = (np.median(ranges) * 1.25) if ranges else 1.0
        if not np.isfinite(spacing) or spacing <= 0:
            spacing = 1.0

        specs = []
        for idx, entry in enumerate(ordered):
            base_shift = idx * spacing if self._total_view_mode == 'stack' else 0.0
            specs.append({
                'name': entry.name,
                'filepath': entry.filepath,
                'wn': entry.wavenumber,
                'ab': entry.absorbance,
                'base_shift': base_shift,
                'shift': session_shifts.get(entry.name, 0.0),
                'color': self._potential_color(entry, potentials, pot_min, pot_max),
                'potential': potentials.get(entry.name),
            })
        return specs

    def _apply_total_view(self, preserve_view: bool = False):
        view_state = self._capture_plot_view() if preserve_view else None
        self.plot_widget.clear_endpoint_items()
        self.plot_widget.clear_analysis_region()
        self.plot_widget.clear_fit_result()
        self.plot_widget.clear_baseline_curve()
        specs = self._build_total_specs()
        active_name = self._current_entry.name if self._current_entry is not None else None
        self.plot_widget.show_total_spectra(specs, active_name=active_name)
        if view_state:
            self._restore_plot_view(view_state)
        self.status_label.setText(
            f"Total view  |  {len(specs)} spectra  |  {self._total_view_mode}"
        )

    def _on_total_view_changed(self, view_mode: str):
        self._total_view_mode = 'stack' if view_mode == 'stack' else 'overlay'
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=True)

    def _normalize_total_shifts(self, raw_shifts) -> dict[str, dict[str, float]]:
        if not isinstance(raw_shifts, dict):
            return {}
        if all(isinstance(v, dict) for v in raw_shifts.values()):
            return {
                str(session_key): {
                    str(name): float(shift)
                    for name, shift in shifts.items()
                }
                for session_key, shifts in raw_shifts.items()
                if isinstance(shifts, dict)
            }
        converted = {}
        for name, shift in raw_shifts.items():
            entry = next(
                (e for e in self.spectrum_list.get_all_entries() if e.name == name),
                None,
            )
            session_key = (
                self.spectrum_list.get_session_key_for_entry(entry)
                if entry is not None else self.spectrum_list.LOOSE_FILES_KEY
            )
            converted.setdefault(session_key, {})[str(name)] = float(shift)
        return converted

    def _on_total_shift_changed(self, spectrum_name: str, shift: float):
        session_key = self.spectrum_list.get_current_session_filter()
        self._total_shifts.setdefault(session_key, {})[spectrum_name] = shift

    def _on_total_spectrum_selected(self, spectrum_name: str):
        entries = self.spectrum_list.get_all_entries()
        for row, entry in enumerate(entries):
            if entry.name == spectrum_name:
                if self.spectrum_list.list_widget.currentRow() != row:
                    self.spectrum_list.list_widget.setCurrentRow(row)
                break

    def _reset_total_shifts(self):
        session_key = self.spectrum_list.get_current_session_filter()
        self._total_shifts.pop(session_key, None)
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=True)

    def _get_sio_area_for_filepath(self, filepath: str):
        state = self._sio_states.get(filepath, {})
        area = state.get('area')
        if area is not None:
            return area

        target_entry = next(
            (entry for entry in self.spectrum_list.get_all_entries() if entry.filepath == filepath),
            None,
        )
        if target_entry is not None:
            target_session = self.spectrum_list.get_session_key_for_entry(target_entry)
            for entry in self.spectrum_list.get_all_entries():
                if self.spectrum_list.get_session_key_for_entry(entry) != target_session:
                    continue
                session_state = self._sio_states.get(entry.filepath, {})
                session_area = session_state.get('area')
                if session_area is not None:
                    return session_area

        if len(self.spectrum_list.get_all_entries()) <= 1:
            return self._sio_ref_area
        return None

    def _get_sio_area_for_entry(self, entry: SpectrumEntry | None):
        if entry is None:
            return None
        return self._get_sio_area_for_filepath(entry.filepath)

    def _visible_sio_areas(self) -> dict:
        result = {}
        for entry in self.spectrum_list.get_visible_entries():
            area = self._get_sio_area_for_entry(entry)
            if area is not None:
                result[entry.name] = area
        return result

    def _auto_fit_target_entries(self) -> tuple[list[SpectrumEntry], str]:
        visible_entries = self.spectrum_list.get_visible_entries()
        if not visible_entries:
            return [], ""

        session_filter = self.spectrum_list.get_current_session_filter()
        return visible_entries, self.spectrum_list.get_session_label_for_key(session_filter)

    def _import_sessions(self):
        fps, _ = QFileDialog.getOpenFileNames(
            self, "Import Sessions", "",
            "In Situ IR Analyzer Session (*.irsession *.session)"
        )
        if fps:
            self._import_sessions_from_paths(fps)

    def _import_sessions_from_paths(self, fps: list[str]):
        if not fps:
            return

        loaded_payloads = []
        errors = []
        for fp in fps:
            try:
                loaded_payloads.append((fp, load_session(fp)))
            except Exception as e:
                errors.append(f"{Path(fp).name}: {e}")

        if not loaded_payloads:
            QMessageBox.critical(self, "Import Error", "\n\n".join(errors) if errors else "세션을 불러오지 못했습니다.")
            return

        first_new_index = None
        imported_sessions = 0
        imported_spectra = 0
        self._loading_session = True
        self.spectrum_list.begin_bulk_update()
        try:
            for fp, data in loaded_payloads:
                start_idx = len(self.spectrum_list.get_all_entries())
                count = self._merge_session_data(data, fp)
                if count <= 0:
                    continue
                if first_new_index is None:
                    first_new_index = start_idx
                imported_sessions += 1
                imported_spectra += count
        finally:
            self.spectrum_list.end_bulk_update()
            self._loading_session = False

        if imported_spectra == 0:
            if errors:
                QMessageBox.warning(self, "Import Error", "\n\n".join(errors))
            return

        self._refresh_after_session_merge()

        if first_new_index is not None:
            first_entry = self.spectrum_list.get_entry(first_new_index)
            if first_entry is not None:
                first_session = self.spectrum_list.get_session_key_for_entry(first_entry)
                self.spectrum_list.set_session_filter(first_session)
                self.spectrum_list.list_widget.setCurrentRow(first_new_index)
                self.right_panel.set_mode('Total')

        status = f"Imported {imported_sessions} session(s)  |  {imported_spectra} spectra added"
        if errors:
            status += f"  |  {len(errors)} failed"
            QMessageBox.warning(self, "Import Completed with Errors", "\n\n".join(errors))
        self.status_label.setText(status)

    def _merge_session_data(self, data: dict, session_path: str) -> int:
        spectra_data = data.get('spectra', [])
        if not spectra_data:
            return 0

        existing_labels = self._session_labels_in_use()
        fallback_label = Path(session_path).stem
        mapped_label = self._make_unique_session_label(fallback_label, existing_labels)

        names_in_use = {entry.name for entry in self.spectrum_list.get_all_entries()}
        keys_in_use = {entry.filepath for entry in self.spectrum_list.get_all_entries()}
        old_to_new_name = {}
        old_to_new_fp = {}
        imported_entries = []

        for idx, spectrum in enumerate(spectra_data):
            original_name = spectrum.get('original_name') or spectrum.get('name') or Path(spectrum.get('filepath', '')).name
            display_name = self._make_unique_display_name(mapped_label, original_name, names_in_use)
            source_filepath = spectrum.get('source_spectrum_path') or spectrum.get('filepath', '')
            import_key = self._make_import_key(mapped_label, original_name, source_filepath, idx, keys_in_use)

            old_to_new_name[spectrum.get('name', original_name)] = display_name
            old_to_new_fp[spectrum.get('filepath', source_filepath)] = import_key

            imported_entries.append(SpectrumEntry(
                filepath=import_key,
                name=display_name,
                wavenumber=spectrum['wavenumber'],
                absorbance=spectrum['absorbance'],
                color=spectrum.get('color', '#89b4fa'),
                fit_done=spectrum.get('fit_done', False),
                original_name=original_name,
                source_session_label=mapped_label,
                source_session_path=session_path,
                source_spectrum_path=source_filepath,
            ))

        for entry in imported_entries:
            self.spectrum_list.add_entry(entry, select=False, emit_signal=False)

        imported_potentials = data.get('potentials', {})
        merged_potentials = self.spectrum_list.get_potentials()
        for old_name, potential in imported_potentials.items():
            new_name = old_to_new_name.get(old_name)
            if new_name is not None:
                merged_potentials[new_name] = potential
        self.spectrum_list.set_potentials(
            [entry.name for entry in self.spectrum_list.get_all_entries()],
            merged_potentials,
            emit_changed=False,
        )

        for old_fp, state in data.get('spectrum_states', {}).items():
            new_fp = old_to_new_fp.get(old_fp)
            if new_fp is not None:
                self._spectrum_states[new_fp] = copy.deepcopy(state)

        for old_fp, state in data.get('co_states', {}).items():
            new_fp = old_to_new_fp.get(old_fp)
            if new_fp is not None:
                self._co_states[new_fp] = copy.deepcopy(state)

        for old_fp, state in data.get('sio_states', {}).items():
            new_fp = old_to_new_fp.get(old_fp)
            if new_fp is not None:
                self._sio_states[new_fp] = copy.deepcopy(state)

        for record in data.get('fit_records', []):
            new_name = old_to_new_name.get(record.get('filename'))
            if new_name is None:
                continue
            new_record = copy.deepcopy(record)
            new_record['filename'] = new_name
            self._fit_records.append(new_record)

        for record in data.get('co_fit_records', []):
            new_name = old_to_new_name.get(record.get('filename'))
            if new_name is None:
                continue
            new_record = copy.deepcopy(record)
            new_record['filename'] = new_name
            self._co_fit_records.append(new_record)

        imported_sio_ref = data.get('sio_ref_area')
        if imported_sio_ref is not None:
            self._sio_ref_area = imported_sio_ref

        imported_shifts = data.get('total_shifts', {})
        if isinstance(imported_shifts, dict):
            if all(isinstance(v, dict) for v in imported_shifts.values()):
                for old_session_key, shifts in imported_shifts.items():
                    for old_name, shift in shifts.items():
                        new_name = old_to_new_name.get(old_name)
                        if new_name is not None:
                            entry = next((e for e in imported_entries if e.name == new_name), None)
                            mapped_session = (
                                entry.source_session_label
                                if entry is not None and entry.source_session_label
                                else mapped_label
                            )
                            self._total_shifts.setdefault(mapped_session, {})[new_name] = float(shift)
            else:
                for old_name, shift in imported_shifts.items():
                    new_name = old_to_new_name.get(old_name)
                    if new_name is None:
                        continue
                    entry = next((e for e in imported_entries if e.name == new_name), None)
                    if entry is not None:
                        self._total_shifts.setdefault(entry.source_session_label or self.spectrum_list.LOOSE_FILES_KEY, {})[new_name] = float(shift)

        return len(imported_entries)

    def _setup_detachable_tab_widget(self, tabs: QTabWidget, context_handler):
        bar = _DetachableTabBar(tabs)
        tabs.setTabBar(bar)
        bar.detach_requested.connect(
            lambda index, global_pos, tab_widget=tabs:
                self._on_tab_detach_requested(tab_widget, index, global_pos)
        )
        bar.setContextMenuPolicy(Qt.CustomContextMenu)
        bar.customContextMenuRequested.connect(context_handler)

    def _analysis_is_detached(self) -> bool:
        return (
            self._analysis_window is not None
            and self._analysis_window.centralWidget() is self.analysis_widget
        )

    def _analysis_view_is_active(self) -> bool:
        if self._analysis_is_detached():
            return self._analysis_window.isVisible()
        if self._right_tabs is not None and self._right_tabs.indexOf(self.analysis_widget) != -1:
            return True
        return self.center_tabs.currentWidget() == self.analysis_widget

    def _show_analysis_view(self, preferred_subtab: str | None = None):
        self._suppress_analysis_tab_reset = True
        if self._analysis_is_detached():
            self.analysis_widget.show()
            if preferred_subtab:
                self.analysis_widget.set_current_subtab(preferred_subtab)
            self._analysis_window.showNormal()
            self._analysis_window.raise_()
            self._analysis_window.activateWindow()
            self._suppress_analysis_tab_reset = False
            return
        if self._right_tabs is not None and self._right_tabs.indexOf(self.analysis_widget) != -1:
            self.analysis_widget.show()
            self._right_tabs.setCurrentWidget(self.analysis_widget)
            if preferred_subtab:
                self.analysis_widget.set_current_subtab(preferred_subtab)
            self._suppress_analysis_tab_reset = False
            return
        if self.center_tabs.indexOf(self.analysis_widget) != -1:
            self.analysis_widget.show()
            self.center_tabs.setCurrentWidget(self.analysis_widget)
            if preferred_subtab:
                self.analysis_widget.set_current_subtab(preferred_subtab)
        self._suppress_analysis_tab_reset = False

    def _on_tab_changed(self, tabs: QTabWidget, index: int):
        if self._suppress_analysis_tab_reset:
            return

    def _update_split_button(self):
        if not hasattr(self, '_btn_split'):
            return
        if self._analysis_is_detached():
            self._btn_split.setText("↩")
            self._btn_split.setToolTip("Detached Analysis 창을 다시 붙이기")
        elif self._is_split:
            self._btn_split.setText("⊟")
            self._btn_split.setToolTip("분할 보기 해제")
        else:
            self._btn_split.setText("⊞")
            self._btn_split.setToolTip("Analysis 탭을 오른쪽에 분할 표시")

    def _on_tab_detach_requested(self, tab_widget: QTabWidget, index: int, global_pos):
        if tab_widget.widget(index) is not self.analysis_widget:
            return
        self._detach_analysis_to_window(tab_widget, global_pos)

    def _place_analysis_window(self, global_pos=None):
        if self._analysis_window is None:
            return
        if self._analysis_window_geometry:
            self._analysis_window.restoreGeometry(self._analysis_window_geometry)
            return

        screen = QApplication.screenAt(global_pos) if global_pos is not None else self.screen()
        if screen is None:
            self._analysis_window.resize(980, 760)
            return

        geom = screen.availableGeometry()
        width = min(max(int(geom.width() * 0.58), 820), geom.width())
        height = min(max(int(geom.height() * 0.74), 620), geom.height())
        self._analysis_window.resize(width, height)

        if global_pos is None:
            pos = geom.center() - self._analysis_window.rect().center()
            self._analysis_window.move(pos)
            return

        x = max(geom.left(), min(global_pos.x() - 120, geom.right() - width))
        y = max(geom.top(), min(global_pos.y() - 24, geom.bottom() - height))
        self._analysis_window.move(x, y)

    def _detach_analysis_to_window(self, source_tabs: QTabWidget | None = None, global_pos=None):
        if self._analysis_is_detached():
            self._show_analysis_view()
            return

        if source_tabs is None:
            if self._right_tabs is not None and self._right_tabs.indexOf(self.analysis_widget) != -1:
                source_tabs = self._right_tabs
            else:
                source_tabs = self.center_tabs

        index = source_tabs.indexOf(self.analysis_widget)
        if index == -1:
            return

        source_tabs.removeTab(index)

        if source_tabs is self._right_tabs and self._is_split:
            self._analysis_restore_target = 'center'
            self._unsplit_view()
        else:
            self._analysis_restore_target = 'center'

        if self._analysis_window is None:
            self._analysis_window = _DetachedAnalysisWindow(self)
            self._analysis_window.close_requested.connect(self._reattach_analysis_from_window)

        self._analysis_window.takeCentralWidget()
        self._analysis_window.setCentralWidget(self.analysis_widget)
        self.analysis_widget.show()
        self._place_analysis_window(global_pos)
        self._analysis_window.show()
        self._analysis_window.raise_()
        self._analysis_window.activateWindow()
        self._update_split_button()
        self.status_label.setText("Analysis 창 분리  |  다른 모니터로 옮겨서 사용할 수 있습니다.")

    def _reattach_analysis_from_window(self):
        if not self._analysis_is_detached():
            return

        self._analysis_window_geometry = self._analysis_window.saveGeometry()
        self._analysis_window.takeCentralWidget()

        target_tabs = self.center_tabs
        if (
            self._analysis_restore_target == 'split'
            and self._is_split
            and self._right_tabs is not None
        ):
            target_tabs = self._right_tabs

        if target_tabs.indexOf(self.analysis_widget) == -1:
            target_tabs.addTab(self.analysis_widget, "  Analysis  ")
        self.analysis_widget.show()
        self._analysis_window.hide()
        self._show_analysis_view()
        self._update_split_button()
        self.status_label.setText("Analysis 창 다시 붙임")

    # ── 파일 열기 ─────────────────────────────────────────────

    def _session_potential_count(self, session_key: str, potentials: dict,
                                 exclude_names: set[str] | None = None) -> int:
        exclude_names = exclude_names or set()
        return sum(
            1
            for entry in self.spectrum_list.get_all_entries()
            if (
                self.spectrum_list.get_session_key_for_entry(entry) == session_key
                and entry.name in potentials
                and entry.name not in exclude_names
            )
        )

    def _on_spectrum_added(self, entry):
        """새 스펙트럼 로드 시 Potential 테이블에 자동 할당 (-0.1 × n V)"""
        if self._loading_session:
            return
        potentials = self.spectrum_list.get_potentials()
        session_key = self.spectrum_list.get_session_key_for_entry(entry)
        n = self._session_potential_count(session_key, potentials, {entry.name})
        potential = -0.1 * (n + 1)
        self.spectrum_list.add_spectrum_potential(entry.name, potential)
        self._initialize_auto_analysis_defaults(entry)
        self._sync_analysis_sidebar()
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=False)
        else:
            self._refresh_current_mode_view_after_defaults()

    def _on_spectra_added(self, entries: list[SpectrumEntry]):
        """여러 raw 파일 추가 시 비싼 UI 갱신을 한 번만 수행."""
        if self._loading_session or not entries:
            return

        potentials = self.spectrum_list.get_potentials()
        new_names = {entry.name for entry in entries}
        session_counts: dict[str, int] = {}
        for entry in entries:
            session_key = self.spectrum_list.get_session_key_for_entry(entry)
            if session_key not in session_counts:
                session_counts[session_key] = self._session_potential_count(
                    session_key, potentials, new_names
                )
            session_counts[session_key] += 1
            potentials[entry.name] = -0.1 * session_counts[session_key]
            self._initialize_auto_analysis_defaults(entry)

        self.spectrum_list.set_potentials(
            [entry.name for entry in self.spectrum_list.get_all_entries()],
            potentials,
            emit_changed=False,
        )
        self._sync_analysis_sidebar()
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=False)
        else:
            self._refresh_current_mode_view_after_defaults()

    def _initialize_auto_analysis_defaults(self, entry: SpectrumEntry):
        cfg = self.right_panel.get_config()
        wn_oh, ab_oh = crop_region(
            entry.wavenumber, entry.absorbance, cfg['wn_min'], cfg['wn_max'])
        auto_points = auto_oh_baseline_points(wn_oh, ab_oh)
        baseline = (baseline_from_points(wn_oh, ab_oh, auto_points)
                    if len(auto_points) >= 2 else np.zeros_like(ab_oh))

        self._spectrum_states.setdefault(entry.filepath, {
            'wn_crop': wn_oh.copy(),
            'ab_crop': ab_oh.copy(),
            'baseline': baseline.copy(),
            'ab_corrected': subtract_baseline(ab_oh, baseline),
            'fit_result': None,
            'guesses': [],
            'locks': [],
            'baseline_points': list(auto_points),
            'snapshots': [],
        })

    def _refresh_current_mode_view_after_defaults(self):
        row = self.spectrum_list.list_widget.currentRow()
        entry = self.spectrum_list.get_entry(row) if row >= 0 else self._current_entry
        if entry is None:
            return
        self._on_spectrum_selected(entry)

        co_eps = auto_co_baseline_endpoints(entry.wavenumber, entry.absorbance)
        co_state = self._co_states.setdefault(entry.filepath, {})
        for sub, default_eps in [('CO_L', (2000.0, 2100.0)), ('CO_B', (1650.0, 1900.0))]:
            ep0, ep1 = co_eps.get(sub, default_eps)
            sub_state = co_state.setdefault(sub, {})
            sub_state.setdefault('ep0', ep0)
            sub_state.setdefault('ep1', ep1)
            sub_state.setdefault('fit_result', None)
            sub_state.setdefault('manual_override', False)

    def _on_spectrum_removed(self, index: int, filepath: str, name: str):
        self.spectrum_list.remove_spectrum_potential(name, emit_changed=False)

        # 상태 정리
        self._spectrum_states.pop(filepath, None)
        self._fit_records = [r for r in self._fit_records
                             if r['filename'] != name]
        self._co_fit_records = [r for r in self._co_fit_records
                                if r['filename'] != name]
        self._co_states.pop(filepath, None)
        self._sio_states.pop(filepath, None)
        for shifts in self._total_shifts.values():
            shifts.pop(name, None)

        # 제거된 스펙트럼이 현재 표시 중이면 화면 초기화
        if self._current_entry is not None and self._current_entry.filepath == filepath:
            self._clear_display()
        self._on_potential_assignments_changed()

    def _on_spectra_reordered(self):
        names = [entry.name for entry in self.spectrum_list.get_all_entries()]
        order = {name: idx for idx, name in enumerate(names)}

        self._fit_records.sort(key=lambda record: order.get(record.get('filename'), len(order)))
        self._co_fit_records.sort(key=lambda record: order.get(record.get('filename'), len(order)))
        self.spectrum_list.set_potentials(
            names,
            self.spectrum_list.get_potentials(),
            emit_changed=False,
        )

        self._on_potential_assignments_changed()
        self.status_label.setText("Spectra 순서 변경됨")

    def _sync_analysis_sidebar(self):
        spectra_names = self._visible_entry_names()
        current_name = (
            self._current_entry.name
            if self._current_entry is not None and self.spectrum_list.entry_belongs_to_current_filter(self._current_entry)
            else None
        )
        self.analysis_widget.set_potential_assignments(
            spectra_names,
            self._visible_potentials(),
            current_filename=current_name,
        )

    def _on_analysis_assignment_selected(self, spectrum_name: str):
        entries = self.spectrum_list.get_all_entries()
        for row, entry in enumerate(entries):
            if entry.name == spectrum_name:
                if self.spectrum_list.list_widget.currentRow() != row:
                    self.spectrum_list.list_widget.setCurrentRow(row)
                break

    def _on_potential_assignments_changed(self):
        if self._loading_session:
            return

        self._refresh_analysis_for_visible_session()
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=True)
            return

    def _on_session_filter_changed(self, _session_key: str):
        if (
            self._current_entry is not None
            and not self.spectrum_list.entry_belongs_to_current_filter(self._current_entry)
        ):
            self._clear_display()
        self._refresh_analysis_for_visible_session()
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=False)
            return

    def _on_workspace_created(self, session_key: str):
        self._clear_display()
        self._refresh_analysis_for_visible_session()
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=False)
        label = self.spectrum_list.get_session_label_for_key(session_key)
        self.status_label.setText(f"Workspace created: {label}")

    def _clear_display(self):
        """스펙트럼 표시 영역 전체 초기화"""
        self._current_entry   = None
        self._fit_result      = None
        self._fit_edit_pending = False
        self._baseline        = None
        self._ab_corrected    = None
        self._wn_crop         = None
        self._ab_crop         = None
        self._baseline_points = []
        self.plot_widget._clear_all()
        self.right_panel.clear_current_summary()
        self.right_panel.set_snapshot_names([], selected_index=-1)
        self.right_panel.btn_snapshot_save.setEnabled(False)
        self.setWindowTitle("In Situ IR Analyzer")
        self.status_label.setText(
            "Ready  —  drag CSV files onto the sidebar to load"
        )
        self._sync_analysis_sidebar()

    def _capture_plot_view(self):
        if self.right_panel.get_mode() == 'Total':
            return self.plot_widget.get_view_state()
        if self._current_entry is None:
            return None
        return self.plot_widget.get_view_state()

    def _restore_plot_view(self, view_state):
        if view_state is not None:
            self.plot_widget.restore_view_state(view_state)

    def _build_overlay_spectra(self, entries):
        mode = self.right_panel.get_mode()
        overlays = []
        for entry in entries:
            wn = entry.wavenumber
            ab = entry.absorbance

            if mode == 'OH':
                state = self._spectrum_states.get(entry.filepath)
                if state is not None:
                    wn = state.get('wn_crop', wn)
                    ab = state.get('ab_corrected', ab)
                kind = 'corrected'
            else:
                kind = 'raw'

            overlays.append({
                'filepath': entry.filepath,
                'color': entry.color,
                'wn': wn,
                'ab': ab,
                'kind': kind,
            })
        return overlays

    def _refresh_selected_overlays(self, view_state=None):
        if self.right_panel.get_mode() == 'Total':
            return
        selected_entries = self.spectrum_list.get_selected_entries()
        active_path = self._current_entry.filepath if self._current_entry is not None else None
        overlays = self._build_overlay_spectra(selected_entries)
        self.plot_widget.set_overlay_spectra(overlays, active_path)
        self._restore_plot_view(view_state)

    def _get_oh_snapshots(self, filepath: str | None = None):
        if filepath is None:
            if self._current_entry is None:
                return []
            filepath = self._current_entry.filepath
        state = self._spectrum_states.setdefault(filepath, {})
        return state.setdefault('snapshots', [])

    def _capture_current_oh_snapshot_state(self):
        if self._current_entry is None or self._wn_crop is None:
            return None
        ab_crop = self._ab_crop.copy() if self._ab_crop is not None else self._current_entry.absorbance.copy()
        baseline = self._baseline.copy() if self._baseline is not None else np.zeros_like(ab_crop)
        ab_corrected = self._ab_corrected.copy() if self._ab_corrected is not None else ab_crop.copy()
        return {
            'wn_crop': self._wn_crop.copy(),
            'ab_crop': ab_crop,
            'baseline': baseline,
            'ab_corrected': ab_corrected,
            'fit_result': copy.deepcopy(self._fit_result),
            'guesses': copy.deepcopy(self.right_panel.get_guesses()),
            'locks': copy.deepcopy(self.right_panel.get_locks()),
            'baseline_points': copy.deepcopy(self._baseline_points),
            'config': copy.deepcopy(self.right_panel.get_config()),
            'n_peaks': self.right_panel.get_n_peaks(),
            'baseline_edit_enabled': self.right_panel.btn_edit_bl.isChecked(),
        }

    def _refresh_snapshot_panel(self, selected_index: int = -1):
        can_save = (
            self._current_entry is not None
            and self.right_panel.get_mode() == 'OH'
            and self._wn_crop is not None
        )
        self.right_panel.btn_snapshot_save.setEnabled(can_save)
        if not can_save:
            self.right_panel.set_snapshot_names([], selected_index=-1)
            return
        snapshots = self._get_oh_snapshots(self._current_entry.filepath)
        names = [snap['label'] for snap in snapshots]
        self.right_panel.set_snapshot_names(names, selected_index=selected_index)

    def _save_current_snapshot(self):
        if (self._current_entry is None
                or self.right_panel.get_mode() != 'OH'
                or self._wn_crop is None):
            return

        snapshots = self._get_oh_snapshots(self._current_entry.filepath)
        snapshot_state = self._capture_current_oh_snapshot_state()
        if snapshot_state is None:
            return

        label = f"Snapshot {len(snapshots) + 1} · {datetime.now():%H:%M:%S}"
        snapshots.append({
            'label': label,
            'state': snapshot_state,
        })
        if len(snapshots) > MAX_OH_SNAPSHOTS:
            snapshots.pop(0)

        self._refresh_snapshot_panel(selected_index=len(snapshots) - 1)
        self.status_label.setText(f"{self._current_entry.name}  |  saved {label}")

    def _restore_snapshot(self, index: int):
        if self._current_entry is None or self.right_panel.get_mode() != 'OH':
            return
        snapshots = self._get_oh_snapshots(self._current_entry.filepath)
        if not (0 <= index < len(snapshots)):
            return

        snapshot_state = copy.deepcopy(snapshots[index]['state'])
        config = snapshot_state.get('config', self.right_panel.get_config())
        guesses = snapshot_state.get('guesses', [])
        locks = snapshot_state.get('locks', [])
        view_state = self._capture_plot_view()

        self._refit_timer.stop()
        self._fit_edit_pending = False
        self._fit_result = snapshot_state.get('fit_result')
        self._wn_crop = snapshot_state.get('wn_crop')
        self._ab_crop = snapshot_state.get('ab_crop')
        self._baseline = snapshot_state.get('baseline')
        self._ab_corrected = snapshot_state.get('ab_corrected')
        self._baseline_points = list(snapshot_state.get('baseline_points', []))

        self.right_panel.set_wavenumber_range(
            self._current_entry.wavenumber[0],
            self._current_entry.wavenumber[-1],
        )
        self.right_panel.set_oh_snapshot_config(
            config,
            snapshot_state.get('n_peaks'),
            baseline_edit_enabled=snapshot_state.get('baseline_edit_enabled'),
        )

        self.plot_widget.set_raw_spectrum(
            self._current_entry.wavenumber,
            self._current_entry.absorbance,
        )
        self.plot_widget.set_corrected_spectrum(self._wn_crop, self._ab_corrected, self._baseline)
        self.plot_widget.show_analysis_region([(config['wn_min'], config['wn_max'])])
        if self._baseline_points:
            self.plot_widget.restore_baseline_points(self._baseline_points)

        self.right_panel.set_guesses(guesses, locks=locks)

        if self._fit_result is not None:
            ab = np.maximum(self._ab_corrected, 0)
            self.plot_widget.show_fit_result(self._wn_crop, ab, self._fit_result)
            self.right_panel.update_results(self._fit_result)
            total_oh = sum(p.area for p in self._fit_result.peaks)
            self.right_panel.update_oh_total_area(total_oh, self._get_sio_area_for_entry(self._current_entry))
            self.spectrum_list.mark_fit_done(self.spectrum_list.list_widget.currentRow())
        else:
            self.plot_widget.show_peak_guesses(self._wn_crop, guesses)
            self.right_panel.clear_results()
            total_oh = abs(float(_trapezoid(self._ab_corrected, self._wn_crop)))
            self.right_panel.update_oh_total_area(total_oh, self._get_sio_area_for_entry(self._current_entry))
            self.spectrum_list.clear_fit_done(self.spectrum_list.list_widget.currentRow())

        self._spectrum_states[self._current_entry.filepath] = {
            'wn_crop': self._wn_crop.copy(),
            'ab_crop': self._ab_crop.copy(),
            'baseline': self._baseline.copy(),
            'ab_corrected': self._ab_corrected.copy(),
            'fit_result': copy.deepcopy(self._fit_result),
            'guesses': copy.deepcopy(self.right_panel.get_guesses()),
            'locks': copy.deepcopy(self.right_panel.get_locks()),
            'baseline_points': list(self._baseline_points),
            'snapshots': snapshots,
        }

        self._sync_baseline_edit_state_for_current_spectrum()
        self.plot_widget.set_peak_locks(self.right_panel.get_locks())
        self._refresh_selected_overlays(view_state)
        self._refresh_snapshot_panel(selected_index=index)
        self.status_label.setText(
            f"{self._current_entry.name}  |  restored {snapshots[index]['label']}"
        )

    def _delete_snapshot(self, index: int):
        if self._current_entry is None or self.right_panel.get_mode() != 'OH':
            return
        snapshots = self._get_oh_snapshots(self._current_entry.filepath)
        if not (0 <= index < len(snapshots)):
            return

        label = snapshots.pop(index)['label']
        next_index = min(index, len(snapshots) - 1)
        self._refresh_snapshot_panel(selected_index=next_index)
        self.status_label.setText(f"{self._current_entry.name}  |  deleted {label}")

    def _on_spectrum_selection_changed(self, _entries):
        if self._current_entry is None:
            return
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=True)
            return
        view_state = self._capture_plot_view()
        self._refresh_selected_overlays(view_state)

    def _open_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Open IR Spectra", "",
            "IR Spectra (*.csv *.txt *.asc *.dpt);;All Files (*)"
        )
        self.spectrum_list.add_files(files)

    # ── 스펙트럼 선택 ─────────────────────────────────────────

    def _on_spectrum_selected(self, entry: SpectrumEntry):
        view_state = self._capture_plot_view()

        # 스펙트럼 전환 전 현재 상태 저장 (베이스라인 포인트 포함)
        self._save_current_spectrum_state()

        self._current_entry   = entry
        self._fit_result      = None
        self._fit_edit_pending = False
        self._baseline        = np.zeros_like(entry.absorbance)
        self._ab_corrected    = entry.absorbance.copy()
        self._wn_crop         = None
        self._ab_crop         = None
        self._baseline_points = []
        self.right_panel.set_wavenumber_range(entry.wavenumber[0], entry.wavenumber[-1])
        self.setWindowTitle(f"In Situ IR Analyzer  —  {entry.name}")
        mode = self.right_panel.get_mode()

        if mode == 'Total':
            self.right_panel.set_guesses([], locks=[])
            self.right_panel.clear_results()
            if not self.plot_widget.set_total_active_spectrum(entry.name):
                self._apply_total_view(preserve_view=True)
            self._sync_analysis_sidebar()
            return

        self.plot_widget.clear_baseline_points()
        # set_raw_spectrum 이 _clear_all() 을 호출하므로 Total 모드가 아닐 때만 실행
        self.plot_widget.set_raw_spectrum(entry.wavenumber, entry.absorbance)

        # 저장된 상태가 있으면 복원
        saved = self._spectrum_states.get(entry.filepath)
        if saved:
            self._wn_crop         = saved['wn_crop']
            self._ab_crop         = saved['ab_crop']
            self._baseline        = saved['baseline']
            self._ab_corrected    = saved['ab_corrected']
            self._fit_result      = saved['fit_result']
            self._baseline_points = list(saved['baseline_points'])

            if mode == 'OH':
                self.plot_widget.set_corrected_spectrum(
                    self._wn_crop, self._ab_corrected, self._baseline)
                if self._baseline_points:
                    self.plot_widget.restore_baseline_points(self._baseline_points)
                self.right_panel.set_guesses(saved['guesses'], locks=saved.get('locks', []))

                if self._fit_result is not None:
                    ab = np.maximum(self._ab_corrected, 0)
                    self.plot_widget.show_fit_result(self._wn_crop, ab, self._fit_result)
                    self.right_panel.update_results(self._fit_result)
                    total_oh = sum(p.area for p in self._fit_result.peaks)
                    self.right_panel.update_oh_total_area(total_oh, self._get_sio_area_for_entry(entry))
                    self.status_label.setText(
                        f"{entry.name}  |  R²={self._fit_result.r_squared:.5f} (restored)"
                    )
                else:
                    self.plot_widget.show_peak_guesses(self._wn_crop, saved['guesses'])
                    self.right_panel.clear_results()
                    if self._ab_corrected is not None and self._wn_crop is not None:
                        total_oh = abs(float(_trapezoid(self._ab_corrected, self._wn_crop)))
                        self.right_panel.update_oh_total_area(total_oh, self._get_sio_area_for_entry(entry))
                    self.status_label.setText(
                        f"{entry.name}  |  {len(entry.wavenumber)} pts  "
                        f"|  {entry.wavenumber[0]:.0f}–{entry.wavenumber[-1]:.0f} cm⁻¹  "
                        f"(피크 위치 복원됨)"
                    )
        else:
            self.right_panel.set_guesses([], locks=[])
            self.right_panel.clear_results()
            self.status_label.setText(
                f"{entry.name}  |  {len(entry.wavenumber)} pts  "
                f"|  {entry.wavenumber[0]:.0f}–{entry.wavenumber[-1]:.0f} cm⁻¹"
            )
            # 저장 상태 없이 처음 로드 시 → OH 모드에서만 baseline 계산
            if mode == 'OH':
                self._update_baseline()

        # 분석 영역 표시 복원 + 줌
        cfg = self.right_panel.get_config()
        if mode == 'OH':
            wn_min, wn_max = cfg['wn_min'], cfg['wn_max']
            self.plot_widget.show_analysis_region([(wn_min, wn_max)])
            self.plot_widget.zoom_to(wn_min, wn_max)
            # baseline 재계산 → corrected spectrum + wn_crop 갱신
            if not saved:
                self._update_baseline()
        elif mode == 'CO':
            self._apply_co_view(entry)
        elif mode == 'SiO':
            self._apply_sio_view(entry)

        self._sync_baseline_edit_state_for_current_spectrum()
        self.plot_widget.set_peak_locks(self.right_panel.get_locks())
        self._refresh_selected_overlays(view_state)
        self._refresh_snapshot_panel()
        self._sync_analysis_sidebar()

    def _apply_co_view(self, entry: SpectrumEntry | None, preserve_view: bool = False):
        cfg = self.right_panel.get_config()
        view_state = self.plot_widget.get_view_state() if preserve_view else None
        self._co_drag_targets = {}
        self.plot_widget.clear_fit_result()
        self.plot_widget.clear_baseline_curve()
        self.plot_widget.clear_analysis_region()
        if entry is None:
            if not preserve_view:
                self.plot_widget.zoom_to(cfg['wn_min'], cfg['wn_max'])
            self.right_panel.set_co_guesses([])
            return

        wn, ab = crop_region(entry.wavenumber, entry.absorbance,
                             cfg['wn_min'], cfg['wn_max'])

        co_state = self._co_states.get(entry.filepath, {})
        if not co_state or 'CO_L' not in co_state or 'CO_B' not in co_state:
            auto_eps = auto_co_baseline_endpoints(entry.wavenumber, entry.absorbance)
            co_state = self._co_states.setdefault(entry.filepath, {})
            for sub, default_eps in [('CO_L', (2000.0, 2100.0)), ('CO_B', (1650.0, 1900.0))]:
                ep0, ep1 = auto_eps.get(sub, default_eps)
                sub_state = co_state.setdefault(sub, {})
                sub_state.setdefault('ep0', ep0)
                sub_state.setdefault('ep1', ep1)
                sub_state.setdefault('fit_result', None)
                sub_state.setdefault('manual_override', False)
        l_eps = (co_state.get('CO_L', {}).get('ep0', 2000.0),
                 co_state.get('CO_L', {}).get('ep1', 2100.0))
        b_eps = (co_state.get('CO_B', {}).get('ep0', 1650.0),
                 co_state.get('CO_B', {}).get('ep1', 1900.0))

        full_baseline = None
        if self._co_uses_endpoint_linear_baseline(cfg.get('baseline_algo')):
            self.plot_widget.show_highlighted_region(wn, ab)
        else:
            full_baseline = self._compute_co_full_baseline(
                entry,
                algo=cfg['baseline_algo'],
                params=cfg['baseline_params'],
                restore_points=self.right_panel.btn_edit_bl.isChecked(),
            )
            _, _, bl, ab_corr, _ = full_baseline
            self.plot_widget.set_corrected_spectrum(wn, ab_corr, bl)

        self.plot_widget.show_co_baselines(
            entry.wavenumber,
            entry.absorbance,
            l_eps,
            b_eps,
            draw_baseline=full_baseline is None,
        )

        co_locks = co_state.get('CO_B', {}).get('locks', [])
        raw_fit = co_state.get('CO_B', {}).get('raw_fit_result')
        if raw_fit and raw_fit.success:
            self._restore_co_b_fit_viz(entry, co_state)
            self.right_panel.set_co_guesses(co_state.get('CO_B', {}).get('guesses', []), locks=co_locks)
        else:
            co_b_guesses = co_state.get('CO_B', {}).get('guesses')
            if co_b_guesses and self.right_panel.get_co_b_fit_mode() != 'simple_only':
                ep0_b, ep1_b = sorted(b_eps)
                wn_b, _, bl_b, _ = self._co_subregion_data(
                    entry, ep0_b, ep1_b, full_baseline=full_baseline)
                if len(wn_b) >= 5:
                    self._co_drag_targets = {
                        i: {'type': 'co_b_guess', 'sub': 'CO_B', 'peak_idx': i}
                        for i in range(len(co_b_guesses))
                    }
                    self.plot_widget.show_peak_guesses(
                        wn_b,
                        co_b_guesses,
                        baseline=self._co_display_baseline(bl_b, full_baseline),
                    )
                self.right_panel.set_co_guesses(co_b_guesses, locks=co_locks)
            else:
                self.right_panel.set_co_guesses([])

        co_l = co_state.get('CO_L', {}).get('fit_result')
        co_b = co_state.get('CO_B', {}).get('fit_result')
        self.right_panel.update_co_results(co_l, co_b)
        if preserve_view and view_state:
            self.plot_widget.restore_view_state(view_state)
        else:
            self.plot_widget.zoom_to(cfg['wn_min'], cfg['wn_max'])

    def _apply_sio_view(self, entry: SpectrumEntry | None):
        cfg = self.right_panel.get_config()
        self.plot_widget.clear_fit_result()
        self.plot_widget.clear_baseline_curve()
        self.plot_widget.show_analysis_region([(cfg['wn_min'], cfg['wn_max'])])
        if entry is None:
            self.plot_widget.zoom_to(cfg['wn_min'], cfg['wn_max'])
            return

        wn, ab = crop_region(entry.wavenumber, entry.absorbance,
                             cfg['wn_min'], cfg['wn_max'])
        self.plot_widget.show_highlighted_region(wn, ab)

        sio_state = self._sio_states.get(entry.filepath, {})
        eps = (sio_state.get('ep0', 1100.0), sio_state.get('ep1', 1300.0))
        self.plot_widget.show_sio_baseline(entry.wavenumber, entry.absorbance, eps)
        self.plot_widget.zoom_to(cfg['wn_min'], cfg['wn_max'])

        self.right_panel.update_sio_area(self._get_sio_area_for_entry(entry))

    # ── 영역 / 베이스라인 ─────────────────────────────────────

    def _on_region_changed(self, wn_min, wn_max):
        mode = self.right_panel.get_mode()
        if mode == 'CO':
            self.plot_widget.clear_analysis_region()
        else:
            self.plot_widget.show_analysis_region([(wn_min, wn_max)])

        if mode == 'Total':
            self.plot_widget.clear_analysis_region()
            self.plot_widget.zoom_to(wn_min, wn_max)
        elif mode == 'OH':
            self._apply_oh_region_to_all_spectra(wn_min, wn_max)
            self._update_baseline()
            self._refresh_oh_stark_results(self._visible_potentials())
        elif mode == 'CO':
            if self._current_entry is not None:
                self._apply_co_view(self._current_entry)
            self.plot_widget.zoom_to(wn_min, wn_max)

    def _apply_oh_region_to_all_spectra(self, wn_min: float, wn_max: float):
        cfg = self.right_panel.get_config()
        algo = cfg['baseline_algo']
        params = cfg['baseline_params']
        affected_names = set()

        for entry in self.spectrum_list.get_all_entries():
            wn, ab = crop_region(entry.wavenumber, entry.absorbance, wn_min, wn_max)
            if len(wn) == 0:
                continue

            existing = self._spectrum_states.get(entry.filepath, {})
            manual_points = [
                pt for pt in existing.get('baseline_points', [])
                if wn_min <= pt[0] <= wn_max or wn_max <= pt[0] <= wn_min
            ]
            manual_override = bool(existing.get('baseline_manual_override', False))

            if algo == 'Manual' or (algo == 'OH Auto Baseline' and manual_override and len(manual_points) >= 2):
                baseline_points = list(manual_points)
                bl = (baseline_from_points(wn, ab, baseline_points)
                      if len(baseline_points) >= 2 else np.zeros_like(ab))
            elif algo == 'OH Auto Baseline':
                baseline_points = auto_oh_baseline_points(wn, ab)
                bl = (baseline_from_points(wn, ab, baseline_points)
                      if len(baseline_points) >= 2 else np.zeros_like(ab))
                manual_override = False
            elif algo == 'Rubber Band':
                baseline_points = []
                bl = baseline_rubberband(wn, ab)
                manual_override = False
            elif algo == 'ARPLS':
                baseline_points = []
                bl = baseline_arpls(ab, lam=params.get('lam', 1e4))
                manual_override = False
            elif algo == 'SNIP':
                baseline_points = []
                bl = baseline_snip(ab, n_iter=params.get('n_iter', 50))
                manual_override = False
            elif algo == 'Linear':
                baseline_points = []
                bl = baseline_linear(wn, ab)
                manual_override = False
            else:
                baseline_points = []
                bl = np.zeros_like(ab)
                manual_override = False

            snapshots = existing.get('snapshots', [])
            self._spectrum_states[entry.filepath] = {
                'wn_crop':        wn.copy(),
                'ab_crop':        ab.copy(),
                'baseline':       bl.copy(),
                'ab_corrected':   subtract_baseline(ab, bl),
                'fit_result':     None,
                'guesses':        [],
                'locks':          existing.get('locks', []),
                'baseline_points': list(baseline_points),
                'baseline_manual_override': manual_override,
                'snapshots':      snapshots,
            }
            affected_names.add(entry.name)

        if affected_names:
            self._fit_records = [
                r for r in self._fit_records
                if r.get('filename') not in affected_names
            ]
        elif mode == 'SiO':
            self.plot_widget.clear_baseline_curve()
            if self._current_entry is not None:
                wn, ab = crop_region(
                    self._current_entry.wavenumber,
                    self._current_entry.absorbance,
                    wn_min, wn_max
                )
                self.plot_widget.show_highlighted_region(wn, ab)
                sio_state = self._sio_states.get(self._current_entry.filepath, {})
                eps = (sio_state.get('ep0', 1100.0), sio_state.get('ep1', 1300.0))
                self.plot_widget.show_sio_baseline(
                    self._current_entry.wavenumber, self._current_entry.absorbance, eps)
            self.plot_widget.zoom_to(wn_min, wn_max)

    def _on_bl_mode_toggled(self, enabled: bool):
        """Edit Baseline 버튼 ON/OFF"""
        cfg = self.right_panel.get_config()
        mode = self.right_panel.get_mode()
        is_editable = cfg['baseline_algo'] in ('Manual', 'OH Auto Baseline')
        self.plot_widget.set_baseline_edit_mode(enabled and is_editable)
        if enabled:
            if mode == 'CO':
                self._update_co_baseline_for_current(
                    algo=cfg['baseline_algo'],
                    params=cfg['baseline_params'],
                )
            else:
                self._update_baseline()

    def _on_bl_apply(self, algo: str, params: dict):
        """알고리즘 또는 파라미터 변경 시 즉시 재계산"""
        if self.right_panel.get_mode() == 'CO':
            current_store = (
                self._co_baseline_store(self._current_entry)
                if self._current_entry is not None else {}
            )
            if algo == 'OH Auto Baseline' and current_store.get('manual_override'):
                algo = 'Manual'
            is_manual = (algo == 'Manual')
            is_oh_auto = (algo == 'OH Auto Baseline')
            bl_on = self.right_panel.btn_edit_bl.isChecked()
            self.plot_widget.set_baseline_edit_mode(bl_on and (is_manual or is_oh_auto))
            if self._current_entry is None:
                return
            if not (is_manual or is_oh_auto):
                self._co_baseline_store(self._current_entry)['points'] = []
                self._co_baseline_store(self._current_entry)['manual_override'] = False
                self.plot_widget.clear_baseline_points()
            self._update_co_baseline_for_current(
                algo=algo,
                params=params,
                manual_override=(True if is_manual else None),
            )
            return

        current_state = (
            self._spectrum_states.get(self._current_entry.filepath, {})
            if self._current_entry is not None else {}
        )
        if algo == 'OH Auto Baseline' and current_state.get('baseline_manual_override'):
            algo = 'Manual'

        is_manual = (algo == 'Manual')
        is_oh_auto = (algo == 'OH Auto Baseline')
        bl_on = self.right_panel.btn_edit_bl.isChecked()
        self.plot_widget.set_baseline_edit_mode(bl_on and (is_manual or is_oh_auto))
        if not (is_manual or is_oh_auto):
            # 자동 알고리즘 전환 시 수동 포인트 초기화
            self._baseline_points.clear()
            self.plot_widget.clear_baseline_points()
        self._update_baseline(algo=algo, params=params)
        if not (is_manual or is_oh_auto):
            self._save_current_spectrum_state(baseline_manual_override=False)

    def _on_bl_undo(self):
        if self.right_panel.get_mode() == 'CO':
            if self._current_entry is None:
                return
            store = self._co_baseline_store(self._current_entry)
            points = list(store.get('points', []))
            if points:
                points.pop()
                store['points'] = points
                store['manual_override'] = True
                self.plot_widget.undo_last_baseline_point()
                self._update_co_baseline_for_current(algo='Manual', manual_override=True)
            return

        if self._baseline_points:
            self._baseline_points.pop()
            self.plot_widget.undo_last_baseline_point()
            self._update_baseline(algo='Manual')
            self._save_current_spectrum_state(baseline_manual_override=True)

    def _on_bl_clear(self):
        if self.right_panel.get_mode() == 'CO':
            if self._current_entry is None:
                return
            store = self._co_baseline_store(self._current_entry)
            store['points'] = []
            store['manual_override'] = True
            self.plot_widget.clear_baseline_points()
            self._update_co_baseline_for_current(algo='Manual', manual_override=True)
            return

        self._baseline_points.clear()
        self.plot_widget.clear_baseline_points()
        self._update_baseline(algo='Manual')
        self._save_current_spectrum_state(baseline_manual_override=True)

    def _on_baseline_point_added(self, wn, ab):
        if self.right_panel.get_mode() == 'CO':
            if self._current_entry is None:
                return
            store = self._co_baseline_store(self._current_entry)
            points = list(store.get('points', []))
            points.append((wn, ab))
            store['points'] = points
            store['manual_override'] = True
            self._update_co_baseline_for_current(algo='Manual', manual_override=True)
            return

        self._baseline_points.append((wn, ab))
        self._update_baseline(algo='Manual')
        self._save_current_spectrum_state(baseline_manual_override=True)

    def _on_baseline_point_removed(self, idx: int):
        if self.right_panel.get_mode() == 'CO':
            if self._current_entry is None:
                return
            store = self._co_baseline_store(self._current_entry)
            points = list(store.get('points', []))
            if 0 <= idx < len(points):
                points.pop(idx)
            store['points'] = points
            store['manual_override'] = True
            self._update_co_baseline_for_current(algo='Manual', manual_override=True)
            return

        if 0 <= idx < len(self._baseline_points):
            self._baseline_points.pop(idx)
        self._update_baseline(algo='Manual')
        self._save_current_spectrum_state(baseline_manual_override=True)

    def _save_current_spectrum_state(self, baseline_manual_override: bool | None = None):
        if self._current_entry is None or self._wn_crop is None:
            return
        existing = self._spectrum_states.get(self._current_entry.filepath, {})
        manual_override = (
            existing.get('baseline_manual_override', False)
            if baseline_manual_override is None
            else bool(baseline_manual_override)
        )
        self._spectrum_states[self._current_entry.filepath] = {
            'wn_crop':        self._wn_crop.copy(),
            'ab_crop':        self._ab_crop.copy() if self._ab_crop is not None
                              else self._wn_crop.copy(),
            'baseline':       self._baseline.copy() if self._baseline is not None
                              else np.zeros_like(self._wn_crop),
            'ab_corrected':   self._ab_corrected.copy() if self._ab_corrected is not None
                              else np.zeros_like(self._wn_crop),
            'fit_result':     existing.get('fit_result', self._fit_result),
            'guesses':        existing.get('guesses', []),
            'locks':          self.right_panel.get_locks(),
            'baseline_points': list(self._baseline_points),
            'baseline_manual_override': manual_override,
            'snapshots':      existing.get('snapshots', []),
        }

    def _update_baseline(self, algo: str = None, params: dict = None):
        if self._current_entry is None:
            return
        cfg = self.right_panel.get_config()
        wn, ab = crop_region(
            self._current_entry.wavenumber,
            self._current_entry.absorbance,
            cfg['wn_min'], cfg['wn_max']
        )
        self._wn_crop = wn
        self._ab_crop = ab

        if algo is None:
            algo = cfg['baseline_algo']
        if params is None:
            params = cfg['baseline_params']

        if algo == 'OH Auto Baseline':
            if len(self._baseline_points) < 2:
                self._baseline_points = auto_oh_baseline_points(wn, ab)
            self.plot_widget.restore_baseline_points(self._baseline_points)
            bl = (baseline_from_points(wn, ab, self._baseline_points)
                  if len(self._baseline_points) >= 2 else np.zeros_like(ab))
        elif algo == 'Manual':
            pts = self._baseline_points
            bl = baseline_from_points(wn, ab, pts) if len(pts) >= 2 else np.zeros_like(ab)
        elif algo == 'Rubber Band':
            bl = baseline_rubberband(wn, ab)
        elif algo == 'ARPLS':
            bl = baseline_arpls(ab, lam=params.get('lam', 1e4))
        elif algo == 'SNIP':
            bl = baseline_snip(ab, n_iter=params.get('n_iter', 50))
        elif algo == 'Linear':
            bl = baseline_linear(wn, ab)
        else:
            bl = np.zeros_like(ab)

        self._baseline     = bl
        self._ab_corrected = subtract_baseline(ab, bl)
        self.plot_widget.set_corrected_spectrum(wn, self._ab_corrected, bl)
        self._refresh_selected_overlays(self._capture_plot_view())

        # 피팅 없이도 OH total area 자동 표시 (베이스라인 보정 후 trapz 적분)
        if self.right_panel.get_mode() == 'OH':
            total_oh = abs(float(_trapezoid(self._ab_corrected, wn)))
            self.right_panel.update_oh_total_area(total_oh, self._get_sio_area_for_entry(self._current_entry))

    def _sync_baseline_edit_state_for_current_spectrum(self):
        """스펙트럼 전환 후 Edit Baseline 상태와 auto baseline 적용을 다시 맞춘다."""
        mode = self.right_panel.get_mode()
        cfg = self.right_panel.get_config()
        algo = cfg['baseline_algo']
        edit_on = self.right_panel.btn_edit_bl.isChecked()
        is_editable = (mode in ('OH', 'CO') and algo in ('Manual', 'OH Auto Baseline'))

        self.plot_widget.set_baseline_edit_mode(edit_on and is_editable)

        if edit_on and mode == 'CO' and algo == 'OH Auto Baseline':
            if self._current_entry is not None:
                store = self._co_baseline_store(self._current_entry)
                if not store.get('manual_override'):
                    self._update_co_baseline_for_current(
                        algo=algo,
                        params=cfg['baseline_params'],
                    )
            return

        if not (edit_on and mode == 'OH' and algo == 'OH Auto Baseline'):
            return

        saved = self._spectrum_states.get(self._current_entry.filepath, {})
        if saved.get('baseline_manual_override'):
            return

        expected_wn, _ = crop_region(
            self._current_entry.wavenumber,
            self._current_entry.absorbance,
            cfg['wn_min'], cfg['wn_max']
        )
        region_changed = (
            self._wn_crop is None
            or len(self._wn_crop) != len(expected_wn)
            or not np.allclose(self._wn_crop, expected_wn)
        )
        if region_changed:
            self._baseline_points = []

        self._update_baseline(algo=algo, params=cfg['baseline_params'])

    # ── 활성 영역 줌 (Ctrl+A) ────────────────────────────────

    def _zoom_to_active_region(self):
        """현재 활성 분석 영역을 화면에 꽉 차게 줌 (Ctrl+A / Cmd+A)."""
        mode = self.right_panel.get_mode()
        cfg  = self.right_panel.get_config()

        if mode == 'OH':
            wn_min, wn_max = cfg['wn_min'], cfg['wn_max']
            self.plot_widget.zoom_to(wn_min, wn_max, padding=0.05)
            self.plot_widget.fit_y_to_current_x_range()

        elif mode == 'CO':
            self.plot_widget.zoom_to(cfg['wn_min'], cfg['wn_max'], padding=0.05)
            self.plot_widget.fit_y_to_current_x_range(
                exclude_keys=('raw',),
                exclude_overlay_kinds=('raw',),
            )

        elif mode == 'SiO':
            self.plot_widget.zoom_to(cfg['wn_min'], cfg['wn_max'], padding=0.05)
            self.plot_widget.fit_y_to_current_x_range()

    # ── 피크 자동 감지 ────────────────────────────────────────

    def _auto_detect(self):
        if self._current_entry is None:
            QMessageBox.warning(self, "No spectrum", "먼저 스펙트럼을 선택하세요.")
            return
        self._update_baseline()
        ab = np.maximum(self._ab_corrected, 0)
        n  = self.right_panel.get_n_peaks()
        guesses = find_peaks_second_derivative(self._wn_crop, ab, n_peaks=n)
        self.right_panel.set_guesses(
            guesses,
            locks=[{'center': False, 'amplitude': False, 'sigma': False} for _ in guesses]
        )
        self.plot_widget.show_peak_guesses(self._wn_crop, guesses)
        self.status_label.setText(f"Auto-detected {len(guesses)} peaks")

    # ── 피크 마우스 클릭 생성 ─────────────────────────────────

    def _on_peak_created(self, wn_pos: float, amplitude: float, sigma: float):
        """플롯 클릭 또는 우클릭 드래그 → 피크 추가.
        amplitude: 피크 높이 (absorbance 단위)
        sigma: Gaussian sigma (cm⁻¹)
        """
        if self._wn_crop is None:
            return
        self.right_panel.add_peak_guess(wn_pos, amplitude, sigma)
        guesses = self.right_panel.get_guesses()
        self.plot_widget.show_peak_guesses(self._wn_crop, guesses)
        self.status_label.setText(f"Peak added at {wn_pos:.0f} cm⁻¹  |  총 {len(guesses)}개")

    # ── 피크 드래그 ───────────────────────────────────────────

    def _on_peak_dragged(self, idx: int, new_center: float):
        if self.right_panel.get_mode() == 'CO':
            self._handle_co_peak_center_drag(idx, new_center)
        else:
            locks = self.right_panel.get_locks()
            if idx < len(locks) and locks[idx].get('center', False):
                return
            self._invalidate_current_fit_if_needed()
            self.right_panel.update_peak_center(idx, new_center)
            self.status_label.setText(f"P{idx+1} → {new_center:.1f} cm⁻¹  (preview updated)")
            self._save_guesses_to_state()
            self._schedule_post_fit_refit()

    def _on_peak_amplitude_dragged(self, idx: int, amplitude: float):
        if self.right_panel.get_mode() == 'CO':
            locks = self.right_panel.get_co_locks()
            if idx < len(locks) and locks[idx].get('amplitude', False):
                return
            self._update_co_b_guess(idx, new_amplitude=amplitude)
            self.right_panel.update_co_peak_amplitude(idx, amplitude)
            self.status_label.setText(f"CO_B P{idx+1} height → {amplitude:.3f}")
            return
        locks = self.right_panel.get_locks()
        if idx < len(locks) and locks[idx].get('amplitude', False):
            return
        self._invalidate_current_fit_if_needed()
        self.right_panel.update_peak_amplitude(idx, amplitude)
        self._save_guesses_to_state()
        self._schedule_post_fit_refit()
        self.status_label.setText(f"P{idx+1} height → {amplitude:.3f}")

    def _on_peak_sigma_changed(self, idx: int, sigma: float):
        if self.right_panel.get_mode() == 'CO':
            locks = self.right_panel.get_co_locks()
            if idx < len(locks) and locks[idx].get('sigma', False):
                return
            self._update_co_b_guess(idx, new_sigma=sigma)
            self.right_panel.update_co_peak_sigma(idx, sigma)
        else:
            locks = self.right_panel.get_locks()
            if idx < len(locks) and locks[idx].get('sigma', False):
                return
            self._invalidate_current_fit_if_needed()
            self.right_panel.update_peak_sigma(idx, sigma)
            self._save_guesses_to_state()
            self._schedule_post_fit_refit()
            self.status_label.setText(f"P{idx+1} sigma → {sigma:.1f} cm⁻¹")

    def _handle_co_peak_center_drag(self, idx: int, new_center: float):
        target = self._co_drag_targets.get(idx)
        if target and target.get('type') == 'simple':
            sub = target.get('sub', 'CO_B')
            self._set_manual_co_center(sub, new_center)
            self.status_label.setText(f"{sub} center → {new_center:.1f} cm⁻¹")
            return

        locks_for_table = self.right_panel.get_co_locks()
        if idx < len(locks_for_table) and locks_for_table[idx].get('center', False):
            return
        self._update_co_b_guess(idx, new_center=new_center)
        self.right_panel.update_co_peak_center(idx, new_center)
        if self._current_entry is not None:
            co_state = self._co_states.setdefault(self._current_entry.filepath, {})
            co_b_sub = co_state.setdefault('CO_B', {})
            locks = self.right_panel.get_co_locks()
            if idx < len(locks):
                locks[idx]['center'] = True
            co_b_sub['locks'] = locks
            co_b_sub['manual_center_locks'] = [
                i for i, lock in enumerate(locks) if lock.get('center', False)
            ]
            raw_fit = co_b_sub.get('raw_fit_result')
            if (
                target
                and target.get('type') == 'co_b_fit'
                and raw_fit is not None
                and 0 <= idx < len(getattr(raw_fit, 'peaks', []))
            ):
                raw_fit.peaks[idx].center = float(new_center)
                self._refresh_co_b_result_from_raw_fit()
        self.status_label.setText(
            f"CO_B P{idx + 1} center → {new_center:.1f} cm⁻¹"
        )

    def _update_co_b_guess(self, idx: int, new_center: float = None,
                           new_sigma: float = None, new_amplitude: float = None):
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        co_b_sub = co_state.setdefault('CO_B', {})
        guesses = co_b_sub.get('guesses', [])
        if 0 <= idx < len(guesses):
            if new_center is not None:
                guesses[idx].center = new_center
            if new_sigma is not None:
                guesses[idx].sigma = new_sigma
            if new_amplitude is not None:
                guesses[idx].amplitude = new_amplitude
            co_b_sub.pop('raw_fit_result', None)

    def _save_guesses_to_state(self):
        """피크 위치/sigma 수정 시 상태에 저장 — 스펙트럼 전환 후에도 위치 유지"""
        if self._current_entry is None or self._wn_crop is None:
            return
        guesses = self.right_panel.get_guesses()
        if not guesses:
            return
        filepath = self._current_entry.filepath
        existing = self._spectrum_states.get(filepath)
        if existing:
            existing['guesses'] = guesses
            existing['locks'] = self.right_panel.get_locks()
        else:
            self._spectrum_states[filepath] = {
                'wn_crop':        self._wn_crop.copy(),
                'ab_crop':        self._ab_crop.copy() if self._ab_crop is not None
                                  else self._wn_crop.copy(),
                'baseline':       self._baseline.copy() if self._baseline is not None
                                  else np.zeros_like(self._wn_crop),
                'ab_corrected':   self._ab_corrected.copy() if self._ab_corrected is not None
                                  else np.zeros_like(self._wn_crop),
                'fit_result':     None,
                'guesses':        guesses,
                'locks':          self.right_panel.get_locks(),
                'baseline_points': list(self._baseline_points),
                'snapshots':      self._get_oh_snapshots(filepath),
            }

    def _on_peak_locks_changed(self, locks):
        self.plot_widget.set_peak_locks(locks)
        if self._current_entry is None:
            return
        state = self._spectrum_states.setdefault(self._current_entry.filepath, {
            'wn_crop': self._wn_crop.copy() if self._wn_crop is not None else None,
            'ab_crop': self._ab_crop.copy() if self._ab_crop is not None else None,
            'baseline': self._baseline.copy() if self._baseline is not None else None,
            'ab_corrected': self._ab_corrected.copy() if self._ab_corrected is not None else None,
            'fit_result': self._fit_result,
            'guesses': self.right_panel.get_guesses(),
            'locks': [],
            'baseline_points': list(self._baseline_points),
            'snapshots': self._get_oh_snapshots(self._current_entry.filepath),
        })
        state['locks'] = locks

    def _invalidate_current_fit_if_needed(self):
        """post-fit 편집 시작 시 현재 fit 결과를 stale 처리."""
        if self._current_entry is None or self._fit_result is None:
            return

        self._fit_result = None
        self._fit_edit_pending = True
        self.right_panel.clear_results()

        filepath = self._current_entry.filepath
        if filepath in self._spectrum_states:
            self._spectrum_states[filepath]['fit_result'] = None
            self._spectrum_states[filepath]['guesses'] = self.right_panel.get_guesses()
            self._spectrum_states[filepath]['locks'] = self.right_panel.get_locks()

        entry_name = self._current_entry.name
        self._fit_records = [r for r in self._fit_records if r['filename'] != entry_name]

        list_idx = self.spectrum_list.list_widget.currentRow()
        self.spectrum_list.clear_fit_done(list_idx)

        potentials = self.spectrum_list.get_potentials()
        self._refresh_oh_stark_results(potentials)

    def _schedule_post_fit_refit(self):
        if self._fit_edit_pending:
            self._refit_timer.start()

    def _on_peak_rows_deleted(self, guesses):
        if self._current_entry is None or self._wn_crop is None:
            return

        self._fit_result = None
        self._fit_edit_pending = False
        self._refit_timer.stop()
        self.plot_widget.clear_fit_result()
        self.plot_widget.show_peak_guesses(self._wn_crop, guesses)
        self.right_panel.clear_results()

        filepath = self._current_entry.filepath
        entry_name = self._current_entry.name
        state = self._spectrum_states.setdefault(filepath, {
            'wn_crop': self._wn_crop.copy(),
            'ab_crop': self._ab_crop.copy() if self._ab_crop is not None else self._wn_crop.copy(),
            'baseline': self._baseline.copy() if self._baseline is not None else np.zeros_like(self._wn_crop),
            'ab_corrected': self._ab_corrected.copy() if self._ab_corrected is not None else np.zeros_like(self._wn_crop),
            'fit_result': None,
            'guesses': [],
            'locks': [],
            'baseline_points': list(self._baseline_points),
            'snapshots': self._get_oh_snapshots(filepath),
        })
        state['fit_result'] = None
        state['guesses'] = list(guesses)
        state['locks'] = self.right_panel.get_locks()

        self._fit_records = [r for r in self._fit_records if r['filename'] != entry_name]
        list_idx = self.spectrum_list.list_widget.currentRow()
        self.spectrum_list.clear_fit_done(list_idx)

        potentials = self.spectrum_list.get_potentials()
        self._refresh_oh_stark_results(potentials)

        self.status_label.setText(f"Peak removed  |  {len(guesses)} peaks remain")

    def _on_peak_params_changed(self, guesses):
        if self._wn_crop is None:
            return
        had_fit = self._fit_result is not None or self._fit_edit_pending
        if self._fit_result is not None:
            self._invalidate_current_fit_if_needed()
        for i, g in enumerate(guesses):
            self.plot_widget.update_guess_line(
                i,
                g.center,
                amplitude=g.amplitude,
                sigma=g.sigma,
                shape=getattr(g, 'shape', 'gaussian'),
            )
        self._save_guesses_to_state()
        if had_fit:
            self._schedule_post_fit_refit()

    def _on_peaks_cleared(self):
        if self._current_entry is None:
            return

        self._fit_result = None
        self._fit_edit_pending = False
        self._refit_timer.stop()
        self.plot_widget.clear_fit_result()
        self.right_panel.clear_results()

        filepath = self._current_entry.filepath
        entry_name = self._current_entry.name

        if filepath in self._spectrum_states:
            state = self._spectrum_states[filepath]
            state['fit_result'] = None
            state['guesses'] = []

        self._fit_records = [
            r for r in self._fit_records
            if r['filename'] != entry_name
        ]

        list_idx = self.spectrum_list.list_widget.currentRow()
        self.spectrum_list.clear_fit_done(list_idx)

        potentials = self.spectrum_list.get_potentials()
        self._refresh_oh_stark_results(potentials)
        self._co_stark_results = []

        self.status_label.setText("Peaks cleared")

    def _co_b_preview_arrays_for_current(self):
        if self._current_entry is None:
            return None
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        co_b = co_state.setdefault('CO_B', {})
        ep0 = co_b.get('ep0', 1650.0)
        ep1 = co_b.get('ep1', 1900.0)
        cfg = self.right_panel.get_config()
        full_baseline = None
        if not self._co_uses_endpoint_linear_baseline(cfg.get('baseline_algo')):
            full_baseline = self._compute_co_full_baseline(
                self._current_entry,
                algo=cfg['baseline_algo'],
                params=cfg['baseline_params'],
            )
        return (
            *self._co_subregion_data(
                self._current_entry, ep0, ep1, full_baseline=full_baseline),
            full_baseline,
        )

    def _on_co_peak_params_changed(self, guesses):
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        co_b_sub = co_state.setdefault('CO_B', {})
        co_b_sub['guesses'] = list(guesses)
        co_b_sub['locks'] = self.right_panel.get_co_locks()
        co_b_sub['manual_center_locks'] = [
            i for i, lock in enumerate(co_b_sub['locks']) if lock.get('center', False)
        ]
        co_b_sub.pop('raw_fit_result', None)
        preview = self._co_b_preview_arrays_for_current()
        if preview is None:
            return
        wn_b, _, bl_b, _, full_baseline = preview
        if len(wn_b) >= 5 and guesses:
            self._co_drag_targets = {
                i: {'type': 'co_b_guess', 'sub': 'CO_B', 'peak_idx': i}
                for i in range(len(guesses))
            }
            self.plot_widget.show_peak_guesses(
                wn_b,
                guesses,
                baseline=self._co_display_baseline(bl_b, full_baseline),
            )
            self.plot_widget.set_peak_locks(co_b_sub['locks'])
        else:
            self.plot_widget.clear_fit_result()

    def _on_co_peak_locks_changed(self, locks):
        self.plot_widget.set_peak_locks(locks)
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        co_b_sub = co_state.setdefault('CO_B', {})
        co_b_sub['locks'] = locks
        co_b_sub['manual_center_locks'] = [
            i for i, lock in enumerate(locks) if lock.get('center', False)
        ]

    def _on_co_peaks_cleared(self):
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        co_b_sub = co_state.setdefault('CO_B', {})
        for key in ('guesses', 'locks', 'manual_center_locks', 'raw_fit_result',
                    'wn_b', 'ab_pos_b', 'baseline_b'):
            co_b_sub.pop(key, None)
        self._co_drag_targets = {}
        self.plot_widget.clear_fit_result()
        self._apply_co_view(self._current_entry)
        self.status_label.setText("CO_B peaks cleared")

    def _on_co_peak_rows_deleted(self, guesses):
        self._on_co_peak_params_changed(guesses)
        self.status_label.setText(f"CO_B peak removed  |  {len(guesses)} peaks remain")

    # ── Split View ────────────────────────────────────────────

    def _toggle_split_view(self):
        if self._analysis_is_detached():
            self._reattach_analysis_from_window()
        elif self._is_split:
            self._unsplit_view()
        else:
            self._split_view()

    def _split_view(self):
        if self._is_split or self._analysis_is_detached():
            return
        center_idx = self.main_splitter.indexOf(self.center_tabs)
        center_size = self.main_splitter.sizes()[center_idx]

        # 내부 스플리터 + 오른쪽 탭 생성
        self._inner_splitter = QSplitter(Qt.Horizontal)
        self._right_tabs = QTabWidget()
        self._right_tabs.setObjectName("center_tabs")
        self._setup_detachable_tab_widget(self._right_tabs, self._on_right_tab_context_menu)
        self._right_tabs.currentChanged.connect(
            lambda index, tabs=self._right_tabs: self._on_tab_changed(tabs, index)
        )

        # Analysis 위젯을 오른쪽 탭으로 이동
        analysis_idx = self.center_tabs.indexOf(self.analysis_widget)
        if analysis_idx == -1:
            return
        self.center_tabs.removeTab(analysis_idx)
        self._right_tabs.addTab(self.analysis_widget, "  Analysis  ")
        self.analysis_widget.show()
        self._right_tabs.setCurrentWidget(self.analysis_widget)

        # center_tabs + right_tabs → inner_splitter
        # (center_tabs 가 main_splitter 에서 자동으로 빠짐)
        self._inner_splitter.addWidget(self.center_tabs)
        self._inner_splitter.addWidget(self._right_tabs)
        self._inner_splitter.setSizes([center_size // 2, center_size // 2])

        # main_splitter 에 inner_splitter 삽입
        self.main_splitter.insertWidget(center_idx, self._inner_splitter)

        self._is_split = True
        self._update_split_button()

    def _unsplit_view(self):
        if not self._is_split or self._inner_splitter is None:
            return
        inner_idx = self.main_splitter.indexOf(self._inner_splitter)

        # Analysis 를 center_tabs 로 복원
        analysis_idx = self._right_tabs.indexOf(self.analysis_widget)
        if analysis_idx != -1:
            self._right_tabs.removeTab(analysis_idx)
            if self.center_tabs.indexOf(self.analysis_widget) == -1:
                self.center_tabs.addTab(self.analysis_widget, "  Analysis  ")
            self.analysis_widget.show()
            self.center_tabs.setCurrentWidget(self.analysis_widget)
        elif self._analysis_restore_target == 'split':
            self._analysis_restore_target = 'center'

        # center_tabs 를 main_splitter 에 직접 복원
        # (center_tabs 가 inner_splitter 에서 자동으로 빠짐)
        self.main_splitter.insertWidget(inner_idx, self.center_tabs)

        # 빈 inner_splitter / right_tabs 제거
        self._inner_splitter.setParent(None)
        self._right_tabs.setParent(None)
        self._inner_splitter.deleteLater()
        self._right_tabs.deleteLater()
        self._inner_splitter = None
        self._right_tabs = None

        self._is_split = False
        self._update_split_button()

    def _on_center_tab_context_menu(self, point):
        menu = QMenu(self)
        tab_index = self.center_tabs.tabBar().tabAt(point)

        if self._analysis_is_detached():
            menu.addAction("↩  Attach Analysis", self._reattach_analysis_from_window)
        elif tab_index != -1 and self.center_tabs.widget(tab_index) is self.analysis_widget:
            menu.addAction("⇱  Detach Analysis",
                           lambda: self._detach_analysis_to_window(self.center_tabs))

        if self._is_split:
            menu.addAction("⊟  Unsplit", self._unsplit_view)
        else:
            menu.addAction("⊞  Split Right", self._split_view)
        menu.exec_(self.center_tabs.tabBar().mapToGlobal(point))

    def _on_right_tab_context_menu(self, point):
        menu = QMenu(self)
        if not self._analysis_is_detached():
            menu.addAction("⇱  Detach Analysis",
                           lambda: self._detach_analysis_to_window(self._right_tabs))
        menu.addAction("⊟  Unsplit", self._unsplit_view)
        menu.exec_(self._right_tabs.tabBar().mapToGlobal(point))

    # ── 피크 조정 후 자동 재피팅 ──────────────────────────────

    def _auto_refit_current(self):
        """피크 드래그 / FWHM 수정 → 400 ms 후 자동 재피팅 + 상태 저장"""
        if (self._current_entry is None
                or self._wn_crop is None
                or self._ab_corrected is None
                or self._ab_crop is None):
            return

        guesses = self.right_panel.get_guesses()
        if not guesses:
            return

        from core.peak_finder import PeakGuess
        cfg   = self.right_panel.get_config()
        locks = self.right_panel.get_locks()
        ab    = np.maximum(self._ab_corrected, 0)

        result = fit_peaks(self._wn_crop, ab, guesses,
                           center_tolerance=cfg['center_tolerance'],
                           locks=locks)
        if not result.success:
            return

        self._fit_result = result
        self._fit_edit_pending = False
        # amplitude = 피크 높이 (lmfit amplitude 아님)
        fitted_guesses = [
            PeakGuess(center=p.center,
                      amplitude=float(np.max(result.individual_curves[i]))
                                if i < len(result.individual_curves) else p.amplitude,
                      sigma=p.sigma, index=i)
            for i, p in enumerate(result.peaks)
        ]

        # 상태 저장 — 스펙트럼 전환 후 돌아와도 유지됨
        self._spectrum_states[self._current_entry.filepath] = {
            'wn_crop':        self._wn_crop.copy(),
            'ab_crop':        self._ab_crop.copy(),
            'baseline':       self._baseline.copy() if self._baseline is not None
                              else np.zeros_like(self._wn_crop),
            'ab_corrected':   self._ab_corrected.copy(),
            'fit_result':     result,
            'guesses':        fitted_guesses,
            'locks':          locks,
            'baseline_points': list(self._baseline_points),
            'snapshots':      self._get_oh_snapshots(self._current_entry.filepath),
        }

        # fit_records 갱신
        entry_name   = self._current_entry.name
        existing_idx = next((i for i, r in enumerate(self._fit_records)
                             if r['filename'] == entry_name), None)
        record = {'filename': entry_name, 'fit_result': result}
        if existing_idx is not None:
            self._fit_records[existing_idx] = record
        else:
            self._fit_records.append(record)

        # 플롯 업데이트
        self.plot_widget.show_fit_result(self._wn_crop, ab, result)
        self.right_panel.update_results(result)
        self.right_panel.set_guesses(fitted_guesses, locks=locks)

        # Analysis / Stark 자동 갱신
        potentials = self.spectrum_list.get_potentials()
        self._refresh_oh_stark_results(potentials)

        self.status_label.setText(
            f"Refit  |  R²={result.r_squared:.5f}  |  "
            + "  ".join(f"P{i+1}:{p.center:.0f}" for i, p in enumerate(result.peaks))
        )

    # ── 피팅 실행 ─────────────────────────────────────────────

    def _run_fit(self):
        mode = self.right_panel.get_mode()
        if mode == 'CO':
            self._run_co_fit()
        elif mode == 'SiO':
            self._calc_sio_area()
        else:
            self._run_oh_fit()

    def _run_oh_fit(self):
        if self._current_entry is None:
            QMessageBox.warning(self, "No spectrum", "먼저 스펙트럼을 선택하세요.")
            return
        self._update_baseline()
        ab = np.maximum(self._ab_corrected, 0)

        guesses = self.right_panel.get_guesses()
        if not guesses:
            guesses = find_peaks_second_derivative(
                self._wn_crop, ab, n_peaks=self.right_panel.get_n_peaks())
            self.right_panel.set_guesses(guesses, locks=self.right_panel.get_locks())

        cfg    = self.right_panel.get_config()
        locks  = self.right_panel.get_locks()
        result = fit_peaks(self._wn_crop, ab, guesses,
                           center_tolerance=cfg['center_tolerance'],
                           locks=locks)
        self._fit_result = result

        if result.success:
            self._fit_edit_pending = False
            # Initial Parameters 테이블을 피팅된 center/sigma 로 업데이트
            from core.peak_finder import PeakGuess
            fitted_guesses = [
                PeakGuess(center=p.center,
                          amplitude=float(np.max(result.individual_curves[i]))
                                    if i < len(result.individual_curves) else p.amplitude,
                          sigma=p.sigma, index=i)
                for i, p in enumerate(result.peaks)
            ]
            self.right_panel.set_guesses(fitted_guesses, locks=locks)

            # 스펙트럼별 상태 저장 (전환 시 복원용)
            self._spectrum_states[self._current_entry.filepath] = {
                'wn_crop':        self._wn_crop.copy(),
                'ab_crop':        self._ab_crop.copy(),
                'baseline':       self._baseline.copy(),
                'ab_corrected':   self._ab_corrected.copy(),
                'fit_result':     result,
                'guesses':        fitted_guesses,
                'locks':          locks,
                'baseline_points': list(self._baseline_points),
                'snapshots':      self._get_oh_snapshots(self._current_entry.filepath),
            }

            # Stark 분석용 기록 갱신
            entry_name = self._current_entry.name
            existing_idx = next((i for i, r in enumerate(self._fit_records)
                                 if r['filename'] == entry_name), None)
            record = {'filename': entry_name, 'fit_result': result}
            if existing_idx is not None:
                self._fit_records[existing_idx] = record
            else:
                self._fit_records.append(record)

            self.plot_widget.show_fit_result(self._wn_crop, ab, result)
            self.right_panel.update_results(result)
            self.right_panel.set_guesses(fitted_guesses, locks=locks)
            idx = self.spectrum_list.list_widget.currentRow()
            self.spectrum_list.mark_fit_done(idx)
            self.status_label.setText(
                f"Fit OK  |  R²={result.r_squared:.5f}  |  "
                + "  ".join(f"P{i+1}:{p.center:.0f}" for i, p in enumerate(result.peaks))
            )

            # OH total area 업데이트
            total_oh = sum(p.area for p in result.peaks)
            self.right_panel.update_oh_total_area(total_oh, self._get_sio_area_for_entry(self._current_entry))

            # Analysis / Stark 자동 갱신
            potentials = self.spectrum_list.get_potentials()
            self._refresh_oh_stark_results(potentials)
        else:
            QMessageBox.warning(self, "Fit Failed", result.message)

    def _refresh_oh_stark_results(self, potentials: dict | None = None):
        if potentials is None:
            potentials = self._visible_potentials()
        fit_records = self._visible_fit_records()
        if not fit_records:
            self._stark_results = []
            self.right_panel.update_stark_results([])
            self.analysis_widget.update_plots([], potentials, [])
            self._refresh_session_compare()
            return

        results = calculate_stark_slopes(
            fit_records,
            potentials=potentials if potentials else None
        )
        self._stark_results = results
        self.right_panel.update_stark_results(results)
        self.analysis_widget.update_plots(fit_records, potentials, results)
        if self._visible_sio_areas():
            self.analysis_widget.update_oh_normalized(
                fit_records, self._visible_sio_areas(), potentials)
        self._refresh_session_compare()

    # ── Export ────────────────────────────────────────────────

    def _visible_spectrum_states(self) -> dict:
        visible_paths = {entry.filepath for entry in self.spectrum_list.get_visible_entries()}
        return {
            filepath: state
            for filepath, state in self._spectrum_states.items()
            if filepath in visible_paths
        }

    def _visible_co_states(self) -> dict:
        visible_paths = {entry.filepath for entry in self.spectrum_list.get_visible_entries()}
        return {
            filepath: state
            for filepath, state in self._co_states.items()
            if filepath in visible_paths
        }

    def _export_plot_image(self):
        fp, _ = QFileDialog.getSaveFileName(
            self, "Save Plot Image", "",
            "PNG Image (*.png);;SVG Vector (*.svg)")
        if fp:
            self.plot_widget.do_export_image(fp)
            self.status_label.setText(f"Plot saved: {Path(fp).name}")

    def _export(self):
        entries = self.spectrum_list.get_visible_entries()
        potentials = self.spectrum_list.get_potentials(visible_only=True)
        spectrum_states = self._visible_spectrum_states()
        co_states = self._visible_co_states()
        fit_records = self._visible_fit_records()
        co_fit_records = self._visible_co_fit_records()

        fitted_count = sum(
            1 for s in spectrum_states.values()
            if s.get('fit_result') and s['fit_result'].success
        )
        co_count = len(co_fit_records)

        if fitted_count == 0 and co_count == 0:
            QMessageBox.warning(self, "No result", "현재 세션에 내보낼 피팅 결과가 없습니다.")
            return

        fp, _ = QFileDialog.getSaveFileName(
            self, "Export Results", "", "Excel (*.xlsx)")
        if not fp:
            return
        if not fp.endswith('.xlsx'):
            fp += '.xlsx'

        saved_files = []
        try:
            if fitted_count > 1:
                stark = calculate_stark_slopes(
                    fit_records,
                    potentials=potentials if potentials else None,
                )
                sio_areas = {
                    entry.name: self._get_sio_area_for_entry(entry)
                    for entry in entries
                    if self._get_sio_area_for_entry(entry) is not None
                }
                export_all_spectra(entries, spectrum_states,
                                   potentials, stark, fp,
                                   sio_ref_area=sio_areas)
                saved_files.append(f"{fitted_count}개 OH 스펙트럼: {Path(fp).name}")
            elif fitted_count == 1:
                fitted_entry = None
                fitted_state = None
                for entry in entries:
                    state = spectrum_states.get(entry.filepath)
                    if state and state.get('fit_result') and state['fit_result'].success:
                        fitted_entry = entry
                        fitted_state = state
                        break
                if fitted_entry is None or fitted_state is None:
                    QMessageBox.warning(self, "No result", "먼저 피팅을 실행하세요.")
                    return
                export_single(fitted_state['wn_crop'], fitted_state['ab_crop'],
                              fitted_state['baseline'], fitted_state['fit_result'],
                              fp, fitted_entry.name)
                saved_files.append(f"OH: {Path(fp).name}")

            if co_count > 0:
                co_fp = fp.replace('.xlsx', '_CO.xlsx')
                co_stark = calculate_co_stark_slopes(
                    co_fit_records,
                    potentials=potentials if potentials else None
                )
                export_co_results(co_fit_records, co_states,
                                  entries, potentials, co_fp, co_stark)
                saved_files.append(f"{co_count}개 CO 스펙트럼: {Path(co_fp).name}")

            QMessageBox.information(self, "Exported",
                                    "저장 완료:\n" + "\n".join(saved_files))
        except Exception as e:
            QMessageBox.critical(self, "Export 실패", str(e))

    def _export_spectra(self):
        entries = self.spectrum_list.get_visible_entries()
        if not entries:
            QMessageBox.warning(self, "No spectra", "현재 세션에 내보낼 스펙트럼이 없습니다.")
            return

        default_name = "ir_spectra.xlsx"
        if len(entries) == 1:
            default_name = f"{Path(entries[0].name).stem}_spectra.xlsx"

        fp, _ = QFileDialog.getSaveFileName(
            self, "Export Spectra", default_name, "Excel (*.xlsx)")
        if not fp:
            return
        if not fp.endswith('.xlsx'):
            fp += '.xlsx'

        try:
            export_info = export_spectra_excel(
                entries,
                self.spectrum_list.get_potentials(visible_only=True),
                fp,
                spectrum_states=self._visible_spectrum_states(),
                co_states=self._visible_co_states(),
            )
            layout_label = "matrix" if export_info['layout'] == 'matrix' else "long-format"
            processed_parts = []
            if export_info.get('oh_points', 0) > 0:
                processed_parts.append(f"OH {export_info['oh_points']} pts")
            if export_info.get('co_points', 0) > 0:
                processed_parts.append(f"CO {export_info['co_points']} pts")
            processed_text = ""
            if processed_parts:
                processed_text = "\n" + " / ".join(processed_parts)
            QMessageBox.information(
                self,
                "Exported",
                "저장 완료:\n"
                f"{Path(fp).name}\n\n"
                f"{export_info['n_spectra']}개 스펙트럼 / "
                f"{export_info['n_points']} points / "
                f"{layout_label}"
                f"{processed_text}"
            )
            self.status_label.setText(
                f"Spectra exported: {Path(fp).name}  |  "
                f"{export_info['n_spectra']} spectra  |  {layout_label}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Export 실패", str(e))

    # ── Batch ─────────────────────────────────────────────────

    def _open_batch(self):
        dlg = BatchDialog(self)
        if dlg.exec_():
            fps, config = dlg.get_config()
            self._run_batch(fps, config)

    def _run_batch(self, filepaths, config):
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(filepaths))
        self._batch_worker = BatchWorker(filepaths, config)
        self._batch_worker.progress.connect(
            lambda c, t, f: (
                self.progress_bar.setValue(c),
                self.status_label.setText(f"Batch [{c}/{t}]  {f}")
            )
        )
        self._batch_worker.finished.connect(self._on_batch_done)
        self._batch_worker.error.connect(
            lambda e: QMessageBox.critical(self, "Batch Error", e))
        self._batch_worker.start()

    def _on_batch_done(self, results):
        self.batch_results = results
        self.progress_bar.setVisible(False)
        ok = sum(1 for r in results if r.success)
        self.status_label.setText(f"Batch done  |  {ok}/{len(results)} succeeded")
        reply = QMessageBox.question(
            self, "Batch Complete",
            f"{ok}/{len(results)}개 성공.\nExcel로 내보내시겠습니까?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            fp, _ = QFileDialog.getSaveFileName(self, "Save Batch", "", "Excel (*.xlsx)")
            if fp:
                if not fp.endswith('.xlsx'):
                    fp += '.xlsx'
                export_batch(
                    [{'filename': r.filename, 'fit_result': r.fit_result}
                     for r in results if r.success], fp)
                QMessageBox.information(self, "OK", f"저장 완료:\n{fp}")

    # ── Auto Fit (Strategy B: 양 끝점 보간) ──────────────────

    def _auto_fit(self):
        entries, session_label = self._auto_fit_target_entries()
        if not entries:
            QMessageBox.warning(
                self, "Auto Fit",
                "여러 세션이 함께 보이는 상태입니다.\n"
                "좌측 SPECTRA에서 세션 토글을 선택하거나,\n"
                "현재 스펙트럼이 속한 세션을 먼저 선택하세요."
            )
            return
        if len(entries) < 3:
            QMessageBox.warning(self, "Auto Fit",
                                "Auto Fit은 최소 3개의 스펙트럼이 필요합니다.\n"
                                "(첫 번째·마지막을 먼저 수동 피팅하세요.)")
            return

        first_entry = entries[0]
        last_entry  = entries[-1]
        first_state = self._spectrum_states.get(first_entry.filepath)
        last_state  = self._spectrum_states.get(last_entry.filepath)

        if not first_state:
            QMessageBox.warning(self, "Auto Fit", "첫 번째 스펙트럼을 먼저 피팅하세요.")
            return
        if not last_state:
            QMessageBox.warning(self, "Auto Fit", "마지막 스펙트럼을 먼저 피팅하세요.")
            return

        first_fit = first_state['fit_result']
        last_fit  = last_state['fit_result']

        if len(first_fit.peaks) != len(last_fit.peaks):
            QMessageBox.warning(self, "Auto Fit",
                                "첫 번째와 마지막 스펙트럼의 피크 수가 다릅니다.\n"
                                "동일한 피크 수로 피팅 후 다시 시도하세요.")
            return

        # 전위값 기반 보간 준비
        potentials  = self.spectrum_list.get_potentials()
        V1 = potentials.get(first_entry.name)
        VN = potentials.get(last_entry.name)
        use_potential = (V1 is not None and VN is not None and V1 != VN)

        cfg       = self.right_panel.get_config()
        shape     = self.right_panel.get_peak_shape()
        n_total   = len(entries)

        from core.peak_finder import PeakGuess

        success_count = 0
        intermediate = entries[1:-1]   # 첫·끝 제외

        all_entries = self.spectrum_list.get_all_entries()

        self.progress_bar.setRange(0, len(intermediate))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        for k, entry in enumerate(intermediate, start=1):
            self.progress_bar.setValue(k)
            self.status_label.setText(
                f"Auto Fit  [{k}/{len(intermediate)}]  {entry.name}")
            self._fit_records = [
                r for r in self._fit_records
                if r.get('filename') != entry.name
            ]
            stale_state = self._spectrum_states.get(entry.filepath)
            if stale_state is not None:
                stale_state['fit_result'] = None
            list_idx = next(
                (i for i, e in enumerate(all_entries)
                 if e.filepath == entry.filepath), -1)
            if list_idx >= 0:
                self.spectrum_list.clear_fit_done(list_idx)

            # 보간 비율 t 계산
            if use_potential:
                Vk = potentials.get(entry.name)
                t  = ((Vk - V1) / (VN - V1)) if Vk is not None else k / (n_total - 1)
            else:
                t = k / (n_total - 1)
            t = max(0.0, min(1.0, t))

            # 피크 파라미터 선형 보간
            guesses = []
            for i, (p1, pN) in enumerate(zip(first_fit.peaks, last_fit.peaks)):
                center = p1.center    + t * (pN.center    - p1.center)
                sigma  = p1.sigma     + t * (pN.sigma     - p1.sigma)
                amp    = p1.amplitude + t * (pN.amplitude - p1.amplitude)
                guesses.append(PeakGuess(center=center, amplitude=amp,
                                         sigma=sigma, index=i))

            # 베이스라인 보정
            wn, ab = crop_region(entry.wavenumber, entry.absorbance,
                                  cfg['wn_min'], cfg['wn_max'])
            algo   = cfg['baseline_algo']
            params = cfg['baseline_params']

            if algo == 'OH Auto Baseline':
                saved = self._spectrum_states.get(entry.filepath)
                if saved and saved.get('baseline_points'):
                    wn = saved['wn_crop']
                    ab = saved['ab_crop']
                    bl = saved['baseline']
                    ab_cor = saved['ab_corrected']
                    baseline_points = list(saved.get('baseline_points', []))
                else:
                    baseline_points = auto_oh_baseline_points(wn, ab)
                    bl = baseline_from_points(wn, ab, baseline_points)
                    ab_cor = subtract_baseline(ab, bl)
            elif algo == 'Manual':
                # Manual 모드: 저장된 베이스라인이 있으면 재사용, 없으면 zero
                saved = self._spectrum_states.get(entry.filepath)
                if saved:
                    wn     = saved['wn_crop']
                    ab     = saved['ab_crop']
                    bl     = saved['baseline']
                    ab_cor = saved['ab_corrected']
                    baseline_points = list(saved.get('baseline_points', []))
                else:
                    bl     = np.zeros_like(ab)
                    ab_cor = ab.copy()
                    baseline_points = []
            elif algo == 'Rubber Band':
                bl     = baseline_rubberband(wn, ab)
                ab_cor = subtract_baseline(ab, bl)
                baseline_points = []
            elif algo == 'ARPLS':
                bl     = baseline_arpls(ab, lam=params.get('lam', 1e4))
                ab_cor = subtract_baseline(ab, bl)
                baseline_points = []
            elif algo == 'SNIP':
                bl     = baseline_snip(ab, n_iter=params.get('n_iter', 50))
                ab_cor = subtract_baseline(ab, bl)
                baseline_points = []
            elif algo == 'Linear':
                bl     = baseline_linear(wn, ab)
                ab_cor = subtract_baseline(ab, bl)
                baseline_points = []
            else:
                bl     = np.zeros_like(ab)
                ab_cor = ab.copy()
                baseline_points = []

            ab_pos = np.maximum(ab_cor, 0)

            # 피팅 실행
            locks = [
                {'center': True, 'amplitude': False, 'sigma': False}
                for _ in guesses
            ]
            result = fit_peaks(wn, ab_pos, guesses,
                               shape=shape, locks=locks)
            if not result.success:
                continue

            fitted_guesses = [
                PeakGuess(center=p.center,
                          amplitude=float(np.max(result.individual_curves[i]))
                                    if i < len(result.individual_curves) else p.amplitude,
                          sigma=p.sigma, index=i)
                for i, p in enumerate(result.peaks)
            ]
            self._spectrum_states[entry.filepath] = {
                'wn_crop':        wn.copy(),
                'ab_crop':        ab.copy(),
                'baseline':       bl.copy(),
                'ab_corrected':   ab_cor.copy(),
                'fit_result':     result,
                'guesses':        fitted_guesses,
                'locks':          locks,
                'baseline_points': baseline_points,
                'snapshots':      self._get_oh_snapshots(entry.filepath),
            }

            # fit_records 갱신
            existing_idx = next(
                (i for i, r in enumerate(self._fit_records)
                 if r['filename'] == entry.name), None)
            record = {'filename': entry.name, 'fit_result': result}
            if existing_idx is not None:
                self._fit_records[existing_idx] = record
            else:
                self._fit_records.append(record)

            # 목록에 ✓ 표시
            if list_idx >= 0:
                self.spectrum_list.mark_fit_done(list_idx)

            success_count += 1

        self.progress_bar.setVisible(False)

        # 현재 선택된 스펙트럼 화면 갱신
        if self._current_entry:
            saved = self._spectrum_states.get(self._current_entry.filepath)
            if saved:
                ab = np.maximum(saved['ab_corrected'], 0)
                self.plot_widget.show_fit_result(
                    saved['wn_crop'], ab, saved['fit_result'])
                self.right_panel.update_results(saved['fit_result'])

        self.status_label.setText(
            f"Auto Fit 완료  |  {success_count}/{len(intermediate)} 성공  "
            f"|  session={session_label or 'current'}  "
            "|  center locked"
        )

        # Auto Fit 완료 후 Stark 자동 계산 및 Analysis 탭 표시
        if self._visible_fit_records():
            self._refresh_oh_stark_results(self._visible_potentials())

        QMessageBox.information(
            self, "Auto Fit 완료",
            f"{success_count} / {len(intermediate)} 개 스펙트럼 피팅 성공.\n"
            f"세션: {session_label or 'current'}\n"
            "피크 center: 첫/마지막 피팅값 사이의 보간값으로 고정\n\n"
            "Analysis 창 또는 탭에서 결과를 확인하세요."
        )

    # ── Stark Tuning Analysis ─────────────────────────────────

    def _calculate_stark(self):
        potentials = self._visible_potentials()
        mode = self.right_panel.get_mode()
        analysis_subtab = None
        if self._analysis_view_is_active():
            analysis_subtab = self.analysis_widget.get_current_subtab()

        co_fit_records = self._visible_co_fit_records()
        if mode == 'CO' or (analysis_subtab == 'CO' and co_fit_records):
            if not co_fit_records:
                QMessageBox.warning(self, "No data", "분석된 CO 스펙트럼이 없습니다.")
                return
            results = calculate_co_stark_slopes(
                co_fit_records,
                potentials=potentials if potentials else None
            )
            if not results:
                QMessageBox.warning(self, "No results",
                                    "전위값을 파싱할 수 없습니다.\n"
                                    "Spectra 패널의 Potential Assignments 테이블을 확인하세요.")
                return
            self._co_stark_results = results
            self.right_panel.update_co_stark_results(results)
            self.analysis_widget.update_co_plots(co_fit_records, potentials, results)
            self._show_analysis_view(preferred_subtab='CO')
            self.status_label.setText(
                "CO Stark slopes calculated  |  "
                + "  ".join(f"{r.series_name}: {r.slope:.1f} cm⁻¹/V" for r in results)
            )
            return

        fit_records = self._visible_fit_records()
        if not fit_records:
            QMessageBox.warning(self, "No data", "피팅된 스펙트럼이 없습니다.")
            return
        results = calculate_stark_slopes(
            fit_records,
            potentials=potentials if potentials else None
        )
        if not results:
            QMessageBox.warning(self, "No results",
                                "전위값을 파싱할 수 없습니다.\n"
                                "Spectra 패널의 Potential Assignments 테이블을 확인하세요.")
            return
        self.right_panel.update_stark_results(results)
        self._stark_results = results

        # Analysis 탭 업데이트 후 자동 전환
        self.analysis_widget.update_plots(fit_records, potentials, results)
        if self._visible_sio_areas():
            self.analysis_widget.update_oh_normalized(
                fit_records, self._visible_sio_areas(), potentials)
        self._show_analysis_view(preferred_subtab='OH')

        self.status_label.setText(
            f"Stark slopes calculated  |  {len(results)} peaks  |  "
            + "  ".join(f"P{r.peak_index+1}: {r.slope:.1f} cm⁻¹/V" for r in results)
        )

    def _show_stark_plot(self, peak_idx: int):
        if not hasattr(self, '_stark_results'):
            return
        result = next((r for r in self._stark_results if r.peak_index == peak_idx), None)
        if result is None:
            return

        from PyQt5.QtWidgets import QDialog, QVBoxLayout
        import pyqtgraph as pg
        import numpy as np

        dlg = QDialog(self)
        dlg.setWindowTitle(f"P{peak_idx + 1} Stark Tuning Plot")
        dlg.resize(450, 380)
        layout = QVBoxLayout(dlg)

        pw = pg.PlotWidget(background='#1e1e2e')
        pw.setLabel('bottom', 'Potential (V)', color='#6c7086')
        pw.setLabel('left', 'Peak Center (cm⁻¹)', color='#6c7086')
        pw.showGrid(x=True, y=True, alpha=0.15)
        pw.getAxis('bottom').setTextPen(pg.mkPen('#6c7086'))
        pw.getAxis('left').setTextPen(pg.mkPen('#6c7086'))

        color = '#89b4fa'
        scatter = pg.ScatterPlotItem(
            result.potentials, result.centers,
            size=10, brush=pg.mkBrush(color),
            pen=pg.mkPen('#1e1e2e', width=1)
        )
        pw.addItem(scatter)

        x_fit = np.linspace(min(result.potentials), max(result.potentials), 100)
        y_fit = result.slope * x_fit + result.intercept
        pw.plot(x_fit, y_fit, pen=pg.mkPen('#f38ba8', width=2),
                name=f'slope={result.slope:.2f} cm⁻¹/V  R²={result.r_squared:.4f}')

        pw.addLegend(labelTextColor='#a6adc8', brush=pg.mkBrush('#1e1e2e'),
                     pen=pg.mkPen('#313244'))

        from PyQt5.QtWidgets import QLabel as QL
        info = QL(
            f"P{peak_idx+1}:  slope = {result.slope:.3f} cm⁻¹/V  "
            f"|  R² = {result.r_squared:.4f}  |  N = {result.n_points}"
        )
        info.setStyleSheet("color: #cdd6f4; padding: 4px;")

        layout.addWidget(pw)
        layout.addWidget(info)
        dlg.exec_()

    # ── Session Save / Load ───────────────────────────────────

    def _new_workspace(self):
        entries = self.spectrum_list.get_all_entries()
        if entries:
            reply = QMessageBox.question(
                self, "New Workspace",
                f"현재 화면의 {len(entries)}개 스펙트럼을 모두 닫을까요?\n"
                "저장된 원본 파일과 .irsession 파일은 삭제되지 않습니다.",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        self._reset_workspace_state()
        self.status_label.setText("New workspace ready")

    def _reset_workspace_state(self):
        self._loading_session = True
        try:
            self.spectrum_list.clear_all()
            self._current_entry    = None
            self._baseline         = None
            self._ab_corrected     = None
            self._fit_result       = None
            self._wn_crop          = None
            self._ab_crop          = None
            self._baseline_points  = []
            self.batch_results     = []
            self._fit_records      = []
            self._spectrum_states  = {}
            self._co_states        = {}
            self._sio_states       = {}
            self._co_fit_records   = []
            self._co_stark_results = []
            self._co_drag_targets  = {}
            self._stark_results    = []
            self._sio_ref_area     = None
            self._total_shifts     = {}
            self._total_view_mode  = 'overlay'
            self.right_panel.set_total_view_mode(self._total_view_mode)
            self.right_panel.clear_current_summary()
            self.right_panel.clear_stark_results()
            self.right_panel.set_snapshot_names([], selected_index=-1)
            self.right_panel.btn_snapshot_save.setEnabled(False)
            self.plot_widget.reset_view()
            self.plot_widget._clear_all()
            self.analysis_widget.update_plots([], {}, [])
            self.analysis_widget.update_co_plots([], {}, [])
            self.analysis_widget.update_co_stark_results([])
            self.analysis_widget.update_compare_plot([])
            self.setWindowTitle("In Situ IR Analyzer")
            self._sync_analysis_sidebar()
        finally:
            self._loading_session = False

    def _entries_for_session_key(self, session_key: str) -> list[SpectrumEntry]:
        return [
            entry for entry in self.spectrum_list.get_all_entries()
            if self.spectrum_list.get_session_key_for_entry(entry) == session_key
        ]

    def _current_session_key(self) -> str:
        if self._current_entry is not None:
            return self.spectrum_list.get_session_key_for_entry(self._current_entry)
        return self.spectrum_list.get_current_session_filter()

    def _safe_session_filename(self, label: str) -> str:
        safe = "".join(
            ch if ch.isalnum() or ch in (" ", ".", "_", "-") else "_"
            for ch in (label or "session")
        ).strip()
        return f"{safe or 'session'}.irsession"

    def _build_session_payload(self, entries: list[SpectrumEntry]) -> dict:
        entry_names = {entry.name for entry in entries}
        entry_paths = {entry.filepath for entry in entries}
        potentials_all = self.spectrum_list.get_potentials()
        potentials = {
            name: potential
            for name, potential in potentials_all.items()
            if name in entry_names
        }
        fit_records = [
            copy.deepcopy(record)
            for record in self._fit_records
            if record.get('filename') in entry_names
        ]
        co_fit_records = [
            copy.deepcopy(record)
            for record in self._co_fit_records
            if record.get('filename') in entry_names
        ]
        total_shifts = {}
        for session_key, shifts in self._total_shifts.items():
            filtered = {
                name: float(shift)
                for name, shift in shifts.items()
                if name in entry_names
            }
            if filtered:
                total_shifts[session_key] = filtered

        stark_results = (
            calculate_stark_slopes(
                fit_records,
                potentials=potentials if potentials else None,
            )
            if fit_records else []
        )
        co_stark_results = (
            calculate_co_stark_slopes(
                co_fit_records,
                potentials=potentials if potentials else None,
            )
            if co_fit_records else []
        )

        return {
            'spectra': [
                {
                    'filepath':   e.filepath,
                    'name':       e.name,
                    'original_name': e.original_name or e.name,
                    'source_session_label': e.source_session_label,
                    'source_session_path': e.source_session_path,
                    'source_spectrum_path': e.source_spectrum_path or e.filepath,
                    'color':      e.color,
                    'fit_done':   e.fit_done,
                    'wavenumber': e.wavenumber,
                    'absorbance': e.absorbance,
                }
                for e in entries
            ],
            'spectrum_states': {
                fp: copy.deepcopy(state)
                for fp, state in self._spectrum_states.items()
                if fp in entry_paths
            },
            'fit_records':     fit_records,
            'potentials':      potentials,
            'stark_results':   stark_results,
            'co_states': {
                fp: copy.deepcopy(state)
                for fp, state in self._co_states.items()
                if fp in entry_paths
            },
            'co_fit_records':  co_fit_records,
            'co_stark_results': co_stark_results,
            'sio_states': {
                fp: copy.deepcopy(state)
                for fp, state in self._sio_states.items()
                if fp in entry_paths
            },
            'sio_ref_area':    self._sio_ref_area,
            'total_shifts':    total_shifts,
            'total_view_mode': self._total_view_mode,
        }

    def _save_entries(self, entries: list[SpectrumEntry], title: str,
                      suggested_name: str):
        if not entries:
            QMessageBox.information(self, title, "저장할 스펙트럼이 없습니다.")
            return
        fp, _ = QFileDialog.getSaveFileName(
            self, title, self._initial_session_dialog_path(suggested_name),
            "In Situ IR Analyzer Session (*.irsession)")
        if not fp:
            return
        if not fp.endswith('.irsession'):
            fp += '.irsession'
        self._remember_session_dialog_path(fp)

        data = self._build_session_payload(entries)
        try:
            save_session(fp, data)
            self.status_label.setText(
                f"Saved {len(entries)} spectra: {Path(fp).name}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def _save_session(self):
        entries = self.spectrum_list.get_all_entries()
        self._save_entries(entries, "Save Workspace", "workspace.irsession")

    def _save_session_key(self, session_key: str):
        entries = self._entries_for_session_key(session_key)
        label = self.spectrum_list.get_session_label_for_key(session_key)
        self._save_entries(
            entries,
            f"Save Session - {label}",
            self._safe_session_filename(label),
        )

    def _save_current_session(self):
        self._save_session_key(self._current_session_key())

    def _load_session(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "Load Session", self._initial_session_dialog_path(),
            "In Situ IR Analyzer Session (*.irsession *.session)")
        if not fp:
            return
        self._remember_session_dialog_path(fp)
        self._import_sessions_from_paths([fp])

    def _close_session_key(self, session_key: str):
        entries = self._entries_for_session_key(session_key)
        session_keys = self.spectrum_list.get_session_keys()
        if session_key not in session_keys and not entries:
            QMessageBox.information(
                self, "Close Session", "닫을 세션이 없습니다."
            )
            return

        label = self.spectrum_list.get_session_label_for_key(session_key)
        count = len(entries)
        reply = QMessageBox.question(
            self, "Close Session",
            f"{label} 세션의 {count}개 스펙트럼을 닫을까요?\n"
            "저장된 원본 파일과 .irsession 파일은 삭제되지 않습니다.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        self._loading_session = True
        try:
            removed = self.spectrum_list.remove_session(session_key)
            self._total_shifts.pop(session_key, None)
        finally:
            self._loading_session = False

        remaining = self.spectrum_list.get_all_entries()
        if remaining:
            row = self.spectrum_list.list_widget.currentRow()
            self._current_entry = self.spectrum_list.get_entry(row) if row >= 0 else None
            self._refresh_after_session_merge()
            if self.right_panel.get_mode() == 'Total':
                self._apply_total_view(preserve_view=False)
            elif self._current_entry is not None:
                self._on_spectrum_selected(self._current_entry)
        else:
            self._clear_display()
            self._refresh_analysis_for_visible_session()
            if self.right_panel.get_mode() == 'Total':
                self._apply_total_view(preserve_view=False)

        self.status_label.setText(
            f"Closed {label}: {len(removed)} spectra removed"
        )

    def _close_current_session(self):
        self._close_session_key(self._current_session_key())

    def _load_session_from_file(self, fp: str):
        try:
            data = load_session(fp)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))
            return

        # 현재 상태 초기화
        self._loading_session = True
        self.spectrum_list.clear_all()
        self.right_panel.clear_stark_results()
        self._spectrum_states  = {}
        self._fit_records      = []
        self._co_states        = {}
        self._co_fit_records   = []
        self._co_stark_results = []
        self._co_drag_targets  = {}
        self._sio_states       = {}
        self._sio_ref_area     = None
        self._total_shifts     = {}
        self._total_view_mode  = 'overlay'
        self._current_entry    = None
        self.plot_widget.reset_view()

        # 스펙트럼 복원
        spectra_data = data.get('spectra', [])
        self.spectrum_list.begin_bulk_update()
        try:
            for s in spectra_data:
                entry = SpectrumEntry(
                    filepath=s['filepath'],
                    name=s['name'],
                    wavenumber=s['wavenumber'],
                    absorbance=s['absorbance'],
                    color=s['color'],
                    fit_done=s.get('fit_done', False),
                    original_name=s.get('original_name', s['name']),
                    source_session_label=s.get('source_session_label', ''),
                    source_session_path=s.get('source_session_path', ''),
                    source_spectrum_path=s.get('source_spectrum_path', s['filepath']),
                )
                self.spectrum_list.add_entry(entry, select=False, emit_signal=False)
        finally:
            self.spectrum_list.end_bulk_update()

        # fit_done 마킹
        for i, s in enumerate(spectra_data):
            if s.get('fit_done', False):
                self.spectrum_list.mark_fit_done(i)

        # OH 분석 상태 복원
        self._spectrum_states = data.get('spectrum_states', {})
        self._fit_records     = data.get('fit_records', [])

        # CO / Si-O 상태 복원
        self._co_states      = data.get('co_states', {})
        self._co_fit_records = data.get('co_fit_records', [])
        self._co_stark_results = data.get('co_stark_results', [])
        self._co_drag_targets = {}
        self._sio_states     = data.get('sio_states', {})
        self._sio_ref_area   = data.get('sio_ref_area', None)
        self._total_shifts   = self._normalize_total_shifts(data.get('total_shifts', {}))
        self._total_view_mode = data.get('total_view_mode', 'overlay')
        self.right_panel.set_total_view_mode(self._total_view_mode)

        # Potential 테이블 복원 (저장된 값 그대로)
        saved_potentials = data.get('potentials', {})
        spectra_names    = [s['name'] for s in spectra_data]
        self.spectrum_list.set_potentials(spectra_names, saved_potentials, emit_changed=False)
        self._sync_analysis_sidebar()

        # Stark 결과 복원
        stark_results = data.get('stark_results', [])
        if stark_results:
            self._stark_results = stark_results
            self.right_panel.update_stark_results(stark_results)
            self.analysis_widget.update_plots(
                self._visible_fit_records(), self._visible_potentials(), stark_results)

        # CO Analysis 탭 복원
        if self._co_fit_records:
            self.analysis_widget.update_co_plots(
                self._visible_co_fit_records(),
                self._visible_potentials(),
                self._co_stark_results,
            )
        if self._co_stark_results:
            self.right_panel.update_co_stark_results(self._co_stark_results)
            self.analysis_widget.update_co_stark_results(self._co_stark_results)

        # Si-O 정규화 복원
        if self._current_entry is not None:
            self.right_panel.update_sio_area(self._get_sio_area_for_entry(self._current_entry))
            if self._fit_records and self._visible_sio_areas():
                self.analysis_widget.update_oh_normalized(
                    self._visible_fit_records(), self._visible_sio_areas(), self._visible_potentials())

        self._refresh_session_compare()
        self._loading_session = False

        # 세션을 열면 전체 스펙트럼 비교 화면을 먼저 보여준다.
        self.right_panel.set_mode('Total')

        # 첫 번째 스펙트럼 선택 → Total view 에서 active spectrum 강조
        if spectra_data:
            self.spectrum_list.list_widget.setCurrentRow(0)
        else:
            self._sync_analysis_sidebar()

        self.status_label.setText(
            f"Session loaded: {Path(fp).name}  |  {len(spectra_data)} spectra  |  Total view"
        )

    # ── Mode Change ───────────────────────────────────────────

    def _on_mode_changed(self, mode: str):
        self.plot_widget.clear_analysis_region()

        if mode == 'Total':
            self.plot_widget.clear_endpoint_items()
            self.plot_widget.clear_baseline_points()
            self._apply_total_view(preserve_view=False)
            self._sync_baseline_edit_state_for_current_spectrum()
            self._refresh_snapshot_panel()
            return

        if self._current_entry is None:
            row = self.spectrum_list.list_widget.currentRow()
            entry = self.spectrum_list.get_entry(row) if row >= 0 else None
            if entry is not None:
                self._current_entry = entry

        if self._current_entry is not None:
            self.plot_widget.set_raw_spectrum(
                self._current_entry.wavenumber,
                self._current_entry.absorbance,
            )

        if mode == 'OH':
            # CO/SiO guess 라인 및 endpoint 선 제거
            self.plot_widget.clear_endpoint_items()
            self.plot_widget._clear_guess_lines()

            cfg = self.right_panel.get_config()
            wn_min, wn_max = cfg['wn_min'], cfg['wn_max']
            self.plot_widget.show_analysis_region([(wn_min, wn_max)])
            self.plot_widget.zoom_to(wn_min, wn_max)

            if self._current_entry is not None:
                # 항상 baseline 재계산 → corrected spectrum + wn_crop 갱신
                self._update_baseline()
                # 저장된 피팅 결과 복원
                saved = self._spectrum_states.get(self._current_entry.filepath)
                if saved and saved.get('fit_result') is not None:
                    ab_pos = np.maximum(saved['ab_corrected'], 0)
                    self.plot_widget.show_fit_result(
                        saved['wn_crop'], ab_pos, saved['fit_result'])
                    self.right_panel.update_results(saved['fit_result'])
                    self.right_panel.set_guesses(saved.get('guesses', []), locks=saved.get('locks', []))
                elif saved and saved.get('guesses'):
                    self.plot_widget.show_peak_guesses(
                        self._wn_crop, saved['guesses'])
                    self.right_panel.set_guesses(saved['guesses'], locks=saved.get('locks', []))

        elif mode == 'CO':
            self._apply_co_view(self._current_entry)
            self._analyze_all_co(auto_triggered=True)

        elif mode == 'SiO':
            self._apply_sio_view(self._current_entry)

        if self._current_entry is not None:
            self._sync_baseline_edit_state_for_current_spectrum()
            if mode == 'CO':
                self.plot_widget.set_peak_locks(self.right_panel.get_co_locks())
            else:
                self.plot_widget.set_peak_locks(self.right_panel.get_locks())
            self._refresh_selected_overlays(self._capture_plot_view())
            self._refresh_snapshot_panel()

        if mode != 'CO':
            self.status_label.setText(f"Mode: {mode}")

    # ── CO Fit ────────────────────────────────────────────────

    def _manual_co_center(self, co_state: dict, sub: str,
                          ep0: float, ep1: float) -> float | None:
        sub_state = co_state.get(sub, {})
        if not sub_state.get('manual_center_override', False):
            return None
        center = sub_state.get('manual_center')
        if center is None:
            return None
        lo, hi = sorted((ep0, ep1))
        center = float(center)
        if lo <= center <= hi:
            return center
        return None

    def _co_result_area(self, result) -> float:
        if result and result.success and result.peaks:
            return float(getattr(result.peaks[0], 'area', 0.0))
        return 0.0

    def _update_co_fit_record_for_entry(self, entry: SpectrumEntry):
        co_state = self._co_states.get(entry.filepath, {})
        co_l = co_state.get('CO_L', {}).get('fit_result')
        co_b = co_state.get('CO_B', {}).get('fit_result')
        record = {'filename': entry.name, 'CO_L': co_l, 'CO_B': co_b}
        existing_idx = next((i for i, r in enumerate(self._co_fit_records)
                             if r['filename'] == entry.name), None)
        if existing_idx is not None:
            self._co_fit_records[existing_idx] = record
        else:
            self._co_fit_records.append(record)

    def _refresh_co_outputs_after_manual_edit(self):
        if self._current_entry is None:
            return
        co_state = self._co_states.get(self._current_entry.filepath, {})
        self._update_co_fit_record_for_entry(self._current_entry)
        self.right_panel.update_co_results(
            co_state.get('CO_L', {}).get('fit_result'),
            co_state.get('CO_B', {}).get('fit_result'),
        )
        self.analysis_widget.update_co_plots(
            self._visible_co_fit_records(),
            self._visible_potentials(),
        )
        self._refresh_co_stark_results(self._visible_potentials())

    def _set_manual_co_center(self, sub: str, center: float):
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        sub_state = co_state.setdefault(sub, {})
        old_result = sub_state.get('fit_result')
        area = self._co_result_area(old_result)
        sub_state['manual_center'] = float(center)
        sub_state['manual_center_override'] = True
        sub_state['fit_result'] = _make_co_result(float(center), area)
        self._refresh_co_outputs_after_manual_edit()

    def _refresh_co_b_result_from_raw_fit(self):
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        co_b_sub = co_state.setdefault('CO_B', {})
        raw_fit = co_b_sub.get('raw_fit_result')
        if raw_fit is None or not getattr(raw_fit, 'success', False) or not raw_fit.peaks:
            return
        co_b_i = max(range(len(raw_fit.peaks)), key=lambda i: raw_fit.peaks[i].center)
        wn_b = co_b_sub.get('wn_b')
        if (
            wn_b is not None
            and getattr(raw_fit, 'individual_curves', None)
            and co_b_i < len(raw_fit.individual_curves)
            and len(raw_fit.individual_curves[co_b_i]) == len(wn_b)
        ):
            area = abs(float(_trapezoid(raw_fit.individual_curves[co_b_i], wn_b)))
        else:
            area = float(getattr(raw_fit.peaks[co_b_i], 'area', 0.0))
        co_b_sub['fit_result'] = _make_co_result(raw_fit.peaks[co_b_i].center, area)
        self._refresh_co_outputs_after_manual_edit()

    def _get_co_endpoints_for_entry(self, entry: SpectrumEntry, prefer_plot: bool = False) -> dict:
        if prefer_plot and self._current_entry is not None and entry.filepath == self._current_entry.filepath:
            eps = self.plot_widget.get_co_endpoints()
            if eps:
                return {
                    'CO_L': tuple(sorted(eps.get('CO_L', (2000.0, 2100.0)))),
                    'CO_B': tuple(sorted(eps.get('CO_B', (1650.0, 1900.0)))),
                }

        co_state = self._co_states.setdefault(entry.filepath, {})
        auto_eps = auto_co_baseline_endpoints(entry.wavenumber, entry.absorbance)
        endpoints = {}
        for sub, default_eps in [('CO_L', (2000.0, 2100.0)), ('CO_B', (1650.0, 1900.0))]:
            ep0, ep1 = auto_eps.get(sub, default_eps)
            sub_state = co_state.setdefault(sub, {})
            if sub_state.get('manual_override', False):
                ep0 = sub_state.get('ep0', ep0)
                ep1 = sub_state.get('ep1', ep1)
            endpoints[sub] = tuple(sorted((ep0, ep1)))
        return endpoints

    def _should_use_co_b_deconv(self, wn_b: np.ndarray, ab_pos_b: np.ndarray) -> bool:
        fit_mode = self.right_panel.get_co_b_fit_mode()
        if fit_mode == 'always_2peak':
            return True
        if fit_mode == 'simple_only':
            return False

        guesses = find_peaks_second_derivative(wn_b, ab_pos_b, n_peaks=2)
        if len(guesses) < 2:
            return False

        by_amp = sorted(
            guesses,
            key=lambda g: getattr(g, 'amplitude', 0.0),
            reverse=True
        )
        by_center = sorted(guesses, key=lambda g: g.center)
        left_peak = by_center[0]
        right_peak = by_center[-1]

        amp0 = max(getattr(by_amp[0], 'amplitude', 0.0), 1e-9)
        amp1 = max(getattr(by_amp[1], 'amplitude', 0.0), 0.0)
        separation = abs(right_peak.center - left_peak.center)
        area_ratio = abs(float(_trapezoid(ab_pos_b, wn_b))) / max(float(np.max(ab_pos_b)), 1e-9)
        has_h2o_like_left = 1635.0 <= left_peak.center <= 1705.0
        has_co_like_right = 1740.0 <= right_peak.center <= 1865.0
        return (
            has_h2o_like_left
            and has_co_like_right
            and separation >= 45.0
            and amp1 / amp0 >= 0.12
            and area_ratio >= 32.0
        )

    def _co_uses_endpoint_linear_baseline(self, algo: str | None = None) -> bool:
        if algo is None:
            algo = self.right_panel.get_config().get('baseline_algo', 'Linear')
        return algo == 'Linear'

    def _co_baseline_store(self, entry: SpectrumEntry) -> dict:
        co_state = self._co_states.setdefault(entry.filepath, {})
        return co_state.setdefault('_baseline', {})

    def _compute_co_full_baseline(self, entry: SpectrumEntry, algo: str | None = None,
                                  params: dict | None = None, restore_points: bool = False,
                                  manual_override: bool | None = None):
        cfg = self.right_panel.get_config()
        if algo is None:
            algo = cfg['baseline_algo']
        if params is None:
            params = cfg['baseline_params']

        wn, ab = crop_region(entry.wavenumber, entry.absorbance,
                             cfg['wn_min'], cfg['wn_max'])
        store = self._co_baseline_store(entry)
        points = [
            pt for pt in store.get('points', [])
            if min(cfg['wn_min'], cfg['wn_max']) <= pt[0] <= max(cfg['wn_min'], cfg['wn_max'])
        ]
        if manual_override is None:
            manual_override = bool(store.get('manual_override', False))

        if len(wn) == 0:
            bl = np.zeros_like(ab)
        elif algo == 'OH Auto Baseline':
            if manual_override and len(points) >= 2:
                pass
            else:
                points = auto_oh_baseline_points(wn, ab)
                manual_override = False
            bl = baseline_from_points(wn, ab, points) if len(points) >= 2 else np.zeros_like(ab)
        elif algo == 'Manual':
            bl = baseline_from_points(wn, ab, points) if len(points) >= 2 else np.zeros_like(ab)
            manual_override = True
        elif algo == 'Rubber Band':
            points = []
            bl = baseline_rubberband(wn, ab)
            manual_override = False
        elif algo == 'ARPLS':
            points = []
            bl = baseline_arpls(ab, lam=params.get('lam', 1e4))
            manual_override = False
        elif algo == 'SNIP':
            points = []
            bl = baseline_snip(ab, n_iter=params.get('n_iter', 50))
            manual_override = False
        else:
            points = []
            bl = baseline_linear(wn, ab) if len(wn) else np.zeros_like(ab)
            manual_override = False

        corrected = subtract_baseline(ab, bl)
        store.update({
            'wn': wn.copy(),
            'ab': ab.copy(),
            'baseline': bl.copy(),
            'ab_corrected': corrected.copy(),
            'points': list(points),
            'algo': algo,
            'params': dict(params),
            'manual_override': bool(manual_override),
        })
        if restore_points and points:
            self.plot_widget.restore_baseline_points(points)
        return wn, ab, bl, corrected, points

    def _co_subregion_data(self, entry: SpectrumEntry, ep0: float, ep1: float,
                           full_baseline=None):
        ep0, ep1 = sorted((ep0, ep1))
        wn_sub, ab_sub = crop_region(entry.wavenumber, entry.absorbance, ep0, ep1)
        if len(wn_sub) == 0:
            return wn_sub, ab_sub, np.zeros_like(ab_sub), np.zeros_like(ab_sub)

        if full_baseline is not None:
            wn_full, _, bl_full, _, _ = full_baseline
            if len(wn_full) and len(bl_full) == len(wn_full):
                order = np.argsort(wn_full)
                bl_sub = np.interp(wn_sub, np.asarray(wn_full)[order], np.asarray(bl_full)[order])
            else:
                bl_sub = np.zeros_like(ab_sub)
        else:
            y0 = float(np.interp(ep0, entry.wavenumber, entry.absorbance))
            y1 = float(np.interp(ep1, entry.wavenumber, entry.absorbance))
            bl_sub = np.interp(wn_sub, [ep0, ep1], [y0, y1])
        return wn_sub, ab_sub, bl_sub, np.maximum(ab_sub - bl_sub, 0)

    def _co_display_baseline(self, baseline, full_baseline):
        """CO fit graphics use raw baseline only when the plot is still raw."""
        return None if full_baseline is not None else baseline

    def _update_co_baseline_for_current(self, algo: str | None = None,
                                        params: dict | None = None,
                                        manual_override: bool | None = None):
        if self._current_entry is None:
            return
        if self._co_uses_endpoint_linear_baseline(algo):
            self.plot_widget.clear_baseline_curve()
            self._apply_co_view(self._current_entry, preserve_view=True)
            return
        self._compute_co_full_baseline(
            self._current_entry,
            algo=algo,
            params=params,
            restore_points=True,
            manual_override=manual_override,
        )
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        co_state.setdefault('CO_B', {}).pop('raw_fit_result', None)
        self._apply_co_view(self._current_entry, preserve_view=True)

    def _fit_co_entry(self, entry: SpectrumEntry, prefer_plot_eps: bool = False,
                      refresh_plot: bool = False) -> dict:
        cfg = self.right_panel.get_config()
        full_baseline = None
        if not self._co_uses_endpoint_linear_baseline(cfg.get('baseline_algo')):
            full_baseline = self._compute_co_full_baseline(
                entry,
                algo=cfg['baseline_algo'],
                params=cfg['baseline_params'],
            )
        eps = self._get_co_endpoints_for_entry(entry, prefer_plot=prefer_plot_eps)

        results = {}
        co_state = self._co_states.setdefault(entry.filepath, {})

        ep0_l, ep1_l = eps['CO_L']
        wn_l, _, _, ab_pos_l = self._co_subregion_data(
            entry, ep0_l, ep1_l, full_baseline=full_baseline)
        if len(wn_l) >= 3:
            peak_idx_l = int(np.argmax(ab_pos_l))
            center_l = self._manual_co_center(co_state, 'CO_L', ep0_l, ep1_l)
            if center_l is None:
                center_l = float(wn_l[peak_idx_l])
            results['CO_L'] = _make_co_result(
                center_l,
                abs(float(_trapezoid(ab_pos_l, wn_l)))
            )
        else:
            results['CO_L'] = None

        used_deconv = False
        needs_review = False
        ep0_b, ep1_b = eps['CO_B']
        wn_b, _, bl_b, ab_pos_b = self._co_subregion_data(
            entry, ep0_b, ep1_b, full_baseline=full_baseline)
        if len(wn_b) >= 5:
            used_deconv = self._should_use_co_b_deconv(wn_b, ab_pos_b)
            co_b_sub = co_state.setdefault('CO_B', {})

            if used_deconv:
                guesses = co_b_sub.get('guesses')
                if guesses and any(not (ep0_b <= g.center <= ep1_b) for g in guesses):
                    guesses = None
                if not guesses:
                    guesses = find_peaks_second_derivative(wn_b, ab_pos_b, n_peaks=2)

                if guesses:
                    shape = self.right_panel.get_peak_shape_co()
                    locks = list(co_b_sub.get('locks') or [])
                    if not locks:
                        manual_locks = set(
                            int(i) for i in co_b_sub.get('manual_center_locks', [])
                        )
                        locks = [
                            {'center': i in manual_locks,
                             'amplitude': False,
                             'sigma': False}
                            for i in range(len(guesses))
                        ]
                    fit_result = fit_peaks(wn_b, ab_pos_b, guesses, shape=shape,
                                           center_tolerance=80.0, locks=locks)
                    if fit_result.success and fit_result.peaks:
                        co_b_i = max(
                            range(len(fit_result.peaks)),
                            key=lambda i: fit_result.peaks[i].center
                        )
                        co_b_area = abs(float(
                            _trapezoid(fit_result.individual_curves[co_b_i], wn_b)
                        ))
                        results['CO_B'] = _make_co_result(
                            fit_result.peaks[co_b_i].center,
                            co_b_area
                        )

                        fitted_guesses = [
                            PeakGuess(
                                center=p.center,
                                amplitude=float(np.max(fit_result.individual_curves[i]))
                                if i < len(fit_result.individual_curves) else p.amplitude,
                                sigma=p.sigma,
                                index=i,
                                shape=getattr(p, 'shape', shape),
                            )
                            for i, p in enumerate(fit_result.peaks)
                        ]
                        co_b_sub['guesses'] = fitted_guesses
                        co_b_sub['locks'] = locks
                        co_b_sub['raw_fit_result'] = fit_result
                        co_b_sub['wn_b'] = wn_b
                        co_b_sub['ab_pos_b'] = ab_pos_b
                        co_b_sub['baseline_b'] = bl_b
                        needs_review = True

                        if refresh_plot:
                            self.plot_widget.show_fit_result(wn_b, ab_pos_b, fit_result,
                                                             baseline=self._co_display_baseline(bl_b, full_baseline))
                            self.right_panel.set_co_guesses(fitted_guesses, locks=locks)
                    else:
                        results['CO_B'] = None
                        co_b_sub.pop('raw_fit_result', None)
                        needs_review = True
                else:
                    results['CO_B'] = None
                    co_b_sub.pop('raw_fit_result', None)
                    needs_review = True
            else:
                peak_idx_b = int(np.argmax(ab_pos_b))
                center_b = self._manual_co_center(co_state, 'CO_B', ep0_b, ep1_b)
                if center_b is None:
                    center_b = float(wn_b[peak_idx_b])
                results['CO_B'] = _make_co_result(
                    center_b,
                    abs(float(_trapezoid(ab_pos_b, wn_b)))
                )
                co_b_sub.pop('guesses', None)
                co_b_sub.pop('raw_fit_result', None)
        else:
            results['CO_B'] = None
            needs_review = True

        for sub in ('CO_L', 'CO_B'):
            ep0, ep1 = eps[sub]
            sub_state = co_state.setdefault(sub, {})
            sub_state['ep0'] = ep0
            sub_state['ep1'] = ep1
            sub_state['fit_result'] = results.get(sub)
            sub_state['status'] = 'review' if needs_review else 'ok'
            sub_state['analysis_mode'] = 'deconv' if used_deconv and sub == 'CO_B' else 'simple'

        co_l = results.get('CO_L')
        co_b = results.get('CO_B')
        fname = entry.name
        existing_idx = next((i for i, r in enumerate(self._co_fit_records)
                             if r['filename'] == fname), None)
        record = {'filename': fname, 'CO_L': co_l, 'CO_B': co_b}
        if existing_idx is not None:
            self._co_fit_records[existing_idx] = record
        else:
            self._co_fit_records.append(record)

        return {
            'co_l': co_l,
            'co_b': co_b,
            'used_deconv': used_deconv,
            'needs_review': needs_review,
        }

    def _analyze_all_co(self, auto_triggered: bool = False):
        entries = self.spectrum_list.get_all_entries()
        if not entries:
            return

        self._co_stark_results = []
        if self.right_panel.get_mode() == 'CO':
            self.right_panel.update_co_stark_results([])
        analyzed = 0
        deconv_count = 0
        review_count = 0
        for entry in entries:
            outcome = self._fit_co_entry(entry, prefer_plot_eps=False, refresh_plot=False)
            analyzed += 1
            if outcome['used_deconv']:
                deconv_count += 1
            if outcome['needs_review']:
                review_count += 1

        if self._current_entry is not None and self.right_panel.get_mode() == 'CO':
            self._apply_co_view(self._current_entry)

        if self._co_fit_records:
            potentials = self.spectrum_list.get_potentials()
            self.analysis_widget.update_co_plots(self._visible_co_fit_records(), self._visible_potentials())
            self._refresh_co_stark_results(potentials)
        else:
            self.right_panel.update_co_stark_results([])

        prefix = "CO Auto Analyze" if auto_triggered else "CO Analyze All"
        self.status_label.setText(
            f"{prefix}  |  {analyzed} spectra  |  deconv {deconv_count}  |  review {review_count}"
        )

    def _run_co_fit(self):
        if self._current_entry is None:
            QMessageBox.warning(self, "No spectrum", "먼저 스펙트럼을 선택하세요.")
            return
        entry = self._current_entry
        self._co_stark_results = []
        self.right_panel.update_co_stark_results([])
        outcome = self._fit_co_entry(entry, prefer_plot_eps=True, refresh_plot=True)
        self._apply_co_view(entry)

        msg_parts = []
        for sub, r in [('CO_L', outcome['co_l']), ('CO_B', outcome['co_b'])]:
            if r and r.success and r.peaks:
                msg_parts.append(f"{sub}: {r.peaks[0].center:.1f} cm⁻¹")
            else:
                msg_parts.append(f"{sub}: FAILED")
        suffix = "  |  review recommended" if outcome['needs_review'] else ""
        self.status_label.setText("CO Reanalyze  |  " + "  ".join(msg_parts) + suffix)

        if self._co_fit_records:
            potentials = self.spectrum_list.get_potentials()
            self.analysis_widget.update_co_plots(self._visible_co_fit_records(), self._visible_potentials())
            self._refresh_co_stark_results(potentials)
        else:
            self.right_panel.update_co_stark_results([])

    def _refresh_co_stark_results(self, potentials: dict | None = None):
        if potentials is None:
            potentials = self._visible_potentials()
        co_fit_records = self._visible_co_fit_records()
        if not co_fit_records:
            self._co_stark_results = []
            self.right_panel.update_co_stark_results([])
            self.analysis_widget.update_co_stark_results([])
            return
        results = calculate_co_stark_slopes(
            co_fit_records,
            potentials=potentials if potentials else None
        )
        self._co_stark_results = results
        self.right_panel.update_co_stark_results(results)
        self.analysis_widget.update_co_stark_results(results)

    # ── CO_B Auto Detect ──────────────────────────────────────

    def _auto_detect_co_b(self):
        if self._current_entry is None:
            QMessageBox.warning(self, "No spectrum", "먼저 스펙트럼을 선택하세요.")
            return

        entry = self._current_entry
        wn_full = entry.wavenumber
        ab_full = entry.absorbance

        eps = self.plot_widget.get_co_endpoints()
        if not eps or 'CO_B' not in eps:
            QMessageBox.warning(self, "CO_B", "CO 모드에서 실행하세요.")
            return

        ep0_b, ep1_b = sorted(eps['CO_B'])
        cfg = self.right_panel.get_config()
        full_baseline = None
        if not self._co_uses_endpoint_linear_baseline(cfg.get('baseline_algo')):
            full_baseline = self._compute_co_full_baseline(
                entry,
                algo=cfg['baseline_algo'],
                params=cfg['baseline_params'],
            )
        wn_b, _, bl_b, ab_pos_b = self._co_subregion_data(
            entry, ep0_b, ep1_b, full_baseline=full_baseline)
        if len(wn_b) < 5:
            return

        co_state = self._co_states.setdefault(entry.filepath, {})
        co_b_sub = co_state.setdefault('CO_B', {})
        guesses = co_b_sub.get('guesses')
        if guesses and any(not (ep0_b <= g.center <= ep1_b) for g in guesses):
            guesses = None
        if not guesses:
            guesses = find_peaks_second_derivative(wn_b, ab_pos_b, n_peaks=2)
        if not guesses:
            self.status_label.setText("CO_B: 피크를 자동 감지할 수 없습니다.")
            return

        co_b_sub['guesses'] = guesses
        co_b_sub['locks'] = [
            {'center': False, 'amplitude': False, 'sigma': False}
            for _ in guesses
        ]
        co_b_sub.pop('manual_center_locks', None)
        co_b_sub.pop('raw_fit_result', None)
        co_b_sub['wn_b'] = wn_b
        co_b_sub['ab_pos_b'] = ab_pos_b
        co_b_sub['baseline_b'] = bl_b
        self._co_drag_targets = {
            i: {'type': 'co_b_guess', 'sub': 'CO_B', 'peak_idx': i}
            for i in range(len(guesses))
        }
        self.right_panel.set_co_guesses(guesses, locks=co_b_sub['locks'])
        self.plot_widget.show_peak_guesses(
            wn_b,
            guesses,
            baseline=self._co_display_baseline(bl_b, full_baseline),
        )
        self.status_label.setText(
            f"CO_B: {len(guesses)}개 피크 감지  —  드래그로 위치 조정 후 Reanalyze Current")

    def _restore_co_b_fit_viz(self, entry: SpectrumEntry, co_state: dict):
        """스펙트럼 전환 / 모드 전환 시 CO_B 피팅 시각화 복원"""
        co_b_sub = co_state.get('CO_B', {})
        raw_fit = co_b_sub.get('raw_fit_result')
        if raw_fit is None or not raw_fit.success:
            return
        ep0_b = co_state.get('CO_B', {}).get('ep0', 1650.0)
        ep1_b = co_state.get('CO_B', {}).get('ep1', 1900.0)
        ep0_b, ep1_b = sorted((ep0_b, ep1_b))
        cfg = self.right_panel.get_config()
        full_baseline = None
        if not self._co_uses_endpoint_linear_baseline(cfg.get('baseline_algo')):
            full_baseline = self._compute_co_full_baseline(
                entry,
                algo=cfg['baseline_algo'],
                params=cfg['baseline_params'],
            )

        fit_len = 0
        if getattr(raw_fit, 'individual_curves', None):
            fit_len = len(raw_fit.individual_curves[0])

        stored_wn = co_b_sub.get('wn_b')
        stored_ab_pos = co_b_sub.get('ab_pos_b')
        stored_bl = co_b_sub.get('baseline_b')
        if fit_len and stored_wn is not None and len(stored_wn) == fit_len:
            wn_b = np.asarray(stored_wn, dtype=float)
            if stored_ab_pos is not None and len(stored_ab_pos) == fit_len:
                ab_pos_b = np.asarray(stored_ab_pos, dtype=float)
            else:
                ab_pos_b = np.zeros_like(wn_b)
            if full_baseline is None and stored_bl is not None and len(stored_bl) == fit_len:
                bl_b = np.asarray(stored_bl, dtype=float)
            else:
                _, _, bl_b, _ = self._co_subregion_data(
                    entry, ep0_b, ep1_b, full_baseline=full_baseline)
            self.plot_widget.show_fit_result(
                wn_b,
                ab_pos_b,
                raw_fit,
                baseline=self._co_display_baseline(bl_b, full_baseline),
            )
            self._co_drag_targets = {
                i: {'type': 'co_b_fit', 'sub': 'CO_B', 'peak_idx': i}
                for i in range(len(raw_fit.peaks))
            }
            return

        wn_b, _, bl_b, ab_pos_b = self._co_subregion_data(
            entry, ep0_b, ep1_b, full_baseline=full_baseline)
        if len(wn_b) < 5:
            return
        self.plot_widget.show_fit_result(
            wn_b,
            ab_pos_b,
            raw_fit,
            baseline=self._co_display_baseline(bl_b, full_baseline),
        )
        self._co_drag_targets = {
            i: {'type': 'co_b_fit', 'sub': 'CO_B', 'peak_idx': i}
            for i in range(len(raw_fit.peaks))
        }

    # ── Si-O Area ─────────────────────────────────────────────

    def _calc_sio_area(self):
        if self._current_entry is None:
            QMessageBox.warning(self, "No spectrum", "먼저 스펙트럼을 선택하세요.")
            return

        entry = self._current_entry
        wn_full = entry.wavenumber
        ab_full = entry.absorbance

        # endpoint 라인이 없으면 초기화
        if 'SiO_0' not in self.plot_widget._ep_lines:
            sio_state = self._sio_states.get(entry.filepath, {})
            eps = (sio_state.get('ep0', 1100.0), sio_state.get('ep1', 1300.0))
            self.plot_widget.show_sio_baseline(wn_full, ab_full, eps)

        ep0, ep1 = sorted(self.plot_widget.get_sio_endpoints())
        wn, ab = crop_region(wn_full, ab_full, ep0, ep1)
        if len(wn) < 3:
            QMessageBox.warning(self, "Si-O", "선택 영역이 너무 좁습니다.")
            return

        # 선형 베이스라인
        y0 = float(np.interp(ep0, wn_full, ab_full))
        y1 = float(np.interp(ep1, wn_full, ab_full))
        bl = np.interp(wn, [ep0, ep1], [y0, y1])
        ab_cor = ab - bl

        area = abs(float(_trapezoid(ab_cor, wn)))

        # 현재 세션 전체에 동일한 Si-O reference area를 적용
        target_session = self.spectrum_list.get_session_key_for_entry(entry)
        for session_entry in self.spectrum_list.get_all_entries():
            if self.spectrum_list.get_session_key_for_entry(session_entry) != target_session:
                continue
            session_state = dict(self._sio_states.get(session_entry.filepath, {}))
            if session_entry.filepath == entry.filepath:
                session_state['ep0'] = ep0
                session_state['ep1'] = ep1
            else:
                session_state.setdefault('ep0', 1100.0)
                session_state.setdefault('ep1', 1300.0)
            session_state['area'] = area
            self._sio_states[session_entry.filepath] = session_state
        self._sio_ref_area = area

        self.right_panel.update_sio_area(area)

        # 현재 스펙트럼의 OH total area / Si-O 정규화 업데이트
        oh_state = self._spectrum_states.get(entry.filepath)
        if oh_state and oh_state.get('fit_result') and oh_state['fit_result'].success:
            total_oh = sum(p.area for p in oh_state['fit_result'].peaks)
            self.right_panel.update_oh_total_area(total_oh, area)

        # Analysis 탭 normalized OH 업데이트 (스펙트럼별 Si-O area 사용)
        if self._fit_records and self._visible_sio_areas():
            self.analysis_widget.update_oh_normalized(
                self._visible_fit_records(), self._visible_sio_areas(), self._visible_potentials())

        self.status_label.setText(
            f"Si-O Area: {area:.4f}  |  {ep0:.0f}–{ep1:.0f} cm⁻¹")

    # ── Endpoint 드래그 핸들러 ────────────────────────────────

    def _on_co_endpoint_moved(self, sub: str, side: int, wn: float):
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        sub_state = co_state.setdefault(sub, {'ep0': 2000.0, 'ep1': 2100.0,
                                               'fit_result': None})
        if side == 0:
            sub_state['ep0'] = wn
        else:
            sub_state['ep1'] = wn
        sub_state['manual_override'] = True

    def _on_sio_endpoint_moved(self, side: int, wn: float):
        if self._current_entry is None:
            return
        sio_state = self._sio_states.setdefault(self._current_entry.filepath, {})
        if side == 0:
            sio_state['ep0'] = wn
        else:
            sio_state['ep1'] = wn

    def closeEvent(self, event):
        if self._analysis_window is not None:
            self._analysis_window.allow_close()
            self._analysis_window.close()
        super().closeEvent(event)
