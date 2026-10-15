"""El arranque real con pythonw conserva el registro y puede cerrar con Qt."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("sin_consola", [False, True])
def test_arranque_y_cierre_con_interprete_portable(tmp_path, sin_consola):
    interprete = Path(sys.executable)
    if sin_consola:
        if os.name != "nt":
            pytest.skip("pythonw solo existe en Windows")
        interprete = interprete.with_name("pythonw.exe")
    raiz = Path(__file__).resolve().parents[1]
    guion = tmp_path / "arranque.py"
    resultado = tmp_path / "resultado.json"
    guion.write_text(
        f"""
import json, sys
from pathlib import Path
sys.path.insert(0, {str(raiz)!r})
import run_gui
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from app.gui.main_window import MainWindow
from app.utils import preferencias_ui
from app.utils.logging import setup_logging
from loguru import logger
carpeta = Path({str(tmp_path)!r})
preferencias_ui.ruta_preferencias = lambda raiz=None: carpeta / 'interfaz.json'
run_gui.setup_logging = lambda _ruta: setup_logging(carpeta / 'logs')
MainWindow.load_initial_data = lambda self: None
MainWindow.mostrar_novedades_instaladas = lambda self: None
MainWindow.vigilar_actualizaciones = lambda self: None
ejecutar = QApplication.exec
def ejecutar_y_cerrar(self):
    QTimer.singleShot(100, self.closeAllWindows)
    return ejecutar()
QApplication.exec = ejecutar_y_cerrar
codigo = run_gui.main()
logger.complete()
(carpeta / 'resultado.json').write_text(json.dumps({{
    'codigo': codigo, 'sin_consola': sys.stderr is None,
}}))
""",
        encoding="utf-8",
    )
    inicio = None
    if sin_consola:
        inicio = subprocess.STARTUPINFO()
        inicio.dwFlags |= subprocess.STARTF_USESTDHANDLES
        inicio.hStdInput = inicio.hStdOutput = inicio.hStdError = 0
    proceso = subprocess.run(
        [str(interprete), "-B", str(guion)], cwd=raiz,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        capture_output=not sin_consola, timeout=20, startupinfo=inicio,
    )
    assert proceso.returncode == 0, (proceso.stderr or b"").decode(errors="replace")
    assert json.loads(resultado.read_text()) == {
        "codigo": 0, "sin_consola": sin_consola,
    }
    assert any("GUI iniciada" in ruta.read_text(encoding="utf-8")
               for ruta in (tmp_path / "logs").glob("*.log"))
