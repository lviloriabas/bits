"""Regresiones de progreso, confirmacion por muestra y cargas rechazadas."""

from types import SimpleNamespace

import pytest

from app.airvault.config import AirVaultConfig
from app.airvault.flujo import (BUSCANDO, COMPLETADO, PUBLICADO, SIN_SUBIR,
                               EstadoParte, Trabajo, estado_local)
from app.airvault.model import EstadoEtapa, Manifiesto, Registro
from app.airvault.websearch import Consulta, muestra_de
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
def test_busqueda_confirma_muestra_y_persiste(app, tmp_path, cantidad):
    t = trabajo(tmp_path, "buscar", cantidad)
    consultas = []
    buscador = SimpleNamespace(renovar_consultas=lambda: None,
                              publicada=lambda n: consultas.append(n) or Consulta(True))
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [t], "buscador": buscador,
    })
    worker._conectar = lambda: None
    worker._buscar_websearch()
    assert consultas == muestra_de([r.log_number for r in t.manifiesto.registros], 5)
    assert len(consultas) == min(cantidad, 5)
    recargado = Trabajo.cargar(t.config, t.carpeta)
    assert recargado.manifiesto.websearch_confirmado
    assert recargado.manifiesto.etapa_hecha("subir")
    assert estado_local(recargado).estado == PUBLICADO
    assert not estado_local(recargado).se_puede_subir


@pytest.mark.parametrize("respuesta", [False, None])
def test_muestra_parcial_o_sin_respuesta_no_confirma(app, tmp_path, respuesta):
    t = trabajo(tmp_path, "parcial", 50)
    llamadas = []

    def consultar(n):
        llamadas.append(n)
        return Consulta(True if len(llamadas) < 5 else respuesta, "Sin respuesta" if respuesta is None else "")

    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [t],
        "buscador": SimpleNamespace(renovar_consultas=lambda: None, publicada=consultar),
    })
    worker._conectar = lambda: None
    worker._buscar_websearch()
    assert not t.manifiesto.websearch_confirmado
    assert not t.manifiesto.etapa_hecha("subir")


def test_la_busqueda_no_repite_confirmados(app, tmp_path):
    t = trabajo(tmp_path, "confirmado")
    t.manifiesto.websearch_confirmado = "2026-10-09T10:00:00"
    worker = TrabajoAirVaultWorker("buscar_websearch", {
        "buscar_trabajos": [t],
        "buscador": SimpleNamespace(renovar_consultas=lambda: None,
                                   publicada=lambda n: pytest.fail("No debe repetir la consulta")),
    })
    worker._conectar = lambda: None
    worker._buscar_websearch()


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
