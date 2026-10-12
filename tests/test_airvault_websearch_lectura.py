"""La consulta espera filas completas y recupera una pestaña interrumpida."""

import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QListWidgetItem

from app.airvault import websearch_pagina as modulo
from app.airvault.config import AirVaultConfig
from app.airvault.errores_websearch import ErrorDeWebSearch, MOTIVOS, mensaje_error_websearch
from app.airvault.session import ErrorDeSesion, SesionCancelada
from app.airvault.web_reports import ErrorDePaginaAirVault
from app.gui.airvault_window import AirVaultWindow
from tests.test_airvault_websearch_pagina import NavegadorFalso, PaginaFalsa


def rejilla(config):
    node = os.environ.get("BITS_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Hace falta Node para ejecutar la lectura JavaScript contra una rejilla simulada")
    if subprocess.run([node, "--version"], capture_output=True).returncode:
        pytest.skip("Node no tiene una version configurada en este equipo")
    codigo = r"""
const vm = require('vm');
const entrada = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const cfg = entrada.config;
const filas = (cfg.filas || []).map((datos, n) => ({id: String(n), datos}));
const tabla = {p: cfg.parametros || {}, grid: {hDiv: {loading: !!cfg.cargando}},
               querySelectorAll: () => filas};
const pie = {innerText: cfg.pie || 'View 1 - 1 of 1'};
function jQuery() { return {jqGrid: (accion, clave) => {
  if (cfg.pluginFalla) throw new Error('plugin anterior');
  return tabla.p[clave];
}}; }
jQuery.getResultRowData = id => filas[Number(id)].datos;
const contexto = {jQuery, window: {jQuery}, location: {pathname: '/zfp/', hostname: 'airvault.test'},
 document: {querySelector: selector => selector === '.ui-jqgrid-btable' ? tabla :
 selector === '[id$=Pager_right]' ? pie : null, getElementById: () => null}};
const lectura = vm.runInNewContext(entrada.lectura, contexto);
const listo = vm.runInNewContext(entrada.listo, contexto);
process.stdout.write(JSON.stringify({lectura, listo: !!listo}));
"""
    resultado = subprocess.run([node, "-e", codigo], capture_output=True, text=True,
                              input=json.dumps(dict(config=config, lectura=modulo._LEER_MUESTRA,
                                                    listo=modulo._LISTA_O_ACCESO)))
    assert resultado.returncode == 0, resultado.stderr
    return json.loads(resultado.stdout)


def test_el_pie_visible_no_da_por_cargadas_filas_que_siguen_en_vuelo():
    resultado = rejilla(dict(cargando=True, parametros=dict(records=0, reccount=0),
                            pie="No records to view"))
    assert resultado == dict(lectura=None, listo=False)


@pytest.mark.parametrize("parametros,plugin_falla", [
    (dict(records=1, reccount=1), False),
    (dict(records=1), False),
    (dict(records=1, reccount=1), True),
])
def test_lee_filas_validas_con_contadores_y_versiones_de_rejilla_distintos(parametros, plugin_falla):
    resultado = rejilla(dict(filas=[dict(LogNo="2000001")], parametros=parametros,
                            pluginFalla=plugin_falla))
    assert resultado == dict(lectura=[dict(log="2000001")], listo=True)


@pytest.mark.parametrize("config", [
    dict(parametros={}, pie="View"),
    dict(parametros=dict(records=2, reccount=2), filas=[dict(LogNo="2000001")]),
    dict(parametros=dict(records=1, reccount=1), filas=[dict(otro="2000001")]),
])
def test_una_rejilla_incompleta_no_prueba_que_falte_la_bitacora(config):
    assert rejilla(config) == dict(lectura=None, listo=False)


def test_solo_una_busqueda_vacia_confirmada_prueba_la_ausencia():
    resultado = rejilla(dict(parametros=dict(records=0, reccount=0), pie="No records to view"))
    assert resultado == dict(lectura=[], listo=True)


def acceso():
    return SimpleNamespace(config=AirVaultConfig(),
        sesion=object(), generacion=0, navegador=NavegadorFalso(), version={}, cancelado=lambda: False)


def test_reabre_la_pestana_si_la_primera_consulta_no_carga(monkeypatch):
    primera, segunda = PaginaFalsa(), PaginaFalsa()
    primera.lista = False
    paginas = iter((primera, segunda))
    monkeypatch.setattr(modulo, "_Pagina", lambda *_a, **_k: next(paginas))
    lector = modulo.LectorWebSearch(acceso())
    assert lector.consultar_numero("2000001", 15) == {"rows": [{"LogNo": "2000001"}]}
    assert primera.cerrada
    assert len(lector.acceso.navegador.urls) == 2
    assert lector.acceso.navegador.urls[0] == lector.acceso.navegador.urls[1]
    lector.cerrar()


def test_el_reintento_no_extiende_el_tiempo_del_batch(monkeypatch):
    reloj, creadas = [0], []
    monkeypatch.setattr(modulo.time, "monotonic", lambda: reloj[0])
    primera = PaginaFalsa()

    def esperar(*_a, **_k):
        reloj[0] = 20
        return False

    primera.esperar = esperar
    monkeypatch.setattr(modulo, "_Pagina", lambda *_a, **_k: creadas.append(primera) or primera)
    lector = modulo.LectorWebSearch(acceso())
    with pytest.raises(ErrorDeWebSearch, match="no terminó de cargar"):
        lector.consultar_numero("2000001", 15)
    assert len(creadas) == 1


def test_cancelar_no_abre_un_reintento(monkeypatch):
    a = acceso()
    a.cancelado = lambda: True
    monkeypatch.setattr(modulo, "_Pagina", lambda *_a, **_k: pytest.fail("No debe abrirse"))
    with pytest.raises(SesionCancelada):
        modulo.LectorWebSearch(a).consultar_numero("2000001", 15)


@pytest.mark.parametrize("aviso,tipo", [
    ("Access denied.", ErrorDeWebSearch),
    ("Your session has expired.", ErrorDeSesion),
])
def test_un_rechazo_del_servidor_no_se_toma_como_ausencia(monkeypatch, aviso, tipo):
    pagina = PaginaFalsa()
    pagina.filas = []
    original = pagina.evaluar
    pagina.evaluar = lambda expresion: aviso if expresion == modulo._MENSAJE else original(expresion)
    monkeypatch.setattr(modulo, "_Pagina", lambda *_a, **_k: pagina)
    lector = modulo.LectorWebSearch(acceso())
    with pytest.raises(tipo):
        lector.consultar_numero("2000001", 15)
    assert len(lector.acceso.navegador.urls) == 1
    assert modulo._LEER_MUESTRA not in pagina.expresiones
    lector.cerrar()


@pytest.mark.parametrize("error,clave", [
    (ErrorDeSesion("Sesión privada"), "acceso"),
    (ErrorDePaginaAirVault("La pestaña se cerró"), "navegador"),
    (ErrorDeWebSearch("carga"), "carga"),
    (ValueError("C:/interno/privado token=secreto"), "desconocido"),
])
def test_la_causa_publica_se_conserva_sin_exponer_datos_internos(error, clave):
    texto = mensaje_error_websearch(error)
    assert texto == MOTIVOS[clave]
    assert mensaje_error_websearch(texto) == texto
    assert "privado" not in texto and "secreto" not in texto


def test_un_bloqueo_de_conexion_local_no_se_confunde_con_un_archivo():
    error = PermissionError("Conexión local bloqueada")
    error.winerror = 10013
    assert mensaje_error_websearch(error) == MOTIVOS["bloqueada"]


def test_la_revision_muestra_el_batch_actual_y_las_bitacoras_revisadas(app, tmp_path, monkeypatch):
    ventana = AirVaultWindow(tmp_path)
    lector = SimpleNamespace(modo="buscar_websearch", hay_que_parar=lambda: False)
    monkeypatch.setattr(ventana, "hilo", lambda: None)
    monkeypatch.setattr(ventana, "lectura_websearch", lambda: lector)
    ventana._linea_viva = QListWidgetItem()
    ventana._cuenta_websearch = (2, 7)
    ventana._mostrar_paso_websearch("Revisando números de bitácora del batch «Entrega -3» en Web Search", 4, 25)
    assert "Entrega -3" in ventana.estado_label.text()
    assert "4 de 25 bitácoras revisadas" in ventana.estado_label.text()
    assert "Entrega -3" in ventana._linea_viva.text()
    assert "2 de 7 batches" in ventana._linea_viva.text()


def test_el_error_de_websearch_se_ve_completo_sin_mensaje_al_pasar_el_cursor(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana._al_fallar_websearch(MOTIVOS["carga"])
    entrada = ventana.bitacora.item(ventana.bitacora.count() - 1)
    assert MOTIVOS["carga"] in entrada.text()
    assert entrada.toolTip() == ""
    assert ventana.estado_label.toolTip() == ""
