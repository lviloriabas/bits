"""El registro de AirVault pinta el icono de cada fila en su propia columna."""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QListView, QListWidget, QListWidgetItem

from app.gui.airvault_bitacora import RegistroAirVaultDelegate
from app.gui.tokens import SPACE_S, SPACE_XS


def registro(app, filas):
    lista = QListWidget()
    lista.setWordWrap(True)
    lista.setResizeMode(QListView.ResizeMode.Adjust)
    lista.setItemDelegate(RegistroAirVaultDelegate(lista))
    lista.resize(520, 200)
    for texto, con_icono in filas:
        fila = QListWidgetItem(texto)
        if con_icono:
            mapa = QPixmap(32, 32)
            mapa.fill(QColor("#ff0000"))
            fila.setIcon(QIcon(mapa))
        lista.addItem(fila)
    lista.show()
    app.processEvents()
    return lista


def rojos(imagen, zona: QRect) -> int:
    """Cuantos puntos del icono de prueba hay en esa zona."""
    return sum(
        1
        for x in range(zona.left(), zona.right() + 1)
        for y in range(zona.top(), zona.bottom() + 1)
        if (color := imagen.pixelColor(x, y)).red() > 200
        and color.green() < 60 and color.blue() < 60
    )


def test_el_icono_no_se_pinta_encima_de_la_hora(app):
    """El arco que gira caia sobre la hora de las filas que se estan leyendo."""
    lista = registro(app, [
        ("19:50:25  Batch «uno»: revisando, 0 de 23 bitácoras", True),
        ("19:50:25  Batch «dos»: revisando, 0 de 23 bitácoras", False),
        ("Revisando batches en Web Search (0:07)", True),
    ])
    imagen = lista.viewport().grab().toImage()
    lado = lista.fontMetrics().height()
    for fila in (0, 2):
        caja = lista.visualItemRect(lista.item(fila))
        columna = QRect(caja.x() + SPACE_S, caja.y() + SPACE_XS, lado, lado)
        resto = QRect(columna.right() + 1, caja.y(), caja.width() - columna.right() - 2, caja.height())
        # Todo el icono cabe en su columna y nada de el pasa al texto.
        assert rojos(imagen, columna) > lado * lado // 2
        assert rojos(imagen, resto) == 0
    sin_icono = lista.visualItemRect(lista.item(1))
    assert rojos(imagen, sin_icono) == 0
    lista.close()


def test_la_columna_de_iconos_se_reserva_en_todas_las_filas(app):
    """Con icono o sin el, la hora y el texto empiezan en el mismo sitio."""
    lista = registro(app, [
        # La primera se queda con el foco de la lista y su fondo es otro.
        ("19:50:20  Web Search: se revisan 2 batches", False),
        ("19:50:25  Batch «uno»: revisando, 0 de 23 bitácoras", False),
        ("19:50:25  Batch «uno»: revisando, 0 de 23 bitácoras", True),
    ])
    imagen = lista.viewport().grab().toImage()
    lado = lista.fontMetrics().height()
    primera, segunda = (lista.visualItemRect(lista.item(f)) for f in (1, 2))
    assert primera.height() == segunda.height()
    desde = SPACE_S + lado + 1
    for x in range(desde, primera.width() - 1):
        for y in range(primera.height()):
            assert (imagen.pixelColor(primera.x() + x, primera.y() + y)
                    == imagen.pixelColor(segunda.x() + x, segunda.y() + y))
    lista.close()


def test_una_fila_larga_con_icono_reserva_el_alto_de_sus_lineas(app):
    largo = "Revisando batches en Web Search " * 12
    lista = registro(app, [(largo, True), ("Revisando", True)])
    assert lista.sizeHintForRow(0) > lista.sizeHintForRow(1) * 2
    assert lista.item(0).data(Qt.ItemDataRole.DisplayRole) == largo
    lista.close()
