"""La ventana pinta el avance del batch, no el del documento en curso.

El par que llega en la señal es del PDF abierto (o de los archivos ya
vistos): la barra encogía al cambiar de archivo y el texto anunciaba "52 de
100" cuando el usuario iba por la 152 de 300. Y con una docena de páginas en
vuelo los avisos llegan desordenados, así que el contador solo puede subir.
"""

from __future__ import annotations

from pathlib import Path
import time

from PySide6.QtWidgets import QApplication

from app.core.progress import PAGES_STAGE, VOID_STAGE
from app.gui.main_window import MainWindow
from app.gui.worker import PipelineWorker


def _window() -> MainWindow:
    QApplication.instance() or QApplication([])
    return MainWindow()


def test_the_bar_and_the_text_count_the_pages_of_the_batch():
    window = _window()
    try:
        window._total_global = 300
        window._last_done = 0
        # Lo que emite el pipeline del segundo archivo: 52 de sus 100.
        window._on_progress(152, 100, f"Archivo 2/3: b.pdf - {PAGES_STAGE}")
        assert window.progress.maximum() == 300
        assert window.progress.value() == 152
        assert window.status_label.text() == (
            f"Archivo 2/3: b.pdf - {PAGES_STAGE} 152/300"
        )
    finally:
        window._teardown()


def test_the_counter_never_goes_back():
    window = _window()
    try:
        window._total_global = 300
        window._last_done = 0
        window._on_progress(152, 300, PAGES_STAGE)
        window._on_progress(148, 300, PAGES_STAGE)
        assert window.progress.value() == 152
        assert window.status_label.text() == f"{PAGES_STAGE} 152/300"
        assert window._done_global == 152
        window._on_progress(153, 300, PAGES_STAGE)
        assert window.progress.value() == 153
    finally:
        window._teardown()


def test_other_stages_keep_their_own_text():
    window = _window()
    try:
        window._total_global = 300
        window._last_done = 0
        window._on_progress(300, 300, "Generando reporte")
        assert window.status_label.text() == "Generando reporte"
    finally:
        window._teardown()


def test_the_worker_offset_survives_a_stage_with_a_smaller_total():
    """La revisión de firmas informa con las páginas leídas, no con el tramo.

    Sumar el desplazamiento otra vez al ver ese total más pequeño adelantaba
    el contador global de golpe (y con la barra fija, lo dejaba al 100 %).
    """
    QApplication.instance() or QApplication([])
    worker = PipelineWorker(
        [Path("a.pdf"), Path("b.pdf")], Path("t.json"), None
    )
    updates: list[tuple[int, int]] = []
    worker.progress.connect(lambda done, total, _m: updates.append((done, total)))

    worker._current_file_index = 1
    worker._on_progress(0, 50, PAGES_STAGE)
    worker._on_progress(30, 50, PAGES_STAGE)
    # Etapa que cuenta solo las páginas leídas de una bitácora cancelada.
    worker._on_progress(30, 30, "Contrastando firmas inciertas con el libro")
    worker._current_file_index = 2
    worker._on_progress(0, 40, PAGES_STAGE)
    worker._on_progress(10, 40, PAGES_STAGE)

    assert [done for done, _total in updates] == [0, 30, 30, 50, 60]
    assert [total for _done, total in updates] == [50, 50, 50, 90, 90]


def test_the_worker_sends_the_void_stage_apart_from_the_pages():
    QApplication.instance() or QApplication([])
    worker = PipelineWorker([Path("a.pdf")], Path("t.json"), None)
    updates: list[tuple[int, int, str]] = []
    reviews: list[tuple[int, int, int]] = []
    files: list[tuple[int, int, int]] = []
    worker.progress.connect(lambda *args: updates.append(args))
    worker.review_progress.connect(lambda *args: reviews.append(args))
    worker.file_progress.connect(lambda *args: files.append(args))

    worker._current_file_index = 1
    worker._on_progress(50, 50, PAGES_STAGE)
    worker._on_progress(1, 4, VOID_STAGE)

    assert updates[-1] == (
        50, 50, f"Archivo 1/1: a.pdf - {VOID_STAGE} 1/4"
    )
    assert reviews == [(1, 1, 4)]
    assert files == [(1, 50, 50)]


def test_void_is_reserved_in_the_bar_and_in_the_remaining_time():
    """La barra no se llena ni el restante cae a cero con VOID pendiente."""
    window = _window()
    try:
        window._total_global = 50
        window._file_page_counts = [50]
        window._reset_review_progress(reserve=True)
        window._review_weight = 1.0
        window._review_ratio = 0.5
        window._last_done = 0

        window._on_progress(50, 50, PAGES_STAGE)
        assert window.progress.maximum() == 75
        assert window.progress.value() == 50

        window._on_review_progress(1, 5, 20)
        assert window.progress.maximum() == 70
        assert window.progress.value() == 55

        window._run_started = time.monotonic() - 55
        window._on_timer_tick()
        assert window.time_labels["remaining"].text() != "00:00:00"

        window._on_progress(40, 50, PAGES_STAGE)
        assert window.progress.value() == 55
    finally:
        window._run_started = None
        window._teardown()
