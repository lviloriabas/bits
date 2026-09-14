"""La fila seleccionada se pinta como una sola banda azul.

El estilo nativo de Windows 11 dibuja la selección con un rectángulo
redondeado por celda, separado del vecino: la fila se veía como una hilera de
cuadros azules sueltos. La prueba renderiza la tabla y comprueba que el azul
llega entero de un extremo al otro, sin las esquinas redondas ni los huecos de
ese estilo; lo único que puede cortarlo es la línea de rejilla de 1 px que la
tabla dibuja en todas sus filas.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QStyleFactory, QTableWidget, QTableWidgetItem

from app.gui.tokens import accent_color, paleta
from app.gui.widgets import (
    data_table_qss,
    FlatSelectionDelegate,
    style_data_table,
)
from app.gui.depuracion_dialog import _arbol_qss

_COLUMNS = 5
_ROWS = 4


def test_todas_las_tablas_usan_el_borde_gris_claro():
    borde = f"border: 1px solid {paleta().PANE_BORDER}"
    assert borde in data_table_qss()
    assert borde in _arbol_qss()


def _table() -> QTableWidget:
    table = QTableWidget(_ROWS, _COLUMNS)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setStyleSheet(data_table_qss())
    style_data_table(table)
    for row in range(_ROWS):
        for column in range(_COLUMNS):
            # Sin texto: los píxeles de las letras no son huecos de la banda.
            table.setItem(row, column, QTableWidgetItem(""))
    table.resize(QSize(500, 200))
    return table


def _selection_scanlines(image: QImage) -> dict[int, list[tuple[int, int]]]:
    """Tramos horizontales del color de selección, por línea de la imagen."""
    target = QColor(accent_color()).rgb()
    scanlines: dict[int, list[tuple[int, int]]] = {}
    for y in range(image.height()):
        runs: list[tuple[int, int]] = []
        start = None
        for x in range(image.width()):
            if image.pixel(x, y) == target:
                if start is None:
                    start = x
            elif start is not None:
                runs.append((start, x - 1))
                start = None
        if start is not None:
            runs.append((start, image.width() - 1))
        if runs:
            scanlines[y] = runs
    return scanlines


def test_selected_row_is_one_continuous_band(tmp_path):
    app = QApplication.instance() or QApplication([])
    # El estilo real de la aplicación en Windows; offscreen arranca en Fusion.
    previous = app.style().objectName()
    if "windows11" in QStyleFactory.keys():
        app.setStyle("windows11")
    table = _table()
    try:
        table.show()
        app.processEvents()
        table.selectRow(1)
        app.processEvents()

        image = QImage(table.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        table.render(image)
        scanlines = _selection_scanlines(image)

        assert scanlines, "la fila seleccionada no se pintó"
        widths = {
            sum(end - start + 1 for start, end in runs)
            for runs in scanlines.values()
        }
        # Una banda recta mide lo mismo en todas sus líneas; las esquinas
        # redondeadas del estilo nativo dejaban las primeras más cortas.
        assert len(widths) == 1
        for runs in scanlines.values():
            gaps = [
                runs[index + 1][0] - runs[index][1] - 1
                for index in range(len(runs) - 1)
            ]
            # Solo la rejilla de la tabla puede cortar el azul.
            assert all(gap == 1 for gap in gaps), gaps
    finally:
        table.close()
        app.setStyle(previous)
        app.processEvents()


def test_data_tables_paint_their_own_selection():
    app = QApplication.instance() or QApplication([])
    table = _table()
    try:
        assert isinstance(table.itemDelegate(), FlatSelectionDelegate)
    finally:
        table.close()
        app.processEvents()


def test_a_cell_without_item_does_not_cut_the_band():
    """La columna vacía de la tabla de batches no puede partir el azul.

    La banda se pintaba a partir del estado que llegaba en cada celda. Una
    celda sin ``QTableWidgetItem`` (el ID de un batch que todavía no lo
    tiene) podía quedarse fuera y dejar la fila con el azul empezando en la
    segunda columna. Ahora quien decide es la selección de la vista, así
    que la fila entera se pinta tenga ítems o no.
    """
    from PySide6.QtWidgets import QStyleOptionViewItem

    app = QApplication.instance() or QApplication([])
    table = _table()
    try:
        table.setItem(1, 0, None)
        table.selectRow(1)
        delegado = table.itemDelegate()
        fondos = []
        for columna in range(_COLUMNS):
            option = QStyleOptionViewItem()
            option.initFrom(table)
            delegado.initStyleOption(option, table.model().index(1, columna))
            fondos.append(option.backgroundBrush.color().name())

        assert fondos == [QColor(accent_color()).name()] * _COLUMNS
    finally:
        table.close()
        app.processEvents()


def test_an_unselected_row_keeps_its_own_colour():
    app = QApplication.instance() or QApplication([])
    table = _table()
    try:
        table.selectRow(1)
        from PySide6.QtWidgets import QStyleOptionViewItem

        option = QStyleOptionViewItem()
        option.initFrom(table)
        table.itemDelegate().initStyleOption(
            option, table.model().index(2, 0)
        )

        assert option.backgroundBrush.color().name() != QColor(
            accent_color()
        ).name()
    finally:
        table.close()
        app.processEvents()
