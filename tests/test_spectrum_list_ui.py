import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ir_analyzer"))

from PyQt5.QtWidgets import QApplication, QComboBox

from ui.main_window import MainWindow
from ui.analysis_widget import AnalysisWidget
from ui.spectrum_list import SpectrumListWidget


class SpectrumListUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_sidebar_renders_potential_assignments_table(self):
        widget = SpectrumListWidget()

        self.assertIsNotNone(
            widget.findChild(object, "spectra_potential_table"),
            "Potential Assignments table should remain in the spectra sidebar",
        )

    def test_analysis_view_does_not_render_potential_assignment_sidebar(self):
        widget = AnalysisWidget()

        self.assertIsNone(
            widget.findChild(object, "analysis_assignments_table"),
            "Potential Assignments table should not be rendered in Analysis",
        )
        self.assertIsNone(
            widget.findChild(object, "analysis_focus_card"),
            "Focused Spectrum card should not be rendered in Analysis",
        )

    def test_analysis_compare_tab_exposes_peak_area_metrics(self):
        widget = AnalysisWidget()
        combo = widget.findChild(QComboBox, "analysis_compare_metric_combo")

        self.assertIsNotNone(combo)
        self.assertEqual(combo.currentData(), "P3+P4")
        self.assertEqual(
            [combo.itemData(i) for i in range(combo.count())],
            ["P1", "P2", "P3", "P4", "P1+P2", "P3+P4"],
        )

    def test_analysis_subtab_is_preserved_when_returning_from_spectrum(self):
        window = MainWindow()
        try:
            window.analysis_widget.set_current_subtab("Compare")
            window.center_tabs.setCurrentWidget(window.plot_widget)
            window.center_tabs.setCurrentWidget(window.analysis_widget)

            self.assertEqual(window.analysis_widget.get_current_subtab(), "Compare")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
