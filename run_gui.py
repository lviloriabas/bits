#!/usr/bin/env python3
"""Punto de entrada de la GUI principal de BITS."""

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
from app.utils.app_identity import (
    set_windows_process_taskbar_identity,
    set_windows_taskbar_icon,
)


def main() -> int:
    # Antes de que Qt cree ninguna ventana nativa: cada proceso de la GUI
    # principal debe ocupar su propio item, aunque Windows combine botones.
    set_windows_process_taskbar_identity()
    setup_logging(Path("output") / "logs")
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from app.airvault.navegador import mantener_navegadores_hasta_el_cierre
        from app.gui.main_window import MainWindow
        from app.gui.text_copy import install_text_copy_support
        from app.gui.theme import install_application_theme
    except ImportError as exc:
        print("PySide6 no está instalado. Ejecute:\n"
              "  python -m pip install --user -r requirements.txt",
              file=sys.stderr)
        return 1

    from PySide6.QtGui import QIcon

    root = Path(__file__).resolve().parent
    icon = root / "assets" / "icon.ico"
    if not icon.exists():
        icon = root / "assets" / "icon.png"
    app = QApplication(sys.argv)
    app.setApplicationName(APPLICATION_NAME)
    app.setApplicationDisplayName(APPLICATION_DISPLAY_NAME)
    app.setOrganizationName("BITS")
    install_application_theme(app)
    install_text_copy_support(app)
    app_icon = QIcon(str(icon))
    app.setWindowIcon(app_icon)
    mantener_navegadores_hasta_el_cierre()
    window = MainWindow()
    window.setWindowIcon(app_icon)
    window.show()
    set_windows_taskbar_icon(window, icon)
    # Qt y el shell terminan de crear/asociar el botón de la barra de tareas
    # después de show(). Se reafirma entonces el icono de la ventana real.
    QTimer.singleShot(
        250,
        partial(set_windows_taskbar_icon, window, icon),
    )
    # Detectar PDFs y calcular la estimación puede tocar archivos grandes.
    # Se difiere para que la ventana sea visible inmediatamente.
    QTimer.singleShot(0, window.load_initial_data)
    # Pregunta al repositorio si hay versión nueva. Va en un hilo y solo
    # muestra el botón de actualizar si la copia local quedó atrás.
    QTimer.singleShot(0, window.vigilar_actualizaciones)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
