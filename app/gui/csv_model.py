"""Las filas del CSV vistas por la tabla, sin un ítem por celda.

Una ejecución grande son unas 2.400 páginas por 86 columnas: más de 200.000
celdas. Con un ``QTableWidgetItem`` por celda, abrir el CSV costaba varios
segundos y cada clic en una cabecera movía los 200.000 ítems de sitio, así
que la ventana se quedaba sin responder y el orden parecía no aplicarse.

Aquí la tabla no guarda nada: lee del CSV que ya está en memoria y pinta
solo lo que se ve. Ordenar es reordenar una lista de índices, que es
instantáneo por grande que sea la ejecución.

Cada columna de la tabla es una del CSV, en el mismo orden. Hubo una primera
columna de casillas para marcar páginas sueltas, pero no seguía a la
selección y confundía: lo que se elige es lo resaltado, con Ctrl o Mayús.
"""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from app.gui.csv_utils import csv_display_name
from app.gui.table_sort import sorted_row_order

STATUS_COLORS = {
    "OK": "#1a7f37",
    "WARNING": "#9a6700",
    "ERROR": "#cf222e",
}


class CsvTableModel(QAbstractTableModel):
    """Filas y columnas del CSV abierto, con sus colores."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._columns: list[str] = []
        self._rows: list[dict[str, str]] = []
        # Fila del CSV que ocupa hoy cada fila mostrada. Es lo único que
        # cambia al ordenar.
        self._order: list[int] = []
        self._status: Callable[[dict[str, str], str], str | None] = (
            lambda _row, _column: None
        )
        self._comment: Callable[[dict[str, str], str], str] = (
            lambda _row, _column: ""
        )

    # ── contenido ───────────────────────────────────────────────────────

    def set_lookups(
        self,
        status: Callable[[dict[str, str], str], str | None],
        comment: Callable[[dict[str, str], str], str],
    ) -> None:
        """Fija de dónde salen el color y el comentario de cada celda."""
        self._status = status
        self._comment = comment

    def set_content(
        self, columns: Sequence[str], rows: Sequence[dict[str, str]]
    ) -> None:
        """Cambia el CSV que se muestra y devuelve la tabla a su orden."""
        self.beginResetModel()
        self._columns = list(columns)
        self._rows = list(rows)
        self._order = list(range(len(self._rows)))
        self.endResetModel()

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    @property
    def rows(self) -> list[dict[str, str]]:
        return self._rows

    def source_row(self, display_row: int) -> int:
        """Fila del CSV que se está mostrando en esa posición."""
        if 0 <= display_row < len(self._order):
            return self._order[display_row]
        return -1

    def display_row(self, source_row: int) -> int:
        """Dónde está hoy en la tabla una fila del CSV."""
        try:
            return self._order.index(source_row)
        except ValueError:
            return -1

    def column_of(self, name: str) -> int:
        """Columna de la tabla que muestra esa columna del CSV."""
        try:
            return self._columns.index(name)
        except ValueError:
            return -1

    def name_of(self, column: int) -> str:
        """Columna del CSV que se ve en esa columna de la tabla."""
        if 0 <= column < len(self._columns):
            return self._columns[column]
        return ""

    # ── orden ───────────────────────────────────────────────────────────

    def column_values(self, column: int) -> list[str]:
        """Texto de esa columna para cada fila del CSV, en orden del CSV."""
        name = self.name_of(column)
        if not name:
            return [""] * len(self._rows)
        return [row.get(name, "") for row in self._rows]

    def apply_order(self, order: Sequence[int]) -> None:
        """Reubica las filas sin tocar los datos ni perder la selección."""
        nuevo = list(order)
        if nuevo == self._order:
            return
        self.layoutAboutToBeChanged.emit()
        anteriores = self.persistentIndexList()
        posicion = {origen: fila for fila, origen in enumerate(nuevo)}
        destinos = [
            self.index(
                posicion.get(self._order[indice.row()], indice.row()),
                indice.column(),
            )
            if 0 <= indice.row() < len(self._order)
            else indice
            for indice in anteriores
        ]
        self._order = nuevo
        self.changePersistentIndexList(anteriores, destinos)
        self.layoutChanged.emit()

    def sort_rows(self, column: int, descending: bool) -> None:
        """Ordena las filas por una columna, sin mover ningún dato."""
        self.apply_order(
            sorted_row_order(self.column_values(column), descending)
        )

    def reset_order(self) -> None:
        """Devuelve la tabla al orden en que se generó el CSV."""
        self.apply_order(list(range(len(self._rows))))

    # ── API de Qt ───────────────────────────────────────────────────────

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - API Qt
        return 0 if parent.isValid() else len(self._order)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - API Qt
        return 0 if parent.isValid() else len(self._columns)

    def headerData(  # noqa: N802 - API Qt
        self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole
    ):
        if orientation is not Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return csv_display_name(self.name_of(section))
        return None

    def flags(self, index):  # noqa: D102 - API Qt
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):  # noqa: D102
        if not index.isValid():
            return None
        origen = self.source_row(index.row())
        if origen < 0:
            return None
        column = index.column()

        if role == Qt.ItemDataRole.UserRole:
            return origen

        row = self._rows[origen]
        name = self.name_of(column)
        if role == Qt.ItemDataRole.DisplayRole:
            return row.get(name, "")
        if role in (
            Qt.ItemDataRole.BackgroundRole,
            Qt.ItemDataRole.ForegroundRole,
            Qt.ItemDataRole.ToolTipRole,
        ):
            status = self._status(row, name)
            if not status:
                return None
            if role == Qt.ItemDataRole.BackgroundRole:
                return QColor(STATUS_COLORS[status])
            if role == Qt.ItemDataRole.ForegroundRole:
                return QColor("#ffffff")
            comment = self._comment(row, name)
            return f"Estado: {status}" + (f"\n{comment}" if comment else "")
        return None
