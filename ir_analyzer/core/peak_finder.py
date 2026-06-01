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
    # 스무딩 후 2차 미분
    window = smooth_window
    if window % 2 == 0:
        window += 1
    if window >= len(absorbance):
        window = len(absorbance) // 3
        if window % 2 == 0:
            window -= 1

    second_deriv = savgol_filter(absorbance, window_length=window,
                                  polyorder=smooth_polyorder, deriv=2)

    # 2차 미분의 극솟값 = 피크 위치 (음의 방향 피크 찾기)
    neg_deriv = -second_deriv
    peaks_idx, properties = find_peaks(
        neg_deriv,
        distance=len(wavenumber) // (n_peaks * 2),  # 최소 간격
        prominence=np.max(neg_deriv) * 0.05          # 최소 prominence
    )

    if len(peaks_idx) == 0:
        # 2차 미분 실패 시 단순 분할
        return _fallback_peaks(wavenumber, absorbance, n_peaks)

    # prominence 기준 상위 n_peaks 선택
    prominences = properties.get('prominences', neg_deriv[peaks_idx])
    if len(peaks_idx) > n_peaks:
        top_idx = np.argsort(prominences)[-n_peaks:]
        peaks_idx = peaks_idx[top_idx]

    # wavenumber 순서로 정렬
    peaks_idx = np.sort(peaks_idx)

    # PeakGuess 생성
    guesses = []
    total_span = abs(wavenumber[-1] - wavenumber[0])
    default_sigma = total_span / (n_peaks * 4)  # 초기 폭 추정

    for idx in peaks_idx:
        center = wavenumber[idx]
        amplitude = absorbance[idx]
        guesses.append(PeakGuess(
            center=center,
            amplitude=max(amplitude, 0.01),
            sigma=default_sigma,
            index=int(idx)
        ))

    # n_peaks보다 적게 찾힌 경우 보완
    while len(guesses) < n_peaks:
        guesses = _fill_missing_peaks(wavenumber, absorbance, guesses, n_peaks)
        break

    return guesses[:n_peaks]


def _fallback_peaks(wavenumber: np.ndarray, absorbance: np.ndarray,
                    n_peaks: int) -> list[PeakGuess]:
    """2차 미분 실패 시 균등 분할로 초기값 생성"""
    indices = np.linspace(len(wavenumber) // (n_peaks + 1),
                          len(wavenumber) - len(wavenumber) // (n_peaks + 1),
                          n_peaks, dtype=int)
    guesses = []
    span = abs(wavenumber[-1] - wavenumber[0])
    for idx in indices:
        guesses.append(PeakGuess(
            center=wavenumber[idx],
            amplitude=absorbance[idx],
            sigma=span / (n_peaks * 4),
            index=int(idx)
        ))
    return guesses


def _fill_missing_peaks(wavenumber, absorbance, existing, n_peaks):
    """피크가 부족할 때 빈 영역에 추가"""
    existing_centers = [g.center for g in existing]
    span = abs(wavenumber[-1] - wavenumber[0])

    # 균등 간격으로 추가 후보 생성
    candidates = np.linspace(wavenumber[0], wavenumber[-1], n_peaks * 3)
    for c in candidates:
        if all(abs(c - ec) > span / (n_peaks * 2) for ec in existing_centers):
            idx = np.argmin(np.abs(wavenumber - c))
            existing.append(PeakGuess(
                center=c,
                amplitude=absorbance[idx],
                sigma=span / (n_peaks * 4),
                index=int(idx)
            ))
            existing_centers.append(c)
        if len(existing) >= n_peaks:
            break

    return sorted(existing, key=lambda g: g.center)
