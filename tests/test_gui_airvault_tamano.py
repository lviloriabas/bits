"""La ventana de AirVault no se sale de la pantalla por lo que pide dentro.

El layout exige de mínimo lo que suman sus controles puestos en fila, y la
fila de botones de abajo sola pide más de 1200 px. Qt aplica ese mínimo por
encima del tamaño con el que la ventana se abrió, así que en una pantalla
baja la ventana crecía sola y dejaba los botones fuera del alcance.
"""

from __future__ import annotations

import pytest

from PySide6.QtCore import QPoint, QRect

from app.gui import airvault_window as modulo
from app.gui.airvault_window import AirVaultWindow


@pytest.fixture
def pantalla(monkeypatch):
    """Finge el escritorio disponible, que offscreen no sabe medir."""

    def fijar(ancho: int, alto: int) -> None:
        monkeypatch.setattr(
            modulo, "available_area", lambda _w=None: QRect(0, 0, ancho, alto)
        )

    return fijar


@pytest.mark.parametrize(
    "ancho, alto", [(1920, 1080), (1366, 768), (1280, 720), (1024, 768)]
)
def test_la_ventana_cabe_en_la_pantalla(
    app, pantalla, tmp_path, ancho, alto
):
    pantalla(ancho, alto)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana.show()
        app.processEvents()

        assert ventana.width() <= ancho, "la ventana se sale de ancho"
        assert ventana.height() <= alto, "la ventana se sale de alto"
        assert ventana.minimumWidth() <= ancho, (
            "el mínimo exigido no cabe: Qt la volvería a estirar"
        )
        assert ventana.minimumHeight() <= alto
    finally:
        ventana.close()
        app.processEvents()


def test_margen_avance_y_botones_coinciden_con_la_ventana_principal(
    app, pantalla, tmp_path
):
    pantalla(1920, 1080)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana.resize(1280, 800)
        ventana.show()
        app.processEvents()

        margenes = ventana._root_layout.contentsMargins()
        assert (
            margenes.left(),
            margenes.top(),
            margenes.right(),
            margenes.bottom(),
        ) == (8, 8, 8, 8)
        assert not ventana.estado_label.isVisibleTo(ventana)
        progreso = ventana.progreso.mapTo(ventana, QPoint())
        assert progreso.x() == margenes.left()
        assert progreso.x() + ventana.progreso.width() == (
            ventana.panel_batches.mapTo(ventana, QPoint()).x()
            + ventana.panel_batches.width()
        )
        assert ventana.reloj_label.parentWidget() is ventana.progreso
        cerrar = ventana.boton_cerrar.mapTo(ventana, QPoint())
        espacio_inferior = (
            ventana.height() - cerrar.y() - ventana.boton_cerrar.height()
        )
        assert espacio_inferior >= margenes.bottom()
    finally:
        ventana.close()
        app.processEvents()


def test_el_contenido_no_puede_estirar_la_ventana_fuera_del_escritorio(
    app, pantalla, tmp_path
):
    """Los controles proporcionados caben sin exigir un escritorio mayor."""
    pantalla(1024, 768)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana.show()
        app.processEvents()
        assert ventana.minimumSizeHint().width() <= 1024
        assert ventana.minimumWidth() <= 1024
        for control in (ventana.boton_cerrar, ventana.boton_subir,
                        ventana.boton_continuar, ventana.boton_reiniciar):
            assert ventana.rect().contains(control.mapTo(ventana, control.rect().topLeft()))
            assert ventana.rect().contains(control.mapTo(ventana, control.rect().bottomRight()))
    finally:
        ventana.close()
        app.processEvents()


def test_una_pantalla_diminuta_no_encoge_la_ventana_hasta_lo_inservible(
    app, pantalla, tmp_path
):
    pantalla(320, 240)
    ventana = AirVaultWindow(tmp_path)
    try:
        ventana.show()
        app.processEvents()

        assert ventana.minimumWidth() == modulo.ANCHO_MINIMO_VENTANA
        assert ventana.minimumHeight() == modulo.ALTO_MINIMO_VENTANA
    finally:
        ventana.close()
        app.processEvents()
