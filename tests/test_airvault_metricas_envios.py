"""Fechas y tiempos de cada envio, con conteos separados y total durable."""

from datetime import datetime

import pytest

from app.airvault import flujo, manifest, uploader
from app.airvault.config import AirVaultConfig
from app.airvault.flujo import Trabajo, anotar_reenvio, reiniciar_subida, subir_partes
from app.airvault.model import MAXIMO_HISTORIAL, Manifiesto
from tests.test_airvault_trabajo import SubidorFalso, cliente_con_lote, corrida


def preparar(tmp_path, monkeypatch, subidor=None):
    monkeypatch.setattr(uploader, "SubidorQuickUpload", subidor or SubidorFalso())
    return Trabajo.preparar(
        AirVaultConfig(), tmp_path / "job", corrida(tmp_path),
        "DP | BIT 18 AUG 2026 05 42",
    )


@pytest.mark.parametrize("manual", [False, True], ids=["automatico", "manual"])
def test_cuenta_el_origen_real_de_la_subida_y_no_la_repite(tmp_path, monkeypatch, manual):
    trabajo = preparar(tmp_path, monkeypatch)
    forzados = [str(trabajo.carpeta)] if manual else []

    assert subir_partes([trabajo], object(), forzados=forzados) == []
    trabajo.subir(object())

    envios = manifest.cargar(trabajo.carpeta).envios
    assert envios.total == 1
    assert envios.manuales == int(manual)
    assert envios.automaticos == int(not manual)
    assert envios.sin_clasificar == 0
    assert envios.historial[0].tiempos.subido
    assert not trabajo._envio_automatico


def test_conserva_fechas_y_tiempos_junto_a_cada_reenvio(tmp_path, monkeypatch):
    class Reloj(datetime):
        actual = datetime(2026, 10, 18, 10, 0)

        @classmethod
        def now(cls):
            return cls.actual

    monkeypatch.setattr(flujo, "datetime", Reloj)
    trabajo = preparar(tmp_path, monkeypatch)
    trabajo.subir(object())
    Reloj.actual = datetime(2026, 10, 18, 10, 5)
    trabajo.descubrir(cliente_con_lote(), esperar=False)
    trabajo.manifiesto.tiempos.indexacion_s = 20
    trabajo.guardar()
    primero = manifest.cargar(trabajo.carpeta).envios.historial[0]
    assert primero.automatico is False
    assert primero.tiempos.subido == "2026-10-18T10:00:00.000000"
    assert primero.tiempos.disponible_index == "2026-10-18T10:05:00.000000"
    assert primero.tiempos.espera_index_s == 300
    assert primero.tiempos.indexacion_por_pagina_s == 10
    assert primero.batch_id == "003SRO"

    # La autorizacion todavia no equivale a otro envio.
    anotar_reenvio(trabajo, automatico=True)
    assert manifest.cargar(trabajo.carpeta).envios.total == 1
    reiniciar_subida(trabajo)
    trabajo = Trabajo.cargar(trabajo.config, trabajo.carpeta)
    trabajo._duplicado_permitido = True
    Reloj.actual = datetime(2026, 10, 18, 14, 0)
    trabajo.subir(object())

    guardado = manifest.cargar(trabajo.carpeta)
    assert guardado.envios.total == 2
    assert guardado.envios.automaticos == guardado.envios.manuales == 1
    assert guardado.envios.historial[0] == primero
    assert guardado.envios.historial[1].tiempos.subido == "2026-10-18T14:00:00.000000"
    assert guardado.envios.historial[1].tiempos.disponible_index == ""
    assert guardado.envios.historial[1].tiempos.indexacion_s is None
    assert guardado.model_dump()["envios"]["total"] == 2


@pytest.mark.parametrize("finish_enviado", [False, True])
def test_una_interrupcion_solo_cuenta_si_la_carga_pudo_llegar(
    tmp_path, monkeypatch, finish_enviado,
):
    from tests.test_airvault_interrupciones import SubidorQueSeCorta

    subidor = SubidorQueSeCorta(RuntimeError("conexion cortada"), finish_enviado)
    trabajo = preparar(tmp_path, monkeypatch, subidor)

    with pytest.raises(RuntimeError, match="conexion cortada"):
        trabajo.subir(object())

    assert manifest.cargar(trabajo.carpeta).envios.total == int(finish_enviado)


def test_una_respuesta_perdida_cuenta_un_solo_envio(tmp_path, monkeypatch):
    class SinRespuesta(SubidorFalso):
        def subir(self, ruta, valores, avisar=None):
            return uploader.ResultadoSubida(str(ruta), True, confirmada=False)

    trabajo = preparar(tmp_path, monkeypatch, SinRespuesta())
    trabajo.subir(object())
    assert manifest.cargar(trabajo.carpeta).envios.total == 1
    trabajo.descubrir(cliente_con_lote(), esperar=False)
    envios = manifest.cargar(trabajo.carpeta).envios
    assert envios.total == 1
    assert envios.historial[0].resultado == "sin_respuesta"
    assert envios.historial[0].tiempos.disponible_index


def test_una_carga_rechazada_no_cuenta_como_envio(tmp_path, monkeypatch):
    class Rechazada(SubidorFalso):
        def subir(self, ruta, valores, avisar=None):
            return uploader.ResultadoSubida(str(ruta), False, "rechazada")

    trabajo = preparar(tmp_path, monkeypatch, Rechazada())
    with pytest.raises(flujo.ErrorDeCorrida, match="rechazada"):
        trabajo.subir(object())
    assert manifest.cargar(trabajo.carpeta).envios.total == 0


def test_los_conteos_no_dependen_del_historial_recortado(tmp_path):
    m = Manifiesto(job_id="envios")
    for numero in range(MAXIMO_HISTORIAL + 2):
        m.registrar_envio(numero % 2 == 0, "aceptada", f"envio-{numero}")
        m.anotar("subida", "aceptada")
    manifest.guardar(m, tmp_path)

    envios = manifest.cargar(tmp_path).envios
    assert envios.total == MAXIMO_HISTORIAL + 2
    assert envios.automaticos == envios.manuales == (MAXIMO_HISTORIAL + 2) // 2


def test_recupera_envios_antiguos_sin_inventar_el_origen():
    datos = {
        "job_id": "anterior", "historial": [
            {"paso": "subida", "resultado": "aceptada", "momento": "2026-10-17T10:00:00"},
            {"paso": "deteccion", "resultado": "encontrado", "momento": "2026-10-17T10:05:00",
             "detalle": "003SRO (2 paginas)"},
            {"paso": "reenvio", "resultado": "automatico"},
            {"paso": "subida", "resultado": "no_enviada"},
            {"paso": "subida", "resultado": "sin_respuesta", "momento": "2026-10-17T14:00:00"},
            {"paso": "reenvio", "resultado": "manual"},
            {"paso": "subida", "resultado": "aceptada", "momento": "2026-10-18T10:00:00"},
        ],
    }
    m = Manifiesto.model_validate(datos)
    assert m.envios.total == 3
    assert m.envios.automaticos == m.envios.manuales == m.envios.sin_clasificar == 1
    assert m.envios.historial[0].tiempos.disponible_index == "2026-10-17T10:05:00"
    assert m.envios.historial[0].batch_id == "003SRO"
    assert Manifiesto.model_validate_json(m.model_dump_json()).envios == m.envios
