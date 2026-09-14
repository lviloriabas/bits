#! /usr/bin/env python3
"""Launcher de doble clic de BITS - Clasificación de Bitácoras.

Convierte a ``BITS.exe`` (via PyInstaller) un pequeño
lanzador que arranca la GUI con el Python portable incluido en la carpeta,
sin mostrar consola y sin instalar nada. Toda la aplicación vive dentro de
la misma carpeta; solo hay que copiar la carpeta completa a otra máquina.

Métodos de uso:
    - Doble clic en ``BITS.exe`` -> abre la GUI.
    - ``python launcher_gui.py`` -> equivalente durante el desarrollo.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from app.branding import APPLICATION_SHORT_NAME


def app_root() -> Path:
    """Raíz del proyecto: donde está el .exe (frozen) o este archivo."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _pythonw(root: Path) -> Path | None:
    """Ruta del intérprete sin consola (portable)."""
    base = root / "portable" / "python312" / "tools"
    if (base / "pythonw.exe").exists():
        return base / "pythonw.exe"
    if (base / "python.exe").exists():
        return base / "python.exe"
    return None


def _show_error(message: str) -> int:
    """Muestra un diálogo de error sin depender de la GUI de la app."""
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None, str(message), APPLICATION_SHORT_NAME, 0x10
    )
    return 1


def main() -> int:
    root = app_root()
    target = root / "run_gui.py"
    interpreter = _pythonw(root)

    if not target.exists():
        return _show_error(f"No se encontró: {target}")
    if interpreter is None:
        return _show_error(
            "No se encontró el Python portable en:\n"
            f"{root}\\portable\\python312\\tools\\"
        )

    proc = subprocess.Popen(
        [str(interpreter), str(target)],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Si el intérprete muere al instante con error, se informa al usuario.
    time.sleep(1.5)
    if proc.poll() is not None and proc.returncode != 0:
        return _show_error(
            "La aplicación no pudo iniciarse (código "
            f"{proc.returncode}).\n\n"
            "Pruebe a ejecutar desde la consola:\n"
            f'"{interpreter}" "{target}"'
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
