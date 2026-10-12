"""Lista lateral de batches con seleccion multiple y acciones de la cola."""

from PySide6.QtCore import QItemSelectionModel, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (QAbstractItemView, QListView, QListWidget,
                               QListWidgetItem, QStyledItemDelegate)

from app.gui.tokens import SPACE_S


class _TarjetaBatchDelegate(QStyledItemDelegate):
    """Reserva el alto de todas las lineas cuando el nombre se parte."""

    def sizeHint(self, option, index):
        self.initStyleOption(option, index)
        lista = self.parent()
        ancho = max(1, lista.viewport().width() - 2 * lista.spacing())
        texto = option.fontMetrics.boundingRect(
            QRect(0, 0, max(1, ancho - 2 * (SPACE_S + 1)), 100000),
            Qt.TextFlag.TextWordWrap, option.text,
        )
        return QSize(ancho, texto.height() + 2 * (SPACE_S + 1))


def agregar_filtros_al_menu(menu, controles):
    """Refleja las casillas del panel sin mantener otra copia de su estado."""
    acciones = []
    for control in controles:
        accion = menu.addAction(control.text())
        accion.setCheckable(True)
        accion.setChecked(control.isChecked())
        accion.setToolTip(control.toolTip())
        accion.toggled.connect(control.setChecked)
        control.toggled.connect(accion.setChecked)
        acciones.append(accion)
    return acciones


class BatchesSidebar(QListWidget):
    """Presenta cada batch en tres lineas, sin cabeceras ni columnas visibles.

    Conserva el acceso a los datos por campo que usan las acciones de la cola.
    La seleccion pertenece al modelo de lista, con una sola columna.
    """

    def __init__(self):
        super().__init__()
        self._campos = []
        self.setObjectName("batchesSidebar")
        self.setItemDelegate(_TarjetaBatchDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setWordWrap(True)
        self.setMouseTracking(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(3)
        self.setMinimumWidth(205)

    def rowCount(self):
        return self.count()

    def columnCount(self):
        return 1

    def rowAt(self, y):
        return self.indexAt(QPoint(self.viewport().width() // 2, y)).row()

    def selectRow(self, row):
        self.setCurrentRow(row, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def setRowCount(self, count):
        if count == 0:
            self.clear()
            self._campos.clear()

    def insertRow(self, row):
        self.insertItem(row, QListWidgetItem())
        self._campos.insert(row, [None] * 4)

    def item(self, row, column=None):
        if column is None:
            return super().item(row)
        return self._campos[row][column]

    def setItem(self, row, column, item):
        self._campos[row][column] = item
        if column != 3:
            return
        ident, nombre, paginas, estado = self._campos[row]
        tarjeta = super().item(row)
        tarjeta.setText(
            f"{nombre.text()}\n"
            f"{paginas.text()} páginas" + (f", ID {ident.text()}" if ident.text() else "")
            + f"\n{estado.text()}"
        )
        tarjeta.setToolTip(nombre.text() + "\n" + estado.toolTip())
        tarjeta.setForeground(estado.foreground())

    def sizeHintForColumn(self, column):
        return self.minimumWidth()

    def rowHeight(self, row):
        return self.sizeHintForRow(row)

    def setColumnWidth(self, column, width):
        pass
