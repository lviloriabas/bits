"""Los mensajes visibles se pueden llevar al portapapeles."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
)

from app.gui.text_copy import CopyableListWidget, install_text_copy_support
from app.gui.widgets import ElidedLabel


def _app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    install_text_copy_support(app)
    return app


def test_las_etiquetas_de_la_interfaz_permiten_seleccionar_texto():
    app = _app()
    label = QLabel("Error que se necesita copiar")
    label.show()
    app.processEvents()

    assert (
        label.textInteractionFlags()
        & Qt.TextInteractionFlag.TextSelectableByMouse
    )


def test_un_mensaje_recortado_copia_el_texto_completo():
    app = _app()
    message = "Error completo que no cabe en la linea de estado"
    label = ElidedLabel(message)
    label.resize(80, label.sizeHint().height())
    label.show()
    label.setFocus()
    app.processEvents()

    QTest.keyClick(label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert QApplication.clipboard().text() == message


def test_la_bitacora_copia_varias_lineas_en_orden():
    _app()
    log = CopyableListWidget()
    log.setSelectionMode(CopyableListWidget.SelectionMode.ExtendedSelection)
    log.addItems(["10:00  Inicio", "10:01  Error", "10:02  Fin"])
    log.item(2).setSelected(True)
    log.item(1).setSelected(True)
    log.setFocus()

    QTest.keyClick(log, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert QApplication.clipboard().text() == "10:01  Error\n10:02  Fin"


def _tabla_por_filas(filas: tuple[tuple[str, ...], ...]) -> QTableWidget:
    """Una tabla como las del CSV: elegir una fila la elige entera."""
    table = QTableWidget(len(filas), len(filas[0]))
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    for fila, valores in enumerate(filas):
        for columna, valor in enumerate(valores):
            table.setItem(fila, columna, QTableWidgetItem(valor))
    return table


def test_el_csv_copia_la_celda_bajo_el_cursor_y_no_la_fila():
    """Se pulsa una celda para copiar ese dato, no los diez de su fila."""
    app = _app()
    table = _tabla_por_filas((("A320", "2312238"), ("B737", "2312239")))
    table.setCurrentCell(0, 1)
    table.show()
    app.processEvents()

    QApplication.clipboard().clear()
    QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert QApplication.clipboard().text() == "2312238"


def test_el_menu_del_csv_copia_la_celda_sobre_la_que_se_hizo_clic():
    """Y no la que estuviera activa de antes, que casi nunca es esa."""
    app = _app()
    table = _tabla_por_filas((("A320", "2312238"), ("B737", "2312239")))
    # Con menu propio: se comprueba que la celda sigue al clic sin abrir
    # ningun menu, que en una prueba se quedaria esperando para siempre.
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    table.setCurrentCell(0, 0)
    table.show()
    app.processEvents()

    viewport = table.viewport()
    destino = table.visualRect(table.model().index(1, 1)).center()
    app.sendEvent(
        viewport,
        QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse,
            destino,
            viewport.mapToGlobal(destino),
        ),
    )

    assert table.currentIndex().row() == 1
    assert table.currentIndex().column() == 1


def test_el_menu_sobre_una_fila_elegida_no_deshace_la_seleccion():
    """Con varias filas elegidas el menu es para todas, como en Windows.

    Fuera de la seleccion, en cambio, la fila del clic pasa a ser la unica:
    actuar sobre otras que quedaron resaltadas de antes seria un error.
    """
    app = _app()
    table = _tabla_por_filas(
        (("A320", "2312238"), ("B737", "2312239"), ("E190", "2312240"))
    )
    table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    table.show()
    app.processEvents()
    table.selectRow(0)
    table.selectionModel().select(
        table.model().index(1, 0),
        table.selectionModel().SelectionFlag.Select
        | table.selectionModel().SelectionFlag.Rows,
    )
    viewport = table.viewport()

    def menu_sobre(fila: int) -> None:
        # Como el raton: primero la pulsacion, que es la que elige, y
        # despues el menu.
        destino = table.visualRect(table.model().index(fila, 1)).center()
        QTest.mousePress(
            viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, destino
        )
        QTest.mouseRelease(
            viewport, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, destino
        )
        app.sendEvent(
            viewport,
            QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, destino, viewport.mapToGlobal(destino)
            ),
        )

    def elegidas() -> list[int]:
        return sorted(i.row() for i in table.selectionModel().selectedRows())

    menu_sobre(1)
    assert elegidas() == [0, 1]
    assert table.currentIndex().row() == 1

    menu_sobre(2)
    assert elegidas() == [2]


def test_una_tabla_que_elige_por_celdas_copia_el_bloque_elegido():
    """Ahi elegir varias es una decision, no un efecto de pulsar la fila."""
    app = _app()
    table = QTableWidget(1, 3)
    for columna, valor in enumerate(("A320", "oculta", "12")):
        table.setItem(0, columna, QTableWidgetItem(valor))
    table.setColumnHidden(1, True)
    table.selectRow(0)
    table.show()
    app.processEvents()

    QApplication.clipboard().clear()
    QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    # Y sigue omitiendo lo que la vista resumida esconde.
    assert QApplication.clipboard().text() == "A320\t12"


def test_una_tabla_con_menu_propio_conserva_su_menu():
    """El filtro no puede quedarse el clic derecho de la cola de AirVault."""
    app = _app()
    table = QTableWidget(1, 1)
    table.setItem(0, 0, QTableWidgetItem("batch"))
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    pedidos: list[object] = []
    table.customContextMenuRequested.connect(pedidos.append)
    table.show()
    app.processEvents()

    # Qt entrega el menu contextual al viewport, no a la tabla.
    viewport = table.viewport()
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        QPoint(5, 5),
        viewport.mapToGlobal(QPoint(5, 5)),
    )
    app.sendEvent(viewport, event)

    assert pedidos, "el menu propio de la tabla no llego a pedirse"
