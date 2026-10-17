"""Cortar el trabajo en curso sin esperar a las páginas en vuelo.

La cancelación ordenada y el cierre ordenado dejan terminar lo que ya se
estaba leyendo. Con páginas grandes eso tarda, y la ventana parecía colgada.
Aquí se comprueba la salida rápida: que exista, que avise antes de usarla y
que no destruya ningún hilo para conseguirla.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from app.core import pipeline
from app.gui import main_window
from app.gui.main_window import MainWindow


class HiloFalso:
    """Un trabajador que dice estar corriendo hasta que lo cortan."""

    def __init__(self) -> None:
        self.interrumpido = False
        self.corriendo = True
        self.esperas: list[int] = []

    def isRunning(self) -> bool:  # noqa: N802 - API Qt
        return self.corriendo

    def requestInterruption(self) -> None:  # noqa: N802 - API Qt
        self.interrumpido = True

    def wait(self, ms: int) -> bool:  # noqa: N802 - API Qt
        self.esperas.append(ms)
        self.corriendo = False
        return True


class PoolFalso:
    def __init__(self) -> None:
        self.abortado = False

    def abortar(self) -> None:
        self.abortado = True


def test_cortar_rompe_los_pools_y_no_destruye_los_hilos(window, monkeypatch):
    """El corte va contra el OCR, no contra el QThread: matarlo aborta el proceso."""
    hilo = HiloFalso()
    monkeypatch.setattr(window, "_running_workers", lambda: [hilo])
    pool = PoolFalso()
    monkeypatch.setattr(pipeline, "_POOLS_VIVOS", {pool})

    window._cortar_trabajo_en_curso()

    assert window._forzado
    assert hilo.interrumpido, "al cortar se pide antes la parada ordenada"
    assert pool.abortado, "el corte rompe el pool que tiene esperando al hilo"
    assert hilo.corriendo, "el hilo termina solo; no se destruye desde fuera"


def test_la_segunda_cancelacion_ofrece_cortar(window, monkeypatch):
    hilo = HiloFalso()
    window._worker = hilo
    # Lo que deja el arranque de un procesamiento: botón disponible y
    # ninguna cancelación pedida todavía.
    window._rearmar_cancelar()
    preguntas: list[str] = []

    def responder(*args, **kwargs):
        preguntas.append(args[1])
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(main_window.QMessageBox, "warning", responder)

    window._request_cancel()

    assert hilo.interrumpido
    assert window._cancel_pedido
    assert window.btn_cancel.isEnabled(), (
        "el botón sigue disponible: la segunda pulsación es la que corta"
    )
    assert not preguntas, "la primera cancelación no pregunta nada"

    window._request_cancel()

    assert preguntas, "la segunda pulsación ofrece cortar"
    assert "Cancelar a la fuerza" in preguntas[0]
    assert not window._forzado, "se dijo que no; no se corta nada"


def test_al_empezar_otro_trabajo_el_boton_vuelve_a_cancelar(window):
    window._cancel_pedido = True
    window.btn_cancel.setText("Cancelar sin esperar")

    window._rearmar_cancelar()

    assert not window._cancel_pedido
    assert window.btn_cancel.text() == "Cancelar"
    assert window.btn_cancel.isEnabled()


def test_cerrar_dos_veces_ofrece_cortar_la_espera(app, monkeypatch):
    """El primer cierre espera; el segundo pregunta si hace falta seguir esperando."""
    window = MainWindow()
    try:
        hilo = HiloFalso()
        # Es el OCR: solo con paginas en vuelo tiene sentido ofrecer cortar.
        window._worker = hilo
        monkeypatch.setattr(window, "_running_workers", lambda: [hilo])
        preguntas: list[str] = []

        def responder(*args, **kwargs):
            preguntas.append(args[1])
            return QMessageBox.StandardButton.No

        monkeypatch.setattr(main_window.QMessageBox, "warning", responder)

        window.close()

        assert window._closing
        assert not preguntas, "el primer cierre espera sin preguntar"
        assert "Vuelva a cerrar" in window.status_label.text()

        window.close()

        assert preguntas, "el segundo cierre ofrece cortar"
        assert "Cerrar a la fuerza" in preguntas[0]
    finally:
        window._shutdown_timer.stop()
        window._teardown()
        app.processEvents()
