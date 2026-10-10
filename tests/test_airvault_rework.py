"""Regresiones de progreso, confirmacion por identidad y cargas rechazadas."""

from types import SimpleNamespace

import pytest

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (BUSCANDO, COMPLETADO, PUBLICADO, SIN_SUBIR,
                               EstadoParte, Trabajo, estado_local)
from app.airvault.model import EstadoEtapa, Manifiesto, Registro
from app.gui.airvault_window import AirVaultWindow, TrabajoAirVaultWorker


def trabajo(raiz, nombre, cantidad=10):
    manifiesto = Manifiesto(
        job_id=nombre, nombre_batch=nombre, csv_origen=str(raiz / nombre / "reporte.csv"),
        registros=[Registro(seq=i + 1, log_number=str(2000000 + i)) for i in range(cantidad)],
    )
    t = Trabajo(AirVaultConfig(), raiz / "output" / "airvault" / nombre, manifiesto)
    t.guardar()
    return t


def test_sidebar_recupera_completados_y_pendientes_sin_elegir_ejecucion(app, tmp_path):
    pendiente = trabajo(tmp_path, "pendiente")
    terminado = trabajo(tmp_path, "terminado")
    terminado.manifiesto.etapa("completar").marcar(EstadoEtapa.HECHA)
    terminado.guardar()
    ventana = AirVaultWindow(tmp_path)
    assert {p.nombre for p in ventana._estados} == {"pendiente", "terminado"}
    assert ventana.lotes.count() == 2
    assert ventana._trabajos[0].carpeta == pendiente.carpeta


def test_los_campos_numericos_tienen_sitio_para_el_texto(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana.show()
    app.processEvents()
    for campo in (ventana.limite_batch_spin, ventana.minutos_spin, ventana.porcentaje_duplicados_spin):
        assert campo.lineEdit().width() >= campo.fontMetrics().horizontalAdvance(campo.text())


def test_porcentaje_pesa_paginas_y_no_batches(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    chico, grande = trabajo(tmp_path, "chico", 1), trabajo(tmp_path, "grande", 99)
    ventana._estados = [EstadoParte(chico, COMPLETADO), EstadoParte(grande, SIN_SUBIR)]
    assert ventana._avance_global() == pytest.approx(0.01)


def test_reintento_no_borra_el_porcentaje_del_batch(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    t = trabajo(tmp_path, "carga", 100)
    ventana._estados = [EstadoParte(t, SIN_SUBIR)]
    ventana._en_vuelo[str(t.carpeta)] = ("subir", 0.9)
    antes = ventana._avance_global()
    ventana._en_vuelo.clear()
    assert ventana._avance_global() == antes
    ventana._en_vuelo[str(t.carpeta)] = ("subir", 0.1)
    assert ventana._avance_global() == antes


def test_el_filtro_visual_no_anuncia_fin_prematuro(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    uno, dos = trabajo(tmp_path, "uno"), trabajo(tmp_path, "dos")
    ventana._estados = [EstadoParte(uno, COMPLETADO), EstadoParte(dos, BUSCANDO)]
    ventana._alcance_proceso = {str(uno.carpeta), str(dos.carpeta)}
    ventana._corrida = uno.manifiesto.csv_origen
    ventana.solo_ejecucion_check.setChecked(True)
    assert ventana.lotes.count() == 1
    assert ventana._firma_de_fin() is None
    assert ventana.progreso.value() < 100


def test_repintar_preserva_seleccion_por_identidad(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    uno, dos = trabajo(tmp_path, "uno"), trabajo(tmp_path, "dos")
    ventana._estados = [estado_local(uno), estado_local(dos)]
    ventana._pintar_lotes()
    ventana.lotes.selectRow(1)
    ventana._estados.reverse()
    ventana._pintar_lotes()
    assert ventana._seleccionadas()[0].trabajo is dos


@pytest.mark.parametrize("cantidad", [1, 2, 5, 100, 1000])
def test_busqueda_confirma_carga_completa_y_persiste(app, tmp_path, cantidad):
    from app.airvault.confirmacion import muestra_de_batch
    from tests.test_airvault_confirmacion import caso
    t, buscador, _, consultas = caso(tmp_path, cantidad)
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [t], "buscador": buscador,
    })
    worker._conectar = lambda: None
    worker._buscar_websearch()
    assert len(consultas) == len(muestra_de_batch(t.manifiesto))
    recargado = Trabajo.cargar(t.config, t.carpeta)
    assert recargado.manifiesto.websearch_confirmado
    assert recargado.manifiesto.etapa_hecha("subir")
    assert recargado.manifiesto.etapa_hecha("completar")
    assert estado_local(recargado).estado == COMPLETADO
    assert not estado_local(recargado).se_puede_subir


def test_respuesta_parcial_no_confirma(app, tmp_path):
    from tests.test_airvault_confirmacion import caso
    t, buscador, filas, _ = caso(tmp_path, 50)
    filas.pop()
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [t], "buscador": buscador,
    })
    worker._conectar = lambda: None
    worker._buscar_websearch()
    assert not t.manifiesto.websearch_confirmado
    assert not t.manifiesto.etapa_hecha("subir")


@pytest.mark.parametrize("con_sesion", [True, False])
def test_confirmar_solo_consulta_websearch_y_no_prepara_el_cliente_de_index(app, tmp_path, monkeypatch, con_sesion):
    from app.airvault.websearch import Buscador
    from tests.test_airvault_confirmacion import caso
    t, b, _, consultas = caso(tmp_path, 1000)
    config = t.config.with_overrides(ruta_websearch=b.ruta, parametros_websearch=b._plantilla)
    rutas = []
    get_original = b.sesion.get

    def get(ruta, parametros):
        rutas.append(ruta)
        return get_original(ruta, parametros)

    b.sesion.get = get
    estado = {"buscar_trabajos": [t], "config": config}
    if con_sesion:
        estado["sesion"] = b.sesion
    else:
        monkeypatch.setattr("app.airvault.session.abrir_sesion", lambda *args, **kwargs: b.sesion)
    worker = TrabajoAirVaultWorker("buscar_websearch", estado)
    worker._preparar_buscador = lambda: estado.update(buscador=Buscador(
        estado["sesion"], config, _ruta=b.ruta, _plantilla=b._plantilla))
    worker._conectar = lambda: pytest.fail("La confirmacion no consulta Web Index")
    worker._buscar_websearch()
    assert len(consultas) == 15
    assert all(ruta.startswith("/zfp/") for ruta in rutas)
    assert t.manifiesto.etapa_hecha("completar")
    assert "cliente" not in estado


def test_la_muestra_completa_la_fila_y_el_progreso_sin_otra_revision(app, tmp_path, monkeypatch):
    from app.airvault.flujo import INDEXADO
    from tests.test_airvault_confirmacion import caso
    t, b, _, _ = caso(tmp_path)
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [t]
    ventana._estados = [EstadoParte(t, INDEXADO)]
    ventana.completar_check.setChecked(True)
    worker = TrabajoAirVaultWorker("buscar_websearch", {"buscar_trabajos": [t], "buscador": b})
    worker.buscado.connect(ventana._al_buscar_websearch)
    worker._buscar_websearch()
    monkeypatch.setattr(ventana, "_comprobar", lambda: pytest.fail("No se vuelve a revisar Web Index"))
    ventana._al_terminar()
    assert ventana._estados[0].estado == COMPLETADO
    assert ventana.progreso.value() == 100
    assert ventana.estado_label.text() == "Proceso terminado"


@pytest.mark.parametrize("automatico", [True, False])
def test_la_consulta_manual_puede_renovar_su_sesion_de_lectura(app, tmp_path, automatico):
    from app.airvault.websearch import Buscador
    from tests.test_airvault_confirmacion import caso
    t, b, _, _ = caso(tmp_path, 10)
    clones = []

    def clonar(renovable):
        clones.append(renovable)
        return SimpleNamespace(config=t.config, get=b.sesion.get,
                               http=SimpleNamespace(close=lambda: None))

    buscador = Buscador(SimpleNamespace(clonar=clonar), t.config,
                        _ruta=b.ruta, _plantilla=b._plantilla)
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [t], "buscador": buscador, "confirmacion_automatica": automatico,
    })
    worker._buscar_websearch()
    assert clones == [not automatico]
    assert t.manifiesto.etapa_hecha("completar")


def test_la_busqueda_no_repite_confirmados(app, tmp_path):
    from tests.test_airvault_confirmacion import caso
    t, buscador, _, consultas = caso(tmp_path)
    t.manifiesto.websearch_confirmado = "2026-10-09T10:00:00"
    t.manifiesto.websearch_metodo = "completo_por_identidad"
    t.manifiesto.websearch_batch_id = "B01"
    from app.airvault.confirmacion import huella_de_batch
    t.manifiesto.websearch_huella = huella_de_batch(t.manifiesto)
    t.manifiesto.websearch_cotejadas = len(t.manifiesto.bitacoras())
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [t], "buscador": buscador,
    })
    worker._conectar = lambda: None
    worker._buscar_websearch()
    assert consultas == []


@pytest.mark.parametrize("etapa", ["Upload/", "FinishUpload"])
def test_http_200_con_rechazo_no_se_cuenta_subido(tmp_path, etapa):
    from app.airvault.uploader import SubidorQuickUpload
    archivo = tmp_path / "batch.pdf"
    archivo.write_bytes(b"%PDF")
    rutas = []

    def post(ruta, **kwargs):
        rutas.append(ruta)
        return SimpleNamespace(json=lambda: {"success": False, "message": "rechazado"}
                               if ruta.endswith(etapa) else {"success": True})

    with pytest.raises(RuntimeError, match="rechazó"):
        SubidorQuickUpload(SimpleNamespace(post=post), 3209).subir(archivo, {})
    if etapa == "Upload/":
        assert not any(r.endswith("FinishUpload") for r in rutas)


def test_ultimo_trozo_confirma_avance_completo(tmp_path):
    from app.airvault.uploader import SubidorQuickUpload, TROZO_BYTES
    archivo = tmp_path / "grande.pdf"
    archivo.write_bytes(b"x" * (TROZO_BYTES + 1))
    pasos = []
    resultado = SubidorQuickUpload(SimpleNamespace(post=lambda *a, **k: None), 3209).subir(
        archivo, {}, lambda texto, hechas, total: pasos.append((hechas, total)))
    assert resultado.ok
    assert (2, 2) in pasos


def test_el_cierre_fallido_no_entra_en_un_ciclo_inmediato(app, tmp_path):
    from app.airvault.flujo import INDEXADO
    ventana = AirVaultWindow(tmp_path)
    t = trabajo(tmp_path, "cerrar")
    ventana._estados = [EstadoParte(t, INDEXADO)]
    ventana.completar_check.setChecked(True)
    datos = {"cierres": [(t, SimpleNamespace(completado=False))]}
    ventana._anotar_cierres(datos)
    assert ventana._por_completar(automatico=True) == []
    assert ventana._por_completar() == [t]
    assert ventana._falta_esperar()
    ventana._anotar_cierres(datos)
    ventana._anotar_cierres(datos)
    assert not ventana._falta_esperar()
    assert ventana._firma_de_fin() is None


def test_el_prefijo_distingue_partes_de_ejecuciones_diferentes(tmp_path):
    from app.airvault.flujo import _prefijo
    uno, dos = trabajo(tmp_path, "ayer"), trabajo(tmp_path, "hoy")
    uno.manifiesto.partes = dos.manifiesto.partes = 3
    uno.manifiesto.parte = dos.manifiesto.parte = 1
    assert _prefijo(uno) != _prefijo(dos)


def test_subir_sin_indexado_termina_al_quedar_listo(app, tmp_path):
    from app.airvault.flujo import LISTO
    from app.gui.automatizacion import INDEXAR
    ventana = AirVaultWindow(tmp_path)
    ventana._opciones.fijar(INDEXAR, False)
    t = trabajo(tmp_path, "solo-subir")
    ventana._estados = [EstadoParte(t, LISTO)]
    ventana._fin_pendiente = True
    ventana._anunciar_fin()
    assert ventana.progreso.value() == 100


def test_muestra_confirmada_no_esconde_un_error_de_upload(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    t = trabajo(tmp_path, "confirmado")
    t.manifiesto.websearch_confirmado = "2026-10-09T10:00:00"
    ventana._estados = [estado_local(t)]
    ventana._al_fallar("No respondió AirVault")
    ventana._al_terminar()
    assert ventana.progreso.value() < 100


def test_sidebar_pequeno_crece_solo_cuando_el_usuario_lo_redimensiona(app, tmp_path):
    ventana = AirVaultWindow(tmp_path)
    ventana.show()
    app.processEvents()
    assert 220 <= ventana.panel_batches.width() <= 240
    ancho = ventana.panel_batches.width()
    ventana.resize(1280, 720)
    app.processEvents()
    assert ancho <= ventana.panel_batches.width() <= 240
    ventana.divisor_batches.setSizes([350, 900])
    app.processEvents()
    assert ventana.panel_batches.width() > ancho
    assert ventana.panel_batches.isAncestorOf(ventana.progreso)


def test_filtros_ocultan_solo_la_vista_y_conservan_la_meta(app, tmp_path):
    from app.airvault.flujo import CANCELADO, INDEXADO
    ventana = AirVaultWindow(tmp_path)
    terminado, indexado, pendiente, cancelado = [trabajo(tmp_path, n) for n in ("terminado", "indexado", "pendiente", "cancelado")]
    ventana._estados = [EstadoParte(terminado, COMPLETADO), EstadoParte(indexado, INDEXADO),
                        EstadoParte(pendiente, SIN_SUBIR), EstadoParte(cancelado, CANCELADO)]
    antes = ventana._avance_global()
    ventana._pintar_lotes()
    assert ventana.ocultar_indexados_check.text() == "Ocultar batches indexados"
    assert ventana.ocultar_completados_check.text() == "Ocultar batches completados"
    assert ventana.lotes.count() == 4
    ventana.ocultar_indexados_check.setChecked(True)
    assert [p.trabajo for p in ventana._partes_en_cola()] == [terminado, pendiente, cancelado]
    ventana.ocultar_indexados_check.setChecked(False)
    ventana.ocultar_completados_check.setChecked(True)
    assert [p.trabajo for p in ventana._partes_en_cola()] == [indexado, pendiente, cancelado]
    ventana.ocultar_indexados_check.setChecked(True)
    assert [p.trabajo for p in ventana._partes_en_cola()] == [pendiente, cancelado]
    assert ventana._ejecucion() == [terminado, indexado, pendiente, cancelado]
    assert ventana._avance_global() == antes
    assert ventana._firma_de_fin() is None
    ventana._habilitar(False)
    assert ventana.ocultar_completados_check.isEnabled() and ventana.ocultar_indexados_check.isEnabled()
    otra = AirVaultWindow(tmp_path)
    assert otra.ocultar_completados_check.isChecked() and otra.ocultar_indexados_check.isChecked()


def test_ocultar_indexados_sin_completar_conserva_cien_por_ciento(app, tmp_path):
    from app.airvault.flujo import INDEXADO
    ventana = AirVaultWindow(tmp_path)
    t = trabajo(tmp_path, "indexado")
    ventana._estados = [EstadoParte(t, INDEXADO)]
    ventana.completar_check.setChecked(False)
    ventana._fin_pendiente = True
    ventana._anunciar_fin()
    ventana.ocultar_indexados_check.setChecked(True)
    assert ventana.lotes.count() == 0
    assert ventana.progreso.value() == 100


def test_revision_periodica_recorre_dos_antiguos_sin_bloquear_una_subida(app, tmp_path, monkeypatch):
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [trabajo(tmp_path, n) for n in ("primero", "segundo", "tercero")]
    for t in ventana._trabajos:
        t.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
    ventana._trabajos[0].manifiesto.websearch_revision = "2026-10-09T10:00:00"
    ventana.auto_check.setChecked(True)
    ventana._ajustar_confirmacion()
    assert ventana._confirmador.isActive()
    lanzamientos = []
    monkeypatch.setattr(ventana, "_lanzar", lambda modo, estado: lanzamientos.append((modo, dict(estado))))
    monkeypatch.setattr(ventana, "hilo", lambda: object())
    ventana._confirmar_solo()
    assert not lanzamientos
    monkeypatch.setattr(ventana, "hilo", lambda: None)
    ventana._confirmar_solo()
    modo, estado = lanzamientos[0]
    assert modo == "buscar_websearch"
    assert estado["buscar_trabajos"] == ventana._trabajos[1:]
    assert estado["confirmacion_automatica"] and estado["limite_confirmacion"] == 2
    ventana.auto_check.setChecked(False)
    assert not ventana._confirmador.isActive()


def test_busqueda_manual_incluye_todos_los_batches_ocultos(app, tmp_path, monkeypatch):
    ventana = AirVaultWindow(tmp_path)
    ventana._trabajos = [trabajo(tmp_path, n) for n in ("a", "b", "c")]
    ventana._estados = [EstadoParte(t, COMPLETADO) for t in ventana._trabajos]
    ventana.ocultar_completados_check.setChecked(True)
    assert ventana.lotes.count() == 0
    peticiones = []
    monkeypatch.setattr(ventana, "_encolar", lambda *args: peticiones.append(args))
    ventana._buscar_websearch()
    assert peticiones[0][1] == ventana._trabajos


def test_confirmacion_automatica_no_hace_retroceder_el_progreso_final(app, tmp_path, monkeypatch):
    from app.airvault.flujo import INDEXADO
    ventana = AirVaultWindow(tmp_path)
    t = trabajo(tmp_path, "indexado")
    ventana._estados = [EstadoParte(t, INDEXADO)]
    ventana._fin_pendiente = True
    ventana._anunciar_fin()
    assert ventana.progreso.value() == 100
    monkeypatch.setattr(ventana, "hilo", lambda: SimpleNamespace(modo="buscar_websearch",
                                                               estado={"confirmacion_automatica": True}))
    ventana._pintar_avance()
    assert ventana.progreso.value() == 100
    ventana._al_fallar("Web Search no respondió")
    assert ventana.progreso.value() == 100
    assert "Web Search pendiente" in ventana.resumen.text()


def test_confirmar_un_batch_indexado_mantiene_el_progreso_final(app, tmp_path, monkeypatch):
    from app.airvault.flujo import INDEXADO
    from app.airvault.confirmacion import huella_de_batch
    ventana = AirVaultWindow(tmp_path)
    t = trabajo(tmp_path, "indexado")
    ventana._trabajos = [t]
    ventana._estados = [EstadoParte(t, INDEXADO)]
    ventana._fin_pendiente = True
    ventana._anunciar_fin()
    t.manifiesto.batch_id = "B01"
    t.manifiesto.websearch_confirmado = "2026-10-09T10:00:00"
    t.manifiesto.websearch_metodo = "completo_por_identidad"
    t.manifiesto.websearch_batch_id = "b01"
    t.manifiesto.websearch_cotejadas = len(t.manifiesto.bitacoras())
    t.manifiesto.websearch_huella = huella_de_batch(t.manifiesto)
    monkeypatch.setattr(ventana, "hilo", lambda: SimpleNamespace(modo="buscar_websearch",
                                                               estado={"confirmacion_automatica": True}))
    ventana._al_buscar_websearch({"resultados": [(t, True)], "automatico": True})
    assert ventana._estados[0].estado == PUBLICADO
    assert ventana.progreso.value() == 100
