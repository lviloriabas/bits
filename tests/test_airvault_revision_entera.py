"""Revisar todas las bitacoras de un batch y ocultar los ya confirmados.

La muestra dice si un batch esta publicado; no audita cada pagina. Cuando
piden bitacoras que no aparecen hace falta poder mirarlas todas, y poder
quitar de la vista lo que ya esta confirmado para quedarse con lo que falta.
"""

import pytest

from app.airvault.confirmacion import muestra_de_batch, numeros_de_batch
from app.airvault.flujo import COMPLETADO, estado_local, websearch_confirmacion_valida
from app.airvault.manifest import cargar
from app.airvault.model import EstadoEtapa
from app.gui import airvault_window as modulo
from app.gui.airvault_window import AirVaultWindow
from tests.test_airvault_chequeo_completo import comprobar
from tests.test_airvault_chequeo_reintentos import _bitacora, _en_la_ventana, _terminar
from tests.test_airvault_confirmacion import caso
from tests.test_airvault_revision_inicio import WorkerFalso


# ── revisar todas las bitacoras ────────────────────────────────────

@pytest.mark.parametrize("pedido", ["todos", "carpeta"])
def test_la_revision_completa_consulta_todas_las_bitacoras(app, tmp_path, pedido):
    t, buscador, _, consultas = caso(tmp_path, 50)
    enteros = True if pedido == "todos" else [str(t.carpeta)]
    comprobar(t, buscador, app, todas_las_bitacoras=enteros)
    assert len(consultas) == 50
    assert t.manifiesto.websearch_resultado == "encontrado"
    assert t.manifiesto.websearch_cotejadas == len(t.manifiesto.websearch_muestra) == 50
    assert "Las 50 bitácoras del batch están en Web Search" in t.manifiesto.websearch_detalle
    # Cuenta como confirmacion igual que la muestra, tambien al releerlo.
    assert websearch_confirmacion_valida(t.manifiesto)
    assert websearch_confirmacion_valida(cargar(t.carpeta))
    assert estado_local(t).estado == COMPLETADO


def test_sin_pedirlo_se_sigue_consultando_solo_la_muestra(app, tmp_path):
    t, buscador, _, consultas = caso(tmp_path / "a", 50)
    otro = caso(tmp_path / "b", 50)[0]
    comprobar(t, buscador, app, todas_las_bitacoras=[str(otro.carpeta)])
    assert len(consultas) == 7
    assert websearch_confirmacion_valida(t.manifiesto)


def test_la_revision_completa_ve_la_bitacora_que_la_muestra_no_miro(app, tmp_path):
    """Es el caso que la muestra no puede ver: falta una sola pagina."""
    t, buscador, filas, _ = caso(tmp_path, 50)
    fuera = next(n for n in numeros_de_batch(t.manifiesto)
                 if n not in muestra_de_batch(t.manifiesto))
    filas[:] = [fila for fila in filas if fila["C_DocNo"] != fuera]
    comprobar(t, buscador, app)
    assert t.manifiesto.websearch_resultado == "encontrado"
    comprobar(t, buscador, app, todas_las_bitacoras=True)
    assert t.manifiesto.websearch_resultado == "ausente"
    assert t.manifiesto.websearch_cotejadas == 49
    assert "49 de 50 números de bitácora del batch" in t.manifiesto.websearch_detalle
    assert not websearch_confirmacion_valida(t.manifiesto)


def test_una_revision_completa_a_medias_no_confirma(tmp_path):
    """Todas encontradas o nada: 49 de 50 no es una confirmacion."""
    t, _, _, _ = caso(tmp_path, 50)
    m = t.manifiesto
    from app.airvault.confirmacion import METODO_MUESTRA, huella_de_numeros

    m.websearch_confirmado = "2026-10-12T12:00:00"
    m.websearch_metodo = METODO_MUESTRA
    m.websearch_huella = huella_de_numeros(m)
    m.websearch_muestra = numeros_de_batch(m)
    m.websearch_cotejadas = 49
    assert not websearch_confirmacion_valida(m)
    m.websearch_cotejadas = 50
    assert websearch_confirmacion_valida(m)
    # Ni la muestra ni todas: una lista cualquiera no prueba nada.
    m.websearch_muestra = numeros_de_batch(m)[:20]
    m.websearch_cotejadas = 20
    assert not websearch_confirmacion_valida(m)


@pytest.fixture
def ventana(app, tmp_path, monkeypatch):
    monkeypatch.setattr(modulo, "TrabajoAirVaultWorker", WorkerFalso)
    return AirVaultWindow(tmp_path)


def _acciones(menu):
    return {accion.text(): accion for accion in menu.actions() if not accion.isSeparator()}


def test_acciones_ofrece_la_muestra_y_la_revision_de_todas(ventana, tmp_path):
    trabajos = _en_la_ventana(ventana, tmp_path)
    ventana._cargar_sidebar()
    menu = _acciones(ventana.batch_actions_button.menu())
    assert menu["Verificar todas las bitácoras en Web Search"].isEnabled()
    menu["Verificar todas las bitácoras en Web Search"].trigger()
    estado = ventana._worker_websearch.estado
    assert estado["buscar_trabajos"] == trabajos
    assert estado["todas_las_bitacoras"] == sorted(str(t.carpeta) for t in trabajos)
    assert "Web Search: se revisan 2 batches con todas sus bitácoras, varios a la vez" in _bitacora(ventana)
    _terminar(ventana)
    ventana._parar_reintento_del_chequeo()
    # La orden mas reciente manda: la muestra no arrastra la revision completa.
    menu["Verificar subidas en Web Search"].trigger()
    assert ventana._worker_websearch.estado["todas_las_bitacoras"] == []
    assert "Web Search: se revisan 2 batches, varios a la vez" in _bitacora(ventana)


def test_el_clic_derecho_revisa_enteros_solo_los_batches_elegidos(ventana, tmp_path):
    from app.airvault.flujo import EstadoParte, SIN_SUBIR

    uno, dos = _en_la_ventana(ventana, tmp_path)
    menu = _acciones(ventana._acciones_de_la_cola([EstadoParte(uno, SIN_SUBIR)]))
    menu["Verificar todas las bitácoras en Web Search"].trigger()
    estado = ventana._worker_websearch.estado
    assert estado["buscar_trabajos"] == [uno]
    assert estado["todas_las_bitacoras"] == [str(uno.carpeta)]
    assert "Web Search: se revisa 1 batch con todas sus bitácoras" in _bitacora(ventana)
    assert ventana._websearch_todas == {str(uno.carpeta)}
    assert str(dos.carpeta) not in ventana._websearch_todas


def test_un_nuevo_intento_de_la_revision_completa_no_vuelve_a_la_muestra(ventana, tmp_path):
    trabajos = _en_la_ventana(ventana, tmp_path)
    ventana._buscar_todas_en_websearch()
    ventana._al_fallar_websearch("AirVault no responde")
    _terminar(ventana)
    ventana._reintento_chequeo.stop()
    ventana._repetir_chequeo()
    assert ventana._worker_websearch.estado["todas_las_bitacoras"] == sorted(
        str(t.carpeta) for t in trabajos)


def test_la_revision_completa_se_olvida_cuando_contesta_y_no_antes(ventana, tmp_path):
    uno, dos = _en_la_ventana(ventana, tmp_path)
    ventana._buscar_todas_en_websearch()
    uno.manifiesto.websearch_resultado = "encontrado"
    ventana._al_buscar_websearch({"resultados": [(uno, True)], "revisados": 1, "total": 2})
    assert ventana._websearch_todas == {str(dos.carpeta)}
    dos.manifiesto.websearch_resultado = "error"
    ventana._al_buscar_websearch({"resultados": [(dos, False)], "por_repetir": True,
                                  "revisados": 1, "total": 2})
    # Se retoma al final, y se retoma entero.
    assert ventana._websearch_todas == {str(dos.carpeta)}
    ventana._cancelar()
    assert not ventana._websearch_todas


def test_pedir_entero_un_batch_que_ya_se_lee_por_muestra_lo_deja_en_turno(ventana, tmp_path):
    uno, dos = _en_la_ventana(ventana, tmp_path)
    ventana._buscar_websearch()
    ventana._iniciar_confirmacion([uno], automatico=False, todas=True)
    # El resultado de la muestra no gasta la revision completa pedida.
    uno.manifiesto.websearch_resultado = "encontrado"
    ventana._al_buscar_websearch({"resultados": [(uno, True)], "revisados": 1, "total": 2})
    _terminar(ventana)
    estado = ventana._worker_websearch.estado
    assert estado["buscar_trabajos"] == [uno]
    assert estado["todas_las_bitacoras"] == [str(uno.carpeta)]


# ── ocultar los confirmados ────────────────────────────────────────

def test_la_casilla_oculta_los_confirmados_y_deja_lo_que_falta(app, tmp_path):
    confirmado, buscador, _, _ = caso(tmp_path / "a", 50)
    comprobar(confirmado, buscador, app)
    # Marcado como completado sin estar en Web Search: justo el que hay que ver.
    sin_prueba = caso(tmp_path / "b", 50)[0]
    for etapa in ("subir", "completar"):
        sin_prueba.manifiesto.etapa(etapa).marcar(EstadoEtapa.HECHA)
    sin_prueba.guardar()
    pendiente = caso(tmp_path / "c", 50, conocido=False)[0]
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [confirmado, sin_prueba, pendiente]
    ventana._estados = [estado_local(t) for t in ventana._trabajos]
    ventana._pintar_lotes()

    casilla = ventana.ocultar_confirmados_check
    assert casilla.text() == "Ocultar batches confirmados"
    assert not casilla.isChecked()
    assert ventana.lotes.count() == 3
    casilla.setChecked(True)
    assert [p.trabajo for p in ventana._partes_en_cola()] == [sin_prueba, pendiente]
    assert ventana.lotes.count() == 2
    # La de completados es otra cosa: tambien se lleva al que no tiene prueba.
    casilla.setChecked(False)
    ventana.ocultar_completados_check.setChecked(True)
    assert [p.trabajo for p in ventana._partes_en_cola()] == [pendiente]
    ventana.ocultar_completados_check.setChecked(False)
    # Solo cambia la vista: el chequeo sigue revisando todos.
    casilla.setChecked(True)
    assert ventana._por_confirmar_websearch(incluir_confirmados=True) == ventana._trabajos


def test_la_casilla_va_con_las_demas_y_se_recuerda(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    casilla = ventana.ocultar_confirmados_check
    # Al final, para no mover de sitio a las que ya estaban.
    assert ventana._controles_filtro_batches()[-1] is casilla
    assert ventana._controles_filtro_batches()[1:3] == (
        ventana.ocultar_indexados_check, ventana.ocultar_completados_check)
    assert casilla.height() == ventana.ocultar_completados_check.height()
    assert casilla.toolTip()
    espejo = _acciones(ventana.batch_actions_button.menu())["Ocultar batches confirmados"]
    espejo.setChecked(True)
    assert casilla.isChecked()
    ventana._habilitar(False)
    assert casilla.isEnabled()
    assert AirVaultWindow(tmp_path).ocultar_confirmados_check.isChecked()
