# Desktop Build & Website Deployment

## 1. Local build

### macOS

```bash
bash scripts/build_macos.sh
```

Output:

- `dist/IR Analyzer.app`
- `release/IR-Analyzer-macOS.zip`

### Windows

Windows machine or GitHub Actions runner에서 아래 스크립트를 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
```

Output:

- `dist/IR Analyzer/`
- `release/IR-Analyzer-Windows.zip`

## 2. GitHub Actions로 macOS / Windows 동시 빌드

`.github/workflows/build-desktop.yml` 이 포함되어 있습니다.

- 수동 실행: GitHub Actions에서 `Build Desktop Apps` 실행
- 릴리스 빌드: `v1.0.0` 같은 태그 push

생성 결과:

- macOS zip artifact
- Windows zip artifact

이 artifact를 받아서 홈페이지에 그대로 올리면 됩니다.

## 3. 홈페이지 업로드 방식

다운로드 버튼은 보통 아래처럼 운영합니다.

- `IR-Analyzer-macOS.zip`
- `IR-Analyzer-Windows.zip`

예시 링크:

```html
<a href="/downloads/IR-Analyzer-macOS.zip">Download for macOS</a>
<a href="/downloads/IR-Analyzer-Windows.zip">Download for Windows</a>
```

## 4. 사용자 안내 문구

### macOS

- 앱이 unsigned 상태이면 처음 실행 시 Gatekeeper 경고가 뜰 수 있습니다.
- 사용자는 `System Settings > Privacy & Security` 에서 `Open Anyway` 로 실행할 수 있습니다.

### Windows

- 서명되지 않은 실행 파일은 SmartScreen 경고가 뜰 수 있습니다.
- 배포 전 신뢰도를 높이려면 코드 서명 인증서를 사용하는 것이 좋습니다.

## 5. 출시 전에 권장할 마지막 점검

- 실제 macOS 장비에서 앱 실행 확인
- 실제 Windows 장비에서 실행 확인
- 샘플 CSV 열기 / 피팅 / Excel export 동작 확인
- 배치 처리 확인
- 홈페이지 다운로드 링크 연결 확인
