"""El calendario de las fechas solo ofrece, a la vista, lo que se puede elegir."""

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QTableView

from app.gui.tokens import paleta
from app.gui.widgets import _APPLICATION_THEME_PROPERTY
from app.gui.web_reports_window import WebReportsWindow


def _color(calendario, dia: QDate):
    pincel = calendario.dateTextFormat(dia).foreground()
    return pincel.color().name() if pincel.style() != Qt.BrushStyle.NoBrush else None


def test_los_dias_futuros_y_de_otros_meses_salen_en_gris(app, tmp_path, opciones_de_interfaz):
    ventana = WebReportsWindow(tmp_path)
    try:
        hoy = QDate.currentDate()
        gris = QColor(paleta().TEXT_DISABLED).name()
        for campo in (ventana.desde_edit, ventana.hasta_edit):
            calendario = campo.calendarWidget()
            calendario.setCurrentPage(hoy.year(), hoy.month())
            assert campo.maximumDate() == hoy
            assert _color(calendario, hoy.addDays(1)) == gris
            assert _color(calendario, QDate(hoy.year(), hoy.month(), 1).addDays(-1)) == gris
            assert _color(calendario, hoy) is None

            # Al ir a un mes pasado se apagan solo los días de sus vecinos.
            anterior = hoy.addMonths(-1)
            calendario.setCurrentPage(anterior.year(), anterior.month())
            assert _color(calendario, QDate(anterior.year(), anterior.month(), 15)) is None
            assert _color(calendario, QDate(hoy.year(), hoy.month(), 1)) == gris
    finally:
        ventana.close()


def test_sabados_y_domingos_no_salen_en_rojo(app, tmp_path, opciones_de_interfaz):
    ventana = WebReportsWindow(tmp_path)
    try:
        calendario = ventana.desde_edit.calendarWidget()
        for dia in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            pincel = calendario.weekdayTextFormat(dia).foreground()
            assert pincel.style() == Qt.BrushStyle.NoBrush
    finally:
        ventana.close()


def test_la_ultima_semana_del_calendario_se_ve_entera(app, tmp_path, opciones_de_interfaz):
    """La hoja de las tablas le ponía un borde a la rejilla y la cortaba.

    Pasa con el tema global instalado, como al arrancar BITS: entonces la
    hoja de la ventana lleva solo lo suyo y manda sobre la global.
    """
    app.setProperty(_APPLICATION_THEME_PROPERTY, True)
    ventana = WebReportsWindow(tmp_path)
    try:
        calendario = ventana.hasta_edit.calendarWidget()
        emergente = calendario.window()
        emergente.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        emergente.show()
        app.processEvents()
        rejilla = calendario.findChild(QTableView)
        filas = sum(rejilla.rowHeight(fila) for fila in range(rejilla.model().rowCount()))
        assert rejilla.frameWidth() == 0
        assert filas <= rejilla.viewport().height()
        emergente.hide()
    finally:
        ventana.close()
        app.setProperty(_APPLICATION_THEME_PROPERTY, None)
