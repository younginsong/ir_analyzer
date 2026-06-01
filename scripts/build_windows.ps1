$ErrorActionPreference = "Stop"

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { Join-Path $RootDir ".venv-build" }
$DistDir = Join-Path $RootDir "dist"
$ReleaseDir = Join-Path $RootDir "release"
$AppName = "In Situ IR Analyzer"

Write-Host "[1/5] Creating build virtualenv at $VenvDir"
& $PythonBin -m venv $VenvDir

$ActivateScript = Join-Path $VenvDir "Scripts\\Activate.ps1"
. $ActivateScript

Write-Host "[2/5] Installing dependencies"
python -m pip install --upgrade pip
python -m pip install -r (Join-Path $RootDir "ir_analyzer\\requirements.txt") -r (Join-Path $RootDir "requirements-build.txt")

Write-Host "[2.5/5] Applying SciPy frozen-app compatibility patch"
$ScipyInfraFile = Join-Path $VenvDir "Lib\\site-packages\\scipy\\stats\\_distn_infrastructure.py"
if (Test-Path $ScipyInfraFile) {
  $content = Get-Content $ScipyInfraFile -Raw
  $patched = $content -replace "del obj", "try`r`n    del obj`r`nexcept NameError:`r`n    pass"
  if ($patched -ne $content) {
    Set-Content -Path $ScipyInfraFile -Value $patched -NoNewline
  }
}

Write-Host "[3/5] Cleaning previous artifacts"
Remove-Item -Recurse -Force (Join-Path $RootDir "build") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $DistDir -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $ReleaseDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $ReleaseDir | Out-Null

Write-Host "[4/5] Building Windows executable"
pyinstaller --clean --noconfirm (Join-Path $RootDir "ir_analyzer.spec")

Write-Host "[5/5] Packaging release zip"
Compress-Archive -Path (Join-Path $DistDir $AppName) -DestinationPath (Join-Path $ReleaseDir "In-Situ-IR-Analyzer-Windows.zip") -Force

Write-Host ""
Write-Host "Build complete:"
Write-Host "  App folder: $DistDir\\$AppName"
Write-Host "  Zip file:   $ReleaseDir\\In-Situ-IR-Analyzer-Windows.zip"
