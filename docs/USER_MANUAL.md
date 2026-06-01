# IR Analyzer User Manual

이 문서는 IR Analyzer를 처음 사용하는 사람이 스펙트럼을 불러오고,
피팅하고, 전위별 분석 결과를 내보내는 전체 흐름을 따라갈 수 있도록
작성한 사용자 매뉴얼입니다.

## 1. 설치와 실행

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r ir_analyzer/requirements.txt
python ir_analyzer/main.py
```

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r ir_analyzer\requirements.txt
python ir_analyzer\main.py
```

배포된 데스크톱 패키지를 사용하는 경우에는 압축을 풀고 `IR Analyzer`
앱 또는 실행 파일을 열면 됩니다.

## 2. 입력 데이터 준비

지원 확장자는 `.csv`, `.txt`, `.asc`, `.dpt`입니다.

파일의 첫 번째 숫자 컬럼은 wavenumber, 두 번째 숫자 컬럼은 absorbance로
해석됩니다. 헤더, `#` 주석, 콤마/탭/세미콜론/공백 구분자는 자동으로
처리됩니다.

```text
wavenumber, absorbance
3800.0, 0.0023
3799.5, 0.0031
3799.0, 0.0034
```

FTIR 장비에서 흔히 나오는 내림차순 wavenumber 데이터도 자동으로
오름차순으로 정렬됩니다.

## 3. 화면 구성

- 왼쪽 `SPECTRA`: 스펙트럼 추가, 제거, 선택, 세션 필터, 전위값 입력.
- 중앙 `Spectrum`: 원본/보정/피팅 결과를 보여주는 플롯.
- 중앙 `Analysis`: 전위별 면적, Stark tuning, CO, OH/Si-O 그래프.
- 오른쪽 패널: 분석 모드, 영역, baseline, 피크 설정, 결과 요약, export,
  batch 실행.

분석 모드는 오른쪽 위 버튼에서 `Total`, `OH`, `CO`, `Si-O` 중 선택합니다.

## 4. 스펙트럼 불러오기

다음 방법 중 하나를 사용합니다.

- `File > Open CSV...`
- 왼쪽 `+ Add`
- 파일을 왼쪽 `SPECTRA` 목록으로 드래그 앤 드롭

여러 파일을 한 번에 불러올 수 있습니다. `.irsession` 파일도 드래그해서
기존 세션을 불러올 수 있습니다.

## 5. OH Peak Deconvolution

OH stretching 영역의 다중 피크를 분해하는 기본 workflow입니다.

1. 오른쪽 모드에서 `OH`를 선택합니다.
2. 왼쪽 `SPECTRA`에서 분석할 스펙트럼을 선택합니다.
3. `Analysis Region`에서 `Min`, `Max`를 지정하고 `Apply`를 누릅니다.
   기본값은 대략 3000-3990 cm^-1입니다.
4. `Baseline`에서 `Edit Baseline`을 켭니다.
5. baseline 알고리즘을 선택합니다.
6. `# Peaks`에 예상 피크 수를 입력합니다.
7. `Auto Detect`를 눌러 초기 피크를 자동 감지합니다.
8. 필요하면 플롯에서 피크 핸들을 드래그하거나 `Initial Parameters`
   테이블에서 값을 직접 수정합니다.
9. `Run Fit`을 누릅니다.
10. `Current Summary`에서 R2, peak center, FWHM, area, area fraction을
    확인합니다.

### Baseline 알고리즘

- `OH Auto Baseline`: OH 영역에 맞춘 자동 baseline.
- `Manual`: 사용자가 baseline point를 직접 지정해 보정.
- `Rubber Band`: convex-hull 기반 자동 baseline.
- `ARPLS`: smoothness 파라미터 `lambda`를 사용하는 baseline.
- `SNIP`: 반복 횟수를 사용하는 baseline.
- `Linear`: 양 끝점을 잇는 단순 baseline.

Manual 또는 OH Auto Baseline 사용 중에는 `Undo`, `Clear`로 baseline point
상태를 되돌릴 수 있습니다.

### 피크 파라미터 조정

`Initial Parameters` 표의 주요 컬럼은 다음과 같습니다.

- `Shape`: Gaussian, Lorentzian, Voigt.
- `Center`: 피크 중심 위치.
- `Amp`: 초기 피크 높이.
- `Sigma`: 피크 폭 관련 초기값.
- `C`, `A`, `S`: center, amplitude, sigma를 피팅 중 고정하는 lock.

피팅이 흔들리면 center lock이나 더 좁은 `Tolerance`를 먼저 시도하는 것이
좋습니다.

### Snapshots

OH 모드의 `Snapshots`는 현재 baseline, peak guess, fit 상태를 저장했다가
다시 불러오는 기능입니다. 같은 스펙트럼에서 여러 피팅 조건을 비교할 때
유용합니다.

## 6. 여러 스펙트럼과 전위 분석

여러 전위에서 측정한 스펙트럼을 분석할 때는 왼쪽 아래
`POTENTIAL ASSIGNMENTS` 표에 전위값을 입력합니다.

1. 여러 스펙트럼을 불러옵니다.
2. 각 스펙트럼 행의 `Potential (V)` 값을 입력합니다.
3. 각 스펙트럼에 대해 OH fit을 실행합니다.
4. 오른쪽 `Current Summary`의 `Calculate Stark Slopes`를 실행합니다.
5. 중앙 `Analysis` 탭에서 다음 그래프를 확인합니다.

- Area Fraction vs Potential
- Stark Tuning: Peak Center vs Potential
- OH Total Area vs Potential
- Normalized OH: OH / Si-O vs Potential

전위값이 없거나 파일명에서 전위를 파싱할 수 없으면 Stark 결과가 나오지
않을 수 있으므로, `POTENTIAL ASSIGNMENTS`를 먼저 확인하세요.

## 7. Auto Fit

`Auto Fit`은 한 세션 안의 여러 스펙트럼을 빠르게 피팅하기 위한 기능입니다.

권장 절차:

1. 같은 세션의 스펙트럼을 3개 이상 불러옵니다.
2. 첫 번째 스펙트럼을 수동으로 baseline/피크 조정 후 `Run Fit`합니다.
3. 마지막 스펙트럼도 수동으로 `Run Fit`합니다.
4. `Auto Fit`을 실행합니다.

앱은 양 끝 피팅 결과를 참고해 중간 스펙트럼의 초기값을 보간하고 자동으로
피팅합니다. 중간 결과가 좋지 않으면 해당 스펙트럼만 선택해 baseline,
peak center, lock을 수정한 뒤 다시 `Run Fit`합니다.

## 8. Total View

`Total` 모드는 로드된 스펙트럼을 한 화면에서 비교하는 모드입니다.

- `Overlay`: 모든 스펙트럼을 같은 y축 위치에 겹쳐 표시.
- `Stack`: 스펙트럼을 y 방향으로 띄워서 표시.
- `Shift Mode`: 선택한 스펙트럼의 y-shift를 드래그로 조절.
- `Coordinate Probe`: 마우스 위치의 좌표를 표시.
- `Reset Shifts`: 조정한 shift를 초기화.

전위값이 지정된 경우 색상은 potential 기준으로 정렬됩니다.

## 9. CO 분석

`CO` 모드는 CO linear / bridge 관련 영역을 분석합니다.

1. 오른쪽 모드에서 `CO`를 선택합니다.
2. `CO_B Handling`을 선택합니다.
3. 전체 데이터를 처리하려면 `Analyze All CO`를 누릅니다.
4. 현재 스펙트럼만 다시 계산하려면 `Reanalyze Current`를 누릅니다.
5. CO_B 피크가 겹쳐 보이면 `Refine CO_B Peaks`로 2-peak deconvolution을
   보정합니다.

`CO_B Handling` 옵션:

- `Auto (Recommended)`: 필요한 경우에만 CO_B deconvolution을 적용.
- `Always 2-Peak`: CO_B를 항상 2개 피크로 분해.
- `Simple Only`: 단순 endpoint baseline 기반 면적만 계산.

CO 결과는 `Analysis` 탭의 `CO` 서브탭에서 확인합니다.

- CO Linear Area vs Potential
- CO Bridge Area vs Potential
- CO_L / CO_B Ratio vs Potential
- CO Stark Tuning

## 10. Si-O 분석과 OH 정규화

`Si-O` 모드는 대략 1100-1300 cm^-1 영역의 Si-O 면적을 계산합니다.

1. 오른쪽 모드에서 `Si-O`를 선택합니다.
2. 플롯의 endpoint를 드래그해 baseline 위치를 조정합니다.
3. `Calculate Si-O Area`를 누릅니다.

Si-O 면적이 있으면 OH 분석의 `OH / Si-O` 정규화 그래프에 사용됩니다.

## 11. 저장과 불러오기

### Workspace 저장

`File > Save Workspace...`는 현재 로드된 모든 스펙트럼과 분석 상태를
`.irsession` 파일로 저장합니다.

저장되는 정보:

- 원본 스펙트럼 배열
- 파일명과 세션 정보
- potential assignments
- baseline 상태
- OH fit 결과와 snapshots
- CO / Si-O 분석 상태
- Stark 분석 결과
- Total view shift 상태

### 현재 세션 저장

`File > Save Current Session As...`는 현재 선택된 세션만 저장합니다.

### 세션 불러오기

- `File > Load Session...`: `.irsession` 파일 하나를 불러옵니다.
- `File > Load Multiple Sessions...`: 여러 세션을 함께 불러옵니다.
- `.irsession` 파일을 왼쪽 목록에 드래그해도 불러올 수 있습니다.

여러 세션을 불러오면 왼쪽 상단의 세션 버튼으로 화면에 표시할 세션을
필터링할 수 있습니다.

## 12. 내보내기

### Export Results

`File > Export Results (Excel)...` 또는 오른쪽 `Export` 버튼을 사용합니다.

내보내는 내용:

- 단일 OH fit 결과
- 여러 OH fit 결과와 Stark 결과
- CO 분석 결과가 있으면 별도의 `_CO.xlsx` 파일

### Export Spectra

`File > Export Spectra (Excel)...`은 스펙트럼 데이터를 내보냅니다.

포함될 수 있는 시트:

- `Index`: 스펙트럼 목록과 메타데이터
- `Raw Matrix` 또는 `Raw Spectra`: 원본 스펙트럼
- `OH Processed`: OH baseline/corrected 데이터
- `CO Processed`: CO endpoint baseline/corrected 데이터

모든 스펙트럼의 wavenumber grid가 같으면 matrix 형식으로, 다르면
long-format으로 저장됩니다.

### Save Plot

오른쪽 `Plot View > Save Plot...`으로 현재 플롯을 PNG 또는 SVG로 저장합니다.

## 13. Batch Processing

많은 파일에 같은 피팅 조건을 적용할 때 사용합니다.

1. `File > Batch Process...` 또는 오른쪽 `Batch`를 누릅니다.
2. `Add Files...`로 분석할 파일을 추가합니다.
3. `WN Min`, `WN Max`, `# Peaks`, `Peak Shape`, `Center Tolerance`를 설정합니다.
4. 필요하면 `첫 번째 피팅 결과를 이후 파일 초기값으로 재사용`을 켭니다.
5. `OK`를 누릅니다.
6. 완료 메시지가 나오면 Excel로 저장합니다.

배치 처리는 자동 baseline과 자동 peak detection을 사용하므로, 아주 복잡한
스펙트럼은 먼저 단일 파일 workflow로 조건을 확인한 뒤 batch를 실행하는
것이 좋습니다.

## 14. 자주 생기는 문제

### 파일이 열리지 않음

- 첫 두 컬럼이 숫자인지 확인합니다.
- 빈 행, 장비 메타데이터, 주석이 너무 복잡하면 숫자 데이터만 별도 파일로
  저장해 다시 시도합니다.
- macOS의 `._filename` 형태 메타데이터 파일은 분석 파일이 아닙니다.

### Auto Detect 결과가 이상함

- `Analysis Region`이 목표 피크 영역만 포함하는지 확인합니다.
- baseline이 과하게 휘지 않았는지 확인합니다.
- `# Peaks`를 실제 피크 수에 맞춥니다.

### Fit R2가 낮거나 피크가 뒤섞임

- peak center를 수동으로 조금 옮깁니다.
- `Tolerance`를 줄여 center 이동 범위를 제한합니다.
- 중요한 피크는 `C` lock으로 center를 고정합니다.
- baseline 알고리즘을 바꿔봅니다.
- Gaussian, Lorentzian, Voigt 중 더 맞는 shape를 선택합니다.

### Stark slopes가 계산되지 않음

- 각 스펙트럼의 potential 값이 입력되어 있는지 확인합니다.
- 현재 세션 필터가 올바른지 확인합니다.
- 최소 2개 이상의 피팅된 스펙트럼이 필요합니다.

### Export 버튼을 눌러도 결과가 없음

- 먼저 `Run Fit`, `Analyze All CO`, 또는 `Calculate Si-O Area`를 실행했는지
  확인합니다.
- 현재 보이는 세션에 결과가 있는지 확인합니다.

## 15. 권장 분석 순서

일반적인 in situ IR 전위 의존성 실험에서는 다음 순서를 권장합니다.

1. 모든 전위 스펙트럼을 불러옵니다.
2. `POTENTIAL ASSIGNMENTS`를 입력합니다.
3. `Total` 모드에서 스펙트럼 순서와 전체 모양을 확인합니다.
4. 대표 스펙트럼 한두 개로 OH baseline과 peak 조건을 잡습니다.
5. 모든 스펙트럼을 OH fit합니다.
6. 필요한 경우 `Si-O` 면적을 계산합니다.
7. 필요한 경우 `CO` 모드에서 CO_L / CO_B를 분석합니다.
8. `Calculate Stark Slopes`를 실행합니다.
9. `Analysis` 탭의 그래프를 검토합니다.
10. `Export Results`와 `Export Spectra`로 결과를 저장합니다.
11. `Save Workspace`로 `.irsession`을 저장합니다.
