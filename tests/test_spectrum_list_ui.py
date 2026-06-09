import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ir_analyzer"))

from PyQt5.QtWidgets import QApplication

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


if __name__ == "__main__":
    unittest.main()
