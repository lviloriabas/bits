"""Las opciones de la interfaz siguen donde se dejaron al volver a abrir.

Una casilla que vuelve a su valor de fabrica en cada arranque obliga a
rehacer el mismo ajuste todos los dias, y la que se guardaba dentro de un
archivo versionado (las discrepancias, en el JSON de la plantilla) dejaba el
repositorio sucio y bloqueaba el pull en la otra maquina. Aqui se comprueban
las dos cosas: que cada control nace con lo ultimo elegido y que todo eso
vive en el archivo local de la interfaz.

Cerrar y volver a abrir se representa construyendo otra ventana: el valor no
se copia de un objeto a otro, se relee del disco.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.gui.memoria import AIRVAULT, PRINCIPAL, SALIDA, VISOR, WEB_REPORTS
from tests.conftest import soltar_hilos


def _guardado(archivo: Path) -> dict:
    if not archivo.is_file():
        return {}
    return json.loads(archivo.read_text(encoding="utf-8"))


# ── la pieza que lo hace ───────────────────────────────────────────


def test_recordar_repone_sin_disparar_la_senal(app, opciones_de_interfaz):
    """Restaurar no es elegir: no puede correr lo que hace el control."""
    from PySide6.QtWidgets import QCheckBox

    from app.gui.memoria import recordar

    # Alguien la apago en una sesion anterior.
    anterior = QCheckBox()
    anterior.setChecked(True)
    recordar("prueba", "casilla", anterior)
    anterior.setChecked(False)

    avisos: list[bool] = []
    marcada = QCheckBox()
    marcada.setChecked(True)
    marcada.toggled.connect(avisos.append)
    recordar("prueba", "casilla", marcada)
    assert not marcada.isChecked()
    assert avisos == []


def test_recordar_no_escribe_hasta_que_alguien_mueve_el_control(
    app, opciones_de_interfaz
):
    from PySide6.QtWidgets import QCheckBox

    from app.gui.memoria import recordar

    casilla = QCheckBox()
    recordar("prueba", "intacta", casilla)
    assert "prueba.intacta" not in _guardado(opciones_de_interfaz)
    casilla.setChecked(True)
    assert _guardado(opciones_de_interfaz)["prueba.intacta"] is True


def test_un_desplegable_se_guarda_por_su_texto_y_no_por_su_sitio(
    app, opciones_de_interfaz
):
    """El indice cambia al reordenar las opciones; el texto elegido no."""
    from PySide6.QtWidgets import QComboBox

    from app.gui.memoria import recordar

    combo = QComboBox()
    combo.addItem("Primera", 1)
    combo.addItem("Segunda", 2)
    recordar("prueba", "combo", combo)
    combo.setCurrentIndex(1)
    assert _guardado(opciones_de_interfaz)["prueba.combo"] == "Segunda"

    reordenado = QComboBox()
    reordenado.addItem("Segunda", 2)
    reordenado.addItem("Primera", 1)
    recordar("prueba", "combo", reordenado)
    assert reordenado.currentText() == "Segunda"


def test_un_valor_fuera_del_rango_no_rompe_el_contador(app, opciones_de_interfaz):
    from PySide6.QtWidgets import QSpinBox

    from app.gui.memoria import recordar

    opciones_de_interfaz.write_text(
        json.dumps({"prueba.spin": 9999}), encoding="utf-8"
    )
    spin = QSpinBox()
    spin.setRange(1, 60)
    spin.setValue(5)
    recordar("prueba", "spin", spin)
    assert spin.value() == 5


def test_un_archivo_danado_no_impide_abrir(app, opciones_de_interfaz):
    from PySide6.QtWidgets import QCheckBox

    from app.gui.memoria import recordar

    opciones_de_interfaz.write_text("{esto no es json", encoding="utf-8")
    casilla = QCheckBox()
    casilla.setChecked(True)
    recordar("prueba", "rota", casilla)
    assert casilla.isChecked()


# ── ventana principal y cuadro de salida ───────────────────────────


@pytest.fixture
def principal(app):
    """Abre ventanas principales y las suelta todas al terminar."""
    from app.gui.main_window import MainWindow

    abiertas = []

    def abrir():
        ventana = MainWindow()
        abiertas.append(ventana)
        return ventana

    yield abrir

    for ventana in abiertas:
        ventana.close()
        soltar_hilos(ventana)
    app.processEvents()


def test_verificar_matriculas_se_queda_apagada(principal):
    primera = principal()
    assert primera.fleet_check.isChecked()  # como viene de fabrica
    primera.fleet_check.setChecked(False)
    assert not principal().fleet_check.isChecked()


def test_los_campos_de_la_vista_previa_vuelven_como_se_dejaron(principal):
    primera = principal()
    primera.fields_check.setChecked(True)
    primera.important_fields_check.setChecked(True)

    otra = principal()
    assert otra.fields_check.isChecked()
    assert otra.important_fields_check.isChecked()
    # «Columnas importantes» solo se puede tocar con los campos a la vista,
    # y eso lo arrastra una senal que al reponer va bloqueada.
    assert otra.important_fields_check.isEnabled()


def test_las_columnas_importantes_nacen_apagadas_sin_los_campos(principal):
    primera = principal()
    primera.fields_check.setChecked(True)
    primera.fields_check.setChecked(False)
    assert not principal().important_fields_check.isEnabled()


def test_la_separacion_y_el_formato_de_salida_se_recuerdan(principal):
    salida = principal().export_options
    salida.set_un_solo_pdf(False)
    salida.matricula_check.setChecked(False)
    salida.mes_check.setChecked(True)
    salida.errores_check.setChecked(True)
    salida.discrepancias_check.setChecked(False)

    otra = principal().export_options
    assert not otra.un_solo_pdf()
    assert not otra.matricula_check.isChecked()
    assert otra.mes_check.isChecked()
    assert otra.errores_check.isChecked()
    assert not otra.discrepancias_check.isChecked()
    assert otra.separar_por() == ["mes"]


def test_la_casilla_de_dividir_vuelve_con_su_fila_ajustada(principal):
    """Con varios PDF no se divide nada, y el control tiene que decirlo."""
    primera = principal().export_options
    primera.partes_check.setChecked(True)
    primera.set_un_solo_pdf(False)

    otra = principal().export_options
    assert otra.partes_check.isChecked()
    assert not otra.partes_check.isEnabled()
    assert not otra.partes_spin.isEnabled()


def test_todo_eso_vive_en_el_archivo_local_de_la_interfaz(
    principal, opciones_de_interfaz
):
    ventana = principal()
    ventana.fleet_check.setChecked(False)
    ventana.export_options.mes_check.setChecked(True)
    guardado = _guardado(opciones_de_interfaz)
    assert guardado[f"{PRINCIPAL}.verificar_matriculas"] is False
    assert guardado[f"{SALIDA}.separar_mes"] is True


def test_la_plantilla_elegida_se_guarda_por_su_nombre(
    principal, opciones_de_interfaz
):
    """Por el nombre y no por la ruta: la carpeta se copia a otra maquina."""
    ventana = principal()
    if ventana.template_combo.count() < 2:
        pytest.skip("hace falta mas de una plantilla instalada")
    ventana.template_combo.setCurrentIndex(1)
    esperado = ventana.template_combo.currentText()
    guardado = _guardado(opciones_de_interfaz)[f"{PRINCIPAL}.plantilla"]
    assert guardado == esperado
    assert "/" not in guardado and "\\" not in guardado
    assert principal().template_combo.currentText() == esperado


# ── visor de CSV ───────────────────────────────────────────────────


def test_el_visor_recuerda_como_se_mira_el_csv(app, tmp_path, opciones_de_interfaz):
    from app.gui.csv_viewer import CsvViewerWindow

    primero = CsvViewerWindow(tmp_path)
    primero.fields_check.setChecked(True)
    primero.column_toggle.setChecked(False)
    guardado = _guardado(opciones_de_interfaz)
    assert guardado[f"{VISOR}.mostrar_campos"] is True
    assert guardado[f"{VISOR}.columnas_importantes"] is False

    otro = CsvViewerWindow(tmp_path)
    assert otro.fields_check.isChecked()
    assert not otro.column_toggle.isChecked()


# ── AirVault ───────────────────────────────────────────────────────


def test_airvault_recuerda_sus_casillas_y_el_intervalo(app, tmp_path):
    from app.gui.airvault_window import AirVaultWindow
    from app.gui.automatizacion import OpcionesAutomatizacion

    opciones = OpcionesAutomatizacion(tmp_path)
    primera = AirVaultWindow(tmp_path, opciones)
    primera.compresion_check.setChecked(True)
    primera.solo_ejecucion_check.setChecked(True)
    primera.auto_check.setChecked(False)
    primera.minutos_spin.setValue(7)

    otra = AirVaultWindow(tmp_path, opciones)
    assert otra.compresion_check.isChecked()
    assert otra.solo_ejecucion_check.isChecked()
    assert not otra.auto_check.isChecked()
    assert otra.minutos_spin.value() == 7


def test_airvault_no_arranca_la_vigilancia_al_reponer(app, tmp_path):
    """Reponer no es elegir: sin batches que esperar no hay que preguntar."""
    from app.gui.airvault_window import AirVaultWindow
    from app.gui.automatizacion import OpcionesAutomatizacion

    opciones = OpcionesAutomatizacion(tmp_path)
    primera = AirVaultWindow(tmp_path, opciones)
    primera.auto_check.setChecked(True)

    otra = AirVaultWindow(tmp_path, opciones)
    assert otra.auto_check.isChecked()
    assert otra._vigilante is None or not otra._vigilante.isActive()


# ── Web Reports ────────────────────────────────────────────────────


def test_web_reports_recuerda_el_filtro_y_las_previas(
    app, tmp_path, opciones_de_interfaz
):
    from app.gui.web_reports_window import WebReportsWindow

    primera = WebReportsWindow(tmp_path)
    primera.mostrar_previas.setChecked(False)
    primera.filtro_combo.setCurrentIndex(2)
    elegido = primera.filtro_combo.currentText()
    assert _guardado(opciones_de_interfaz)[f"{WEB_REPORTS}.filtro"] == elegido

    otra = WebReportsWindow(tmp_path)
    assert not otra.mostrar_previas.isChecked()
    assert otra.filtro_combo.currentText() == elegido
    assert otra.filtro_combo.currentData() == primera.filtro_combo.currentData()


def test_cada_ventana_lleva_su_propia_seccion():
    """Dos ventanas con la misma casilla no pueden pisarse la preferencia."""
    secciones = [PRINCIPAL, SALIDA, AIRVAULT, VISOR, WEB_REPORTS]
    assert len(set(secciones)) == len(secciones)
