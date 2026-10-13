"""Lista lateral de batches con seleccion multiple y acciones de la cola."""

from PySide6.QtCore import QItemSelectionModel, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QListView, QListWidget,
                               QListWidgetItem, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem)

from app.gui.tokens import SPACE_S, paleta

# El numero con el que se nombra el batch en la cola y en el registro. Va en
# la tarjeta y no en su texto: asi el nombre conserva su linea entera y el
# numero queda siempre en la misma columna, a la izquierda.
ROL_NUMERO = Qt.ItemDataRole.UserRole + 1
# Un ID que AirVault devolvio alguna vez y el manifiesto ya no conserva.
ROL_ID_ANTERIOR = Qt.ItemDataRole.UserRole + 2


class _TarjetaBatchDelegate(QStyledItemDelegate):
    """Reserva el alto de todas las lineas cuando el nombre se parte."""

    @staticmethod
    def _canal(option) -> int:
        """Ancho de la columna del numero: tres cifras y su separacion."""
        return option.fontMetrics.horizontalAdvance("000") + SPACE_S

    def sizeHint(self, option, index):
        self.initStyleOption(option, index)
        lista = self.parent()
        ancho = max(1, lista.viewport().width() - 2 * lista.spacing())
        canal = self._canal(option) if index.data(ROL_NUMERO) else 0
        texto = option.fontMetrics.boundingRect(
            QRect(0, 0, max(1, ancho - 2 * (SPACE_S + 1) - canal), 100000),
            Qt.TextFlag.TextWordWrap, option.text,
        )
        return QSize(ancho, texto.height() + 2 * (SPACE_S + 1))

    def paint(self, painter, option, index):
        numero = index.data(ROL_NUMERO)
        if not numero:
            super().paint(painter, option, index)
            return
        opcion = QStyleOptionViewItem(option)
        self.initStyleOption(opcion, index)
        texto = opcion.text
        # El estilo pinta el fondo, el borde y la seleccion; el texto se
        # escribe aparte para dejarle su columna al numero.
        opcion.text = ""
        opcion.widget.style().drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opcion, painter, opcion.widget
        )
        margen = SPACE_S + 1
        caja = opcion.rect.adjusted(margen, margen, -margen, -margen)
        canal = self._canal(opcion)
        seleccionada = bool(opcion.state & QStyle.StateFlag.State_Selected)
        color = (QColor(paleta().PANE_TEXT) if seleccionada
                 else opcion.palette.color(QPalette.ColorRole.Text))
        painter.save()
        painter.setPen(color)
        negrita = QFont(opcion.font)
        negrita.setBold(True)
        painter.setFont(negrita)
        painter.drawText(
            QRect(caja.left(), caja.top(), canal - SPACE_S, caja.height()),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, str(numero),
        )
        painter.setFont(opcion.font)
        painter.drawText(
            caja.adjusted(canal, 0, 0, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap, texto,
        )
        painter.restore()


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
        anterior = ident.data(ROL_ID_ANTERIOR) or ""
        identidad = (f", ID {ident.text()}" if ident.text()
                     else f", último ID {anterior}" if anterior else "")
        tarjeta.setText(
            f"{nombre.text()}\n"
            f"{paginas.text()} páginas" + identidad
            + f"\n{estado.text()}"
        )
        tarjeta.setToolTip(nombre.text() + "\n" + estado.toolTip())
        tarjeta.setForeground(estado.foreground())

    def numerar(self, row, numero):
        """Pone el numero del batch en su columna y en la ayuda de la tarjeta."""
        tarjeta = super().item(row)
        tarjeta.setData(ROL_NUMERO, int(numero))
        tarjeta.setToolTip(f"Batch {numero}: " + tarjeta.toolTip())

    def sizeHintForColumn(self, column):
        return self.minimumWidth()

    def rowHeight(self, row):
        return self.sizeHintForRow(row)

    def setColumnWidth(self, column, width):
        pass
