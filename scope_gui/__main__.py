import sys
import warnings

from PyQt6.QtWidgets import QApplication

from . import errors, theme
from .main_window import MainWindow

# NI-VISA reports 'config not loaded' when no config exists; defaults are fine.
warnings.filterwarnings("ignore", message=".*VI_WARN_CONFIG_NLOADED.*")


def main():
    app = QApplication(sys.argv)
    theme.apply(app, theme.saved_name())
    win = MainWindow(app)
    errors.install(app, win)      # an unexpected error is logged and shown, it no longer closes the program
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
