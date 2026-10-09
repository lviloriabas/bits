#!/usr/bin/env python3
"""Punto de entrada del editor visual de plantillas."""

from __future__ import annotations

import os
import sys
from functools import partial
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from app.utils.portable import ensure_portable_env

ensure_portable_env()
os.chdir(_ROOT)

from app.branding import APPLICATION_DISPLAY_NAME, APPLICATION_NAME
from app.utils.logging import setup_logging
from app.utils.app_identity import set_windows_taskbar_icon


def main() -> int:
    setup_logging(Path("output") / "logs")
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from app.gui.editor_window import EditorWindow
        from app.gui.theme import install_application_theme
    except ImportError as exc:
        from loguru import logger
        logger.opt(exception=exc).debug("No se pudo cargar el editor de BITS")
        print("La carpeta de BITS está incompleta. Copie de nuevo la carpeta completa.",
              file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName(f"{APPLICATION_NAME} - Editor de plantillas")
    app.setApplicationDisplayName(
        f"{APPLICATION_DISPLAY_NAME} - Editor de plantillas"
    )
    app.setOrganizationName("BITS")
    install_application_theme(app)
    root = Path(__file__).resolve().parent
    icon = root / "assets" / "icon.ico"
    if not icon.exists():
        icon = root / "assets" / "icon.png"
    from PySide6.QtGui import QIcon

    app_icon = QIcon(str(icon))
    app.setWindowIcon(app_icon)
    window = EditorWindow()
    window.setWindowIcon(app_icon)
    window.show()
    set_windows_taskbar_icon(window, icon)
    QTimer.singleShot(
        250,
        partial(set_windows_taskbar_icon, window, icon),
    )
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
