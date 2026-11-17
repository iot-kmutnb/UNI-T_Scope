from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication

DARK = {
    "window": "#1e2126", "base": "#15181c", "text": "#e6e8eb", "button": "#2b3038",
    "mid": "#3a404a", "accent": "#3b9eff", "muted": "#8d96a3",
    # scope face
    "face": "#000000", "grid": "#5a5f66", "grid_major": "#8a9099", "bar": "#202429",
    "bar_text": "#e8eaed", "ch1": "#00e5ff", "ch2": "#ffd400", "ok": "#2ecc71", "warn": "#f5a623", "bad": "#e5484d",
    "cur_x": "#ff6ad5", "cur_y": "#ff9f43", "stop": "#ff5a24",
}
LIGHT = {
    "window": "#f3f4f6", "base": "#ffffff", "text": "#1c1f24", "button": "#e6e8ec",
    "mid": "#c7ccd4", "accent": "#0a66d6", "muted": "#454d5a",
    "face": "#fbfbfb", "grid": "#b9bdc4", "grid_major": "#7d838c", "bar": "#e1e4e9",
    "bar_text": "#1c1f24", "ch1": "#00798f", "ch2": "#a66f00", "ok": "#1f9d57", "warn": "#c77700", "bad": "#d92d34",
    "cur_x": "#b0158a", "cur_y": "#b85c00", "stop": "#d63e0a",
}

_settings = QSettings("UNI-T-Scope", "ScopeGUI")


def settings():
    """The QSettings object holding the theme and the GUI preferences."""
    return _settings


def use_settings(s):
    """Use another settings store (the tests give it a temporary file)."""
    global _settings
    _settings = s


def saved_name():
    v = _settings.value("theme", "dark")
    return v if v in ("dark", "light") else "dark"


def colors(name):
    return DARK if name == "dark" else LIGHT


_base_font = None


def apply(app: QApplication, name):
    global _base_font
    c = colors(name)
    app.setStyle("Fusion")
    if _base_font is None:
        _base_font = QFont(app.font())
    font = QFont(_base_font)
    if name == "light":                       # thin text on a light background reads poorly
        font.setWeight(QFont.Weight.DemiBold)
    app.setFont(font)
    p = QPalette()
    q = QColor
    p.setColor(QPalette.ColorRole.Window, q(c["window"]))
    p.setColor(QPalette.ColorRole.WindowText, q(c["text"]))
    p.setColor(QPalette.ColorRole.Base, q(c["base"]))
    p.setColor(QPalette.ColorRole.AlternateBase, q(c["window"]))
    p.setColor(QPalette.ColorRole.Text, q(c["text"]))
    p.setColor(QPalette.ColorRole.Button, q(c["button"]))
    p.setColor(QPalette.ColorRole.ButtonText, q(c["text"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, q(c["base"]))
    p.setColor(QPalette.ColorRole.ToolTipText, q(c["text"]))
    p.setColor(QPalette.ColorRole.Highlight, q(c["accent"]))
    p.setColor(QPalette.ColorRole.HighlightedText, q("#ffffff"))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, q(c["muted"]))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, q(c["muted"]))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, q(c["muted"]))
    app.setPalette(p)
    app.setStyleSheet(f"""
        QGroupBox {{ border: 1px solid {c['mid']}; border-radius: 6px; margin-top: 14px; padding: 10px 8px 8px 8px; font-weight: 600; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['muted']}; }}
        QPushButton {{ border: 1px solid {c['mid']}; border-radius: 5px; padding: 6px 12px; background: {c['button']}; }}
        QPushButton:hover {{ border-color: {c['accent']}; }}
        QPushButton:disabled {{ color: {c['muted']}; }}
        QPushButton:checked {{ background: {c['accent']}; color: #ffffff; border-color: {c['accent']}; }}
        QPushButton#chip {{ padding: 4px 10px; }}
        QPushButton#connectBtn {{ padding: 9px 12px; font-weight: 600; }}
        QPushButton#connectBtn:checked {{ background: {c['ok']}; border-color: {c['ok']}; }}
        QPushButton#runBtn[action="run"]:enabled {{ background: {c['ok']}; border-color: {c['ok']}; color: #101214; font-weight: 600; }}
        QPushButton#runBtn[action="stop"]:enabled {{ background: {c['stop']}; border-color: {c['stop']}; color: #ffffff; font-weight: 600; }}
        QStatusBar {{ color: {c['muted']}; }}
        QLabel#muted {{ color: {c['muted']}; }}
        QTableWidget {{ background: {c['base']}; color: {c['text']}; gridline-color: {c['mid']};
                       border: 1px solid {c['mid']}; }}
        QTableWidget::item {{ padding: 0 10px; }}
        QHeaderView {{ background: {c['button']}; }}
        QHeaderView::section {{ background: {c['button']}; color: {c['text']}; font-weight: 600; padding: 0 10px;
                               border: 0; border-right: 1px solid {c['mid']}; border-bottom: 1px solid {c['mid']}; }}
    """)
    _settings.setValue("theme", name)
