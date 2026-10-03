"""La consulta recuerda sus parametros usando las preferencias portables."""

import json

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QDateEdit, QLineEdit

from app.gui.memoria import recordar
from app.gui.web_reports_window import WebReportsWindow


def test_recuerda_todos_los_parametros_al_reabrir(app, tmp_path, opciones_de_interfaz):
    primera = WebReportsWindow(tmp_path)
    primera.desde_edit.setDate(QDate(2025, 2, 3))
    primera.hasta_edit.setDate(QDate(2025, 4, 5))
    primera.aeronaves_edit.setText("HP-9913CMP, HP-9813CMP")
    primera.bitacoras_edit.setText("2008150 - 2008199")
    for combo in (
        primera.filtro_combo,
        primera.repositorio_combo,
        primera.libro_combo,
        primera.minimo_combo,
        primera.orden_combo,
        primera.actualizar_combo,
    ):
        combo.setCurrentIndex(1)
    primera.mostrar_previas.setChecked(not primera.mostrar_previas.isChecked())
    parametros = primera._parametros_consulta()
    filtros = primera.filtro_combo.currentData()
    revisar = primera.mostrar_previas.isChecked()
    primera.close()
    antes = opciones_de_interfaz.read_bytes()

    otra = WebReportsWindow(tmp_path)
    assert otra.desde_edit.date() == QDate(2025, 2, 3)
    assert otra.hasta_edit.date() == QDate(2025, 4, 5)
    assert otra._parametros_consulta() == parametros
    assert otra.filtro_combo.currentData() == filtros
    assert otra.mostrar_previas.isChecked() == revisar
    assert opciones_de_interfaz.read_bytes() == antes
    guardado = json.loads(antes)
    assert guardado["web_reports.desde"] == "2025-02-03"
    assert guardado["web_reports.hasta"] == "2025-04-05"


def test_limpiar_los_filtros_tambien_se_recuerda(app, tmp_path):
    primera = WebReportsWindow(tmp_path)
    primera.aeronaves_edit.setText("HP-9913CMP")
    primera.bitacoras_edit.setText("2008150")
    primera.aeronaves_edit.clear()
    primera.bitacoras_edit.clear()
    primera.close()

    otra = WebReportsWindow(tmp_path)
    assert otra.aeronaves_edit.text() == ""
    assert otra.bitacoras_edit.text() == ""


@pytest.mark.parametrize("valor", ["fecha rota", "2025-02-30", "1999-12-31", "9999-01-01", 123, {}])
def test_fecha_invalida_conserva_el_defecto(app, opciones_de_interfaz, valor):
    opciones_de_interfaz.write_text(json.dumps({"prueba.fecha": valor}), encoding="utf-8")
    fecha = QDateEdit(QDate(2025, 2, 3))
    fecha.setDateRange(QDate(2000, 1, 1), QDate(2026, 1, 1))
    recordar("prueba", "fecha", fecha)
    assert fecha.date() == QDate(2025, 2, 3)


@pytest.mark.parametrize("tipo", [QDateEdit, QLineEdit])
def test_restaurar_fecha_y_texto_no_emite_ni_reescribe(app, opciones_de_interfaz, tipo):
    anterior = tipo()
    recordar("prueba", "control", anterior)
    if tipo is QDateEdit:
        anterior.setDate(QDate(2025, 2, 3))
    else:
        anterior.setText("HP-9913CMP")
    contenido = opciones_de_interfaz.read_bytes()
    otra = tipo()
    avisos = []
    senal = otra.dateChanged if tipo is QDateEdit else otra.textChanged
    senal.connect(avisos.append)
    recordar("prueba", "control", otra)
    assert avisos == []
    assert opciones_de_interfaz.read_bytes() == contenido
    if tipo is QDateEdit:
        assert otra.date() == anterior.date()
    else:
        assert otra.text() == anterior.text()


def test_abrir_web_reports_sin_preferencias_no_escribe(app, tmp_path, opciones_de_interfaz):
    ventana = WebReportsWindow(tmp_path)
    hoy = QDate.currentDate()
    assert ventana.desde_edit.date() == QDate(hoy.year(), hoy.month(), 1)
    assert ventana.hasta_edit.date() == hoy
    assert not opciones_de_interfaz.exists()
