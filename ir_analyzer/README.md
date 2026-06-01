# IR Spectrum Analyzer

FTIR 스펙트럼 디컨볼루션 도구 — OH stretching 4-peak 분석에 최적화

## 설치

```bash
pip install -r requirements.txt
```

## 실행

```bash
python main.py
```

## 기능

### 단일 파일 분석
1. `File → Open CSV` 로 스펙트럼 로드
2. Settings 탭에서 분석 영역 설정 (기본: 3000–3800 cm⁻¹)
3. Peaks 탭에서 `🔍 Auto Detect` 클릭 → 2차 미분으로 피크 4개 자동 감지
4. 초기값 테이블에서 필요시 수동 조정
5. `▶ Run Fit` 클릭 → 피팅 실행
6. 결과: 피크 위치, FWHM, 면적, 면적% 자동 표시
7. `File → Export Results (Excel)` 로 저장

### 배치 처리
1. `File → Batch Process (Ctrl+B)` 
2. CSV 파일 여러 개 추가
3. 피팅 조건 설정 후 OK
4. 완료 후 Excel 일괄 저장
   - 파일명 / R² / 각 피크별 center, FWHM, area, area% 

## 프로젝트 구조

```
ir_analyzer/
├── main.py                  # 진입점
├── requirements.txt
├── core/
│   ├── loader.py            # CSV 파싱
│   ├── baseline.py          # 베이스라인 (Rubber Band / 수동)
│   ├── peak_finder.py       # 2차 미분 자동 피크 감지
│   ├── fitter.py            # lmfit 피팅 엔진
│   └── exporter.py          # Excel 내보내기
├── batch/
│   └── batch_processor.py   # 배치 자동화
└── ui/
    ├── main_window.py        # 메인 윈도우
    ├── plot_widget.py        # pyqtgraph 인터랙티브 플롯
    ├── control_panel.py      # 영역/베이스라인 설정
    ├── peak_panel.py         # 피크 파라미터 & 결과
    └── batch_dialog.py       # 배치 설정 다이얼로그
```

## CSV 포맷

첫 번째 컬럼 = wavenumber (cm⁻¹), 두 번째 컬럼 = absorbance
헤더 유무 자동 감지. 오름차순/내림차순 자동 처리.

```
3800.0, 0.0023
3799.5, 0.0031
...
```

## 피크 함수 선택 가이드

| 함수 | 특징 |
|---|---|
| Gaussian | 균일한 환경, 대부분의 IR 피크 |
| Lorentzian | 균질 넓어짐 (homogeneous broadening) |
| Voigt | Gaussian + Lorentzian 혼합, 가장 유연 |

OH stretching은 보통 **Gaussian** 또는 **Voigt** 추천.
