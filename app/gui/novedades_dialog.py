"""Aviso de los cambios que instalo la ultima actualizacion.

Se abre una vez, en el primer arranque despues de actualizar. Cada cambio
ocupa una fila: su identificador a la izquierda, en letra pequeña y gris, y
la descripcion a la derecha, que baja de linea cuando no cabe.

El cuadro crece con los cambios solo hasta un tope. Una actualizacion que
trae cincuenta no puede pedir cincuenta filas de alto: a partir de ahi la
lista se desplaza y el boton de aceptar sigue a la vista.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.gui.responsive import fit_to_screen
from app.gui.theme import gestor_tema
from app.gui.tokens import FONT_CAPTION_PT, SPACE_M, SPACE_S
from app.gui.widgets import (
    data_table_qss,
    pintar_celda_del_tema,
    style_data_table,
    window_stylesheet,
)

_ANCHO = 560
# Tope del alto. Entra con holgura en un escritorio de 1366x768; en uno mas
# bajo todavia lo recorta ``fit_to_screen``.
_ALTO_MAXIMO = 480
# Con menos que esto la lista ya no enseña ni dos cambios seguidos.
_ALTO_MINIMO_LISTA = 96
# Sitio de la barra vertical, que la hoja de la tabla deja en 12 px.
_ANCHO_BARRA = 12


def separar_cambio(linea: str) -> tuple[str, str]:
    """Parte «abc1234 Mejora el visor» en identificador y descripcion."""
    identificador, _, descripcion = linea.strip().partition(" ")
    return identificador, descripcion.strip()


class NovedadesDialog(QDialog):
    """Lista desplazable de los cambios instalados, uno por fila."""

    def __init__(
        self, novedades: Iterable[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Actualización instalada")
        self._cambios = [separar_cambio(linea) for linea in novedades]
        self._density = fit_to_screen(self, _ANCHO, _ALTO_MAXIMO)
        self._aplicar_hoja()
        # La hoja lleva el fragmento de la densidad, así que la rehace el
        # propio cuadro cuando cambia el tema.
        gestor_tema().cambiado.connect(self._al_cambiar_tema)
        self._build_ui()
        self._ajustar_alto()

    def _aplicar_hoja(self) -> None:
        """La hoja del cuadro, con los tonos y las medidas de ahora."""
        self.setStyleSheet(
            window_stylesheet(data_table_qss() + self._density.qss)
        )

    def _al_cambiar_tema(self, _nombre: str) -> None:
        """Rehace la hoja del cuadro con los tonos del tema nuevo."""
        self._aplicar_hoja()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        margin = max(SPACE_S, self._density.window_margin)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(SPACE_S)

        cantidad = len(self._cambios)
        self.intro = QLabel(
            "Se instaló 1 cambio:"
            if cantidad == 1
            else f"Se instalaron {cantidad} cambios:"
        )
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)

        self.tabla = QTableWidget(cantidad, 2)
        self.tabla.setAccessibleName("Cambios instalados")
        self.tabla.horizontalHeader().setVisible(False)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setShowGrid(False)
        self.tabla.setWordWrap(True)
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tabla.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # Las filas no miden lo mismo: por filas enteras, una descripción de
        # tres líneas saltaría de golpe.
        self.tabla.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.tabla.setMinimumHeight(_ALTO_MINIMO_LISTA)
        style_data_table(self.tabla)

        letra_id = self.tabla.font()
        letra_id.setPointSize(FONT_CAPTION_PT)
        centrado = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for fila, (identificador, descripcion) in enumerate(self._cambios):
            celda_id = QTableWidgetItem(identificador)
            celda_id.setFont(letra_id)
            celda_id.setTextAlignment(centrado)
            # El identificador acompaña, no se lee: va en el gris de lo
            # secundario para que la vista caiga en la descripción.
            pintar_celda_del_tema(celda_id, "TEXT_TERTIARY")
            self.tabla.setItem(fila, 0, celda_id)
            celda_texto = QTableWidgetItem(descripcion)
            celda_texto.setTextAlignment(centrado)
            self.tabla.setItem(fila, 1, celda_texto)
        layout.addWidget(self.tabla, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        aceptar = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        aceptar.setText("Aceptar")
        aceptar.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        layout.addWidget(self.buttons)
        aceptar.setFocus()

    def _ajustar_alto(self) -> None:
        """Abre el cuadro del alto de sus cambios, sin pasar del tope."""
        self.ensurePolished()
        margenes = self.layout().contentsMargins()
        ancho_tabla = self.width() - margenes.left() - margenes.right()
        cabecera = self.tabla.horizontalHeader()
        self.tabla.resizeColumnToContents(0)
        self.tabla.setColumnWidth(0, self.tabla.columnWidth(0) + SPACE_M)
        # La descripción se mide con el ancho que va a tener, barra incluida:
        # de eso depende en cuántas líneas se parte cada una.
        self.tabla.setColumnWidth(
            1,
            ancho_tabla
            - self.tabla.columnWidth(0)
            - 2 * self.tabla.frameWidth()
            - _ANCHO_BARRA,
        )
        self._ajustar_filas()
        cabecera.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        cabecera.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # Al ensanchar o estrechar el cuadro cambian los saltos de línea.
        cabecera.sectionResized.connect(self._ajustar_filas)

        alto_filas = sum(
            self.tabla.rowHeight(fila) for fila in range(self.tabla.rowCount())
        ) + 2 * self.tabla.frameWidth()
        alto = (
            self.layout().totalHeightForWidth(self.width())
            - self.tabla.sizeHint().height()
            + max(alto_filas, _ALTO_MINIMO_LISTA)
        )
        fit_to_screen(self, self.width(), min(alto, _ALTO_MAXIMO))

    def _ajustar_filas(self, *_args) -> None:
        """Da a cada fila el alto de su texto más un respiro."""
        for fila in range(self.tabla.rowCount()):
            self.tabla.setRowHeight(
                fila, self.tabla.sizeHintForRow(fila) + SPACE_S
            )

    def cambios(self) -> list[tuple[str, str]]:
        """Lo que enseña la lista: identificador y descripción por fila."""
        return [
            (self.tabla.item(fila, 0).text(), self.tabla.item(fila, 1).text())
            for fila in range(self.tabla.rowCount())
        ]
