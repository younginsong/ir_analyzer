"""
peak_finder.py - 2차 미분 기반 자동 피크 감지
OH stretching처럼 broad하고 겹친 피크에 최적화
"""

import numpy as np
from scipy.signal import savgol_filter, find_peaks
from dataclasses import dataclass


@dataclass
class PeakGuess:
    """자동 감지된 피크 초기값"""
    center: float       # cm⁻¹
    amplitude: float    # 피크 높이
    sigma: float        # 초기 폭 추정값 (Gaussian sigma)
    index: int          # 배열 인덱스
    shape: str = 'gaussian'  # 피크 형태 (gaussian, lorentzian, voigt)


def find_peaks_second_derivative(wavenumber: np.ndarray,
                                  absorbance: np.ndarray,
                                  n_peaks: int = 4,
                                  smooth_window: int = 31,
                                  smooth_polyorder: int = 4) -> list[PeakGuess]:
    """
    2차 미분의 극솟값으로 피크 위치 자동 감지.

    OH stretching (~3000–3700 cm⁻¹) 같은 broad 피크에 효과적.
    n_peaks: 찾을 피크 수 (기본 4)
    """
    wavenumber = np.asarray(wavenumber, dtype=float)
    absorbance = np.asarray(absorbance, dtype=float)
    if len(wavenumber) == 0 or len(absorbance) == 0:
        return []
    if len(wavenumber) != len(absorbance):
        raise ValueError("wavenumber and absorbance must have the same length")
    if n_peaks <= 0:
        return []

    # 스무딩 후 2차 미분. 데이터 포인트가 적을 때도 Savitzky-Golay 조건을 맞춘다.
    window = _valid_savgol_window(len(absorbance), smooth_window, smooth_polyorder)
    if window is None:
        return _fallback_peaks(wavenumber, absorbance, n_peaks)

    y_smooth = savgol_filter(absorbance, window_length=window,
                             polyorder=smooth_polyorder)
    second_deriv = savgol_filter(absorbance, window_length=window,
                                 polyorder=smooth_polyorder, deriv=2)

    # 2차 미분의 극솟값 = 피크 위치 (음의 방향 피크 찾기)
    neg_deriv = -second_deriv
    max_neg = float(np.nanmax(neg_deriv)) if len(neg_deriv) else 0.0
    if not np.isfinite(max_neg) or max_neg <= 0:
        return _fallback_peaks(wavenumber, absorbance, n_peaks)

    peaks_idx, properties = find_peaks(
        neg_deriv,
        distance=max(1, len(wavenumber) // (n_peaks * 2)),  # 최소 간격
        prominence=max_neg * 0.05                       # 최소 prominence
    )

    if len(peaks_idx) == 0:
        return _fallback_peaks(wavenumber, absorbance, n_peaks)

    # 2차 미분 후보를 실제 corrected spectrum의 local maximum으로 보정한다.
    prominences = properties.get('prominences', neg_deriv[peaks_idx])
    search_radius = max(2, len(wavenumber) // (n_peaks * 6))
    candidates = []
    used_indices = set()
    for idx, prominence in zip(peaks_idx, prominences):
        refined_idx = _local_max_index(y_smooth, int(idx), search_radius)
        if refined_idx in used_indices:
            continue
        used_indices.add(refined_idx)
        candidates.append((refined_idx, float(prominence), float(absorbance[refined_idx])))

    if not candidates:
        return _fallback_peaks(wavenumber, absorbance, n_peaks)

    max_abs = float(np.nanmax(absorbance))
    min_candidate_height = max_abs * 0.03 if np.isfinite(max_abs) and max_abs > 0 else 0.0
    candidates = [
        item for item in candidates
        if item[2] >= min_candidate_height
    ]
    if not candidates:
        return _fallback_peaks(wavenumber, absorbance, n_peaks)

    # curvature prominence와 실제 intensity를 같이 고려해 상위 후보를 고른다.
    candidates.sort(key=lambda item: (item[2], item[1]), reverse=True)
    selected_idx = [idx for idx, _, _ in candidates[:n_peaks]]

    # n_peaks보다 적게 찾힌 경우 보완
    if len(selected_idx) < n_peaks:
        selected_idx = _fill_missing_peak_indices(
            wavenumber, absorbance, y_smooth, selected_idx, n_peaks)

    guesses = _build_guesses_from_indices(
        wavenumber, absorbance, sorted(selected_idx), n_peaks)
    _scale_guesses_to_envelope(wavenumber, absorbance, guesses)

    return guesses[:n_peaks]


def _valid_savgol_window(length: int, requested: int,
                         polyorder: int) -> int | None:
    """Return an odd Savitzky-Golay window that is valid for the data length."""
    if length <= polyorder + 1:
        return None

    window = min(requested, length if length % 2 == 1 else length - 1)
    if window % 2 == 0:
        window -= 1
    min_window = polyorder + 2
    if min_window % 2 == 0:
        min_window += 1
    if window < min_window:
        window = min_window
    return window if window <= length else None


def _local_max_index(y: np.ndarray, center_idx: int, radius: int) -> int:
    """Find the nearest local maximum around a derivative-based candidate."""
    lo = max(0, center_idx - radius)
    hi = min(len(y), center_idx + radius + 1)
    if lo >= hi:
        return int(center_idx)
    return int(lo + np.argmax(y[lo:hi]))


def _estimate_sigma(wavenumber: np.ndarray, absorbance: np.ndarray,
                    idx: int, n_peaks: int) -> float:
    """Estimate Gaussian sigma from local FWHM of the corrected spectrum."""
    span = abs(float(wavenumber[-1] - wavenumber[0]))
    default_sigma = span / max(n_peaks * 6, 1)
    amplitude = float(absorbance[idx])
    if amplitude <= 0 or not np.isfinite(amplitude):
        return max(default_sigma, 1.0)

    half_height = amplitude * 0.5
    left = idx
    while left > 0 and absorbance[left] > half_height:
        left -= 1
    right = idx
    while right < len(absorbance) - 1 and absorbance[right] > half_height:
        right += 1

    fwhm = abs(float(wavenumber[right] - wavenumber[left]))
    if not np.isfinite(fwhm) or fwhm <= 0:
        return max(default_sigma, 1.0)

    sigma = fwhm / 2.3548
    min_sigma = max(span / max(n_peaks * 40, 1), 1.0)
    max_sigma = max(span / max(n_peaks * 3, 1), min_sigma)
    return float(np.clip(sigma, min_sigma, max_sigma))


def _build_guesses_from_indices(wavenumber: np.ndarray, absorbance: np.ndarray,
                                indices: list[int], n_peaks: int) -> list[PeakGuess]:
    guesses = []
    for idx in indices:
        amplitude = max(float(absorbance[idx]), 1e-6)
        guesses.append(PeakGuess(
            center=float(wavenumber[idx]),
            amplitude=amplitude,
            sigma=_estimate_sigma(wavenumber, absorbance, int(idx), n_peaks),
            index=int(idx)
        ))
    return guesses


def _scale_guesses_to_envelope(wavenumber: np.ndarray, absorbance: np.ndarray,
                               guesses: list[PeakGuess]):
    """Keep the preview sum of initial guesses on the spectrum's intensity scale."""
    if not guesses:
        return
    max_abs = float(np.nanmax(absorbance)) if len(absorbance) else 0.0
    if not np.isfinite(max_abs) or max_abs <= 0:
        return

    total = np.zeros_like(absorbance, dtype=float)
    for guess in guesses:
        sigma = max(abs(float(guess.sigma)), 1e-6)
        total += guess.amplitude * np.exp(
            -0.5 * ((wavenumber - guess.center) / sigma) ** 2
        )

    max_total = float(np.nanmax(total)) if len(total) else 0.0
    if not np.isfinite(max_total) or max_total <= 0:
        return
    scale = min(1.0, (max_abs * 0.92) / max_total)
    for guess in guesses:
        guess.amplitude = max(float(guess.amplitude) * scale, 1e-6)


def _fallback_peaks(wavenumber: np.ndarray, absorbance: np.ndarray,
                    n_peaks: int) -> list[PeakGuess]:
    """2차 미분 실패 시 corrected spectrum의 실제 local maxima로 초기값 생성"""
    y = np.asarray(absorbance, dtype=float)
    if len(y) == 0:
        return []
    max_y = float(np.nanmax(y))
    if not np.isfinite(max_y) or max_y <= 0:
        return []
    if len(y) >= 5:
        window = _valid_savgol_window(len(y), min(31, len(y)), 2)
        y_smooth = (
            savgol_filter(y, window_length=window, polyorder=2)
            if window is not None else y
        )
    else:
        y_smooth = y
    indices = _fill_missing_peak_indices(
        wavenumber, absorbance, y_smooth, [], n_peaks)
    guesses = _build_guesses_from_indices(
        wavenumber, absorbance, sorted(indices), n_peaks)
    _scale_guesses_to_envelope(wavenumber, absorbance, guesses)
    return guesses


def _fill_missing_peak_indices(wavenumber, absorbance, y_smooth,
                               existing_indices, n_peaks):
    """피크가 부족할 때 local maxima를 우선하되 요청 개수까지 보완."""
    span = abs(wavenumber[-1] - wavenumber[0])
    min_distance_idx = max(1, len(wavenumber) // max(n_peaks * 3, 1))
    max_y = float(np.nanmax(y_smooth)) if len(y_smooth) else 0.0
    if not np.isfinite(max_y) or max_y <= 0:
        return list(existing_indices)

    peak_indices, _ = find_peaks(
        y_smooth,
        distance=min_distance_idx,
        prominence=max_y * 0.005,
    )
    if len(peak_indices) == 0:
        peak_indices = np.array([], dtype=int)

    selected = list(existing_indices)
    selected_centers = [wavenumber[idx] for idx in selected]

    def try_add(indices, min_distance_wn: float, min_height: float):
        nonlocal selected_centers
        for idx in sorted(indices, key=lambda i: y_smooth[int(i)], reverse=True):
            idx = int(idx)
            if idx in selected:
                continue
            if y_smooth[idx] < min_height:
                continue
            center = wavenumber[idx]
            if min_distance_wn > 0 and any(
                abs(center - c) < min_distance_wn for c in selected_centers
            ):
                continue
            selected.append(int(idx))
            selected_centers.append(center)
            if len(selected) >= n_peaks:
                return

    # 1) 실제 local maxima를 우선하되, 너무 빽빽하면 단계적으로 거리 제한을 완화한다.
    for distance_factor, height_factor in [
        (2.0, 0.03),
        (3.0, 0.01),
        (5.0, 0.003),
        (8.0, 0.0),
    ]:
        if len(selected) >= n_peaks:
            break
        try_add(
            peak_indices,
            span / max(n_peaks * distance_factor, 1),
            max_y * height_factor,
        )

    # 2) local maxima만으로 부족하면 strongest points로 채워 요청한 개수를 지킨다.
    if len(selected) < n_peaks:
        strongest = np.argsort(y_smooth)[::-1]
        for distance_factor in (8.0, 12.0, 0.0):
            if len(selected) >= n_peaks:
                break
            distance = 0.0 if distance_factor == 0.0 else span / max(n_peaks * distance_factor, 1)
            try_add(strongest, distance, 0.0)

    return selected[:n_peaks]
