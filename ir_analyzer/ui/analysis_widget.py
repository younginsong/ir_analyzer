"""
analysis_widget.py - Analysis 탭
OH 서브탭: Area Fraction, Stark Tuning, OH Total Area, OH/SiO Normalized
CO 서브탭: CO_L Area, CO_B Area, CO_L/CO_B Ratio, CO Stark Tuning
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from scipy.stats import linregress
from collections import defaultdict
from typing import Optional


def _connect_points(pw, xs, ys, color, width=1.5):
    """포인트를 전위 순으로 정렬 후 직선으로 연결"""
    order = np.argsort(xs)
    pw.plot(xs[order], ys[order], pen=pg.mkPen(color, width=width))

from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QVBoxLayout, QGridLayout, QTabWidget,
    QComboBox,
)
from PyQt5.QtCore import Qt, pyqtSignal


PEAK_COLORS = ['#89b4fa', '#fab387', '#cba6f7', '#94e2d5',
               '#f9e2af', '#a6e3a1', '#89dceb', '#f38ba8']
SESSION_COLORS = ['#89b4fa', '#a6e3a1', '#fab387', '#f38ba8',
                  '#cba6f7', '#94e2d5', '#f9e2af', '#89dceb']
COMPARE_METRICS = [
    ('P1', (0,)),
    ('P2', (1,)),
    ('P3', (2,)),
    ('P4', (3,)),
    ('P1+P2', (0, 1)),
    ('P3+P4', (2, 3)),
]
COMPARE_METRIC_MAP = dict(COMPARE_METRICS)
DEFAULT_COMPARE_METRIC = 'P3+P4'


def _style_pw(pw, title, xlabel, ylabel):
    """공통 다크 테마 스타일"""
    pw.setTitle(title, color='#a6adc8', size='12px')
    pw.setLabel('bottom', xlabel, color='#6c7086')
    pw.setLabel('left',   ylabel, color='#6c7086')
    pw.showGrid(x=True, y=True, alpha=0.15)
    for axis in ('bottom', 'left'):
        pw.getAxis(axis).setTextPen(pg.mkPen('#a6adc8'))
        pw.getAxis(axis).setPen(pg.mkPen('#45475a'))


def _reset_legend(pw):
    """plot 초기화 후 새 LegendItem 반환"""
    pi = pw.getPlotItem()
    pi.clear()
    if pi.legend is not None:
        try:
            if pi.legend.scene() is not None:
                pi.legend.scene().removeItem(pi.legend)
        except Exception:
            pass
        pi.legend = None
    return pi.addLegend(
        labelTextColor='#a6adc8',
        brush=pg.mkBrush('#1e1e2e'),
        pen=pg.mkPen('#313244'),
        offset=(10, 10),
    )


def _make_pw(title, xlabel, ylabel):
    pw = pg.PlotWidget(background='#1e1e2e')
    _style_pw(pw, title, xlabel, ylabel)
    return pw


class AnalysisWidget(QWidget):
    assignment_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fit_records = []
        self._co_fit_records = []
        self._potentials = {}
        self._stark_results = []
        self._co_stark_results = []
        self._sio_ref_area = None
        self._sio_areas = {}
        self._focus_filename = None
        self._focus_items = []
        self._compare_records = []
        self._build_ui()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("center_tabs")
        content_layout.addWidget(self._tabs)
        root.addWidget(self._content, 1)

        # ── OH 서브탭 ──────────────────────────────────────────
        self._oh_widget = QWidget()
        oh_grid = QGridLayout(self._oh_widget)
        oh_grid.setContentsMargins(6, 6, 6, 6)
        oh_grid.setSpacing(8)

        self.area_pw = _make_pw('Area Fraction vs Potential',
                                'Potential (V)', 'Area Fraction (%)')
        self.stark_pw = _make_pw('Stark Tuning  (Peak Center vs Potential)',
                                 'Potential (V)', 'Peak Center (cm⁻¹)')
        self.oh_total_pw = _make_pw('OH Total Area vs Potential',
                                    'Potential (V)', 'OH Total Area')
        self.oh_norm_pw = _make_pw('Normalized OH  (OH / Si-O) vs Potential',
                                   'Potential (V)', 'OH / Si-O')

        oh_grid.addWidget(self.area_pw,    0, 0)
        oh_grid.addWidget(self.stark_pw,   0, 1)
        oh_grid.addWidget(self.oh_total_pw, 1, 0)
        oh_grid.addWidget(self.oh_norm_pw,  1, 1)
        oh_grid.setRowStretch(0, 1)
        oh_grid.setRowStretch(1, 1)

        self._tabs.addTab(self._oh_widget, "  OH  ")

        # ── CO 서브탭 ──────────────────────────────────────────
        self._co_widget = QWidget()
        co_grid = QGridLayout(self._co_widget)
        co_grid.setContentsMargins(6, 6, 6, 6)
        co_grid.setSpacing(8)

        self.co_l_pw    = _make_pw('CO Linear Area vs Potential',
                                   'Potential (V)', 'CO_L Area')
        self.co_b_pw    = _make_pw('CO Bridge Area vs Potential',
                                   'Potential (V)', 'CO_B Area')
        self.co_ratio_pw = _make_pw('CO_L / CO_B Ratio vs Potential',
                                    'Potential (V)', 'CO_L / CO_B')
        self.co_stark_pw = _make_pw('CO Stark Tuning  (Peak Center vs Potential)',
                                    'Potential (V)', 'Peak Center (cm⁻¹)')

        co_grid.addWidget(self.co_l_pw,     0, 0)
        co_grid.addWidget(self.co_b_pw,     0, 1)
        co_grid.addWidget(self.co_ratio_pw, 1, 0)
        co_grid.addWidget(self.co_stark_pw, 1, 1)
        co_grid.setRowStretch(0, 1)
        co_grid.setRowStretch(1, 1)

        self._tabs.addTab(self._co_widget, "  CO  ")

        # ── Compare 서브탭 ─────────────────────────────────────
        self._compare_widget = QWidget()
        compare_layout = QVBoxLayout(self._compare_widget)
        compare_layout.setContentsMargins(6, 6, 6, 6)
        compare_layout.setSpacing(8)

        compare_controls = QHBoxLayout()
        compare_controls.setContentsMargins(0, 0, 0, 0)
        compare_controls.setSpacing(6)
        metric_label = QLabel("Metric")
        metric_label.setStyleSheet("color: #a6adc8;")
        self.compare_metric_combo = QComboBox()
        self.compare_metric_combo.setObjectName("analysis_compare_metric_combo")
        for label, _indices in COMPARE_METRICS:
            self.compare_metric_combo.addItem(label, label)
        default_index = self.compare_metric_combo.findData(DEFAULT_COMPARE_METRIC)
        if default_index >= 0:
            self.compare_metric_combo.setCurrentIndex(default_index)
        self.compare_metric_combo.currentIndexChanged.connect(
            self._on_compare_metric_changed
        )

        compare_controls.addWidget(metric_label)
        compare_controls.addWidget(self.compare_metric_combo)
        compare_controls.addStretch(1)
        compare_layout.addLayout(compare_controls)

        self.compare_pw = _make_pw(
            f'{DEFAULT_COMPARE_METRIC} Area Fraction by Sample',
            'Potential (V)',
            'Area Fraction (%)',
        )
        compare_layout.addWidget(self.compare_pw, 1)

        self._tabs.addTab(self._compare_widget, "  Compare  ")

        # 초기 안내 레이블
        self._overlay = QWidget(self._content)
        ol = QVBoxLayout(self._overlay)
        hint = QLabel(
            "오른쪽 패널에서 Calculate Stark Slopes 를 실행하면\n"
            "분석 그래프가 여기에 표시됩니다."
        )
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: #45475a; font-size: 13px;")
        ol.addWidget(hint)
        self._overlay.setVisible(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._overlay.setGeometry(self._content.rect())

    def get_current_subtab(self) -> str:
        widget = self._tabs.currentWidget()
        if widget is getattr(self, '_co_widget', None):
            return 'CO'
        if widget is getattr(self, '_compare_widget', None):
            return 'Compare'
        index = self._tabs.currentIndex()
        if index == 1:
            return 'CO'
        if index == 2:
            return 'Compare'
        return 'OH'

    def set_current_subtab(self, subtab: str):
        index = {'OH': 0, 'CO': 1, 'Compare': 2}.get(subtab, 0)
        self._tabs.setCurrentIndex(index)

    def _on_compare_metric_changed(self, _index: int):
        self._redraw_compare_plot()

    def set_potential_assignments(self, spectra_names: list, potentials: dict, current_filename: Optional[str] = None):
        self._potentials = dict(potentials or {})
        self.set_focus_spectrum(current_filename)

    def set_focus_spectrum(self, filename: Optional[str]):
        self._focus_filename = filename
        self._apply_focus_highlight()

    def _clear_focus_items(self):
        for plot_widget, item in self._focus_items:
            try:
                plot_widget.removeItem(item)
            except Exception:
                pass
        self._focus_items.clear()

    def _add_focus_line(self, pw, x_value: float):
        color = pg.mkColor('#89b4fa')
        color.setAlpha(95)
        line = pg.InfiniteLine(
            pos=x_value,
            angle=90,
            movable=False,
            pen=pg.mkPen(color, width=1.2, style=Qt.DashLine),
        )
        line.setZValue(5)
        pw.addItem(line)
        self._focus_items.append((pw, line))

    def _add_focus_marker(self, pw, x_value: float, y_value: float, color: str):
        halo_color = pg.mkColor(color)
        halo_color.setAlpha(70)
        halo = pg.ScatterPlotItem(
            [x_value], [y_value],
            size=20,
            brush=pg.mkBrush(halo_color),
            pen=pg.mkPen(halo_color, width=1),
        )
        halo.setZValue(7)

        point = pg.ScatterPlotItem(
            [x_value], [y_value],
            size=12,
            brush=pg.mkBrush(color),
            pen=pg.mkPen('#f5f5f5', width=1.8),
        )
        point.setZValue(8)

        pw.addItem(halo)
        pw.addItem(point)
        self._focus_items.append((pw, halo))
        self._focus_items.append((pw, point))

    def _apply_focus_highlight(self):
        self._clear_focus_items()
        if not self._focus_filename:
            return
        self._apply_oh_focus_highlight()
        self._apply_co_focus_highlight()

    def _apply_oh_focus_highlight(self):
        potential = self._potentials.get(self._focus_filename)
        if potential is None:
            return

        focus_record = next(
            (record for record in self._fit_records if record.get('filename') == self._focus_filename),
            None,
        )
        if not focus_record:
            return

        fit_result = focus_record.get('fit_result')
        peaks = list(getattr(fit_result, 'peaks', []) or [])
        if not peaks:
            return

        for pw in (self.area_pw, self.stark_pw, self.oh_total_pw):
            self._add_focus_line(pw, potential)
        sio_area = self._sio_areas.get(self._focus_filename, self._sio_ref_area)
        if sio_area:
            self._add_focus_line(self.oh_norm_pw, potential)

        total_area = 0.0
        for peak in peaks:
            color = PEAK_COLORS[peak.index % len(PEAK_COLORS)]
            self._add_focus_marker(self.area_pw, potential, peak.area_fraction, color)
            self._add_focus_marker(self.stark_pw, potential, peak.center, color)
            total_area += peak.area

        self._add_focus_marker(self.oh_total_pw, potential, total_area, '#89b4fa')
        if sio_area:
            self._add_focus_marker(self.oh_norm_pw, potential, total_area / sio_area, '#a6e3a1')

    def _apply_co_focus_highlight(self):
        potential = self._potentials.get(self._focus_filename)
        if potential is None:
            return

        focus_record = next(
            (record for record in self._co_fit_records if record.get('filename') == self._focus_filename),
            None,
        )
        if not focus_record:
            return

        co_l = focus_record.get('CO_L')
        co_b = focus_record.get('CO_B')
        l_peak = co_l.peaks[0] if (co_l and co_l.success and co_l.peaks) else None
        b_peak = co_b.peaks[0] if (co_b and co_b.success and co_b.peaks) else None
        l_area = l_peak.area if l_peak is not None else None
        b_area = b_peak.area if b_peak is not None else None

        for pw in (self.co_l_pw, self.co_b_pw, self.co_ratio_pw, self.co_stark_pw):
            self._add_focus_line(pw, potential)

        if l_area is not None:
            self._add_focus_marker(self.co_l_pw, potential, l_area, '#89b4fa')
        if b_area is not None:
            self._add_focus_marker(self.co_b_pw, potential, b_area, '#fab387')
        if l_area is not None and b_area is not None and b_area > 0:
            self._add_focus_marker(self.co_ratio_pw, potential, l_area / b_area, '#cba6f7')
        if l_peak is not None:
            self._add_focus_marker(self.co_stark_pw, potential, l_peak.center, '#89b4fa')
        if b_peak is not None:
            self._add_focus_marker(self.co_stark_pw, potential, b_peak.center, '#fab387')

    # ── OH 업데이트 ────────────────────────────────────────────

    def update_plots(self, fit_records: list,
                     potentials: dict,
                     stark_results: list):
        """
        fit_records  : [{'filename': str, 'fit_result': FitResult}]
        potentials   : {filename: float}
        stark_results: [StarkResult]
        """
        self._fit_records = list(fit_records)
        self._potentials = dict(potentials or {})
        self._stark_results = list(stark_results)
        self._overlay.setVisible(False)

        # ── Area Fraction vs Potential ──────────────────────────
        _reset_legend(self.area_pw)

        area_data = defaultdict(lambda: {'V': [], 'area': []})
        for record in fit_records:
            V = potentials.get(record['filename'])
            if V is None:
                continue
            for peak in record['fit_result'].peaks:
                area_data[peak.index]['V'].append(V)
                area_data[peak.index]['area'].append(peak.area_fraction)

        for peak_idx in sorted(area_data.keys()):
            d     = area_data[peak_idx]
            color = PEAK_COLORS[peak_idx % len(PEAK_COLORS)]
            xs    = np.array(d['V'])
            ys    = np.array(d['area'])

            self.area_pw.plot(
                xs, ys,
                pen=None, symbol='o', symbolSize=10,
                symbolBrush=pg.mkBrush(color),
                symbolPen=pg.mkPen('#1e1e2e', width=1),
                name=f'P{peak_idx + 1}',
            )
            if len(xs) >= 2:
                _connect_points(self.area_pw, xs, ys, color)

        # ── Stark Tuning ────────────────────────────────────────
        _reset_legend(self.stark_pw)

        for r in stark_results:
            color = PEAK_COLORS[r.peak_index % len(PEAK_COLORS)]
            xs    = np.array(r.potentials)
            ys    = np.array(r.centers)
            label = f'P{r.peak_index + 1}:  {r.slope:.1f} cm⁻¹/V  R²={r.r_squared:.3f}'

            self.stark_pw.plot(
                xs, ys,
                pen=None, symbol='o', symbolSize=10,
                symbolBrush=pg.mkBrush(color),
                symbolPen=pg.mkPen('#1e1e2e', width=1),
                name=label,
            )
            x_fit = np.linspace(xs.min(), xs.max(), 100)
            self.stark_pw.plot(
                x_fit, r.slope * x_fit + r.intercept,
                pen=pg.mkPen(color, width=2),
            )

        # OH total area 업데이트 시도
        self._update_oh_total(fit_records, potentials)
        self._apply_focus_highlight()

    def update_oh_normalized(self, fit_records: list,
                             sio_ref_area,
                             potentials: dict):
        """
        OH total area vs V + OH/SiO normalized vs V 업데이트.
        sio_ref_area: 단일 float 또는 {filename: area} dict
        """
        self._fit_records = list(fit_records)
        self._potentials = dict(potentials or {})
        if isinstance(sio_ref_area, dict):
            self._sio_areas = dict(sio_ref_area)
            self._sio_ref_area = None
        else:
            self._sio_ref_area = sio_ref_area
            self._sio_areas = {}
        self._update_oh_total(fit_records, potentials)

        if (not self._sio_areas) and (not self._sio_ref_area or self._sio_ref_area == 0):
            self._apply_focus_highlight()
            return

        norm_data = {'V': [], 'ratio': []}
        for record in fit_records:
            V = potentials.get(record['filename'])
            if V is None:
                continue
            area = self._sio_areas.get(record['filename'], self._sio_ref_area)
            if not area or area == 0:
                continue
            total_oh = sum(p.area for p in record['fit_result'].peaks)
            norm_data['V'].append(V)
            norm_data['ratio'].append(total_oh / area)

        _reset_legend(self.oh_norm_pw)
        if norm_data['V']:
            xs = np.array(norm_data['V'])
            ys = np.array(norm_data['ratio'])
            self.oh_norm_pw.plot(
                xs, ys,
                pen=None, symbol='o', symbolSize=10,
                symbolBrush=pg.mkBrush('#a6e3a1'),
                symbolPen=pg.mkPen('#1e1e2e', width=1),
                name='OH / Si-O',
            )
            if len(xs) >= 2:
                _connect_points(self.oh_norm_pw, xs, ys, '#a6e3a1')
        self._apply_focus_highlight()

    # ── Sample comparison 업데이트 ─────────────────────────────

    def update_compare_plot(self, compare_records: list):
        """
        compare_records:
        [{
            'session_label': str,
            'filename': str,
            'potential': float,
            'area_fractions': {peak_index: area_fraction_percent}
        }]
        """
        self._compare_records = list(compare_records or [])
        if self._compare_records:
            self._overlay.setVisible(False)
        self._redraw_compare_plot()

    def _selected_compare_metric(self) -> str:
        key = self.compare_metric_combo.currentData()
        return key or DEFAULT_COMPARE_METRIC

    def _compare_metric_value(self, record: dict, peak_indices: tuple[int, ...]):
        fractions = record.get('area_fractions') or {}
        values = []
        for idx in peak_indices:
            if idx not in fractions:
                return None
            values.append(float(fractions[idx]))
        return sum(values)

    def _redraw_compare_plot(self):
        metric = self._selected_compare_metric()
        peak_indices = COMPARE_METRIC_MAP.get(metric, ())
        self.compare_pw.setTitle(
            f'{metric} Area Fraction by Sample',
            color='#a6adc8',
            size='12px',
        )
        legend = _reset_legend(self.compare_pw)

        grouped = defaultdict(lambda: {'V': [], 'area': [], 'filenames': []})
        for record in self._compare_records:
            potential = record.get('potential')
            if potential is None:
                continue
            value = self._compare_metric_value(record, peak_indices)
            if value is None:
                continue
            label = record.get('session_label') or 'Loose Files'
            grouped[label]['V'].append(float(potential))
            grouped[label]['area'].append(float(value))
            grouped[label]['filenames'].append(record.get('filename', ''))

        if not grouped:
            try:
                if legend.scene() is not None:
                    legend.scene().removeItem(legend)
            except Exception:
                pass
            return

        for session_idx, session_label in enumerate(grouped.keys()):
            color = SESSION_COLORS[session_idx % len(SESSION_COLORS)]
            values = grouped[session_label]
            xs = np.array(values['V'])
            ys = np.array(values['area'])
            order = np.argsort(xs)
            xs = xs[order]
            ys = ys[order]

            self.compare_pw.plot(
                xs, ys,
                pen=None,
                symbol='o',
                symbolSize=10,
                symbolBrush=pg.mkBrush(color),
                symbolPen=pg.mkPen('#1e1e2e', width=1),
                name=session_label,
            )
            if len(xs) >= 2:
                self.compare_pw.plot(
                    xs, ys,
                    pen=pg.mkPen(color, width=1.8),
                )

    def _update_oh_total(self, fit_records: list, potentials: dict):
        """OH total area vs Potential 플롯"""
        total_data = {'V': [], 'area': []}
        for record in fit_records:
            V = potentials.get(record['filename'])
            if V is None:
                continue
            total = sum(p.area for p in record['fit_result'].peaks)
            total_data['V'].append(V)
            total_data['area'].append(total)

        _reset_legend(self.oh_total_pw)
        if total_data['V']:
            xs = np.array(total_data['V'])
            ys = np.array(total_data['area'])
            self.oh_total_pw.plot(
                xs, ys,
                pen=None, symbol='o', symbolSize=10,
                symbolBrush=pg.mkBrush('#89b4fa'),
                symbolPen=pg.mkPen('#1e1e2e', width=1),
                name='OH total',
            )
            if len(xs) >= 2:
                _connect_points(self.oh_total_pw, xs, ys, '#89b4fa')

    # ── CO 업데이트 ────────────────────────────────────────────

    def update_co_plots(self, co_fit_records: list, potentials: dict,
                        co_stark_results: list | None = None):
        """
        co_fit_records: [{'filename': str, 'CO_L': FitResult, 'CO_B': FitResult}]
        potentials    : {filename: float}
        co_stark_results: [NamedStarkResult] 또는 None
        """
        self._co_fit_records = list(co_fit_records)
        self._potentials = dict(potentials or {})
        self._overlay.setVisible(False)
        self._tabs.setCurrentIndex(1)  # CO 탭으로 전환

        co_l_data  = {'V': [], 'area': []}
        co_b_data  = {'V': [], 'area': []}
        ratio_data = {'V': [], 'ratio': []}

        for record in co_fit_records:
            V = potentials.get(record['filename'])
            if V is None:
                continue
            co_l = record.get('CO_L')
            co_b = record.get('CO_B')
            l_area = co_l.peaks[0].area if (co_l and co_l.success and co_l.peaks) else None
            b_area = co_b.peaks[0].area if (co_b and co_b.success and co_b.peaks) else None

            if l_area is not None:
                co_l_data['V'].append(V)
                co_l_data['area'].append(l_area)
            if b_area is not None:
                co_b_data['V'].append(V)
                co_b_data['area'].append(b_area)
            if l_area is not None and b_area is not None and b_area > 0:
                ratio_data['V'].append(V)
                ratio_data['ratio'].append(l_area / b_area)

        def _plot_series(pw, xs_list, ys_list, color, label):
            _reset_legend(pw)
            if not xs_list:
                return
            xs = np.array(xs_list)
            ys = np.array(ys_list)
            pw.plot(xs, ys, pen=None, symbol='o', symbolSize=10,
                    symbolBrush=pg.mkBrush(color),
                    symbolPen=pg.mkPen('#1e1e2e', width=1), name=label)
            if len(xs) >= 2:
                _connect_points(pw, xs, ys, color)

        _plot_series(self.co_l_pw,    co_l_data['V'],  co_l_data['area'],
                     '#89b4fa', 'CO_L')
        _plot_series(self.co_b_pw,    co_b_data['V'],  co_b_data['area'],
                     '#fab387', 'CO_B')
        _plot_series(self.co_ratio_pw, ratio_data['V'], ratio_data['ratio'],
                     '#cba6f7', 'CO_L / CO_B')
        self.update_co_stark_results(co_stark_results or [])
        self._apply_focus_highlight()

    def update_co_stark_results(self, co_stark_results: list):
        """CO_L / CO_B center vs potential Stark tuning 플롯"""
        self._co_stark_results = list(co_stark_results or [])
        _reset_legend(self.co_stark_pw)

        series_colors = {
            'CO_L': '#89b4fa',
            'CO_B': '#fab387',
        }
        for r in self._co_stark_results:
            color = series_colors.get(r.series_name, '#cba6f7')
            xs = np.array(r.potentials)
            ys = np.array(r.centers)
            if len(xs) == 0:
                continue
            label = f'{r.series_name}:  {r.slope:.1f} cm⁻¹/V  R²={r.r_squared:.3f}'

            self.co_stark_pw.plot(
                xs, ys,
                pen=None, symbol='o', symbolSize=10,
                symbolBrush=pg.mkBrush(color),
                symbolPen=pg.mkPen('#1e1e2e', width=1),
                name=label,
            )
            if len(xs) >= 2:
                x_fit = np.linspace(xs.min(), xs.max(), 100)
                self.co_stark_pw.plot(
                    x_fit, r.slope * x_fit + r.intercept,
                    pen=pg.mkPen(color, width=2),
                )
        self._apply_focus_highlight()
