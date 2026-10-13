from __future__ import annotations

import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import traceback
from pathlib import Path

from . import __version__


def _run_app() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from .app import LOCAL_ROOT, MainWindow

    # Failures the app survives by falling back -- a Windows query that errors, a
    # runtime file that will not parse -- are logged at debug level and dropped. Setting
    # this keeps them, for a bug report.
    if os.environ.get("VIRTUAL_HDR_OSD_TRACE"):
        LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            handlers=[RotatingFileHandler(LOCAL_ROOT / "trace.log", maxBytes=1048576, backupCount=1, encoding="utf-8")],
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(message)s",
        )
        logging.getLogger(__name__).debug("Virtual HDR OSD for Windows %s", __version__)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Virtual HDR OSD for Windows")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Local Display Tools")
    window = MainWindow()
    window.show()
    return app.exec()


def _report_startup_failure() -> None:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Virtual_HDR_OSD_for_Windows"
    try:
        root.mkdir(parents=True, exist_ok=True)
        log_path = root / "startup_error.log"
        # The version first: a traceback without it cannot be matched to the code it
        # came from, and this file is what gets pasted into a bug report.
        log_path.write_text(
            f"Virtual HDR OSD for Windows {__version__}\n\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        details = f"The application could not start.\n\nDiagnostic log:\n{log_path}"
    except OSError:
        details = "The application could not start. Run Install.ps1 again and review its output."

    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                details,
                "Virtual HDR OSD for Windows",
                0x00000010,
            )
            return
        except Exception:
            pass
    print(details, file=sys.stderr)


def main() -> int:
    try:
        return _run_app()
    except Exception:
        _report_startup_failure()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
