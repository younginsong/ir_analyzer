"""
stark_analysis.py - Stark Tuning Slope 분석
파일명에서 전위값 파싱 후 peak center vs potential 선형회귀
"""

from __future__ import annotations

import re
import numpy as np
from scipy.stats import linregress
from dataclasses import dataclass, field


@dataclass
class StarkResult:
    peak_index: int
    slope: float         # cm⁻¹/V
    intercept: float
    r_squared: float
    n_points: int
    potentials: list = field(default_factory=list)
    centers: list = field(default_factory=list)


@dataclass
class NamedStarkResult:
    series_name: str
    slope: float
    intercept: float
    r_squared: float
    n_points: int
    potentials: list = field(default_factory=list)
    centers: list = field(default_factory=list)


def parse_potential_from_filename(filename: str) -> float | None:
    """
    파일명에서 전위값 파싱.
    패턴: "250311_PSBI 1.8_.26_corrected.txt" → 0.26
    `_.<digits>_` 패턴에서 0.<digits> 형태로 변환.
    파싱 실패 시 None 반환.
    """
    match = re.search(r'_\.(\d+)_', filename)
    if match:
        try:
            return float('0.' + match.group(1))
        except ValueError:
            return None
    return None


def calculate_stark_slopes(fit_records: list,
                           potentials: dict = None) -> list[StarkResult]:
    """
    fit_records: [{'filename': str, 'fit_result': FitResult}, ...]
    potentials:  {filename: potential_V} 오버라이드 딕셔너리.
                 None 이면 파일명 파싱으로 fallback.
    각 피크 인덱스별 potential vs center 선형회귀 수행.
    데이터 포인트가 2개 미만인 피크는 제외.
    """
    from collections import defaultdict
    peak_data = defaultdict(lambda: {'potentials': [], 'centers': []})

    for record in fit_records:
        if potentials is not None:
            potential = potentials.get(record['filename'])
        else:
            potential = parse_potential_from_filename(record['filename'])
        if potential is None:
            continue
        for peak in record['fit_result'].peaks:
            peak_data[peak.index]['potentials'].append(potential)
            peak_data[peak.index]['centers'].append(peak.center)

    results = []
    for peak_idx in sorted(peak_data.keys()):
        data = peak_data[peak_idx]
        potentials = data['potentials']
        centers = data['centers']
        if len(potentials) < 2:
            continue
        slope, intercept, r, p, se = linregress(potentials, centers)
        results.append(StarkResult(
            peak_index=peak_idx,
            slope=slope,
            intercept=intercept,
            r_squared=r ** 2,
            n_points=len(potentials),
            potentials=list(potentials),
            centers=list(centers),
        ))
    return results


def calculate_co_stark_slopes(co_fit_records: list,
                              potentials: dict = None) -> list[NamedStarkResult]:
    """
    co_fit_records: [{'filename': str, 'CO_L': FitResult, 'CO_B': FitResult}, ...]
    potentials: {filename: potential_V} 오버라이드 딕셔너리.
    CO_L / CO_B 각각의 center vs potential 선형회귀 수행.
    """
    series_data = {
        'CO_L': {'potentials': [], 'centers': []},
        'CO_B': {'potentials': [], 'centers': []},
    }

    for record in co_fit_records:
        if potentials is not None:
            potential = potentials.get(record['filename'])
        else:
            potential = parse_potential_from_filename(record['filename'])
        if potential is None:
            continue

        for series_name in ('CO_L', 'CO_B'):
            fit_result = record.get(series_name)
            if fit_result and fit_result.success and fit_result.peaks:
                series_data[series_name]['potentials'].append(potential)
                series_data[series_name]['centers'].append(fit_result.peaks[0].center)

    results = []
    for series_name in ('CO_L', 'CO_B'):
        pots = series_data[series_name]['potentials']
        ctrs = series_data[series_name]['centers']
        if len(pots) < 2:
            continue
        slope, intercept, r, p, se = linregress(pots, ctrs)
        results.append(NamedStarkResult(
            series_name=series_name,
            slope=slope,
            intercept=intercept,
            r_squared=r ** 2,
            n_points=len(pots),
            potentials=list(pots),
            centers=list(ctrs),
        ))
    return results
