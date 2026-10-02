"""Separacion de cargas y recuperacion de mezclas demostradas por contenido."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.airvault.config import CAMPO_BATCH_NAME, CAMPO_LOG_NUMBER
from app.airvault.flujo import ErrorDeCorrida, Trabajo, comprobar_partes, subir_partes
from app.airvault.mezclas import PREFIJO_APARTADO, identificar_partes, recuperar_mezclas
from app.airvault.model import EstadoEtapa
from app.airvault.uploader import serializar_cargas
from tests.airvault_fake import ClienteFalso, lote, pagina
from tests.test_airvault_comprobar import (
    SesionFalsa, _trabajos_principal_division_y_revisar, paginas_ocr,
)


def escenario(tmp_path):
    trabajos = _trabajos_principal_division_y_revisar(tmp_path)[:2]
    paginas = {}
    for indice, trabajo in enumerate(trabajos):
        trabajo.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)
        for numero, registro in enumerate(trabajo.manifiesto.registros):
            registro.log_number = str(2312238 + indice * 2 + numero)
            posicion = indice * 2 + numero + 1
            paginas[posicion] = pagina(posicion, estado=3, valores={CAMPO_LOG_NUMBER: registro.log_number})
        trabajo.guardar()
    cliente = ClienteFalso(lotes=[lote("003MIX", "2026-Batch", 4)], paginas=paginas)
    return trabajos, cliente


def test_aparta_la_mezcla_y_prepara_solo_las_partes_involucradas(tmp_path):
    trabajos, cliente = escenario(tmp_path)
    assert recuperar_mezclas(trabajos, cliente) == trabajos
    assert cliente.renombrados == [("003MIX", PREFIJO_APARTADO + "003MIX")]
    for trabajo in trabajos:
        assert trabajo.manifiesto.batches_descartados == ["003MIX"]
        assert trabajo.manifiesto.resubir_por_mezcla
        assert not trabajo.manifiesto.batch_id
        assert not trabajo.manifiesto.etapa_hecha("subir")
        recargado = Trabajo.cargar(trabajo.config, trabajo.carpeta)
        assert recargado.manifiesto.resubir_por_mezcla
    assert all(p.estado == "sin_subir" for p in comprobar_partes(trabajos, cliente))
    assert cliente.escrituras == []


@pytest.mark.parametrize("caso", ["ajena", "valida", "sin_ocr", "ambiguo", "fuera_de_cola"])
def test_no_aparta_si_no_se_demuestra_la_mezcla_completa(tmp_path, caso):
    trabajos, cliente = escenario(tmp_path)
    if caso == "ajena":
        cliente.paginas[4] = pagina(4, valores={CAMPO_LOG_NUMBER: "9999999"})
    elif caso == "valida":
        cliente.paginas[4] = replace(cliente.paginas[4], estado=0)
    elif caso == "sin_ocr":
        cliente.paginas[4] = pagina(4)
    elif caso == "ambiguo":
        trabajos.append(trabajos[0])
    else:
        trabajos = trabajos[:1]
    assert recuperar_mezclas(trabajos, cliente) == []
    assert cliente.renombrados == []


def test_reanuda_si_airvault_no_confirmo_el_renombrado(tmp_path, monkeypatch):
    trabajos, cliente = escenario(tmp_path)
    renombrar = cliente.renombrar_lote
    monkeypatch.setattr(cliente, "renombrar_lote", lambda *_a: False)
    with pytest.raises(ErrorDeCorrida, match="apartar"):
        recuperar_mezclas(trabajos, cliente)
    assert all(t.manifiesto.etapa_hecha("subir") for t in trabajos)
    assert all(t.manifiesto.mezcla_pendiente == "003MIX" for t in trabajos)
    monkeypatch.setattr(cliente, "renombrar_lote", renombrar)
    assert recuperar_mezclas(trabajos, cliente) == trabajos


def test_no_sube_el_segundo_si_el_primero_sigue_sin_identificar(tmp_path, monkeypatch):
    trabajos, _ = escenario(tmp_path)
    for t in trabajos:
        t.manifiesto.etapas.clear()
        t.guardar()
    cliente = ClienteFalso()
    enviados = []

    def subir(self, *_args, **_kwargs):
        enviados.append(self)
        self.manifiesto.etapa("subir").marcar(EstadoEtapa.HECHA)

    def descubrir(*_args, **_kwargs):
        raise ErrorDeCorrida("sigue procesando")

    monkeypatch.setattr(Trabajo, "subir", subir)
    monkeypatch.setattr(Trabajo, "descubrir", descubrir)
    fallos = subir_partes(trabajos, SesionFalsa(), cliente=cliente)
    assert enviados == trabajos[:1]
    assert len(fallos) == 1
    subir_partes(trabajos, SesionFalsa(), cliente=cliente)
    assert enviados == trabajos[:1]


@pytest.mark.parametrize("nombre", [
    "2026-Batch", "08/09/2026-Batch", "2026-Batch-2",
    "Carga-Batch", "Escaneo-batch", "Archivo-BATCH-2", "-Batch",
])
def test_el_sufijo_batch_se_recupera_por_identidad_y_se_renombra(tmp_path, nombre):
    from app.airvault.flujo import _es_nombre_temporal

    assert _es_nombre_temporal(nombre)
    trabajos, _ = escenario(tmp_path)
    trabajo = trabajos[0]
    cliente = ClienteFalso(lotes=[lote("003NUE", nombre, 2)], paginas=paginas_ocr(trabajo, trabajo.manifiesto.nombre_batch))
    comprobar_partes([trabajo], cliente)
    assert trabajo.manifiesto.batch_id == "003NUE"
    assert cliente.renombrados == [("003NUE", trabajo.manifiesto.nombre_batch)]


@pytest.mark.parametrize("nombre", ["Carga-Batches", "BatchName", "Carga-Batch-revisado"])
def test_batch_dentro_de_otro_nombre_no_se_confunde_con_el_sufijo(nombre):
    from app.airvault.flujo import _es_nombre_temporal

    assert not _es_nombre_temporal(nombre)


def test_recupera_mezclas_con_sufijo_batch_sin_ano(tmp_path):
    trabajos, cliente = escenario(tmp_path)
    cliente.lotes = [lote("003MIX", "Escaneo-Batch", 4)]
    assert recuperar_mezclas(trabajos, cliente) == trabajos


def test_dos_ventanas_no_entran_a_cargar_al_mismo_tiempo():
    dentro = threading.Event()
    liberar = threading.Event()
    segundo = threading.Event()

    @serializar_cargas
    def cargar(nombre, sesion):
        if nombre == "primero":
            dentro.set()
            assert liberar.wait(5)
        else:
            segundo.set()

    with ThreadPoolExecutor(max_workers=2) as ejecutor:
        primero = ejecutor.submit(cargar, "primero", SesionFalsa())
        assert dentro.wait(5)
        siguiente = ejecutor.submit(cargar, "segundo", SesionFalsa())
        try:
            assert not segundo.wait(0.1)
        finally:
            liberar.set()
        primero.result()
        siguiente.result()
    assert segundo.is_set()


def test_subir_recupera_la_mezcla_y_publica_dos_batches_independientes(tmp_path, monkeypatch):
    from app.airvault.uploader import ResultadoSubida, SubidorQuickUpload

    trabajos, cliente = escenario(tmp_path)
    enviados = []

    def subir(self, archivo, valores, avisar=None):
        nombre = valores[CAMPO_BATCH_NAME]
        trabajo = next(t for t in trabajos if t.manifiesto.nombre_batch == nombre)
        batch_id = f"003NUE{len(enviados)}"
        enviados.append(nombre)
        cliente.lotes.append(lote(batch_id, "2026-Batch", 2))
        cliente.paginas_por_lote[batch_id] = paginas_ocr(trabajo, nombre)
        return ResultadoSubida(str(archivo), True)

    monkeypatch.setattr(SubidorQuickUpload, "subir", subir)
    monkeypatch.setattr("app.airvault.flujo._paginas_del_pdf", lambda _p: 2)
    # Apartar la mezcla conserva los originales remotos: no se reenvian
    # los mismos PDF hasta que la persona acepte la advertencia.
    fallos = subir_partes(trabajos, SesionFalsa(), cliente=cliente)
    assert len(fallos) == 2
    assert enviados == []
    from app.airvault.flujo import autorizar_posible_duplicado
    for trabajo in trabajos:
        autorizar_posible_duplicado(trabajo)
    assert subir_partes(trabajos, SesionFalsa(), cliente=cliente) == []
    assert enviados == [t.manifiesto.nombre_batch for t in trabajos]
    assert [t.manifiesto.batch_id for t in trabajos] == ["003NUE0", "003NUE1"]
    assert all(not t.manifiesto.resubir_por_mezcla for t in trabajos)
    assert cliente.lotes[0].nombre == PREFIJO_APARTADO + "003MIX"


def test_no_escribe_si_airvault_junta_paginas_despues_de_planificar(tmp_path):
    from app.airvault.guards import ErrorDeGuarda

    trabajos, _ = escenario(tmp_path)
    trabajo = trabajos[0]
    trabajo.fijar_lote("003UNO")
    cli = ClienteFalso(page_count=2)
    plan, indexador = trabajo.planificar(cli)
    cli.page_count = 4
    with pytest.raises(ErrorDeGuarda, match="4 paginas"):
        trabajo.indexar(indexador, plan)
    assert cli.escrituras == []
    assert trabajo._tomado is False


def test_la_recuperacion_no_se_repite_indefinidamente(tmp_path):
    trabajos, cliente = escenario(tmp_path)
    trabajos[0].manifiesto.batches_descartados = ["003ANT1", "003ANT2"]
    with pytest.raises(ErrorDeCorrida, match="dos recuperaciones"):
        recuperar_mezclas(trabajos, cliente)
    assert cliente.renombrados == []
