"""
loader.py - CSV / DPT 파일 로드 및 파싱
IR 스펙트럼 파일을 읽어 wavenumber, absorbance 배열로 반환
"""

import numpy as np
from pathlib import Path
from typing import Optional


def _sniff_numeric_table(filepath: str) -> tuple[Optional[str], int]:
    """Return delimiter and rows to skip before the first numeric data row."""
    with open(filepath, 'r', encoding='utf-8-sig', errors='replace') as f:
        for line_no, line in enumerate(f):
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue

            if ',' in stripped:
                delimiter = ','
                fields = stripped.split(',')
            elif '\t' in stripped:
                delimiter = '\t'
                fields = stripped.split('\t')
            elif ';' in stripped:
                delimiter = ';'
                fields = stripped.split(';')
            else:
                delimiter = None
                fields = stripped.split()

            try:
                float(fields[0])
                float(fields[1])
                return delimiter, line_no
            except (IndexError, ValueError):
                continue

    raise ValueError("데이터 행을 찾을 수 없습니다.")


def _load_numeric_table(filepath: str) -> tuple[np.ndarray, np.ndarray]:
    delimiter, skiprows = _sniff_numeric_table(filepath)
    data = np.loadtxt(
        filepath,
        comments='#',
        delimiter=delimiter,
        skiprows=skiprows,
        usecols=(0, 1),
        ndmin=2,
    )
    if data.shape[0] == 0:
        raise ValueError("로드된 데이터가 없습니다.")
    return data[:, 0], data[:, 1]


def load_csv(filepath: str) -> tuple[np.ndarray, np.ndarray]:
    """
    CSV 파일을 로드하여 (wavenumber, absorbance) 반환.
    첫 번째 컬럼 = wavenumber, 두 번째 컬럼 = absorbance 로 가정.
    헤더 자동 감지.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {filepath}")

    try:
        wavenumber, absorbance = _load_numeric_table(filepath)
    except Exception:
        # Fallback for unusual CSV dialects that pandas can still parse.
        import pandas as pd
        df = pd.read_csv(filepath, comment='#', header=None, engine='python')
        numeric = df.iloc[:, :2].apply(pd.to_numeric, errors='coerce').dropna()
        if numeric.empty:
            raise ValueError("숫자 스펙트럼 데이터를 찾을 수 없습니다.")
        wavenumber = numeric.iloc[:, 0].to_numpy(dtype=float)
        absorbance = numeric.iloc[:, 1].to_numpy(dtype=float)

    # wavenumber 내림차순 → 오름차순 정렬 (FTIR 관례 대응)
    if wavenumber[0] > wavenumber[-1]:
        wavenumber = wavenumber[::-1]
        absorbance = absorbance[::-1]

    return wavenumber, absorbance


def load_dpt(filepath: str) -> tuple[np.ndarray, np.ndarray]:
    """
    Bruker FTIR DPT 파일을 로드하여 (wavenumber, absorbance) 반환.
    탭/공백 또는 콤마 구분자 모두 지원.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {filepath}")

    # macOS AppleDouble 메타데이터 파일 (._로 시작) 거부
    if path.name.startswith('._'):
        raise ValueError(f"macOS 메타데이터 파일은 로드할 수 없습니다: {path.name}")

    wavenumber, absorbance = _load_numeric_table(filepath)

    # wavenumber 내림차순 → 오름차순 정렬
    if wavenumber[0] > wavenumber[-1]:
        wavenumber = wavenumber[::-1]
        absorbance = absorbance[::-1]

    return wavenumber, absorbance


def load_spectrum(filepath: str) -> tuple[np.ndarray, np.ndarray]:
    """
    파일 확장자에 따라 적절한 로더를 선택하여 (wavenumber, absorbance) 반환.
    지원 형식: .csv, .txt, .asc, .dpt
    """
    ext = Path(filepath).suffix.lower()
    if ext == '.dpt':
        return load_dpt(filepath)
    else:
        return load_csv(filepath)


def crop_region(wavenumber: np.ndarray, absorbance: np.ndarray,
                wn_min: float, wn_max: float) -> tuple[np.ndarray, np.ndarray]:
    """특정 wavenumber 범위로 크롭"""
    mask = (wavenumber >= wn_min) & (wavenumber <= wn_max)
    return wavenumber[mask], absorbance[mask]
