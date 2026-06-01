"""
batch_processor.py - 배치 자동 처리
동일한 피팅 조건을 여러 CSV 파일에 적용
"""

import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable
from core.loader import load_spectrum, crop_region
from core.baseline import baseline_rubberband, subtract_baseline
from core.peak_finder import find_peaks_second_derivative, PeakGuess
from core.fitter import fit_peaks, FitResult, PeakShape


@dataclass
class BatchConfig:
    """배치 처리 설정"""
    wn_min: float = 3000.0          # 분석 영역 시작 (cm⁻¹)
    wn_max: float = 3800.0          # 분석 영역 끝 (cm⁻¹)
    n_peaks: int = 4                 # 피크 수
    peak_shape: PeakShape = 'gaussian'
    center_tolerance: float = 30.0   # 피크 중심 이동 허용 범위
    auto_baseline: bool = True       # 자동 베이스라인 사용
    baseline_points: Optional[list] = None  # 수동 베이스라인 포인트
    reference_guesses: Optional[list[PeakGuess]] = None  # 첫 파일 피팅 후 재사용


@dataclass
class BatchItem:
    """단일 배치 아이템 결과"""
    filename: str
    filepath: str
    success: bool
    fit_result: Optional[FitResult] = None
    error_message: str = ""
    wavenumber: Optional[np.ndarray] = None
    absorbance_corrected: Optional[np.ndarray] = None
    baseline: Optional[np.ndarray] = None


def process_batch(filepaths: list[str],
                  config: BatchConfig,
                  progress_callback: Optional[Callable[[int, int, str], None]] = None
                  ) -> list[BatchItem]:
    """
    여러 CSV 파일을 동일 조건으로 피팅.

    progress_callback(current, total, filename): UI 프로그레스바용
    """
    results = []
    reference_guesses = config.reference_guesses

    for i, filepath in enumerate(filepaths):
        filename = Path(filepath).name

        if progress_callback:
            progress_callback(i + 1, len(filepaths), filename)

        try:
            # 1. 로드 & 크롭
            wn, ab = load_spectrum(filepath)
            wn, ab = crop_region(wn, ab, config.wn_min, config.wn_max)

            if len(wn) < 10:
                raise ValueError(f"데이터 포인트가 너무 적습니다 ({len(wn)}개)")

            # 2. 베이스라인
            if config.auto_baseline:
                from core.baseline import baseline_rubberband
                bl = baseline_rubberband(wn, ab)
            elif config.baseline_points:
                from core.baseline import baseline_from_points
                bl = baseline_from_points(wn, ab, config.baseline_points)
            else:
                bl = np.zeros_like(ab)

            ab_corr = subtract_baseline(ab, bl)
            ab_corr = np.maximum(ab_corr, 0)  # 음수 제거

            # 3. 피크 감지
            if reference_guesses is not None:
                # 레퍼런스 초기값 재사용 (배치에서 일관성 유지)
                guesses = reference_guesses
            else:
                guesses = find_peaks_second_derivative(wn, ab_corr, n_peaks=config.n_peaks)

            # 4. 피팅
            fit = fit_peaks(wn, ab_corr, guesses,
                            shape=config.peak_shape,
                            center_tolerance=config.center_tolerance)

            # 첫 번째 성공한 피팅을 레퍼런스로 저장
            if fit.success and reference_guesses is None and config.reference_guesses is None:
                from core.peak_finder import PeakGuess
                reference_guesses = [
                    PeakGuess(center=p.center,
                              amplitude=float(np.max(fit.individual_curves[i]))
                                        if i < len(fit.individual_curves) else p.amplitude,
                              sigma=p.sigma, index=0)
                    for i, p in enumerate(fit.peaks)
                ]

            results.append(BatchItem(
                filename=filename,
                filepath=filepath,
                success=fit.success,
                fit_result=fit,
                wavenumber=wn,
                absorbance_corrected=ab_corr,
                baseline=bl
            ))

        except Exception as e:
            results.append(BatchItem(
                filename=filename,
                filepath=filepath,
                success=False,
                error_message=str(e)
            ))

    return results
