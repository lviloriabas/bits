"""Lista lateral de batches con seleccion multiple y acciones de la cola."""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem


class BatchesSidebar(QListWidget):
    """Presenta cada batch en tres lineas, sin cabeceras ni columnas visibles.

    Conserva el acceso a los datos por campo que usan las acciones de la cola.
    La seleccion pertenece al modelo de lista, con una sola columna.
    """

    def __init__(self):
        super().__init__()
        self._campos = []
        self.setObjectName("batchesSidebar")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setWordWrap(True)
        self.setSpacing(3)
        self.setMinimumWidth(205)

    def rowCount(self):
        return self.count()

    def columnCount(self):
        return 1

    def rowAt(self, y):
        return self.indexAt(QPoint(0, y)).row()

    def selectRow(self, row):
        self.setCurrentRow(row)

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
