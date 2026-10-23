"""Lo que se escribe en la fila de consulta tiene que caber y verse entero.

Las dos medidas que fija esta ventana no salen de un capricho de diseño:

* Qt pide el ancho del campo de fecha midiendo sus dos límites, y los
  dos (1/1/2000 y el día de hoy) llevan día y mes de una cifra. Con ese
  ancho una fecha como 30/12/2025 no entraba y el año se cortaba.
* El desplegable de «Mostrar» pedía sitio para 26 mayúsculas, casi el doble de
  lo que miden sus tres opciones, y encima se llevaba el estiramiento de la
  fila: se estiraba hasta el borde de la ventana y ese exceso salía de los
  campos de fecha, que se quedaban en su mínimo.
"""

from __future__ import annotations

import pytest

from PySide6.QtCore import QDate, QRect
from PySide6.QtWidgets import (
    QHBoxLayout, QSizePolicy, QStyle, QStyleOptionFrame, QWidget,
)

from app.gui import responsive
from app.gui.web_reports_window import (
    FECHA_MAS_LARGA, FORMATO_FECHA, WebReportsWindow, _CampoDeFecha,
)


@pytest.fixture
def pantalla(monkeypatch):
    """Finge el escritorio disponible, que offscreen no sabe medir."""

    def fijar(ancho: int, alto: int) -> None:
        monkeypatch.setattr(
            responsive,
            "available_area",
            lambda _w=None: QRect(0, 0, ancho, alto),
        )

    return fijar


def _sitio_para_el_texto(campo) -> int:
    """Lo que el editor deja de verdad para la fecha, con el cursor dentro.

    El editor no presta todo su ancho: el estilo le pone relleno, y QLineEdit
    guarda dos píxeles por lado y el cursor. Medir su ancho entero dejaba
    pasar una fecha que en pantalla salía cortada.
    """
    editor = campo.lineEdit()
    marco = QStyleOptionFrame()
    editor.initStyleOption(marco)
    libre = editor.style().subElementRect(
        QStyle.SubElement.SE_LineEditContents, marco, editor
    ).width()
    cursor = editor.style().pixelMetric(QStyle.PixelMetric.PM_TextCursorWidth, None, editor)
    return libre - 2 * 2 - cursor


@pytest.mark.parametrize("alto", [1080, 768])
def test_la_fecha_mas_larga_se_ve_entera(app, pantalla, tmp_path, alto):
    """Con las dos densidades: la baja aprieta el relleno de los lados."""
    pantalla(1366, alto)
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana.show()
        app.processEvents()

        for campo in (ventana.desde_edit, ventana.hasta_edit):
            campo.setDate(QDate(2025, 12, 30))
        app.processEvents()

        for campo in (ventana.desde_edit, ventana.hasta_edit):
            escrito = campo.fontMetrics().horizontalAdvance(campo.text())
            assert _sitio_para_el_texto(campo) >= escrito, (
                f"{campo.text()} no cabe en el campo y se corta"
            )
    finally:
        ventana.close()
        app.processEvents()


def test_un_campo_que_arranca_angosto_se_ensancha_hasta_que_la_fecha_cabe(app):
    """Con 22/10/2026 el editor quedaba justo del ancho del texto.

    El ancho de partida es una estimación, y con el estilo de Windows se
    quedaba corta: se cortaba el año o, con el cursor al principio, la
    primera cifra. Lo que lo arregla es medir el editor ya puesto y pedir lo
    que falte; aquí el campo arranca angosto a propósito.
    """
    caja = QWidget()
    fila = QHBoxLayout(caja)
    campo = _CampoDeFecha(QDate(2025, 12, 30))
    campo.setDisplayFormat(FORMATO_FECHA)
    campo.setCalendarPopup(True)
    campo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    campo.setMinimumWidth(40)
    fila.addWidget(campo)
    fila.addStretch(1)
    try:
        caja.resize(600, 60)
        caja.show()
        # Cada ensanche pide otro reparto de la fila, que llega como evento
        # pendiente; se reparte a mano hasta que el campo deje de crecer.
        ancho = -1
        while campo.width() != ancho:
            ancho = campo.width()
            app.sendPostedEvents()
            fila.activate()
            app.processEvents()

        escrito = campo.fontMetrics().horizontalAdvance(campo.text())
        assert _sitio_para_el_texto(campo) >= escrito
    finally:
        caja.close()


def test_la_fecha_mas_larga_es_la_de_dos_cifras(app, tmp_path):
    """El patrón con el que se mide no puede quedarse corto ante otra fecha."""
    ventana = WebReportsWindow(tmp_path)
    try:
        fuente = ventana.desde_edit.fontMetrics()
        formato = ventana.desde_edit.displayFormat()
        patron = fuente.horizontalAdvance(
            FECHA_MAS_LARGA.toString(formato)
        )
        for mes in (1, 9, 10, 12):
            dias = QDate(2025, mes, 1).daysInMonth()
            for dia in (1, 9, 10, 28, dias):
                fecha = QDate(2025, mes, dia)
                ancho = fuente.horizontalAdvance(fecha.toString(formato))
                assert ancho <= patron, (
                    f"{fecha.toString(formato)} mide más que el patrón"
                )
    finally:
        ventana.close()


def test_mostrar_no_se_estira_hasta_el_borde(app, pantalla, tmp_path):
    pantalla(1920, 1080)
    ventana = WebReportsWindow(tmp_path)
    try:
        ventana.show()
        ventana.resize(1280, 800)
        app.processEvents()

        combo = ventana.filtro_combo
        fuente = combo.fontMetrics()
        opcion = max(
            fuente.horizontalAdvance(combo.itemText(indice))
            for indice in range(combo.count())
        )
        # El hueco de la flecha y el relleno de los lados suman menos de cien
        # píxeles; el resto sería sitio pedido y no usado.
        assert combo.width() < opcion + 100, (
            "el desplegable pide mucho más de lo que miden sus opciones"
        )
        assert combo.width() >= opcion, (
            "las opciones no caben y se leerían recortadas"
        )
        # Y lo que sobra en la fila se queda a su derecha, no dentro.
        borde = ventana.width() - ventana._densidad.window_margin
        assert combo.geometry().right() < borde - 100
    finally:
        ventana.close()
        app.processEvents()
