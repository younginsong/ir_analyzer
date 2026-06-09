"""
theme.py - 다크 모던 테마 (QSS)
"""

DARK_QSS = """
/* ── 전체 배경 ── */
QMainWindow, QDialog, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Helvetica Neue", Arial;
    font-size: 13px;
}

/* ── 메뉴바 ── */
QMenuBar {
    background-color: #181825;
    color: #cdd6f4;
    border-bottom: 1px solid #313244;
    padding: 2px 0;
}
QMenuBar::item:selected { background-color: #313244; border-radius: 4px; }
QMenu {
    background-color: #1e1e2e;
    border: 1px solid #45475a;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item { padding: 6px 24px; border-radius: 4px; }
QMenu::item:selected { background-color: #313244; }
QMenu::separator { height: 1px; background-color: #45475a; margin: 4px 8px; }

/* ── 스플리터 ── */
QSplitter::handle { background-color: #313244; }
QSplitter::handle:horizontal { width: 1px; }

/* ── 스크롤바 ── */
QScrollBar:vertical {
    background: #181825; width: 8px; border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #45475a; border-radius: 4px; min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: #6c7086; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QScrollBar:horizontal {
    background: #181825; height: 8px; border-radius: 4px;
}
QScrollBar::handle:horizontal {
    background: #45475a; border-radius: 4px; min-width: 20px;
}
QScrollBar::handle:horizontal:hover { background: #6c7086; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ── 버튼 ── */
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 500;
}
QPushButton:hover { background-color: #45475a; border-color: #6c7086; }
QPushButton:pressed { background-color: #1e1e2e; }
QPushButton:disabled { color: #585b70; border-color: #313244; }

QPushButton#btn_primary {
    background-color: #89b4fa;
    color: #1e1e2e;
    border: none;
    font-weight: 600;
}
QPushButton#btn_primary:hover { background-color: #b4befe; }

QPushButton#btn_success {
    background-color: #a6e3a1;
    color: #1e1e2e;
    border: none;
    font-weight: 600;
}
QPushButton#btn_success:hover { background-color: #94e2d5; }

QPushButton#btn_danger {
    background-color: #f38ba8;
    color: #1e1e2e;
    border: none;
}
QPushButton#btn_danger:hover { background-color: #eba0ac; }

QPushButton#btn_flat {
    background-color: transparent;
    border: none;
    color: #89b4fa;
    padding: 4px 8px;
}
QPushButton#btn_flat:hover { color: #b4befe; background-color: #313244; border-radius: 4px; }
QPushButton#btn_flat:checked {
    background-color: #313244;
    color: #cdd6f4;
    border-radius: 4px;
    border-bottom: 2px solid #cdd6f4;
}

/* ── 입력 필드 ── */
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: #89b4fa;
    selection-color: #1e1e2e;
}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {
    border-color: #89b4fa;
}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QSpinBox::up-button, QSpinBox::down-button {
    background-color: #45475a;
    border: none;
    border-radius: 2px;
    width: 16px;
}
QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover { background-color: #6c7086; }
QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover { background-color: #6c7086; }

QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow { image: none; border: none; }
QComboBox QAbstractItemView {
    background-color: #1e1e2e;
    border: 1px solid #45475a;
    selection-background-color: #313244;
}
QComboBox#table_combo {
    padding: 3px 8px;
    min-height: 24px;
}
QComboBox#table_combo::drop-down {
    width: 18px;
}

/* ── 체크박스 ── */
QCheckBox { spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #45475a;
    border-radius: 4px;
    background-color: #313244;
}
QCheckBox::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}

/* ── GroupBox ── */
QGroupBox {
    border: 1px solid #313244;
    border-radius: 8px;
    margin-top: 8px;
    padding-top: 8px;
    font-weight: 600;
    color: #a6adc8;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #89b4fa;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

/* ── 탭 ── */
QTabWidget::pane {
    border: 1px solid #313244;
    border-radius: 8px;
    background-color: #1e1e2e;
}
QTabBar::tab {
    background-color: #181825;
    color: #6c7086;
    border: none;
    padding: 8px 16px;
    border-radius: 6px 6px 0 0;
    margin-right: 2px;
    font-weight: 500;
}
QTabBar::tab:selected { background-color: #313244; color: #cdd6f4; }
QTabBar::tab:hover { background-color: #313244; color: #a6adc8; }

/* ── 중앙 탭 (Spectrum / Analysis) — 오른쪽 패널보다 크게 ── */
QTabWidget#center_tabs::pane {
    border: none;
    background-color: #1e1e2e;
}
QTabWidget#center_tabs > QTabBar::tab {
    padding: 9px 28px;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.3px;
}
QTabWidget#center_tabs > QTabBar::tab:selected {
    background-color: #313244;
    color: #89b4fa;
    border-bottom: 2px solid #89b4fa;
}

/* ── 리스트 위젯 ── */
QListWidget {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 8px;
    outline: none;
}
QListWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
    margin: 1px 4px;
    color: #cdd6f4;
}
QListWidget::item:selected {
    background-color: #313244;
    color: #89b4fa;
}
QListWidget::item:hover { background-color: #262638; }

/* ── 테이블 ── */
QTableWidget {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 8px;
    gridline-color: #313244;
    outline: none;
}
QTableWidget::item { padding: 6px 8px; border: none; }
QTableWidget::item:selected {
    background-color: #313244;
    color: #89b4fa;
}
QTableWidget::indicator {
    width: 16px;
    height: 16px;
}
QTableWidget::indicator:unchecked {
    background-color: #1e1e2e;
    border: 1px solid #45475a;
    border-radius: 4px;
}
QTableWidget::indicator:checked {
    background-color: #89b4fa;
    border: 1px solid #89b4fa;
    border-radius: 4px;
}
QHeaderView::section {
    background-color: #1e1e2e;
    color: #6c7086;
    border: none;
    border-bottom: 1px solid #313244;
    padding: 6px 8px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

/* ── 테이블 셀 인라인 편집기 ── */
QAbstractItemView QLineEdit {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #89b4fa;
    border-radius: 3px;
    padding: 2px 6px;
    selection-background-color: #89b4fa;
    selection-color: #1e1e2e;
}

/* ── 상태바 ── */
QStatusBar {
    background-color: #181825;
    color: #6c7086;
    border-top: 1px solid #313244;
    font-size: 12px;
}
QStatusBar QLabel { color: #a6adc8; padding: 0 8px; }

/* ── 프로그레스바 ── */
QProgressBar {
    background-color: #313244;
    border: none;
    border-radius: 4px;
    height: 6px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 4px;
}

/* ── 다이얼로그 버튼박스 ── */
QDialogButtonBox QPushButton { min-width: 80px; }

/* ── 툴팁 ── */
QToolTip {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 4px 8px;
}

/* ── 사이드바 파일 패널 ── */
QWidget#sidebar {
    background-color: #181825;
    border-right: 1px solid #313244;
}

/* ── 섹션 레이블 ── */
QLabel#section_label {
    color: #6c7086;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 4px 0 2px 0;
}

/* ── 결과 값 레이블 ── */
QLabel#result_value {
    background-color: #313244;
    color: #a6e3a1;
    border-radius: 4px;
    padding: 3px 8px;
    font-family: Menlo, Monaco;
    font-size: 12px;
}
"""
