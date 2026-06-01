#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
RELEASE_DIR="$ROOT_DIR/release"
APP_NAME="In Situ IR Analyzer"
STAGE_DIR="${STAGE_DIR:-$(mktemp -d /tmp/ir-analyzer-build.XXXXXX)}"
VENV_DIR="$STAGE_DIR/.venv-build"

if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ "$(uname -m)" == "arm64" && -x "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" ]]; then
    PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
  else
    PYTHON_BIN="python3"
  fi
fi

cleanup() {
  rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

echo "[0/5] Syncing sources to ASCII-safe path $STAGE_DIR"
rsync -a \
  --exclude ".git/" \
  --exclude ".venv-build/" \
  --exclude "build/" \
  --exclude "dist/" \
  --exclude "release/" \
  --exclude "__pycache__/" \
  "$ROOT_DIR/" "$STAGE_DIR/"

echo "[1/5] Creating build virtualenv at $VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "[2/5] Installing dependencies"
python -m pip install --upgrade pip
python -m pip install -r "$STAGE_DIR/ir_analyzer/requirements.txt" -r "$STAGE_DIR/requirements-build.txt"

echo "[2.5/5] Applying SciPy frozen-app compatibility patch"
SCIPY_INFRA_FILE="$VENV_DIR/lib/python3.12/site-packages/scipy/stats/_distn_infrastructure.py"
if [[ -f "$SCIPY_INFRA_FILE" ]]; then
  python - <<'PY' "$SCIPY_INFRA_FILE"
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = "del obj"
new = "try:\n    del obj\nexcept NameError:\n    pass"
if old in text and new not in text:
    path.write_text(text.replace(old, new, 1))
PY
fi

echo "[3/5] Cleaning previous artifacts"
rm -rf "$ROOT_DIR/build" "$DIST_DIR" "$RELEASE_DIR" "$STAGE_DIR/build" "$STAGE_DIR/dist" "$STAGE_DIR/release"
mkdir -p "$DIST_DIR" "$RELEASE_DIR"

echo "[4/5] Building macOS app bundle"
pyinstaller \
  --clean \
  --noconfirm \
  --distpath "$STAGE_DIR/dist" \
  --workpath "$STAGE_DIR/build" \
  "$STAGE_DIR/ir_analyzer.spec"

echo "[5/5] Sanitizing bundle and packaging release zip"
cp -R "$STAGE_DIR/dist/$APP_NAME.app" "$DIST_DIR/"
xattr -crs "$DIST_DIR/$APP_NAME.app" || true
codesign --force --deep --sign - "$DIST_DIR/$APP_NAME.app"
ditto -c -k --keepParent --norsrc "$DIST_DIR/$APP_NAME.app" "$RELEASE_DIR/In-Situ-IR-Analyzer-macOS.zip"

echo
echo "Build complete:"
echo "  App bundle: $DIST_DIR/$APP_NAME.app"
echo "  Zip file:   $RELEASE_DIR/In-Situ-IR-Analyzer-macOS.zip"
