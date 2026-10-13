"""Registro con una columna de iconos, otra de hora y el texto alineado."""

import time

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QStyleOptionViewItem

from app.gui.tokens import SPACE_M, SPACE_S, SPACE_XS, paleta


def pintar_latido(pintor: QPainter, caja: QRectF, color: str, girando: bool) -> None:
    """Un arco que da una vuelta por segundo, o un circulo quieto.

    Es la marca de lo que esta en marcha: la lleva la linea viva del
    registro y la tarjeta del batch que se esta procesando. Se dibuja y no
    se escribe con un caracter porque la fuente de la interfaz no trae los
    de una terminal y cada Windows los sustituia por otro.
    """
    pintor.save()
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing)
    pluma = QPen(QColor(color))
    pluma.setWidthF(2.0)
    pluma.setCapStyle(Qt.PenCapStyle.RoundCap)
    pintor.setPen(pluma)
    pintor.setBrush(Qt.BrushStyle.NoBrush)
    arco = caja.adjusted(2, 2, -2, -2)
    if girando:
        # Angulos de Qt en dieciseisavos de grado y en sentido contrario al
        # reloj; el signo lo hace girar como uno.
        angulo = int(time.monotonic() * 360) % 360
        pintor.drawArc(arco, -angulo * 16, 270 * 16)
    else:
        pintor.drawEllipse(arco)
    pintor.restore()


class RegistroAirVaultDelegate(QStyledItemDelegate):
    """Alinea las continuaciones por medidas de fuente, sin sangrias de espacios.

    El icono de una fila (el arco que gira mientras se lee un batch, el de
    la linea viva) tiene su propia columna a la izquierda, reservada en
    todas las filas. Pintado por el estilo caia encima de la hora, y con la
    columna solo donde hay icono el texto saltaba de sitio cada vez que una
    fila empezaba o terminaba de girar.
    """

    @staticmethod
    def separar(texto):
        hora, separador, cuerpo = str(texto).partition("  ")
        if (separador and len(hora) == 8 and hora[2] == hora[5] == ":"
                and hora.replace(":", "").isdigit()):
            return hora, cuerpo
        return None

    @staticmethod
    def _canal(opcion) -> int:
        """Ancho de la columna de iconos: uno del alto de la letra y su aire."""
        return opcion.fontMetrics.height() + SPACE_S

    @staticmethod
    def _con_icono(index) -> bool:
        icono = index.data(Qt.ItemDataRole.DecorationRole)
        return isinstance(icono, QIcon) and not icono.isNull()

    def sizeHint(self, option, index):
        partes = self.separar(index.data(Qt.ItemDataRole.DisplayRole))
        if partes is None and not self._con_icono(index):
            return super().sizeHint(option, index)
        opcion = QStyleOptionViewItem(option)
        self.initStyleOption(opcion, index)
        ancho = max(1, self.parent().viewport().width() - SPACE_S * 2)
        sangria = self._canal(opcion)
        cuerpo = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if partes is not None:
            sangria += opcion.fontMetrics.horizontalAdvance("00:00:00") + SPACE_M
            cuerpo = partes[1]
        texto = opcion.fontMetrics.boundingRect(
            QRect(0, 0, max(1, ancho - sangria), 100000),
            Qt.TextFlag.TextWordWrap, cuerpo,
        )
        return QSize(ancho, max(texto.height(), opcion.fontMetrics.height()) + SPACE_XS * 2)

    def paint(self, painter, option, index):
        partes = self.separar(index.data(Qt.ItemDataRole.DisplayRole))
        con_icono = self._con_icono(index)
        if partes is None and not con_icono:
            super().paint(painter, option, index)
            return
        opcion = QStyleOptionViewItem(option)
        self.initStyleOption(opcion, index)
        # Del modelo y no de la opcion: al vaciar el de la opcion, mas abajo,
        # se vaciaria tambien este.
        icono = index.data(Qt.ItemDataRole.DecorationRole)
        cuerpo_sin_hora = str(opcion.text)
        # El estilo pinta solo el fondo y la seleccion: el icono y el texto
        # van aparte, cada uno en su columna.
        opcion.text = ""
        opcion.icon = QIcon()
        opcion.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        opcion.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opcion, painter, opcion.widget)
        c = paleta()
        seleccionado = bool(opcion.state & QStyle.StateFlag.State_Selected)
        # La fila elegida y la que tiene el cursor encima cambian de fondo
        # con un gris del tema (lo pone la hoja de la ventana), no con el
        # acento: el texto sigue siendo el del panel. Pintado con el color
        # de encima del acento salia negro sobre ese gris y no se leia.
        principal = c.PANE_TEXT
        secundario = principal if seleccionado else c.TEXT_SECONDARY
        caja = opcion.rect.adjusted(SPACE_S, SPACE_XS, -SPACE_S, -SPACE_XS)
        lado = opcion.fontMetrics.height()
        painter.save()
        painter.setFont(opcion.font)
        if con_icono:
            icono.paint(painter, QRect(caja.x(), caja.y(), lado, lado))
        contenido = caja.adjusted(self._canal(opcion), 0, 0, 0)
        marcas = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        if partes is None:
            # La linea viva: sin hora, con el color que le puso la ventana.
            propio = index.data(Qt.ItemDataRole.ForegroundRole)
            painter.setPen(propio.color() if propio is not None and not seleccionado
                           else QColor(principal))
            painter.drawText(contenido, marcas | Qt.TextFlag.TextWordWrap, cuerpo_sin_hora)
        else:
            ancho_hora = opcion.fontMetrics.horizontalAdvance("00:00:00")
            hora = QRect(contenido.x(), contenido.y(), ancho_hora, lado)
            painter.setPen(QColor(secundario))
            painter.drawText(hora, marcas, partes[0])
            painter.setPen(QColor(principal))
            painter.drawText(contenido.adjusted(ancho_hora + SPACE_M, 0, 0, 0),
                             marcas | Qt.TextFlag.TextWordWrap, partes[1])
        painter.restore()
