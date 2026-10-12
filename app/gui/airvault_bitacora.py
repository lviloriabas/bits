"""Registro con una columna de hora y texto alineado al envolver lineas."""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QStyleOptionViewItem

from app.gui.tokens import SPACE_M, SPACE_S, SPACE_XS, on_accent_text, paleta


class RegistroAirVaultDelegate(QStyledItemDelegate):
    """Alinea las continuaciones por medidas de fuente, sin sangrias de espacios."""

    @staticmethod
    def separar(texto):
        hora, separador, cuerpo = str(texto).partition("  ")
        if (separador and len(hora) == 8 and hora[2] == hora[5] == ":"
                and hora.replace(":", "").isdigit()):
            return hora, cuerpo
        return None

    def sizeHint(self, option, index):
        partes = self.separar(index.data(Qt.ItemDataRole.DisplayRole))
        if partes is None:
            return super().sizeHint(option, index)
        opcion = QStyleOptionViewItem(option)
        self.initStyleOption(opcion, index)
        ancho = max(1, self.parent().viewport().width() - SPACE_S * 2)
        columna_hora = opcion.fontMetrics.horizontalAdvance("00:00:00") + SPACE_M
        texto = opcion.fontMetrics.boundingRect(
            QRect(0, 0, max(1, ancho - columna_hora), 100000),
            Qt.TextFlag.TextWordWrap, partes[1],
        )
        return QSize(ancho, max(texto.height(), opcion.fontMetrics.height()) + SPACE_XS * 2)

    def paint(self, painter, option, index):
        partes = self.separar(index.data(Qt.ItemDataRole.DisplayRole))
        if partes is None:
            super().paint(painter, option, index)
            return
        opcion = QStyleOptionViewItem(option)
        self.initStyleOption(opcion, index)
        opcion.text = ""
        opcion.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opcion, painter, opcion.widget)
        c = paleta()
        seleccionado = bool(opcion.state & QStyle.StateFlag.State_Selected)
        principal = on_accent_text() if seleccionado else c.PANE_TEXT
        secundario = principal if seleccionado else c.TEXT_SECONDARY
        caja = opcion.rect.adjusted(SPACE_S, SPACE_XS, -SPACE_S, -SPACE_XS)
        ancho_hora = opcion.fontMetrics.horizontalAdvance("00:00:00")
        hora = QRect(caja.x(), caja.y(), ancho_hora, opcion.fontMetrics.height())
        cuerpo = caja.adjusted(ancho_hora + SPACE_M, 0, 0, 0)
        painter.save()
        painter.setFont(opcion.font)
        painter.setPen(QColor(secundario))
        painter.drawText(hora, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, partes[0])
        painter.setPen(QColor(principal))
        painter.drawText(cuerpo, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, partes[1])
        painter.restore()
