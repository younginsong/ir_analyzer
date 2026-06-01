# IR Analyzer User Manual

This manual walks new users through the full IR Analyzer workflow: loading
spectra, fitting peaks, assigning potentials, reviewing analysis plots, and
exporting results.

Korean version: [USER_MANUAL.md](USER_MANUAL.md)

## 1. Installation And Launch

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

If you are using a packaged desktop release, unzip the package and open the
`IR Analyzer` app or executable.

## 2. Preparing Input Data

Supported file extensions are `.csv`, `.txt`, `.asc`, and `.dpt`.

The first numeric column is interpreted as wavenumber and the second numeric
column as absorbance. Headers, `#` comments, comma/tab/semicolon/space
delimiters, and descending wavenumber order are handled automatically.

```text
wavenumber, absorbance
3800.0, 0.0023
3799.5, 0.0031
3799.0, 0.0034
```

## 3. Application Layout

- Left `SPECTRA` panel: add, remove, select, filter sessions, and assign
  potentials.
- Center `Spectrum` tab: raw spectra, corrected spectra, peak guesses, and
  fitting results.
- Center `Analysis` tab: potential-dependent area, Stark tuning, CO, and
  OH/Si-O plots.
- Right panel: analysis mode, region settings, baseline tools, peak settings,
  summary results, export, and batch processing.

Use the mode buttons in the right panel to switch between `Total`, `OH`, `CO`,
and `Si-O` workflows.

## 4. Loading Spectra

Use any of these methods:

- `File > Open CSV...`
- The `+ Add` button in the left panel
- Drag spectrum files into the left `SPECTRA` list

You can load multiple files at once. You can also drag `.irsession` files into
the list to restore saved sessions.

## 5. OH Peak Deconvolution

This is the standard workflow for OH stretching peak deconvolution.

1. Select `OH` mode in the right panel.
2. Select a spectrum in the left `SPECTRA` panel.
3. Set `Min` and `Max` in `Analysis Region`, then click `Apply`.
   The default OH region is roughly 3000-3990 cm^-1.
4. Turn on `Edit Baseline` in the `Baseline` section.
5. Choose a baseline algorithm.
6. Set the expected number of peaks in `# Peaks`.
7. Click `Auto Detect` to generate initial peak guesses.
8. If needed, drag peak handles in the plot or edit values in the
   `Initial Parameters` table.
9. Click `Run Fit`.
10. Review R2, peak centers, FWHM, area, and area fraction in
    `Current Summary`.

### Baseline Algorithms

- `OH Auto Baseline`: automatic baseline tuned for the OH region.
- `Manual`: user-defined baseline points.
- `Rubber Band`: convex-hull based automatic baseline.
- `ARPLS`: baseline with a smoothness parameter `lambda`.
- `SNIP`: iterative baseline correction.
- `Linear`: simple baseline between the region endpoints.

When using `Manual` or `OH Auto Baseline`, use `Undo` and `Clear` to revise
baseline points.

### Peak Parameter Editing

The `Initial Parameters` table contains:

- `Shape`: Gaussian, Lorentzian, or Voigt.
- `Center`: peak center position.
- `Amp`: initial peak height.
- `Sigma`: initial peak width parameter.
- `C`, `A`, `S`: locks for center, amplitude, and sigma during fitting.

If fitting is unstable, first try adjusting the baseline, narrowing
`Tolerance`, moving peak centers manually, or locking important peak centers.

### Snapshots

`Snapshots` save the current OH state, including baseline, peak guesses, and
fit information. Use them to compare several fitting strategies for the same
spectrum.

## 6. Multiple Spectra And Potential Analysis

For potential-dependent experiments, enter potentials in the
`POTENTIAL ASSIGNMENTS` table in the left panel.

1. Load multiple spectra.
2. Enter `Potential (V)` for each spectrum.
3. Run OH fitting for each spectrum.
4. Click `Calculate Stark Slopes` in `Current Summary`.
5. Review plots in the center `Analysis` tab.

Available OH analysis plots:

- Area Fraction vs Potential
- Stark Tuning: Peak Center vs Potential
- OH Total Area vs Potential
- Normalized OH: OH / Si-O vs Potential

If Stark results are missing, check that potentials are assigned and that at
least two fitted spectra are visible in the current session filter.

## 7. Auto Fit

`Auto Fit` speeds up fitting for a series of spectra in the same session.

Recommended workflow:

1. Load at least three spectra in one session.
2. Manually baseline and fit the first spectrum with `Run Fit`.
3. Manually baseline and fit the last spectrum with `Run Fit`.
4. Run `Auto Fit`.

The app interpolates initial values from the first and last fit to process the
intermediate spectra. If an intermediate fit looks poor, select that spectrum,
adjust baseline, peak centers, or locks, and run `Run Fit` again.

## 8. Total View

`Total` mode displays all loaded spectra together.

- `Overlay`: draw spectra on the same y-axis position.
- `Stack`: offset spectra vertically for comparison.
- `Shift Mode`: drag a selected spectrum to adjust its y-shift.
- `Coordinate Probe`: show the cursor coordinates on the plot.
- `Reset Shifts`: reset all manual shifts.

When potential values are assigned, colors are mapped according to potential.

## 9. CO Analysis

`CO` mode analyzes CO linear and bridge regions.

1. Select `CO` mode in the right panel.
2. Choose `CO_B Handling`.
3. Click `Analyze All CO` to process all visible spectra.
4. Click `Reanalyze Current` to process only the selected spectrum.
5. If CO_B overlap is suspected, use `Refine CO_B Peaks` for 2-peak
   deconvolution.

`CO_B Handling` options:

- `Auto (Recommended)`: apply CO_B deconvolution only when needed.
- `Always 2-Peak`: always fit CO_B with two peaks.
- `Simple Only`: use endpoint-baseline area analysis only.

CO plots are available in the `CO` subtab of `Analysis`:

- CO Linear Area vs Potential
- CO Bridge Area vs Potential
- CO_L / CO_B Ratio vs Potential
- CO Stark Tuning

## 10. Si-O Analysis And OH Normalization

`Si-O` mode calculates the Si-O area in the approximate 1100-1300 cm^-1 region.

1. Select `Si-O` mode in the right panel.
2. Drag the plot endpoints to adjust the baseline.
3. Click `Calculate Si-O Area`.

When Si-O area is available, it is used for the `OH / Si-O` normalized OH plot.

## 11. Saving And Loading Sessions

### Save Workspace

`File > Save Workspace...` saves all currently loaded spectra and analysis
state to an `.irsession` file.

Saved data includes:

- Raw spectrum arrays
- File names and session labels
- Potential assignments
- Baseline states
- OH fit results and snapshots
- CO and Si-O analysis state
- Stark analysis results
- Total view shift state

### Save Current Session

`File > Save Current Session As...` saves only the currently selected session.

### Load Sessions

- `File > Load Session...`: load one `.irsession` file.
- `File > Load Multiple Sessions...`: load several sessions together.
- Drag `.irsession` files into the left list to load them.

When multiple sessions are loaded, use the session buttons at the top of the
left panel to filter what is shown.

## 12. Exporting Data

### Export Results

Use `File > Export Results (Excel)...` or the right-panel `Export` button.

This can export:

- Single-spectrum OH fit results
- Multi-spectrum OH results and Stark results
- CO results in a separate `_CO.xlsx` file when CO analysis is available

### Export Spectra

`File > Export Spectra (Excel)...` exports spectrum data.

Possible workbook sheets:

- `Index`: spectrum list and metadata
- `Raw Matrix` or `Raw Spectra`: raw spectra
- `OH Processed`: OH baseline/corrected data
- `CO Processed`: CO endpoint baseline/corrected data

If all spectra share the same wavenumber grid, data is saved as a matrix.
Otherwise, it is saved in long format.

### Save Plot

Use `Plot View > Save Plot...` in the right panel to save the current plot as
PNG or SVG.

## 13. Batch Processing

Use batch processing when you want to apply the same fitting settings to many
files.

1. Open `File > Batch Process...` or click `Batch` in the right panel.
2. Add files with `Add Files...`.
3. Set `WN Min`, `WN Max`, `# Peaks`, `Peak Shape`, and `Center Tolerance`.
4. Enable `Reuse first fit result as initial values for later files` if useful.
5. Click `OK`.
6. When processing finishes, export the batch results to Excel.

Batch processing uses automatic baseline correction and automatic peak
detection. For complex spectra, first tune settings with the single-spectrum
workflow, then run batch processing.

## 14. Troubleshooting

### A File Does Not Open

- Check that the first two columns contain numeric data.
- If metadata rows are too complex, save only the numeric spectrum data to a
  separate file and try again.
- macOS `._filename` metadata files are not spectrum files.

### Auto Detect Gives Poor Peaks

- Make sure `Analysis Region` contains only the target peak region.
- Check whether the baseline is over-corrected or under-corrected.
- Set `# Peaks` to the expected number of real peaks.

### R2 Is Low Or Peaks Swap Positions

- Move peak centers manually before fitting.
- Reduce `Tolerance` to restrict center movement.
- Lock important centers with the `C` column.
- Try another baseline algorithm.
- Compare Gaussian, Lorentzian, and Voigt peak shapes.

### Stark Slopes Are Not Calculated

- Confirm that potential values are assigned.
- Confirm that the correct session filter is active.
- At least two fitted spectra are required.

### Export Has No Results

- Run `Run Fit`, `Analyze All CO`, or `Calculate Si-O Area` first.
- Confirm that results exist in the currently visible session.

## 15. Recommended Analysis Order

For a typical in situ IR potential series:

1. Load all potential-dependent spectra.
2. Enter `POTENTIAL ASSIGNMENTS`.
3. Use `Total` mode to inspect ordering and overall spectral trends.
4. Tune OH baseline and peak settings on one or two representative spectra.
5. Fit OH peaks for all spectra.
6. Calculate Si-O area if OH normalization is needed.
7. Analyze CO_L / CO_B in `CO` mode if needed.
8. Run `Calculate Stark Slopes`.
9. Review plots in the `Analysis` tab.
10. Save results with `Export Results` and `Export Spectra`.
11. Save the workspace as an `.irsession` file.
