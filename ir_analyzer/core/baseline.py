"""
baseline.py - 베이스라인 보정
수동 포인트 지정 또는 자동 방식 지원

지원 알고리즘:
  - Manual       : 사용자 클릭 포인트 선형 보간
  - Rubber Band  : Convex Hull 하단 경계
  - ARPLS        : Asymmetrically Reweighted Penalized Least Squares
  - SNIP         : Statistics-sensitive Non-linear Iterative Peak-clipping
  - Linear       : 두 끝점 직선 연결
"""

from __future__ import annotations

import numpy as np
from scipy.interpolate import interp1d
from scipy.signal import savgol_filter


def _smooth_for_baseline(absorbance: np.ndarray) -> np.ndarray:
    n = len(absorbance)
    if n < 5:
        return absorbance.copy()
    window = min(11, n if n % 2 == 1 else n - 1)
    if window < 5:
        return absorbance.copy()
    return savgol_filter(absorbance, window_length=window, polyorder=3)


def _pick_window_min(wavenumber: np.ndarray, absorbance: np.ndarray,
                     wn_min: float, wn_max: float) -> tuple[float, float] | None:
    lo, hi = sorted((wn_min, wn_max))
    mask = (wavenumber >= lo) & (wavenumber <= hi)
    if not np.any(mask):
        return None

    wn_seg = wavenumber[mask]
    ab_seg = absorbance[mask]
    ab_smooth = _smooth_for_baseline(ab_seg)
    idx = int(np.argmin(ab_smooth))
    return float(wn_seg[idx]), float(ab_seg[idx])


def _pick_nearest_point(wavenumber: np.ndarray, absorbance: np.ndarray,
                        target_wn: float) -> tuple[float, float] | None:
    if len(wavenumber) == 0:
        return None
    idx = int(np.argmin(np.abs(wavenumber - target_wn)))
    return float(wavenumber[idx]), float(absorbance[idx])


def _pick_window_anchor_points(wavenumber: np.ndarray, absorbance: np.ndarray,
                               wn_min: float, wn_max: float,
                               n_points: int = 3) -> list[tuple[float, float]]:
    lo, hi = sorted((wn_min, wn_max))
    if n_points <= 0:
        return []

    ab_smooth = _smooth_for_baseline(absorbance)
    edges = np.linspace(lo, hi, n_points + 1)
    points = []

    for i in range(n_points):
        left = edges[i]
        right = edges[i + 1]
        if i == n_points - 1:
            mask = (wavenumber >= left) & (wavenumber <= right)
        else:
            mask = (wavenumber >= left) & (wavenumber < right)
        if not np.any(mask):
            continue

        wn_seg = wavenumber[mask]
        ab_seg = absorbance[mask]
        ab_sm_seg = ab_smooth[mask]
        idx = int(np.argmin(ab_sm_seg))
        points.append((float(wn_seg[idx]), float(ab_seg[idx])))

    dedup = {}
    for wn, ab in points:
        dedup[round(wn, 6)] = (wn, ab)
    return sorted(dedup.values(), key=lambda p: p[0])


def auto_oh_baseline_points(wavenumber: np.ndarray, absorbance: np.ndarray,
                            high_targets: tuple[float, float] = (3990.0, 3900.0),
                            low_center: float = 3000.0,
                            low_half_width: float = 45.0,
                            low_points: int = 3) -> list[tuple[float, float]]:
    """
    OH 영역 anchor point를 자동 선택.
    기본적으로 3990/3900 근처 각 1점 + 3000 근처 3점을 반환한다.
    """
    points = []

    for target in high_targets:
        pt = _pick_nearest_point(wavenumber, absorbance, target)
        if pt is not None:
            points.append(pt)

    points.extend(_pick_window_anchor_points(
        wavenumber, absorbance,
        low_center - low_half_width, low_center + low_half_width,
        n_points=low_points))
    dedup = {}
    for wn, ab in points:
        dedup[round(wn, 6)] = (wn, ab)
    return sorted(dedup.values(), key=lambda p: p[0])


def auto_co_baseline_endpoints(wavenumber: np.ndarray,
                               absorbance: np.ndarray) -> dict[str, tuple[float, float]]:
    """
    CO_L / CO_B 각각의 직선 baseline endpoint를 자동 탐지.
    반환값: {'CO_L': (ep0, ep1), 'CO_B': (ep0, ep1)}
    """
    windows = {
        'CO_L': ((1975.0, 2025.0), (2075.0, 2125.0)),
        'CO_B': ((1645.0, 1695.0), (1880.0, 1915.0)),
    }

    result = {}
    for sub, (w0, w1) in windows.items():
        p0 = _pick_window_min(wavenumber, absorbance, *w0)
        p1 = _pick_window_min(wavenumber, absorbance, *w1)
        if p0 is None or p1 is None:
            result[sub] = (w0[0] + w0[1]) / 2, (w1[0] + w1[1]) / 2
        else:
            result[sub] = (p0[0], p1[0])
    return result


def baseline_from_points(wavenumber: np.ndarray, absorbance: np.ndarray,
                          points: list[tuple[float, float]]) -> np.ndarray:
    """
    사용자가 지정한 (wavenumber, absorbance) 포인트들을 cubic spline 보간하여 베이스라인 생성.
    포인트가 2~3개이면 linear/quadratic으로 자동 강등.
    points: [(wn1, abs1), (wn2, abs2), ...]
    """
    if len(points) < 2:
        return np.zeros_like(absorbance)

    pts = sorted(points, key=lambda x: x[0])
    wn_pts = np.array([p[0] for p in pts])
    ab_pts = np.array([p[1] for p in pts])

    n = len(pts)
    kind = 'cubic' if n >= 4 else ('quadratic' if n == 3 else 'linear')
    f = interp1d(wn_pts, ab_pts, kind=kind, fill_value='extrapolate')
    return f(wavenumber)


def baseline_rubberband(wavenumber: np.ndarray, absorbance: np.ndarray) -> np.ndarray:
    """
    Rubber band (convex hull) 베이스라인.
    FTIR에서 자주 쓰이는 자동 방식.
    """
    from scipy.spatial import ConvexHull

    pts = np.column_stack([wavenumber, absorbance])
    hull = ConvexHull(pts)

    hull_pts = pts[hull.vertices]
    hull_pts = hull_pts[hull_pts[:, 0].argsort()]

    f = interp1d(hull_pts[:, 0], hull_pts[:, 1],
                 kind='linear', fill_value='extrapolate')
    baseline = f(wavenumber)

    baseline = np.minimum(baseline, absorbance)
    return baseline


def baseline_arpls(absorbance: np.ndarray,
                   lam: float = 1e4,
                   ratio: float = 0.05,
                   itermax: int = 100) -> np.ndarray:
    """
    ARPLS (Asymmetrically Reweighted Penalized Least Squares) 베이스라인.
    Baek et al. (2015). Analyst 140, 250.

    lam  : 평활도 파라미터. 클수록 baseline이 부드러워짐 (1e2 ~ 1e8 권장)
    ratio: 수렴 판정 기준 (기본값 0.05)
    """
    from scipy import sparse
    from scipy.sparse.linalg import spsolve

    N = len(absorbance)
    D = sparse.diags([1, -2, 1], [0, 1, 2], shape=(N - 2, N), format='csc')
    H = lam * D.T.dot(D)
    w = np.ones(N)

    for _ in range(itermax):
        W = sparse.diags(w, 0, format='csc')
        z = spsolve(W + H, w * absorbance)
        d = absorbance - z
        dn = d[d < 0]
        if len(dn) == 0:
            break
        m = dn.mean()
        s = dn.std()
        if s < 1e-10:
            break
        w_new = 1.0 / (1.0 + np.exp(2.0 * (d - (2.0 * s - m)) / s))
        if np.linalg.norm(w_new - w) / np.linalg.norm(w) < ratio:
            break
        w = w_new

    return z


def baseline_snip(absorbance: np.ndarray, n_iter: int = 50) -> np.ndarray:
    """
    SNIP (Statistics-sensitive Non-linear Iterative Peak-clipping) 베이스라인.
    Ryan et al. (1988). Nuclear Instruments and Methods in Physics Research.

    n_iter: 반복 횟수. 클수록 baseline이 낮고 넓게 잡힘 (10 ~ 200 권장)
    """
    y = np.log(np.log(np.sqrt(np.maximum(absorbance, 0) + 1) + 1) + 1)
    N = len(y)

    for p in range(1, n_iter + 1):
        if 2 * p >= N:
            break
        y_prev = y.copy()
        y[p:N - p] = np.minimum(
            y_prev[p:N - p],
            (y_prev[:N - 2 * p] + y_prev[2 * p:]) / 2
        )

    return (np.exp(np.exp(y) - 1) - 1) ** 2 - 1


def baseline_linear(wavenumber: np.ndarray, absorbance: np.ndarray) -> np.ndarray:
    """
    두 끝점(첫 번째, 마지막 데이터 포인트)을 직선으로 연결하는 가장 단순한 베이스라인.
    """
    slope = (absorbance[-1] - absorbance[0]) / (wavenumber[-1] - wavenumber[0])
    return absorbance[0] + slope * (wavenumber - wavenumber[0])


def subtract_baseline(absorbance: np.ndarray, baseline: np.ndarray) -> np.ndarray:
    """베이스라인 제거된 스펙트럼 반환"""
    return absorbance - baseline


def smooth_spectrum(absorbance: np.ndarray,
                    window: int = 11, polyorder: int = 3) -> np.ndarray:
    """Savitzky-Golay 스무딩"""
    if window % 2 == 0:
        window += 1
    return savgol_filter(absorbance, window_length=window, polyorder=polyorder)
