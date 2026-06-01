"""
fitter.py - lmfit 기반 피크 피팅 엔진
Gaussian / Lorentzian / Voigt 지원
OH stretching 4-peak deconvolution에 최적화
"""

import numpy as np
from lmfit import Model, Parameters
from lmfit.models import GaussianModel, LorentzianModel, VoigtModel
from dataclasses import dataclass, field
from typing import Literal
from core.peak_finder import PeakGuess

_trapezoid = np.trapezoid if hasattr(np, 'trapezoid') else np.trapz


PeakShape = Literal['gaussian', 'lorentzian', 'voigt']


@dataclass
class PeakResult:
    """개별 피크 피팅 결과"""
    index: int              # 피크 번호 (0-based)
    shape: PeakShape
    center: float           # cm⁻¹
    center_err: float
    amplitude: float
    amplitude_err: float
    sigma: float
    sigma_err: float
    fwhm: float             # cm⁻¹
    area: float             # 적분 면적
    area_fraction: float    # 전체 면적 대비 비율 (%)


@dataclass
class FitResult:
    """전체 피팅 결과"""
    success: bool
    peaks: list[PeakResult]
    fitted_curve: np.ndarray        # 전체 피팅 곡선
    individual_curves: list[np.ndarray]  # 개별 피크 곡선
    residual: np.ndarray
    r_squared: float
    chi_squared: float
    message: str = ""


def build_model(n_peaks: int, shapes: list[PeakShape] = None):
    """n개의 피크 복합 모델 생성"""
    model_map = {
        'gaussian': GaussianModel,
        'lorentzian': LorentzianModel,
        'voigt': VoigtModel,
    }

    if shapes is None:
        shapes = ['gaussian'] * n_peaks

    composite = None
    for i in range(n_peaks):
        shape = shapes[i] if i < len(shapes) else 'gaussian'
        ModelClass = model_map[shape.lower()]
        m = ModelClass(prefix=f'p{i}_')
        composite = m if composite is None else composite + m

    return composite


def fit_peaks(wavenumber: np.ndarray,
              absorbance: np.ndarray,
              guesses: list[PeakGuess],
              shape: PeakShape = 'gaussian',
              center_tolerance: float = 30.0,
              locks: list = None) -> FitResult:
    """
    locks : [{'center': bool, 'sigma': bool}, ...] — True 이면 해당 파라미터 고정
    """
    """
    피크 피팅 실행.

    wavenumber, absorbance: 베이스라인 제거된 스펙트럼
    guesses: PeakGuess 리스트 (자동 감지 또는 수동)
    center_tolerance: 피크 중심 이동 허용 범위 (cm⁻¹)
    """
    n = len(guesses)
    shapes = [getattr(g, 'shape', shape) for g in guesses]
    model = build_model(n, shapes)
    params = Parameters()
    wn_min = float(np.min(wavenumber))
    wn_max = float(np.max(wavenumber))

    # lmfit 모델에서 파라미터 초기화
    for i, g in enumerate(guesses):
        prefix   = f'p{i}_'
        sigma_init = max(abs(float(g.sigma)), 1.0)
        amp_init = g.amplitude * sigma_init * np.sqrt(2 * np.pi)
        lock     = (locks[i] if locks and i < len(locks) else {})
        lock_ctr = lock.get('center', False)
        lock_sig = lock.get('sigma',  False)
        lock_amp = lock.get('amplitude', False)

        if lock_ctr:
            params.add(f'{prefix}center', value=g.center, vary=False)
        else:
            center_min = max(float(g.center) - center_tolerance, wn_min)
            center_max = min(float(g.center) + center_tolerance, wn_max)
            center_val = min(max(float(g.center), center_min), center_max)
            if center_min >= center_max:
                center_min, center_max = wn_min, wn_max
                center_val = min(max(float(g.center), center_min), center_max)
            params.add(f'{prefix}center',
                       value=center_val,
                       min=center_min,
                       max=center_max)

        if lock_amp:
            params.add(f'{prefix}amplitude', value=max(amp_init, 1e-6), vary=False)
        else:
            params.add(f'{prefix}amplitude',
                       value=max(amp_init, 1e-6),
                       min=0)

        if lock_sig:
            params.add(f'{prefix}sigma', value=sigma_init, vary=False)
        else:
            params.add(f'{prefix}sigma',
                       value=sigma_init,
                       min=1.0,
                       max=sigma_init * 10)

    try:
        result = model.fit(absorbance, params, x=wavenumber,
                           method='leastsq', max_nfev=5000)

        # 개별 피크 곡선 계산
        individual_curves = []
        peak_results = []
        total_area = 0.0
        areas = []

        for i in range(n):
            prefix = f'p{i}_'
            center = result.params[f'{prefix}center'].value
            amplitude = result.params[f'{prefix}amplitude'].value
            sigma = result.params[f'{prefix}sigma'].value

            # 개별 피크 곡선
            single_shape = shapes[i] if i < len(shapes) else shape
            single_model = build_model(1, [single_shape])
            single_params = Parameters()
            single_params.add('p0_center', value=center)
            single_params.add('p0_amplitude', value=amplitude)
            single_params.add('p0_sigma', value=sigma)
            curve = single_model.eval(single_params, x=wavenumber)
            individual_curves.append(curve)

            # 면적 계산 (수치적분)
            area = _trapezoid(curve, wavenumber)
            areas.append(abs(area))
            total_area += abs(area)

            # FWHM (Gaussian: 2.355σ)
            fwhm = 2.3548 * sigma

            # 에러 (없으면 0)
            def get_err(name):
                p = result.params[name]
                return p.stderr if p.stderr is not None else 0.0

            peak_results.append(PeakResult(
                index=i,
                shape=single_shape,
                center=center,
                center_err=get_err(f'{prefix}center'),
                amplitude=amplitude,
                amplitude_err=get_err(f'{prefix}amplitude'),
                sigma=sigma,
                sigma_err=get_err(f'{prefix}sigma'),
                fwhm=fwhm,
                area=abs(area),
                area_fraction=0.0  # 아래서 채움
            ))

        # area fraction 계산
        for pr, area in zip(peak_results, areas):
            pr.area_fraction = (area / total_area * 100) if total_area > 0 else 0.0

        # R² 계산
        ss_res = np.sum(result.residual ** 2)
        ss_tot = np.sum((absorbance - np.mean(absorbance)) ** 2)
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

        return FitResult(
            success=True,
            peaks=peak_results,
            fitted_curve=result.best_fit,
            individual_curves=individual_curves,
            residual=result.residual,
            r_squared=r_squared,
            chi_squared=result.chisqr,
            message=result.message
        )

    except Exception as e:
        return FitResult(
            success=False,
            peaks=[],
            fitted_curve=np.zeros_like(absorbance),
            individual_curves=[],
            residual=absorbance,
            r_squared=0.0,
            chi_squared=0.0,
            message=str(e)
        )
