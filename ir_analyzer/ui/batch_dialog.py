"""
batch_dialog.py - 배치 처리 설정 다이얼로그
"""

from pathlib import Path
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QPushButton, QListWidget, QFileDialog, QDoubleSpinBox,
    QSpinBox, QComboBox, QCheckBox, QDialogButtonBox
)
from batch.batch_processor import BatchConfig


class BatchDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Batch Processing")
        self.setMinimumWidth(450)
        self._filepaths = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── 파일 목록 ─────────────────────────────────────────
        file_group = QGroupBox("Files")
        fl = QVBoxLayout()

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Add Files...")
        btn_remove = QPushButton("Remove Selected")
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_remove)
        fl.addLayout(btn_row)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.MultiSelection)
        fl.addWidget(self.file_list)

        btn_add.clicked.connect(self._add_files)
        btn_remove.clicked.connect(self._remove_files)
        file_group.setLayout(fl)
        layout.addWidget(file_group)

        # ── 피팅 설정 ─────────────────────────────────────────
        config_group = QGroupBox("Fitting Settings")
        form = QFormLayout()

        self.spin_wn_min = QDoubleSpinBox()
        self.spin_wn_min.setRange(0, 10000)
        self.spin_wn_min.setValue(3000)

        self.spin_wn_max = QDoubleSpinBox()
        self.spin_wn_max.setRange(0, 10000)
        self.spin_wn_max.setValue(3800)

        self.spin_n_peaks = QSpinBox()
        self.spin_n_peaks.setRange(1, 10)
        self.spin_n_peaks.setValue(4)

        self.combo_shape = QComboBox()
        self.combo_shape.addItems(['Gaussian', 'Lorentzian', 'Voigt'])

        self.spin_tolerance = QDoubleSpinBox()
        self.spin_tolerance.setRange(1, 200)
        self.spin_tolerance.setValue(30)

        self.cb_reuse_ref = QCheckBox("첫 번째 피팅 결과를 이후 파일 초기값으로 재사용")
        self.cb_reuse_ref.setChecked(True)

        form.addRow("WN Min (cm\u207b\xb9):", self.spin_wn_min)
        form.addRow("WN Max (cm\u207b\xb9):", self.spin_wn_max)
        form.addRow("# Peaks:", self.spin_n_peaks)
        form.addRow("Peak Shape:", self.combo_shape)
        form.addRow("Center Tolerance:", self.spin_tolerance)
        form.addRow(self.cb_reuse_ref)
        config_group.setLayout(form)
        layout.addWidget(config_group)

        # ── OK / Cancel ───────────────────────────────────────
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select CSV Files", "",
            "IR Spectra (*.csv *.txt *.asc *.dpt);;All Files (*)"
        )
        for f in files:
            if f not in self._filepaths:
                self._filepaths.append(f)
                self.file_list.addItem(Path(f).name)

    def _remove_files(self):
        rows = sorted(
            [self.file_list.row(item) for item in self.file_list.selectedItems()],
            reverse=True
        )
        for row in rows:
            self.file_list.takeItem(row)
            self._filepaths.pop(row)

    def get_config(self):
        config = BatchConfig(
            wn_min=self.spin_wn_min.value(),
            wn_max=self.spin_wn_max.value(),
            n_peaks=self.spin_n_peaks.value(),
            peak_shape=self.combo_shape.currentText().lower(),
            center_tolerance=self.spin_tolerance.value(),
            auto_baseline=True,
        )
        if not self.cb_reuse_ref.isChecked():
            config.reference_guesses = None
        return self._filepaths, config
