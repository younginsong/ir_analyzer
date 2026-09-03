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
                               subtract_baseline)
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
CO_ANALYSIS_REGIONS = {
    'CO_L': (2000.0, 2100.0),
    'CO_B': (1650.0, 1900.0),
}
CO_DISPLAY_REGION = (1400.0, 2230.0)
CO_ASSIGNMENT_TARGETS = ('CO_L', 'CO_B')


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
        self._total_baseline_states: dict = {}  # {filepath: Total baseline/corrected state}
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
        self._total_inactive_ranges: dict[str, list[tuple[float, float]]] = {}
        self._total_integral_regions: dict[str, dict[str, dict]] = {}
        self._total_integral_results: dict[str, dict[str, list[dict]]] = {}
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
        self.plot_widget.cb_baseline.setVisible(False)
        self.right_panel.region_changed.connect(self._on_region_changed)
        self.right_panel.baseline_mode_toggled.connect(self._on_bl_mode_toggled)
        self.right_panel.baseline_apply.connect(self._on_bl_apply)
        self.right_panel.baseline_undo.connect(self._on_bl_undo)
        self.right_panel.baseline_clear.connect(self._on_bl_clear)
        self.right_panel.baseline_point_mode_changed.connect(
            self._on_baseline_point_mode_changed)
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
        self.right_panel.co_peak_add_mode_toggled.connect(
            self._on_co_peak_add_mode_toggled)
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
        self.right_panel.total_shift_toggled.connect(self._on_total_shift_toggled)
        self.right_panel.total_probe_toggled.connect(self.plot_widget.set_total_probe_mode)
        self.right_panel.total_reset_shifts.connect(self._reset_total_shifts)
        self.right_panel.oh_overlay_intensity_changed.connect(self._on_oh_overlay_intensity_changed)
        self.right_panel.total_integral_requested.connect(self._calculate_total_integral)
        self.spectrum_list.potential_assignments_changed.connect(self._on_potential_assignments_changed)
        self.spectrum_list.session_filter_changed.connect(self._on_session_filter_changed)
        self.spectrum_list.workspace_created.connect(self._on_workspace_created)
        self.spectrum_list.session_save_requested.connect(self._save_session_key)
        self.spectrum_list.session_close_requested.connect(self._close_session_key)
        self.plot_widget.peak_sigma_changed.connect(self._on_peak_sigma_changed)
        self.plot_widget.peak_amplitude_dragged.connect(self._on_peak_amplitude_dragged)
        self.plot_widget.sio_endpoint_moved.connect(self._on_sio_endpoint_moved)
        self.plot_widget.total_spectrum_selected.connect(self._on_total_spectrum_selected)
        self.plot_widget.total_shift_changed.connect(self._on_total_shift_changed)
        self.plot_widget.total_shift_mode_changed.connect(self.right_panel.set_total_shift_checked)
        self.plot_widget.total_shift_mode_changed.connect(self._on_total_shift_toggled)
        self.plot_widget.total_probe_mode_changed.connect(self.right_panel.set_total_probe_checked)
        self.plot_widget.total_region_toggled.connect(self._on_total_region_toggled)

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

    def _saved_session_key_for_spectrum(self, spectrum: dict) -> str:
        saved_key = spectrum.get('session_key')
        if saved_key:
            return str(saved_key)
        label = str(spectrum.get('source_session_label') or '').strip()
        return label if label else self.spectrum_list.LOOSE_FILES_KEY

    def _session_key_for_import_label(self, label: str) -> str:
        label = str(label or '').strip()
        return label if label else self.spectrum_list.LOOSE_FILES_KEY

    def _workspace_session_meta(self, data: dict, spectra_data: list[dict]) -> list[dict]:
        raw_meta = data.get('workspace_sessions')
        meta_by_key = {}
        ordered_keys = []
        if isinstance(raw_meta, (list, tuple)):
            for item in raw_meta:
                if not isinstance(item, dict):
                    continue
                key = str(item.get('key') or '').strip()
                if not key:
                    continue
                if key not in ordered_keys:
                    ordered_keys.append(key)
                meta_by_key[key] = {
                    'key': key,
                    'label': str(item.get('label') or '').strip(),
                }

        for spectrum in spectra_data:
            key = self._saved_session_key_for_spectrum(spectrum)
            if key not in ordered_keys:
                ordered_keys.append(key)
            if key not in meta_by_key:
                label = str(spectrum.get('source_session_label') or '').strip()
                if key == self.spectrum_list.LOOSE_FILES_KEY:
                    label = ""
                meta_by_key[key] = {'key': key, 'label': label}

        return [meta_by_key[key] for key in ordered_keys if key in meta_by_key]

    def _session_import_label_map(self, data: dict, session_path: str,
                                  spectra_data: list[dict]) -> tuple[dict[str, str], bool]:
        session_meta = self._workspace_session_meta(data, spectra_data)
        used_keys = {self._saved_session_key_for_spectrum(s) for s in spectra_data}
        session_meta = [meta for meta in session_meta if meta['key'] in used_keys]
        is_workspace = bool(data.get('workspace_sessions')) and len(session_meta) > 1
        if not is_workspace:
            distinct = {
                self._saved_session_key_for_spectrum(s)
                for s in spectra_data
            }
            is_workspace = len(distinct) > 1

        existing_labels = self._session_labels_in_use()
        fallback_label = Path(session_path).stem
        if not is_workspace:
            mapped_label = self._make_unique_session_label(fallback_label, existing_labels)
            return {
                self._saved_session_key_for_spectrum(s): mapped_label
                for s in spectra_data
            }, False

        label_map = {}
        for meta in session_meta:
            key = meta['key']
            if key == self.spectrum_list.LOOSE_FILES_KEY:
                label_map[key] = ""
                continue
            base_label = meta.get('label') or key
            label_map[key] = self._make_unique_session_label(base_label, existing_labels)
        return label_map, True

    def _refresh_after_session_merge(self):
        self._refresh_analysis_for_visible_session()
        self.right_panel.update_sio_area(self._get_sio_area_for_entry(self._current_entry))

    def _refresh_analysis_for_visible_session(self):
        current_subtab = self.analysis_widget.get_current_subtab()
        potentials = self._visible_potentials()
        self._sync_analysis_sidebar()

        self._refresh_oh_stark_results(potentials)
        self._recalculate_total_integrals_for_session(
            self._current_total_integral_session_key(),
            update_analysis=False,
        )
        self._refresh_total_integral_analysis()

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

    def _current_total_integral_session_key(self) -> str:
        return self.spectrum_list.get_current_session_filter()

    def _normalize_total_integral_regions(self, regions) -> dict[str, dict[str, dict]]:
        if not isinstance(regions, dict):
            return {}
        normalized = {}
        for session_key, session_regions in regions.items():
            if not isinstance(session_regions, dict):
                continue
            for region_name, region in session_regions.items():
                if not isinstance(region, dict):
                    continue
                name = str(region.get('name') or region_name or 'Region').strip() or 'Region'
                try:
                    wn_min = float(region.get('wn_min'))
                    wn_max = float(region.get('wn_max'))
                except (TypeError, ValueError):
                    continue
                lo, hi = sorted((wn_min, wn_max))
                normalized.setdefault(str(session_key), {})[name] = {
                    'name': name,
                    'wn_min': lo,
                    'wn_max': hi,
                }
        return normalized

    def _merge_total_integral_region_set(self, session_key: str, regions: dict):
        normalized = self._normalize_total_integral_regions({
            session_key: regions or {},
        }).get(session_key, {})
        if not normalized:
            return
        target = self._total_integral_regions.setdefault(session_key, {})
        for name, region in normalized.items():
            target[name] = region

    def _last_total_integral_region(self, session_key: str | None = None) -> dict | None:
        key = session_key or self._current_total_integral_session_key()
        regions = self._total_integral_regions.get(key, {})
        if not regions:
            return None
        return copy.deepcopy(next(reversed(regions.values())))

    def _visible_total_integral_records(self) -> list[dict]:
        session_key = self._current_total_integral_session_key()
        visible_paths = {entry.filepath for entry in self._visible_entries()}
        records = []
        for rows in self._total_integral_results.get(session_key, {}).values():
            records.extend(
                copy.deepcopy(row)
                for row in rows
                if row.get('filepath') in visible_paths
            )
        return records

    def _update_total_integral_preview(self, records: list[dict],
                                       region_name: str | None = None):
        if self._current_entry is None:
            self.right_panel.update_total_integral_preview(None)
            return
        for record in records:
            if (
                record.get('filepath') == self._current_entry.filepath
                and (region_name is None or record.get('region_name') == region_name)
            ):
                self.right_panel.update_total_integral_preview(
                    record.get('area'), record.get('source'))
                return
        self.right_panel.update_total_integral_preview(None)

    def _refresh_total_integral_analysis(self, sync_controls: bool = False):
        region = self._last_total_integral_region()
        if sync_controls and region is not None:
            self.right_panel.set_total_integral_config(region)
        records = self._visible_total_integral_records()
        self.analysis_widget.update_integrated_areas(records)
        self._update_total_integral_preview(
            records,
            region.get('name') if region else None,
        )

    def _total_integral_row_for_entry(self, entry: SpectrumEntry,
                                      region: dict) -> dict | None:
        wn_min = float(region['wn_min'])
        wn_max = float(region['wn_max'])
        wn, _raw, _baseline, corrected, source = self._analysis_arrays_for_entry(
            entry, wn_min, wn_max)
        if len(wn) < 2 or len(corrected) < 2:
            return None
        n = min(len(wn), len(corrected))
        area = abs(float(_trapezoid(corrected[:n], wn[:n])))
        potentials = self.spectrum_list.get_potentials()
        session_key = self.spectrum_list.get_session_key_for_entry(entry)
        return {
            'session_key': session_key,
            'session_label': self.spectrum_list.get_session_label_for_key(session_key),
            'region_name': region['name'],
            'filename': entry.name,
            'filepath': entry.filepath,
            'potential': potentials.get(entry.name),
            'wn_min': min(wn_min, wn_max),
            'wn_max': max(wn_min, wn_max),
            'area': area,
            'source': 'corrected' if source == 'total' else 'raw',
        }

    def _recalculate_total_integrals_for_session(self, session_key: str,
                                                 update_analysis: bool = True):
        regions = self._total_integral_regions.get(session_key, {})
        if not regions:
            self._total_integral_results.pop(session_key, None)
            if update_analysis:
                self._refresh_total_integral_analysis()
            return

        entries = self._entries_for_session_key(session_key)
        session_results = {}
        for region_name, region in regions.items():
            rows = []
            for entry in entries:
                row = self._total_integral_row_for_entry(entry, region)
                if row is not None:
                    rows.append(row)
            session_results[region_name] = rows
        self._total_integral_results[session_key] = session_results
        if update_analysis:
            self._refresh_total_integral_analysis()

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

    def _selected_total_entries(self) -> list[SpectrumEntry]:
        entries = self.spectrum_list.get_selected_entries()
        if entries:
            return entries
        if self._current_entry is not None:
            return [self._current_entry]
        visible = self._visible_entries()
        return visible[:1]

    def _analysis_arrays_for_entry(self, entry: SpectrumEntry,
                                   wn_min: float, wn_max: float):
        """Return Total-corrected data when available, otherwise unmodified raw data."""
        state = self._total_baseline_states.get(entry.filepath, {})
        state_wn = state.get('wn')
        state_corrected = state.get('corrected')
        if state_wn is not None and state_corrected is not None:
            wn = np.asarray(state_wn, dtype=float)
            corrected = np.asarray(state_corrected, dtype=float)
            n = min(len(wn), len(corrected))
            if n:
                wn = wn[:n]
                corrected = corrected[:n]
                raw = np.asarray(state.get('raw', []), dtype=float)
                baseline = np.asarray(state.get('baseline', []), dtype=float)
                if len(raw) != n:
                    order = np.argsort(entry.wavenumber)
                    raw = np.interp(
                        wn,
                        np.asarray(entry.wavenumber, dtype=float)[order],
                        np.asarray(entry.absorbance, dtype=float)[order],
                    )
                else:
                    raw = raw[:n]
                if len(baseline) != n:
                    baseline = raw - corrected
                else:
                    baseline = baseline[:n]

                lo, hi = sorted((float(wn_min), float(wn_max)))
                mask = (wn >= lo) & (wn <= hi)
                if np.any(mask):
                    return (
                        wn[mask].copy(),
                        raw[mask].copy(),
                        baseline[mask].copy(),
                        corrected[mask].copy(),
                        'total',
                    )

        wn, raw = crop_region(
            entry.wavenumber, entry.absorbance, wn_min, wn_max)
        return (
            wn.copy(),
            raw.copy(),
            np.zeros_like(raw),
            raw.copy(),
            'raw',
        )

    def _prepare_oh_analysis_input(self, entry: SpectrumEntry):
        cfg = self.right_panel.get_config()
        wn, raw, baseline, corrected, source = self._analysis_arrays_for_entry(
            entry, cfg['wn_min'], cfg['wn_max'])
        self._wn_crop = wn
        self._ab_crop = raw
        self._baseline = baseline
        self._ab_corrected = corrected
        self._baseline_points = []
        self.plot_widget.show_highlighted_region(wn, corrected)
        self._refresh_selected_overlays(self._capture_plot_view())
        if len(wn):
            total_oh = abs(float(_trapezoid(corrected, wn)))
            self.right_panel.update_oh_total_area(
                total_oh, self._get_sio_area_for_entry(entry))
        return source

    def _total_display_data_for_entry(self, entry: SpectrumEntry):
        state = self._total_baseline_states.get(entry.filepath)
        if state is not None:
            wn = state.get('wn')
            corrected = state.get('corrected')
            if wn is not None and corrected is not None:
                return np.asarray(wn, dtype=float), np.asarray(corrected, dtype=float)
        return np.asarray(entry.wavenumber, dtype=float), np.asarray(entry.absorbance, dtype=float)

    def _normalize_total_display(self, wn, ab):
        return self._normalize_total_values(wn, ab, ab)

    def _total_normalization_range_mask(self, wn):
        cfg = self.right_panel.get_oh_overlay_intensity_config()
        wn_arr = np.asarray(wn, dtype=float)
        return (wn_arr >= cfg['wn_min']) & (wn_arr <= cfg['wn_max'])

    def _total_normalization_divisor(self, wn, reference_ab) -> float:
        active_ab = self._mask_total_inactive_ranges(wn, reference_ab)
        max_value = self._max_in_wavenumber_range(
            wn,
            active_ab,
            self.right_panel.get_oh_overlay_intensity_config()['wn_min'],
            self.right_panel.get_oh_overlay_intensity_config()['wn_max'],
        )
        if max_value is None or abs(max_value) <= 1e-12:
            return 1.0
        return max_value

    def _normalize_total_values(self, wn, values, reference_ab):
        cfg = self.right_panel.get_oh_overlay_intensity_config()
        if cfg['mode'] != 'normalize':
            return values
        divisor = self._total_normalization_divisor(wn, reference_ab)
        return np.asarray(values, dtype=float) / divisor

    def _total_state_has_effective_baseline(self, entry: SpectrumEntry) -> bool:
        state = self._total_baseline_states.get(entry.filepath, {})
        if len(state.get('points', [])) >= 2:
            return True
        baseline = np.asarray(state.get('baseline', []), dtype=float)
        finite = baseline[np.isfinite(baseline)]
        return bool(len(finite) and np.any(np.abs(finite) > 1e-12))

    def _total_comparison_display(self, entry: SpectrumEntry, wn, ab):
        values = np.asarray(ab, dtype=float)
        if not self._total_state_has_effective_baseline(entry):
            active_values = self._mask_total_inactive_ranges(wn, values)
            finite = active_values[np.isfinite(active_values)]
            if len(finite):
                threshold = float(np.percentile(finite, 20.0))
                low_envelope = finite[finite <= threshold]
                if len(low_envelope):
                    values = values - float(np.median(low_envelope))
        return self._normalize_total_values(wn, values, values)

    def _current_total_inactive_ranges(self) -> list[tuple[float, float]]:
        session_key = self.spectrum_list.get_current_session_filter()
        return list(self._total_inactive_ranges.get(session_key, []))

    def _mask_total_inactive_ranges(self, wn, ab):
        ranges = self._current_total_inactive_ranges()
        if not ranges:
            return ab
        wn_arr = np.asarray(wn, dtype=float)
        masked = np.asarray(ab, dtype=float).copy()
        n = min(len(wn_arr), len(masked))
        for start, end in ranges:
            lo, hi = sorted((float(start), float(end)))
            region_mask = (wn_arr[:n] >= lo) & (wn_arr[:n] <= hi)
            masked[np.flatnonzero(region_mask)] = np.nan
        return masked

    def _merge_total_ranges(self, ranges):
        merged = []
        for start, end in sorted(
            (sorted((float(a), float(b))) for a, b in ranges),
            key=lambda pair: pair[0],
        ):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(start, end) for start, end in merged]

    def _on_total_region_toggled(self, start: float, end: float):
        if self.right_panel.get_mode() != 'Total':
            return
        lo, hi = sorted((float(start), float(end)))
        if hi - lo <= 1e-9:
            return

        session_key = self.spectrum_list.get_current_session_filter()
        ranges = self._current_total_inactive_ranges()
        midpoint = (lo + hi) * 0.5
        reactivate = any(a <= midpoint <= b for a, b in ranges)

        if reactivate:
            updated = []
            for a, b in ranges:
                if hi <= a or lo >= b:
                    updated.append((a, b))
                    continue
                if a < lo:
                    updated.append((a, min(lo, b)))
                if hi < b:
                    updated.append((max(hi, a), b))
            ranges = self._merge_total_ranges(updated)
            action = "Activated"
        else:
            ranges = self._merge_total_ranges(ranges + [(lo, hi)])
            action = "Deactivated"

        self._total_inactive_ranges[session_key] = ranges
        self._apply_total_view(preserve_view=True)
        self.status_label.setText(
            f"{action} Total region: {lo:.1f}-{hi:.1f} cm⁻¹"
        )

    def _calculate_total_integral(self):
        if self.right_panel.get_mode() != 'Total':
            return
        session_key = self._current_total_integral_session_key()
        entries = self._entries_for_session_key(session_key)
        if not entries:
            QMessageBox.warning(self, "No spectra", "현재 workspace에 적분할 스펙트럼이 없습니다.")
            return

        config = self.right_panel.get_total_integral_config()
        region_name = config['name']
        self._total_integral_regions.setdefault(session_key, {})[region_name] = config
        self._recalculate_total_integrals_for_session(session_key, update_analysis=True)
        self._apply_total_view(preserve_view=True)
        self._show_analysis_view(preferred_subtab='Integrated Areas')

        rows = self._total_integral_results.get(session_key, {}).get(region_name, [])
        self._update_total_integral_preview(rows, region_name)
        self.status_label.setText(
            f"Integrated {region_name}: "
            f"{config['wn_min']:.1f}-{config['wn_max']:.1f} cm⁻¹  |  "
            f"{len(rows)}/{len(entries)} spectra"
        )

    def _build_total_specs(self) -> list[dict]:
        entries = self._selected_total_entries()
        potentials = self._visible_potentials()
        session_key = self.spectrum_list.get_current_session_filter()
        session_shifts = self._total_shifts.get(session_key, {})
        allow_manual_shift = self.right_panel.is_total_shift_enabled()
        pot_values = [potentials[e.name] for e in entries if e.name in potentials]
        pot_min = min(pot_values) if pot_values else None
        pot_max = max(pot_values) if pot_values else None

        ordered = list(entries)
        if pot_values:
            ordered.sort(key=lambda e: potentials.get(e.name, float('inf')))

        display_data = {
            entry.filepath: self._total_display_data_for_entry(entry)
            for entry in ordered
        }
        ranges = []
        for entry in ordered:
            x, y = display_data[entry.filepath]
            y = self._total_comparison_display(entry, x, y)
            y = self._mask_total_inactive_ranges(x, y)
            finite = y[np.isfinite(y)]
            if len(finite) > 0:
                ranges.append(float(np.max(finite) - np.min(finite)))
        spacing = (np.median(ranges) * 1.25) if ranges else 1.0
        if not np.isfinite(spacing) or spacing <= 0:
            spacing = 1.0

        specs = []
        for idx, entry in enumerate(ordered):
            wn, ab = display_data[entry.filepath]
            ab = self._total_comparison_display(entry, wn, ab)
            ab = self._mask_total_inactive_ranges(wn, ab)
            base_shift = idx * spacing if self._total_view_mode == 'stack' else 0.0
            specs.append({
                'name': entry.name,
                'filepath': entry.filepath,
                'wn': wn,
                'ab': ab,
                'base_shift': base_shift,
                'shift': session_shifts.get(entry.name, 0.0) if allow_manual_shift else 0.0,
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
        self.plot_widget.set_total_inactive_ranges(
            self._current_total_inactive_ranges())
        integral_region = self._last_total_integral_region()
        if integral_region is not None:
            self.plot_widget.show_analysis_region(
                [(integral_region['wn_min'], integral_region['wn_max'])],
                color='#f9e2af',
            )
        if self.right_panel.btn_edit_bl.isChecked():
            state = (
                self._total_baseline_states.get(self._current_entry.filepath, {})
                if self._current_entry is not None else {}
            )
            active_spec = next(
                (spec for spec in specs
                 if spec.get('filepath') == getattr(self._current_entry, 'filepath', None)),
                None,
            )
            wn = np.asarray(state.get('wn', []), dtype=float)
            raw = np.asarray(state.get('raw', []), dtype=float)
            baseline = np.asarray(state.get('baseline', []), dtype=float)
            n = min(len(wn), len(raw), len(baseline))
            if n and active_spec is not None:
                display_raw = self._normalize_total_values(
                    wn[:n], raw[:n], raw[:n])
                display_raw = self._mask_total_inactive_ranges(
                    wn[:n], display_raw)
                display_raw = (
                    display_raw
                    + float(active_spec.get('base_shift', 0.0))
                    + float(active_spec.get('shift', 0.0))
                )
                self.plot_widget.set_total_edit_raw_curve(wn[:n], display_raw)
                display_baseline = self._normalize_total_values(
                    wn[:n], baseline[:n], raw[:n])
                display_baseline = self._mask_total_inactive_ranges(
                    wn[:n], display_baseline)
                display_baseline = (
                    display_baseline
                    + float(active_spec.get('base_shift', 0.0))
                    + float(active_spec.get('shift', 0.0))
                )
                self.plot_widget.set_baseline_curve(wn[:n], display_baseline)
            self._restore_total_baseline_points_for_current()
        if view_state:
            self._restore_plot_view(view_state)
        preserve_baseline_view = (
            view_state is not None
            and self.right_panel.btn_edit_bl.isChecked()
        )
        if self.right_panel.cb_x_auto.isChecked() and not preserve_baseline_view:
            finite_x = []
            for spec in specs:
                values = np.asarray(spec.get('wn', []), dtype=float)
                values = values[np.isfinite(values)]
                if len(values):
                    finite_x.append(values)
            if finite_x:
                all_x = np.concatenate(finite_x)
                self.plot_widget.zoom_to(
                    float(np.min(all_x)), float(np.max(all_x)), padding=0.03)
        if self.right_panel.cb_y_auto.isChecked() and not preserve_baseline_view:
            self.plot_widget.fit_y_to_current_x_range(padding=0.05)
        self.status_label.setText(
            f"Total view  |  {len(specs)} spectra  |  {self._total_view_mode}"
        )

    def _on_total_view_changed(self, view_mode: str):
        self._total_view_mode = 'stack' if view_mode == 'stack' else 'overlay'
        if self.right_panel.get_mode() == 'Total':
            self._apply_total_view(preserve_view=True)

    def _on_total_shift_toggled(self, enabled: bool):
        self.plot_widget.set_total_shift_mode(enabled)
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

    def _sanitize_total_baseline_states(self, states) -> dict:
        if not isinstance(states, dict):
            return {}
        sanitized = {}
        for filepath, state in states.items():
            if not isinstance(state, dict):
                continue
            algo = state.get('algo')
            is_unedited_auto = (
                algo in ('Auto Baseline', 'OH Auto Baseline')
                and not state.get('manual_override', False)
            )
            if is_unedited_auto:
                continue
            sanitized[filepath] = copy.deepcopy(state)
        return sanitized

    def _on_total_shift_changed(self, spectrum_name: str, shift: float):
        session_key = self.spectrum_list.get_current_session_filter()
        self._total_shifts.setdefault(session_key, {})[spectrum_name] = shift

    def _on_total_spectrum_selected(self, spectrum_name: str):
        entries = self.spectrum_list.get_all_entries()
        selected_paths = {
            item.data(Qt.UserRole)
            for item in self.spectrum_list.list_widget.selectedItems()
            if item is not None
        }
        for row, entry in enumerate(entries):
            if entry.name == spectrum_name:
                if self.spectrum_list.list_widget.currentRow() != row:
                    self.spectrum_list.list_widget.setCurrentRow(row)
                    for i in range(self.spectrum_list.list_widget.count()):
                        item = self.spectrum_list.list_widget.item(i)
                        if item is not None and item.data(Qt.UserRole) in selected_paths:
                            item.setSelected(True)
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

        session_label_map, is_workspace_payload = self._session_import_label_map(
            data, session_path, spectra_data)
        fallback_label = next(iter(session_label_map.values()), Path(session_path).stem)

        names_in_use = {entry.name for entry in self.spectrum_list.get_all_entries()}
        keys_in_use = {entry.filepath for entry in self.spectrum_list.get_all_entries()}
        old_to_new_name = {}
        old_to_new_fp = {}
        imported_entries = []

        for idx, spectrum in enumerate(spectra_data):
            original_name = spectrum.get('original_name') or spectrum.get('name') or Path(spectrum.get('filepath', '')).name
            old_session_key = self._saved_session_key_for_spectrum(spectrum)
            mapped_label = session_label_map.get(old_session_key, fallback_label)
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

        for old_fp, state in data.get('total_baseline_states', {}).items():
            new_fp = old_to_new_fp.get(old_fp)
            sanitized = self._sanitize_total_baseline_states({old_fp: state})
            if new_fp is not None and old_fp in sanitized:
                self._total_baseline_states[new_fp] = copy.deepcopy(state)

        imported_inactive_ranges = data.get('total_inactive_ranges', {})
        if isinstance(imported_inactive_ranges, dict):
            if is_workspace_payload:
                for old_session_key, ranges in imported_inactive_ranges.items():
                    if not isinstance(ranges, (list, tuple)):
                        continue
                    mapped_label = session_label_map.get(str(old_session_key))
                    if mapped_label is None:
                        continue
                    mapped_key = self._session_key_for_import_label(mapped_label)
                    self._total_inactive_ranges[mapped_key] = self._merge_total_ranges(ranges)
            else:
                merged_ranges = []
                for ranges in imported_inactive_ranges.values():
                    if isinstance(ranges, (list, tuple)):
                        merged_ranges.extend(ranges)
                if merged_ranges:
                    mapped_key = self._session_key_for_import_label(fallback_label)
                    self._total_inactive_ranges[mapped_key] = self._merge_total_ranges(
                        merged_ranges)

        imported_integral_regions = self._normalize_total_integral_regions(
            data.get('total_integral_regions', {}))
        if imported_integral_regions:
            if is_workspace_payload:
                for old_session_key, regions in imported_integral_regions.items():
                    mapped_label = session_label_map.get(str(old_session_key))
                    if mapped_label is None:
                        continue
                    mapped_key = self._session_key_for_import_label(mapped_label)
                    self._merge_total_integral_region_set(mapped_key, regions)
            else:
                mapped_key = self._session_key_for_import_label(fallback_label)
                for regions in imported_integral_regions.values():
                    self._merge_total_integral_region_set(mapped_key, regions)

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
                                else self.spectrum_list.LOOSE_FILES_KEY
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
        # Baseline is now created explicitly in Total. New spectra should not
        # receive OH-specific anchor points merely because they were loaded.
        return

    def _refresh_current_mode_view_after_defaults(self):
        row = self.spectrum_list.list_widget.currentRow()
        entry = self.spectrum_list.get_entry(row) if row >= 0 else self._current_entry
        if entry is None:
            return
        self._on_spectrum_selected(entry)

    def _on_spectrum_removed(self, index: int, filepath: str, name: str):
        self.spectrum_list.remove_spectrum_potential(name, emit_changed=False)

        # 상태 정리
        self._spectrum_states.pop(filepath, None)
        self._total_baseline_states.pop(filepath, None)
        self._fit_records = [r for r in self._fit_records
                             if r['filename'] != name]
        self._co_fit_records = [r for r in self._co_fit_records
                                if r['filename'] != name]
        self._co_states.pop(filepath, None)
        self._sio_states.pop(filepath, None)
        for shifts in self._total_shifts.values():
            shifts.pop(name, None)
        for by_region in self._total_integral_results.values():
            for region_name, rows in list(by_region.items()):
                by_region[region_name] = [
                    row for row in rows
                    if row.get('filepath') != filepath and row.get('filename') != name
                ]

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
        self._refresh_total_integral_analysis(sync_controls=True)
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
        self.right_panel.update_total_integral_preview(None)
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

    def _oh_display_data_for_entry(self, entry):
        if (
            self._current_entry is not None
            and entry.filepath == self._current_entry.filepath
            and self._wn_crop is not None
            and self._ab_corrected is not None
        ):
            return self._wn_crop, self._ab_corrected

        state = self._spectrum_states.get(entry.filepath)
        if state is not None:
            wn = state.get('wn_crop')
            ab = state.get('ab_corrected')
            if wn is not None and ab is not None:
                return wn, ab

        return entry.wavenumber, entry.absorbance

    def _max_in_wavenumber_range(self, wn, ab, wn_min: float, wn_max: float):
        wn_arr = np.asarray(wn, dtype=float)
        ab_arr = np.asarray(ab, dtype=float)
        if len(wn_arr) == 0 or len(ab_arr) == 0:
            return None
        n = min(len(wn_arr), len(ab_arr))
        wn_arr = wn_arr[:n]
        ab_arr = ab_arr[:n]
        lo, hi = sorted((float(wn_min), float(wn_max)))
        mask = (wn_arr >= lo) & (wn_arr <= hi) & np.isfinite(ab_arr)
        if not np.any(mask):
            return None
        max_value = float(np.nanmax(ab_arr[mask]))
        if not np.isfinite(max_value) or abs(max_value) < 1e-12:
            return None
        return max_value

    def _normalize_oh_overlay(self, wn, ab):
        cfg = self.right_panel.get_oh_overlay_intensity_config()
        if cfg['mode'] != 'normalize' or self._current_entry is None:
            return ab

        ref_wn, ref_ab = self._oh_display_data_for_entry(self._current_entry)
        ref_max = self._max_in_wavenumber_range(
            ref_wn, ref_ab, cfg['wn_min'], cfg['wn_max'])
        ab_max = self._max_in_wavenumber_range(
            wn, ab, cfg['wn_min'], cfg['wn_max'])
        if ref_max is None or ab_max is None:
            return ab

        return np.asarray(ab, dtype=float) * (ref_max / ab_max)

    def _build_overlay_spectra(self, entries):
        mode = self.right_panel.get_mode()
        overlays = []
        for entry in entries:
            wn = entry.wavenumber
            ab = entry.absorbance

            if mode == 'OH':
                wn, ab = self._oh_display_data_for_entry(entry)
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
        if self.right_panel.get_mode() in ('Total', 'SiO'):
            return
        selected_entries = self.spectrum_list.get_selected_entries()
        active_path = self._current_entry.filepath if self._current_entry is not None else None
        overlays = self._build_overlay_spectra(selected_entries)
        self.plot_widget.set_overlay_spectra(overlays, active_path)
        self._restore_plot_view(view_state)

    def _on_oh_overlay_intensity_changed(self):
        if self.right_panel.get_mode() != 'Total':
            return
        self._apply_total_view(preserve_view=True)

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
            baseline_edit_enabled=False,
        )

        self.plot_widget.set_raw_spectrum(
            self._current_entry.wavenumber,
            self._current_entry.absorbance,
        )
        self.plot_widget.show_highlighted_region(self._wn_crop, self._ab_corrected)
        self.plot_widget.show_analysis_region([(config['wn_min'], config['wn_max'])])

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
            self._apply_total_view(preserve_view=True)
            self._refresh_total_integral_analysis()
            self._sync_analysis_sidebar()
            return

        self.plot_widget.clear_baseline_points()
        # set_raw_spectrum 이 _clear_all() 을 호출하므로 Total 모드가 아닐 때만 실행
        self.plot_widget.set_raw_spectrum(entry.wavenumber, entry.absorbance)

        # 저장된 상태가 있으면 복원
        saved = self._spectrum_states.get(entry.filepath)
        if saved:
            self._wn_crop = saved.get('wn_crop')
            self._ab_crop = saved.get('ab_crop')
            self._baseline = saved.get('baseline')
            self._ab_corrected = saved.get('ab_corrected')
            self._fit_result = saved.get('fit_result')
            self._baseline_points = list(saved.get('baseline_points', []))

            if mode == 'OH' and self._wn_crop is not None and self._ab_corrected is not None:
                if self._ab_crop is None:
                    self._ab_crop = np.asarray(self._ab_corrected, dtype=float).copy()
                if self._baseline is None:
                    self._baseline = np.zeros_like(self._ab_corrected)
                self.plot_widget.show_highlighted_region(
                    self._wn_crop, self._ab_corrected)
                self.right_panel.set_guesses(saved.get('guesses', []), locks=saved.get('locks', []))

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
                    self.plot_widget.show_peak_guesses(self._wn_crop, saved.get('guesses', []))
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
        # 분석 영역 표시 복원 + 줌
        cfg = self.right_panel.get_config()
        if mode == 'OH':
            wn_min, wn_max = cfg['wn_min'], cfg['wn_max']
            self.plot_widget.show_analysis_region([(wn_min, wn_max)])
            self.plot_widget.zoom_to(wn_min, wn_max)
            if not saved or self._wn_crop is None or self._ab_corrected is None:
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
        view_state = self.plot_widget.get_view_state() if preserve_view else None
        wn_min, wn_max = self._co_display_region_for_entry(entry)
        self._co_drag_targets = {}
        self.plot_widget.clear_fit_result()
        self.plot_widget.clear_baseline_curve()
        self.plot_widget.clear_analysis_region()
        if entry is None:
            if not preserve_view:
                self.plot_widget.zoom_to(wn_min, wn_max)
            self.right_panel.set_co_guesses([])
            return

        wn, _, _, analysis_ab, _ = self._analysis_arrays_for_entry(
            entry, wn_min, wn_max)
        self.plot_widget.show_highlighted_region(wn, analysis_ab)

        co_state = self._co_states.setdefault(entry.filepath, {})
        manual = self._ensure_co_manual_state(entry)
        co_locks = manual.get('locks', [])
        raw_fit = manual.get('fit_result')
        co_guesses = manual.get('guesses') or []
        if raw_fit and raw_fit.success:
            self._restore_co_b_fit_viz(entry, co_state)
            self.right_panel.set_co_guesses(co_guesses, locks=co_locks)
        else:
            if co_guesses:
                wn_b, _, _, _ = self._co_b_fit_data(entry)
                if len(wn_b) >= 5:
                    self._co_drag_targets = {
                        i: {'type': 'co_peak_guess', 'peak_idx': i}
                        for i in range(len(co_guesses))
                    }
                    self.plot_widget.show_peak_guesses(
                        wn_b,
                        co_guesses,
                        baseline=None,
                    )
                self.right_panel.set_co_guesses(co_guesses, locks=co_locks)
            else:
                self.right_panel.set_co_guesses([])

        co_l = co_state.get('CO_L', {}).get('fit_result')
        co_b = co_state.get('CO_B', {}).get('fit_result')
        if not (raw_fit and raw_fit.success) and not co_guesses:
            markers = []
            self._co_drag_targets = {}
            for sub, result, color in (
                ('CO_L', co_l, '#89b4fa'),
                ('CO_B', co_b, '#fab387'),
            ):
                if result and result.success and result.peaks:
                    idx = len(markers)
                    markers.append({
                        'center': result.peaks[0].center,
                        'label': sub,
                        'color': color,
                    })
                    self._co_drag_targets[idx] = {'type': 'simple', 'sub': sub}
            if markers:
                self.plot_widget.show_peak_center_markers(markers)
        self.right_panel.update_co_results(co_l, co_b)
        if preserve_view and view_state:
            self.plot_widget.restore_view_state(view_state)
        else:
            self.plot_widget.zoom_to(wn_min, wn_max)

    def _apply_sio_view(self, entry: SpectrumEntry | None):
        cfg = self.right_panel.get_config()
        self.plot_widget._clear_all()
        self.plot_widget.show_analysis_region([(cfg['wn_min'], cfg['wn_max'])])
        if entry is None:
            self.plot_widget.zoom_to(cfg['wn_min'], cfg['wn_max'])
            return

        wn_full = np.asarray(entry.wavenumber, dtype=float)
        finite_wn = wn_full[np.isfinite(wn_full)]
        if len(finite_wn):
            display_min, display_max = float(np.min(finite_wn)), float(np.max(finite_wn))
        else:
            display_min, display_max = cfg['wn_min'], cfg['wn_max']
        wn_display, _, _, corrected_display, source = self._analysis_arrays_for_entry(
            entry, display_min, display_max)
        self.plot_widget.show_highlighted_region(wn_display, corrected_display)
        sio_state = self._sio_states.get(entry.filepath, {})
        eps = (
            float(sio_state.get('ep0', cfg['wn_min'])),
            float(sio_state.get('ep1', cfg['wn_max'])),
        )
        self.plot_widget.show_sio_region_handles(
            np.asarray(entry.wavenumber, dtype=float),
            np.asarray(entry.absorbance, dtype=float),
            eps,
        )
        self.plot_widget.zoom_to(cfg['wn_min'], cfg['wn_max'])

        self.right_panel.update_sio_area(self._get_sio_area_for_entry(entry))
        self.status_label.setText(
            f"Si-O view  |  {cfg['wn_min']:.0f}–{cfg['wn_max']:.0f} cm⁻¹  |  source={source}"
        )

    # ── 영역 / 베이스라인 ─────────────────────────────────────

    def _calculate_baseline_data(self, entry: SpectrumEntry, algo: str,
                                 params: dict, points: list | None = None):
        cfg = self.right_panel.get_config()
        wn, ab = crop_region(
            entry.wavenumber,
            entry.absorbance,
            cfg['wn_min'],
            cfg['wn_max'],
        )
        points = list(points or [])
        if len(wn) == 0:
            return {
                'wn': wn.copy(),
                'raw': ab.copy(),
                'baseline': np.zeros_like(ab),
                'corrected': ab.copy(),
                'points': [],
                'algo': algo,
                'params': copy.deepcopy(params),
                'region': (float(cfg['wn_min']), float(cfg['wn_max'])),
            }

        if algo == 'Manual':
            bl = baseline_from_points(wn, ab, points) if len(points) >= 2 else np.zeros_like(ab)
        elif algo == 'Rubber Band':
            points = []
            bl = baseline_rubberband(wn, ab)
        elif algo == 'ARPLS':
            points = []
            bl = baseline_arpls(ab, lam=params.get('lam', 1e4))
        elif algo == 'SNIP':
            points = []
            bl = baseline_snip(ab, n_iter=params.get('n_iter', 50))
        elif algo == 'Linear':
            points = []
            bl = baseline_linear(wn, ab)
        else:
            points = []
            bl = np.zeros_like(ab)

        return {
            'wn': wn.copy(),
            'raw': ab.copy(),
            'baseline': bl.copy(),
            'corrected': subtract_baseline(ab, bl),
            'points': list(points),
            'algo': algo,
            'params': copy.deepcopy(params),
            'region': (float(cfg['wn_min']), float(cfg['wn_max'])),
        }

    def _update_total_baseline_for_entries(self, entries: list[SpectrumEntry],
                                           algo: str | None = None,
                                           params: dict | None = None,
                                           manual_override: bool | None = None):
        if not entries:
            return
        cfg = self.right_panel.get_config()
        algo = algo or cfg['baseline_algo']
        params = params if params is not None else cfg['baseline_params']
        active_path = self._current_entry.filepath if self._current_entry is not None else None

        for entry in entries:
            existing = self._total_baseline_states.get(entry.filepath, {})
            entry_algo = algo
            points = []
            if entry_algo == 'Manual':
                if entry.filepath != active_path:
                    continue
                points = list(existing.get('points', []))
            state = self._calculate_baseline_data(entry, entry_algo, params, points)
            state['manual_override'] = (
                bool(manual_override)
                if manual_override is not None
                else entry_algo == 'Manual'
            )
            self._total_baseline_states[entry.filepath] = state

        affected_session_keys = {
            self.spectrum_list.get_session_key_for_entry(entry)
            for entry in entries
        }
        for session_key in affected_session_keys:
            self._recalculate_total_integrals_for_session(
                session_key,
                update_analysis=False,
            )
        self._refresh_total_integral_analysis()
        self._apply_total_view(preserve_view=True)

    def _restore_total_baseline_points_for_current(self):
        self.plot_widget.clear_baseline_points()
        if self._current_entry is None:
            return
        state = self._total_baseline_states.get(self._current_entry.filepath, {})
        points = state.get('points', [])
        if not points:
            return

        active_spec = next(
            (spec for spec in self._build_total_specs()
             if spec.get('filepath') == self._current_entry.filepath),
            None,
        )
        if active_spec is None:
            self.plot_widget.restore_baseline_points(points)
            return

        spec_wn = np.asarray(state.get('wn', active_spec['wn']), dtype=float)
        spec_ab = np.asarray(state.get('raw', active_spec['ab']), dtype=float)
        n = min(len(spec_wn), len(spec_ab))
        display_points = []
        for point_wn, point_y in points:
            if n == 0:
                continue
            display_y = self._normalize_total_values(
                spec_wn[:n],
                np.asarray([float(point_y)], dtype=float),
                spec_ab[:n],
            )[0]
            display_points.append((
                float(point_wn),
                float(display_y)
                + float(active_spec.get('base_shift', 0.0))
                + float(active_spec.get('shift', 0.0)),
            ))
        self.plot_widget.restore_baseline_points(display_points)

    def _on_region_changed(self, wn_min, wn_max):
        mode = self.right_panel.get_mode()
        if mode == 'CO':
            self.plot_widget.clear_analysis_region()
        else:
            self.plot_widget.show_analysis_region([(wn_min, wn_max)])

        if mode == 'Total':
            self.plot_widget.clear_analysis_region()
            self._update_total_baseline_for_entries(self._selected_total_entries())
            self.plot_widget.zoom_to(wn_min, wn_max)
        elif mode == 'OH':
            self._update_baseline()
            self._refresh_oh_stark_results(self._visible_potentials())
        elif mode == 'CO':
            if self._current_entry is not None:
                self._apply_co_view(self._current_entry)
            self.plot_widget.zoom_to(*self._co_display_region_for_entry(self._current_entry))
        elif mode == 'SiO':
            if self._current_entry is not None:
                sio_state = self._sio_states.setdefault(self._current_entry.filepath, {})
                sio_state['ep0'] = float(wn_min)
                sio_state['ep1'] = float(wn_max)
                sio_state['region'] = tuple(sorted((float(wn_min), float(wn_max))))
                self._apply_sio_view(self._current_entry)

    def _on_bl_mode_toggled(self, enabled: bool):
        """Edit Baseline 버튼 ON/OFF"""
        cfg = self.right_panel.get_config()
        mode = self.right_panel.get_mode()
        if mode != 'Total':
            self.plot_widget.set_baseline_edit_mode(False)
            self.plot_widget.clear_baseline_points()
            return
        is_editable = cfg['baseline_algo'] == 'Manual'
        self.plot_widget.set_baseline_edit_mode(enabled and is_editable)
        if enabled:
            self._update_total_baseline_for_entries(
                self._selected_total_entries(),
                algo=cfg['baseline_algo'],
                params=cfg['baseline_params'],
            )
            self._restore_total_baseline_points_for_current()
        else:
            self.plot_widget.clear_baseline_points()
            self._apply_total_view(preserve_view=True)

    def _on_bl_apply(self, algo: str, params: dict):
        """알고리즘 또는 파라미터 변경 시 즉시 재계산"""
        mode = self.right_panel.get_mode()
        if mode == 'Total':
            is_manual = (algo == 'Manual')
            bl_on = self.right_panel.btn_edit_bl.isChecked()
            self.plot_widget.set_baseline_edit_mode(bl_on and is_manual)
            if not is_manual:
                self.plot_widget.clear_baseline_points()
            self._update_total_baseline_for_entries(
                self._selected_total_entries(),
                algo=algo,
                params=params,
                manual_override=(True if is_manual else False),
            )
            if is_manual:
                self._restore_total_baseline_points_for_current()
            return

        self.plot_widget.set_baseline_edit_mode(False)

    def _on_bl_undo(self):
        mode = self.right_panel.get_mode()
        if mode == 'Total':
            if self._current_entry is None:
                return
            state = self._total_baseline_states.get(self._current_entry.filepath, {})
            points = list(state.get('points', []))
            if points:
                points.pop()
                state['points'] = points
                state['manual_override'] = True
                self._total_baseline_states[self._current_entry.filepath] = state
                self.plot_widget.undo_last_baseline_point()
                self._update_total_baseline_for_entries(
                    [self._current_entry], algo='Manual', manual_override=True)
            return

    def _on_bl_clear(self):
        mode = self.right_panel.get_mode()
        if mode == 'Total':
            if self._current_entry is None:
                return
            state = self._total_baseline_states.get(self._current_entry.filepath, {})
            state['points'] = []
            state['manual_override'] = True
            self._total_baseline_states[self._current_entry.filepath] = state
            self.plot_widget.clear_baseline_points()
            self._update_total_baseline_for_entries(
                [self._current_entry], algo='Manual', manual_override=True)
            return

    def _on_baseline_point_added(self, wn, ab):
        mode = self.right_panel.get_mode()
        if mode == 'Total':
            if self._current_entry is None:
                return
            raw_wn = np.asarray(self._current_entry.wavenumber, dtype=float)
            raw_ab = np.asarray(self._current_entry.absorbance, dtype=float)
            if len(raw_wn) == 0 or len(raw_ab) == 0:
                return
            if self.right_panel.get_baseline_point_mode() == 'free':
                snapped_point = (float(wn), float(ab))
            else:
                nearest = int(np.argmin(np.abs(raw_wn - float(wn))))
                snapped_point = (float(raw_wn[nearest]), float(raw_ab[nearest]))
            state = self._total_baseline_states.setdefault(
                self._current_entry.filepath, {})
            points = list(state.get('points', []))
            points = [pt for pt in points if abs(float(pt[0]) - snapped_point[0]) > 1e-9]
            points.append(snapped_point)
            state['points'] = points
            state['manual_override'] = True
            self._update_total_baseline_for_entries(
                [self._current_entry], algo='Manual', manual_override=True)
            return

    def _on_baseline_point_mode_changed(self, _mode: str):
        if self.right_panel.get_mode() == 'Total' and self._current_entry is not None:
            self.status_label.setText(
                "Baseline point mode: "
                + ("Free" if self.right_panel.get_baseline_point_mode() == 'free'
                   else "Follow Spectrum")
            )

    def _on_baseline_point_removed(self, idx: int):
        mode = self.right_panel.get_mode()
        if mode == 'Total':
            if self._current_entry is None:
                return
            state = self._total_baseline_states.get(self._current_entry.filepath, {})
            points = list(state.get('points', []))
            if 0 <= idx < len(points):
                points.pop(idx)
            state['points'] = points
            state['manual_override'] = True
            self._total_baseline_states[self._current_entry.filepath] = state
            self._update_total_baseline_for_entries(
                [self._current_entry], algo='Manual', manual_override=True)
            return

    def _save_current_spectrum_state(self, baseline_manual_override: bool | None = None):
        if self._current_entry is None or self._wn_crop is None:
            return
        existing = self._spectrum_states.get(self._current_entry.filepath, {})
        is_oh_mode = self.right_panel.get_mode() == 'OH'
        guesses = (
            self.right_panel.get_guesses()
            if is_oh_mode else existing.get('guesses', [])
        )
        locks = (
            self.right_panel.get_locks()
            if is_oh_mode else existing.get('locks', [])
        )
        fit_result = self._fit_result if is_oh_mode else existing.get('fit_result')
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
            'fit_result':     fit_result,
            'guesses':        guesses,
            'locks':          locks,
            'baseline_points': list(self._baseline_points),
            'baseline_manual_override': manual_override,
            'snapshots':      existing.get('snapshots', []),
        }

    def _update_baseline(self, algo: str = None, params: dict = None):
        if self._current_entry is None:
            return
        self._prepare_oh_analysis_input(self._current_entry)

    def _sync_baseline_edit_state_for_current_spectrum(self):
        """스펙트럼 전환 후 Edit Baseline 상태와 auto baseline 적용을 다시 맞춘다."""
        mode = self.right_panel.get_mode()
        cfg = self.right_panel.get_config()
        algo = cfg['baseline_algo']
        edit_on = self.right_panel.btn_edit_bl.isChecked()
        is_editable = (mode == 'Total' and algo == 'Manual')

        self.plot_widget.set_baseline_edit_mode(edit_on and is_editable)

        if edit_on and mode == 'Total':
            self._restore_total_baseline_points_for_current()

    # ── 활성 영역 줌 (Ctrl+A) ────────────────────────────────

    def _zoom_to_active_region(self):
        """현재 활성 분석 영역을 화면에 꽉 차게 줌 (Ctrl+A / Cmd+A)."""
        mode = self.right_panel.get_mode()
        cfg  = self.right_panel.get_config()

        if mode == 'Total':
            specs = self._build_total_specs()
            active_x = []
            series = []
            for spec in specs:
                wn = np.asarray(spec['wn'], dtype=float)
                ab = np.asarray(spec['ab'], dtype=float)
                n = min(len(wn), len(ab))
                if n == 0:
                    continue
                wn = wn[:n]
                display_y = (
                    ab[:n]
                    + float(spec.get('base_shift', 0.0))
                    + float(spec.get('shift', 0.0))
                )
                mask = np.isfinite(wn) & np.isfinite(display_y)
                if not np.any(mask):
                    continue
                active_x.append(wn[mask])
                series.append((wn, display_y))

            if active_x:
                x_values = np.concatenate(active_x)
                self.plot_widget.zoom_to(
                    float(np.min(x_values)),
                    float(np.max(x_values)),
                    padding=0.03,
                )
                self.plot_widget.fit_y_to_series_in_current_x_range(
                    series, padding=0.05)
            else:
                self.status_label.setText("Total view has no active region")

        elif mode == 'OH':
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
        locks = [
            {'center': False, 'amplitude': False, 'sigma': False}
            for _ in guesses
        ]
        self.right_panel.set_guesses(
            guesses,
            locks=locks,
        )
        self._fit_result = None
        self._fit_edit_pending = False
        self.right_panel.clear_results()
        self.plot_widget.show_peak_guesses(self._wn_crop, guesses)
        self._spectrum_states[self._current_entry.filepath] = {
            'wn_crop':        self._wn_crop.copy(),
            'ab_crop':        self._ab_crop.copy() if self._ab_crop is not None
                              else self._wn_crop.copy(),
            'baseline':       self._baseline.copy() if self._baseline is not None
                              else np.zeros_like(self._wn_crop),
            'ab_corrected':   self._ab_corrected.copy() if self._ab_corrected is not None
                              else np.zeros_like(self._wn_crop),
            'fit_result':     None,
            'guesses':        guesses,
            'locks':          locks,
            'baseline_points': list(self._baseline_points),
            'baseline_manual_override': self._spectrum_states.get(
                self._current_entry.filepath, {}
            ).get('baseline_manual_override', False),
            'snapshots':      self._get_oh_snapshots(self._current_entry.filepath),
        }
        entry_name = self._current_entry.name
        self._fit_records = [
            r for r in self._fit_records
            if r['filename'] != entry_name
        ]
        list_idx = self.spectrum_list.list_widget.currentRow()
        self.spectrum_list.clear_fit_done(list_idx)
        self.status_label.setText(f"Auto-detected {len(guesses)} peaks")

    # ── 피크 마우스 클릭 생성 ─────────────────────────────────

    def _on_peak_created(self, wn_pos: float, amplitude: float, sigma: float):
        """플롯 클릭 또는 우클릭 드래그 → 피크 추가.
        amplitude: 피크 높이 (absorbance 단위)
        sigma: Gaussian sigma (cm⁻¹)
        """
        if self.right_panel.get_mode() == 'CO':
            self._on_co_peak_created(wn_pos, amplitude, sigma)
            return
        if self._wn_crop is None:
            return
        self.right_panel.add_peak_guess(wn_pos, amplitude, sigma)
        guesses = self.right_panel.get_guesses()
        self.plot_widget.show_peak_guesses(self._wn_crop, guesses)
        self.status_label.setText(f"Peak added at {wn_pos:.0f} cm⁻¹  |  총 {len(guesses)}개")

    def _on_co_peak_add_mode_toggled(self, checked: bool):
        self.plot_widget.btn_add_peak.setChecked(checked)
        if checked:
            self.status_label.setText(
                "CO Add Peak Mode  |  plot click or right-drag to create a peak"
            )

    def _on_co_peak_created(self, wn_pos: float, amplitude: float, sigma: float):
        if self._current_entry is None:
            return
        wn_fit, _, _, ab_fit = self._co_b_fit_data(self._current_entry)
        if len(wn_fit) < 5:
            return

        manual = self._ensure_co_manual_state(self._current_entry)
        guesses = list(manual.get('guesses') or self.right_panel.get_co_guesses())
        locks = list(manual.get('locks') or self.right_panel.get_co_locks())
        idx = len(guesses)
        guess = PeakGuess(
            center=float(wn_pos),
            amplitude=max(float(amplitude), 1e-6),
            sigma=max(float(sigma), 1.0),
            index=idx,
            shape=self.right_panel.get_peak_shape_co(),
        )
        guess.assignment = 'Unassigned'
        guesses.append(guess)
        locks.append({'center': False, 'amplitude': False, 'sigma': False})

        manual['guesses'] = guesses
        manual['locks'] = locks
        manual['assignments'] = [self._co_peak_assignment(g) for g in guesses]
        manual.pop('fit_result', None)
        manual.pop('raw_fit_result', None)
        manual['wn'] = wn_fit
        manual['ab'] = ab_fit

        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        for sub in CO_ASSIGNMENT_TARGETS:
            co_state.setdefault(sub, {})['fit_result'] = None

        self.right_panel.set_co_guesses(guesses, locks=locks)
        self._on_co_peak_params_changed(self.right_panel.get_co_guesses())
        self.status_label.setText(
            f"CO peak added at {wn_pos:.1f} cm⁻¹  |  {len(guesses)} peaks"
        )

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
            self.status_label.setText(f"CO P{idx+1} height → {amplitude:.3f}")
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
            manual = self._ensure_co_manual_state(self._current_entry)
            locks = self.right_panel.get_co_locks()
            if idx < len(locks):
                locks[idx]['center'] = True
            manual['locks'] = locks
            manual['manual_center_locks'] = [
                i for i, lock in enumerate(locks) if lock.get('center', False)
            ]
            raw_fit = manual.get('fit_result')
            if (
                target
                and target.get('type') in ('co_b_fit', 'co_peak_fit')
                and raw_fit is not None
                and 0 <= idx < len(getattr(raw_fit, 'peaks', []))
            ):
                raw_fit.peaks[idx].center = float(new_center)
                self._refresh_co_manual_results_from_fit(self._current_entry)
        self.status_label.setText(
            f"CO P{idx + 1} center → {new_center:.1f} cm⁻¹"
        )

    def _update_co_b_guess(self, idx: int, new_center: float = None,
                           new_sigma: float = None, new_amplitude: float = None):
        if self._current_entry is None:
            return
        manual = self._ensure_co_manual_state(self._current_entry)
        guesses = manual.get('guesses', [])
        if 0 <= idx < len(guesses):
            if new_center is not None:
                guesses[idx].center = new_center
            if new_sigma is not None:
                guesses[idx].sigma = new_sigma
            if new_amplitude is not None:
                guesses[idx].amplitude = new_amplitude
            manual['assignments'] = [self._co_peak_assignment(g) for g in guesses]
            manual.pop('fit_result', None)
            manual.pop('raw_fit_result', None)

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
        return (
            *self._co_b_fit_data(self._current_entry),
            None,
        )

    def _on_co_peak_params_changed(self, guesses):
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        manual = self._ensure_co_manual_state(self._current_entry)
        old_guesses = list(manual.get('guesses') or [])
        old_fit = manual.get('fit_result')
        manual['guesses'] = list(guesses)
        manual['locks'] = self.right_panel.get_co_locks()
        manual['assignments'] = [self._co_peak_assignment(g) for g in guesses]
        manual['manual_center_locks'] = [
            i for i, lock in enumerate(manual['locks']) if lock.get('center', False)
        ]
        numeric_changed = len(old_guesses) != len(guesses)
        if not numeric_changed:
            for old, new in zip(old_guesses, guesses):
                if (
                    abs(float(old.center) - float(new.center)) > 1e-6
                    or abs(float(old.amplitude) - float(new.amplitude)) > 1e-9
                    or abs(float(old.sigma) - float(new.sigma)) > 1e-6
                    or getattr(old, 'shape', '') != getattr(new, 'shape', '')
                ):
                    numeric_changed = True
                    break
        if numeric_changed:
            manual.pop('fit_result', None)
            manual.pop('raw_fit_result', None)
            for sub in CO_ASSIGNMENT_TARGETS:
                co_state.setdefault(sub, {})['fit_result'] = None
        elif old_fit is not None:
            manual['fit_result'] = old_fit
            self._refresh_co_manual_results_from_fit(self._current_entry)
            self.right_panel.update_co_results(
                co_state.get('CO_L', {}).get('fit_result'),
                co_state.get('CO_B', {}).get('fit_result'),
            )
            wn_fit = manual.get('wn')
            ab_fit = manual.get('ab')
            if wn_fit is not None and ab_fit is not None:
                self.plot_widget.show_fit_result(
                    np.asarray(wn_fit, dtype=float),
                    np.asarray(ab_fit, dtype=float),
                    old_fit,
                    baseline=None,
                )
                self._co_drag_targets = {
                    i: {'type': 'co_peak_fit', 'peak_idx': i}
                    for i in range(len(getattr(old_fit, 'peaks', []) or []))
                }
                self.plot_widget.set_peak_locks(manual['locks'])
                return
        preview = self._co_b_preview_arrays_for_current()
        if preview is None:
            return
        wn_b, _, _, _, _ = preview
        if len(wn_b) >= 5 and guesses:
            self._co_drag_targets = {
                i: {'type': 'co_peak_guess', 'peak_idx': i}
                for i in range(len(guesses))
            }
            self.plot_widget.show_peak_guesses(
                wn_b,
                guesses,
                baseline=None,
            )
            self.plot_widget.set_peak_locks(manual['locks'])
        else:
            self.plot_widget.clear_fit_result()

    def _on_co_peak_locks_changed(self, locks):
        self.plot_widget.set_peak_locks(locks)
        if self._current_entry is None:
            return
        manual = self._ensure_co_manual_state(self._current_entry)
        manual['locks'] = locks
        manual['manual_center_locks'] = [
            i for i, lock in enumerate(locks) if lock.get('center', False)
        ]

    def _on_co_peaks_cleared(self):
        if self._current_entry is None:
            return
        co_state = self._co_states.setdefault(self._current_entry.filepath, {})
        manual = self._ensure_co_manual_state(self._current_entry)
        for key in ('guesses', 'locks', 'assignments', 'manual_center_locks',
                    'fit_result', 'raw_fit_result', 'wn', 'ab', 'baseline',
                    'wn_b', 'ab_pos_b', 'baseline_b'):
            manual.pop(key, None)
        legacy_co_b = co_state.get('CO_B', {})
        if isinstance(legacy_co_b, dict):
            for key in ('guesses', 'locks', 'manual_center_locks',
                        'raw_fit_result', 'wn_b', 'ab_pos_b', 'baseline_b'):
                legacy_co_b.pop(key, None)
        for sub in CO_ASSIGNMENT_TARGETS:
            co_state.setdefault(sub, {})['fit_result'] = None
            co_state.setdefault(sub, {})['status'] = 'review'
        self._co_drag_targets = {}
        self.plot_widget.clear_fit_result()
        self.right_panel.update_co_results(None, None)
        self._co_fit_records = [
            record for record in self._co_fit_records
            if record.get('filename') != self._current_entry.name
        ]
        self.analysis_widget.update_co_plots(
            self._visible_co_fit_records(),
            self._visible_potentials(),
        )
        self._refresh_co_stark_results(self._visible_potentials())
        self._apply_co_view(self._current_entry)
        self.status_label.setText("CO peaks cleared")

    def _on_co_peak_rows_deleted(self, guesses):
        self._on_co_peak_params_changed(guesses)
        self.status_label.setText(f"CO peak removed  |  {len(guesses)} peaks remain")

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

    def _visible_export_spectrum_states(self, prefer_total: bool = False) -> dict:
        states = {
            filepath: copy.deepcopy(state)
            for filepath, state in self._visible_spectrum_states().items()
        }
        if not prefer_total:
            return states

        visible_paths = {entry.filepath for entry in self.spectrum_list.get_visible_entries()}
        for filepath, state in self._total_baseline_states.items():
            if filepath not in visible_paths:
                continue
            states[filepath] = {
                'wn_crop': np.asarray(state.get('wn', []), dtype=float),
                'ab_crop': np.asarray(state.get('raw', []), dtype=float),
                'baseline': np.asarray(state.get('baseline', []), dtype=float),
                'ab_corrected': np.asarray(state.get('corrected', []), dtype=float),
                'fit_result': None,
                'guesses': [],
                'locks': [],
                'baseline_points': list(state.get('points', [])),
                'baseline_manual_override': bool(state.get('manual_override', False)),
                'snapshots': [],
            }
        return states

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
        export_spectrum_states = self._visible_export_spectrum_states(prefer_total=True)
        co_states = self._visible_co_states()
        fit_records = self._visible_fit_records()
        co_fit_records = self._visible_co_fit_records()
        integrated_records = self._visible_total_integral_records()

        fitted_count = sum(
            1 for s in spectrum_states.values()
            if s.get('fit_result') and s['fit_result'].success
        )
        oh_processed_count = sum(
            1 for s in export_spectrum_states.values()
            if s.get('wn_crop') is not None
            and (
                s.get('baseline') is not None
                or s.get('ab_corrected') is not None
            )
        )
        co_count = len(co_fit_records)
        integral_count = len(integrated_records)

        if fitted_count == 0 and oh_processed_count == 0 and co_count == 0 and integral_count == 0:
            QMessageBox.warning(self, "No result", "현재 세션에 내보낼 결과 또는 스펙트럼 상태가 없습니다.")
            return

        fp, _ = QFileDialog.getSaveFileName(
            self, "Export Results", "", "Excel (*.xlsx)")
        if not fp:
            return
        if not fp.endswith('.xlsx'):
            fp += '.xlsx'

        saved_files = []
        try:
            if fitted_count == 0 and (oh_processed_count > 0 or integral_count > 0):
                export_info = export_spectra_excel(
                    entries,
                    potentials,
                    fp,
                    spectrum_states=export_spectrum_states,
                    co_states=co_states,
                    integrated_records=integrated_records,
                )
                integral_text = (
                    f" / Integrals {export_info.get('integral_rows', 0)} rows"
                    if export_info.get('integral_rows', 0) else ""
                )
                saved_files.append(
                    f"{export_info['n_spectra']}개 스펙트럼"
                    f" / OH {export_info.get('oh_points', 0)} pts"
                    f"{integral_text}: {Path(fp).name}"
                )
            elif fitted_count > 1:
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
                                   sio_ref_area=sio_areas,
                                   integrated_records=integrated_records)
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
                              fp, fitted_entry.name,
                              integrated_records=integrated_records)
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
                spectrum_states=self._visible_export_spectrum_states(prefer_total=True),
                co_states=self._visible_co_states(),
                integrated_records=self._visible_total_integral_records(),
            )
            layout_label = "matrix" if export_info['layout'] == 'matrix' else "long-format"
            processed_parts = []
            if export_info.get('oh_points', 0) > 0:
                processed_parts.append(f"OH {export_info['oh_points']} pts")
            if export_info.get('co_points', 0) > 0:
                processed_parts.append(f"CO {export_info['co_points']} pts")
            if export_info.get('integral_rows', 0) > 0:
                processed_parts.append(f"Integrals {export_info['integral_rows']} rows")
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

        first_fit = first_state.get('fit_result')
        last_fit  = last_state.get('fit_result')

        if first_fit is None or not getattr(first_fit, 'peaks', None):
            QMessageBox.warning(self, "Auto Fit", "첫 번째 스펙트럼을 먼저 피팅하세요.")
            return
        if last_fit is None or not getattr(last_fit, 'peaks', None):
            QMessageBox.warning(self, "Auto Fit", "마지막 스펙트럼을 먼저 피팅하세요.")
            return

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

            # Total에서 만든 corrected spectrum을 우선 사용하고, 없으면 raw.
            wn, ab, bl, ab_cor, _ = self._analysis_arrays_for_entry(
                entry, cfg['wn_min'], cfg['wn_max'])
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
            self._total_baseline_states = {}
            self._total_integral_regions = {}
            self._total_integral_results = {}
            self._co_states        = {}
            self._sio_states       = {}
            self._co_fit_records   = []
            self._co_stark_results = []
            self._co_drag_targets  = {}
            self._stark_results    = []
            self._sio_ref_area     = None
            self._total_shifts     = {}
            self._total_view_mode  = 'overlay'
            self._total_inactive_ranges = {}
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
            self.analysis_widget.update_integrated_areas([])
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
        entry_session_keys = {
            self.spectrum_list.get_session_key_for_entry(entry)
            for entry in entries
        }
        session_order = [
            key for key in self.spectrum_list.get_session_keys()
            if key in entry_session_keys
        ]
        workspace_sessions = [
            {
                'key': key,
                'label': self.spectrum_list.get_session_label_for_key(key),
            }
            for key in session_order
        ]
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

        total_integral_regions = {
            session_key: copy.deepcopy(regions)
            for session_key, regions in self._total_integral_regions.items()
            if session_key in entry_session_keys
        }
        total_integral_results = {}
        for session_key, by_region in self._total_integral_results.items():
            if session_key not in entry_session_keys or not isinstance(by_region, dict):
                continue
            filtered_regions = {}
            for region_name, rows in by_region.items():
                filtered_rows = [
                    copy.deepcopy(row)
                    for row in rows
                    if row.get('filename') in entry_names
                    and row.get('filepath') in entry_paths
                ]
                if filtered_rows:
                    filtered_regions[region_name] = filtered_rows
            if filtered_regions:
                total_integral_results[session_key] = filtered_regions

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
            'workspace_sessions': workspace_sessions,
            'session_order': session_order,
            'spectra': [
                {
                    'filepath':   e.filepath,
                    'name':       e.name,
                    'original_name': e.original_name or e.name,
                    'session_key': self.spectrum_list.get_session_key_for_entry(e),
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
            'total_baseline_states': {
                fp: copy.deepcopy(state)
                for fp, state in self._total_baseline_states.items()
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
            'total_inactive_ranges': {
                session_key: list(ranges)
                for session_key, ranges in self._total_inactive_ranges.items()
                if session_key in entry_session_keys
            },
            'total_integral_regions': total_integral_regions,
            'total_integral_results': total_integral_results,
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
            self._total_inactive_ranges.pop(session_key, None)
            self._total_integral_regions.pop(session_key, None)
            self._total_integral_results.pop(session_key, None)
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
        self._total_baseline_states = {}
        self._total_integral_regions = {}
        self._total_integral_results = {}
        self._fit_records      = []
        self._co_states        = {}
        self._co_fit_records   = []
        self._co_stark_results = []
        self._co_drag_targets  = {}
        self._sio_states       = {}
        self._sio_ref_area     = None
        self._total_shifts     = {}
        self._total_view_mode  = 'overlay'
        self._total_inactive_ranges = {}
        self._current_entry    = None
        self.plot_widget.reset_view()

        # 스펙트럼 복원
        spectra_data = data.get('spectra', [])
        self.spectrum_list.begin_bulk_update()
        try:
            for s in spectra_data:
                saved_session_key = self._saved_session_key_for_spectrum(s)
                source_session_label = (
                    ""
                    if saved_session_key == self.spectrum_list.LOOSE_FILES_KEY
                    else str(s.get('source_session_label') or saved_session_key)
                )
                entry = SpectrumEntry(
                    filepath=s['filepath'],
                    name=s['name'],
                    wavenumber=s['wavenumber'],
                    absorbance=s['absorbance'],
                    color=s['color'],
                    fit_done=s.get('fit_done', False),
                    original_name=s.get('original_name', s['name']),
                    source_session_label=source_session_label,
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
        self._total_baseline_states = self._sanitize_total_baseline_states(
            data.get('total_baseline_states', {}))
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
        self._total_inactive_ranges = {
            str(session_key): self._merge_total_ranges(ranges)
            for session_key, ranges in data.get('total_inactive_ranges', {}).items()
            if isinstance(ranges, (list, tuple))
        }
        self._total_integral_regions = self._normalize_total_integral_regions(
            data.get('total_integral_regions', {}))
        self._total_integral_results = copy.deepcopy(
            data.get('total_integral_results', {}))
        for session_key in self._total_integral_regions:
            self._recalculate_total_integrals_for_session(
                session_key,
                update_analysis=False,
            )
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
        self._refresh_total_integral_analysis(sync_controls=True)
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
        self.plot_widget.set_baseline_edit_mode(False)
        self.plot_widget.clear_baseline_points()
        self.plot_widget.cb_baseline.setVisible(mode == 'Total')

        if mode == 'Total':
            self.plot_widget.clear_endpoint_items()
            self.plot_widget.clear_baseline_points()
            self._apply_total_view(preserve_view=False)
            self._sync_baseline_edit_state_for_current_spectrum()
            self._refresh_total_integral_analysis(sync_controls=True)
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
                saved = self._spectrum_states.get(self._current_entry.filepath)
                if saved and saved.get('wn_crop') is not None and saved.get('ab_corrected') is not None:
                    self._wn_crop = np.asarray(saved['wn_crop'], dtype=float)
                    self._ab_crop = np.asarray(saved.get('ab_crop', saved['ab_corrected']), dtype=float)
                    self._baseline = np.asarray(
                        saved.get('baseline', np.zeros_like(self._wn_crop)), dtype=float)
                    self._ab_corrected = np.asarray(saved['ab_corrected'], dtype=float)
                    self._fit_result = saved.get('fit_result')
                    self._baseline_points = list(saved.get('baseline_points', []))
                    self.plot_widget.show_highlighted_region(
                        self._wn_crop, self._ab_corrected)
                else:
                    self._update_baseline()

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
            self._sync_co_b_mode_from_saved_state()
            self._apply_co_view(self._current_entry)
            if self._should_auto_analyze_co_on_mode_entry():
                self._analyze_all_co(auto_triggered=True)
            else:
                self._refresh_co_analysis_from_saved_state()

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

    def _co_peak_assignment(self, guess, default: str = 'Unassigned') -> str:
        value = getattr(guess, 'assignment', default)
        if value in ('CO_L', 'CO_B', 'OH bending', 'Other'):
            return value
        return default

    def _ensure_co_manual_state(self, entry: SpectrumEntry) -> dict:
        """Return the manual CO fit state, migrating legacy CO_B deconv data."""
        co_state = self._co_states.setdefault(entry.filepath, {})
        manual = co_state.setdefault('manual_fit', {})
        legacy = co_state.get('CO_B', {})

        if not manual.get('guesses') and legacy.get('guesses'):
            manual['guesses'] = copy.deepcopy(legacy.get('guesses') or [])
        if not manual.get('locks') and legacy.get('locks'):
            manual['locks'] = copy.deepcopy(legacy.get('locks') or [])
        if manual.get('fit_result') is None and legacy.get('raw_fit_result') is not None:
            manual['fit_result'] = legacy.get('raw_fit_result')
        if manual.get('wn') is None and legacy.get('wn_b') is not None:
            manual['wn'] = legacy.get('wn_b')
        if manual.get('ab') is None and legacy.get('ab_pos_b') is not None:
            manual['ab'] = legacy.get('ab_pos_b')
        if manual.get('baseline') is None and legacy.get('baseline_b') is not None:
            manual['baseline'] = legacy.get('baseline_b')

        guesses = manual.get('guesses') or []
        assignments = list(manual.get('assignments') or [])
        if len(assignments) != len(guesses):
            assignments = [self._co_peak_assignment(g) for g in guesses]
            manual['assignments'] = assignments
        for guess, assignment in zip(guesses, assignments):
            setattr(guess, 'assignment', assignment)
        return manual

    def _assigned_co_results_from_fit(self, fit_result, assignments: list,
                                      wn_fit: np.ndarray) -> dict:
        results = {label: None for label in CO_ASSIGNMENT_TARGETS}
        if fit_result is None or not getattr(fit_result, 'success', False):
            return results

        candidates = {label: [] for label in CO_ASSIGNMENT_TARGETS}
        for idx, peak in enumerate(getattr(fit_result, 'peaks', []) or []):
            assignment = assignments[idx] if idx < len(assignments) else 'Unassigned'
            if assignment not in candidates:
                continue
            curve = None
            if getattr(fit_result, 'individual_curves', None) and idx < len(fit_result.individual_curves):
                curve = fit_result.individual_curves[idx]
            if curve is not None and len(curve) == len(wn_fit):
                area = abs(float(_trapezoid(curve, wn_fit)))
            else:
                area = float(getattr(peak, 'area', 0.0))
            candidates[assignment].append((area, float(peak.center)))

        for assignment, peaks in candidates.items():
            if peaks:
                area, center = max(peaks, key=lambda item: item[0])
                results[assignment] = _make_co_result(center, area)
        return results

    def _refresh_co_manual_results_from_fit(self, entry: SpectrumEntry | None = None):
        entry = entry or self._current_entry
        if entry is None:
            return
        co_state = self._co_states.setdefault(entry.filepath, {})
        manual = self._ensure_co_manual_state(entry)
        fit_result = manual.get('fit_result')
        wn_fit = manual.get('wn')
        if fit_result is None or wn_fit is None:
            assigned = {label: None for label in CO_ASSIGNMENT_TARGETS}
        else:
            assigned = self._assigned_co_results_from_fit(
                fit_result,
                list(manual.get('assignments') or []),
                np.asarray(wn_fit, dtype=float),
            )
        missing_assignment = any(assigned[label] is None for label in CO_ASSIGNMENT_TARGETS)
        for sub in CO_ASSIGNMENT_TARGETS:
            sub_state = co_state.setdefault(sub, {})
            sub_state['fit_result'] = assigned.get(sub)
            sub_state['status'] = 'review' if missing_assignment else 'ok'
            sub_state['analysis_mode'] = 'manual_fit'
        self._update_co_fit_record_for_entry(entry)

    def _co_b_state_is_deconv(self, co_b_state: dict) -> bool:
        if not isinstance(co_b_state, dict):
            return False
        return (
            co_b_state.get('analysis_mode') == 'deconv'
            or co_b_state.get('raw_fit_result') is not None
            or bool(co_b_state.get('guesses'))
        )

    def _co_state_has_fit(self, co_state: dict) -> bool:
        if not isinstance(co_state, dict):
            return False
        return any(
            co_state.get(sub, {}).get('fit_result') is not None
            for sub in ('CO_L', 'CO_B')
        )

    def _co_fit_records_for_entries(self, entries: list[SpectrumEntry]) -> list:
        names = {entry.name for entry in entries}
        return [
            record for record in self._co_fit_records
            if record.get('filename') in names
        ]

    def _sync_co_b_mode_from_saved_state(self):
        entries = self.spectrum_list.get_visible_entries()
        if not entries and self._current_entry is not None:
            entries = [self._current_entry]

        if self._current_entry is not None:
            current_state = self._co_states.get(self._current_entry.filepath, {})
            current_co_b = current_state.get('CO_B', {})
            if self._co_b_state_is_deconv(current_co_b):
                self.right_panel.set_co_b_fit_mode('auto')
                return
            if self._co_state_has_fit(current_state):
                self.right_panel.set_co_b_fit_mode('simple_only')
                return

        has_deconv = False
        has_simple = False
        for entry in entries:
            co_state = self._co_states.get(entry.filepath, {})
            co_b_state = co_state.get('CO_B', {})
            if self._co_b_state_is_deconv(co_b_state):
                has_deconv = True
            elif self._co_state_has_fit(co_state):
                has_simple = True

        if has_deconv:
            self.right_panel.set_co_b_fit_mode('auto')
        elif has_simple:
            self.right_panel.set_co_b_fit_mode('simple_only')
        else:
            self.right_panel.set_co_b_fit_mode('auto')

    def _should_auto_analyze_co_on_mode_entry(self) -> bool:
        entries = self.spectrum_list.get_visible_entries()
        if not entries:
            return False
        if self._co_fit_records_for_entries(entries):
            return False
        return not any(
            self._co_state_has_fit(self._co_states.get(entry.filepath, {}))
            for entry in entries
        )

    def _refresh_co_analysis_from_saved_state(self):
        if self._co_fit_records:
            self.analysis_widget.update_co_plots(
                self._visible_co_fit_records(),
                self._visible_potentials(),
            )
            self._refresh_co_stark_results(self._visible_potentials())
        else:
            self.right_panel.update_co_stark_results([])
        self.status_label.setText("CO saved analysis restored")

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
        self._refresh_co_manual_results_from_fit(self._current_entry)
        self._refresh_co_outputs_after_manual_edit()

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

    def _co_subregion_data(self, entry: SpectrumEntry, ep0: float, ep1: float):
        ep0, ep1 = sorted((ep0, ep1))
        wn_sub, ab_sub, _, corrected, _ = self._analysis_arrays_for_entry(
            entry, ep0, ep1)
        if len(wn_sub) == 0:
            return wn_sub, ab_sub, np.zeros_like(ab_sub), np.zeros_like(ab_sub)
        return wn_sub, ab_sub, np.zeros_like(corrected), np.maximum(corrected, 0)

    def _co_display_region_for_entry(self, entry: SpectrumEntry | None):
        if entry is None:
            return CO_DISPLAY_REGION
        wn, _ = self._total_display_data_for_entry(entry)
        wn = np.asarray(wn, dtype=float)
        finite = wn[np.isfinite(wn)]
        if len(finite) == 0:
            return CO_DISPLAY_REGION
        return float(np.min(finite)), float(np.max(finite))

    def _co_b_fit_data(self, entry: SpectrumEntry):
        ep0, ep1 = self._co_display_region_for_entry(entry)
        return self._co_subregion_data(entry, ep0, ep1)

    def _fit_co_entry(self, entry: SpectrumEntry, prefer_plot_eps: bool = False,
                      refresh_plot: bool = False) -> dict:
        co_state = self._co_states.setdefault(entry.filepath, {})
        manual = self._ensure_co_manual_state(entry)
        results = {label: None for label in CO_ASSIGNMENT_TARGETS}
        needs_review = True
        wn_b, _, bl_b, ab_pos_b = self._co_b_fit_data(entry)
        guesses = list(manual.get('guesses') or [])

        if len(wn_b) >= 5:
            if guesses:
                shape = self.right_panel.get_peak_shape_co()
                assignments = [self._co_peak_assignment(g) for g in guesses]
                locks = list(manual.get('locks') or [])
                if len(locks) != len(guesses):
                    manual_locks = set(
                        int(i) for i in manual.get('manual_center_locks', [])
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
                    fitted_guesses = []
                    for i, p in enumerate(fit_result.peaks):
                        amplitude = (
                            float(np.max(fit_result.individual_curves[i]))
                            if i < len(fit_result.individual_curves)
                            else float(getattr(p, 'amplitude', 0.0))
                        )
                        guess = PeakGuess(
                            center=p.center,
                            amplitude=amplitude,
                            sigma=p.sigma,
                            index=i,
                            shape=getattr(p, 'shape', shape),
                        )
                        guess.assignment = assignments[i] if i < len(assignments) else 'Unassigned'
                        fitted_guesses.append(guess)

                    manual['guesses'] = fitted_guesses
                    manual['locks'] = locks
                    manual['assignments'] = [
                        self._co_peak_assignment(g) for g in fitted_guesses
                    ]
                    manual['fit_result'] = fit_result
                    manual['wn'] = wn_b
                    manual['ab'] = ab_pos_b
                    manual['baseline'] = bl_b
                    manual['analysis_mode'] = 'manual_fit'
                    results = self._assigned_co_results_from_fit(
                        fit_result,
                        manual['assignments'],
                        wn_b,
                    )
                    needs_review = any(
                        results[label] is None for label in CO_ASSIGNMENT_TARGETS
                    )

                    if refresh_plot:
                        self.plot_widget.show_fit_result(wn_b, ab_pos_b, fit_result,
                                                         baseline=None)
                        self.right_panel.set_co_guesses(fitted_guesses, locks=locks)
                else:
                    manual.pop('fit_result', None)
                    manual.pop('raw_fit_result', None)
            else:
                manual.pop('fit_result', None)
                manual.pop('raw_fit_result', None)

        for sub in CO_ASSIGNMENT_TARGETS:
            sub_state = co_state.setdefault(sub, {})
            sub_state['ep0'], sub_state['ep1'] = self._co_display_region_for_entry(entry)
            sub_state['fit_result'] = results.get(sub)
            sub_state['status'] = 'review' if needs_review else 'ok'
            sub_state['analysis_mode'] = 'manual_fit'

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
            'used_deconv': bool(guesses),
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

    # ── CO Auto Guess ─────────────────────────────────────────

    def _auto_detect_co_b(self):
        if self._current_entry is None:
            QMessageBox.warning(self, "No spectrum", "먼저 스펙트럼을 선택하세요.")
            return

        entry = self._current_entry
        wn_b, _, bl_b, ab_pos_b = self._co_b_fit_data(entry)
        if len(wn_b) < 5:
            return

        manual = self._ensure_co_manual_state(entry)
        guesses = manual.get('guesses')
        if not guesses:
            guesses = find_peaks_second_derivative(wn_b, ab_pos_b, n_peaks=2)
        if not guesses:
            self.status_label.setText("CO: 피크를 자동 감지할 수 없습니다.")
            return
        for guess in guesses:
            if not hasattr(guess, 'assignment'):
                guess.assignment = 'Unassigned'

        manual['guesses'] = guesses
        manual['assignments'] = [self._co_peak_assignment(g) for g in guesses]
        manual['locks'] = [
            {'center': False, 'amplitude': False, 'sigma': False}
            for _ in guesses
        ]
        manual.pop('manual_center_locks', None)
        manual.pop('fit_result', None)
        manual.pop('raw_fit_result', None)
        manual['wn'] = wn_b
        manual['ab'] = ab_pos_b
        manual['baseline'] = bl_b
        self._co_drag_targets = {
            i: {'type': 'co_peak_guess', 'peak_idx': i}
            for i in range(len(guesses))
        }
        self.right_panel.set_co_guesses(guesses, locks=manual['locks'])
        self.plot_widget.show_peak_guesses(
            wn_b,
            guesses,
            baseline=None,
        )
        self.status_label.setText(
            f"CO: {len(guesses)}개 피크 감지  —  Assign 지정 후 Fit Current")

    def _restore_co_b_fit_viz(self, entry: SpectrumEntry, co_state: dict):
        """스펙트럼 전환 / 모드 전환 시 CO 피팅 시각화 복원"""
        manual = self._ensure_co_manual_state(entry)
        raw_fit = manual.get('fit_result')
        if raw_fit is None or not raw_fit.success:
            return

        fit_len = 0
        if getattr(raw_fit, 'individual_curves', None):
            fit_len = len(raw_fit.individual_curves[0])

        stored_wn = manual.get('wn')
        stored_ab_pos = manual.get('ab')
        if fit_len and stored_wn is not None and len(stored_wn) == fit_len:
            wn_b = np.asarray(stored_wn, dtype=float)
            if stored_ab_pos is not None and len(stored_ab_pos) == fit_len:
                ab_pos_b = np.asarray(stored_ab_pos, dtype=float)
            else:
                ab_pos_b = np.zeros_like(wn_b)
            self.plot_widget.show_fit_result(
                wn_b,
                ab_pos_b,
                raw_fit,
                baseline=None,
            )
            self._co_drag_targets = {
                i: {'type': 'co_peak_fit', 'peak_idx': i}
                for i in range(len(raw_fit.peaks))
            }
            return

        wn_b, _, _, ab_pos_b = self._co_b_fit_data(entry)
        if len(wn_b) < 5:
            return
        self.plot_widget.show_fit_result(
            wn_b,
            ab_pos_b,
            raw_fit,
            baseline=None,
        )
        self._co_drag_targets = {
            i: {'type': 'co_peak_fit', 'peak_idx': i}
            for i in range(len(raw_fit.peaks))
        }

    # ── Si-O Area ─────────────────────────────────────────────

    def _calc_sio_area(self):
        if self._current_entry is None:
            QMessageBox.warning(self, "No spectrum", "먼저 스펙트럼을 선택하세요.")
            return

        entry = self._current_entry
        cfg = self.right_panel.get_config()
        if self.plot_widget.get_sio_endpoints() != (1100.0, 1300.0) or self.plot_widget._ep_lines:
            ep0, ep1 = sorted(float(v) for v in self.plot_widget.get_sio_endpoints())
        else:
            ep0, ep1 = sorted((float(cfg['wn_min']), float(cfg['wn_max'])))
        wn, _, _, corrected, source = self._analysis_arrays_for_entry(entry, ep0, ep1)
        if len(wn) < 3:
            QMessageBox.warning(self, "Si-O", "선택 영역이 너무 좁습니다.")
            return

        area = abs(float(_trapezoid(corrected, wn)))

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
                session_state.setdefault('ep0', ep0)
                session_state.setdefault('ep1', ep1)
            session_state['area'] = area
            session_state['region'] = (ep0, ep1)
            session_state['source'] = source
            self._sio_states[session_entry.filepath] = session_state
        self._sio_ref_area = area

        self.right_panel.update_sio_area(area)
        self.plot_widget.show_analysis_region([(ep0, ep1)])
        self.plot_widget.update_sio_endpoints(ep0, ep1)

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
            f"Si-O Area: {area:.4f}  |  {ep0:.0f}–{ep1:.0f} cm⁻¹  |  source={source}")

    # ── Endpoint 드래그 핸들러 ────────────────────────────────

    def _on_sio_endpoint_moved(self, side: int, wn: float):
        if self._current_entry is None:
            return
        sio_state = self._sio_states.setdefault(self._current_entry.filepath, {})
        if side == 0:
            sio_state['ep0'] = wn
        else:
            sio_state['ep1'] = wn
        ep0, ep1 = sorted(float(v) for v in self.plot_widget.get_sio_endpoints())
        sio_state['ep0'] = ep0
        sio_state['ep1'] = ep1
        sio_state['region'] = (ep0, ep1)
        self.right_panel.set_region_values(ep0, ep1)
        self.plot_widget.show_analysis_region([(ep0, ep1)])

    def closeEvent(self, event):
        if self._analysis_window is not None:
            self._analysis_window.allow_close()
            self._analysis_window.close()
        super().closeEvent(event)
