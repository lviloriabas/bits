"""Panel de avance por archivo de la ventana principal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from app.gui.main_window import MainWindow
from app.gui.tokens import paleta
from app.models.schemas import PageResult, ValidationReport


def _window() -> MainWindow:
    QApplication.instance() or QApplication([])
    window = MainWindow()
    window._file_page_counts = [50, 30]
    window._prepare_file_rows([Path("primera.pdf"), Path("segunda.pdf")])
    return window


def test_rows_list_the_whole_batch_with_its_pages_before_starting():
    window = _window()
    try:
        assert not window.times_scroll.isHidden()
        assert window.empty_times_label.isHidden()
        assert len(window._file_rows) == 2
        assert window._file_rows[0]["name"].text() == "primera.pdf"
        assert window._file_rows[0]["pages"].text() == "0/50 pág."
        assert window._file_rows[1]["pages"].text() == "0/30 pág."
        assert window._file_rows[0]["bar"].value() == 0
    finally:
        window.close()


def test_the_bar_shows_the_percentage_of_the_file():
    window = _window()
    try:
        bar = window._file_rows[0]["bar"]
        window.show()
        QApplication.processEvents()
        assert bar.height() == 20
        assert bar.isTextVisible()
        assert "%p" in bar.format()
        window._on_file_progress(1, 25, 50)
        assert bar.value() == 50
        assert window._file_rows[0]["pages"].text() == "25/50 pág."
    finally:
        window.close()


def test_file_progress_is_one_aligned_surface():
    window = _window()
    try:
        window.show()
        QApplication.processEvents()
        margins = window.times_pane.layout().contentsMargins()
        assert (
            margins.left(),
            margins.top(),
            margins.right(),
            margins.bottom(),
        ) == (10, 8, 10, 8)
        assert window.file_progress_title.x() == window.preview_context_label.x()
        assert (
            window.times_scroll.viewport().palette().color(
                QPalette.ColorRole.Base
            )
            == QColor(paleta().TABLE_BASE_BG)
        )
    finally:
        window.close()


def test_each_file_advances_on_its_own_row():
    """Con varios PDF en vuelo el avance de uno no puede llenar al otro.

    El contador global se repartía en orden de archivo: al procesar cuatro a
    la vez se llenaban las primeras filas mientras avanzaban las últimas.
    """
    window = _window()
    try:
        window._on_file_progress(2, 15, 30)
        assert window._file_rows[1]["bar"].value() == 50
        assert window._file_rows[0]["bar"].value() == 0
        assert window._file_rows[0]["pages"].text() == "0/50 pág."
    finally:
        window.close()


def test_progress_without_a_page_total_uses_the_counted_pages():
    window = _window()
    try:
        window._on_file_progress(1, 10, 0)
        assert window._file_rows[0]["pages"].text() == "10/50 pág."
        assert window._file_rows[0]["bar"].value() == 20
    finally:
        window.close()


def test_clearing_the_results_leaves_the_screen_empty():
    """La pantalla se limpia sin tocar lo guardado en output/."""
    window = _window()
    try:
        window.table.setColumnCount(2)
        window.table.setRowCount(5)
        window._clear_results_display()
        assert window.table.rowCount() == 0
        assert window.table.columnCount() == 0
        assert window._file_rows == {}
        assert window.times_scroll.isHidden()
        assert window.empty_times_label.isVisible() or window.isHidden()
    finally:
        window.close()


def _processed_window() -> MainWindow:
    window = _window()
    window._reports = [
        ValidationReport(
            pdf_path="primera.pdf",
            template_name="fixture",
            pages=[PageResult(page_number=1)],
        )
    ]
    return window


def test_processing_again_asks_before_discarding_what_is_on_screen():
    window = _processed_window()
    try:
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ) as question:
            assert window._confirm_discard_results() is False
        question.assert_called_once()
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ):
            assert window._confirm_discard_results() is True
    finally:
        window.close()


def test_the_first_run_does_not_ask_anything():
    window = _window()
    try:
        with patch.object(QMessageBox, "question") as question:
            assert window._confirm_discard_results() is True
        question.assert_not_called()
    finally:
        window.close()
