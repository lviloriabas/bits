"""Cierre ordenado y coste de refrescar la ventana principal."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event, get_ident

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication

from app.gui.main_window import _TABLE_CELL_CHUNK, MainWindow


class _InterruptibleWorker(QThread):
    """Worker que respeta la petición de parada, como los del pipeline."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.stopped_on_request = False

    def run(self) -> None:
        for _ in range(500):
            if self.isInterruptionRequested():
                self.stopped_on_request = True
                return
            self.msleep(10)


def _until(app, condition, timeout: float = 10.0) -> bool:
    """Deja correr la cola de eventos hasta que se cumpla ``condition``."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        QThread.msleep(5)
    app.processEvents()
    return condition()


def test_closing_with_work_in_flight_stops_it_instead_of_destroying_the_thread():
    """Cerrar durante una ejecución mataba el programa (0xC0000409).

    Qt aborta el proceso si se destruye un ``QThread`` en marcha, y el cierre
    no detenía ni el OCR, ni el preprocesado, ni la generación de salidas.
    Ahora el cierre pide la parada y solo se completa cuando ya no queda
    ningún hilo corriendo.
    """
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    worker = _InterruptibleWorker(window)
    window._outputs_worker = worker
    worker.start()
    assert _until(app, worker.isRunning)

    window.show()
    window.close()

    # El cierre se aplaza: la ventana sigue viva mientras el hilo termina.
    assert window.isVisible()
    assert window._closing

    assert _until(app, lambda: not worker.isRunning())
    # El hilo salió por la petición de parada, no porque agotara su trabajo.
    assert worker.stopped_on_request
    assert _until(app, lambda: not window.isVisible())
    assert not window._running_workers()


def test_closing_without_work_in_flight_closes_straight_away():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()

    assert window.close()

    assert not window.isVisible()
    assert not window._closing
    app.processEvents()


def test_a_closing_window_does_not_queue_another_export():
    """Una exportación en cola dejaría el cierre sin terminar nunca."""
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._reports = []
    window.btn_process.setEnabled(False)
    window.btn_preprocess.setEnabled(False)
    window._closing = True
    window._pending_export = True
    window._pending_csv_refresh = True

    window._on_outputs_thread_finished()

    assert not window._pending_export
    assert not window._pending_csv_refresh
    # Los botones siguen bloqueados: reactivarlos a medio cerrar invitaría a
    # lanzar trabajo nuevo justo cuando la ventana se está yendo.
    assert not window.btn_process.isEnabled()
    assert not window.btn_preprocess.isEnabled()
    window.close()
    app.processEvents()


class _WaitingWorker(QThread):
    """Simula una consulta de Git que solo termina al recibir la respuesta."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.liberar = Event()

    def run(self):
        self.liberar.wait(2)


def test_el_cierre_no_espera_la_consulta_de_version_nueva(
    window, app, monkeypatch,
):
    """Con la red mala, ``git fetch`` tarda hasta su limite.

    El cierre lo esperaba y decia que terminaba paginas sin haber ninguna.
    """
    from app.gui import actualizacion

    liberar = Event()
    terminada = Event()

    def consultar(_raiz):
        liberar.wait(5)
        terminada.set()
        return 3

    monkeypatch.setattr(actualizacion, "commits_pendientes", consultar)
    ajeno = _WaitingWorker()
    ajeno.start()
    window.show()
    try:
        window.vigilar_actualizaciones()
        assert window._consulta_actualizacion.en_curso()
        inicio = time.monotonic()
        window.close()
        assert time.monotonic() - inicio < .5
        assert not window.isVisible()
        assert not window._closing
        assert window._consulta_actualizacion.en_curso()
        assert ajeno.isRunning()
        assert not ajeno.isInterruptionRequested()
    finally:
        liberar.set()
        ajeno.liberar.set()
        ajeno.wait()
    # Git termina solo despues, y su respuesta ya no toca la ventana.
    assert terminada.wait(2)
    window._recoger_actualizacion()
    assert window.btn_actualizar.isHidden()


def test_la_respuesta_de_version_nueva_llega_sin_bloquear(
    window, app, monkeypatch,
):
    from app.gui import actualizacion

    monkeypatch.setattr(actualizacion, "commits_pendientes", lambda _raiz: 2)
    window.show()
    try:
        window.vigilar_actualizaciones()
        assert _until(app, lambda: not window._respuesta_actualizacion_timer.isActive())
        assert not window.btn_actualizar.isHidden()
    finally:
        window._actualizacion_timer.stop()


def test_sin_paginas_en_curso_el_cierre_no_habla_de_paginas(
    window, app, monkeypatch,
):
    limpieza = _WaitingWorker(window)
    window._browser_shutdown_worker = limpieza
    preguntas = []
    monkeypatch.setattr(
        window, "_confirmar_corte", lambda *a: preguntas.append(a) or True,
    )
    limpieza.start()
    window.show()
    try:
        window.close()
        assert window.isVisible()
        assert "páginas" not in window.status_label.text()
        # Volver a cerrar no ofrece cortar paginas que no existen.
        window.close()
        assert not preguntas
        QTimer.singleShot(20, limpieza.liberar.set)
        assert _until(app, lambda: not window.isVisible())
    finally:
        limpieza.liberar.set()
        limpieza.wait()


def test_con_ocr_en_curso_el_cierre_conserva_su_aviso(window, app, monkeypatch):
    ocr = _WaitingWorker(window)
    window._worker = ocr
    preguntas = []
    monkeypatch.setattr(
        window, "_confirmar_corte", lambda *a: preguntas.append(a) or False,
    )
    ocr.start()
    window.show()
    try:
        window.close()
        assert "páginas ya leídas" in window.status_label.text()
        window.close()
        assert len(preguntas) == 1
        QTimer.singleShot(20, ocr.liberar.set)
        assert _until(app, lambda: not window.isVisible())
    finally:
        ocr.liberar.set()
        ocr.wait()


def test_el_cierre_del_navegador_no_bloquea_qt(window, app, monkeypatch):
    from app.airvault import navegador

    liberar = Event()
    iniciada = Event()
    hilos = []

    def cerrar():
        hilos.append(get_ident())
        iniciada.set()
        liberar.wait(2)

    monkeypatch.setattr(navegador, "hay_navegadores_abiertos", lambda: True)
    monkeypatch.setattr(navegador, "cerrar_navegadores_al_salir", cerrar)
    window.show()
    try:
        inicio = time.monotonic()
        window.close()
        assert time.monotonic() - inicio < .5
        assert window.isVisible()
        assert iniciada.wait(1)
        QTimer.singleShot(20, liberar.set)
        assert _until(app, lambda: not window.isVisible())
        assert len(hilos) == 1
        assert hilos[0] != get_ident()
    finally:
        liberar.set()
        if window._browser_shutdown_worker is not None:
            window._browser_shutdown_worker.wait()


def test_cancela_la_sesion_y_libera_batches_antes_del_navegador(
    window, app, tmp_path, monkeypatch,
):
    from app.airvault import navegador
    from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker

    cancelada = Event()
    liberaciones = []
    orden = []

    class Sesion:
        def cancelar(self):
            cancelada.set()

    class Trabajo(TrabajoAirVaultWorker):
        def run(self):
            cancelada.wait(2)

    airvault = AirVaultWindow(tmp_path)
    trabajo = Trabajo("comprobar", {"sesion": Sesion()}, airvault)
    airvault._worker = trabajo
    window._airvault_windows.append(airvault)
    liberar = _WaitingWorker(airvault)

    def soltar(esperar=False):
        assert not esperar
        assert not trabajo.isRunning()
        liberaciones.append(True)
        airvault._soltando.append(liberar)
        liberar.start()

    def cerrar():
        assert liberar.liberar.is_set()
        assert not liberar.isRunning()
        orden.append("navegador")

    monkeypatch.setattr(airvault, "_soltar_lotes", soltar)
    monkeypatch.setattr(navegador, "hay_navegadores_abiertos", lambda: True)
    monkeypatch.setattr(navegador, "cerrar_navegadores_al_salir", cerrar)
    trabajo.start()
    window.show()
    try:
        window.close()
        assert trabajo._parar
        assert cancelada.is_set()
        assert _until(app, liberar.isRunning)
        assert window.isVisible()
        assert not orden
        QTimer.singleShot(20, liberar.liberar.set)
        assert _until(app, lambda: not window.isVisible())
        assert liberaciones == [True]
        assert orden == ["navegador"]
    finally:
        cancelada.set()
        liberar.liberar.set()
        trabajo.wait()
        liberar.wait()


def test_el_cierre_no_libera_el_mismo_batch_dos_veces(app, tmp_path, monkeypatch):
    from app.gui import airvault_window

    airvault = airvault_window.AirVaultWindow(tmp_path)
    liberar = Event()
    llamadas = []

    def soltar(trabajos, cliente):
        llamadas.append((trabajos, cliente))
        liberar.wait(2)

    monkeypatch.setattr(airvault_window, "_soltar", soltar)
    monkeypatch.setattr(airvault, "_actualizar_botones_proceso", lambda: None)
    airvault._deteniendo = True
    airvault._trabajos = [object()]
    airvault._estado = {"cliente": object()}
    try:
        # Simula el resultado de cancelacion y el sondeo del cierre juntos.
        airvault._soltar_lotes()
        airvault._soltar_lotes()
        assert len(airvault._soltando) == 1
        liberar.set()
        assert _until(app, lambda: not airvault._soltando)
        assert len(llamadas) == 1
    finally:
        liberar.set()
        for hilo in airvault._soltando:
            hilo.wait()


def test_the_table_chunk_is_a_budget_of_cells_not_of_rows():
    """Con presupuesto en filas, un CSV ancho bloqueaba medio segundo por tramo.

    El costo de llenar la tabla es por celda, así que 400 filas cuestan lo
    mismo que 34.000 celdas cuando la ejecución trae 85 columnas.
    """
    app = QApplication.instance() or QApplication([])
    window = MainWindow()

    window._table_columns = ["a", "b", "c"]
    narrow = window._rows_per_chunk()
    window._table_columns = [f"c{index}" for index in range(85)]
    wide = window._rows_per_chunk()

    assert narrow * 3 <= _TABLE_CELL_CHUNK
    assert wide * 85 <= _TABLE_CELL_CHUNK
    # Cuantas más columnas, menos filas por tramo: el tiempo de cada uno es el
    # mismo tenga la ejecución tres columnas o noventa.
    assert narrow > wide >= 1
    window.close()
    app.processEvents()


def test_the_preview_reuses_the_page_counts_of_the_dpi_pass(
    tmp_path: Path, monkeypatch
):
    """Contar las páginas otra vez abría el batch entero por segunda vez."""
    app = QApplication.instance() or QApplication([])
    from app.vision import pdf_loader

    opened: list[Path] = []
    monkeypatch.setattr(
        pdf_loader, "page_count", lambda path: opened.append(Path(path)) or 5
    )
    window = MainWindow()
    pdf = tmp_path / "libro.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    window._set_preview_documents([pdf], [393])

    assert window._preview_document_counts == [393]
    assert opened == []

    # Sin recuento previo sí se cuenta, que es lo que hace el visor de una
    # ejecución ya procesada.
    window._set_preview_documents([pdf])

    assert window._preview_document_counts == [5]
    assert opened == [pdf]
    window.close()
    app.processEvents()
