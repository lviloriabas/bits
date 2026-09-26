"""El manifiesto es la memoria del trabajo: no puede corromperse."""

from __future__ import annotations

import json

import pytest

from app.airvault import manifest as manifiestos
from app.airvault.model import (
    EstadoEtapa,
    EstadoRegistro,
    Manifiesto,
    Registro,
)


def _manifiesto():
    return Manifiesto(
        job_id="prueba", nombre_batch="DP | BITS VARIAS 24",
        registros=[Registro(seq=1, matricula="HP-1848CMP",
                            log_number="2287325", fecha="2026/08/31",
                            fleet="NG")],
    )


def test_guardar_y_cargar(tmp_path):
    manifiestos.guardar(_manifiesto(), tmp_path)
    leido = manifiestos.cargar(tmp_path)
    assert leido.job_id == "prueba"
    assert leido.registros[0].log_number == "2287325"


def test_cargar_sin_manifiesto(tmp_path):
    with pytest.raises(FileNotFoundError):
        manifiestos.cargar(tmp_path)


def test_manifiesto_corrupto_no_pasa_por_bueno(tmp_path):
    manifiestos.ruta_manifiesto(tmp_path).parent.mkdir(
        parents=True, exist_ok=True
    )
    manifiestos.ruta_manifiesto(tmp_path).write_text("{roto", encoding="utf-8")
    with pytest.raises(ValueError):
        manifiestos.cargar(tmp_path)


def test_version_desconocida_se_rechaza(tmp_path):
    datos = json.loads(_manifiesto().model_dump_json())
    datos["version"] = 99
    manifiestos.ruta_manifiesto(tmp_path).parent.mkdir(
        parents=True, exist_ok=True
    )
    manifiestos.ruta_manifiesto(tmp_path).write_text(
        json.dumps(datos), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        manifiestos.cargar(tmp_path)


def test_no_deja_temporales(tmp_path):
    manifiestos.guardar(_manifiesto(), tmp_path)
    manifiestos.guardar(_manifiesto(), tmp_path)
    sobras = [p.name for p in tmp_path.iterdir()
              if p.name.startswith(".manifiesto-")]
    assert sobras == []


def test_guardar_espera_a_que_otro_suelte_el_manifiesto(tmp_path, monkeypatch):
    """En Windows reemplazar falla mientras otro tiene el archivo abierto.

    Con un lector concurrente fallaba mas de la mitad de los guardados, y el
    indexado guarda tras cada pagina: uno solo cortaba el batch.
    """
    manifiestos.guardar(_manifiesto(), tmp_path)
    reemplazar = manifiestos.os.replace
    intentos: list[int] = []
    pausas: list[float] = []

    def ocupado(origen, destino):
        intentos.append(1)
        if len(intentos) < 3:
            raise PermissionError(5, "Access is denied")
        reemplazar(origen, destino)

    monkeypatch.setattr(manifiestos.os, "replace", ocupado)
    monkeypatch.setattr(manifiestos.time, "sleep", pausas.append)
    manifiesto = _manifiesto()
    manifiesto.registros[0].estado = EstadoRegistro.ESCRITA

    manifiestos.guardar(manifiesto, tmp_path)

    assert len(intentos) == 3 and len(pausas) == 2
    assert manifiestos.cargar(tmp_path).registros[0].estado is EstadoRegistro.ESCRITA


def test_guardar_se_rinde_si_el_manifiesto_sigue_tomado(tmp_path, monkeypatch):
    def siempre_ocupado(_origen, _destino):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(manifiestos.os, "replace", siempre_ocupado)
    monkeypatch.setattr(manifiestos.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        manifiestos.guardar(_manifiesto(), tmp_path)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".manifiesto-")]


def test_guardar_dos_veces_no_duplica_estado(tmp_path):
    manifiesto = _manifiesto()
    manifiesto.registros[0].estado = EstadoRegistro.ESCRITA
    manifiestos.guardar(manifiesto, tmp_path)
    manifiestos.guardar(manifiestos.cargar(tmp_path), tmp_path)
    assert manifiestos.cargar(tmp_path).registros[0].estado is (
        EstadoRegistro.ESCRITA
    )


def test_etapas_se_crean_pendientes():
    manifiesto = _manifiesto()
    assert manifiesto.etapa("indexar").estado is EstadoEtapa.PENDIENTE
    assert manifiesto.etapa_hecha("indexar") is False


def test_etapa_omitida_cuenta_como_hecha():
    manifiesto = _manifiesto()
    manifiesto.etapa("subir").marcar(EstadoEtapa.OMITIDA, "subida a mano")
    assert manifiesto.etapa_hecha("subir") is True


def test_etapas_previas():
    manifiesto = _manifiesto()
    assert manifiesto.etapas_previas("indexar") == [
        "procesar", "preparar", "subir", "descubrir",
    ]
    assert manifiesto.etapas_previas("inventada") == []


def test_resumen_cuenta_estados():
    manifiesto = _manifiesto()
    manifiesto.registros.append(Registro(seq=2, avisos=["algo"]))
    manifiesto.registros[0].estado = EstadoRegistro.ESCRITA
    resumen = manifiesto.resumen()
    assert resumen["registros"] == 2
    assert resumen["escritos"] == 1
    assert resumen["con_avisos"] == 1
