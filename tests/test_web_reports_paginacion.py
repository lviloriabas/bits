"""Regresiones de paginacion, grupos y parametros del visor de AirVault."""

from datetime import date, timedelta
import json

import pytest
from PySide6.QtQml import QJSEngine
from PySide6.QtCore import QDate

from app.airvault.config import AirVaultConfig
from app.airvault.web_reports import (
    ClienteLogPageAudit, ConsultaCancelada, ParametrosLogPageAudit,
    _CONTROL, OPCIONES_LIBRO, OPCIONES_MINIMO, OPCIONES_ORDEN,
    parsear_filas, TIPO_DUPLICADA, TIPO_MAL_INDEXADA,
)
from app.gui.web_reports_window import WebReportsWindow


def fila(detalle, matricula="HP-9913CMP", rango="2008150 - 2008199"):
    return [matricula, "Copa-7 (50)", "", rango, "9/1/2026 - 9/30/2026",
            "0", "2", "0", "", detalle]


def continuacion(detalle):
    return [""] * 9 + [detalle]


def test_conserva_el_libro_de_las_filas_siguientes_incluso_tras_la_cabecera():
    resultado = parsear_filas([
        fila("DUPLICATED 2008152(2x)"),
        continuacion("DUPLICATED 2008153(2x)"),
        ["AC#", "Book Type", "", "Book Range", "Date Range", "Missing", "Dups", "Susp Date", "", "Exception"],
        continuacion("2008154 MIS-INDEX to ACN [HP-9813CMP]"),
        fila("DUPLICATED 2008155(2x)", "HP-9914CMP"),
    ], AirVaultConfig())
    assert [r.log_number for r in resultado] == ["2008152", "2008153", "2008154", "2008155"]
    assert [r.matricula_libro for r in resultado] == ["HP-9913CMP"] * 3 + ["HP-9914CMP"]
    assert resultado[2].destino == "HP-9813CMP"
    assert all(r.rango_libro == "2008150 - 2008199" for r in resultado)


def test_no_inventa_un_libro_para_una_continuacion_huerfana():
    assert parsear_filas([continuacion("DUPLICATED 2008152(2x)")], AirVaultConfig()) == []


def test_no_elimina_casos_del_mismo_numero_en_libros_distintos():
    assert len(parsear_filas([
        fila("DUPLICATED 2008152(2x)"),
        fila("DUPLICATED 2008152(2x)", rango="2008100 - 2008149"),
    ], AirVaultConfig())) == 2


def test_conserva_el_repositorio_del_enlace_nativo_en_todas_las_filas():
    primera = fila("DUPLICATED 2008152(2x)")
    primera[2] = {"t": "", "repo_id": 999}
    resultado = parsear_filas([primera, continuacion("DUPLICATED 2008153(2x)")], AirVaultConfig())
    assert all("repoId=999" in r.url_busqueda for r in resultado)
    assert all("repoId=999" in r.url_busqueda_libro for r in resultado)


class Paginas:
    def __init__(self, paginas, totales=None):
        self.paginas = paginas
        self.numero = 1
        self.totales = totales or [str(len(paginas))] * len(paginas)
        self.leidas = []
        self.cancelada = False
        self.avanza = True

    def evaluar(self, expresion):
        if self.cancelada:
            raise ConsultaCancelada()
        if "function(areaId, esperaId" in expresion:
            return {
                "huella": f"contenido-pagina-{self.numero}", "texto": "Log Page Audit",
                "actual": self.numero, "total": self.totales[self.numero - 1],
                "siguiente": self.numero < len(self.paginas), "ocupada": False,
                "valores": {}, "nulos": {},
            }
        if "var salida = []" in expresion:
            self.leidas.append(self.numero)
            return self.paginas[self.numero - 1]
        if "c.click(); return true" in expresion:
            if not self.avanza:
                return False
            self.numero += 1
            return True
        raise AssertionError(expresion)

    def _dormir(self, _s):
        pytest.fail("El visor simulado ya esta listo")


def test_lee_todas_las_paginas_incluso_con_total_provisional():
    pagina = Paginas([
        [fila("DUPLICATED 2008152(2x)")],
        [continuacion("DUPLICATED 2008153(2x)")],
        [fila("2008154 MIS-INDEX to ACN [HP-9813CMP]")],
    ], ["2?", "3?", "3"])
    avisos = []
    progreso = []
    filas = ClienteLogPageAudit._leer_todas_paginas(
        pagina, avisar=avisos.append, progreso=lambda *paso: progreso.append(paso))
    resultado = parsear_filas(filas, AirVaultConfig())
    assert pagina.leidas == [1, 2, 3]
    assert [r.log_number for r in resultado] == ["2008152", "2008153", "2008154"]
    assert avisos[-1] == "Leyendo página 3 de 3"
    assert progreso == [(1, 0), (2, 0), (3, 3)]


def test_informa_cada_pagina_leida_con_el_total_confirmado():
    pasos = []
    ClienteLogPageAudit._leer_todas_paginas(
        Paginas([[], [], []]), progreso=lambda *paso: pasos.append(paso))
    assert pasos == [(1, 3), (2, 3), (3, 3)]


def test_la_barra_cuenta_la_lectura_y_conserva_el_total_de_paginas(app, tmp_path):
    ventana = WebReportsWindow(tmp_path)
    assert ventana.progreso.isTextVisible()
    ventana._al_leer_paginas(1, 4, 1, 2)
    assert ventana.progreso.value() == 12
    assert ventana.progreso.format() == "Lectura: %p%"
    ventana._al_leer_paginas(4, 4, 1, 2)
    assert ventana.progreso.value() == 50
    ventana._al_leer_paginas(1, 2, 2, 2)
    assert ventana.progreso.value() == 75
    ventana._al_leer_paginas(2, 2, 2, 2)
    ventana._al_recibir([])
    ventana._al_terminar()
    assert ventana.progreso.value() == 100
    assert ventana.resumen.text().startswith("Leídas 6 páginas del reporte.")


def test_no_inventa_un_porcentaje_si_ssrs_no_confirma_el_total(app, tmp_path):
    ventana = WebReportsWindow(tmp_path)
    ventana._al_leer_paginas(2, 0, 1, 1)
    assert ventana.progreso.value() == 0
    assert "total por confirmar" in ventana.progreso.format()
    ventana._al_leer_paginas(3, 3, 1, 1)
    ventana._al_recibir([])
    assert ventana.progreso.value() == 100
    assert "total por confirmar" not in ventana.progreso.format()
    assert "Leídas 3 páginas del reporte." in ventana.resumen.text()


def test_no_devuelve_resultados_parciales_si_siguiente_falla():
    pagina = Paginas([[fila("DUPLICATED 2008152(2x)")], []])
    pagina.avanza = False
    with pytest.raises(RuntimeError, match="incompleto"):
        ClienteLogPageAudit._leer_todas_paginas(pagina)


@pytest.mark.parametrize("total", ["3", "1?"])
def test_la_ultima_pagina_debe_confirmar_el_total(total):
    with pytest.raises(RuntimeError, match="incompleto"):
        ClienteLogPageAudit._leer_todas_paginas(Paginas([[]], [total]))


def test_cancelar_durante_el_recorrido_interrumpe_la_consulta():
    pagina = Paginas([[], []])
    def avisar(_texto):
        pagina.cancelada = True
    with pytest.raises(ConsultaCancelada):
        ClienteLogPageAudit._leer_todas_paginas(pagina, avisar=avisar)


def test_espera_la_pagina_nueva_y_no_relee_el_reporte_anterior(monkeypatch):
    estados = iter([
        {"huella": "viejo", "texto": "Log Page Audit", "actual": 1},
        {"huella": "nuevo", "texto": "Log Page Audit", "actual": 2, "ocupada": True},
        {"huella": "nuevo", "texto": "Log Page Audit", "actual": 2},
    ])
    monkeypatch.setattr(ClienteLogPageAudit, "_estado_reporte", staticmethod(lambda _p: next(estados)))
    esperas = []
    class Pagina:
        _dormir = staticmethod(esperas.append)
    ClienteLogPageAudit._esperar_reporte(Pagina(), "viejo", 2)
    assert len(esperas) == 2


@pytest.mark.parametrize("id, valor", [("ctl07_txtValue", "9/7/2026"), ("ctl17_ddValue", "8")])
def test_si_airvault_cambia_la_fecha_o_el_filtro_no_se_acepta_el_reporte(id, valor):
    with pytest.raises(RuntimeError, match="filtros o las fechas"):
        ClienteLogPageAudit._comprobar_parametros(
            {"valores": {_CONTROL + id: valor}}, {_CONTROL + id: "otro"}, {})


def test_acepta_los_ceros_de_las_fechas_devuelta_por_ssrs():
    ClienteLogPageAudit._comprobar_parametros(
        {"valores": {_CONTROL + "ctl07_txtValue": "09/07/2026"}},
        {_CONTROL + "ctl07_txtValue": "9/7/2026"}, {})


def test_un_null_inesperado_no_ignora_las_fechas():
    with pytest.raises(RuntimeError, match="límite de fechas"):
        ClienteLogPageAudit._comprobar_parametros(
            {"nulos": {_CONTROL + "ctl07_cbNull": True}}, {},
            {_CONTROL + "ctl07_cbNull": False})


@pytest.mark.parametrize("sin_inicio,sin_fin", [(False, False), (True, False), (False, True), (True, True)])
def test_escribe_todos_los_parametros_y_los_null_en_el_formulario_real_js(
    app, monkeypatch, sin_inicio, sin_fin,
):
    engine = QJSEngine()
    datos = {}
    for numero, valores in ((5, ["1", "2", "3"]), (15, ["1", "2", "3", "4", "5"]),
                           (17, ["8", "10"]), (19, ["1", "2", "3"]),
                           (21, ["1", "2"]), (23, ["1", "2"])):
        datos[f"{_CONTROL}ctl{numero:02}_ddValue"] = {"value": "1", "options": [{"value": v} for v in valores]}
    for numero in (7, 9, 11, 13):
        datos[f"{_CONTROL}ctl{numero:02}_txtValue"] = {"value": "anterior"}
    for numero in (7, 9):
        datos[f"{_CONTROL}ctl{numero:02}_cbNull"] = {"checked": True}
    engine.evaluate("var controles = " + json.dumps(datos) + "; var envios = 0;")
    engine.evaluate("""
        Object.keys(controles).forEach(id => {
          var c = controles[id]; c.click = function(){c.checked = !c.checked;};
        });
        controles.ReportViewerControl_ctl04_ctl00 = {click: function(){envios++;}};
        var document = {getElementById: id => controles[id]};
    """)
    class DOM:
        def evaluar(self, expresion):
            r = engine.evaluate(expresion)
            assert not r.isError(), r.toString()
            return r.toVariant()
    comprobaciones = []
    monkeypatch.setattr(ClienteLogPageAudit, "_estado_reporte", staticmethod(lambda _p: {"huella": "viejo"}))
    monkeypatch.setattr(ClienteLogPageAudit, "_esperar_reporte", staticmethod(lambda *args: comprobaciones.append(args)))
    monkeypatch.setattr(ClienteLogPageAudit, "_leer_todas_paginas", staticmethod(lambda *_a: []))
    parametros = ParametrosLogPageAudit(tipo_libro="3", aeronaves="HP-9913CMP", bitacoras="2008152",
                                      minimo_paginas="5", orden="3", actualizar="2")
    ClienteLogPageAudit(AirVaultConfig())._correr_reporte(
        DOM(), None if sin_inicio else date(2026, 9, 7),
        None if sin_fin else date(2026, 10, 2), "10", parametros)
    puestos = engine.evaluate("controles").toVariant()
    assert puestos[_CONTROL + "ctl07_cbNull"]["checked"] == sin_inicio
    assert puestos[_CONTROL + "ctl09_cbNull"]["checked"] == sin_fin
    assert puestos[_CONTROL + "ctl07_txtValue"]["value"] == ("" if sin_inicio else "9/7/2026")
    assert puestos[_CONTROL + "ctl09_txtValue"]["value"] == ("" if sin_fin else "10/2/2026")
    assert [puestos[_CONTROL + id]["value"] for id in (
        "ctl05_ddValue", "ctl11_txtValue", "ctl13_txtValue", "ctl15_ddValue",
        "ctl17_ddValue", "ctl19_ddValue", "ctl21_ddValue", "ctl23_ddValue")
    ] == ["3", "HP-9913CMP", "2008152", "5", "10", "3", "1", "2"]
    assert engine.evaluate("envios").toInt() == 1
    assert comprobaciones[0][2] == 1


def test_los_menus_corresponden_a_airvault_y_mostrar_solo_ofrece_tres(app, tmp_path):
    ventana = WebReportsWindow(tmp_path)
    try:
        assert [ventana.filtro_combo.itemData(i) for i in range(3)] == [("8", "10"), ("8",), ("10",)]
        assert ventana.filtro_combo.count() == 3
        for control, opciones in ((ventana.libro_combo, OPCIONES_LIBRO),
                                  (ventana.minimo_combo, OPCIONES_MINIMO), (ventana.orden_combo, OPCIONES_ORDEN)):
            assert [(control.itemText(i), control.itemData(i)) for i in range(control.count())] == list(opciones)
        assert not hasattr(ventana, "exportar_combo")
        assert ventana.actualizar_combo.currentText() == "YES"
        assert ventana._parametros_consulta().actualizar == "1"
        ventana.actualizar_combo.setCurrentIndex(1)
        assert ventana._parametros_consulta().actualizar == "2"
        ventana._habilitar(False)
        assert not ventana.desde_edit.isEnabled()
        assert not ventana.hasta_edit.isEnabled()
        ventana._habilitar(True)
        assert ventana.desde_edit.isEnabled()
        assert ventana.hasta_edit.isEnabled()
        ventana.minimo_combo.setCurrentIndex(4)
        ventana.libro_combo.setCurrentIndex(2)
        ventana.orden_combo.setCurrentIndex(2)
        assert ventana._parametros_consulta().minimo_paginas == "5"
        assert ventana._parametros_consulta().tipo_libro == "3"
        assert ventana._parametros_consulta().orden == "3"
        hoy = QDate.currentDate()
        for campo in (ventana.desde_edit, ventana.hasta_edit):
            assert campo.maximumDate() == hoy
            campo.setDate(hoy.addDays(1))
            assert campo.date() == hoy
    finally:
        ventana.close()


@pytest.mark.parametrize("inicio,fin", [
    (date.today(), date.today() + timedelta(days=1)),
    (date.today() + timedelta(days=1), date.today()),
])
def test_el_cliente_rechaza_fechas_futuras_antes_de_abrir_edge(inicio, fin):
    with pytest.raises(ValueError, match="día actual"):
        ClienteLogPageAudit(AirVaultConfig()).consultar(inicio, fin, ["8"])


def test_un_rango_invertido_se_rechaza_antes_de_abrir_edge():
    with pytest.raises(ValueError, match="posterior a la final"):
        ClienteLogPageAudit(AirVaultConfig()).consultar(date(2026, 9, 7), date(2026, 9, 1), ["10"])


@pytest.mark.parametrize("nombre,guardado,control,valor", [
    ("repositorio", "Pruebas", "repositorio_combo", "2"),
    ("tipo_libro", "Todos", "libro_combo", "1"),
    ("orden", "Aeronave y fecha final del libro", "orden_combo", "3"),
    ("actualizar", "No", "actualizar_combo", "2"),
])
def test_renombrar_las_opciones_conserva_el_filtro_guardado(
    app, tmp_path, opciones_de_interfaz, nombre, guardado, control, valor,
):
    opciones_de_interfaz.write_text(json.dumps({
        f"web_reports.{nombre}": guardado,
        "web_reports.para_exportar": "Sí",
    }), encoding="utf-8")
    ventana = WebReportsWindow(tmp_path)
    try:
        combo = getattr(ventana, control)
        assert combo.currentData() == valor
        assert combo.findText(guardado) == -1
        assert not hasattr(ventana._parametros_consulta(), "para_exportar")
    finally:
        ventana.close()
