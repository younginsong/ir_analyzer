"""
main.py - IR Spectrum Analyzer 진입점
"""

import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from ui.main_window import MainWindow
from ui.theme import DARK_QSS


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("IR Analyzer")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
