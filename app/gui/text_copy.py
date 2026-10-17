"""Soporte comun para copiar mensajes visibles de la interfaz."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QListWidget,
    QMenu,
    QTableView,
)


def _table_for(widget) -> QTableView | None:
    """La tabla a la que pertenece un widget, sea ella o su viewport.

    Las pulsaciones de tecla llegan a la tabla y los menus contextuales a su
    viewport, asi que el filtro tiene que reconocer las dos.
    """
    if isinstance(widget, QTableView):
        return widget
    parent = widget.parent() if hasattr(widget, "parent") else None
    if isinstance(parent, QTableView) and parent.viewport() is widget:
        return parent
    return None


def _celda_bajo_el_cursor(table: QTableView) -> str:
    """El dato de la celda activa, o cadena vacia si no hay ninguna."""
    actual = table.currentIndex()
    if not actual.isValid():
        return ""
    if table.isColumnHidden(actual.column()) or table.isRowHidden(actual.row()):
        return ""
    return actual.data() or ""


def _selecciona_filas(table: QTableView) -> bool:
    return (
        table.selectionBehavior()
        is QAbstractItemView.SelectionBehavior.SelectRows
    )


def selected_cells_as_text(table: QTableView) -> str:
    """Lo que se lleva el portapapeles de una tabla.

    En una tabla que selecciona por **filas** (el CSV procesado, el visor,
    la cola de AirVault) elegir una fila selecciona de paso todas sus
    celdas, asi que copiar la seleccion se llevaba la fila entera separada
    por tabuladores. Nadie pide eso: se pulsa una celda para copiar **ese**
    dato, casi siempre un numero de bitacora o una matricula que hay que
    pegar en otro sitio, y llevarse la fila obliga a limpiar lo pegado.
    Ahi se copia la celda que esta bajo el cursor, que es la que se pulso.

    Donde la seleccion es por celdas se conserva el bloque: ahi elegir
    varias es una decision, no un efecto de haber pulsado una fila. Se
    separan con tabulaciones y saltos de linea, que es lo que esperan la
    hoja de calculo y el editor de texto donde se pega.
    """
    if _selecciona_filas(table):
        return _celda_bajo_el_cursor(table)
    indexes = list(table.selectedIndexes())
    if not indexes:
        actual = table.currentIndex()
        indexes = [actual] if actual.isValid() else []
    if not indexes:
        return ""
    visibles = [
        index for index in indexes
        if not table.isColumnHidden(index.column())
        and not table.isRowHidden(index.row())
    ]
    if not visibles:
        return ""
    filas: dict[int, dict[int, str]] = {}
    for index in visibles:
        filas.setdefault(index.row(), {})[index.column()] = index.data() or ""
    return "\n".join(
        "\t".join(columnas[column] for column in sorted(columnas))
        for _fila, columnas in sorted(filas.items())
    )


class _CopyableTextFilter(QObject):
    """Hace seleccionables las etiquetas sin volverlas editables."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - API Qt
        # Las tablas no traen copia propia en Qt: sin esto, el CSV se lee en
        # pantalla pero no se puede llevar a ningun lado. Se limita a
        # QTableView para no pisar la copia que QListWidget ya resuelve.
        table = _table_for(watched)
        if table is not None:
            if (
                event.type() == QEvent.Type.KeyPress
                and event.matches(QKeySequence.StandardKey.Copy)
            ):
                texto = selected_cells_as_text(table)
                if texto:
                    QApplication.clipboard().setText(texto)
                event.accept()
                return True
            if (
                event.type() == QEvent.Type.ContextMenu
                and watched is table.viewport()
            ):
                # Abrir el menu sobre una celda la deja bajo el cursor. Sin
                # esto, «Copiar» se llevaba la celda que estuviera activa de
                # antes, que casi nunca es sobre la que se hizo clic. Va
                # antes de mirar la politica del menu para que valga tambien
                # en las tablas con menu propio, como la cola de AirVault.
                # Solo desde el viewport, que es donde la posicion del
                # evento son coordenadas de celdas; con el teclado no hay
                # posicion y la celda activa ya es la que corresponde.
                bajo = table.indexAt(event.pos())
                seleccion = table.selectionModel()
                if bajo.isValid() and seleccion is not None and seleccion.isSelected(bajo):
                    # Sobre una fila ya elegida solo se mueve el cursor: con
                    # varias elegidas el menu actua sobre todas, como hace Qt
                    # al pulsar con el boton derecho. Mover el cursor con la
                    # tabla dejaba elegida solo esa fila.
                    seleccion.setCurrentIndex(
                        bajo, QItemSelectionModel.SelectionFlag.NoUpdate
                    )
                elif bajo.isValid():
                    table.setCurrentIndex(bajo)
            # Una tabla con menu propio se queda con el suyo: el filtro de
            # la aplicacion corre antes que el widget, y quedarse con el
            # evento dejaria sin abrir menus como el de la cola de AirVault.
            if (
                event.type() == QEvent.Type.ContextMenu
                and table.contextMenuPolicy()
                == Qt.ContextMenuPolicy.DefaultContextMenu
            ):
                menu = QMenu(table)
                accion = menu.addAction("Copiar")
                texto = selected_cells_as_text(table)
                accion.setEnabled(bool(texto))
                if menu.exec(event.globalPos()) is accion:
                    QApplication.clipboard().setText(texto)
                event.accept()
                return True

        if isinstance(watched, QLabel):
            if event.type() in (QEvent.Type.Polish, QEvent.Type.Show):
                flags = watched.textInteractionFlags()
                watched.setTextInteractionFlags(
                    flags | Qt.TextInteractionFlag.TextSelectableByMouse
                )

            # ElidedLabel pinta un texto recortado, pero ``text()`` conserva
            # el mensaje completo. La copia debe llevarse ese mensaje y no
            # la version visible que termina en puntos suspensivos.
            copy_text = getattr(watched, "fullTextForCopy", None)
            if callable(copy_text):
                if (
                    event.type() == QEvent.Type.KeyPress
                    and event.matches(QKeySequence.StandardKey.Copy)
                ):
                    QApplication.clipboard().setText(copy_text())
                    event.accept()
                    return True
                if event.type() == QEvent.Type.ContextMenu:
                    menu = QMenu(watched)
                    action = menu.addAction("Copiar")
                    action.setEnabled(bool(copy_text()))
                    if menu.exec(event.globalPos()) is action:
                        QApplication.clipboard().setText(copy_text())
                    event.accept()
                    return True

        return super().eventFilter(watched, event)


def install_text_copy_support(app: QApplication | None = None) -> None:
    """Permite seleccionar y copiar el texto de la GUI: etiquetas y tablas."""
    app = app or QApplication.instance()
    if app is None or getattr(app, "_text_copy_filter", None) is not None:
        return

    event_filter = _CopyableTextFilter(app)
    app.installEventFilter(event_filter)
    # QApplication no toma propiedad Python del filtro al instalarlo. Se
    # conserva una referencia para que no sea recolectado mientras corre Qt.
    app._text_copy_filter = event_filter

    # Se instala al arrancar, antes de crear las ventanas. Recorrer
    # ``allWidgets()`` aqui puede devolver wrappers de widgets que Qt ya
    # destruyo durante una ejecucion larga (por ejemplo, en la suite) y
    # provocar una violacion de acceso nativa. Los eventos Polish/Show cubren
    # de forma segura cada etiqueta cuando entra en uso.


class CopyableListWidget(QListWidget):
    """Lista de solo lectura cuyas lineas se pueden copiar juntas."""

    def copySelectedItems(self) -> None:  # noqa: N802 - API Qt
        items = sorted(self.selectedItems(), key=self.row)
        if items:
            QApplication.clipboard().setText(
                "\n".join(item.text() for item in items)
            )

    def keyPressEvent(self, event) -> None:  # noqa: N802 - API Qt
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copySelectedItems()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - API Qt
        item = self.itemAt(event.pos())
        if item is not None and not item.isSelected():
            self.clearSelection()
            item.setSelected(True)
            self.setCurrentItem(item)

        menu = QMenu(self)
        action = menu.addAction("Copiar")
        action.setEnabled(bool(self.selectedItems()))
        if menu.exec(event.globalPos()) is action:
            self.copySelectedItems()
