# In Situ IR Analyzer

In Situ IR Analyzer is a desktop application for in situ IR spectrum inspection,
baseline correction, peak deconvolution, Stark tuning analysis, and Excel
export. It is optimized for OH stretching analysis and also includes CO and
Si-O workflows used in electrochemical IR experiments.

## Main Features

- Load IR spectra from `.csv`, `.txt`, `.asc`, and `.dpt` files.
- Inspect one spectrum or compare multiple spectra in overlay/stack views.
- Apply automatic or manual baseline correction.
- Detect and fit OH peaks with Gaussian, Lorentzian, or Voigt components.
- Analyze CO linear/bridge regions and calculate CO_L / CO_B trends.
- Calculate Si-O area for OH / Si-O normalization.
- Assign potentials and calculate Stark tuning slopes.
- Save and reload full workspaces as `.irsession` files.
- Export fit results, processed spectra, raw spectra, and plots.
- Batch-process many spectra with the same fitting settings.

## Quick Start

### 1. Create a Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r ir_analyzer/requirements.txt
```

On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r ir_analyzer\requirements.txt
```

### 2. Run the application

```bash
python ir_analyzer/main.py
```

### 3. Open spectra

Use `File > Open CSV...`, the `+ Add` button, or drag spectrum files onto the
left `SPECTRA` panel.

Supported input files should contain numeric spectrum data in the first two
columns:

```text
wavenumber, absorbance
3800.0, 0.0023
3799.5, 0.0031
...
```

Headers, comment lines, comma/tab/semicolon/space delimiters, and descending
wavenumber order are handled automatically.

## Basic OH Fitting Workflow

1. Load one or more spectra.
2. Select a spectrum in the left `SPECTRA` panel.
3. In `OH` mode, set the `Analysis Region` and click `Apply`.
4. Open `Baseline`, choose a baseline method, and adjust it if needed.
5. Set `# Peaks`, then click `Auto Detect`.
6. Edit peak centers, amplitudes, sigma values, or lock columns if needed.
7. Click `Run Fit`.
8. Review R2, centers, FWHM, areas, and area fractions.
9. Export with `File > Export Results (Excel)...`.

For a full walkthrough, see the user manuals:

- [English user manual](docs/USER_MANUAL_EN.md)
- [Korean user manual](docs/USER_MANUAL.md)

## Files And Outputs

- `.irsession`: saved workspace/session containing spectra, potentials,
  baseline states, fitting results, CO/Si-O states, and analysis records.
- `Export Results (Excel)`: fit summaries and analysis results.
- `Export Spectra (Excel)`: raw spectra and available processed spectra.
- `Save Plot...`: current plot as PNG or SVG.
- Batch export: summary workbook for successful batch fits.

## Project Structure

```text
ir_analyzer/
  main.py                 Application entry point
  requirements.txt        Runtime Python dependencies
  core/                   Data loading, baseline, fitting, export, sessions
  batch/                  Batch-processing workflow
  ui/                     PyQt5 user interface
scripts/
  build_macos.sh          macOS PyInstaller build
  build_windows.ps1       Windows PyInstaller build
DEPLOYMENT.md             Desktop build and release notes
docs/
  USER_MANUAL.md          Korean end-user manual
  USER_MANUAL_EN.md       English end-user manual
```

## Build Desktop Packages

macOS:

```bash
bash scripts/build_macos.sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
```

Build outputs are written to `dist/` and zipped release files are written to
`release/`. More release notes are in [DEPLOYMENT.md](DEPLOYMENT.md).

## Notes

- The application is currently implemented as a PyQt5 desktop app.
- Python 3.11 or 3.12 is recommended.
- If a fit looks unstable, first check the baseline, analysis region, peak
  count, and center tolerance before changing the model shape.
