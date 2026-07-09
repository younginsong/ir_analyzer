"""
plot_widget.py - 인터랙티브 플롯 (fityk 스타일 deconvolution)

피크 조작:
  - 수직선 드래그      : 피크 center 이동
  - 핸들(흰 사각형) 드래그 : 피크 amplitude 조절
  - 캔버스 우클릭+드래그  : 새 피크 생성 (좌우=폭, 상하=높이)
  - 수직선 우클릭       : FWHM 직접 입력 다이얼로그
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QCheckBox, QLabel, QMenu)
from PyQt5.QtCore import pyqtSignal, Qt, QEvent
from PyQt5.QtGui import QColor
from core.fitter import build_model
from lmfit import Parameters

COLORS = {
    'raw':       '#9399b2',
    'baseline':  '#6c7086',
    'corrected': '#cdd6f4',
    'fitted':    '#f38ba8',
    'residual':  '#a6e3a1',
    'peaks':     ['#89b4fa', '#fab387', '#cba6f7', '#94e2d5',
                  '#f9e2af', '#a6e3a1', '#89dceb', '#f38ba8'],
}

pg.setConfigOptions(antialias=True, background='#1e1e2e', foreground='#cdd6f4')

MIN_SIGMA = 1.0   # 피크 최소 sigma (cm⁻¹)


class PeakLine(pg.InfiniteLine):
    """Draggable peak line. 우클릭 → FWHM 다이얼로그."""

    def __init__(self, *args, peak_idx: int = 0, on_right_click=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._peak_idx = peak_idx
        self._on_right_click = on_right_click

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.RightButton:
            ev.accept()
            if self._on_right_click is not None:
                self._on_right_click(self._peak_idx)
            return
        super().mouseClickEvent(ev)


class PlotWidget(QWidget):
    baseline_point_added   = pyqtSignal(float, float)
    baseline_point_removed = pyqtSignal(int)
    peak_center_dragged    = pyqtSignal(int, float)      # (idx, new_center)
    peak_amplitude_dragged = pyqtSignal(int, float)      # (idx, new_amplitude_height)
    peak_created           = pyqtSignal(float, float, float)  # (center, amp_height, sigma)
    peak_sigma_changed     = pyqtSignal(int, float)      # (idx, new_sigma)
    co_endpoint_moved      = pyqtSignal(str, int, float)
    sio_endpoint_moved     = pyqtSignal(int, float)
    total_spectrum_selected = pyqtSignal(str)          # spectrum name
    total_shift_changed    = pyqtSignal(str, float)      # (spectrum name, y shift)
    total_shift_mode_changed = pyqtSignal(bool)
    total_probe_mode_changed = pyqtSignal(bool)
    total_region_toggled   = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = {}
        self._overlay_items = {}
        self._fill_items = []
        self._baseline_mode = False
        self._peak_add_mode = False
        self._bl_scatter_items = []
        self._current_sigmas = {}
        self._fwhm_regions = {}
        self._has_spectrum = False
        self._ep_lines = {}
        self._ep_ref_wn = None
        self._ep_ref_ab = None
        self._region_items = []
        self._current_guesses = {}   # {idx: PeakGuess}
        self._peak_locks = {}        # {idx: {'center': bool, 'amplitude': bool, 'sigma': bool}}
        self._wn_crop = None
        self._guess_baseline = None
        self._total_mode = False
        self._total_specs = []
        self._total_items = {}
        self._total_shift_enabled = False
        self._total_probe_enabled = False
        self._total_drag = None
        self._total_region_drag = None
        self._total_region_preview = None
        self._total_inactive_region_items = []
        self._coord_text = None
        self._coord_vline = None
        self._coord_hline = None

        # 우클릭 드래그 상태
        self._rclick_drag = None    # {'center': float, 'preview_idx': int|None}

        # 핸들 드래그 상태
        self._handle_drag = None    # {'idx': int}

        self._build_ui()

        # 뷰포트 이벤트 필터 등록 (우클릭 드래그 + 핸들 드래그)
        self.pw.setMouseTracking(True)
        self.pw.viewport().setMouseTracking(True)
        self.pw.viewport().installEventFilter(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_coord_label()

    def _position_coord_label(self):
        if not hasattr(self, '_coord_label') or self._coord_label is None:
            return
        self._coord_label.adjustSize()
        margin = 12
        width = max(self._coord_label.width(), 176)
        height = self._coord_label.height()
        self._coord_label.setGeometry(
            max(margin, self.pw.width() - width - margin),
            margin,
            width,
            height,
        )

    # ── UI 구성 ───────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 6, 8, 6)
        toolbar.setSpacing(6)

        self.cb_raw = QCheckBox("Raw")
        self.cb_raw.setChecked(True)
        self.cb_baseline = QCheckBox("Baseline")
        self.cb_baseline.setChecked(True)
        self.cb_residual = QCheckBox("Residual")
        self.cb_residual.setChecked(False)

        sep = QLabel("|")
        sep.setStyleSheet("color: #45475a;")

        self.btn_add_peak = QPushButton("+ Add Peak")
        self.btn_add_peak.setCheckable(True)
        self.btn_add_peak.setToolTip("클릭 활성화 후 플롯 클릭 → 피크 추가\n또는 캔버스에서 우클릭+드래그로 피크 생성")
        self.btn_add_peak.setObjectName("btn_flat")

        for w in [self.cb_raw, self.cb_baseline, self.cb_residual, sep, self.btn_add_peak]:
            toolbar.addWidget(w)
        toolbar.addStretch()

        hint = QLabel("핸들 드래그: 높이  |  수직선 드래그: center  |  우클릭+드래그: 피크 추가")
        hint.setStyleSheet("color: #45475a; font-size: 11px;")
        toolbar.addWidget(hint)
        layout.addLayout(toolbar)

        self.pw = pg.PlotWidget()
        self.pw.setLabel('bottom', 'Wavenumber (cm⁻¹)',
                         **{'color': '#6c7086', 'font-size': '12px'})
        self.pw.setLabel('left', 'Absorbance',
                         **{'color': '#6c7086', 'font-size': '12px'})
        self.pw.showGrid(x=True, y=True, alpha=0.15)
        self.pw.getPlotItem().invertX(True)
        self.pw.getAxis('bottom').setTextPen(pg.mkPen('#6c7086'))
        self.pw.getAxis('left').setTextPen(pg.mkPen('#6c7086'))
        self.pw.getAxis('bottom').setPen(pg.mkPen('#313244'))
        self.pw.getAxis('left').setPen(pg.mkPen('#313244'))
        self.pw.getPlotItem().setMenuEnabled(False)
        layout.addWidget(self.pw)

        self._coord_label = QLabel(self.pw)
        self._coord_label.setObjectName("coord_probe_label")
        self._coord_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._coord_label.setStyleSheet(
            "QLabel#coord_probe_label {"
            "color: #cdd6f4;"
            "background: rgba(30, 30, 46, 190);"
            "border: 1px solid #45475a;"
            "border-radius: 6px;"
            "padding: 7px 11px;"
            "font-size: 16px;"
            "font-weight: 700;"
            "}"
        )
        self._coord_label.setVisible(False)

        self.legend = self.pw.addLegend(
            offset=(10, 10),
            labelTextColor='#a6adc8',
            brush=pg.mkBrush('#1e1e2e'),
            pen=pg.mkPen('#313244')
        )

        self.cb_raw.toggled.connect(lambda v: self._set_visible('raw', v))
        self.cb_baseline.toggled.connect(lambda v: self._set_visible('baseline', v))
        self.cb_residual.toggled.connect(lambda v: self._set_visible('residual', v))
        self.btn_add_peak.toggled.connect(self._on_peak_add_mode)
        self.pw.scene().sigMouseClicked.connect(self._on_mouse_click)

    # ── 데이터 설정 ───────────────────────────────────────────

    def set_raw_spectrum(self, wn, ab):
        self._clear_all()
        self._total_mode = False
        self._ep_ref_wn = wn
        self._ep_ref_ab = ab
        item = self.pw.plot(wn, ab,
                            pen=pg.mkPen(COLORS['raw'], width=1.2),
                            name='Raw')
        item.setVisible(self.cb_raw.isChecked())
        self._items['raw'] = item
        if not self._has_spectrum:
            self.pw.autoRange()
            self._has_spectrum = True

    def set_overlay_spectra(self, spectra: list, active_filepath: str | None = None):
        self.clear_overlay_spectra()
        for i, spec in enumerate(spectra):
            if spec['filepath'] == active_filepath:
                continue
            color = QColor(spec['color'])
            color.setAlpha(170)
            item = self.pw.plot(
                spec['wn'],
                spec['ab'],
                pen=pg.mkPen(color, width=1.1),
            )
            item.setZValue(2)
            kind = spec.get('kind', 'raw')
            item.setVisible(True if kind == 'corrected' else self.cb_raw.isChecked())
            self._overlay_items[f'overlay_{i}'] = {
                'item': item,
                'kind': kind,
            }

    def clear_overlay_spectra(self):
        for key in list(self._overlay_items.keys()):
            try:
                self.pw.removeItem(self._overlay_items.pop(key)['item'])
            except Exception:
                pass

    def set_corrected_spectrum(self, wn, ab_corr, baseline):
        if 'baseline' in self._items:
            self._items['baseline'].setData(wn, baseline)
        else:
            item = self.pw.plot(wn, baseline,
                                pen=pg.mkPen(COLORS['baseline'], width=1,
                                             style=Qt.DashLine),
                                name='Baseline')
            item.setVisible(self.cb_baseline.isChecked())
            self._items['baseline'] = item

        if 'corrected' in self._items:
            self._items['corrected'].setData(wn, ab_corr)
        else:
            item = self.pw.plot(wn, ab_corr,
                                pen=pg.mkPen(COLORS['corrected'], width=1.8),
                                name='Corrected')
            self._items['corrected'] = item

    def set_baseline_curve(self, wn, baseline):
        if 'baseline' in self._items:
            self._items['baseline'].setData(wn, baseline)
        else:
            item = self.pw.plot(
                wn,
                baseline,
                pen=pg.mkPen(COLORS['baseline'], width=1, style=Qt.DashLine),
                name='Baseline',
            )
            item.setVisible(self.cb_baseline.isChecked())
            item.setZValue(7)
            self._items['baseline'] = item

    def set_total_edit_raw_curve(self, wn, raw):
        if 'raw' in self._items:
            self._items['raw'].setData(wn, raw)
        else:
            color = QColor(COLORS['raw'])
            color.setAlpha(230)
            item = self.pw.plot(
                wn,
                raw,
                pen=pg.mkPen(color, width=1.6),
                name='Raw',
            )
            item.setVisible(self.cb_raw.isChecked())
            item.setZValue(5)
            self._items['raw'] = item

    def show_highlighted_region(self, wn, ab):
        """분석 구간만 흰색으로 강조 표시한다."""
        self._wn_crop = np.asarray(wn, dtype=float)
        if 'corrected' in self._items:
            self._items['corrected'].setData(wn, ab)
        else:
            item = self.pw.plot(
                wn, ab,
                pen=pg.mkPen(COLORS['corrected'], width=1.8),
                name='Corrected')
            self._items['corrected'] = item

    def clear_baseline_curve(self):
        for key in ('baseline', 'corrected'):
            if key in self._items:
                self.pw.removeItem(self._items.pop(key))

    # ── Total view ────────────────────────────────────────────

    def show_total_spectra(self, specs: list, active_name: str | None = None):
        """
        specs: [{name, filepath, wn, ab, base_shift, shift, color, potential}, ...]
        """
        self._clear_all()
        self._total_mode = True
        self._total_specs = []
        self._total_items = {}
        self._total_drag = None
        self._set_total_probe_items_visible(self._total_probe_enabled)

        for i, spec in enumerate(specs):
            wn = np.asarray(spec['wn'], dtype=float)
            ab = np.asarray(spec['ab'], dtype=float)
            base_shift = float(spec.get('base_shift', 0.0))
            shift = float(spec.get('shift', 0.0))
            color = spec.get('color', COLORS['peaks'][i % len(COLORS['peaks'])])
            width = 2.2 if spec.get('name') == active_name else 1.25
            alpha = 255 if spec.get('name') == active_name else 185
            qcolor = QColor(color)
            qcolor.setAlpha(alpha)
            y = ab + base_shift
            potential = spec.get('potential')
            pot_txt = "— V" if potential is None else f"{potential:+.2f} V"
            item = self.pw.plot(
                wn, y,
                pen=pg.mkPen(qcolor, width=width),
            )
            item.setPos(0, shift)
            if hasattr(item, 'setDownsampling'):
                item.setDownsampling(auto=True, method='peak')
            if hasattr(item, 'setClipToView'):
                item.setClipToView(True)
            item.setZValue(6 if spec.get('name') == active_name else 3)
            stored = dict(spec)
            stored.update({
                'wn': wn,
                'ab': ab,
                'y_base': y,
                'base_shift': base_shift,
                'shift': shift,
                'color': color,
                'label': f"{pot_txt} | {spec.get('name', f'Spectrum {i+1}')}",
                'item': item,
            })
            self._total_specs.append(stored)
            self._total_items[stored['name']] = item

        self._has_spectrum = bool(specs)
        if specs:
            self.pw.autoRange()

    def set_total_active_spectrum(self, active_name: str | None) -> bool:
        if not self._total_mode or not self._total_specs:
            return False
        for i, spec in enumerate(self._total_specs):
            item = spec.get('item')
            if item is None:
                continue
            is_active = spec.get('name') == active_name
            qcolor = QColor(spec.get('color', COLORS['peaks'][i % len(COLORS['peaks'])]))
            qcolor.setAlpha(255 if is_active else 185)
            item.setPen(pg.mkPen(qcolor, width=2.2 if is_active else 1.25))
            item.setZValue(6 if is_active else 3)
        return True

    def set_total_inactive_ranges(self, ranges: list[tuple[float, float]]):
        for item in self._total_inactive_region_items:
            try:
                self.pw.removeItem(item)
            except Exception:
                pass
        self._total_inactive_region_items.clear()

        if not self._total_mode:
            return

        for start, end in ranges:
            lo, hi = sorted((float(start), float(end)))
            region = pg.LinearRegionItem(
                values=(lo, hi),
                movable=False,
                brush=pg.mkBrush(243, 139, 168, 38),
                pen=pg.mkPen('#f38ba8', width=1, style=Qt.DashLine),
            )
            region.setZValue(1)
            self.pw.addItem(region)
            self._total_inactive_region_items.append(region)

    def _update_total_region_preview(self, x_value: float):
        if self._total_region_drag is None:
            return
        start_x = float(self._total_region_drag['start_x'])
        if self._total_region_preview is None:
            self._total_region_preview = pg.LinearRegionItem(
                values=(start_x, float(x_value)),
                movable=False,
                brush=pg.mkBrush(249, 226, 175, 45),
                pen=pg.mkPen('#f9e2af', width=1),
            )
            self._total_region_preview.setZValue(30)
            self.pw.addItem(self._total_region_preview)
        else:
            self._total_region_preview.setRegion((start_x, float(x_value)))

    def _clear_total_region_preview(self):
        if self._total_region_preview is not None:
            try:
                self.pw.removeItem(self._total_region_preview)
            except Exception:
                pass
            self._total_region_preview = None

    def set_total_shift_mode(self, enabled: bool):
        self._total_shift_enabled = bool(enabled)
        if enabled:
            self._peak_add_mode = False
            self._baseline_mode = False
            self.btn_add_peak.setChecked(False)
        self.pw.setCursor(Qt.OpenHandCursor if enabled else (
            Qt.CrossCursor if self._total_probe_enabled else Qt.ArrowCursor))

    def set_total_probe_mode(self, enabled: bool):
        self._total_probe_enabled = bool(enabled)
        self._set_total_probe_items_visible(enabled)
        self.pw.setCursor(Qt.CrossCursor if enabled else (
            Qt.OpenHandCursor if self._total_shift_enabled else Qt.ArrowCursor))

    def _set_total_probe_items_visible(self, visible: bool):
        if not self._total_mode:
            if hasattr(self, '_coord_label'):
                self._coord_label.setVisible(False)
            return
        if hasattr(self, '_coord_label'):
            if visible and not self._coord_label.text():
                self._coord_label.setText("Move cursor over plot")
            self._coord_label.setVisible(bool(visible))
            if visible:
                self._coord_label.raise_()
                self._position_coord_label()
        if self._coord_text is None:
            self._coord_text = pg.TextItem("", color="#cdd6f4", anchor=(1, 0))
            self._coord_text.setZValue(50)
            self.pw.addItem(self._coord_text)
        if self._coord_vline is None:
            pen = pg.mkPen("#cdd6f4", width=1, style=Qt.DashLine)
            self._coord_vline = pg.InfiniteLine(angle=90, movable=False, pen=pen)
            self._coord_hline = pg.InfiniteLine(angle=0, movable=False, pen=pen)
            self._coord_vline.setZValue(49)
            self._coord_hline.setZValue(49)
            self.pw.addItem(self._coord_vline)
            self.pw.addItem(self._coord_hline)
        for item in (self._coord_text, self._coord_vline, self._coord_hline):
            if item is not None:
                item.setVisible(bool(visible))

    def _update_total_probe(self, pt):
        if not (self._total_mode and self._total_probe_enabled):
            return
        self._set_total_probe_items_visible(True)
        x = float(pt.x())
        y = float(pt.y())
        self._coord_vline.setValue(x)
        self._coord_hline.setValue(y)
        label_text = f"({x:.1f}, {y:.5g})"
        if hasattr(self, '_coord_label'):
            self._coord_label.setText(label_text)
            self._coord_label.setVisible(True)
            self._coord_label.raise_()
            self._position_coord_label()

        x_range, y_range = self.pw.getViewBox().viewRange()
        self._coord_text.setText(label_text)
        self._coord_text.setPos(max(x_range), max(y_range))

    def _display_y_for_total_spec(self, spec):
        y_base = spec.get('y_base')
        if y_base is None:
            y_base = spec['ab'] + float(spec.get('base_shift', 0.0))
        return y_base + float(spec.get('shift', 0.0))

    def _interp_total_y(self, spec, x_value: float):
        x = spec['wn']
        y = spec.get('y_base')
        if y is None:
            y = spec['ab'] + float(spec.get('base_shift', 0.0))
        if len(x) == 0:
            return None
        if x[0] > x[-1]:
            x = x[::-1]
            y = y[::-1]
        return float(np.interp(x_value, x, y)) + float(spec.get('shift', 0.0))

    def _hit_test_total_spectrum(self, pt):
        if not self._total_specs:
            return None
        vb = self.pw.getViewBox()
        x_range, y_range = vb.viewRange()
        y_span = max(y_range[1] - y_range[0], 1e-9)
        px_per_y = max(vb.rect().height() / y_span, 1e-9)
        hit_y = 16.0 / px_per_y

        best = None
        best_dist = float('inf')
        for spec in self._total_specs:
            wn = spec['wn']
            x_min, x_max = min(wn[0], wn[-1]), max(wn[0], wn[-1])
            if not (x_min <= pt.x() <= x_max):
                continue
            y_at_x = self._interp_total_y(spec, pt.x())
            if y_at_x is None:
                continue
            dist = abs(pt.y() - y_at_x)
            if dist < best_dist:
                best = spec
                best_dist = dist
        return best if best is not None and best_dist <= hit_y else None

    def _update_total_shift(self, spec, shift: float):
        spec['shift'] = float(shift)
        item = spec.get('item')
        if item is not None:
            item.setPos(0, float(shift))
        self.total_shift_changed.emit(spec['name'], float(shift))

    def _show_total_context_menu(self, global_pos):
        menu = QMenu(self)
        shift_action = menu.addAction("Shift Mode")
        shift_action.setCheckable(True)
        shift_action.setChecked(self._total_shift_enabled)
        probe_action = menu.addAction("Coordinate Probe")
        probe_action.setCheckable(True)
        probe_action.setChecked(self._total_probe_enabled)
        menu.addSeparator()
        view_all_action = menu.addAction("View All")
        chosen = menu.exec_(global_pos)
        if chosen is shift_action:
            enabled = not self._total_shift_enabled
            self.set_total_shift_mode(enabled)
            self.total_shift_mode_changed.emit(enabled)
        elif chosen is probe_action:
            enabled = not self._total_probe_enabled
            self.set_total_probe_mode(enabled)
            self.total_probe_mode_changed.emit(enabled)
        elif chosen is view_all_action:
            self.do_auto_range()

    # ── 베이스라인 편집 모드 ──────────────────────────────────

    def set_baseline_edit_mode(self, enabled: bool):
        self._baseline_mode = enabled
        self._peak_add_mode = False
        self.btn_add_peak.setChecked(False)
        cursor = Qt.CrossCursor if enabled else Qt.ArrowCursor
        self.pw.setCursor(cursor)

    def clear_baseline_points(self):
        for item in self._bl_scatter_items:
            self.pw.removeItem(item)
        self._bl_scatter_items.clear()

    def undo_last_baseline_point(self):
        if self._bl_scatter_items:
            item = self._bl_scatter_items.pop()
            self.pw.removeItem(item)

    def restore_baseline_points(self, points: list):
        """저장된 베이스라인 포인트 마커를 플롯에 복원."""
        self.clear_baseline_points()
        visible = self._baseline_mode or self.cb_baseline.isChecked()
        for wn, ab in points:
            scatter = pg.ScatterPlotItem(
                x=[wn], y=[ab],
                symbol='o', size=10,
                brush=pg.mkBrush(COLORS['baseline']),
                pen=pg.mkPen('#f8f8ff', width=1.2)
            )
            scatter.setZValue(80)
            scatter.setVisible(visible)
            self.pw.addItem(scatter)
            self._bl_scatter_items.append(scatter)

    # ── 피크 guess 라인 (드래그 가능) ────────────────────────

    def show_peak_guesses(self, wn, guesses, baseline=None):
        self._clear_guess_lines()
        self._wn_crop = wn
        self._current_guesses = {}
        for i, g in enumerate(guesses):
            g.index = i
            self._current_guesses[i] = g
        self._guess_baseline = (
            np.asarray(baseline, dtype=float)
            if baseline is not None else None
        )
        for i, g in enumerate(guesses):
            self._add_interactive_peak(
                i, g.center,
                getattr(g, 'amplitude', 0.1),
                getattr(g, 'sigma', 30.0),
                getattr(g, 'shape', 'gaussian')
            )

    def show_peak_center_markers(self, markers: list[dict]):
        """Show draggable center-only markers, used by simple CO analysis."""
        from core.peak_finder import PeakGuess

        self._clear_guess_lines()
        self._current_guesses = {}

        for i, marker in enumerate(markers):
            center = float(marker['center'])
            label = marker.get('label', f'P{i + 1}')
            color = marker.get('color', COLORS['peaks'][i % len(COLORS['peaks'])])
            self._current_guesses[i] = PeakGuess(
                center=center, amplitude=0.0, sigma=1.0, index=i
            )

            line = PeakLine(
                pos=center, angle=90, movable=True,
                pen=pg.mkPen(color, width=2, style=Qt.DotLine),
                hoverPen=pg.mkPen(color, width=3, style=Qt.DotLine),
                label=label,
                labelOpts={'position': 0.90, 'color': color,
                           'fill': pg.mkBrush(30, 30, 46, 200)},
                peak_idx=i,
                on_right_click=None,
            )
            line.setZValue(15)

            def make_preview_handler(idx):
                def on_drag(l):
                    if idx in self._current_guesses:
                        self._current_guesses[idx].center = l.value()
                return on_drag

            def make_finish_handler(idx):
                def on_drag_finish(l):
                    if idx in self._current_guesses:
                        self._current_guesses[idx].center = l.value()
                    self.peak_center_dragged.emit(idx, l.value())
                return on_drag_finish

            line.sigPositionChanged.connect(make_preview_handler(i))
            if hasattr(line, 'sigDragged'):
                line.sigDragged.connect(make_preview_handler(i))
            line.sigPositionChangeFinished.connect(make_finish_handler(i))
            self.pw.addItem(line)
            self._items[f'guess_{i}'] = line

    def _baseline_array(self):
        if self._wn_crop is None:
            return None
        if self._guess_baseline is None:
            return np.zeros_like(self._wn_crop, dtype=float)
        if len(self._guess_baseline) != len(self._wn_crop):
            return np.zeros_like(self._wn_crop, dtype=float)
        return self._guess_baseline

    def _baseline_at(self, center: float) -> float:
        if self._wn_crop is None or self._guess_baseline is None:
            return 0.0
        if len(self._guess_baseline) != len(self._wn_crop):
            return 0.0
        x = np.asarray(self._wn_crop)
        y = np.asarray(self._guess_baseline)
        if len(x) == 0:
            return 0.0
        if x[0] > x[-1]:
            x = x[::-1]
            y = y[::-1]
        return float(np.interp(center, x, y))

    def _evaluate_guess_curve(self, center: float, amplitude: float,
                              sigma: float, shape: str):
        if self._wn_crop is None:
            return np.array([])
        sigma = max(abs(float(sigma)), MIN_SIGMA)
        lmfit_amp = max(amplitude * sigma * np.sqrt(2 * np.pi), 1e-8)
        single_model = build_model(1, [shape])
        single_params = Parameters()
        single_params.add('p0_center', value=center)
        single_params.add('p0_amplitude', value=lmfit_amp)
        single_params.add('p0_sigma', value=sigma)
        try:
            return single_model.eval(single_params, x=self._wn_crop)
        except Exception:
            return np.zeros_like(self._wn_crop)

    def _evaluate_fit_peak_curve(self, wn, peak):
        shape = getattr(peak, 'shape', 'gaussian')
        sigma = max(abs(float(getattr(peak, 'sigma', MIN_SIGMA))), MIN_SIGMA)
        model = build_model(1, [shape])
        params = Parameters()
        params.add('p0_center', value=float(peak.center))
        params.add('p0_amplitude', value=max(float(peak.amplitude), 1e-8))
        params.add('p0_sigma', value=sigma)
        try:
            return model.eval(params, x=wn)
        except Exception:
            return np.zeros_like(wn)

    def _display_curve_for_peak(self, fit_result, peak_idx: int, wn):
        if peak_idx < len(getattr(fit_result, 'individual_curves', []) or []):
            curve = np.asarray(fit_result.individual_curves[peak_idx], dtype=float)
            if len(curve) == len(wn):
                return curve
        return self._evaluate_fit_peak_curve(wn, fit_result.peaks[peak_idx])

    def set_peak_locks(self, locks: list):
        self._peak_locks = {
            i: {
                'center': bool(lock.get('center', False)),
                'amplitude': bool(lock.get('amplitude', False)),
                'sigma': bool(lock.get('sigma', False)),
            }
            for i, lock in enumerate(locks)
        }

    def _is_locked(self, idx: int, key: str) -> bool:
        return bool(self._peak_locks.get(idx, {}).get(key, False))

    def _add_interactive_peak(self, idx: int, center: float, amplitude: float,
                               sigma: float = 30.0, shape: str = 'gaussian'):
        """
        피크 interactive 요소 추가:
          - PeakLine (수직선, 드래그 → center)
          - g_curve (피크 곡선 미리보기)
          - g_handle (흰 사각형 핸들, 드래그 → amplitude)
          - fwhm region (반투명 FWHM 표시)
        amplitude: 피크 높이 (absorbance 단위) — lmfit amplitude 아님
        """
        sigma = max(abs(float(sigma)), MIN_SIGMA)
        self._current_sigmas[idx] = sigma
        if self._current_guesses and idx in self._current_guesses:
            self._current_guesses[idx].sigma = sigma
        color = COLORS['peaks'][idx % len(COLORS['peaks'])]

        # 수직선
        line = PeakLine(
            pos=center, angle=90, movable=True,
            pen=pg.mkPen(pg.mkColor(color).darker(150), width=1, style=Qt.DotLine),
            hoverPen=pg.mkPen(color, width=2, style=Qt.DotLine),
            label=f'P{idx+1}',
            labelOpts={'position': 0.90, 'color': color,
                       'fill': pg.mkBrush(30, 30, 46, 200)},
            peak_idx=idx,
            on_right_click=self._on_peak_line_right_click,
        )
        line.setZValue(10)

        def make_center_handler(i):
            def on_drag_finish(l):
                self.peak_center_dragged.emit(i, l.value())
                if self._current_guesses and i in self._current_guesses:
                    self._current_guesses[i].center = l.value()
                    g = self._current_guesses[i]
                    self._update_guess_curve(i, l.value(), g.amplitude, g.sigma,
                                             getattr(g, 'shape', 'gaussian'))
            return on_drag_finish

        line.sigPositionChangeFinished.connect(make_center_handler(idx))
        self.pw.addItem(line)
        self._items[f'guess_{idx}'] = line

        # 피크 곡선 (미리보기)
        c_item = pg.PlotDataItem(pen=pg.mkPen(color, width=1.5))
        c_item.setZValue(9)
        self.pw.addItem(c_item)
        self._items[f'g_curve_{idx}'] = c_item

        # 핸들 (흰 사각형)
        handle = pg.ScatterPlotItem(
            x=[center], y=[self._baseline_at(center) + amplitude],
            symbol='s', size=10,
            pen=pg.mkPen('w', width=1.5),
            brush=pg.mkBrush(30, 30, 46),
        )
        handle.setZValue(20)
        self.pw.addItem(handle)
        self._items[f'g_handle_{idx}'] = handle

        self._update_guess_curve(idx, center, amplitude, sigma, shape)
        self._update_fwhm_region(idx, center, sigma)

    def _update_guess_curve(self, idx: int, center: float, amplitude: float,
                             sigma: float, shape: str):
        """
        guess 곡선 및 핸들 위치 갱신.
        amplitude = 피크 높이 (absorbance 단위).
        lmfit p0_amplitude = amplitude * sigma * sqrt(2π).
        """
        if self._wn_crop is None:
            return

        curve_y = self._evaluate_guess_curve(center, amplitude, sigma, shape)
        baseline_y = self._baseline_array()
        display_y = curve_y + baseline_y

        c_item = self._items.get(f'g_curve_{idx}')
        if c_item is not None:
            c_item.setData(self._wn_crop, display_y)

        # 핸들 위치 = 곡선 최대값
        peak_y = float(np.max(display_y)) if len(display_y) > 0 else self._baseline_at(center) + amplitude
        handle = self._items.get(f'g_handle_{idx}')
        if handle is not None:
            handle.setData(x=[center], y=[peak_y])

        # _current_guesses 동기
        if self._current_guesses and idx in self._current_guesses:
            self._current_guesses[idx].amplitude = amplitude

        self._update_sum_curve()

    def _update_sum_curve(self):
        """모든 g_curve 의 합산 곡선(노란색) 갱신."""
        if self._wn_crop is None:
            return

        sum_y = np.zeros_like(self._wn_crop, dtype=float)
        has_curves = False
        for idx, g in self._current_guesses.items():
            if f'g_curve_{idx}' not in self._items:
                continue
            sum_y += self._evaluate_guess_curve(
                g.center, g.amplitude, g.sigma,
                getattr(g, 'shape', 'gaussian')
            )
            has_curves = True

        if not hasattr(self, '_sum_curve_item'):
            self._sum_curve_item = pg.PlotDataItem(
                pen=pg.mkPen('#f9e2af', width=2))
            self._sum_curve_item.setZValue(8)
            self.pw.addItem(self._sum_curve_item)

        if has_curves:
            self._sum_curve_item.setData(self._wn_crop, sum_y + self._baseline_array())
            self._sum_curve_item.setVisible(True)
        else:
            self._sum_curve_item.setVisible(False)

    def update_guess_line(self, idx: int, center: float,
                          amplitude: float = None, sigma: float = None,
                          shape: str = None):
        key = f'guess_{idx}'
        if key in self._items:
            self._items[key].blockSignals(True)
            self._items[key].setValue(center)
            self._items[key].blockSignals(False)

        g = self._current_guesses.get(idx)
        if g:
            g.center = center
            if amplitude is not None:
                g.amplitude = amplitude
            if sigma is not None:
                g.sigma = sigma
                self._current_sigmas[idx] = sigma
            if shape is not None:
                g.shape = shape

            self._update_guess_curve(
                idx,
                center,
                g.amplitude,
                g.sigma,
                getattr(g, 'shape', 'gaussian'),
            )

        self._update_fwhm_region(idx, center, self._current_sigmas.get(idx, 30.0))

    def remove_guess_line(self, idx: int):
        for prefix in ('guess_', 'g_curve_', 'g_handle_'):
            key = f'{prefix}{idx}'
            if key in self._items:
                self.pw.removeItem(self._items.pop(key))
        fwhm_key = f'fwhm_{idx}'
        if fwhm_key in self._fwhm_regions:
            self.pw.removeItem(self._fwhm_regions.pop(fwhm_key))
        self._current_guesses.pop(idx, None)
        self._update_sum_curve()

    def _clear_guess_lines(self):
        for key in list(self._items.keys()):
            if key.startswith(('guess_', 'g_curve_', 'g_handle_')):
                self.pw.removeItem(self._items.pop(key))
        for key in list(self._fwhm_regions.keys()):
            self.pw.removeItem(self._fwhm_regions.pop(key))
        self._current_sigmas.clear()
        self._current_guesses.clear()
        if hasattr(self, '_sum_curve_item'):
            self._sum_curve_item.setVisible(False)
            self._sum_curve_item.setData([], [])

    def _update_fwhm_region(self, idx: int, center: float, sigma: float):
        key = f'fwhm_{idx}'
        if key in self._fwhm_regions:
            self.pw.removeItem(self._fwhm_regions.pop(key))
        if sigma <= 0:
            return
        fwhm = sigma * 2.3548
        color = QColor(COLORS['peaks'][idx % len(COLORS['peaks'])])
        color.setAlpha(30)
        region = pg.LinearRegionItem(
            values=[center - fwhm / 2, center + fwhm / 2],
            brush=pg.mkBrush(color),
            pen=pg.mkPen(None),
            movable=False,
        )
        region.setZValue(5)
        self.pw.addItem(region)
        self._fwhm_regions[key] = region

    def _on_peak_line_right_click(self, idx: int):
        """수직선 우클릭 → FWHM 입력 다이얼로그."""
        if self._is_locked(idx, 'sigma'):
            return
        from PyQt5.QtWidgets import QInputDialog
        sigma = self._current_sigmas.get(idx, 30.0)
        fwhm_current = sigma * 2.3548
        fwhm, ok = QInputDialog.getDouble(
            self, f'P{idx + 1} FWHM',
            f'P{idx + 1}  FWHM (cm⁻¹):',
            value=round(fwhm_current, 1),
            min=1.0, max=500.0, decimals=1,
        )
        if ok:
            new_sigma = fwhm / 2.3548
            self._current_sigmas[idx] = new_sigma
            center = self._items[f'guess_{idx}'].value() if f'guess_{idx}' in self._items else 0
            if self._current_guesses and idx in self._current_guesses:
                self._hide_fit_fills()
                self._current_guesses[idx].sigma = new_sigma
                g = self._current_guesses[idx]
                self._update_guess_curve(idx, center, g.amplitude, new_sigma,
                                         getattr(g, 'shape', 'gaussian'))
            self._update_fwhm_region(idx, center, new_sigma)
            self.peak_sigma_changed.emit(idx, new_sigma)

    # ── 피팅 결과 표시 ────────────────────────────────────────

    def show_fit_result(self, wn, ab_corr, fit_result, baseline=None):
        self._clear_guess_lines()
        wn = np.asarray(wn, dtype=float)
        ab_corr = np.asarray(ab_corr, dtype=float)
        self._guess_baseline = (
            np.asarray(baseline, dtype=float)
            if baseline is not None and len(baseline) == len(wn) else None
        )
        for fill in self._fill_items:
            self.pw.removeItem(fill)
        self._fill_items.clear()
        for key in list(self._items.keys()):
            if key.startswith(('peak_', 'label_', 'fitted', 'residual')):
                self.pw.removeItem(self._items.pop(key))

        base = (
            np.asarray(baseline, dtype=float)
            if baseline is not None and len(baseline) == len(wn)
            else np.zeros_like(wn)
        )
        display_individual_curves = []
        for i, peak in enumerate(fit_result.peaks):
            curve = self._display_curve_for_peak(fit_result, i, wn)
            display_individual_curves.append(curve)
            display_curve = curve + base
            color = QColor(COLORS['peaks'][i % len(COLORS['peaks'])])
            color.setAlpha(80)
            fill = pg.FillBetweenItem(
                pg.PlotDataItem(wn, display_curve),
                pg.PlotDataItem(wn, base),
                brush=pg.mkBrush(color)
            )
            self.pw.addItem(fill)
            self._fill_items.append(fill)

            c = COLORS['peaks'][i % len(COLORS['peaks'])]
            pk_item = self.pw.plot(
                wn, display_curve,
                pen=pg.mkPen(c, width=1.5),
                name=f'P{i+1}: {peak.center:.0f} cm⁻¹ ({peak.area_fraction:.1f}%)'
            )
            self._items[f'peak_{i}'] = pk_item

            idx_max = np.argmax(curve)
            text = pg.TextItem(
                f'{peak.center:.0f}\n{peak.area_fraction:.1f}%',
                color=c, anchor=(0.5, 1.1)
            )
            label_font = pg.QtGui.QFont('', 13)
            label_font.setBold(True)
            text.setFont(label_font)
            text.setPos(wn[idx_max], display_curve[idx_max])
            self.pw.addItem(text)
            self._items[f'label_{i}'] = text

        fitted_curve = np.asarray(getattr(fit_result, 'fitted_curve', []), dtype=float)
        if len(fitted_curve) != len(wn):
            fitted_curve = np.sum(display_individual_curves, axis=0) if display_individual_curves else np.zeros_like(wn)

        item = self.pw.plot(wn, fitted_curve + base,
                            pen=pg.mkPen(COLORS['fitted'], width=2.5),
                            name=f'Fit  R²={fit_result.r_squared:.4f}')
        self._items['fitted'] = item

        residual = np.asarray(getattr(fit_result, 'residual', []), dtype=float)
        if len(residual) != len(wn):
            residual = ab_corr - fitted_curve if len(ab_corr) == len(wn) else np.zeros_like(wn)

        item = self.pw.plot(wn, residual,
                            pen=pg.mkPen(COLORS['residual'], width=1),
                            name='Residual')
        item.setVisible(self.cb_residual.isChecked())
        self._items['residual'] = item

        # 피팅 완료 후 인터랙티브 핸들 재생성
        for i, peak in enumerate(fit_result.peaks):
            self._add_draggable_line(i, peak.center, peak.sigma,
                                     display_individual_curves[i], wn)

    def _add_draggable_line(self, idx: int, center: float, sigma: float,
                             fitted_curve: np.ndarray = None, wn: np.ndarray = None):
        """
        피팅 완료 후 호출: 인터랙티브 수직선 + 피크 곡선(g_curve) + 핸들 추가.
        g_curve 가 있어야 드래그 시 곡선 + 합산이 실시간 갱신됨.
        fitted_curve: 해당 피크의 피팅 결과 곡선 (초기 표시 + 핸들 y 위치용).
        """
        from core.peak_finder import PeakGuess

        if fitted_curve is not None and len(fitted_curve) > 0:
            amp_y = float(np.max(fitted_curve))
        else:
            amp_y = 0.1

        self._current_guesses[idx] = PeakGuess(
            center=center, amplitude=amp_y, sigma=sigma, index=idx)
        self._current_sigmas[idx] = sigma

        if wn is not None:
            self._wn_crop = wn

        color = COLORS['peaks'][idx % len(COLORS['peaks'])]

        # 수직선
        line = PeakLine(
            pos=center, angle=90, movable=True,
            pen=pg.mkPen(pg.mkColor(color).darker(150), width=1, style=Qt.DotLine),
            hoverPen=pg.mkPen(color, width=2, style=Qt.DotLine),
            label=f'P{idx+1}',
            labelOpts={'position': 0.90, 'color': color,
                       'fill': pg.mkBrush(30, 30, 46, 200)},
            peak_idx=idx,
            on_right_click=self._on_peak_line_right_click,
        )
        line.setZValue(10)

        def make_center_preview_handler(i):
            def on_drag(l):
                if self._current_guesses and i in self._current_guesses:
                    if self._is_locked(i, 'center'):
                        g = self._current_guesses[i]
                        line = self._items.get(f'guess_{i}')
                        if line is not None:
                            line.blockSignals(True)
                            line.setValue(g.center)
                            line.blockSignals(False)
                        return
                    self._hide_fit_fills()
                    g = self._current_guesses[i]
                    g.center = l.value()
                    self._update_guess_curve(
                        i, l.value(), g.amplitude, g.sigma,
                        getattr(g, 'shape', 'gaussian')
                    )
                    self._update_fwhm_region(i, l.value(), g.sigma)
            return on_drag

        def make_center_handler(i):
            def on_drag_finish(l):
                if self._current_guesses and i in self._current_guesses and self._is_locked(i, 'center'):
                    g = self._current_guesses[i]
                    line = self._items.get(f'guess_{i}')
                    if line is not None:
                        line.blockSignals(True)
                        line.setValue(g.center)
                        line.blockSignals(False)
                    return
                self._hide_fit_fills()
                self.peak_center_dragged.emit(i, l.value())
                if self._current_guesses and i in self._current_guesses:
                    g = self._current_guesses[i]
                    g.center = l.value()
                    self._update_guess_curve(i, l.value(), g.amplitude, g.sigma,
                                             getattr(g, 'shape', 'gaussian'))
                    self._update_fwhm_region(i, l.value(), g.sigma)
            return on_drag_finish

        line.sigPositionChanged.connect(make_center_preview_handler(idx))
        if hasattr(line, 'sigDragged'):
            line.sigDragged.connect(make_center_preview_handler(idx))
        line.sigPositionChangeFinished.connect(make_center_handler(idx))
        self.pw.addItem(line)
        self._items[f'guess_{idx}'] = line

        # g_curve — 드래그 시 실시간 업데이트용 피크 곡선
        c_item = pg.PlotDataItem(pen=pg.mkPen(color, width=2))
        c_item.setZValue(11)   # 피팅 fill(z=0) 위, 수직선(z=10) 아래
        self.pw.addItem(c_item)
        self._items[f'g_curve_{idx}'] = c_item
        # 초기값: 피팅 결과 곡선 그대로 표시
        if fitted_curve is not None and wn is not None:
            c_item.setData(wn, fitted_curve + self._baseline_array())

        # 핸들 (흰 사각형)
        handle = pg.ScatterPlotItem(
            x=[center], y=[self._baseline_at(center) + amp_y],
            symbol='s', size=10,
            pen=pg.mkPen('w', width=1.5),
            brush=pg.mkBrush(30, 30, 46),
        )
        handle.setZValue(20)
        self.pw.addItem(handle)
        self._items[f'g_handle_{idx}'] = handle

        self._update_sum_curve()

    # ── 이벤트 필터 (우클릭 드래그 + 핸들 드래그) ────────────

    def eventFilter(self, obj, event):
        if obj is not self.pw.viewport():
            return super().eventFilter(obj, event)

        t = event.type()
        vb = self.pw.getViewBox()

        def to_data(qpos):
            scene_pt = self.pw.mapToScene(qpos)
            return vb.mapSceneToView(scene_pt)

        # ── 마우스 버튼 누름 ──
        if t == QEvent.MouseButtonPress:
            pt = to_data(event.pos())

            if self._total_mode:
                if self._baseline_mode:
                    return False
                if event.button() == Qt.RightButton:
                    self._total_region_drag = {
                        'start_x': float(pt.x()),
                        'start_px': int(event.pos().x()),
                        'start_py': int(event.pos().y()),
                    }
                    self.pw.setCursor(Qt.CrossCursor)
                    return True
                if event.button() == Qt.LeftButton and self._total_shift_enabled:
                    spec = self._hit_test_total_spectrum(pt)
                    if spec is not None:
                        self.total_spectrum_selected.emit(spec['name'])
                        self._total_drag = {
                            'spec': spec,
                            'start_y': float(pt.y()),
                            'start_shift': float(spec.get('shift', 0.0)),
                        }
                        self.pw.setCursor(Qt.ClosedHandCursor)
                        return True
                if event.button() == Qt.LeftButton:
                    spec = self._hit_test_total_spectrum(pt)
                    if spec is not None:
                        self.total_spectrum_selected.emit(spec['name'])
                        return True

            if event.button() == Qt.RightButton:
                if self._baseline_mode:
                    return False   # 베이스라인 모드: sigMouseClicked에 맡김
                # 핸들 위에서 우클릭 → 핸들 자유 이동
                hit = self._hit_test_handles(pt)
                if hit >= 0:
                    if self._is_locked(hit, 'center') and self._is_locked(hit, 'amplitude'):
                        return True
                    self._handle_drag = {'idx': hit, 'button': Qt.RightButton}
                    return True
                if self._hit_test_guess_lines(pt) >= 0:
                    return False
                # 빈 공간 우클릭 → 새 피크 생성
                self._rclick_drag = {'start': pt, 'preview_idx': None}
                self.pw.setCursor(Qt.CrossCursor)
                return True

            if event.button() == Qt.LeftButton and not self._baseline_mode:
                # 핸들 위에서 좌클릭 → 핸들 자유 이동
                hit = self._hit_test_handles(pt)
                if hit >= 0:
                    if self._is_locked(hit, 'center') and self._is_locked(hit, 'amplitude'):
                        return True
                    self._handle_drag = {'idx': hit, 'button': Qt.LeftButton}
                    return True

        # ── 마우스 이동 ──
        elif t == QEvent.MouseMove:
            pt = to_data(event.pos())

            if self._total_mode:
                self._update_total_probe(pt)
                if (
                    self._total_region_drag is not None
                    and (event.buttons() & Qt.RightButton)
                ):
                    dx = int(event.pos().x()) - self._total_region_drag['start_px']
                    dy = int(event.pos().y()) - self._total_region_drag['start_py']
                    if dx * dx + dy * dy >= 16:
                        self._update_total_region_preview(pt.x())
                    return True
                if self._total_drag is not None and (event.buttons() & Qt.LeftButton):
                    dy = float(pt.y()) - self._total_drag['start_y']
                    self._update_total_shift(
                        self._total_drag['spec'],
                        self._total_drag['start_shift'] + dy,
                    )
                    return True

            if self._handle_drag is not None:
                btn = self._handle_drag.get('button', Qt.LeftButton)
                if event.buttons() & btn:
                    self._on_handle_dragged(self._handle_drag['idx'], pt.x(), pt.y())
                    return True

            if self._rclick_drag is not None and (event.buttons() & Qt.RightButton):
                self._update_rclick_preview(pt)
                return True

        # ── 마우스 버튼 뗌 ──
        elif t == QEvent.MouseButtonRelease:
            if self._total_region_drag is not None and event.button() == Qt.RightButton:
                pt = to_data(event.pos())
                dx = int(event.pos().x()) - self._total_region_drag['start_px']
                dy = int(event.pos().y()) - self._total_region_drag['start_py']
                start_x = float(self._total_region_drag['start_x'])
                self._total_region_drag = None
                self._clear_total_region_preview()
                self.pw.setCursor(Qt.CrossCursor if self._total_probe_enabled else (
                    Qt.OpenHandCursor if self._total_shift_enabled else Qt.ArrowCursor))
                if dx * dx + dy * dy >= 16 and abs(float(pt.x()) - start_x) > 1e-9:
                    self.total_region_toggled.emit(start_x, float(pt.x()))
                else:
                    self._show_total_context_menu(event.globalPos())
                return True

            if self._total_drag is not None and event.button() == Qt.LeftButton:
                self._total_drag = None
                self.pw.setCursor(Qt.OpenHandCursor if self._total_shift_enabled else (
                    Qt.CrossCursor if self._total_probe_enabled else Qt.ArrowCursor))
                return True

            # 핸들 드래그 종료 (좌/우 공통)
            if self._handle_drag is not None:
                btn = self._handle_drag.get('button', Qt.LeftButton)
                if event.button() == btn:
                    self._handle_drag = None
                    return True

            if event.button() == Qt.RightButton and self._rclick_drag is not None:
                pt = to_data(event.pos())
                self._finish_rclick_drag(pt)
                self._rclick_drag = None
                self.pw.setCursor(Qt.ArrowCursor)
                return True

        return super().eventFilter(obj, event)

    def _hit_test_handles(self, data_pt) -> int:
        """data_pt 근방의 핸들 idx 반환. 없으면 -1."""
        vb = self.pw.getViewBox()
        vr = vb.viewRange()
        xrange = max(vr[0][1] - vr[0][0], 1e-9)
        yrange = max(vr[1][1] - vr[1][0], 1e-9)
        view_rect = vb.rect()
        px_per_x = view_rect.width() / xrange
        px_per_y = view_rect.height() / yrange
        # 히트 반경: 12 픽셀
        hit_x = 12.0 / px_per_x if px_per_x > 0 else xrange * 0.01
        hit_y = 12.0 / px_per_y if px_per_y > 0 else yrange * 0.01

        for idx in list(self._current_guesses.keys()):
            handle = self._items.get(f'g_handle_{idx}')
            if handle is None:
                continue
            try:
                hx, hy = handle.getData()
            except Exception:
                continue
            if hx is None or len(hx) == 0:
                continue
            dx = abs(data_pt.x() - hx[0]) / hit_x
            dy = abs(data_pt.y() - hy[0]) / hit_y
            if dx * dx + dy * dy < 1.0:
                return idx
        return -1

    def _hit_test_guess_lines(self, data_pt) -> int:
        vb = self.pw.getViewBox()
        vr = vb.viewRange()
        xrange = max(vr[0][1] - vr[0][0], 1e-9)
        view_rect = vb.rect()
        px_per_x = view_rect.width() / xrange
        hit_x = 8.0 / px_per_x if px_per_x > 0 else xrange * 0.01

        for idx in list(self._current_guesses.keys()):
            line = self._items.get(f'guess_{idx}')
            if line is None:
                continue
            if abs(data_pt.x() - line.value()) <= hit_x:
                return idx
        return -1

    def _hide_fit_fills(self):
        """피팅 결과 fill/peak/label/fitted 곡선 숨기기.
        핸들 드래그 시작 시 호출 — 이후 g_curve + sum_curve 만 표시.
        """
        for fill in self._fill_items:
            fill.setVisible(False)
        for key in list(self._items.keys()):
            if key.startswith(('peak_', 'label_', 'fitted', 'residual')):
                self._items[key].setVisible(False)

    def _on_handle_dragged(self, idx: int, new_x: float, new_y: float):
        """핸들 드래그: center(x)와 amplitude(y) 동시 변경.
        드래그 후 자동 Run Fit 없음 — 합산 곡선만 실시간 업데이트.
        """
        # 첫 드래그 시 이전 피팅 결과 숨김
        self._hide_fit_fills()

        g = self._current_guesses.get(idx)
        if g is None:
            return
        new_height = max(new_y - self._baseline_at(new_x), 1e-6)

        if self._is_locked(idx, 'center'):
            new_x = g.center
        if self._is_locked(idx, 'amplitude'):
            new_height = g.amplitude

        g.center = new_x
        g.amplitude = new_height
        shape = getattr(g, 'shape', 'gaussian')

        # PeakLine(수직선) 위치 동기화 — 시그널 루프 방지
        line = self._items.get(f'guess_{idx}')
        if line is not None:
            line.blockSignals(True)
            line.setValue(new_x)
            line.blockSignals(False)

        # g_curve 가 있으면 실시간 업데이트 (합산 곡선 포함)
        if f'g_curve_{idx}' in self._items:
            self._update_guess_curve(idx, new_x, new_height, g.sigma, shape)
            self._update_fwhm_region(idx, new_x, g.sigma)
        else:
            # 피팅 후 모드: 핸들만 위치 갱신
            handle = self._items.get(f'g_handle_{idx}')
            if handle is not None:
                handle.setData(x=[new_x], y=[self._baseline_at(new_x) + new_height])

        # right_panel 테이블 동기화 (auto refit 없음)
        self.peak_center_dragged.emit(idx, new_x)
        self.peak_amplitude_dragged.emit(idx, new_height)

    # ── 우클릭 드래그 피크 생성 ───────────────────────────────

    def _update_rclick_preview(self, current_pt):
        """우클릭 드래그 중 미리보기 피크 실시간 갱신."""
        if self._wn_crop is None:
            return

        start = self._rclick_drag['start']
        center = start.x()
        amp_y = max(current_pt.y() - self._baseline_at(center), 1e-6)
        dx = abs(current_pt.x() - start.x())
        sigma = max(dx / 2.3548, MIN_SIGMA)

        from core.peak_finder import PeakGuess

        pidx = self._rclick_drag.get('preview_idx')
        if pidx is None:
            # 새 피크 인덱스 결정
            existing = [int(k.split('_')[1]) for k in self._items if k.startswith('g_curve_')]
            pidx = max(existing, default=-1) + 1
            self._rclick_drag['preview_idx'] = pidx
            g = PeakGuess(center=center, amplitude=amp_y, sigma=sigma, index=pidx)
            self._current_guesses[pidx] = g
            self._add_interactive_peak(pidx, center, amp_y, sigma)
        else:
            g = self._current_guesses.get(pidx)
            if g is None:
                return
            g.center = center
            g.amplitude = amp_y
            g.sigma = sigma
            self._current_sigmas[pidx] = sigma
            self._update_guess_curve(pidx, center, amp_y, sigma,
                                     getattr(g, 'shape', 'gaussian'))
            self._update_fwhm_region(pidx, center, sigma)

    def _finish_rclick_drag(self, release_pt):
        """우클릭 드래그 완료: 피크 확정 또는 미리보기 제거."""
        pidx = self._rclick_drag.get('preview_idx')
        if pidx is None:
            return   # 드래그 없이 바로 뗀 경우

        start = self._rclick_drag['start']
        center = start.x()
        dx = abs(release_pt.x() - start.x())
        sigma = max(dx / 2.3548, MIN_SIGMA)
        amp_y = max(release_pt.y() - self._baseline_at(center), 1e-6)

        # 드래그가 너무 짧으면 (거의 클릭) 제거
        if dx < 2.0:
            self.remove_guess_line(pidx)
            return

        # 피크 확정 → 외부에서 right_panel 에 추가
        self.peak_created.emit(center, amp_y, sigma)

    # ── 마우스 클릭 (베이스라인 / Add Peak 모드) ─────────────

    def _on_peak_add_mode(self, checked):
        self._peak_add_mode = checked
        if checked:
            self._baseline_mode = False
        cursor = Qt.CrossCursor if checked else Qt.ArrowCursor
        self.pw.setCursor(cursor)

    def _on_mouse_click(self, event):
        pos = event.scenePos()
        vb = self.pw.getViewBox()
        pt = vb.mapSceneToView(pos)
        click_wn, click_ab = pt.x(), pt.y()

        if event.button() == Qt.RightButton and self._baseline_mode:
            # 가장 가까운 베이스라인 포인트 제거
            min_dist = float('inf')
            min_idx = -1
            for i, scatter in enumerate(self._bl_scatter_items):
                xs, _ = scatter.getData()
                if len(xs) > 0:
                    dist = abs(xs[0] - click_wn)
                    if dist < min_dist:
                        min_dist = dist
                        min_idx = i
            if min_idx >= 0:
                self.pw.removeItem(self._bl_scatter_items[min_idx])
                self._bl_scatter_items.pop(min_idx)
                self.baseline_point_removed.emit(min_idx)
            return

        if event.button() != Qt.LeftButton:
            return

        if self._peak_add_mode:
            # 클릭 y 위치 = 초기 peak height, 기본 sigma
            amp_y = max(click_ab - self._baseline_at(click_wn), 1e-6)
            self.peak_created.emit(click_wn, amp_y, 30.0)

        elif self._baseline_mode:
            scatter = pg.ScatterPlotItem(
                [click_wn], [click_ab], symbol='o', size=8,
                brush=pg.mkBrush(COLORS['baseline']),
                pen=pg.mkPen('#1e1e2e', width=1)
            )
            self.pw.addItem(scatter)
            self._bl_scatter_items.append(scatter)
            self.baseline_point_added.emit(click_wn, click_ab)

    # ── CO / Si-O Baseline Endpoint 드래그 ───────────────────

    def _ab_at(self, wn_pos: float) -> float:
        if self._ep_ref_wn is None or self._ep_ref_ab is None:
            return 0.0
        idx = int(np.argmin(np.abs(self._ep_ref_wn - wn_pos)))
        return float(self._ep_ref_ab[idx])

    def _draw_ep_baseline(self, key_prefix: str, ep0: float, ep1: float, color: str):
        curve_key = f'bl_{key_prefix}'
        wn = self._ep_ref_wn
        if wn is None:
            return
        left, right = min(ep0, ep1), max(ep0, ep1)
        mask = (wn >= left) & (wn <= right)
        if not np.any(mask):
            return
        wn_seg = wn[mask]
        y0, y1 = self._ab_at(left), self._ab_at(right)
        bl_seg = np.interp(wn_seg, [left, right], [y0, y1])
        if curve_key in self._items:
            self._items[curve_key].setData(wn_seg, bl_seg)
        else:
            item = self.pw.plot(
                wn_seg, bl_seg,
                pen=pg.mkPen(color, width=1.8, style=Qt.DashLine),
                name=key_prefix)
            self._items[curve_key] = item

    def show_co_baselines(self, wn, ab,
                          l_endpoints=(2000.0, 2100.0),
                          b_endpoints=(1700.0, 1900.0),
                          draw_baseline: bool = True):
        self._ep_ref_wn = wn
        self._ep_ref_ab = ab
        self.clear_endpoint_items()

        specs = [
            ('CO_L', l_endpoints, '#89b4fa'),
            ('CO_B', b_endpoints, '#fab387'),
        ]
        for sub, (ep0, ep1), color in specs:
            for side, ep_wn in enumerate([ep0, ep1]):
                key = f'{sub}_{side}'
                line = pg.InfiniteLine(
                    pos=ep_wn, angle=90, movable=True,
                    pen=pg.mkPen(color, width=2, style=Qt.DashLine),
                    hoverPen=pg.mkPen(color, width=4),
                    label=f'{sub} {"L" if side==0 else "R"} {{value:.0f}}',
                    labelOpts={'position': 0.15, 'color': color,
                               'fill': pg.mkBrush(30, 30, 46, 180)},
                )
                line.setZValue(8)

                def _make_handler(s, sd):
                    def _on_change(l):
                        ep_keys = [f'{s}_0', f'{s}_1']
                        eps = [self._ep_lines[k].value() for k in ep_keys]
                        clr = '#89b4fa' if s == 'CO_L' else '#fab387'
                        if draw_baseline:
                            self._draw_ep_baseline(s, eps[0], eps[1], clr)
                    def _on_done(l):
                        _on_change(l)
                        self.co_endpoint_moved.emit(s, sd, l.value())
                    return _on_change, _on_done

                on_change, on_done = _make_handler(sub, side)
                line.sigPositionChanged.connect(on_change)
                line.sigPositionChangeFinished.connect(on_done)
                self.pw.addItem(line)
                self._ep_lines[f'{sub}_{side}'] = line

            if draw_baseline:
                self._draw_ep_baseline(sub, ep0, ep1, color)

    def show_sio_baseline(self, wn, ab, endpoints=(1100.0, 1300.0),
                          draw_baseline: bool = True):
        self._ep_ref_wn = wn
        self._ep_ref_ab = ab
        self.clear_endpoint_items()

        color = '#cba6f7'
        for side, ep_wn in enumerate(endpoints):
            key = f'SiO_{side}'
            line = pg.InfiniteLine(
                pos=ep_wn, angle=90, movable=True,
                pen=pg.mkPen(color, width=2, style=Qt.DashLine),
                hoverPen=pg.mkPen(color, width=4),
                label=f'Si-O {"L" if side==0 else "R"} {{value:.0f}}',
                labelOpts={'position': 0.15, 'color': color,
                           'fill': pg.mkBrush(30, 30, 46, 180)},
            )
            line.setZValue(8)

            def _make_sio_handler(sd):
                def _on_change(l):
                    if draw_baseline:
                        eps = [self._ep_lines['SiO_0'].value(),
                               self._ep_lines['SiO_1'].value()]
                        self._draw_ep_baseline('SiO', eps[0], eps[1], color)
                def _on_done(l):
                    _on_change(l)
                    self.sio_endpoint_moved.emit(sd, l.value())
                return _on_change, _on_done

            on_change, on_done = _make_sio_handler(side)
            line.sigPositionChanged.connect(on_change)
            line.sigPositionChangeFinished.connect(on_done)
            self.pw.addItem(line)
            self._ep_lines[key] = line

        if draw_baseline:
            self._draw_ep_baseline('SiO', endpoints[0], endpoints[1], color)

    def show_sio_region_handles(self, wn, ab, endpoints=(1100.0, 1300.0)):
        """Show draggable Si-O integration-region handles without a baseline line."""
        self.show_sio_baseline(wn, ab, endpoints=endpoints, draw_baseline=False)

    def get_co_endpoints(self) -> dict:
        result = {}
        for sub in ('CO_L', 'CO_B'):
            k0, k1 = f'{sub}_0', f'{sub}_1'
            if k0 in self._ep_lines and k1 in self._ep_lines:
                result[sub] = (self._ep_lines[k0].value(),
                               self._ep_lines[k1].value())
        return result

    def get_sio_endpoints(self) -> tuple:
        k0, k1 = 'SiO_0', 'SiO_1'
        if k0 in self._ep_lines and k1 in self._ep_lines:
            return (self._ep_lines[k0].value(), self._ep_lines[k1].value())
        return (1100.0, 1300.0)

    def update_co_endpoints(self, sub: str, ep0: float, ep1: float):
        for side, val in enumerate([ep0, ep1]):
            key = f'{sub}_{side}'
            if key in self._ep_lines:
                self._ep_lines[key].blockSignals(True)
                self._ep_lines[key].setValue(val)
                self._ep_lines[key].blockSignals(False)
        color = '#89b4fa' if sub == 'CO_L' else '#fab387'
        self._draw_ep_baseline(sub, ep0, ep1, color)

    def update_sio_endpoints(self, ep0: float, ep1: float):
        for side, val in enumerate([ep0, ep1]):
            key = f'SiO_{side}'
            if key in self._ep_lines:
                self._ep_lines[key].blockSignals(True)
                self._ep_lines[key].setValue(val)
                self._ep_lines[key].blockSignals(False)
        if 'bl_SiO' in self._items:
            self._draw_ep_baseline('SiO', ep0, ep1, '#cba6f7')

    def show_analysis_region(self, wn_pairs: list, color: str = '#cdd6f4'):
        self.clear_analysis_region()
        pen = pg.mkPen('#ffffff', width=1.5, style=Qt.SolidLine)
        for wn_min, wn_max in wn_pairs:
            for pos in (min(wn_min, wn_max), max(wn_min, wn_max)):
                line = pg.InfiniteLine(pos=pos, angle=90, movable=False, pen=pen)
                line.setZValue(20)
                self.pw.addItem(line)
                self._region_items.append(line)

    def clear_analysis_region(self):
        for item in self._region_items:
            try:
                self.pw.removeItem(item)
            except Exception:
                pass
        self._region_items.clear()

    def zoom_to(self, wn_min: float, wn_max: float, padding: float = 0.05):
        lo, hi = min(wn_min, wn_max), max(wn_min, wn_max)
        span = hi - lo
        self.pw.setXRange(lo - span * padding, hi + span * padding, padding=0)

    def clear_endpoint_items(self):
        for key in list(self._ep_lines.keys()):
            try:
                self.pw.removeItem(self._ep_lines.pop(key))
            except Exception:
                pass
        for key in [k for k in self._items
                    if k.startswith('bl_CO') or k.startswith('bl_SiO')]:
            try:
                self.pw.removeItem(self._items.pop(key))
            except Exception:
                pass

    # ── 뷰 컨트롤 ─────────────────────────────────────────────

    def reset_view(self):
        self._has_spectrum = False

    def do_auto_range(self):
        self.pw.autoRange()

    def fit_y_to_current_x_range(self, padding: float = 0.05,
                                 include_keys: tuple[str, ...] | None = None,
                                 exclude_keys: tuple[str, ...] = (),
                                 exclude_overlay_kinds: tuple[str, ...] = ()):
        """Fit y range to visible curve data inside the current x range."""
        vb = self.pw.getViewBox()
        x_range, _ = vb.viewRange()
        x0, x1 = sorted(x_range)
        ys = []

        def should_include_key(key: str) -> bool:
            if any(key == pattern or key.startswith(pattern)
                   for pattern in exclude_keys):
                return False
            if include_keys is None:
                return True
            return any(key == pattern or key.startswith(pattern)
                       for pattern in include_keys)

        def collect_from_item(item):
            if item is None or not item.isVisible() or not hasattr(item, 'getData'):
                return
            try:
                x, y = item.getData()
            except Exception:
                return
            if x is None or y is None:
                return
            x = np.asarray(x, dtype=float)
            y = np.asarray(y, dtype=float)
            if x.size == 0 or y.size == 0:
                return
            n = min(x.size, y.size)
            x = x[:n]
            y = y[:n]
            try:
                pos = item.pos()
                y = y + float(pos.y())
            except Exception:
                pass
            mask = (x >= x0) & (x <= x1) & np.isfinite(x) & np.isfinite(y)
            if np.any(mask):
                ys.append(y[mask])

        for key, item in self._items.items():
            if should_include_key(key):
                collect_from_item(item)
        if include_keys is None:
            for overlay in self._overlay_items.values():
                if overlay.get('kind') in exclude_overlay_kinds:
                    continue
                collect_from_item(overlay.get('item'))
            for item in self._total_items.values():
                collect_from_item(item)

        if not ys:
            vb.enableAutoRange('y', True)
            vb.autoRange()
            return

        y_values = np.concatenate(ys)
        y_min = float(np.min(y_values))
        y_max = float(np.max(y_values))
        span = y_max - y_min
        if span <= 0:
            span = max(abs(y_min), 1.0) * 0.1
            y_min -= span
            y_max += span
        else:
            y_min -= span * padding
            y_max += span * padding
        self.pw.setYRange(y_min, y_max, padding=0)

    def fit_y_to_series_in_current_x_range(self, series, padding: float = 0.05) -> bool:
        """Fit y range from explicit (x, y) series clipped to the current x range."""
        vb = self.pw.getViewBox()
        x_range, _ = vb.viewRange()
        x0, x1 = sorted(x_range)
        ys = []

        for x, y in series:
            if x is None or y is None:
                continue
            x = np.asarray(x, dtype=float)
            y = np.asarray(y, dtype=float)
            if x.size == 0 or y.size == 0:
                continue
            n = min(x.size, y.size)
            x = x[:n]
            y = y[:n]
            mask = (x >= x0) & (x <= x1) & np.isfinite(x) & np.isfinite(y)
            if np.any(mask):
                ys.append(y[mask])

        if not ys:
            return False

        y_values = np.concatenate(ys)
        y_min = float(np.min(y_values))
        y_max = float(np.max(y_values))
        span = y_max - y_min
        if span <= 0:
            span = max(abs(y_min), 1.0) * 0.1
            y_min -= span
            y_max += span
        else:
            y_min -= span * padding
            y_max += span * padding
        self.pw.setYRange(y_min, y_max, padding=0)
        return True

    def get_view_state(self):
        vb = self.pw.getViewBox()
        x_range, y_range = vb.viewRange()
        return {
            'x_range': tuple(x_range),
            'y_range': tuple(y_range),
        }

    def restore_view_state(self, state):
        if not state:
            return
        x_range = state.get('x_range')
        y_range = state.get('y_range')
        if x_range is not None:
            self.pw.setXRange(x_range[0], x_range[1], padding=0)
        if y_range is not None:
            self.pw.setYRange(y_range[0], y_range[1], padding=0)

    def do_export_image(self, filepath: str):
        if filepath.lower().endswith('.svg'):
            from pyqtgraph.exporters import SVGExporter
            exporter = SVGExporter(self.pw.getPlotItem())
        else:
            from pyqtgraph.exporters import ImageExporter
            exporter = ImageExporter(self.pw.getPlotItem())
        exporter.export(filepath)

    def set_x_auto_range(self, enabled: bool):
        vb = self.pw.getViewBox()
        vb.enableAutoRange('x', enabled)
        if enabled:
            vb.autoRange()

    def set_y_auto_range(self, enabled: bool):
        vb = self.pw.getViewBox()
        vb.setAutoVisible(y=enabled)
        vb.enableAutoRange('y', enabled)
        if enabled:
            vb.updateAutoRange()

    def clear_fit_result(self):
        self._clear_guess_lines()
        self._guess_baseline = None
        for fill in self._fill_items:
            self.pw.removeItem(fill)
        self._fill_items.clear()
        for key in list(self._items.keys()):
            if key.startswith(('peak_', 'label_', 'fitted', 'residual')):
                self.pw.removeItem(self._items.pop(key))

    # ── 유틸 ──────────────────────────────────────────────────

    def _set_visible(self, key, visible):
        if key in self._items:
            self._items[key].setVisible(visible)
        if key == 'baseline':
            for scatter in self._bl_scatter_items:
                scatter.setVisible(visible)
        if key == 'raw':
            for overlay in self._overlay_items.values():
                if overlay.get('kind') == 'raw':
                    overlay['item'].setVisible(visible)

    def _clear_all(self):
        self.pw.clear()
        try:
            self.legend.clear()
        except Exception:
            pass
        self._items.clear()
        self._overlay_items.clear()
        self._fill_items.clear()
        self._bl_scatter_items.clear()
        self._fwhm_regions.clear()
        self._current_sigmas.clear()
        self._current_guesses.clear()
        self._guess_baseline = None
        self._peak_locks.clear()
        self._ep_lines.clear()
        self._region_items.clear()
        self._total_mode = False
        self._total_specs.clear()
        self._total_items.clear()
        self._total_drag = None
        self._total_region_drag = None
        self._total_region_preview = None
        self._total_inactive_region_items.clear()
        if hasattr(self, '_coord_label'):
            self._coord_label.setVisible(False)
            self._coord_label.setText("")
        self._coord_text = None
        self._coord_vline = None
        self._coord_hline = None
        # _sum_curve_item 은 pw.clear() 로 이미 제거됨
        if hasattr(self, '_sum_curve_item'):
            del self._sum_curve_item
